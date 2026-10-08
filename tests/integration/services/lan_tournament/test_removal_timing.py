from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, text

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_dashboard_service as dashboard,
    tournament_match_service as matches,
    tournament_participant_service as participants,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
    tournament_team_service as teams,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    OperationalClock,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    clock_value_us,
    derive_due_match_ids,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


# Far from the real time of generation, so a fact that took the wall
# clock instead of the operation time cannot equal a reading by accident.
START = datetime(2031, 9, 10, 18, 0, 0)
SCHEDULED = datetime(2031, 9, 10, 15, 0, 0)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
MINUTE_US = 60 * 1_000_000

CLOSED = TournamentStatus.REGISTRATION_CLOSED
OPEN = TournamentStatus.REGISTRATION_OPEN
ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
COMPLETED = TournamentStatus.COMPLETED

SE = EliminationMode.SINGLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN

EPISODES = 'lan_tournament_match_due_episodes'
LONG_AGO = datetime(2001, 1, 1)

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)


class _Clock:
    """Stand in for the server operation time, one reading per call."""

    def __init__(self, now: datetime, step: timedelta = timedelta(seconds=1)):
        self.now = now
        self.step = step
        self.log: list[datetime] = []

    def __call__(self) -> datetime:
        value = self.now
        self.log.append(value)
        self.now += self.step
        return value


@pytest.fixture(scope='module')
def party(make_party, brand):
    suffix = str(generate_uuid7())
    return make_party(brand, PartyID(f'removal-timing-{suffix}'), 'Removal')


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'RemovalTiming{suffix}P{i}') for i in range(10)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'RemovalTimingAdmin{str(generate_uuid7())[:18]}')


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin(
        {'lan_tournament.administrate'},
        screen_name=f'RemovalTimingViewer{str(generate_uuid7())[:18]}',
    )
    return CurrentUser.create_authenticated(
        user, None, frozenset({'lan_tournament.administrate'})
    )


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(START)
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


@pytest.fixture
def worlds(party, players, admin, clock):
    """Build committed tournaments, and delete them afterwards."""
    created: list = []

    def _create(**fields):
        result = tournament_service.create_tournament(
            party.id,
            f'Removal timing {generate_uuid7()}',
            start_time=SCHEDULED,
            tournament_status=fields.pop('status', CLOSED),
            **fields,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        return tournament

    def _register(tournament, user, team_id=None):
        participant = TournamentParticipant(
            id=TournamentParticipantID(generate_uuid7()),
            user_id=user.id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=team_id,
            created_at=SCHEDULED,
        )
        repo.create_participant(participant)
        return participant

    def _generate(tournament):
        board = seeding.get_board(tournament.id, initiator_id=admin.id)
        generated = seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.unwrap().version,
            initiator_id=admin.id,
        )
        assert generated.is_ok(), generated.unwrap_err()

    def solo(count=4, *, mode=SE, status=CLOSED, generate=True):
        tournament = _create(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=mode,
            status=status,
            max_players=16,
        )
        for user in players[:count]:
            _register(tournament, user)
        db.session.commit()
        if generate:
            _generate(tournament)
        return tournament

    def team(rosters, *, captains=None, loose=(), mode=SE, status=CLOSED):
        """Build a team tournament; rosters and loose are player indexes.

        The first player of a roster captains the team, unless `captains`
        names another player of the party for that team.
        """
        tournament = _create(
            contestant_type=ContestantType.TEAM,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=mode,
            status=status,
        )
        team_ids = []
        for i, roster in enumerate(rosters):
            captain = (captains or {}).get(i, roster[0])
            team_id = TournamentTeamID(generate_uuid7())
            repo.create_team(
                TournamentTeam(
                    id=team_id,
                    tournament_id=tournament.id,
                    name=f'Removal team {i} {team_id}',
                    tag=None,
                    description=None,
                    image_url=None,
                    captain_user_id=players[captain].id,
                    join_code=None,
                    created_at=SCHEDULED,
                )
            )
            team_ids.append(team_id)
        db.session.flush()
        for team_id, roster in zip(team_ids, rosters, strict=True):
            for index in roster:
                _register(tournament, players[index], team_id)
        for index in loose:
            _register(tournament, players[index])
        db.session.commit()
        _generate(tournament)
        return SimpleNamespace(tournament=tournament, team_ids=team_ids)

    yield SimpleNamespace(
        solo=solo, team=team, create=_create, register=_register
    )
    db.session.rollback()
    for tournament in created:
        if repo.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def start(admin, clock):
    """Start a tournament well after everything its generation sampled."""

    def _start(tournament) -> datetime:
        clock.now = START + HOUR
        started_at = clock.now
        result = tournament_service.change_status(
            tournament.id, ONGOING, admin.id
        )
        assert result.is_ok(), result.unwrap_err()
        clock.now = START + 2 * HOUR
        return started_at

    return _start


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


# -- observation: only committed data, read through another connection --


def _read(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).mappings().all()


def _stamps(tournament) -> dict[UUID, datetime | None]:
    return {
        row['id']: row['last_changed_at']
        for row in _read(
            'SELECT id, last_changed_at FROM lan_tournament_matches'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _age_stamps(*tournaments) -> None:
    """Give every match a change from long before any reading below.

    A stamp that a path should not write then shows, whichever clock it
    takes: the stored time never moves backwards.
    """
    db.session.rollback()
    for tournament in tournaments:
        db.session.execute(
            text(
                'UPDATE lan_tournament_matches SET last_changed_at = :at'
                ' WHERE tournament_id = :id'
            ),
            {'at': LONG_AGO, 'id': tournament.id},
        )
    db.session.commit()


@contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        yield seen
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)


def _moved(before: dict, after: dict) -> set[UUID]:
    return {i for i in after if after[i] != before.get(i)}


def _matches(tournament):
    db.session.rollback()
    return sorted(
        repo.get_matches_for_tournament(tournament.id),
        key=lambda m: (m.phase, m.round or 0, m.match_order),
    )


def _entries(tournament) -> dict[UUID, set[str]]:
    found = _matches(tournament)
    rows = repo.get_contestants_for_matches([m.id for m in found])
    return {
        m.id: {str(c.participant_id or c.team_id) for c in rows.get(m.id, [])}
        for m in found
    }


def _confirmed(tournament) -> set[UUID]:
    return {m.id for m in _matches(tournament) if m.confirmed_by is not None}


def _episodes(tournament) -> list:
    return list(
        _read(
            'SELECT id, match_id, opened_at, opened_clock_us, closed_at,'
            ' closed_clock_us, xmin::text AS version'
            ' FROM lan_tournament_match_due_episodes'
            ' WHERE tournament_id = :id ORDER BY opened_at, id',
            id=tournament.id,
        )
    )


def _open_episodes(tournament) -> dict[UUID, dict]:
    return {
        e['match_id']: e
        for e in _episodes(tournament)
        if e['closed_at'] is None
    }


def _closed_episodes(tournament) -> dict[UUID, dict]:
    return {
        e['match_id']: e
        for e in _episodes(tournament)
        if e['closed_at'] is not None
    }


def _tournament(tournament):
    db.session.rollback()
    return repo.get_tournament(tournament.id)


def _clock_of(tournament) -> OperationalClock:
    found = _tournament(tournament)
    return OperationalClock(
        elapsed_us=found.operational_clock_elapsed_us,
        running_since=found.operational_clock_running_since,
        activated_at=found.operational_clock_activated_at,
    )


def _due(tournament) -> set[UUID]:
    """Return what the pure policy says is due in the committed state."""
    found = _tournament(tournament)
    ms = repo.get_matches_for_tournament_ordered_fresh(tournament.id)
    rows = repo.get_contestants_for_matches([m.id for m in ms])
    completed = frozenset(m.id for m in ms if m.occupied_since is not None)
    return set(derive_due_match_ids(found, ms, rows, completed))


def _assert_demand_is_current(tournament) -> None:
    """The open episodes are exactly the due matches: no phantom, no gap."""
    assert set(_open_episodes(tournament)) == _due(tournament)


def _participant_of(tournament, user):
    db.session.rollback()
    return next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if p.user_id == user.id
    )


def _first_round(tournament):
    found = _matches(tournament)
    first = min(m.round or 0 for m in found)
    return [m for m in found if (m.round or 0) == first]


def _final(tournament):
    """Return the final, not the match for third place."""
    return next(
        m
        for m in _matches(tournament)
        if m.next_match_id is None and m.bracket is None
    )


def _members(tournament, match) -> list[str]:
    return sorted(_entries(tournament)[match.id])


def _play(tournament, match, admin):
    """Let the lower ID win 2:0."""
    winner, loser = _members(tournament, match)
    result = matches.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(winner): 2, UUID(loser): 0}
    )
    assert result.is_ok(), result.unwrap_err()


def _operation_time(clock, mark: int) -> datetime:
    """Return the operation time: the one reading taken since `mark`.

    A transaction samples the clock once and shares the value, so a second
    reading is a fact that took a time of its own.
    """
    readings = clock.log[mark:]
    assert len(readings) == 1, readings
    return readings[0]


# -- pre-start: the generated layout is stripped and the matches stamped --


@pytest.mark.parametrize(
    'owner',
    [
        'leave_tournament',
        'admin_remove_participant',
        'remove_participants_without_tickets',
        'delete_team',
        'leave_team',
        'remove_team_member',
    ],
)
def test_prestart_removal_strips_and_timestamps_entries(
    worlds, players, admin, clock, monkeypatch, owner
):
    if owner in ('delete_team', 'leave_team', 'remove_team_member'):
        # Four teams. The captain of the first one stands outside of its
        # roster for `remove_team_member`, which only empties a team then.
        emptying = owner == 'remove_team_member'
        world = worlds.team(
            [[0], [1], [2], [3]],
            captains={0: 8} if emptying else None,
            loose=[8] if emptying else [],
        )
        tournament = world.tournament
        team_id = world.team_ids[0]
        removed_entry = str(team_id)
    else:
        tournament = worlds.solo(4)
        if owner == 'leave_tournament':
            # The bracket was generated while registration was closed.
            changed = tournament_service.change_status(
                tournament.id, OPEN, admin.id
            )
            assert changed.is_ok(), changed.unwrap_err()
        victim = _participant_of(tournament, players[0])
        removed_entry = str(victim.id)

    before_entries = _entries(tournament)
    before_stamps = _stamps(tournament)
    stripped = {
        i for i, held in before_entries.items() if removed_entry in held
    }
    assert len(stripped) == 1
    clock.now = START + 2 * HOUR

    if owner == 'leave_tournament':
        result = participants.leave_tournament(tournament.id, victim.id)
    elif owner == 'admin_remove_participant':
        result = participants.admin_remove_participant(
            tournament.id, victim.id, initiator=admin
        )
    elif owner == 'remove_participants_without_tickets':
        everyone_else = {u.id for u in players} - {players[0].id}
        monkeypatch.setattr(
            participants.ticket_service,
            'select_ticket_users_for_party',
            lambda user_ids, party_id: everyone_else,
        )
        result = participants.remove_participants_without_tickets(
            tournament.id, tournament.party_id, initiator_id=admin.id
        )
    elif owner == 'delete_team':
        result = teams.delete_team(team_id)
    elif owner == 'leave_team':
        result = teams.leave_team(_participant_of(tournament, players[0]).id)
    else:
        result = teams.remove_team_member(team_id, players[0].id)
    assert result.is_ok(), result

    after_entries = _entries(tournament)
    after_stamps = _stamps(tournament)
    # The removed entry is gone from the one match that held it, and
    # nothing else of the layout moved.
    assert all(removed_entry not in held for held in after_entries.values())
    for match_id, held in after_entries.items():
        if match_id in stripped:
            assert held == before_entries[match_id] - {removed_entry}
        else:
            assert held == before_entries[match_id]
    # Exactly the stripped match carries a newer change; the others keep
    # the stamp of their generation.
    assert _moved(before_stamps, after_stamps) == stripped
    for match_id in stripped:
        assert after_stamps[match_id] > before_stamps[match_id]
        assert after_stamps[match_id] >= START + 2 * HOUR
    # Nothing here is tracked: no clock, no demand, no episode.
    assert _clock_of(tournament) == OperationalClock()
    assert _episodes(tournament) == []
    assert _confirmed(tournament) == set()


# -- a started tournament: a walkover updates source and destinations --


def _remove(owner, tournament, victim, admin, players, monkeypatch):
    initiator = admin
    if owner == 'admin_remove_participant':
        return participants.admin_remove_participant(
            tournament.id, victim.id, initiator=initiator
        )
    keep = {u.id for u in players} - {victim.user_id}
    monkeypatch.setattr(
        participants.ticket_service,
        'select_ticket_users_for_party',
        lambda user_ids, party_id: keep,
    )
    return participants.remove_participants_without_tickets(
        tournament.id, tournament.party_id, initiator_id=initiator.id
    )


@pytest.mark.parametrize('owner', ['admin_remove_participant', 'ticketless'])
def test_removal_walkover_covers_destination_matches(
    worlds, players, admin, clock, start, monkeypatch, owner
):
    """Source, walkover and destination change together, with their episodes."""
    tournament = worlds.solo(4)
    start(tournament)
    first, second = _first_round(tournament)
    final = _final(tournament)
    victim_id, other_id = _members(tournament, first)
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    before_entries = _entries(tournament)
    before_stamps = _stamps(tournament)
    before_open = _open_episodes(tournament)
    assert set(before_open) == {first.id, second.id}
    assert _due(tournament) == {first.id, second.id}
    mark = len(clock.log)

    result = _remove(owner, tournament, victim, admin, players, monkeypatch)
    assert result.is_ok(), result
    operation_time = _operation_time(clock, mark)

    # The walkover advanced the opponent: source, walkover match and the
    # destination are the actually changed matches, and only those.
    assert _entries(tournament)[first.id] == {other_id}
    assert _entries(tournament)[final.id] == {other_id}
    assert _entries(tournament)[second.id] == before_entries[second.id]
    assert _confirmed(tournament) == {first.id}
    after_stamps = _stamps(tournament)
    assert _moved(before_stamps, after_stamps) == {first.id, final.id}
    assert after_stamps[second.id] == before_stamps[second.id]
    # One transaction, one time: the stamps, the confirmation and the
    # episodes all carry the reading the owner took.
    assert after_stamps[first.id] == operation_time
    assert after_stamps[final.id] == operation_time

    # The walkover match is no demand any more, the final has one side,
    # the untouched match keeps its very episode.
    closed = _closed_episodes(tournament)
    assert set(closed) == {first.id}
    assert closed[first.id]['closed_at'] == operation_time
    assert closed[first.id]['id'] == before_open[first.id]['id']
    kept = _open_episodes(tournament)
    assert set(kept) == {second.id}
    assert kept[second.id]['version'] == before_open[second.id]['version']
    _assert_demand_is_current(tournament)


@pytest.mark.parametrize('owner', ['admin_remove_participant', 'ticketless'])
def test_removal_walkover_opens_the_destination_when_it_becomes_due(
    worlds, players, admin, clock, start, monkeypatch, owner
):
    tournament = worlds.solo(4)
    start(tournament)
    first, second = _first_round(tournament)
    final = _final(tournament)
    _play(tournament, second, admin)
    victim_id, other_id = _members(tournament, first)
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    assert set(_open_episodes(tournament)) == {first.id}
    mark = len(clock.log)

    result = _remove(owner, tournament, victim, admin, players, monkeypatch)
    assert result.is_ok(), result
    operation_time = _operation_time(clock, mark)

    # Both sides of the final are there now: a new demand, from this
    # operation on, at the running clock of this very moment.
    assert len(_entries(tournament)[final.id]) == 2
    opened = _open_episodes(tournament)
    assert set(opened) == {final.id}
    assert opened[final.id]['opened_at'] == operation_time
    assert opened[final.id]['opened_clock_us'] == clock_value_us(
        _clock_of(tournament), operation_time
    )
    assert set(_closed_episodes(tournament)) == {first.id, second.id}
    _assert_demand_is_current(tournament)


def test_removal_walkover_completes_the_tournament_at_the_frozen_clock(
    worlds, players, admin, clock, start
):
    tournament = worlds.solo(2)
    started_at = start(tournament)
    (match,) = _matches(tournament)
    victim_id, other_id = _members(tournament, match)
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    (before,) = _open_episodes(tournament).values()
    mark = len(clock.log)

    result = participants.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    )
    assert result.is_ok(), result
    operation_time = _operation_time(clock, mark)

    found = _tournament(tournament)
    assert found.tournament_status is COMPLETED
    assert str(found.winner_participant_id) == other_id
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    assert started_at <= frozen.activated_at < started_at + MINUTE
    # The completion edge is the operation time: the clock stopped at the
    # very reading the walkover took, not at one of its own.
    assert frozen.elapsed_us == (
        operation_time - frozen.activated_at
    ) // timedelta(microseconds=1)
    assert frozen.elapsed_us >= 1 * MINUTE_US
    assert _open_episodes(tournament) == {}
    (episode,) = _closed_episodes(tournament).values()
    assert episode['id'] == before['id']
    assert episode['closed_at'] == operation_time
    assert episode['closed_clock_us'] == frozen.elapsed_us
    assert _due(tournament) == set()


def test_removal_walkover_completes_a_plain_round_robin_at_the_operation_time(
    worlds, players, admin, clock, start
):
    tournament = worlds.solo(2, mode=RR)
    start(tournament)
    (match,) = _matches(tournament)
    victim_id, other_id = _members(tournament, match)
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    mark = len(clock.log)

    result = participants.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    )
    assert result.is_ok(), result
    operation_time = _operation_time(clock, mark)

    # The last match was a walkover: the table is decided, and the clock
    # stops at the time of that very walkover.
    found = _tournament(tournament)
    assert found.tournament_status is COMPLETED
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    assert frozen.elapsed_us == (
        operation_time - frozen.activated_at
    ) // timedelta(microseconds=1)
    assert _open_episodes(tournament) == {}
    (episode,) = _closed_episodes(tournament).values()
    assert episode['closed_at'] == operation_time


def test_removal_walkover_during_a_pause_uses_the_frozen_clock(
    worlds, players, admin, clock, start
):
    tournament = worlds.solo(4)
    start(tournament)
    first, second = _first_round(tournament)
    _play(tournament, second, admin)
    clock.now = START + 3 * HOUR
    assert tournament_service.change_status(
        tournament.id, PAUSED, admin.id
    ).is_ok()
    frozen = _clock_of(tournament)
    clock.now = START + 5 * HOUR
    victim_id, _ = _members(tournament, first)
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    mark = len(clock.log)

    result = participants.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    )
    assert result.is_ok(), result
    _operation_time(clock, mark)

    # The final turned due while the clock stood: its wait starts at the
    # frozen value, and the two hours of pause never enter it.
    assert _clock_of(tournament) == frozen
    (episode,) = _open_episodes(tournament).values()
    assert episode['match_id'] == _final(tournament).id
    assert episode['opened_clock_us'] == frozen.elapsed_us
    assert set(_closed_episodes(tournament)) == {first.id, second.id}


def test_removal_walkover_moves_the_round_robin_frontier(
    worlds, players, admin, clock, start
):
    tournament = worlds.solo(4, mode=RR)
    start(tournament)
    found = _matches(tournament)
    rounds = sorted({m.round for m in found})
    assert len(rounds) == 3
    by_round = {r: [m for m in found if m.round == r] for r in rounds}
    first = by_round[rounds[0]]
    # Play one match of the first round, then remove a player of the
    # other one: the round is complete once the walkover is in.
    _play(tournament, first[0], admin)
    victim_id, _ = _members(tournament, first[1])
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    assert set(_open_episodes(tournament)) == {first[1].id}
    mark = len(clock.log)

    result = participants.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    )
    assert result.is_ok(), result
    operation_time = _operation_time(clock, mark)

    # The frontier moved to round two; its playable sibling is a demand
    # although no `next_match_id` links it to anything that changed. All
    # of it happened at the one time the owner took.
    _assert_demand_is_current(tournament)
    assert {e['opened_at'] for e in _open_episodes(tournament).values()} == {
        operation_time
    }
    opened = set(_open_episodes(tournament))
    assert first[1].id not in opened
    assert opened
    assert opened <= {m.id for m in by_round[rounds[1]]}


@pytest.mark.parametrize('owner', ['remove_team_member', 'ticketless'])
def test_removal_walkover_of_an_emptied_team_covers_the_destination(
    worlds, players, admin, clock, start, monkeypatch, owner
):
    # Four teams; the captain of team 0 sits outside its roster, so the
    # removal of its one member empties the team.
    world = worlds.team([[0], [1], [2], [3]], captains={0: 8}, loose=[8])
    tournament = world.tournament
    start(tournament)
    first, second = _first_round(tournament)
    final = _final(tournament)
    team_id = str(world.team_ids[0])
    held = _entries(tournament)
    mine, other = (
        (first, second) if team_id in held[first.id] else (second, first)
    )
    (rival,) = held[mine.id] - {team_id}
    before_stamps = _stamps(tournament)
    mark = len(clock.log)

    if owner == 'remove_team_member':
        result = teams.remove_team_member(
            world.team_ids[0], players[0].id, initiator_id=admin.id
        )
    else:
        keep = {u.id for u in players} - {players[0].id}
        monkeypatch.setattr(
            participants.ticket_service,
            'select_ticket_users_for_party',
            lambda user_ids, party_id: keep,
        )
        result = participants.remove_participants_without_tickets(
            tournament.id, tournament.party_id, initiator_id=admin.id
        )
    assert result.is_ok(), result
    operation_time = _operation_time(clock, mark)

    assert _entries(tournament)[mine.id] == {rival}
    assert _entries(tournament)[final.id] == {rival}
    assert _confirmed(tournament) == {mine.id}
    after_stamps = _stamps(tournament)
    assert _moved(before_stamps, after_stamps) == {mine.id, final.id}
    assert after_stamps[mine.id] == after_stamps[final.id] == operation_time
    closed = _closed_episodes(tournament)
    assert set(closed) == {mine.id}
    assert closed[mine.id]['closed_at'] == operation_time
    assert set(_open_episodes(tournament)) == {other.id}
    _assert_demand_is_current(tournament)


def test_deleting_a_team_samples_one_operation_time_for_everything(
    worlds, players, admin, clock, start
):
    world = worlds.team([[0], [1], [2], [3]])
    tournament = world.tournament
    start(tournament)
    first, second = _first_round(tournament)
    team_id = str(world.team_ids[0])
    held = _entries(tournament)
    mine, other = (
        (first, second) if team_id in held[first.id] else (second, first)
    )
    before_stamps = _stamps(tournament)
    mark = len(clock.log)

    result = teams.delete_team(world.team_ids[0])
    assert result.is_ok(), result

    # One reading for the stamp of the match that lost the team and for
    # the close of its episode.
    operation_time = _operation_time(clock, mark)
    after_stamps = _stamps(tournament)
    assert _moved(before_stamps, after_stamps) == {mine.id}
    assert after_stamps[mine.id] == operation_time
    closed = _closed_episodes(tournament)
    assert set(closed) == {mine.id}
    assert closed[mine.id]['closed_at'] == operation_time
    assert set(_open_episodes(tournament)) == {other.id}
    _assert_demand_is_current(tournament)


@pytest.mark.parametrize(
    'owner',
    [
        'admin_remove_participant',
        'ticketless',
        'delete_team',
        'remove_team_member',
    ],
)
def test_a_failing_reconcile_rolls_the_removal_back(
    worlds, players, admin, clock, start, monkeypatch, owner
):
    if owner in ('delete_team', 'remove_team_member'):
        emptying = owner == 'remove_team_member'
        world = worlds.team(
            [[0], [1], [2], [3]],
            captains={0: 8} if emptying else None,
            loose=[8] if emptying else [],
        )
        tournament = world.tournament
    else:
        tournament = worlds.solo(4)
    start(tournament)
    monkeypatch.setattr(
        participants.tournament_operational_service,
        'reconcile_due_matches_flush',
        lambda tournament_id, *, occurred_at: Err('reconcile_failed'),
    )
    entries = _entries(tournament)
    stamps = _stamps(tournament)
    episodes = _episodes(tournament)
    status = _tournament(tournament).tournament_status

    def log_entries() -> int:
        return _read(
            'SELECT count(*) AS n FROM lan_tournament_log_entries'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )[0]['n']

    log_count = log_entries()

    if owner == 'delete_team':
        result = teams.delete_team(world.team_ids[0])
    elif owner == 'remove_team_member':
        result = teams.remove_team_member(
            world.team_ids[0], players[0].id, initiator_id=admin.id
        )
    else:
        first, _ = _first_round(tournament)
        victim_id, _ = _members(tournament, first)
        victim = next(
            p
            for p in repo.get_participants_for_tournament(tournament.id)
            if str(p.id) == victim_id
        )
        result = _remove(owner, tournament, victim, admin, players, monkeypatch)

    assert result.is_err()
    assert result.unwrap_err() == 'reconcile_failed'
    assert _entries(tournament) == entries
    assert _stamps(tournament) == stamps
    assert _episodes(tournament) == episodes
    assert _tournament(tournament).tournament_status is status
    assert _confirmed(tournament) == set()
    assert log_entries() == log_count
    if owner in ('delete_team', 'remove_team_member'):
        teams_left = _read(
            'SELECT count(*) AS n FROM lan_tournament_teams'
            ' WHERE tournament_id = :id AND removed_at IS NULL',
            id=tournament.id,
        )[0]['n']
        assert teams_left == 4


# -- a team roster change moves demand, never a match --


@pytest.mark.parametrize(
    ('op', 'on_team', 'before', 'after'),
    # fmt: off
    [
        ('join_team', False, False, True),
        ('admin_add_member', False, False, True),
        ('remove_team_member', True, True, False),
        ('admin_remove_participant', True, True, False),
        ('remove_participants_without_tickets', True, True, False),
        ('transfer_captain', True, True, True),
    ],
    # fmt: on
)
def test_membership_only_change_affects_demand_not_match_timestamp(
    worlds,
    players,
    admin,
    viewer,
    clock,
    start,
    party,
    monkeypatch,
    op,
    on_team,
    before,
    after,
):
    # Team tournament: two teams, one match. Player 4 is a person of
    # that match (through team 0, maybe) and of a solo match.
    mates = [[0, 1, 4], [2, 3]] if on_team else [[0, 1], [2, 3]]
    world = worlds.team(mates, loose=[] if on_team else [4])
    tournament = world.tournament
    other = worlds.solo(0, generate=False)
    for user in (players[4], players[5]):
        worlds.register(other, user)
    db.session.commit()
    board = seeding.get_board(other.id, initiator_id=admin.id)
    assert seeding.generate_from_seeding(
        other.id, expected_version=board.unwrap().version, initiator_id=admin.id
    ).is_ok()
    start(tournament)
    start(other)
    _age_stamps(tournament, other)
    (match,) = _matches(tournament)
    (solo_match,) = _matches(other)
    team_id = world.team_ids[0]
    person = _participant_of(tournament, players[4])

    def conflicts() -> dict[UUID, set[UUID]]:
        db.session.rollback()
        page = dashboard.get_dashboard_page(
            viewer,
            party.id,
            DashboardQuery(scope='all', view='due', per_page=50),
            settings=SETTINGS,
            now=START + 4 * HOUR,
        ).unwrap()
        return {
            row.match_id: {c.user_id for c in row.conflicts}
            for row in page.rows
        }

    expected = {players[4].id} if before else set()
    assert conflicts() == {match.id: expected, solo_match.id: expected}
    stamps = {**_stamps(tournament), **_stamps(other)}
    assert set(stamps.values()) == {LONG_AGO}
    versions = [_episodes(tournament), _episodes(other)]
    assert set(_open_episodes(tournament)) == {match.id}

    with _statements() as seen:
        if op == 'join_team':
            result = teams.join_team(person.id, team_id)
        elif op == 'admin_add_member':
            result = teams.admin_add_member(team_id, players[4].id)
        elif op == 'remove_team_member':
            result = teams.remove_team_member(team_id, players[4].id)
        elif op == 'admin_remove_participant':
            result = participants.admin_remove_participant(
                tournament.id, person.id, initiator=admin
            )
        elif op == 'remove_participants_without_tickets':
            keep = {u.id for u in players} - {players[4].id}
            monkeypatch.setattr(
                participants.ticket_service,
                'select_ticket_users_for_party',
                lambda user_ids, party_id: keep,
            )
            result = participants.remove_participants_without_tickets(
                tournament.id, tournament.party_id, initiator_id=admin.id
            )
        else:
            result = teams.transfer_captain(team_id, players[1].id)
    assert result.is_ok(), result

    # The people the match asks for changed, so the conflict did ...
    expected = {players[4].id} if after else set()
    assert conflicts() == {match.id: expected, solo_match.id: expected}
    # ... and the match did not: neither its stamp, nor its episode, and
    # the timing layer was not even asked.
    assert {**_stamps(tournament), **_stamps(other)} == stamps
    assert [_episodes(tournament), _episodes(other)] == versions
    assert not [s for s in seen if EPISODES in s]
    assert _confirmed(tournament) == set()
    assert _confirmed(other) == set()


# -- without an initiator nobody confirms: no demand without two sides --


@pytest.mark.parametrize('owner', ['admin_remove_participant', 'ticketless'])
def test_removal_without_initiator_has_no_phantom_demand(
    worlds, players, admin, clock, start, monkeypatch, owner
):
    tournament = worlds.solo(4)
    start(tournament)
    first, second = _first_round(tournament)
    final = _final(tournament)
    victim_id, other_id = _members(tournament, first)
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )
    before_open = _open_episodes(tournament)
    mark = len(clock.log)

    if owner == 'admin_remove_participant':
        result = participants.admin_remove_participant(tournament.id, victim.id)
    else:
        keep = {u.id for u in players} - {victim.user_id}
        monkeypatch.setattr(
            participants.ticket_service,
            'select_ticket_users_for_party',
            lambda user_ids, party_id: keep,
        )
        result = participants.remove_participants_without_tickets(
            tournament.id, tournament.party_id
        )
    assert result.is_ok(), result

    # The opponent stands alone and advanced, and nobody confirmed.
    assert _entries(tournament)[first.id] == {other_id}
    assert _entries(tournament)[final.id] == {other_id}
    assert _confirmed(tournament) == set()
    # A one-sided match is not waiting for anyone: its episode ended with
    # the removal, and the one-sided final never opened one.
    closed = _closed_episodes(tournament)
    assert set(closed) == {first.id}
    assert closed[first.id]['closed_at'] == _operation_time(clock, mark)
    assert closed[first.id]['id'] == before_open[first.id]['id']
    kept = _open_episodes(tournament)
    assert set(kept) == {second.id}
    assert kept[second.id]['version'] == before_open[second.id]['version']
    assert _due(tournament) == {second.id}


def test_removal_without_initiator_in_a_round_robin_closes_the_one_sided_match(
    worlds, players, admin, clock, start
):
    tournament = worlds.solo(4, mode=RR)
    start(tournament)
    found = _matches(tournament)
    first_round = [m for m in found if m.round == min(x.round for x in found)]
    victim_id, other_id = _members(tournament, first_round[0])
    victim = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == victim_id
    )

    result = participants.admin_remove_participant(tournament.id, victim.id)
    assert result.is_ok(), result

    # The victim's matches are one-sided and unconfirmed; a one-sided
    # match of the open round still holds the later rounds back.
    sides = {len(held) for held in _entries(tournament).values()}
    assert 1 in sides
    assert _confirmed(tournament) == set()
    assert first_round[0].id not in _open_episodes(tournament)
    assert first_round[0].id in _closed_episodes(tournament)
    _assert_demand_is_current(tournament)
    assert set(_open_episodes(tournament)) == {first_round[1].id}


def test_removal_without_initiator_from_a_lobby_leaves_no_phantom_demand(
    worlds, players, admin, clock, start
):
    tournament = worlds.create(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=SE,
        max_players=16,
        point_table=[5, 3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
    )
    for user in players[:8]:
        worlds.register(tournament, user)
    db.session.commit()
    generated = matches.generate_ffa_round(
        tournament.id, bracket=None, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()
    start(tournament)
    lobbies = _matches(tournament)
    assert len(lobbies) == 2
    lobby, rest = lobbies
    assert set(_open_episodes(tournament)) == {lobby.id, rest.id}
    held = sorted(_entries(tournament)[lobby.id])
    untouched = _open_episodes(tournament)[rest.id]

    def drop(participant_id):
        participant = next(
            p
            for p in repo.get_participants_for_tournament(tournament.id)
            if str(p.id) == participant_id
        )
        mark = len(clock.log)
        result = participants.admin_remove_participant(
            tournament.id, participant.id
        )
        assert result.is_ok(), result
        return _operation_time(clock, mark)

    # A smaller lobby is a new demand with a new start ...
    first_old = _open_episodes(tournament)[lobby.id]
    at = drop(held[0])
    reopened = _open_episodes(tournament)[lobby.id]
    assert reopened['id'] != first_old['id']
    assert reopened['opened_at'] == at
    assert _closed_episodes(tournament)[lobby.id]['closed_at'] == at
    drop(held[1])
    # ... until a survivor stands alone: nobody plays against them.
    last_at = drop(held[2])
    assert len(_entries(tournament)[lobby.id]) == 1
    assert lobby.id not in _open_episodes(tournament)
    assert lobby.id in _closed_episodes(tournament)
    assert _closed_episodes(tournament)[lobby.id]['closed_at'] == last_at
    kept = _open_episodes(tournament)[rest.id]
    assert kept['version'] == untouched['version']
    assert _confirmed(tournament) == set()
    _assert_demand_is_current(tournament)
