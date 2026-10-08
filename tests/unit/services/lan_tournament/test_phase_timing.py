from dataclasses import replace
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as engine,
    tournament_operational_service as operational,
    tournament_qualification_service as qualification,
    tournament_repository as repo,
    tournament_seeding_repository as seeding_repo,
    tournament_seeding_service as seeding,
)
from byceps.services.lan_tournament.events import TournamentCompletedEvent
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.seeding import (
    SeedingFormat,
    SeedingState,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_match_service import (
    FfaAdvancePlan,
    GenerationOutcome,
)
from byceps.util.result import Err, Ok


TOURNAMENT_ID = TournamentID(UUID('00000000-0000-0000-0000-00000000f151'))
ACTOR = UUID('00000000-0000-0000-0000-00000000a151')
CREATED = datetime(2031, 5, 6, 12, 0, tzinfo=UTC)
CLOCK_START = datetime(2031, 5, 6, 19, 0)
OPERATION_AT = datetime(2031, 5, 6, 23, 0)

SE = GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION


def _pid(i):
    return UUID(f'00000000-0000-0000-0000-{i:012d}')


def _tournament(
    *,
    tracked=True,
    status=TournamentStatus.REGISTRATION_CLOSED,
    **fields,
):
    values = {
        'id': TOURNAMENT_ID,
        'party_id': 'party',
        'name': 'Phase timing',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': CREATED,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': status,
        'game_format': SE[0],
        'elimination_mode': SE[1],
        'operational_clock_activated_at': CLOCK_START if tracked else None,
    }
    values.update(fields)
    return Tournament(**values)


class Harness:
    """In-memory stand-in for the repositories, with an ordered trace."""

    def __init__(self, monkeypatch, tournament):
        self.monkeypatch = monkeypatch
        self.tournament = tournament
        self.trace = []
        self.seedings = {}
        self.calls = {}
        self.outcome = GenerationOutcome(
            count=8,
            created_events=[],
            deleted_events=[],
            ready_match_ids=frozenset(),
            occurred_at=CREATED,
        )
        self.reconcile = Mock(side_effect=self._reconcile)
        self.set_generated = Mock(side_effect=self._set_generated)
        self.run_generator = Mock(side_effect=self._run_generator)
        self.matches_exist = False
        self.install()

    def note(self, label, **values):
        self.trace.append(label)
        self.calls.setdefault(label, []).append(values)

    def patch(self, module, name, value):
        self.monkeypatch.setattr(module, name, value)

    def traced(self, module, name, label, result=None):
        def call(*args, **kwargs):
            self.note(label, args=args, kwargs=kwargs)
            return result() if callable(result) else result

        self.patch(module, name, call)

    def install(self):
        participants = [
            SimpleNamespace(id=_pid(i), user_id=_pid(i)) for i in range(1, 9)
        ]
        self.patch(
            repo,
            'get_tournament',
            lambda tournament_id, *, fresh=False: self.tournament,
        )
        self.patch(
            repo, 'find_tournament', lambda tournament_id: self.tournament
        )
        self.patch(
            repo,
            'get_participants_for_tournament',
            lambda tournament_id: participants,
        )
        self.patch(
            seeding.user_service,
            'get_users_indexed_by_id',
            lambda ids: {
                i: SimpleNamespace(screen_name=f'user-{i.int}') for i in ids
            },
        )
        self.traced(repo, 'lock_tournament_for_update', 'lock')
        self.traced(repo, 'commit_session', 'commit')
        self.traced(repo, 'rollback_session', 'rollback')
        self.traced(repo, 'get_operation_time', 'time', lambda: OPERATION_AT)
        self.patch(seeding_repo, 'find_seeding', self._find)
        self.patch(seeding_repo, 'find_seeding_for_update', self._find)
        self.patch(seeding_repo, 'create_seeding', self._create)
        self.patch(
            seeding_repo,
            'update_seeding_code',
            lambda seeding_id, **kw: Ok(self._update(seeding_id, **kw)),
        )
        self.patch(seeding_repo, 'set_generated', self.set_generated)
        self.patch(seeding, 'create_log_entry', self._log)
        self.patch(seeding, '_run_generator', self.run_generator)
        self.patch(
            seeding, '_has_confirmed_result', lambda tournament_id: False
        )
        self.patch(
            engine, 'has_matches', lambda tournament_id: self.matches_exist
        )
        self.patch(
            engine,
            'collect_generation_invitations_flush',
            self._collect,
        )
        self.traced(engine, 'dispatch_generation_events', 'dispatch')
        self.patch(operational, 'reconcile_due_matches_flush', self.reconcile)

    # -- the stand-ins

    def _find(self, tournament_id, target):
        return self.seedings.get((tournament_id, target))

    def _create(self, row):
        self.seedings[(row.tournament_id, row.target)] = row

    def _update(self, seeding_id, *, seed_code, expected_version, **kw):
        for key, row in self.seedings.items():
            if row.id == seeding_id:
                updated = replace(
                    row,
                    seed_code=seed_code,
                    version=row.version + 1,
                    roster_snapshot=tuple(kw['roster_snapshot']),
                )
                self.seedings[key] = updated
                return updated
        raise AssertionError('unknown draft')

    def _log(self, *args, **kwargs):
        self.note('log', args=args, kwargs=kwargs)

    def _run_generator(self, *args, **kwargs):
        self.note('run', args=args, kwargs=kwargs)
        return Ok(self.outcome)

    def _set_generated(self, seeding_id, **kwargs):
        self.note('set_generated', kwargs=kwargs)
        for row in self.seedings.values():
            if row.id == seeding_id:
                return Ok(row)
        raise AssertionError('unknown draft')

    def _reconcile(self, tournament_id, *, occurred_at):
        self.note('reconcile', tournament_id=tournament_id, at=occurred_at)
        return Ok(None)

    def _collect(self, outcome):
        self.note('collect')
        return outcome

    # -- scenarios

    def draft(self):
        """Draw the initial draft, then forget what that traced."""
        board = seeding.get_board(TOURNAMENT_ID, initiator_id=ACTOR)
        self.trace.clear()
        self.calls.clear()
        return board.unwrap()

    def generate(self, *, version=1):
        return seeding.generate_from_seeding(
            TOURNAMENT_ID, expected_version=version, initiator_id=ACTOR
        )

    def at(self, label):
        return [values for values in self.calls.get(label, [])]


@pytest.fixture
def world(monkeypatch):
    def build(tournament=None):
        return Harness(monkeypatch, tournament or _tournament())

    return build


def _generated(harness, *, regenerating):
    """Leave the draft in the state the matches already follow."""
    harness.matches_exist = regenerating
    harness.draft()
    ((key, row),) = harness.seedings.items()
    harness.seedings[key] = replace(row, generated_seed_code=row.seed_code)


# -- the four plan tests --


# fmt: off
@pytest.mark.parametrize('case', ['unchanged_generation', 'draft_edit'])
# fmt: on
def test_draft_only_and_unchanged_generation_do_not_touch_matches(world, case):
    harness = world()
    if case == 'unchanged_generation':
        _generated(harness, regenerating=True)

        result = harness.generate()

        assert result == Ok(seeding.GENERATION_UNCHANGED)
        # Nothing is sampled, run, stamped, reconciled or committed.
        assert harness.trace == ['lock', 'rollback']
    else:
        board = harness.draft()

        result = seeding.apply_action(
            TOURNAMENT_ID,
            seeding.INITIAL_TARGET,
            seeding.Swap(0, 1),
            expected_version=board.version,
            initiator_id=ACTOR,
        )

        assert result.is_ok(), result.unwrap_err()
        assert harness.trace == ['lock', 'log', 'commit']
    assert not harness.reconcile.called
    assert not harness.run_generator.called
    assert not harness.set_generated.called


# fmt: off
FORMATS = [
    (SeedingFormat.SINGLE_ELIMINATION, '_generate_single_elimination_impl', True),
    (SeedingFormat.DOUBLE_ELIMINATION, '_generate_double_elimination_impl', True),
    (SeedingFormat.ROUND_ROBIN, '_generate_round_robin_impl', True),
    (SeedingFormat.FREE_FOR_ALL, '_generate_ffa_initial_impl', True),
]
# fmt: on


# fmt: off
@pytest.mark.parametrize(('format_', 'helper', 'takes_time'), FORMATS)
@pytest.mark.parametrize('playoff', [False, True])
@pytest.mark.parametrize('tracked', [True, False])
# fmt: on
def test_release_uses_effective_format_clock(
    monkeypatch, format_, helper, takes_time, playoff, tracked
):
    # Every generator gets the operation time of a tracked tournament; an
    # untracked one passes none.
    state = SeedingState(
        format=format_, param=2, tier_count=1, roster=('a', 'b', 'c', 'd'),
        tiers=(0, 0, 0, 0), seed_list=('a', 'b', 'c', 'd'),
        layout=('a', 'b', 'c', 'd'), draw_seed=1,
    )  # fmt: skip
    delegate = Mock(return_value=Ok(object()))
    monkeypatch.setattr(engine, helper, delegate)

    seeding._run_generator(
        TOURNAMENT_ID,
        state,
        False,
        ACTOR,
        playoff=playoff,
        changed_at=OPERATION_AT if tracked else None,
    )

    delegate.assert_called_once()
    if takes_time and tracked:
        assert delegate.call_args.kwargs['changed_at'] == OPERATION_AT
    else:
        assert 'changed_at' not in delegate.call_args.kwargs


def test_release_uses_effective_format_clock_for_the_ffa_rounds(monkeypatch):
    state = SeedingState(
        format=SeedingFormat.FREE_FOR_ALL, param=2, tier_count=1,
        roster=('a', 'b', 'c', 'd'), tiers=(0, 0, 0, 0),
        seed_list=('a', 'b', 'c', 'd'), layout=('a', 'b', 'c', 'd'),
        draw_seed=1,
    )  # fmt: skip
    delegate = Mock(return_value=Ok(object()))
    monkeypatch.setattr(engine, '_generate_ffa_advance_impl', delegate)

    seeding._run_generator(
        TOURNAMENT_ID,
        state,
        False,
        ACTOR,
        ffa_round=(Bracket.WINNERS, 1),
        changed_at=OPERATION_AT,
    )

    assert delegate.call_args.kwargs['changed_at'] == OPERATION_AT


@pytest.mark.parametrize('tracked', [True, False])
def test_generation_samples_one_time_and_reconciles_before_the_commit(
    world, tracked
):
    harness = world(_tournament(tracked=tracked))
    board = harness.draft()

    result = harness.generate(version=board.version)

    assert result.is_ok(), result.unwrap_err()
    if tracked:
        assert harness.trace == [
            'lock', 'time', 'run', 'set_generated', 'log', 'reconcile',
            'collect', 'commit', 'dispatch',
        ]  # fmt: skip
        (run,) = harness.at('run')
        assert run['kwargs']['changed_at'] == OPERATION_AT
        (reconcile,) = harness.at('reconcile')
        assert reconcile['at'] == OPERATION_AT
        assert reconcile['tournament_id'] == TOURNAMENT_ID
    else:
        # A tournament without clock history gets no timing at all.
        assert harness.trace == [
            'lock', 'run', 'set_generated', 'log', 'collect', 'commit',
            'dispatch',
        ]  # fmt: skip
        assert not harness.reconcile.called


def _unrelease(
    monkeypatch,
    *,
    tracked=True,
    status=TournamentStatus.ONGOING,
    reconcile=None,
):
    """Un-release two phase-two matches; return the result and the trace."""
    trace = []
    marks = {}
    tournament = _tournament(
        tracked=tracked,
        status=status,
        playoff_released_at=CLOCK_START,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    phase_one = SimpleNamespace(id=_pid(1), phase=1)
    phase_two = [
        SimpleNamespace(id=_pid(9), phase=2),
        SimpleNamespace(id=_pid(5), phase=2),
    ]

    def mark(label, result=None):
        def call(*args, **kwargs):
            trace.append(label)
            marks.setdefault(label, []).append((args, kwargs))
            return result

        return call

    def read(tournament_id, *, fresh=False):
        return tournament

    monkeypatch.setattr(repo, 'find_tournament', lambda tournament_id: tournament)
    monkeypatch.setattr(repo, 'get_tournament', read)
    monkeypatch.setattr(
        qualification,
        '_compute_state',
        lambda found: Ok(SimpleNamespace(can_unrelease=True)),
    )
    monkeypatch.setattr(
        repo,
        'get_matches_for_tournament_ordered_fresh',
        lambda tournament_id: [phase_one, *phase_two],
    )
    monkeypatch.setattr(
        repo, 'get_operation_time', mark('time', OPERATION_AT)
    )
    monkeypatch.setattr(repo, 'lock_matches_for_update', mark('lock_matches'))
    monkeypatch.setattr(repo, 'retire_dashboard_matches_flush', mark('retire'))
    monkeypatch.setattr(repo, 'clear_playoff_release', mark('clear_release'))
    monkeypatch.setattr(repo, 'rollback_session', mark('rollback'))
    monkeypatch.setattr(engine, 'clear_bracket', mark('clear_bracket', []))
    monkeypatch.setattr(
        qualification.tournament_log_service, 'create_log_entry', mark('log')
    )
    monkeypatch.setattr(
        operational,
        'reconcile_due_matches_flush',
        lambda tournament_id, *, occurred_at: mark('reconcile', reconcile or Ok(None))(
            tournament_id, occurred_at=occurred_at
        ),
    )

    result = qualification._unrelease_locked(TOURNAMENT_ID, 'Reset', ACTOR)
    return result, trace, marks


def test_unrelease_retains_history_and_removes_live_annotations(monkeypatch):
    result, trace, marks = _unrelease(monkeypatch)

    assert result == Ok([])
    # The phase-two demand and pins go before their matches; the rest stays.
    assert trace == [
        'time', 'lock_matches', 'retire', 'clear_bracket', 'clear_release',
        'log', 'reconcile',
    ]  # fmt: skip
    ids = [_pid(5), _pid(9)]
    assert marks['lock_matches'] == [((ids,), {})]
    assert marks['retire'] == [((ids,), {'occurred_at': OPERATION_AT})]
    ((_, cleared),) = marks['clear_bracket']
    assert cleared['phase'] == 2
    assert marks['reconcile'] == [
        ((TOURNAMENT_ID,), {'occurred_at': OPERATION_AT})
    ]


def test_unrelease_of_an_untracked_tournament_writes_no_timing(monkeypatch):
    result, trace, _ = _unrelease(monkeypatch, tracked=False)

    assert result == Ok([])
    assert trace == ['clear_bracket', 'clear_release', 'log']


# fmt: off
@pytest.mark.parametrize('case', [
    'generate_reconcile_err',
    'generate_reconcile_raises',
    'single_survivor_reconcile_err',
    'unrelease_reconcile_err',
])
# fmt: on
def test_phase_failure_rolls_back_timing_with_generation(
    world, monkeypatch, case
):
    if case == 'unrelease_reconcile_err':
        result, trace, _ = _unrelease(
            monkeypatch, reconcile=Err('reconcile_failed')
        )

        assert result == Err('reconcile_failed')
        # The owner rolls the matches, pins and release back with it.
        assert trace[-2:] == ['reconcile', 'rollback']
        return

    harness = world()
    if case == 'single_survivor_reconcile_err':
        harness.tournament = _tournament(status=TournamentStatus.ONGOING)
        plan = FfaAdvancePlan(
            pool=None, round_number=1, survivors=('only',), bands={'only': 0},
            grand_final_eligible=False,
        )  # fmt: skip
        monkeypatch.setattr(
            engine, 'plan_ffa_advance', lambda *a, **kw: Ok(plan)
        )
        complete = Mock(
            side_effect=lambda *a, **kw: Ok(
                TournamentCompletedEvent(
                    occurred_at=CREATED, initiator=None,
                    tournament_id=TOURNAMENT_ID, winner_team_id=None,
                    winner_participant_id=None,
                )
            )
        )  # fmt: skip
        monkeypatch.setattr(engine, 'complete_ffa_single_survivor', complete)
        send = Mock()
        monkeypatch.setattr(
            engine, 'tournament_completed', SimpleNamespace(send=send)
        )
        harness.reconcile.side_effect = lambda *a, **kw: Err('reconcile_failed')

        result = seeding.prepare_ffa_round_draft(
            TOURNAMENT_ID, initiator_id=ACTOR
        )

        assert result == Err('reconcile_failed')
        # The completion was written with the operation time, then undone.
        assert complete.call_args.kwargs['changed_at'] == OPERATION_AT
        assert harness.trace[0] == 'lock'
        assert harness.trace[-1] == 'rollback'
        assert 'commit' not in harness.trace
        send.assert_not_called()
        return

    board = harness.draft()
    if case == 'generate_reconcile_err':
        harness.reconcile.side_effect = lambda *a, **kw: Err('reconcile_failed')
        result = harness.generate(version=board.version)
        assert result == Err('reconcile_failed')
    else:
        harness.reconcile.side_effect = RuntimeError('reconcile exploded')
        with pytest.raises(RuntimeError, match='reconcile exploded'):
            harness.generate(version=board.version)

    # The generation, its draft code and the audit entry go with it.
    assert harness.trace[0] == 'lock'
    assert harness.trace[-1] == 'rollback'
    for label in ('commit', 'dispatch', 'collect'):
        assert label not in harness.trace


# -- the single survivor of the seeding owners --


def _survivor_world(world, monkeypatch, *, tracked):
    harness = world(
        _tournament(tracked=tracked, status=TournamentStatus.ONGOING)
    )
    plan = FfaAdvancePlan(
        pool=None, round_number=1, survivors=('only',), bands={'only': 0},
        grand_final_eligible=False,
    )  # fmt: skip
    monkeypatch.setattr(engine, 'plan_ffa_advance', lambda *a, **kw: Ok(plan))
    event = TournamentCompletedEvent(
        occurred_at=CREATED, initiator=None, tournament_id=TOURNAMENT_ID,
        winner_team_id=None, winner_participant_id=None,
    )  # fmt: skip
    monkeypatch.setattr(
        engine,
        'complete_ffa_single_survivor',
        lambda *args, **kwargs: (
            harness.note('complete', args=args, kwargs=kwargs),
            Ok(event),
        )[1],
    )
    monkeypatch.setattr(
        engine,
        'tournament_completed',
        SimpleNamespace(send=lambda *a, **kw: harness.note('signal')),
    )
    return harness


@pytest.mark.parametrize('tracked', [True, False])
def test_single_survivor_completion_closes_the_demand_before_the_commit(
    world, monkeypatch, tracked
):
    harness = _survivor_world(world, monkeypatch, tracked=tracked)

    result = seeding.prepare_ffa_round_draft(TOURNAMENT_ID, initiator_id=ACTOR)

    assert result == Ok('completed')
    if tracked:
        assert harness.trace == [
            'lock', 'time', 'complete', 'reconcile', 'commit', 'signal',
        ]  # fmt: skip
        (complete,) = harness.at('complete')
        assert complete['kwargs'] == {'changed_at': OPERATION_AT}
        (reconcile,) = harness.at('reconcile')
        assert reconcile['at'] == OPERATION_AT
    else:
        assert harness.trace == ['lock', 'complete', 'commit', 'signal']
        (complete,) = harness.at('complete')
        assert complete['kwargs'] == {}
