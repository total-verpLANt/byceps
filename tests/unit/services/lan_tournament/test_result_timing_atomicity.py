"""
tests.unit.services.lan_tournament.test_result_timing_atomicity
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from byceps.services.lan_tournament import tournament_match_service as engine
from byceps.services.lan_tournament.events import (
    MatchConfirmedEvent,
    TournamentCompletedEvent,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'
_READINESS = 'byceps.services.lan_tournament.tournament_readiness_service'

NOW = datetime(2031, 3, 4, 18, 0, 0, tzinfo=UTC)
# The operation time and the start of the clock are naive UTC, as stored.
ACTIVATED = datetime(2031, 3, 4, 17, 0, 0)
AT = datetime(2031, 3, 4, 19, 0, 0)

SE = EliminationMode.SINGLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN
ONGOING = TournamentStatus.ONGOING
COMPLETED = TournamentStatus.COMPLETED

TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())
NEXT_ID = TournamentMatchID(generate_uuid())
USER_ID = UserID(generate_uuid())
WINNER = TournamentParticipantID(generate_uuid())
LOSER = TournamentParticipantID(generate_uuid())

_SIGNALS = (
    'match_confirmed',
    'match_unconfirmed',
    'match_deleted',
    'match_created',
    'match_ready',
    'contestant_advanced',
    'tournament_completed',
    'tournament_uncompleted',
)


def _tournament(*, tracked: bool = True, mode=SE, **fields) -> Tournament:
    return Tournament(
        id=TOURNAMENT_ID,
        party_id=PartyID('f03-unit'),
        name='Result timing',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=NOW,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=None,
        tournament_status=ONGOING,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=mode,
        operational_clock_running_since=ACTIVATED if tracked else None,
        operational_clock_activated_at=ACTIVATED if tracked else None,
        **fields,
    )


def _match(
    *,
    match_id=MATCH_ID,
    confirmed: bool = False,
    next_match_id=None,
    round: int | None = 0,
    group_order: int | None = None,
    phase: int = 1,
) -> TournamentMatch:
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=group_order,
        match_order=0,
        round=round,
        next_match_id=next_match_id,
        confirmed_by=USER_ID if confirmed else None,
        created_at=NOW,
        phase=phase,
    )


def _contestant(
    participant_id, score: int | None = None, match_id=MATCH_ID
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=participant_id,
        score=score,
        created_at=NOW,
    )


def _played() -> list[TournamentMatchToContestant]:
    return [_contestant(WINNER, 2), _contestant(LOSER, 0)]


def _confirmed_event() -> MatchConfirmedEvent:
    return MatchConfirmedEvent(
        occurred_at=NOW,
        initiator=None,
        tournament_id=TOURNAMENT_ID,
        match_id=MATCH_ID,
        winner_team_id=None,
        winner_participant_id=WINNER,
    )


@dataclass
class _World:
    """A mocked repository and a trace of every ordered step."""

    tournament: Tournament
    match: TournamentMatch
    repo: MagicMock
    operational: MagicMock
    trace: list = field(default_factory=list)
    stack: ExitStack = field(default_factory=ExitStack)
    audit: MagicMock | None = None

    def step(self, name: str, result: Any = None) -> Callable[..., Any]:
        def record(*args, **kwargs):
            self.trace.append(name)
            return result

        return record

    def names(self) -> list[str]:
        return [e if isinstance(e, str) else e[0] for e in self.trace]

    def index(self, name: str) -> int:
        return self.names().index(name)

    def patch(self, target: str, **kwargs) -> MagicMock:
        return self.stack.enter_context(patch(f'{_S}.{target}', **kwargs))


@contextmanager
def _world(
    *,
    tracked: bool = True,
    mode=SE,
    match: TournamentMatch | None = None,
    group: list[TournamentMatch] | None = None,
) -> Iterator[_World]:
    tournament = _tournament(tracked=tracked, mode=mode)
    match = match or _match()
    repo = MagicMock()
    operational = MagicMock()
    world = _World(tournament, match, repo, operational)
    trace = world.trace
    group = group or [match]

    repo.find_match.return_value = match
    repo.find_match_fresh.return_value = match
    repo.get_match.return_value = match
    repo.get_match_for_update.return_value = match
    repo.get_tournament.return_value = tournament
    repo.get_operation_time.side_effect = world.step('time', AT)
    repo.get_contestants_for_match.return_value = _played()
    repo.get_matches_for_tournament_ordered_fresh.return_value = group
    repo.lock_tournament_for_update.side_effect = world.step('lock_tournament')

    def lock_matches(ids):
        trace.append(('lock_matches', tuple(ids)))

    repo.lock_matches_for_update.side_effect = lock_matches
    repo.commit_session.side_effect = world.step('commit')
    repo.rollback_session.side_effect = world.step('rollback')
    repo.set_tournament_status_flush.return_value = Ok(None)
    repo.set_tournament_winner.return_value = Ok(None)
    for name in ('confirm_match', 'update_contestant_scores'):
        getattr(repo, name).side_effect = world.step(f'write:{name}')

    def reconcile(tournament_id, *, occurred_at):
        trace.append(('reconcile', occurred_at))
        return Ok(None)

    def invalidate(match_ids, *, occurred_at):
        trace.append(('invalidate', tuple(match_ids), occurred_at))
        return Ok(None)

    operational.reconcile_due_matches_flush.side_effect = reconcile
    operational.invalidate_due_matches_flush.side_effect = invalidate

    with world.stack:
        world.patch('tournament_repository', new=repo)
        world.patch('tournament_operational_service', new=operational)
        world.patch('_try_auto_release', side_effect=world.step('auto_release'))
        audit = world.patch('create_log_entry', side_effect=world.step('audit'))
        for name in _SIGNALS:
            signal = world.patch(name)
            signal.send.side_effect = world.step(f'signal:{name}')
        for target, result in (
            ('reconcile_invitations_flush', Ok(())),
            ('refresh_pairing_and_invitations_flush', Ok(None)),
            ('reset_readiness_flush', Ok(MagicMock())),
        ):
            world.stack.enter_context(
                patch(f'{_READINESS}.{target}', return_value=result)
            )
        world.stack.enter_context(
            patch(
                f'{_READINESS}.dispatch_pending_invitations',
                side_effect=world.step('dispatch'),
            )
        )
        world.audit = audit
        yield world
        assert all(
            c.kwargs.get('commit') is False for c in audit.call_args_list
        )


def _fail_reconcile(world: _World, how: str) -> None:
    def failing(tournament_id, *, occurred_at):
        world.trace.append(('reconcile', occurred_at))
        if how == 'raises':
            raise RuntimeError('clock failure')
        return Err('clock_failure')

    world.operational.reconcile_due_matches_flush.side_effect = failing


# -- the owners, each with the step that is its last write --


def _confirm_owner(world: _World):
    world.patch(
        '_confirm_match_impl',
        side_effect=world.step(
            'impl', Ok((_confirmed_event(), None, [], [], []))
        ),
    )
    return lambda: engine.confirm_match(MATCH_ID, USER_ID), 'impl'


def _admin_owner(world: _World):
    world.patch(
        '_admin_set_and_confirm_match_impl',
        side_effect=world.step(
            'impl', Ok((_confirmed_event(), None, [], [], []))
        ),
    )
    return (
        lambda: engine.admin_set_and_confirm_match(MATCH_ID, USER_ID, {}),
        'audit',
    )


def _unconfirm_owner(world: _World):
    world.patch(
        '_unconfirm_match_flush',
        side_effect=world.step('impl', Ok(([], [], False, TOURNAMENT_ID))),
    )
    return lambda: engine.unconfirm_match(MATCH_ID, USER_ID), 'impl'


def _correct_owner(world: _World):
    world.patch('_plan_in_place_correction', return_value=None)
    world.patch(
        'classify_result_correction',
        return_value=Ok((CorrectionCase.UNCONFIRMED_DOWNSTREAM, [NEXT_ID])),
    )
    world.patch('_validate_match_scores', return_value=Ok({'row': 3}))
    world.patch('_snapshot_contestant_scores', return_value={})
    world.patch(
        '_unconfirm_match_flush',
        side_effect=world.step('retract', Ok(([], [], False, TOURNAMENT_ID))),
    )
    world.patch(
        '_admin_set_and_confirm_match_impl',
        side_effect=world.step(
            'impl', Ok((_confirmed_event(), None, [], [], []))
        ),
    )
    return (
        lambda: engine.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='typo',
            corrected_scores={WINNER: 3, LOSER: 1},
        ),
        'impl',
    )


def _in_place_owner(world: _World):
    contestants = _played()
    plan = engine._InPlaceCorrection(
        world.match,
        contestants,
        {c.id: 5 for c in contestants},
        contestants[0],
    )
    world.patch('_plan_in_place_correction', return_value=plan)
    world.patch('_snapshot_contestant_scores', return_value={})
    return (
        lambda: engine.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='typo',
            corrected_scores={WINNER: 5, LOSER: 1},
        ),
        'write:confirm_match',
    )


def _settled_owner(world: _World):
    event = TournamentCompletedEvent(
        occurred_at=NOW,
        initiator=None,
        tournament_id=TOURNAMENT_ID,
        winner_team_id=None,
        winner_participant_id=WINNER,
    )
    world.patch(
        'try_complete_plain_round_robin',
        side_effect=world.step('impl', Ok(event)),
    )
    return (
        lambda: engine.complete_settled_plain_round_robin(TOURNAMENT_ID),
        'impl',
    )


# fmt: off
OWNERS = [
    pytest.param(_confirm_owner, id='confirm_match'),
    pytest.param(_admin_owner, id='admin_set_and_confirm_match'),
    pytest.param(_unconfirm_owner, id='unconfirm_match'),
    pytest.param(_correct_owner, id='correct_match_result'),
    pytest.param(_in_place_owner, id='correct_match_result_in_place'),
    pytest.param(_settled_owner, id='complete_settled_plain_round_robin'),
]
# fmt: on


# -- owner atomicity --


@pytest.mark.parametrize('owner', OWNERS)
def test_result_timing_flush_precedes_commit_and_signals(owner):
    with _world() as world:
        run, last_write = owner(world)

        run()

    names = world.names()
    assert names.count('commit') == 1
    assert names.count('reconcile') == 1
    # The episodes follow the last write of the result, and the result
    # commits with them, before any signal or other post-commit effect.
    assert world.index(last_write) < world.index('reconcile')
    assert world.index('reconcile') < world.index('commit')
    after = names[world.index('commit') + 1 :]
    assert 'rollback' not in names
    for name in names:
        if name.startswith('signal:') or name in ('dispatch', 'auto_release'):
            assert world.index(name) > world.index('commit'), name
    assert set(after) <= {
        'signal:match_confirmed',
        'signal:match_unconfirmed',
        'signal:tournament_completed',
        'dispatch',
        'auto_release',
    }
    # One operation time, sampled under the lock and used for the episodes.
    assert names.count('time') == 1
    assert world.index('time') < world.index(last_write)
    (reconciled,) = [e for e in world.trace if e[0:1] == ('reconcile',)]
    assert reconciled[1] == AT


@pytest.mark.parametrize('how', ['err', 'raises'])
@pytest.mark.parametrize('owner', OWNERS)
def test_clock_failure_rolls_back_result_and_audit(owner, how):
    with _world() as world:
        run, last_write = owner(world)
        _fail_reconcile(world, how)

        if how == 'raises':
            with pytest.raises(RuntimeError, match='clock failure'):
                run()
            returned = None
        else:
            returned = run()

    names = world.names()
    # The result and its audit entry were staged, and then rolled back.
    assert world.index(last_write) < world.index('reconcile')
    assert world.index('reconcile') < world.index('rollback')
    assert 'commit' not in names
    # Nothing was announced for a result that did not happen.
    assert not [n for n in names if n.startswith('signal:')]
    assert 'dispatch' not in names
    assert 'auto_release' not in names
    if how == 'err' and returned is not None:
        assert returned.is_err()
        assert returned.unwrap_err() == 'clock_failure'


def test_a_failed_reconcile_of_the_settled_round_robin_is_logged(caplog):
    with _world() as world:
        run, _ = _settled_owner(world)
        _fail_reconcile(world, 'err')

        run()

    assert 'clock_failure' in caplog.text
    assert 'commit' not in world.names()
    assert 'signal:tournament_completed' not in world.names()


# -- the operation time --


def test_the_operation_time_is_sampled_after_the_locks_and_handed_down():
    with _world() as world:
        impl = world.patch(
            '_confirm_match_impl',
            side_effect=world.step(
                'impl', Ok((_confirmed_event(), None, [], [], []))
            ),
        )

        engine.confirm_match(MATCH_ID, USER_ID)

    assert world.index('lock_tournament') < world.index('time')
    assert world.index('time') < world.index('impl')
    assert impl.call_args.kwargs['changed_at'] == AT
    assert impl.call_args.kwargs['_locks_held'] is True


def test_the_operation_time_follows_a_fresh_read_of_the_tournament():
    with _world() as world:
        _confirm_owner(world)[0]()

    world.repo.get_tournament.assert_any_call(TOURNAMENT_ID, fresh=True)


def test_the_settled_round_robin_completes_at_the_operation_time():
    with _world() as world:
        run, _ = _settled_owner(world)
        complete = engine.try_complete_plain_round_robin

        run()

    assert complete.call_args.kwargs == {'changed_at': AT}
    assert world.index('lock_tournament') < world.index('time')
    assert world.index('time') < world.index('impl')


def test_the_admin_owner_hands_the_time_to_its_impl_and_the_locks_with_it():
    with _world() as world:
        impl = world.patch(
            '_admin_set_and_confirm_match_impl',
            side_effect=world.step(
                'impl', Ok((_confirmed_event(), None, [], [], []))
            ),
        )

        engine.admin_set_and_confirm_match(MATCH_ID, USER_ID, {})

    assert world.index('lock_tournament') < world.index('time')
    assert impl.call_args.kwargs['changed_at'] == AT
    assert impl.call_args.kwargs['_locks_held'] is True


def test_a_score_submission_hands_one_time_to_its_write_and_its_confirmation():
    with _world() as world:
        world.patch('_validate_score_submission', return_value=Ok({'row': 2}))
        confirm = world.patch(
            'confirm_match', side_effect=world.step('confirm', Ok(None))
        )

        result = engine.set_match_scores(MATCH_ID, USER_ID, {WINNER: 2})

    assert result.is_ok(), result
    assert world.names().count('time') == 1
    assert world.index('lock_tournament') < world.index('time')
    assert world.index('time') < world.index('write:update_contestant_scores')
    assert world.repo.update_contestant_scores.call_args.kwargs == {
        'changed_at': AT
    }
    confirm.assert_called_once_with(
        MATCH_ID, USER_ID, _locks_held=True, _changed_at=AT
    )


def test_an_untracked_score_submission_calls_its_writers_as_it_always_did():
    with _world(tracked=False) as world:
        world.patch('_validate_score_submission', return_value=Ok({'row': 2}))
        confirm = world.patch('confirm_match', return_value=Ok(None))

        engine.set_match_scores(MATCH_ID, USER_ID, {WINNER: 2})

    world.repo.update_contestant_scores.assert_called_once_with({'row': 2})
    confirm.assert_called_once_with(MATCH_ID, USER_ID, _locks_held=True)


def test_a_confirmation_with_a_given_time_does_not_sample_another():
    with _world() as world:
        impl = world.patch(
            '_confirm_match_impl',
            return_value=Ok((_confirmed_event(), None, [], [], [])),
        )
        given = datetime(2031, 3, 4, 20, 0, 0)

        engine.confirm_match(
            MATCH_ID, USER_ID, _locks_held=True, _changed_at=given
        )

    assert 'time' not in world.names()
    assert impl.call_args.kwargs['changed_at'] == given
    assert ('reconcile', given) in world.trace


def test_the_retraction_gets_the_same_time_as_its_reconcile():
    with _world() as world:
        flush = world.patch(
            '_unconfirm_match_flush',
            return_value=Ok(([], [], False, TOURNAMENT_ID)),
        )

        engine.unconfirm_match(MATCH_ID, USER_ID, reason='Wrong')

    assert flush.call_args.kwargs['changed_at'] == AT
    assert flush.call_args.kwargs['reason'] == 'Wrong'
    assert world.index('lock_tournament') < world.index('time')
    assert ('reconcile', AT) in world.trace


def test_a_correction_hands_one_time_to_both_halves_and_its_reconcile():
    with _world() as world:
        run, _ = _correct_owner(world)
        retract = engine._unconfirm_match_flush
        apply = engine._admin_set_and_confirm_match_impl

        run()

    assert retract.call_args.kwargs['changed_at'] == AT
    assert apply.call_args.kwargs['changed_at'] == AT
    assert apply.call_args.kwargs['_locks_held'] is True
    assert ('reconcile', AT) in world.trace


def test_a_correction_invalidates_the_downstream_before_the_retraction():
    with _world() as world:
        run, _ = _correct_owner(world)

        run()

    names = world.names()
    assert world.index('invalidate') < world.index('retract')
    assert world.index('retract') < world.index('impl')
    (invalidation,) = [e for e in world.trace if e[0:1] == ('invalidate',)]
    # The corrected match and everything the retraction strips.
    assert set(invalidation[1]) == {MATCH_ID, NEXT_ID}
    assert invalidation[2] == AT
    assert names.count('invalidate') == 1


def test_a_failed_invalidation_stops_the_correction_before_it_destroys_anything():
    with _world() as world:
        run, _ = _correct_owner(world)
        world.operational.invalidate_due_matches_flush.side_effect = (
            lambda *a, **k: Err('match_not_found')
        )

        result = run()

    assert result.is_err()
    assert result.unwrap_err() == 'match_not_found'
    assert 'retract' not in world.names()
    assert 'commit' not in world.names()
    assert 'rollback' in world.names()


def test_the_in_place_correction_stamps_with_the_operation_time_and_destroys_nothing():
    with _world() as world:
        run, _ = _in_place_owner(world)

        run()

    assert 'invalidate' not in world.names()
    repo = world.repo
    assert repo.update_contestant_scores.call_args.kwargs == {'changed_at': AT}
    assert repo.confirm_match.call_args.kwargs == {'changed_at': AT}


# -- a tournament without clock history is left alone --


@pytest.mark.parametrize('owner', OWNERS)
def test_a_tournament_without_clock_history_gets_no_timing(owner):
    with _world(tracked=False) as world:
        run, _ = owner(world)

        run()

    names = world.names()
    assert 'time' not in names
    assert 'reconcile' not in names
    assert 'invalidate' not in names
    # No group lock either: nothing beyond the reachable set is taken.
    assert not [
        e
        for e in world.trace
        if isinstance(e, tuple) and e[0] == 'lock_matches' and len(e[1]) > 1
    ]
    world.operational.reconcile_due_matches_flush.assert_not_called()
    world.operational.invalidate_due_matches_flush.assert_not_called()
    assert names.count('commit') == 1


def test_the_writers_of_an_untracked_result_are_called_as_they_always_were():
    next_match = _match(match_id=NEXT_ID, round=1)
    match = _match(next_match_id=NEXT_ID)
    with _world(tracked=False, match=match) as world:
        world.repo.get_tournament.return_value = _tournament(
            tracked=False, mode=SE
        )
        world.repo.find_match.side_effect = lambda i: (
            match if i == MATCH_ID else next_match
        )

        engine.confirm_match(MATCH_ID, USER_ID)

    repo = world.repo
    repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID)
    (created,) = repo.create_match_contestant.call_args_list
    assert created.kwargs == {}
    repo.get_operation_time.assert_not_called()


# -- every writer of a result gets the one time --


def test_every_writer_of_an_advancing_result_gets_the_operation_time():
    match = _match(next_match_id=NEXT_ID)
    with _world(match=match) as world:
        engine.confirm_match(MATCH_ID, USER_ID)

    repo = world.repo
    repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID, changed_at=AT)
    (created,) = repo.create_match_contestant.call_args_list
    assert created.kwargs == {'changed_at': AT}
    assert created.args[0].tournament_match_id == NEXT_ID
    assert world.names().count('time') == 1


def test_the_deciding_result_completes_the_tournament_at_the_operation_time():
    with _world() as world:
        engine.confirm_match(MATCH_ID, USER_ID)

    repo = world.repo
    repo.set_tournament_status_flush.assert_called_once_with(
        TOURNAMENT_ID, COMPLETED, changed_at=AT
    )
    assert world.names().count('time') == 1
    assert ('reconcile', AT) in world.trace


def test_every_writer_of_a_retraction_gets_the_operation_time():
    match = _match(confirmed=True, next_match_id=NEXT_ID)
    next_match = _match(match_id=NEXT_ID, round=1)
    with _world(match=match) as world:
        world.repo.find_match.side_effect = lambda i: (
            match if i == MATCH_ID else next_match
        )

        result = engine.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok(), result
    repo = world.repo
    (stripped,) = repo.delete_contestant_from_match.call_args_list
    assert stripped.args == (NEXT_ID,)
    assert stripped.kwargs['changed_at'] == AT
    repo.unconfirm_match.assert_called_once_with(
        MATCH_ID, reset_readiness=False, changed_at=AT
    )
    repo.clear_contestant_scores.assert_called_once_with(
        MATCH_ID, changed_at=AT
    )
    assert world.names().count('time') == 1


def test_the_reopening_retraction_resumes_the_clock_at_the_operation_time():
    match = _match(confirmed=True)
    with _world(match=match) as world:
        world.repo.get_tournament.return_value = replace_status(
            world.tournament, COMPLETED
        )

        result = engine.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok(), result
    world.repo.set_tournament_status_flush.assert_called_once_with(
        TOURNAMENT_ID, ONGOING, changed_at=AT
    )


def replace_status(tournament: Tournament, status) -> Tournament:
    from dataclasses import replace

    return replace(tournament, tournament_status=status)


# -- round robin: the frontier of the group --


def _group(subject_id=MATCH_ID):
    """Return a group of three, another group and a second phase."""
    ids = sorted(TournamentMatchID(generate_uuid()) for _ in range(5))
    subject = _match(match_id=subject_id, group_order=0)
    sibling = _match(match_id=ids[0], group_order=0, round=0)
    later = _match(match_id=ids[1], group_order=0, round=1)
    other_group = _match(match_id=ids[2], group_order=1, round=0)
    other_phase = _match(match_id=ids[3], group_order=0, round=0, phase=2)
    members = [subject, sibling, later]
    return subject, members, [*members, other_group, other_phase]


def _group_locks(world: _World):
    return [
        e[1]
        for e in world.trace
        if isinstance(e, tuple) and e[0] == 'lock_matches'
    ]


def test_frontier_locks_cover_non_routed_siblings():
    subject, members, everything = _group()
    # Read in descending ID order: the lock must still go in ascending order.
    everything = sorted(everything, key=lambda m: m.id, reverse=True)
    with _world(mode=RR, match=subject, group=everything) as world:
        impl = world.patch(
            '_confirm_match_impl',
            side_effect=world.step(
                'impl', Ok((_confirmed_event(), None, [], [], []))
            ),
        )

        engine.confirm_match(MATCH_ID, USER_ID)

    locks = _group_locks(world)
    # The reachable set is the match alone: nothing routes its siblings.
    assert locks[0] == (MATCH_ID,)
    # Then the whole group, in ID order, before the first write.
    group = tuple(sorted(m.id for m in members))
    assert group in locks[1:]
    assert world.index('lock_tournament') < world.trace.index(
        ('lock_matches', group)
    )
    assert world.trace.index(('lock_matches', group)) < world.index('impl')
    assert world.trace.index(('lock_matches', group)) < world.index('time')
    # Neither another group nor the other phase belongs to the frontier.
    locked = {i for ids in locks for i in ids}
    assert locked == {m.id for m in members}
    assert impl.call_args.kwargs['changed_at'] == AT


@pytest.mark.parametrize(
    'owner',
    [_confirm_owner, _admin_owner, _unconfirm_owner, _correct_owner],
    ids=['confirm', 'admin', 'unconfirm', 'correct'],
)
def test_every_result_owner_locks_the_group_before_its_first_write(owner):
    subject, members, everything = _group()
    with _world(mode=RR, match=subject, group=everything) as world:
        run, last_write = owner(world)

        run()

    group = ('lock_matches', tuple(sorted(m.id for m in members)))
    assert group in world.trace
    assert world.trace.index(group) < world.index(last_write)


def test_a_knockout_match_locks_only_what_it_reaches():
    subject = _match(next_match_id=NEXT_ID)
    sibling = _match(match_id=TournamentMatchID(generate_uuid()))
    with _world(mode=SE, match=subject, group=[subject, sibling]) as world:
        _confirm_owner(world)[0]()

    assert sibling.id not in {i for ids in _group_locks(world) for i in ids}


def test_a_later_phase_of_a_playoff_tournament_is_not_a_group():
    subject = _match(phase=2, group_order=0)
    sibling = _match(
        match_id=TournamentMatchID(generate_uuid()), phase=2, group_order=0
    )
    tournament = _tournament(
        mode=RR,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=SE,
    )
    with _world(match=subject, group=[subject, sibling]) as world:
        world.repo.get_tournament.return_value = tournament
        _confirm_owner(world)[0]()

    # Phase 2 is single elimination: only the reachable set is locked.
    assert sibling.id not in {i for ids in _group_locks(world) for i in ids}
