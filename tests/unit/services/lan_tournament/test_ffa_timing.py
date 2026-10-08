from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as service,
    tournament_operational_service as operational,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
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
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (  # noqa: E501
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_match_service import (
    FfaAdvancePlan,
    UndersizedPool,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


PARTY_ID = PartyID('f03-ffa-timing')
TOURNAMENT_ID = TournamentID(generate_uuid())
USER_ID = UserID(generate_uuid())

SCHEDULED = datetime(2031, 3, 4, 15, 0)
ACTIVATED = datetime(2031, 3, 4, 18, 0)
OPERATION = datetime(2031, 3, 4, 19, 30)

ONGOING = TournamentStatus.ONGOING
CLOSED = TournamentStatus.REGISTRATION_CLOSED

SINGLE = EliminationMode.SINGLE_ELIMINATION
DOUBLE = EliminationMode.DOUBLE_ELIMINATION

WINNERS = Bracket.WINNERS
LOSERS = Bracket.LOSERS
GRAND_FINAL = Bracket.GRAND_FINAL


def make_tournament(
    *,
    status: TournamentStatus | None = ONGOING,
    tracked: bool = True,
    mode: EliminationMode = SINGLE,
    **fields,
) -> Tournament:
    clock = (
        {
            'operational_clock_activated_at': ACTIVATED,
            'operational_clock_running_since': ACTIVATED,
        }
        if tracked
        else {}
    )
    values = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'FFA timing',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': SCHEDULED,
        'created_at': SCHEDULED,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': status,
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': mode,
        'point_table': [10, 7, 5, 3, 1],
        'advancement_count': 2,
        'group_size_min': 2,
        'group_size_max': 4,
        'points_carry_to_losers': False,
        **clock,
        **fields,
    }
    return Tournament(**values)


def make_ids(count: int) -> list[str]:
    return sorted(str(generate_uuid()) for _ in range(count))


def make_match(
    *, bracket: Bracket | None = None, confirmed: bool = False
) -> TournamentMatch:
    return TournamentMatch(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        group_order=0,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=USER_ID if confirmed else None,
        created_at=SCHEDULED,
        bracket=bracket,
    )


def make_row(match_id, participant_id=None, *, placement=None, points=None):
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=TournamentParticipantID(
            participant_id or generate_uuid()
        ),
        score=None,
        created_at=SCHEDULED,
        placement=placement,
        points=points,
    )


class World:
    """Record the writes of an FFA owner, in order, on a mocked repository.

    Only the engine's repository boundary and the operational service
    are replaced; the owners under test run as they are.
    """

    def __init__(self, monkeypatch, tournament: Tournament) -> None:
        self.trace: list[tuple] = []
        self.tournament = tournament
        self.reconcile = 'ok'
        self.marker = 'ok'
        self.markers_seen = 0
        self.status_error: str | None = None
        self.created: list[tuple] = []

        repo = Mock(name='repository')
        self.repo = repo
        monkeypatch.setattr(service, 'tournament_repository', repo)
        repo.get_tournament.return_value = tournament
        repo.get_matches_for_tournament_ordered.return_value = []
        repo.get_teams_for_tournament.return_value = []
        repo.set_tournament_winner.return_value = Ok(None)

        def record(name, result=None):
            def side_effect(*args, **kwargs):
                self.trace.append((name, args, kwargs))
                return result

            return side_effect

        repo.lock_tournament_for_update.side_effect = record('lock')
        repo.commit_session.side_effect = record('commit')
        repo.rollback_session.side_effect = record('rollback')
        repo.get_operation_time.side_effect = record('time', OPERATION)
        repo.update_contestant_placement_and_points.side_effect = record(
            'placements'
        )
        repo.confirm_match.side_effect = record('confirm')
        repo.set_tournament_winner.side_effect = record('winner', Ok(None))
        repo.set_tournament_status_flush.side_effect = self._set_status
        repo.create_match.side_effect = self._create_match

        monkeypatch.setattr(
            service, '_create_match_contestant_flush', self._create_contestant
        )
        monkeypatch.setattr(
            service,
            'create_log_entry',
            lambda event, *a, **kw: self.trace.append(('audit', (event,), kw)),
        )
        monkeypatch.setattr(
            service,
            '_try_auto_release',
            lambda *a, **kw: self.trace.append(('auto_release', a, kw)),
        )
        monkeypatch.setattr(
            service,
            'dispatch_generation_events',
            lambda *a, **kw: self.trace.append(('dispatch', a, kw)),
        )
        for name in ('match_confirmed', 'tournament_completed'):
            monkeypatch.setattr(
                service,
                name,
                SimpleNamespace(
                    send=lambda *a, _name=name, **kw: self.trace.append(
                        ('signal', (_name,), kw)
                    )
                ),
            )
        monkeypatch.setattr(
            operational, 'reconcile_due_matches_flush', self._reconcile
        )
        monkeypatch.setattr(
            operational, 'mark_completed_lobbies_occupied_flush', self._mark
        )

    # collaborators

    def _set_status(self, tournament_id, status, **kwargs):
        self.trace.append(('status', (status,), kwargs))
        if self.status_error:
            return Err(self.status_error)
        return Ok(None)

    def _create_match(self, match, **kwargs):
        self.trace.append(
            (
                'match',
                (match.id, match.bracket),
                {'stamp': match.last_changed_at, **kwargs},
            )
        )
        self.created.append((match.id, match.bracket))

    def _create_contestant(self, contestant, **kwargs):
        self.trace.append(
            ('contestant', (contestant.tournament_match_id,), kwargs)
        )

    def _reconcile(self, tournament_id, *, occurred_at):
        self.trace.append(('reconcile', (tournament_id,), {'at': occurred_at}))
        if self.reconcile == 'raises':
            raise RuntimeError('reconcile failed')
        if self.reconcile == 'err':
            return Err('reconcile_failed')
        return Ok(None)

    def _mark(self, match_ids, *, occurred_at, allow_undersized=False):
        self.markers_seen += 1
        self.trace.append(
            (
                'marker',
                (tuple(match_ids),),
                {'at': occurred_at, 'undersized': allow_undersized},
            )
        )
        failing = self.marker == f'err-{self.markers_seen}'
        if (
            self.marker == 'raises'
            or self.marker == f'raises-{self.markers_seen}'
        ):
            raise RuntimeError('marker failed')
        if failing or self.marker == 'err':
            return Err('lobby_roster_incomplete')
        return Ok(None)

    # setup

    def participants(self, count: int) -> list[str]:
        ids = make_ids(count)
        self.repo.get_participants_for_tournament.return_value = [
            SimpleNamespace(id=UUID(i)) for i in ids
        ]
        return ids

    # inspection

    def names(self) -> list[str]:
        return [name for name, _, _ in self.trace]

    def of(self, name: str) -> list[tuple]:
        return [entry for entry in self.trace if entry[0] == name]

    def position(self, name: str, nth: int = 0) -> int:
        return [i for i, n in enumerate(self.names()) if n == name][nth]


@pytest.fixture
def make_world(monkeypatch):
    def make(tournament: Tournament | None = None, **fields) -> World:
        return World(monkeypatch, tournament or make_tournament(**fields))

    return make


def rows_of(world: World, match_id) -> int:
    return len([e for e in world.of('contestant') if e[1][0] == match_id])


# -- the owners that assemble lobbies --


def _complete_roster(world: World):
    """One call of the public generator: two full lobbies."""
    world.participants(8)
    return service.generate_ffa_round(TOURNAMENT_ID, initiator_id=USER_ID)


def _undersized_pool(world: World, monkeypatch):
    """A winners advance whose losers lobby the generator plans short."""
    wb, lb = make_ids(4), make_ids(3)
    plan = FfaAdvancePlan(
        pool=WINNERS,
        round_number=1,
        survivors=tuple(wb),
        bands=dict.fromkeys(wb, 0),
        grand_final_eligible=False,
        lb_pool=tuple(lb),
        lb_round_number=0,
    )
    monkeypatch.setattr(service, 'plan_ffa_advance', lambda *a, **kw: Ok(plan))
    monkeypatch.setattr(
        service,
        'ffa_undersized_pools',
        lambda *a: (UndersizedPool(LOSERS, 0, 3, (3,), 4),),
    )
    return service.advance_ffa_round(
        TOURNAMENT_ID, pool=WINNERS, initiator_id=USER_ID
    )


def _grand_final(world: World, monkeypatch):
    """The Grand Final merges the survivors of both pools in one lobby."""
    inputs = service._FfaGrandFinalInputs(
        tournament=world.tournament,
        phase=1,
        all_matches=[],
        wb_survivors=make_ids(1),
        wb_dropped=[],
        lb_survivors=make_ids(2),
    )
    monkeypatch.setattr(
        service, '_check_ffa_grand_final', lambda tournament_id: Ok(inputs)
    )
    return service.generate_ffa_grand_final(TOURNAMENT_ID, initiator_id=USER_ID)


def _run_boundary(boundary: str, world: World, monkeypatch):
    if boundary == 'complete':
        return _complete_roster(world)
    if boundary == 'undersized':
        world.tournament = make_tournament(mode=DOUBLE)
        world.repo.get_tournament.return_value = world.tournament
        return _undersized_pool(world, monkeypatch)
    return _grand_final(world, monkeypatch)


# fmt: off
@pytest.mark.parametrize(
    ('boundary', 'expected'),
    [
        # Two lobbies of four, the whole roster assembled by one call.
        ('complete', [(None, [4, 4], False)]),
        # The planned shortfall of a losers lobby is a complete roster.
        ('undersized', [(WINNERS, [4], False), (LOSERS, [3], True)]),
        # The Grand Final is exempt from the minimum lobby size.
        ('grand_final', [(GRAND_FINAL, [3], True)]),
    ],
)
# fmt: on
def test_ffa_occupancy_after_complete_roster(
    make_world, monkeypatch, boundary, expected
):
    world = make_world()

    result = _run_boundary(boundary, world, monkeypatch)

    assert result.is_ok(), result.unwrap_err()
    markers = world.of('marker')
    assert len(markers) == len(expected)
    cursor = 0
    for (bracket, sizes, undersized), marker in zip(
        expected, markers, strict=True
    ):
        _, (lobby_ids,), seen = marker
        marker_at = world.trace.index(marker)
        made = [
            match_id
            for match_id, made_in in world.created[cursor : cursor + len(sizes)]
            if made_in == bracket
        ]
        cursor += len(sizes)
        assert list(lobby_ids) == made
        assert sorted(rows_of(world, i) for i in lobby_ids) == sorted(sizes)
        # No row is inserted after the lobby was declared occupied.
        last_row = max(
            i
            for i, entry in enumerate(world.trace)
            if entry[0] == 'contestant' and entry[1][0] in lobby_ids
        )
        assert last_row < marker_at
        assert seen == {'at': OPERATION, 'undersized': undersized}
    assert world.names().count('time') == 1
    assert world.position('marker', len(markers) - 1) < world.position(
        'reconcile'
    )
    assert world.position('reconcile') < world.position('commit')
    assert world.names().count('reconcile') == 1
    assert world.names().count('commit') == 1


# fmt: off
@pytest.mark.parametrize(
    ('status', 'tracked', 'marked', 'reconciled'),
    [
        # A tournament that has not started yet records the occupancy,
        # because its start reads the lobbies generated before.
        (TournamentStatus.DRAFT, False, True, False),
        (TournamentStatus.REGISTRATION_OPEN, False, True, False),
        (CLOSED, False, True, False),
        # A known clock records and reconciles, in any started status.
        (CLOSED, True, True, True),
        (ONGOING, True, True, True),
        (TournamentStatus.PAUSED, True, True, True),
        # A started tournament without history is never tracked.
        (ONGOING, False, False, False),
        (TournamentStatus.PAUSED, False, False, False),
        (TournamentStatus.COMPLETED, False, False, False),
        (TournamentStatus.CANCELLED, False, False, False),
        (None, False, False, False),
    ],
)
# fmt: on
def test_lobby_occupancy_is_recorded_only_where_it_can_be_read(
    make_world, status, tracked, marked, reconciled
):
    world = make_world(status=status, tracked=tracked)

    result = _complete_roster(world)

    assert result.is_ok(), result.unwrap_err()
    assert bool(world.of('marker')) is marked
    assert bool(world.of('reconcile')) is reconciled
    assert world.names()[-1] == 'commit'
    if tracked:
        # One operation time for the stamps and for the occupancy.
        assert world.names().count('time') == 1
        stamps = {e[2]['stamp'] for e in world.of('match')}
        assert stamps == {OPERATION}
    elif marked:
        # Only the marker samples its own time.
        assert world.names().count('time') == 1
        assert world.of('match')[0][2] == {'stamp': None}
        assert world.of('marker')[0][2]['at'] == OPERATION
    else:
        assert 'time' not in world.names()
        assert all(e[2] == {'stamp': None} for e in world.of('match'))
        assert all(e[2] == {} for e in world.of('contestant'))


# fmt: off
@pytest.mark.parametrize(
    'failure',
    ['err', 'raises'],
)
# fmt: on
def test_a_refused_roster_rolls_the_generation_back(make_world, failure):
    world = make_world()
    world.marker = failure

    if failure == 'raises':
        with pytest.raises(RuntimeError):
            _complete_roster(world)
    else:
        result = _complete_roster(world)
        assert result.unwrap_err() == 'lobby_roster_incomplete'

    assert 'rollback' in world.names()
    assert 'commit' not in world.names()
    assert 'reconcile' not in world.names()


# -- both pools of a winners advance are one operation --


def _winners_advance(monkeypatch, *, waiting: bool = False):
    wb = make_ids(1 if waiting else 4)
    lb = make_ids(6 if waiting else 6)
    plan = FfaAdvancePlan(
        pool=WINNERS,
        round_number=2 if waiting else 1,
        survivors=tuple(wb),
        bands=dict.fromkeys(wb, 0),
        grand_final_eligible=False,
        lb_pool=tuple(lb),
        lb_round_number=1 if waiting else 0,
    )
    monkeypatch.setattr(
        service, 'plan_ffa_advance', lambda *a, **kw: Ok(plan)
    )
    monkeypatch.setattr(service, 'ffa_undersized_pools', lambda *a: ())
    return plan


def test_wb_and_companion_lb_timing_is_atomic(make_world, monkeypatch):
    world = make_world(mode=DOUBLE)
    plan = _winners_advance(monkeypatch)

    result = service.advance_ffa_round(
        TOURNAMENT_ID, pool=WINNERS, initiator_id=USER_ID
    )

    assert result.unwrap() == 'advanced_wb'
    # One operation time reaches the stamps, both markers and the reconcile.
    assert world.names().count('time') == 1
    assert {e[2]['stamp'] for e in world.of('match')} == {OPERATION}
    assert {e[2].get('changed_at') for e in world.of('contestant')} == {
        OPERATION
    }
    pools = [bracket for _, bracket in world.created]
    assert pools[0] is WINNERS
    assert set(pools) == {WINNERS, LOSERS}
    wb_ids = {i for i, b in world.created if b is WINNERS}
    lb_ids = {i for i, b in world.created if b is LOSERS}
    first, second = world.of('marker')
    assert set(first[1][0]) == wb_ids
    assert set(second[1][0]) == lb_ids
    assert first[2]['at'] == second[2]['at'] == OPERATION
    # Both pools, then one reconcile, then the one commit.
    names = world.names()
    assert names.count('reconcile') == 1
    assert names.count('commit') == 1
    assert world.of('reconcile')[0][2] == {'at': OPERATION}
    assert world.position('marker', 1) < world.position('reconcile')
    assert world.position('reconcile') < world.position('commit')
    assert len(wb_ids) == 1 and len(lb_ids) == 2
    assert len(plan.lb_pool) == 6


# fmt: off
@pytest.mark.parametrize(
    ('failing', 'raises'),
    [
        ('marker', False),
        ('marker', True),
        ('reconcile', False),
        ('reconcile', True),
    ],
)
# fmt: on
def test_a_failure_in_either_pool_rolls_both_back(
    make_world, monkeypatch, failing, raises
):
    world = make_world(mode=DOUBLE)
    _winners_advance(monkeypatch)
    if failing == 'marker':
        # The losers round is the second roster of the operation.
        world.marker = 'raises-2' if raises else 'err-2'
    else:
        world.reconcile = 'raises' if raises else 'err'

    def advance():
        return service.advance_ffa_round(
            TOURNAMENT_ID, pool=WINNERS, initiator_id=USER_ID
        )

    if raises:
        with pytest.raises(RuntimeError):
            advance()
    else:
        assert advance().is_err()

    assert 'rollback' in world.names()
    assert 'commit' not in world.names()
    assert world.of('signal') == []


@pytest.mark.parametrize('failing', ['marker', 'reconcile'])
def test_a_failed_clock_leaves_no_grand_final(
    make_world, monkeypatch, failing
):
    world = make_world(mode=DOUBLE)
    setattr(world, failing, 'err')

    result = _grand_final(world, monkeypatch)

    assert result.is_err()
    names = world.names()
    assert 'rollback' in names
    assert 'commit' not in names
    assert 'dispatch' not in names


def test_waiting_survivor_without_lobby_has_no_demand(make_world, monkeypatch):
    world = make_world(mode=DOUBLE)
    plan = _winners_advance(monkeypatch, waiting=True)
    (waiting,) = plan.survivors

    result = service.advance_ffa_round(
        TOURNAMENT_ID, pool=WINNERS, initiator_id=USER_ID
    )

    assert result.is_ok(), result.unwrap_err()
    # The winners round has no lobby, so there is nothing to occupy.
    assert [bracket for _, bracket in world.created] == [LOSERS, LOSERS]
    (marker,) = world.of('marker')
    assert set(marker[1][0]) == {i for i, _ in world.created}
    assert marker[2]['undersized'] is False
    # The survivor sits in no lobby row at all.
    assert not [
        e
        for e in world.of('contestant')
        if str(e[1][0]) == waiting
    ]
    assert [e[1][0] for e in world.of('audit')] == ['bracket-lobby-bye']
    assert world.names().count('commit') == 1
    assert world.position('reconcile') < world.position('commit')


# -- results --


class _Result:
    """A locked FFA lobby of two, placed or not."""

    def __init__(self, world: World, *, placed: bool):
        self.match = make_match(bracket=None)
        ids = make_ids(2)
        unplaced = [make_row(self.match.id, UUID(i)) for i in ids]
        self.rows = [
            replace(row, placement=n, points=(10, 7)[n - 1])
            for n, row in enumerate(unplaced, start=1)
        ]
        self.placements = {
            str(row.participant_id): row.placement for row in self.rows
        }
        repo = world.repo
        repo.find_match.return_value = self.match
        repo.get_match_for_update.return_value = self.match
        # The rows carry their placements once the placements are written.
        repo.get_contestants_for_match.side_effect = lambda match_id: (
            self.rows if placed or world.of('placements') else unplaced
        )
        repo.get_matches_for_round.return_value = [self.match]
        repo.get_participants_for_tournament.return_value = [
            SimpleNamespace(id=r.participant_id) for r in self.rows
        ]


def _owner(name: str, lobby: _Result):
    if name == 'set':
        return service.set_ffa_placements(lobby.match.id, lobby.placements)
    if name == 'confirm':
        return service.confirm_ffa_match(lobby.match.id, USER_ID)
    return service.set_and_confirm_ffa_match(
        lobby.match.id, lobby.placements, USER_ID
    )


def test_ffa_atomic_result_freezes_terminal_clock(make_world):
    world = make_world()
    lobby = _Result(world, placed=False)

    result = _owner('atomic', lobby)

    assert result.is_ok(), result.unwrap_err()
    names = world.names()
    # Result, completion, audit and timing form one operation.
    order = [
        'placements',
        'confirm',
        'winner',
        'status',
        'audit',
        'reconcile',
        'commit',
    ]
    positions = [names.index(n) for n in order]
    assert positions == sorted(positions)
    assert names.count('time') == 1
    assert names.index('time') < names.index('placements')
    assert world.of('placements')[0][2] == {'changed_at': OPERATION}
    assert world.of('confirm')[0][2] == {'changed_at': OPERATION}
    status = world.of('status')[0]
    # The frozen clock and the closed episodes share one moment.
    assert status[1] == (TournamentStatus.COMPLETED,)
    assert status[2] == {'changed_at': OPERATION}
    assert world.of('reconcile')[0][2] == {'at': OPERATION}
    # Signals only after the commit.
    assert names.index('commit') < names.index('signal')


# fmt: off
@pytest.mark.parametrize('failure', ['err', 'raises', 'status'])
# fmt: on
def test_a_failed_clock_rolls_the_atomic_result_back(make_world, failure):
    world = make_world()
    lobby = _Result(world, placed=False)
    if failure == 'status':
        world.status_error = 'unsupported_status_transition'
    else:
        world.reconcile = failure

    if failure == 'raises':
        with pytest.raises(RuntimeError):
            _owner('atomic', lobby)
    else:
        assert _owner('atomic', lobby).is_err()

    names = world.names()
    assert 'rollback' in names
    assert 'commit' not in names
    assert 'signal' not in names


# fmt: off
@pytest.mark.parametrize(
    ('owner', 'placed', 'writes'),
    [
        ('set', False, ['placements']),
        ('confirm', True, ['confirm', 'status']),
        ('atomic', False, ['placements', 'confirm', 'status']),
    ],
)
# fmt: on
def test_every_ffa_result_owner_reconciles_before_its_commit(
    make_world, owner, placed, writes
):
    world = make_world()
    lobby = _Result(world, placed=placed)

    result = _owner(owner, lobby)

    assert result.is_ok(), result.unwrap_err()
    names = world.names()
    assert names.count('reconcile') == 1
    assert names.index('reconcile') < names.index('commit')
    assert world.of('reconcile')[0][2] == {'at': OPERATION}
    for write in writes:
        assert world.of(write)[0][2]['changed_at'] == OPERATION
        assert names.index(write) < names.index('reconcile')


# fmt: off
@pytest.mark.parametrize(
    ('owner', 'placed'),
    [('set', False), ('confirm', True), ('atomic', False)],
)
# fmt: on
def test_an_untracked_tournament_calls_the_result_writers_as_before(
    make_world, owner, placed
):
    world = make_world(tracked=False)
    lobby = _Result(world, placed=placed)

    result = _owner(owner, lobby)

    assert result.is_ok(), result.unwrap_err()
    names = world.names()
    assert 'reconcile' not in names
    assert 'time' not in names
    for write in ('placements', 'confirm', 'status'):
        assert all(e[2] == {} for e in world.of(write))


@pytest.mark.parametrize('failure', ['err', 'raises'])
def test_a_failed_reconcile_rolls_a_confirmation_back(make_world, failure):
    world = make_world()
    lobby = _Result(world, placed=True)
    world.reconcile = failure

    if failure == 'raises':
        with pytest.raises(RuntimeError):
            _owner('confirm', lobby)
    else:
        assert _owner('confirm', lobby).is_err()

    assert 'commit' not in world.names()
    assert 'signal' not in world.names()
    assert 'rollback' in world.names()


def test_the_single_survivor_completion_freezes_the_clock(
    make_world, monkeypatch
):
    world = make_world()
    (survivor,) = make_ids(1)
    world.repo.get_participants_for_tournament.return_value = [
        SimpleNamespace(id=UUID(survivor))
    ]
    plan = FfaAdvancePlan(
        pool=None,
        round_number=2,
        survivors=(survivor,),
        bands={survivor: 0},
        grand_final_eligible=False,
    )
    monkeypatch.setattr(
        service, 'plan_ffa_advance', lambda *a, **kw: Ok(plan)
    )

    result = service.advance_ffa_round(TOURNAMENT_ID, initiator_id=USER_ID)

    assert result.unwrap() == 'completed'
    names = world.names()
    assert world.of('status')[0][2] == {'changed_at': OPERATION}
    assert world.of('reconcile')[0][2] == {'at': OPERATION}
    assert names.index('status') < names.index('reconcile')
    assert names.index('reconcile') < names.index('commit')
    assert names.index('commit') < names.index('signal')


def test_a_refused_advance_writes_nothing(make_world, monkeypatch):
    world = make_world(mode=DOUBLE)
    (survivor,) = make_ids(1)
    plan = FfaAdvancePlan(
        pool=LOSERS,
        round_number=3,
        survivors=(survivor,),
        bands={survivor: 0},
        grand_final_eligible=False,
    )
    monkeypatch.setattr(
        service, 'plan_ffa_advance', lambda *a, **kw: Ok(plan)
    )

    result = service.advance_ffa_round(TOURNAMENT_ID, pool=LOSERS)

    assert result.is_err()
    assert not {'match', 'marker', 'reconcile', 'commit'} & set(world.names())


# -- the marker of the operational service --


class _MarkerRepository:
    """The repository functions of the occupancy marker, recorded."""

    def __init__(self, tournament: Tournament) -> None:
        self.tournament = tournament
        self.matches: dict[TournamentMatchID, TournamentMatch] = {}
        self.rows: dict[TournamentMatchID, list] = {}
        self.written: list[tuple] = []

    def lobby(self, count: int, **fields) -> TournamentMatchID:
        match = replace(make_match(), **fields)
        self.matches[match.id] = match
        self.rows[match.id] = [make_row(match.id) for _ in range(count)]
        return match.id

    def get_matches_by_ids(self, match_ids):
        return [self.matches[i] for i in match_ids if i in self.matches]

    def lock_tournament_for_update(self, tournament_id):
        pass

    def lock_matches_for_update(self, match_ids):
        pass

    def get_tournament(self, tournament_id, *, fresh=False):
        return self.tournament

    def get_matches_for_tournament_ordered_fresh(self, tournament_id):
        return list(self.matches.values())

    def get_contestants_for_matches(self, match_ids):
        return {i: self.rows[i] for i in match_ids}

    def set_ffa_lobby_occupied_since_if_unset_flush(
        self, match_id, occupied_since
    ):
        self.written.append((match_id, occupied_since))
        return True


@pytest.fixture
def marker_repository(monkeypatch):
    repo = _MarkerRepository(make_tournament(group_size_min=4))
    monkeypatch.setattr(operational, 'tournament_repository', repo)
    return repo


def test_the_marker_writes_the_occupancy_of_every_accepted_lobby(
    marker_repository,
):
    first = marker_repository.lobby(4)
    second = marker_repository.lobby(5)

    result = operational.mark_completed_lobbies_occupied_flush(
        [second, first], occurred_at=OPERATION
    )

    assert result.is_ok()
    assert sorted(marker_repository.written) == sorted(
        [(first, OPERATION), (second, OPERATION)]
    )


def test_the_marker_stores_naive_utc_for_an_aware_time(marker_repository):
    from datetime import timedelta, timezone

    lobby = marker_repository.lobby(4)
    plus_two = timezone(timedelta(hours=2))

    operational.mark_completed_lobbies_occupied_flush(
        [lobby], occurred_at=OPERATION.replace(tzinfo=plus_two)
    ).unwrap()

    ((_, stored),) = marker_repository.written
    assert stored == OPERATION - timedelta(hours=2)
    assert stored.tzinfo is None


# fmt: off
@pytest.mark.parametrize(
    ('count', 'undersized', 'accepted'),
    [
        (4, False, True),
        (3, False, False),
        (3, True, True),
        (2, True, True),
        (1, True, False),
    ],
)
# fmt: on
def test_the_marker_writes_only_for_a_complete_roster(
    marker_repository, count, undersized, accepted
):
    lobby = marker_repository.lobby(count)

    result = operational.mark_completed_lobbies_occupied_flush(
        [lobby], occurred_at=OPERATION, allow_undersized=undersized
    )

    assert result.is_ok() is accepted
    assert bool(marker_repository.written) is accepted


def test_one_refused_lobby_leaves_every_lobby_unmarked(marker_repository):
    full = marker_repository.lobby(4)
    short = marker_repository.lobby(3)

    result = operational.mark_completed_lobbies_occupied_flush(
        [full, short], occurred_at=OPERATION
    )

    assert result.unwrap_err() == operational.LOBBY_ROSTER_INCOMPLETE_ERROR
    assert marker_repository.written == []


def test_a_confirmed_lobby_is_never_marked(marker_repository):
    lobby = marker_repository.lobby(4, confirmed_by=USER_ID)

    result = operational.mark_completed_lobbies_occupied_flush(
        [lobby], occurred_at=OPERATION
    )

    assert result.unwrap_err() == 'match_confirmed'
    assert marker_repository.written == []
