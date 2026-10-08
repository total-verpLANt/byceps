"""
tests.integration.services.lan_tournament.test_operational_lifecycle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import event, select, text, update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as matches,
    tournament_operational_service,
    tournament_participant_service as participants,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.dashboard import DbMatchDueEpisode
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
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
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    clock_value_us,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('f03-operational-lifecycle')

# Far from the real time of generation, so a fact that wrongly took the
# generation or the scheduled time cannot equal the actual start.
START = datetime(2031, 3, 4, 18, 0, 0)
SCHEDULED = datetime(2031, 3, 4, 15, 0, 0)
MINUTE = timedelta(minutes=1)
MINUTE_US = 60 * 1_000_000

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
COMPLETED = TournamentStatus.COMPLETED
CANCELLED = TournamentStatus.CANCELLED
CLOSED = TournamentStatus.REGISTRATION_CLOSED

SE = EliminationMode.SINGLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN

EPISODES = 'lan_tournament_match_due_episodes'


class _Clock:
    """Stand in for the server operation time, one reading per call."""

    def __init__(self, now: datetime, step: timedelta = timedelta(0)):
        self.now = now
        self.step = step
        self.readings = 0

    def __call__(self) -> datetime:
        self.readings += 1
        value = self.now
        self.now += self.step
        return value


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 operational lifecycle')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03OperationalLifecycle{i}') for i in range(9)]


@pytest.fixture(scope='module')
def admin(users):
    return users[-1]


@pytest.fixture
def clock(monkeypatch):
    # A reading advances by a second, and the first one is far from
    # `START`, so a fact that was sampled when it should have been given
    # cannot be equal by accident.
    clock = _Clock(START + timedelta(hours=7), step=timedelta(seconds=1))
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        *,
        players=4,
        mode=SE,
        status=CLOSED,
        game_format=GameFormat.ONE_V_ONE,
        generate=True,
        **fields,
    ):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Operational lifecycle {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=game_format,
            elimination_mode=mode,
            tournament_status=status,
            start_time=SCHEDULED,
            max_players=16,
            **fields,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:players]:
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
        if generate:
            _generate(tournament, users[-1])
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if repo.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _generate(tournament, admin):
    board = seeding.get_board(tournament.id, initiator_id=admin.id).unwrap()
    seeding.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()


def _change(tournament, status, admin, **kwargs):
    result = tournament_service.change_status(
        tournament.id, status, admin.id, **kwargs
    )
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()[0]


def _tournament(tournament):
    """Read the committed tournament, never a cached row."""
    db.session.rollback()
    return repo.get_tournament(tournament.id)


def _clock_of(tournament) -> OperationalClock:
    found = _tournament(tournament)
    return OperationalClock(
        elapsed_us=found.operational_clock_elapsed_us,
        running_since=found.operational_clock_running_since,
        activated_at=found.operational_clock_activated_at,
    )


def _matches(tournament):
    return repo.get_matches_for_tournament(tournament.id)


def _members(match):
    return [
        str(c.participant_id or c.team_id)
        for c in repo.get_contestants_for_match(match.id)
    ]


def _play(match, admin):
    """Let the lower ID win 2:0."""
    winner, loser = sorted(_members(match))
    result = matches.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(winner): 2, UUID(loser): 0}
    )
    assert result.is_ok(), result.unwrap_err()


def _episodes(tournament) -> list[DbMatchDueEpisode]:
    db.session.rollback()
    return list(
        db.session.scalars(
            select(DbMatchDueEpisode)
            .where(DbMatchDueEpisode.tournament_id == tournament.id)
            .order_by(DbMatchDueEpisode.match_id, DbMatchDueEpisode.opened_at)
            .execution_options(populate_existing=True)
        )
    )


def _committed_rows(sql: str, **params):
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _episode_versions(tournament):
    """Return each episode with its row version, which any rewrite changes."""
    return _committed_rows(
        'SELECT id, match_id, xmin::text, closed_at, closed_clock_us'
        ' FROM lan_tournament_match_due_episodes'
        ' WHERE tournament_id = :id ORDER BY id',
        id=tournament.id,
    )


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


def _writes(statements: list[str]) -> list[str]:
    return [
        s
        for s in statements
        if s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
        or ' FOR UPDATE' in s.upper()
    ]


def _force_status(tournament, status, **columns):
    """Write a status and clock as an earlier release left them."""
    db.session.rollback()
    db.session.execute(
        update(DbTournament)
        .where(DbTournament.id == tournament.id)
        .values(tournament_status=status.name, **columns)
    )
    db.session.commit()


# -- start: actual demand, not the scheduled or generation time --


@pytest.mark.parametrize(
    ('mode', 'players', 'due'),
    # fmt: off
    [
        (SE, 4, 2),  # the two first-round matches, not the final
        (RR, 4, 2),  # the first round of the round-robin frontier only
    ],
    # fmt: on
)
def test_preassigned_matches_begin_at_actual_start(
    make_tournament, admin, clock, mode, players, due
):
    tournament = make_tournament(mode=mode, players=players)
    assert len(_matches(tournament)) >= due
    assert _clock_of(tournament) == OperationalClock()
    assert _episodes(tournament) == []
    clock.now = START

    started = _change(tournament, ONGOING, admin)

    found = _tournament(tournament)
    activated_at = found.operational_clock_activated_at
    # Neither the scheduled start, nor the generation, nor registration.
    assert START <= activated_at < START + MINUTE
    assert activated_at not in (SCHEDULED, tournament.created_at)
    assert found.operational_clock_running_since == activated_at
    assert found.operational_clock_elapsed_us == 0

    episodes = _episodes(tournament)
    assert len(episodes) == due
    assert {e.opened_at for e in episodes} == {activated_at}
    assert {e.opened_clock_us for e in episodes} == {0}
    assert all(e.closed_at is None for e in episodes)
    first_round = min(m.round for m in _matches(tournament))
    assert {e.match_id for e in episodes} == {
        m.id
        for m in _matches(tournament)
        if m.round == first_round and len(_members(m)) == 2
    }

    # What the lifecycle returns carries the facts it persisted.
    assert started.tournament_status is ONGOING
    assert started.operational_clock_activated_at == activated_at
    assert started.operational_clock_running_since == activated_at
    assert started.operational_clock_elapsed_us == 0

    # The wait of a due match counts from the start, not from the three
    # hours the match had been scheduled before it.
    clock_now = OperationalClock(
        elapsed_us=found.operational_clock_elapsed_us,
        running_since=found.operational_clock_running_since,
        activated_at=activated_at,
    )
    at = activated_at + 12 * MINUTE
    for episode in episodes:
        assert clock_value_us(clock_now, at) - episode.opened_clock_us == (
            12 * MINUTE_US
        )


def test_start_failing_to_reconcile_leaves_nothing_behind(
    make_tournament, admin, clock, monkeypatch
):
    tournament = make_tournament()
    monkeypatch.setattr(
        tournament_operational_service,
        'reconcile_due_matches_flush',
        lambda tournament_id, *, occurred_at: Err('reconcile_failed'),
    )

    result = tournament_service.change_status(tournament.id, ONGOING, admin.id)

    assert result.is_err()
    assert result.unwrap_err() == 'reconcile_failed'
    found = _tournament(tournament)
    assert found.tournament_status is CLOSED
    assert _clock_of(tournament) == OperationalClock()
    assert _episodes(tournament) == []
    assert _committed_rows(
        'SELECT count(*) FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id'
        " AND event_type = 'tournament-status-changed'",
        id=tournament.id,
    ) == [(0,)]


# -- pause, resume, terminal and reopen --


def test_pause_resume_preserves_episode_and_excludes_pause(
    make_tournament, admin, clock
):
    tournament = make_tournament()
    _change(tournament, ONGOING, admin)
    started = _clock_of(tournament)
    start = started.activated_at
    before = _episode_versions(tournament)
    assert len(before) == 2

    clock.now = start + 10 * MINUTE
    with _statements() as paused_statements:
        _change(tournament, PAUSED, admin)
    paused = _clock_of(tournament)
    assert paused.running_since is None
    assert paused.activated_at == start
    ten_minutes = paused.elapsed_us
    assert 10 * MINUTE_US <= ten_minutes < 11 * MINUTE_US
    assert _episode_versions(tournament) == before
    # A pause does not even look at the episodes.
    assert not [s for s in paused_statements if EPISODES in s]

    clock.now = start + 40 * MINUTE
    with _statements() as resumed_statements:
        _change(tournament, ONGOING, admin)
    resumed = _clock_of(tournament)
    assert resumed.running_since is not None
    assert resumed.running_since >= start + 40 * MINUTE
    assert resumed.elapsed_us == ten_minutes
    assert resumed.activated_at == start
    # The same rows, untouched: no renewal and no rewrite per match.
    assert _episode_versions(tournament) == before
    assert not _writes([s for s in resumed_statements if EPISODES in s])

    # The thirty paused minutes never entered the wait.
    episode = _episodes(tournament)[0]
    later = resumed.running_since + 10 * MINUTE
    assert clock_value_us(resumed, later) == ten_minutes + 10 * MINUTE_US
    assert clock_value_us(paused, start + 25 * MINUTE) == ten_minutes
    assert episode.opened_clock_us == 0


def test_terminal_status_closes_demand_at_the_frozen_clock(
    make_tournament, admin, clock
):
    tournament = make_tournament()
    _change(tournament, ONGOING, admin)
    start = _clock_of(tournament).activated_at
    clock.now = start + 7 * MINUTE
    _change(tournament, PAUSED, admin)
    frozen = _clock_of(tournament).elapsed_us
    clock.now = start + 90 * MINUTE

    _change(tournament, CANCELLED, admin)

    after = _clock_of(tournament)
    assert after.running_since is None
    assert after.elapsed_us == frozen
    episodes = _episodes(tournament)
    assert len(episodes) == 2
    assert all(e.closed_at is not None for e in episodes)
    assert {e.closed_clock_us for e in episodes} == {frozen}


def test_reopen_resumes_the_clock_and_opens_new_episodes(
    make_tournament, admin, clock
):
    tournament = make_tournament()
    _change(tournament, ONGOING, admin)
    start = _clock_of(tournament).activated_at
    clock.now = start + 5 * MINUTE
    _change(tournament, COMPLETED, admin)
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    closed = _episodes(tournament)
    assert len(closed) == 2 and all(e.closed_at for e in closed)

    clock.now = start + 60 * MINUTE
    _change(tournament, ONGOING, admin, allow_completed_reopen=True)

    reopened = _clock_of(tournament)
    assert reopened.running_since is not None
    assert reopened.running_since >= start + 60 * MINUTE
    assert reopened.elapsed_us == frozen.elapsed_us
    assert reopened.activated_at == start
    episodes = _episodes(tournament)
    assert len(episodes) == 4
    open_ones = [e for e in episodes if e.closed_at is None]
    assert len(open_ones) == 2
    assert {e.opened_clock_us for e in open_ones} == {frozen.elapsed_us}
    assert {e.id for e in open_ones}.isdisjoint({e.id for e in closed})


# -- automatic status writers bypass `change_status` --


@pytest.mark.parametrize(
    ('mode', 'players'),
    # fmt: off
    [
        (SE, 2),  # the deciding match completes the bracket
        (RR, 3),  # the last match completes a plain round robin
    ],
    # fmt: on
)
def test_auto_completion_and_retraction_update_clock(
    make_tournament, admin, clock, mode, players
):
    tournament = make_tournament(mode=mode, players=players)
    _change(tournament, ONGOING, admin)
    start = _clock_of(tournament).activated_at
    played = sorted(_matches(tournament), key=lambda m: str(m.id))
    clock.now = start + 20 * MINUTE

    for match in played:
        _play(match, admin)

    assert _tournament(tournament).tournament_status is COMPLETED
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    assert frozen.activated_at == start
    assert 20 * MINUTE_US <= frozen.elapsed_us < 21 * MINUTE_US

    # The retraction reopens the tournament: the clock runs again.
    clock.now = start + 100 * MINUTE
    result = matches.unconfirm_match(played[-1].id, admin.id, reason='Wrong')
    assert result.is_ok(), result.unwrap_err()

    assert _tournament(tournament).tournament_status is ONGOING
    resumed = _clock_of(tournament)
    assert resumed.elapsed_us == frozen.elapsed_us
    assert resumed.running_since is not None
    assert resumed.running_since >= start + 100 * MINUTE
    assert resumed.activated_at == start


def _lobbies(tournament):
    return sorted(
        repo.get_matches_for_round(tournament.id, 0, bracket=None),
        key=lambda m: m.group_order or 0,
    )


def _lobby_members(match):
    return sorted(
        str(c.participant_id)
        for c in matches.get_contestants_for_match(match.id)
    )


def _play_lobby(match, admin):
    order = _lobby_members(match)
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()


def test_ffa_single_survivor_completion_and_retraction_update_clock(
    make_tournament, admin, clock
):
    tournament = make_tournament(
        players=8,
        game_format=GameFormat.FREE_FOR_ALL,
        point_table=[5, 3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
    )
    _change(tournament, ONGOING, admin)
    start = _clock_of(tournament).activated_at
    first, second = _lobbies(tournament)
    for lobby in (first, second):
        _play_lobby(lobby, admin)
    # Only the first lobby's winner is left to advance.
    for cid in _lobby_members(second):
        participants.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=admin
        ).unwrap()
    assert _tournament(tournament).tournament_status is ONGOING
    clock.now = start + 30 * MINUTE

    result = matches.advance_ffa_round(tournament.id, initiator_id=admin.id)

    assert result.unwrap() == 'completed'
    assert _tournament(tournament).tournament_status is COMPLETED
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    assert frozen.activated_at == start
    assert 30 * MINUTE_US <= frozen.elapsed_us < 31 * MINUTE_US

    clock.now = start + 120 * MINUTE
    matches.unconfirm_match(first.id, admin.id, reason='Wrong order').unwrap()

    assert _tournament(tournament).tournament_status is ONGOING
    resumed = _clock_of(tournament)
    assert resumed.elapsed_us == frozen.elapsed_us
    assert resumed.running_since is not None
    assert resumed.running_since >= start + 120 * MINUTE


# -- history that no release recorded is never invented --


def test_unsupported_started_history_is_not_fabricated(
    make_tournament, admin, clock
):
    tournament = make_tournament(players=2)
    # A tournament that began before the clock existed.
    _force_status(tournament, ONGOING)
    assert _clock_of(tournament) == OperationalClock()

    clock.readings = 0
    with _statements() as reads:
        found = repo.get_tournament(tournament.id)
        repo.get_tournament(tournament.id, fresh=True)
        repo.find_tournament(tournament.id)
        repo.get_tournaments_for_party(PARTY_ID)
        repo.get_matches_for_tournament(tournament.id)
        repo.list_open_due_episodes(tournament.id)
    # Reading is not an activation: no write, no lock, no operation time.
    assert _writes(reads) == []
    assert found.operational_clock_activated_at is None
    assert clock.readings == 0
    assert _clock_of(tournament) == OperationalClock()

    _change(tournament, PAUSED, admin)
    assert _clock_of(tournament) == OperationalClock()
    _change(tournament, ONGOING, admin)
    assert _clock_of(tournament) == OperationalClock()
    for match in _matches(tournament):
        _play(match, admin)
    assert _tournament(tournament).tournament_status is COMPLETED
    assert _clock_of(tournament) == OperationalClock()
    _change(tournament, ONGOING, admin, allow_completed_reopen=True)
    assert _clock_of(tournament) == OperationalClock()
    assert _episodes(tournament) == []


def test_creation_in_a_started_status_has_no_history(party, clock):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Operational lifecycle {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=SE,
        tournament_status=ONGOING,
    )
    tournament, _ = result.unwrap()
    try:
        assert _tournament(tournament).tournament_status is ONGOING
        assert _clock_of(tournament) == OperationalClock()
    finally:
        tournament_service.delete_tournament(tournament.id)


# -- the central status setter --


def _set(tournament, status, *, at=None):
    result = repo.set_tournament_status_flush(
        tournament.id, status, changed_at=at
    )
    assert result.is_ok(), result.unwrap_err() if result.is_err() else None
    return result.unwrap()


def _known_clock(old):
    """Return the clock columns of a tournament that is in `old`."""
    if old is CLOSED:
        return {}

    columns = {'operational_clock_activated_at': START - 60 * MINUTE}
    if old is ONGOING:
        columns['operational_clock_running_since'] = START - 10 * MINUTE
    else:
        columns['operational_clock_elapsed_us'] = 10 * MINUTE_US
    return columns


@pytest.mark.parametrize(
    ('old', 'new', 'running'),
    # fmt: off
    [
        (CLOSED, ONGOING, True),  # the start activates the clock
        (ONGOING, PAUSED, False),  # leaving ONGOING freezes it
        (ONGOING, COMPLETED, False),
        (ONGOING, CANCELLED, False),
        (PAUSED, ONGOING, True),  # a resume continues it
        (COMPLETED, ONGOING, True),
        (PAUSED, CANCELLED, False),  # still frozen
    ],
    # fmt: on
)
def test_status_setter_follows_the_clock_edges(
    make_tournament, clock, old, new, running
):
    tournament = make_tournament(generate=False)
    _force_status(tournament, old, **_known_clock(old))

    edge = _set(tournament, new, at=START)

    db.session.commit()
    after = _clock_of(tournament)
    assert _tournament(tournament).tournament_status is new
    assert after.running_since == (START if running else None)
    assert after.elapsed_us == (0 if old is CLOSED else 10 * MINUTE_US)
    assert after.activated_at == (
        START if old is CLOSED else START - 60 * MINUTE
    )
    assert edge is not None
    assert edge.at == START
    assert edge.clock == after


@pytest.mark.parametrize(
    ('old', 'new'),
    # fmt: off
    [
        (None, ONGOING),
        (TournamentStatus.DRAFT, ONGOING),
        (TournamentStatus.REGISTRATION_OPEN, ONGOING),
        (CANCELLED, ONGOING),
        (CLOSED, TournamentStatus.REGISTRATION_OPEN),
        (ONGOING, PAUSED),
    ],
    # fmt: on
)
def test_status_setter_never_invents_a_clock_history(
    make_tournament, clock, old, new
):
    tournament = make_tournament(generate=False)
    db.session.rollback()
    db.session.execute(
        update(DbTournament)
        .where(DbTournament.id == tournament.id)
        .values(tournament_status=old.name if old else None)
    )
    db.session.commit()

    edge = _set(tournament, new, at=START)

    db.session.commit()
    assert edge is None
    assert _tournament(tournament).tournament_status is new
    assert _clock_of(tournament) == OperationalClock()


def test_status_setter_refuses_an_edge_a_known_clock_cannot_follow(
    make_tournament, clock
):
    tournament = make_tournament(generate=False)
    _force_status(
        tournament,
        CANCELLED,
        operational_clock_activated_at=START - 60 * MINUTE,
        operational_clock_elapsed_us=3 * MINUTE_US,
    )

    result = repo.set_tournament_status_flush(
        tournament.id, ONGOING, changed_at=START
    )

    assert result.is_err()
    db.session.rollback()
    assert _tournament(tournament).tournament_status is CANCELLED
    assert _clock_of(tournament).running_since is None


def test_status_setter_reads_the_row_not_a_cached_view_of_it(
    make_tournament, clock
):
    tournament = make_tournament(generate=False)
    _force_status(
        tournament,
        PAUSED,
        operational_clock_activated_at=START - 60 * MINUTE,
        operational_clock_elapsed_us=5 * MINUTE_US,
    )
    # This session caches the row as paused ...
    cached = db.session.get(DbTournament, tournament.id)
    assert cached.tournament_status == 'PAUSED'
    # ... while another one commits that it runs.
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE lan_tournaments SET tournament_status = 'ONGOING',"
                ' operational_clock_running_since = :since'
                ' WHERE id = :id'
            ),
            {'since': START - 10 * MINUTE, 'id': tournament.id},
        )

    edge = _set(tournament, PAUSED, at=START)

    # The freeze counted the ten minutes it ran, as the stale view
    # (already paused) would have missed.
    assert edge.clock.running_since is None
    assert edge.clock.elapsed_us == 15 * MINUTE_US


def test_status_setter_samples_the_server_when_no_time_is_given(
    make_tournament, clock
):
    tournament = make_tournament(generate=False)
    clock.now = START

    edge = _set(tournament, ONGOING)

    assert edge.at == START
    assert edge.clock.running_since == START
    assert edge.clock.activated_at == START
    assert clock.readings >= 1


def test_status_setter_stores_naive_utc_whatever_the_session_zone(
    make_tournament, clock
):
    tournament = make_tournament(generate=False)
    plus_two = timezone(timedelta(hours=2))
    started_at = datetime(2031, 3, 4, 20, 0, 0, tzinfo=plus_two)
    db.session.rollback()
    db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))

    edge = _set(tournament, ONGOING, at=started_at)

    assert edge.at == START
    assert edge.clock.running_since == START
    db.session.commit()
    assert _clock_of(tournament).activated_at == START


def test_status_setter_leaves_commit_and_rollback_to_the_owner(
    make_tournament, clock
):
    tournament = make_tournament(generate=False)
    _set(tournament, ONGOING, at=START)

    # Nothing is durable until the owner commits, and a rollback takes
    # the status and the clock back together.
    assert _committed_rows(
        'SELECT tournament_status, operational_clock_activated_at'
        ' FROM lan_tournaments WHERE id = :id',
        id=tournament.id,
    ) == [('REGISTRATION_CLOSED', None)]
    db.session.rollback()
    assert _tournament(tournament).tournament_status is CLOSED
    assert _clock_of(tournament) == OperationalClock()
