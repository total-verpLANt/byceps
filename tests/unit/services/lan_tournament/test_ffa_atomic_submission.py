"""Unit proofs for FFA transaction ownership and post-commit behavior.

The stateful repository stub models staged versus committed DTOs and rollback
cleanup. Real PostgreSQL atomicity/lock contention is covered by Issue 6.
"""

from dataclasses import FrozenInstanceError, dataclass, replace
from inspect import signature
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as service,
    tournament_qualification_service,
)
from byceps.services.lan_tournament.events import (
    MatchConfirmedEvent,
    TournamentCompletedEvent,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid
from tests.unit.services.lan_tournament import test_ffa_de as factories


USER_ID = factories.USER_ID
REAL_AUTO_RELEASE = service._try_auto_release


@dataclass(frozen=True)
class _State:
    tournament: Tournament
    match: TournamentMatch
    contestants: tuple[TournamentMatchToContestant, ...]
    audit: tuple[dict, ...] = ()


@pytest.fixture
def make_transaction(monkeypatch):
    """Mock only repository/notification boundaries, retaining the FFA engine."""

    def make(*, bracket=None, phase=1, team=False, **tournament_kwargs):
        tournament = factories._create_de_tournament(
            **{
                'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                'contestant_type': ContestantType.TEAM
                if team
                else ContestantType.SOLO,
                **tournament_kwargs,
            }
        )
        match = replace(
            factories._create_match(round=0, bracket=bracket), phase=phase
        )
        id_type = TournamentTeamID if team else TournamentParticipantID
        contestant_factory = (
            factories._make_team_contestant
            if team
            else factories._make_contestant
        )
        entries = tuple(
            contestant_factory(
                id_type(generate_uuid()), match.id, placement=p, points=n
            )
            for p, n in [(2, 7), (1, 10)]
        )
        state = _State(tournament, match, entries)
        tx = SimpleNamespace(
            state=state,
            committed=state,
            initial=state,
            repo=Mock(),
            timeline=[],
            peers=[],
            failed=False,
            tournament_locked=False,
            match_locked=False,
            placements={
                service.contestant_id(c): p
                for c, p in zip(entries, [1, 2], strict=True)
            },
        )

        def usable():
            assert not tx.failed, 'session requires rollback before reuse'

        def find_match(match_id):
            usable()
            assert match_id == match.id
            tx.timeline.append('find')
            return tx.state.match

        def lock_tournament(tournament_id):
            usable()
            assert tournament_id == tournament.id
            assert not tx.match_locked
            tx.tournament_locked = True
            tx.timeline.append('lock_tournament')

        def locked_match(match_id):
            usable()
            assert match_id == match.id
            assert tx.tournament_locked
            tx.match_locked = True
            tx.timeline.append('lock_match')
            return tx.state.match

        def get_tournament(tournament_id):
            usable()
            assert tournament_id == tournament.id
            return tx.state.tournament

        def get_contestants(match_id):
            usable()
            assert match_id == match.id
            return list(tx.state.contestants)

        def write_placements(updates):
            assert tx.tournament_locked and tx.match_locked
            tx.timeline.append('placements')
            tx.state = replace(
                tx.state,
                contestants=tuple(
                    replace(
                        c, placement=updates[c.id][0], points=updates[c.id][1]
                    )
                    for c in tx.state.contestants
                ),
            )

        def confirm(match_id, initiator_id):
            assert tx.tournament_locked and tx.match_locked
            assert (match_id, initiator_id) == (match.id, USER_ID)
            tx.timeline.append('confirm')
            tx.state = replace(
                tx.state,
                match=replace(tx.state.match, confirmed_by=initiator_id),
            )

        def set_winner(tournament_id, *, winner_team_id, winner_participant_id):
            assert tournament_id == tournament.id
            tx.timeline.append('winner')
            tx.state = replace(
                tx.state,
                tournament=replace(
                    tx.state.tournament,
                    winner_team_id=winner_team_id,
                    winner_participant_id=winner_participant_id,
                ),
            )
            return Ok(None)

        def set_status(tournament_id, status):
            assert tournament_id == tournament.id
            tx.timeline.append('status')
            tx.state = replace(
                tx.state,
                tournament=replace(
                    tx.state.tournament, tournament_status=status
                ),
            )
            return Ok(None)

        def audit(event_type, tournament_id, initiator_id, *, data, commit):
            assert commit is False
            assert (tournament_id, initiator_id) == (tournament.id, USER_ID)
            tx.timeline.append('audit')
            tx.state = replace(
                tx.state,
                audit=(*tx.state.audit, {'type': event_type, 'data': data}),
            )

        def commit():
            usable()
            tx.timeline.append('commit')
            tx.committed = tx.state
            tx.tournament_locked = tx.match_locked = False

        def rollback():
            tx.timeline.append('rollback')
            tx.state = tx.committed
            tx.failed = tx.tournament_locked = tx.match_locked = False

        def after_commit(label, *args, **kwargs):
            assert tx.state == tx.committed
            assert tx.committed.match.confirmed_by == USER_ID
            assert not tx.tournament_locked and not tx.match_locked
            tx.timeline.append(label)

        tx.repo.find_match.side_effect = find_match
        tx.repo.get_match_for_update.side_effect = locked_match
        tx.repo.lock_tournament_for_update.side_effect = lock_tournament
        tx.repo.get_tournament.side_effect = get_tournament
        tx.repo.get_contestants_for_match.side_effect = get_contestants
        tx.repo.get_matches_for_round.side_effect = lambda *args, **kwargs: [
            tx.state.match,
            *tx.peers,
        ]
        tx.repo.update_contestant_placement_and_points.side_effect = (
            write_placements
        )
        tx.repo.confirm_match.side_effect = confirm
        tx.repo.set_tournament_winner.side_effect = set_winner
        tx.repo.set_tournament_status_flush.side_effect = set_status
        tx.repo.commit_session.side_effect = commit
        tx.repo.rollback_session.side_effect = rollback
        tx.log = Mock(side_effect=audit)
        tx.confirmed = Mock(
            side_effect=lambda *a, **kw: after_commit('match_signal', *a, **kw)
        )
        tx.completed = Mock(
            side_effect=lambda *a, **kw: after_commit(
                'completion_signal', *a, **kw
            )
        )
        tx.followup = Mock(
            side_effect=lambda *a, **kw: after_commit('followup', *a, **kw)
        )
        monkeypatch.setattr(service, 'tournament_repository', tx.repo)
        monkeypatch.setattr(service, 'create_log_entry', tx.log)
        monkeypatch.setattr(
            service, 'match_confirmed', SimpleNamespace(send=tx.confirmed)
        )
        monkeypatch.setattr(
            service, 'tournament_completed', SimpleNamespace(send=tx.completed)
        )
        monkeypatch.setattr(service, '_try_auto_release', tx.followup)
        return tx

    return make


def _submit(tx):
    return service.set_and_confirm_ffa_match(
        tx.state.match.id, tx.placements, USER_ID
    )


def _assert_rolled_back_and_usable(tx):
    tx.repo.rollback_session.assert_called_once_with()
    assert tx.state == tx.initial == tx.committed
    assert not tx.failed and not tx.tournament_locked and not tx.match_locked
    tx.confirmed.assert_not_called()
    tx.completed.assert_not_called()
    tx.followup.assert_not_called()
    # These are same-session reads, not fresh-connection visibility assertions.
    assert (
        tx.repo.get_tournament(tx.initial.tournament.id)
        == tx.initial.tournament
    )
    assert tx.repo.get_contestants_for_match(tx.initial.match.id) == list(
        tx.initial.contestants
    )


def test_atomic_ffa_submission_commits_once(make_transaction):
    tx = make_transaction()

    assert _submit(tx).is_ok()

    tx.repo.commit_session.assert_called_once_with()
    tx.repo.rollback_session.assert_not_called()
    tx.repo.lock_tournament_for_update.assert_called_once_with(
        tx.initial.tournament.id
    )
    tx.repo.get_match_for_update.assert_called_once_with(tx.initial.match.id)
    assert tx.timeline == [
        'find',
        'lock_tournament',
        'lock_match',
        'placements',
        'confirm',
        'winner',
        'status',
        'audit',
        'commit',
        'match_signal',
        'completion_signal',
        'followup',
    ]
    assert [c.placement for c in tx.committed.contestants] == [1, 2]
    assert [c.points for c in tx.committed.contestants] == [10, 7]
    assert tx.committed.match.confirmed_by == USER_ID
    assert (
        tx.committed.tournament.tournament_status is TournamentStatus.COMPLETED
    )
    assert tx.committed.audit == (
        {
            'type': 'ffa-match-confirmed',
            'data': {
                'match_id': str(tx.initial.match.id),
                'placements': {
                    cid: {'placement': p, 'points': 10 if p == 1 else 7}
                    for cid, p in tx.placements.items()
                },
            },
        },
    )


def test_atomic_ffa_submission_does_not_call_committing_public_wrappers(
    make_transaction, monkeypatch
):
    tx = make_transaction()
    old_set = Mock(side_effect=AssertionError('nested placement transaction'))
    old_confirm = Mock(
        side_effect=AssertionError('nested confirmation transaction')
    )
    monkeypatch.setattr(service, 'set_ffa_placements', old_set)
    monkeypatch.setattr(service, 'confirm_ffa_match', old_confirm)
    set_impl = Mock(wraps=service._set_ffa_placements_impl)
    confirm_impl = Mock(wraps=service._confirm_ffa_match_impl)
    monkeypatch.setattr(service, '_set_ffa_placements_impl', set_impl)
    monkeypatch.setattr(service, '_confirm_ffa_match_impl', confirm_impl)

    assert _submit(tx).is_ok()

    old_set.assert_not_called()
    old_confirm.assert_not_called()
    set_impl.assert_called_once_with(
        tx.initial.match, tx.initial.tournament, tx.placements
    )
    confirm_impl.assert_called_once_with(
        tx.initial.match, tx.initial.tournament, USER_ID
    )


# fmt: off
@pytest.mark.parametrize('failure', ['validation', 'confirmation', 'winner', 'status', 'audit'])
# fmt: on
def test_atomic_ffa_failure_rolls_back_every_mutation(make_transaction, monkeypatch, failure):
    tx = make_transaction()
    with monkeypatch.context() as patch:
        if failure == 'validation':
            patch.setattr(tx, 'placements', {cid: 1 for cid in tx.placements})
        elif failure == 'confirmation':
            patch.setattr(service, '_confirm_ffa_match_impl', Mock(return_value=Err('confirmation refused')))
        elif failure in ('winner', 'status'):
            writer = tx.repo.set_tournament_winner if failure == 'winner' else tx.repo.set_tournament_status_flush
            original = writer.side_effect

            def refuse_after_write(*args, **kwargs):
                original(*args, **kwargs)
                return Err('completion refused')

            patch.setattr(writer, 'side_effect', refuse_after_write)
        else:
            original = tx.log.side_effect

            def failed_audit(*args, **kwargs):
                original(*args, **kwargs)
                tx.failed = True
                raise RuntimeError('audit failure')

            patch.setattr(tx.log, 'side_effect', failed_audit)

        if failure == 'audit':
            with pytest.raises(RuntimeError, match='audit failure'):
                _submit(tx)
        else:
            assert _submit(tx).is_err()

    tx.repo.commit_session.assert_not_called()
    _assert_rolled_back_and_usable(tx)
    # A subsequent operation on the same repository/session can commit.
    assert _submit(tx).is_ok()
    tx.repo.commit_session.assert_called_once_with()


# fmt: off
@pytest.mark.parametrize('boundary', [
    'find_match', 'lock_tournament_for_update', 'get_match_for_update', 'get_tournament',
    'update_contestant_placement_and_points', 'confirm_match', 'set_tournament_winner',
    'set_tournament_status_flush', 'audit', 'commit_session',
])
# fmt: on
def test_atomic_ffa_exception_rolls_back_before_reraise(make_transaction, monkeypatch, boundary):
    tx = make_transaction()
    target = tx.log if boundary == 'audit' else getattr(tx.repo, boundary)
    original = target.side_effect

    def fail(*args, **kwargs):
        if boundary != 'commit_session':
            original(*args, **kwargs)
        tx.failed = True
        raise RuntimeError('operational failure')

    with monkeypatch.context() as patch:
        patch.setattr(target, 'side_effect', fail)
        with pytest.raises(RuntimeError, match='operational failure'):
            _submit(tx)

    _assert_rolled_back_and_usable(tx)
    assert _submit(tx).is_ok()


def test_atomic_ffa_commit_exception_does_not_claim_known_rollback(make_transaction, monkeypatch):
    tx = make_transaction()
    original = tx.repo.commit_session.side_effect

    def lost_commit_ack():
        original()
        raise ConnectionError('COMMIT acknowledgement lost')

    monkeypatch.setattr(tx.repo.commit_session, 'side_effect', lost_commit_ack)
    with pytest.raises(ConnectionError, match='acknowledgement lost'):
        _submit(tx)

    tx.repo.rollback_session.assert_called_once_with()
    assert tx.committed.match.confirmed_by == USER_ID
    assert tx.state == tx.committed
    tx.confirmed.assert_not_called()
    tx.completed.assert_not_called()
    tx.followup.assert_not_called()


def test_atomic_ffa_signals_follow_commit(make_transaction):
    tx = make_transaction()
    assert _submit(tx).is_ok()

    match_event = tx.confirmed.call_args.kwargs['event']
    completion_event = tx.completed.call_args.kwargs['event']
    assert isinstance(match_event, MatchConfirmedEvent)
    assert isinstance(completion_event, TournamentCompletedEvent)
    assert tx.confirmed.call_args.args == tx.completed.call_args.args == (None,)
    assert match_event.initiator is completion_event.initiator is None
    assert match_event.tournament_id == completion_event.tournament_id == tx.initial.tournament.id
    assert match_event.match_id == tx.initial.match.id
    assert match_event.winner_participant_id == completion_event.winner_participant_id == tx.initial.contestants[0].participant_id
    assert match_event.winner_team_id is completion_event.winner_team_id is None
    assert match_event.occurred_at == completion_event.occurred_at
    assert tx.timeline.index('commit') < tx.timeline.index('match_signal') < tx.timeline.index('completion_signal') < tx.timeline.index('followup')


# fmt: off
@pytest.mark.parametrize('mode,bracket', [
    (EliminationMode.SINGLE_ELIMINATION, None),
    (EliminationMode.DOUBLE_ELIMINATION, Bracket.GRAND_FINAL),
])
@pytest.mark.parametrize('playoff', [False, True])
@pytest.mark.parametrize('team', [False, True])
# fmt: on
def test_atomic_ffa_final_selects_submitted_winner(make_transaction, mode, bracket, playoff, team):
    settings = {
        'game_format': GameFormat.HIGHSCORE,
        'elimination_mode': None,
        'playoff_game_format': GameFormat.FREE_FOR_ALL,
        'playoff_elimination_mode': mode,
        'playoff_released_at': factories.NOW,
    } if playoff else {'elimination_mode': mode}
    tx = make_transaction(bracket=bracket, phase=2 if playoff else 1, team=team, **settings)
    if playoff and mode is EliminationMode.SINGLE_ELIMINATION:
        # A phase-1 match must not stop the phase-2 final from completing.
        tx.peers = [replace(tx.initial.match, id=TournamentMatchID(generate_uuid()), phase=1)]

    assert _submit(tx).is_ok()

    submitted_winner = tx.initial.contestants[0]
    assert tx.committed.tournament.winner_team_id == submitted_winner.team_id
    assert tx.committed.tournament.winner_participant_id == submitted_winner.participant_id
    assert tx.committed.tournament.tournament_status is TournamentStatus.COMPLETED
    for notification in (tx.confirmed, tx.completed):
        notification.assert_called_once()
        event = notification.call_args.kwargs['event']
        assert event.winner_team_id == submitted_winner.team_id
        assert event.winner_participant_id == submitted_winner.participant_id
        assert event.tournament_id == tx.initial.tournament.id


def test_legacy_ffa_wrappers_keep_separate_transactions(make_transaction):
    tx = make_transaction()
    assert tuple(signature(service.set_ffa_placements).parameters) == ('match_id', 'placements')
    assert tuple(signature(service.confirm_ffa_match).parameters) == (
        'match_id', 'initiator_id', 'confirmation_comment',
    )

    assert service.set_ffa_placements(tx.initial.match.id, tx.placements).is_ok()
    tx.repo.commit_session.assert_called_once_with()
    assert tx.committed.match.confirmed_by is None
    assert not tx.committed.audit
    tx.confirmed.assert_not_called()
    tx.followup.assert_not_called()

    assert service.confirm_ffa_match(tx.initial.match.id, USER_ID).is_ok()
    assert tx.repo.commit_session.call_count == 2
    assert tx.repo.lock_tournament_for_update.call_count == 2
    assert tx.repo.get_match_for_update.call_count == 2
    assert tx.committed.match.confirmed_by == USER_ID
    tx.confirmed.assert_called_once()
    tx.completed.assert_called_once()
    tx.followup.assert_called_once_with(tx.initial.tournament.id, USER_ID)


# fmt: off
@pytest.mark.parametrize('path', ['qualification', 'single_survivor'])
# fmt: on
def test_ffa_qualification_followup_runs_after_commit(make_transaction, monkeypatch, path):
    if path == 'qualification':
        tx = make_transaction(
            playoff_game_format=GameFormat.FREE_FOR_ALL,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        )
        monkeypatch.setattr(service, '_try_auto_release', REAL_AUTO_RELEASE)

        def auto_release(tournament_id, *, triggered_by):
            assert tx.timeline[-1] == 'match_signal'
            assert tx.repo.commit_session.call_count == 1
            assert tx.committed.tournament.tournament_status is TournamentStatus.ONGOING
            assert (tournament_id, triggered_by) == (tx.initial.tournament.id, USER_ID)
            tx.timeline.append('qualification')
            # Independent qualification work may commit after the result.
            tx.repo.commit_session()

        followup = Mock(side_effect=auto_release)
        monkeypatch.setattr(tournament_qualification_service, 'auto_release_after_commit', followup)
        assert _submit(tx).is_ok()
        tx.repo.set_tournament_winner.assert_not_called()
        tx.repo.set_tournament_status_flush.assert_not_called()
        tx.completed.assert_not_called()
        followup.assert_called_once_with(tx.initial.tournament.id, triggered_by=USER_ID)
        assert tx.repo.commit_session.call_count == 2
        assert tx.timeline.index('commit') < tx.timeline.index('qualification')
    else:
        tx = make_transaction()
        tx.peers = [replace(tx.initial.match, id=TournamentMatchID(generate_uuid()), confirmed_by=USER_ID)]
        survivor = tx.initial.contestants[1]
        plan = service.FfaAdvancePlan(
            pool=None, round_number=1, survivors=(service.contestant_id(survivor),),
            bands={}, grand_final_eligible=False,
        )
        monkeypatch.setattr(service, '_single_survivor_source_plan', Mock(return_value=plan))
        monkeypatch.setattr(service, 'active_contestant_ids', Mock(return_value=set(plan.survivors)))
        complete = Mock(wraps=service.complete_ffa_single_survivor)
        monkeypatch.setattr(service, 'complete_ffa_single_survivor', complete)

        assert _submit(tx).is_ok()

        complete.assert_called_once_with(tx.initial.tournament, plan, USER_ID)
        assert tx.committed.tournament.winner_participant_id == survivor.participant_id
        assert [a['type'] for a in tx.committed.audit] == ['bracket-single-survivor', 'ffa-match-confirmed']
        assert tx.confirmed.call_args.kwargs['event'].winner_participant_id == tx.initial.contestants[0].participant_id
        assert tx.completed.call_args.kwargs['event'].winner_participant_id == survivor.participant_id
        tx.completed.assert_called_once()
        tx.repo.commit_session.assert_called_once_with()
        tx.followup.assert_called_once_with(tx.initial.tournament.id, USER_ID)


# fmt: off
@pytest.mark.parametrize('operation', ['atomic', 'confirm'])
@pytest.mark.parametrize('boundary', ['confirmed', 'completed', 'followup'])
# fmt: on
def test_postcommit_ffa_failure_never_rolls_back_committed_result(make_transaction, monkeypatch, operation, boundary):
    tx = make_transaction()
    if operation == 'confirm':
        assert service.set_ffa_placements(tx.initial.match.id, tx.placements).is_ok()
        tx.repo.commit_session.reset_mock()
    monkeypatch.setattr(getattr(tx, boundary), 'side_effect', RuntimeError('postcommit failure'))

    with pytest.raises(RuntimeError, match='postcommit failure'):
        if operation == 'atomic':
            _submit(tx)
        else:
            service.confirm_ffa_match(tx.initial.match.id, USER_ID)

    tx.repo.commit_session.assert_called_once_with()
    tx.repo.rollback_session.assert_not_called()
    assert tx.state == tx.committed
    assert tx.committed.match.confirmed_by == USER_ID
    assert tx.committed.tournament.tournament_status is TournamentStatus.COMPLETED
    assert tx.committed.audit


def test_private_ffa_operations_flush_only_and_return_frozen_outcome(make_transaction):
    tx = make_transaction()
    tx.repo.lock_tournament_for_update(tx.initial.tournament.id)
    tx.repo.get_match_for_update(tx.initial.match.id)
    assert service._set_ffa_placements_impl(tx.initial.match, tx.initial.tournament, tx.placements).is_ok()
    result = service._confirm_ffa_match_impl(tx.initial.match, tx.initial.tournament, USER_ID)

    assert result.is_ok()
    outcome = result.unwrap()
    assert outcome.tournament_id == tx.initial.tournament.id
    assert outcome.match_id == tx.initial.match.id
    assert outcome.winner == tx.state.contestants[0]
    assert outcome.tournament_was_completed is True
    assert outcome.single_survivor_event is None
    with pytest.raises(FrozenInstanceError):
        outcome.tournament_was_completed = False
    tx.repo.commit_session.assert_not_called()
    tx.repo.rollback_session.assert_not_called()
    tx.confirmed.assert_not_called()
    tx.completed.assert_not_called()
    tx.followup.assert_not_called()


# fmt: off
@pytest.mark.parametrize('operation', ['atomic', 'set', 'confirm'])
# fmt: on
def test_released_phase_one_ffa_refusal_rolls_back_without_mutation(make_transaction, operation):
    tx = make_transaction(
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_released_at=factories.NOW,
    )
    if operation == 'atomic':
        result = _submit(tx)
    elif operation == 'set':
        result = service.set_ffa_placements(tx.initial.match.id, tx.placements)
    else:
        result = service.confirm_ffa_match(tx.initial.match.id, USER_ID)

    assert result.is_err()
    assert result.unwrap_err() == service.PHASE1_LOCKED_ERROR
    tx.repo.update_contestant_placement_and_points.assert_not_called()
    tx.repo.confirm_match.assert_not_called()
    tx.log.assert_not_called()
    tx.repo.commit_session.assert_not_called()
    _assert_rolled_back_and_usable(tx)


# fmt: off
@pytest.mark.parametrize('operation', ['set', 'confirm'])
@pytest.mark.parametrize('failure', ['validation', 'mutation', 'commit'])
# fmt: on
def test_legacy_ffa_refusal_and_exception_cleanup_keeps_session_usable(make_transaction, monkeypatch, operation, failure):
    tx = make_transaction()
    with monkeypatch.context() as patch:
        if failure == 'validation':
            if operation == 'set':
                patch.setattr(tx, 'placements', {})
            else:
                missing = tuple(replace(c, placement=None) for c in tx.initial.contestants)
                patch.setattr(tx.repo.get_contestants_for_match, 'side_effect', lambda mid: list(missing))
        else:
            boundary = (
                tx.repo.commit_session if failure == 'commit' else
                tx.repo.update_contestant_placement_and_points if operation == 'set' else
                tx.repo.confirm_match
            )
            original = boundary.side_effect

            def fail(*args, **kwargs):
                if failure != 'commit':
                    original(*args, **kwargs)
                tx.failed = True
                raise RuntimeError('standalone failure')

            patch.setattr(boundary, 'side_effect', fail)

        def invoke():
            if operation == 'set':
                return service.set_ffa_placements(tx.initial.match.id, tx.placements)
            return service.confirm_ffa_match(tx.initial.match.id, USER_ID)

        if failure == 'validation':
            assert invoke().is_err()
        else:
            with pytest.raises(RuntimeError, match='standalone failure'):
                invoke()

    _assert_rolled_back_and_usable(tx)
    assert _submit(tx).is_ok()


def test_atomic_ffa_single_survivor_refusal_rolls_back_all_changes(make_transaction, monkeypatch):
    tx = make_transaction()
    tx.peers = [replace(tx.initial.match, id=TournamentMatchID(generate_uuid()), confirmed_by=USER_ID)]
    plan = service.FfaAdvancePlan(
        pool=None, round_number=1, survivors=(service.contestant_id(tx.initial.contestants[0]),),
        bands={}, grand_final_eligible=False,
    )
    monkeypatch.setattr(service, '_single_survivor_source_plan', Mock(return_value=plan))
    refused = Mock(return_value=Err('single survivor refused'))
    monkeypatch.setattr(service, 'complete_ffa_single_survivor', refused)

    result = _submit(tx)

    assert result.is_err()
    assert result.unwrap_err() == 'single survivor refused'
    refused.assert_called_once_with(tx.initial.tournament, plan, USER_ID)
    assert [c.placement for c in tx.initial.contestants] == [2, 1]
    assert 'placements' in tx.timeline and 'confirm' in tx.timeline
    tx.repo.commit_session.assert_not_called()
    _assert_rolled_back_and_usable(tx)
