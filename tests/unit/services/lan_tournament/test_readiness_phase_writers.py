"""Pure transaction-owner checks for seeding and qualification writers."""

from datetime import datetime, UTC
from functools import partial
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as engine,
    tournament_qualification_service as qualification,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
)
from byceps.services.lan_tournament.models.seeding import SeedingFormat, SeedingState
from byceps.services.lan_tournament.events import MatchDeletedEvent
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


@pytest.fixture
def writer(monkeypatch, request):
    operation = request.param
    tournament_id, actor_id = uuid7(), uuid7()
    trace = []
    for name, label in [
        ('lock_tournament_for_update', 'lock'),
        ('commit_session', 'commit'),
        ('rollback_session', 'rollback'),
    ]:
        monkeypatch.setattr(repo, name, Mock(side_effect=lambda *a, label=label: trace.append(label)))
    outcome = engine.GenerationOutcome(
        count=3, created_events=[], deleted_events=[],
        ready_match_ids=frozenset(), occurred_at=datetime.now(UTC),
    )
    monkeypatch.setattr(engine, 'dispatch_generation_events', Mock(side_effect=lambda *a: trace.append('dispatch')))
    if operation == 'generate':
        module, name = seeding, '_generate_locked'
        call = partial(seeding.generate_from_seeding, tournament_id, expected_version=1, initiator_id=actor_id)
    elif operation == 'release':
        module, name = qualification, '_release_locked'
        call = partial(qualification.release_playoffs, tournament_id, expected_version=1, initiator_id=actor_id)
    elif operation == 'auto':
        monkeypatch.setattr(repo, 'find_tournament', Mock(return_value=object()))
        monkeypatch.setattr(qualification, '_manual_draft_due', Mock(return_value=False))
        monkeypatch.setattr(qualification, '_auto_release_due', Mock(return_value=True))
        module, name = qualification, '_auto_release_locked'
        call = partial(qualification.try_auto_release, tournament_id, triggered_by=actor_id)
    else:
        module, name = qualification, '_unrelease_locked'
        call = partial(qualification.unrelease_playoffs, tournament_id, reason='Reset phase', initiator_id=actor_id)
    staged = Mock(side_effect=lambda *a, **kw: (trace.append('stage'), Ok([] if operation == 'unrelease' else outcome))[1])
    monkeypatch.setattr(module, name, staged)
    return call, staged, trace, operation


@pytest.mark.parametrize('writer', ['generate', 'release', 'auto', 'unrelease'], indirect=True)
def test_generation_and_release_effects_are_post_commit(writer):
    call, _, trace, operation = writer
    assert call().is_ok()
    assert trace == ['lock', 'stage', 'commit'] + ([] if operation == 'unrelease' else ['dispatch'])


@pytest.mark.parametrize('writer', ['generate', 'release', 'auto', 'unrelease'], indirect=True)
def test_writer_errors_rollback_without_effects(writer):
    call, staged, trace, _ = writer
    staged.side_effect = None
    staged.return_value = Err('audit_failed')
    result = call()
    assert result.is_err() and result.unwrap_err() == 'audit_failed'
    assert trace == ['lock', 'rollback']


@pytest.mark.parametrize('writer', ['generate', 'release', 'auto', 'unrelease'], indirect=True)
def test_writer_exceptions_rollback_without_effects(writer):
    call, staged, trace, _ = writer
    staged.side_effect = RuntimeError('audit failed')
    with pytest.raises(RuntimeError, match='audit failed'):
        call()
    assert trace == ['lock', 'rollback']


@pytest.mark.parametrize('writer', ['generate', 'release', 'auto', 'unrelease'], indirect=True)
def test_commit_failure_rolls_back_without_dispatch(writer, monkeypatch):
    call, _, trace, _ = writer
    monkeypatch.setattr(repo, 'commit_session', Mock(side_effect=RuntimeError('commit failed')))
    with pytest.raises(RuntimeError, match='commit failed'):
        call()
    assert trace == ['lock', 'stage', 'rollback']


@pytest.mark.parametrize('writer', ['generate', 'release', 'auto'], indirect=True)
def test_postcommit_dispatch_failure_does_not_rollback(writer, monkeypatch):
    call, _, trace, _ = writer
    monkeypatch.setattr(engine, 'dispatch_generation_events', Mock(side_effect=RuntimeError('effect failed')))
    with pytest.raises(RuntimeError, match='effect failed'):
        call()
    assert trace == ['lock', 'stage', 'commit']


def test_locked_qualification_read_is_fresh(monkeypatch):
    tournament_id = uuid7()
    fresh = object()
    monkeypatch.setattr(repo, 'find_tournament', Mock(return_value=object()))
    reader = Mock(return_value=fresh)
    monkeypatch.setattr(repo, 'get_tournament', reader)
    assert qualification._find_locked_tournament(tournament_id) is fresh
    reader.assert_called_once_with(tournament_id, fresh=True)


# fmt: off
@pytest.mark.parametrize(('format_', 'helper'), [
    (SeedingFormat.SINGLE_ELIMINATION, '_generate_single_elimination_impl'),
    (SeedingFormat.DOUBLE_ELIMINATION, '_generate_double_elimination_impl'),
    (SeedingFormat.ROUND_ROBIN, '_generate_round_robin_impl'),
    (SeedingFormat.FREE_FOR_ALL, '_generate_ffa_initial_impl'),
])
# fmt: on
def test_generation_adapter_is_flush_only(monkeypatch, format_, helper):
    state = SeedingState(
        format=format_, param=2, tier_count=1, roster=('a', 'b', 'c', 'd'),
        tiers=(0, 0, 0, 0), seed_list=('a', 'b', 'c', 'd'),
        layout=('a', 'b', 'c', 'd'), draw_seed=1,
    )
    outcome = object()
    delegate = Mock(return_value=Ok(outcome))
    commit = Mock(side_effect=AssertionError('adapter must not commit'))
    dispatch = Mock(side_effect=AssertionError('adapter must not dispatch'))
    monkeypatch.setattr(engine, helper, delegate)
    monkeypatch.setattr(repo, 'commit_session', commit)
    monkeypatch.setattr(engine, 'dispatch_generation_events', dispatch)
    assert seeding._run_generator(uuid7(), state, False, uuid7()).unwrap() is outcome
    delegate.assert_called_once()
    assert delegate.call_args.kwargs['seeding_target'] == seeding.INITIAL_TARGET
    commit.assert_not_called()
    dispatch.assert_not_called()


def test_qualification_missing_locked_tournament_retains_error(monkeypatch):
    monkeypatch.setattr(repo, 'find_tournament', Mock(return_value=None))
    reader = Mock(side_effect=AssertionError('missing tournament'))
    monkeypatch.setattr(repo, 'get_tournament', reader)
    result = qualification._unrelease_locked(uuid7(), 'Reset phase', uuid7())
    assert result.is_err() and result.unwrap_err() == 'Tournament not found.'
    reader.assert_not_called()


def test_stage_generation_leaves_commit_and_effects_to_caller(monkeypatch):
    tournament_id, actor_id = uuid7(), uuid7()
    outcome = object()
    staged = Mock(return_value=Ok(outcome))
    monkeypatch.setattr(seeding, '_generate_locked', staged)
    commit = Mock(side_effect=AssertionError('flush helper committed'))
    dispatch = Mock(side_effect=AssertionError('flush helper dispatched'))
    monkeypatch.setattr(repo, 'commit_session', commit)
    monkeypatch.setattr(engine, 'dispatch_generation_events', dispatch)
    result = seeding.stage_generation(tournament_id, seeding.PLAYOFF_TARGET,
        expected_version=2, initiator_id=actor_id)
    assert result.unwrap() is outcome
    staged.assert_called_once_with(tournament_id, seeding.PLAYOFF_TARGET, 2,
        actor_id, require_released=False, confirmer_id=None)
    commit.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.parametrize('writer', ['unrelease'], indirect=True)
def test_unrelease_deletion_signal_follows_commit(writer, monkeypatch):
    call, staged, trace, _ = writer
    event = MatchDeletedEvent(occurred_at=datetime.now(UTC), initiator=None,
        tournament_id=uuid7(), match_id=uuid7())
    staged.side_effect = None
    staged.return_value = Ok([event])
    send = Mock(side_effect=lambda *a, **kw: trace.append('deleted'))
    monkeypatch.setattr(qualification.signals.match_deleted, 'send', send)
    assert call().is_ok()
    assert trace == ['lock', 'commit', 'deleted']
    send.assert_called_once_with(None, event=event)
