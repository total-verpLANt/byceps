from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as matches,
    tournament_operational_service as operational,
    tournament_participant_service,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.dashboard import DbMatchDueEpisode
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


START = datetime(2031, 5, 6, 18, 0, 0)
SCHEDULED = datetime(2031, 5, 6, 15, 0, 0)
HOUR_US = 3_600 * 1_000_000

CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING

SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION

WINNERS = Bracket.WINNERS
LOSERS = Bracket.LOSERS
GRAND_FINAL = Bracket.GRAND_FINAL


class _Clock:
    """Stand in for the server operation time, one reading per call."""

    def __init__(self, now: datetime, step: timedelta = timedelta(seconds=1)):
        self.now = now
        self.step = step
        self.readings = 0

    def __call__(self) -> datetime:
        self.readings += 1
        value = self.now
        self.now += self.step
        return value


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    suffix = str(generate_uuid7())
    brand = make_brand(f'ffatiming{suffix}', f'FFA timing {suffix}')
    return make_party(brand, PartyID(f'ffa-timing-{suffix}'), 'FFA timing')


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'FfaTiming{suffix}P{i}') for i in range(16)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'FfaTimingAdmin{str(generate_uuid7())[:18]}')


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(START)
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


@pytest.fixture
def marker_calls(monkeypatch):
    """Record each marker call with the roster the lobbies hold by then."""
    calls = []
    real = operational.mark_completed_lobbies_occupied_flush

    def spy(match_ids, **kwargs):
        ids = list(match_ids)
        rows = repo.get_contestants_for_matches(ids)
        calls.append(
            {
                'ids': ids,
                'rows': {i: len(rows.get(i, [])) for i in ids},
                'kwargs': kwargs,
            }
        )
        return real(match_ids, **kwargs)

    monkeypatch.setattr(
        operational, 'mark_completed_lobbies_occupied_flush', spy
    )
    return calls


@pytest.fixture
def make_ffa(party, players, admin, clock):
    created = []

    def make(
        *,
        mode=SE,
        size=8,
        minimum=2,
        maximum=4,
        cut=2,
        status=CLOSED,
        generate='direct',
    ):
        result = tournament_service.create_tournament(
            party.id,
            f'FFA timing {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=mode,
            tournament_status=status,
            start_time=SCHEDULED,
            max_players=16,
            point_table=[5, 3, 2, 1],
            group_size_min=minimum,
            group_size_max=maximum,
            advancement_count=cut,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in players[:size]:
            repo.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=SCHEDULED,
                )
            )
        db.session.commit()
        if generate == 'direct':
            generated = matches.generate_ffa_round(
                tournament.id,
                bracket=WINNERS if mode is DE else None,
                initiator_id=admin.id,
            )
            assert generated.is_ok(), generated.unwrap_err()
        elif generate == 'seeding':
            board = seeding.get_board(
                tournament.id, initiator_id=admin.id
            ).unwrap()
            seeding.generate_from_seeding(
                tournament.id,
                expected_version=board.version,
                initiator_id=admin.id,
            ).unwrap()
        return tournament

    yield make
    db.session.rollback()
    for tournament in created:
        if repo.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def start(admin, clock):
    """Start a tournament well after everything its generation sampled."""

    def _start(tournament) -> datetime:
        clock.now = START + timedelta(hours=1)
        started_at = clock.now
        result = tournament_service.change_status(
            tournament.id, ONGOING, admin.id
        )
        assert result.is_ok(), result.unwrap_err()
        clock.now = START + timedelta(hours=2)
        return started_at

    return _start


# -- reads: only committed data, through a separate connection --


def _committed(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _lobbies(tournament) -> dict[UUID, dict]:
    return {
        row[0]: {
            'bracket': row[1],
            'round': row[2],
            'occupied_since': row[3],
            'confirmed_by': row[4],
            'last_changed_at': row[5],
        }
        for row in _committed(
            'SELECT id, bracket, round, occupied_since, confirmed_by,'
            ' last_changed_at FROM lan_tournament_matches'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _tournament_row(tournament):
    (row,) = _committed(
        'SELECT tournament_status, operational_clock_elapsed_us,'
        ' operational_clock_running_since, operational_clock_activated_at'
        ' FROM lan_tournaments WHERE id = :id',
        id=tournament.id,
    )
    return row


def _audit_count(tournament, event_type: str) -> int:
    (row,) = _committed(
        'SELECT count(*) FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id AND event_type = :event',
        id=tournament.id,
        event=event_type,
    )
    return row[0]


def _placements(match_id) -> list:
    return [
        row[0]
        for row in _committed(
            'SELECT placement FROM lan_tournament_match_contestants'
            ' WHERE tournament_match_id = :id',
            id=match_id,
        )
    ]


def _episodes(tournament) -> list[DbMatchDueEpisode]:
    db.session.rollback()
    return list(
        db.session.scalars(
            select(DbMatchDueEpisode)
            .where(DbMatchDueEpisode.tournament_id == tournament.id)
            .order_by(DbMatchDueEpisode.opened_at, DbMatchDueEpisode.id)
            .execution_options(populate_existing=True)
        )
    )


def _open(tournament) -> dict[UUID, DbMatchDueEpisode]:
    found = [e for e in _episodes(tournament) if e.closed_at is None]
    assert len({e.match_id for e in found}) == len(found)
    return {e.match_id: e for e in found}


def _round(tournament, bracket, round_number):
    db.session.rollback()
    return sorted(
        repo.get_matches_for_round(
            tournament.id, round_number, bracket=bracket
        ),
        key=lambda m: m.group_order or 0,
    )


def _members(match) -> list[str]:
    db.session.rollback()
    return sorted(
        str(c.participant_id)
        for c in matches.get_contestants_for_match(match.id)
    )


def _play(match, admin) -> str:
    """Place the lobby by contestant ID and confirm it; return the winner."""
    order = _members(match)
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()
    return order[0]


def _all_matches(tournament):
    db.session.rollback()
    return repo.get_matches_for_tournament(tournament.id)


# -- the two pools of a double elimination --


def _to_waiting_winner(make_ffa, start, admin):
    """Play a 16-player DE up to a lone winners survivor who waits."""
    tournament = make_ffa(mode=DE, size=16, cut=1, generate='seeding')
    start(tournament)
    for lobby in _round(tournament, WINNERS, 0):
        _play(lobby, admin)
    matches.advance_ffa_round(
        tournament.id, pool=WINNERS, initiator_id=admin.id
    ).unwrap()
    for lobby in _round(tournament, LOSERS, 0):
        _play(lobby, admin)
    (winners_lobby,) = _round(tournament, WINNERS, 1)
    winner = _play(winners_lobby, admin)
    return tournament, winner


# fmt: off
@pytest.mark.parametrize('boundary', ['complete', 'undersized', 'grand_final'])
# fmt: on
def test_ffa_occupancy_after_complete_roster(
    boundary, make_ffa, start, admin, clock, marker_calls
):
    if boundary == 'complete':
        tournament = make_ffa(size=8)
        facts = _lobbies(tournament)
        (call,) = marker_calls

        # The marker ran once, after both rosters were assembled.
        assert len(facts) == 2
        assert sorted(call['rows'].values()) == [4, 4]
        assert set(call['ids']) == set(facts)
        assert call['kwargs']['allow_undersized'] is False
        occupied = {f['occupied_since'] for f in facts.values()}
        assert occupied == {call['kwargs']['occurred_at']}
        # Before the start nothing is due, so no episode exists.
        assert _episodes(tournament) == []

        started_at = start(tournament)

        # The wait of a preassigned lobby begins at the actual start.
        assert {f['occupied_since'] for f in _lobbies(tournament).values()} == (
            occupied
        )
        assert {e.opened_at for e in _open(tournament).values()} == {
            started_at
        }
        assert {e.opened_clock_us for e in _open(tournament).values()} == {0}
        assert set(_open(tournament)) == set(facts)

    elif boundary == 'undersized':
        tournament = make_ffa(
            mode=DE, size=8, minimum=4, maximum=4, generate='seeding'
        )
        start(tournament)
        first, second = _round(tournament, WINNERS, 0)
        victim = _members(first)[0]
        for lobby in (first, second):
            _play(lobby, admin)
        tournament_participant_service.admin_remove_participant(
            tournament.id,
            TournamentParticipantID(victim),
            initiator=admin,
        ).unwrap()
        before = len(marker_calls)

        advanced = matches.advance_ffa_round(
            tournament.id, pool=WINNERS, initiator_id=admin.id
        )

        assert advanced.is_ok(), advanced.unwrap_err()
        winners_call, losers_call = marker_calls[before:]
        (winners_lobby,) = _round(tournament, WINNERS, 1)
        (losers_lobby,) = _round(tournament, LOSERS, 0)
        assert winners_call['rows'] == {winners_lobby.id: 4}
        assert losers_call['rows'] == {losers_lobby.id: 3}
        assert winners_call['kwargs']['allow_undersized'] is False
        # The planned shortfall is a complete roster.
        assert losers_call['kwargs']['allow_undersized'] is True
        facts = _lobbies(tournament)
        at = winners_call['kwargs']['occurred_at']
        assert losers_call['kwargs']['occurred_at'] == at
        assert facts[winners_lobby.id]['occupied_since'] == at
        assert facts[losers_lobby.id]['occupied_since'] == at
        opened = _open(tournament)
        assert {e.opened_at for e in opened.values()} == {at}
        assert {winners_lobby.id, losers_lobby.id} == set(opened)

    else:
        tournament, winner = _to_waiting_winner(make_ffa, start, admin)
        matches.advance_ffa_round(
            tournament.id, pool=WINNERS, initiator_id=admin.id
        ).unwrap()
        for lobby in _round(tournament, LOSERS, 1):
            _play(lobby, admin)
        eligible = matches.advance_ffa_round(tournament.id, pool=LOSERS)
        assert eligible.unwrap() == 'grand_final_eligible'
        assert _open(tournament) == {}
        before = len(marker_calls)

        generated = matches.generate_ffa_grand_final(
            tournament.id, initiator_id=admin.id
        )

        assert generated.is_ok(), generated.unwrap_err()
        (call,) = marker_calls[before:]
        (final,) = _round(tournament, GRAND_FINAL, 0)
        # The Grand Final is exempt from the minimum lobby size.
        assert call['rows'] == {final.id: 3}
        assert call['kwargs']['allow_undersized'] is True
        occupied = _lobbies(tournament)[final.id]['occupied_since']
        assert occupied == call['kwargs']['occurred_at']
        # Occupancy and demand were committed together.
        (episode,) = _open(tournament).values()
        assert episode.match_id == final.id
        assert episode.opened_at == occupied


# fmt: off
@pytest.mark.parametrize(
    'failure', ['reconcile-err', 'reconcile-raises', 'losers-marker-err']
)
# fmt: on
def test_wb_and_companion_lb_timing_is_atomic(
    failure, make_ffa, start, admin, clock, monkeypatch
):
    tournament = make_ffa(mode=DE, size=16, cut=1, generate='seeding')
    start(tournament)
    for lobby in _round(tournament, WINNERS, 0):
        _play(lobby, admin)
    facts_before = _lobbies(tournament)
    episodes_before = {e.id for e in _episodes(tournament)}
    audit_before = _audit_count(tournament, 'bracket-lobby-undersized')
    seen = {}

    real_reconcile = operational.reconcile_due_matches_flush
    real_marker = operational.mark_completed_lobbies_occupied_flush
    marks = []

    def failing_reconcile(tournament_id, *, occurred_at):
        # Both pools exist in the transaction when the clock fails.
        seen['winners'] = repo.get_matches_for_round(
            tournament.id, 1, bracket=WINNERS
        )
        seen['losers'] = repo.get_matches_for_round(
            tournament.id, 0, bracket=LOSERS
        )
        if failure == 'reconcile-raises':
            raise RuntimeError('clock failed')
        return Err('reconcile_failed')

    def failing_marker(match_ids, **kwargs):
        marks.append(list(match_ids))
        if len(marks) == 2:
            return Err('lobby_roster_incomplete')
        return real_marker(match_ids, **kwargs)

    if failure == 'losers-marker-err':
        monkeypatch.setattr(
            operational, 'mark_completed_lobbies_occupied_flush', failing_marker
        )
    else:
        monkeypatch.setattr(
            operational, 'reconcile_due_matches_flush', failing_reconcile
        )

    def advance():
        return matches.advance_ffa_round(
            tournament.id, pool=WINNERS, initiator_id=admin.id
        )

    if failure == 'reconcile-raises':
        with pytest.raises(RuntimeError):
            advance()
    else:
        assert advance().is_err()

    if failure == 'losers-marker-err':
        assert len(marks) == 2
    else:
        assert len(seen['winners']) == 1
        assert len(seen['losers']) == 3
    # Neither pool, no occupancy, no episode and no audit entry remains.
    assert _lobbies(tournament) == facts_before
    assert {e.id for e in _episodes(tournament)} == episodes_before
    assert _audit_count(tournament, 'bracket-lobby-undersized') == audit_before
    assert not _round(tournament, WINNERS, 1)
    assert not _round(tournament, LOSERS, 0)

    monkeypatch.setattr(
        operational, 'reconcile_due_matches_flush', real_reconcile
    )
    monkeypatch.setattr(
        operational, 'mark_completed_lobbies_occupied_flush', real_marker
    )
    advanced = advance()

    assert advanced.is_ok(), advanced.unwrap_err()
    (winners_lobby,) = _round(tournament, WINNERS, 1)
    losers = _round(tournament, LOSERS, 0)
    assert len(losers) == 3
    new = {winners_lobby.id, *(m.id for m in losers)}
    facts = _lobbies(tournament)
    # One operation: one occupancy time, one episode time, one clock value.
    assert len({facts[i]['occupied_since'] for i in new}) == 1
    assert {facts[i]['last_changed_at'] for i in new} == {
        facts[i]['occupied_since'] for i in new
    }
    opened = [e for e in _open(tournament).values() if e.match_id in new]
    assert {e.match_id for e in opened} == new
    assert len({e.opened_at for e in opened}) == 1
    assert len({e.opened_clock_us for e in opened}) == 1
    assert {e.opened_at for e in opened} == {
        facts[i]['occupied_since'] for i in new
    }


def test_waiting_survivor_without_lobby_has_no_demand(
    make_ffa, start, admin, clock
):
    tournament, winner = _to_waiting_winner(make_ffa, start, admin)

    advanced = matches.advance_ffa_round(
        tournament.id, pool=WINNERS, initiator_id=admin.id
    )

    assert advanced.is_ok(), advanced.unwrap_err()
    # The winners survivor waits: no winners lobby exists for the next round.
    assert not _round(tournament, WINNERS, 2)
    lobbies = _round(tournament, LOSERS, 1)
    assert sorted(len(_members(m)) for m in lobbies) == [3, 3]
    opened = _open(tournament)
    # Demand exists for the real lobbies only, and never for the survivor.
    assert set(opened) == {m.id for m in lobbies}
    assert all(winner not in _members(m) for m in lobbies)
    real = {m.id for m in _all_matches(tournament)}
    assert {e.match_id for e in _episodes(tournament)} <= real

    for lobby in lobbies:
        _play(lobby, admin)
    eligible = matches.advance_ffa_round(tournament.id, pool=LOSERS)

    # Every lobby is confirmed and the Grand Final does not exist yet.
    assert eligible.unwrap() == 'grand_final_eligible'
    assert _open(tournament) == {}


# fmt: off
@pytest.mark.parametrize('owner', ['atomic', 'separate'])
# fmt: on
def test_ffa_atomic_result_freezes_terminal_clock(
    owner, make_ffa, start, admin, clock
):
    tournament = make_ffa(size=4, cut=1)
    started_at = start(tournament)
    (lobby,) = _all_matches(tournament)
    order = _members(lobby)
    placements = {cid: i + 1 for i, cid in enumerate(order)}
    (episode,) = _open(tournament).values()
    assert episode.match_id == lobby.id

    if owner == 'atomic':
        result = matches.set_and_confirm_ffa_match(
            lobby.id, placements, admin.id
        )
        assert result.is_ok(), result.unwrap_err()
    else:
        matches.set_ffa_placements(lobby.id, placements).unwrap()
        # Placements alone change nothing about the demand.
        assert set(_open(tournament)) == {lobby.id}
        assert _tournament_row(tournament)[0] == ONGOING.name
        matches.confirm_ffa_match(lobby.id, admin.id).unwrap()

    status, elapsed_us, running_since, activated_at = _tournament_row(
        tournament
    )
    facts = _lobbies(tournament)[lobby.id]
    (closed,) = _episodes(tournament)
    # The clock froze when the result decided the tournament ...
    assert status == TournamentStatus.COMPLETED.name
    assert running_since is None
    assert activated_at == started_at
    assert facts['confirmed_by'] == admin.id
    at = facts['last_changed_at']
    assert at >= START + timedelta(hours=2)
    assert elapsed_us == int((at - started_at).total_seconds() * 1_000_000)
    # ... and the episode closed at that very moment and clock value.
    assert closed.id == episode.id
    assert closed.closed_at == at
    assert closed.closed_clock_us == elapsed_us
    assert _open(tournament) == {}
    assert _audit_count(tournament, 'ffa-match-confirmed') == 1


# fmt: off
@pytest.mark.parametrize('failure', ['err', 'raises'])
# fmt: on
def test_a_failed_clock_leaves_the_result_uncommitted(
    failure, make_ffa, start, admin, clock, monkeypatch
):
    tournament = make_ffa(size=4, cut=1)
    start(tournament)
    (lobby,) = _all_matches(tournament)
    placements = {cid: i + 1 for i, cid in enumerate(_members(lobby))}
    before = _tournament_row(tournament)
    stamp = _lobbies(tournament)[lobby.id]['last_changed_at']
    (episode,) = _open(tournament).values()

    def failing(tournament_id, *, occurred_at):
        if failure == 'raises':
            raise RuntimeError('clock failed')
        return Err('reconcile_failed')

    monkeypatch.setattr(operational, 'reconcile_due_matches_flush', failing)

    if failure == 'raises':
        with pytest.raises(RuntimeError):
            matches.set_and_confirm_ffa_match(lobby.id, placements, admin.id)
    else:
        result = matches.set_and_confirm_ffa_match(
            lobby.id, placements, admin.id
        )
        assert result.unwrap_err() == 'reconcile_failed'

    # Placements, confirmation, completion, clock and audit: all or nothing.
    assert _placements(lobby.id) == [None] * 4
    facts = _lobbies(tournament)[lobby.id]
    assert facts['confirmed_by'] is None
    assert facts['last_changed_at'] == stamp
    assert _tournament_row(tournament) == before
    assert _audit_count(tournament, 'ffa-match-confirmed') == 0
    assert [e.id for e in _open(tournament).values()] == [episode.id]
    assert _episodes(tournament)[0].closed_at is None


# -- tournaments whose timing nobody can read --


def test_started_tournament_without_history_records_no_timing(
    make_ffa, admin, clock, marker_calls
):
    tournament = make_ffa(size=8, status=ONGOING, generate='direct')

    assert marker_calls == []
    assert {f['occupied_since'] for f in _lobbies(tournament).values()} == {
        None
    }
    assert _episodes(tournament) == []
    assert _tournament_row(tournament)[3] is None


def test_unstarted_tournament_records_occupancy_but_no_episode(
    make_ffa, admin, clock, marker_calls
):
    tournament = make_ffa(size=8, status=CLOSED, generate='direct')

    (call,) = marker_calls
    facts = _lobbies(tournament)
    assert set(call['ids']) == set(facts)
    assert all(f['occupied_since'] is not None for f in facts.values())
    assert _episodes(tournament) == []
    assert _tournament_row(tournament)[3] is None
