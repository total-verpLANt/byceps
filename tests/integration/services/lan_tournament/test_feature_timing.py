from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import threading
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, text

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_dashboard_coordination_service as coordination,
    tournament_dashboard_repository as dashboard_repository,
    tournament_dashboard_service as dashboard,
    tournament_match_service as matches,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


# Far from the real time of the Ready claim, so a stamp that took the wall
# clock instead of the operation time cannot equal a reading by accident.
START = datetime(2031, 9, 10, 18, 0, 0)
SCHEDULED = datetime(2031, 9, 10, 15, 0, 0)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
LONG_AGO = datetime(2001, 1, 1)
PAGE_TIME = START + 3 * HOUR

CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING
SE = EliminationMode.SINGLE_ELIMINATION

LOCK_SECONDS = 10

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

REVIEW_WRITERS = (
    'dispute_result',
    'resolve_dispute',
    'set_match_dispute_flush',
    'clear_match_dispute_flush',
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
    return make_party(brand, PartyID(f'feature-timing-{suffix}'), 'Feature')


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'FeatureTiming{suffix}P{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'FeatureTimingAdmin{str(generate_uuid7())[:18]}')


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin(
        {'lan_tournament.administrate'},
        screen_name=f'FeatureTimingViewer{str(generate_uuid7())[:18]}',
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
            f'Feature timing {generate_uuid7()}',
            start_time=SCHEDULED,
            tournament_status=fields.pop('status', CLOSED),
            **fields,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        return tournament

    def _register(tournament, user):
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

    def solo(count=4):
        tournament = _create(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=SE,
            max_players=16,
        )
        for user in players[:count]:
            _register(tournament, user)
        db.session.commit()
        board = seeding.get_board(tournament.id, initiator_id=admin.id)
        generated = seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.unwrap().version,
            initiator_id=admin.id,
        )
        assert generated.is_ok(), generated.unwrap_err()
        return tournament

    def lobbies(count=8):
        tournament = _create(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=SE,
            max_players=16,
            point_table=[5, 3, 2, 1],
            group_size_min=2,
            group_size_max=4,
            advancement_count=2,
        )
        for user in players[:count]:
            _register(tournament, user)
        db.session.commit()
        generated = matches.generate_ffa_round(
            tournament.id, bracket=None, initiator_id=admin.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        return tournament

    yield SimpleNamespace(solo=solo, lobbies=lobbies)
    db.session.rollback()
    for tournament in created:
        if repo.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def start(admin, clock):
    """Start a tournament well after everything its generation sampled."""

    def _start(tournament) -> None:
        clock.now = START + HOUR
        result = tournament_service.change_status(
            tournament.id, ONGOING, admin.id
        )
        assert result.is_ok(), result.unwrap_err()
        clock.now = START + 2 * HOUR

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


def _facts(match_id) -> SimpleNamespace:
    (row,) = _read(
        'SELECT pairing_id, pairing_generation, readiness_revision,'
        ' ready_at_a, ready_by_a, ready_at_b, ready_by_b,'
        ' invitation_hold_a, invitation_hold_b, occupied_since,'
        ' last_changed_at, confirmed_by'
        ' FROM lan_tournament_matches WHERE id = :id',
        id=match_id,
    )
    return SimpleNamespace(**row)


def _episodes(tournament) -> list:
    return [
        dict(row)
        for row in _read(
            'SELECT id, match_id, pairing_key, opened_at, opened_clock_us,'
            ' closed_at, closed_clock_us, ack_revision, xmin::text AS version'
            ' FROM lan_tournament_match_due_episodes WHERE tournament_id = :id'
            ' ORDER BY opened_at, id',
            id=tournament.id,
        )
    ]


def _acks(tournament) -> list:
    return [
        dict(row)
        for row in _read(
            'SELECT id, episode_id, match_id, actor_id, revision, occurred_at,'
            ' clock_us, comment, xmin::text AS version'
            ' FROM lan_tournament_match_escalation_acks WHERE tournament_id = :id'
            ' ORDER BY revision, id',
            id=tournament.id,
        )
    ]


def _clock_columns(tournament) -> dict:
    (row,) = _read(
        'SELECT operational_clock_elapsed_us,'
        ' operational_clock_running_since, operational_clock_activated_at,'
        ' tournament_status, xmin::text AS version'
        ' FROM lan_tournaments WHERE id = :id',
        id=tournament.id,
    )
    return dict(row)


def _occupancy(tournament) -> dict[UUID, datetime | None]:
    return {
        row['id']: row['occupied_since']
        for row in _read(
            'SELECT id, occupied_since FROM lan_tournament_matches'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _log_types(tournament) -> list[str]:
    return [
        row['event_type']
        for row in _read(
            'SELECT event_type FROM lan_tournament_log_entries'
            ' WHERE tournament_id = :id ORDER BY occurred_at, id',
            id=tournament.id,
        )
    ]


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


def _matches(tournament):
    db.session.rollback()
    return sorted(
        repo.get_matches_for_tournament(tournament.id),
        key=lambda m: (m.phase, m.round or 0, m.match_order),
    )


def _first_round(tournament):
    found = _matches(tournament)
    first = min(m.round or 0 for m in found)
    return [m for m in found if (m.round or 0) == first]


def _page(viewer, party, *, state='all', view='all'):
    db.session.rollback()
    return dashboard.get_dashboard_page(
        viewer,
        party.id,
        DashboardQuery(scope='all', view=view, state=state, per_page=50),
        settings=SETTINGS,
        now=PAGE_TIME,
    ).unwrap()


def _row(viewer, party, match):
    return next(r for r in _page(viewer, party).rows if r.match_id == match.id)


def _operation_time(clock, mark: int) -> datetime:
    """Return the operation time: the one reading taken since `mark`.

    A Ready operation has one stamping writer, so a second reading is a
    fact that took a time of its own.
    """
    readings = clock.log[mark:]
    assert len(readings) == 1, readings
    return readings[0]


# -- Ready operations, as the site view drives them --


def _occupant(tournament, match, side):
    """Return the user who holds a side, from the live pairing."""
    db.session.rollback()
    pairing = repo.get_match_pairing(match.id)
    identity = pairing.side_a if side == MatchSide.A else pairing.side_b
    holder = next(
        p
        for p in repo.get_participants_for_tournament(tournament.id)
        if p.id == identity.id
    )
    db.session.rollback()
    return holder.user_id


def _ready(tournament, match, operation, side):
    """Claim or revoke like `_mutate_readiness`: Err rolls back, Ok commits."""
    facts = _facts(match.id)
    function = (
        readiness.claim_ready_flush
        if operation == 'claim'
        else readiness.revoke_ready_flush
    )
    result = function(
        match.id,
        side,
        _occupant(tournament, match, side),
        expected_pairing_generation=facts.pairing_generation,
        expected_readiness_revision=facts.readiness_revision,
    )
    if result.is_err():
        repo.rollback_session()
    else:
        repo.commit_session()
    return result


def _acknowledge(viewer, party, match):
    row = _row(viewer, party, match)
    result = coordination.acknowledge_match(
        viewer,
        party.id,
        match.id,
        expected_episode_id=row.episode_id,
        expected_ack_revision=row.ack_revision,
        comment='checked',
    )
    assert result.is_ok(), result
    return result.unwrap()


def _alert_facts(viewer, party, tournament, match) -> SimpleNamespace:
    """Everything that makes a match's alert, as committed."""
    return SimpleNamespace(
        episodes=_episodes(tournament),
        acks=_acks(tournament),
        clock=_clock_columns(tournament),
        occupancy=_occupancy(tournament),
        row=_row(viewer, party, match),
    )


def _assert_alert_untouched(before, after) -> None:
    assert after.episodes == before.episodes
    assert after.acks == before.acks
    assert after.clock == before.clock
    assert after.occupancy == before.occupancy
    # The row shows the same wait, tier, episode and acknowledgement. The
    # Ready times and the last change are the facts that may differ.
    blank = dict(last_changed_at=None, ready_at_a=None, ready_at_b=None)
    assert replace(after.row, **blank) == replace(before.row, **blank)


# -- a Ready change touches the last change, not the alert --


def test_ready_change_touches_last_change_without_resetting_alert(
    worlds, party, viewer, clock, start
):
    tournament = worlds.solo(4)
    start(tournament)
    first, other = _first_round(tournament)
    _age_stamps(tournament)
    # An orga checked the delay of the first match an hour into its wait:
    # the episode has an acknowledgement, which the Ready claim must keep.
    _acknowledge(viewer, party, first)
    assert len(_acks(tournament)) == 1
    clock.now = START + 2 * HOUR + 30 * MINUTE
    assert set(_stamps(tournament).values()) == {LONG_AGO}
    before = _alert_facts(viewer, party, tournament, first)
    assert before.row.ack_revision == 1
    assert before.row.latest_acknowledgement is not None
    assert before.row.last_changed_at == LONG_AGO
    assert before.row.ready_at_a is None and before.row.ready_at_b is None
    assert len(before.episodes) == 2 and before.row.episode_id is not None
    other_before = _row(viewer, party, other)

    # Claim: the match, and only it, takes the operation time.
    wall_before = datetime.now(UTC).replace(tzinfo=None)
    mark = len(clock.log)
    claimed = _ready(tournament, first, 'claim', MatchSide.A)
    assert claimed.is_ok(), claimed
    wall_after = datetime.now(UTC).replace(tzinfo=None)
    claim_time = _operation_time(clock, mark)
    assert _stamps(tournament) == {
        **{m.id: LONG_AGO for m in _matches(tournament)},
        first.id: claim_time,
    }
    ready = _facts(first.id)
    # The Ready time is the application's wall time; the stamp is the
    # operation time. Two clocks, two facts: neither stands in for the other.
    assert wall_before <= ready.ready_at_a <= wall_after
    assert ready.ready_at_a != claim_time
    assert ready.ready_by_a == _occupant(tournament, first, MatchSide.A)
    after_claim = _alert_facts(viewer, party, tournament, first)
    _assert_alert_untouched(before, after_claim)
    assert after_claim.row.last_changed_at == claim_time
    assert after_claim.row.ready_at_a == ready.ready_at_a
    assert _row(viewer, party, other) == other_before

    # The second side: another change, its own operation time, still no
    # new episode, no new baseline.
    mark = len(clock.log)
    both = _ready(tournament, first, 'claim', MatchSide.B)
    assert both.is_ok(), both
    both_time = _operation_time(clock, mark)
    assert both_time > claim_time
    assert _facts(first.id).last_changed_at == both_time
    after_both = _alert_facts(viewer, party, tournament, first)
    _assert_alert_untouched(before, after_both)
    assert after_both.row.ready_at_b is not None

    # A revocation is a change too. The first claim stays in the past.
    mark = len(clock.log)
    revoked = _ready(tournament, first, 'revoke', MatchSide.A)
    assert revoked.is_ok(), revoked
    revoke_time = _operation_time(clock, mark)
    assert revoke_time > both_time
    assert _facts(first.id).last_changed_at == revoke_time
    after_revoke = _alert_facts(viewer, party, tournament, first)
    _assert_alert_untouched(before, after_revoke)
    assert after_revoke.row.ready_at_a is None
    assert after_revoke.row.ready_at_b is not None
    assert _row(viewer, party, other) == other_before
    # The clock of the tournament never moved: no pause, no resume, and
    # the running tournament keeps counting from its start.
    assert before.clock['operational_clock_running_since'] == START + HOUR


# fmt: off
REFUSED = [
    pytest.param('stale-revision', id='stale-revision'),
    pytest.param('stale-generation', id='stale-generation'),
    pytest.param('twice', id='claimed-twice'),
    pytest.param('audit-fails', id='audit-failure-rolls-the-stamp-back'),
]
# fmt: on


@pytest.mark.parametrize('case', REFUSED)
def test_a_refused_or_failed_ready_operation_stamps_nothing(
    worlds, party, viewer, clock, start, monkeypatch, case
):
    tournament = worlds.solo(2)
    start(tournament)
    (match,) = _first_round(tournament)
    _age_stamps(tournament)
    side = MatchSide.A
    user = _occupant(tournament, match, side)
    if case == 'twice':
        assert _ready(tournament, match, 'claim', side).is_ok()
        _age_stamps(tournament)
    before = _facts(match.id)
    episodes = _episodes(tournament)
    if case == 'audit-fails':

        def failing(*args, **kwargs):
            raise RuntimeError('audit store is down')

        monkeypatch.setattr(readiness, 'create_log_entry', failing)
    generation = before.pairing_generation + (case == 'stale-generation')
    revision = before.readiness_revision + (case == 'stale-revision')

    mark = len(clock.log)
    result = readiness.claim_ready_flush(
        match.id,
        side,
        user,
        expected_pairing_generation=generation,
        expected_readiness_revision=revision,
    )
    assert result.is_err()
    repo.rollback_session()

    after = _facts(match.id)
    assert after == before
    assert after.last_changed_at == LONG_AGO
    assert _episodes(tournament) == episodes
    if case in ('stale-revision', 'stale-generation', 'twice'):
        # A refusal comes before any write: the server clock is not read.
        assert clock.log[mark:] == []
    else:
        # The stamp was staged; the owner's rollback took it away again.
        assert len(clock.log[mark:]) == 1
        assert result.unwrap_err() == 'readiness_audit_failed'


# -- the Ready writers lock the tournament, then the match --


def _lock_kind(statement: str) -> str | None:
    if 'FOR UPDATE' not in statement:
        return None
    if 'FROM lan_tournaments' in statement:
        return 'tournament'
    if 'FROM lan_tournament_matches' in statement:
        return 'match'
    return None


def _wait_until_blocked_by(pid: int) -> int:
    """Return the backend that waits for a lock held by `pid`."""
    deadline = time.monotonic() + LOCK_SECONDS
    while time.monotonic() < deadline:
        rows = _read(
            'SELECT pid FROM pg_stat_activity'
            ' WHERE :holder = ANY(pg_blocking_pids(pid))',
            holder=pid,
        )
        if rows:
            return rows[0]['pid']
        time.sleep(0.02)
    pytest.fail(f'nobody waits for the lock held by backend {pid}')


def test_ready_writer_takes_tournament_first(admin_app, worlds, clock, start):
    tournament = worlds.solo(2)
    start(tournament)
    (match,) = _first_round(tournament)
    side = MatchSide.A
    user = _occupant(tournament, match, side)

    # The statements of a real claim and a real revoke: the first row lock
    # is the tournament's, the match's follows, and nothing is written
    # before both are held.
    for operation in ('claim', 'revoke'):
        with _statements() as seen:
            result = _ready(tournament, match, operation, side)
        assert result.is_ok(), result
        kinds = [_lock_kind(s) for s in seen]
        locks = [kind for kind in kinds if kind is not None]
        assert locks[:2] == ['tournament', 'match'], locks
        # Every match lock of the operation follows a tournament lock.
        assert locks == ['tournament', 'match'] * (len(locks) // 2), locks
        first_match_lock = kinds.index('match')
        writes = [
            i
            for i, s in enumerate(seen)
            if s.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))
        ]
        assert writes and min(writes) > first_match_lock

    # Under contention: another transaction holds the tournament. The
    # claim waits for it, and by then holds no lock on the match.
    facts = _facts(match.id)
    db.session.rollback()
    holder = db.engine.connect()
    held = holder.begin()
    outcome: dict = {}
    thread = None
    try:
        holder.execute(
            text('SELECT id FROM lan_tournaments WHERE id = :id FOR UPDATE'),
            {'id': tournament.id},
        )
        holder_pid = holder.scalar(text('SELECT pg_backend_pid()'))

        def claim():
            with admin_app.app_context():
                try:
                    db.session.execute(
                        text(f"SET LOCAL lock_timeout = '{LOCK_SECONDS}s'")
                    )
                    result = readiness.claim_ready_flush(
                        match.id,
                        side,
                        user,
                        expected_pairing_generation=facts.pairing_generation,
                        expected_readiness_revision=facts.readiness_revision,
                    )
                    if result.is_ok():
                        repo.commit_session()
                    else:
                        repo.rollback_session()
                    outcome['result'] = result
                except BaseException as error:  # reported by the test
                    outcome['error'] = error
                finally:
                    db.session.rollback()
                    db.session.remove()

        thread = threading.Thread(target=claim, daemon=True)
        thread.start()
        waiter = _wait_until_blocked_by(holder_pid)
        assert waiter != holder_pid
        # A match first would hold the row now and fail this at once.
        with db.engine.connect() as probe:
            with probe.begin():
                probe.execute(
                    text(
                        'SELECT id FROM lan_tournament_matches'
                        ' WHERE id = :id FOR UPDATE NOWAIT'
                    ),
                    {'id': match.id},
                )
        assert _facts(match.id) == facts
    finally:
        held.rollback()
        holder.close()
        if thread is not None:
            thread.join(LOCK_SECONDS + 5)
    assert thread is not None and not thread.is_alive()
    assert 'error' not in outcome, outcome.get('error')
    assert outcome['result'].is_ok(), outcome['result']
    assert _facts(match.id).ready_by_a == user


# -- review: no adapter is installed, and none is faked --


def test_installed_review_changes_are_atomic_and_independent_of_ack(
    worlds, party, viewer, clock, start
):
    """The absent-adapter contract, against the real schema and a real ack.

    No dispute or review feature exists in this tree. The day an adapter
    is installed this test fails: its writers must then be integrated
    (flush only, one operation time, one commit owner, never through the
    acknowledgement) and this contract replaced.
    """
    for module in (
        matches,
        repo,
        readiness,
        coordination,
        dashboard,
        dashboard_repository,
    ):
        installed = [n for n in REVIEW_WRITERS if hasattr(module, n)]
        assert installed == [], (module.__name__, installed)

    # No stored review fact: not in the ORM, not in the live schema.
    assert (
        _read(
            'SELECT table_name, column_name FROM information_schema.columns'
            ' WHERE table_schema = current_schema()'
            " AND table_name LIKE 'lan_tournament%'"
            " AND (column_name ILIKE '%dispute%'"
            " OR column_name ILIKE '%review%')"
        )
        == []
    )
    assert (
        _read(
            'SELECT table_name FROM information_schema.tables'
            ' WHERE table_schema = current_schema()'
            " AND (table_name ILIKE '%dispute%'"
            " OR table_name ILIKE '%review%')"
        )
        == []
    )

    tournament = worlds.solo(4)
    start(tournament)
    first, _ = _first_round(tournament)
    _age_stamps(tournament)
    assert _ready(tournament, first, 'claim', MatchSide.A).is_ok()
    ready_before = _facts(first.id)
    stamps_before = _stamps(tournament)
    log_before = _log_types(tournament)

    def review_flags():
        page = _page(viewer, party)
        assert page.rows
        return {
            (r.match_id, r.review_available, r.review_open) for r in page.rows
        }

    flags = review_flags()
    assert {(a, b) for _, a, b in flags} == {(False, False)}
    assert _page(viewer, party, state='review-open').rows == ()

    # An orga acknowledges the escalation of the match.
    _acknowledge(viewer, party, first)

    # The acknowledgement is its own fact: it moved no Ready fact, no
    # last change, no review flag, and the review state selects nothing.
    assert _facts(first.id) == ready_before
    assert _stamps(tournament) == stamps_before
    assert review_flags() == flags
    assert _page(viewer, party, state='review-open').rows == ()
    new_entries = Counter(_log_types(tournament)) - Counter(log_before)
    assert new_entries == Counter({'match-acknowledged': 1})
    assert not [
        t for t in _log_types(tournament) if 'dispute' in t or 'review' in t
    ]
    # The reverse holds too: a Ready change leaves the acknowledgement.
    acks = _acks(tournament)
    assert len(acks) == 1
    assert _ready(tournament, first, 'revoke', MatchSide.A).is_ok()
    assert _acks(tournament) == acks
    assert review_flags() == flags


# -- FFA has no Ready sides, and no review is invented --


def test_absent_review_and_ffa_ready_are_truthfully_unavailable(
    worlds, party, viewer, players, clock, start
):
    contested = worlds.lobbies(8)
    control = worlds.solo(2)
    start(contested)
    start(control)
    lobbies = _matches(contested)
    assert len(lobbies) == 2
    (duel,) = _first_round(control)
    _age_stamps(contested, control)

    # The page offers no Ready side for a lobby, and no review at all.
    page = _page(viewer, party)
    by_match = {r.match_id: r for r in page.rows}
    for lobby in lobbies:
        row = by_match[lobby.id]
        assert row.readiness_available is False
        assert row.ready_at_a is None and row.ready_at_b is None
        assert row.review_available is False and row.review_open is False
    assert by_match[duel.id].readiness_available is True
    unavailable = {
        r.match_id for r in _page(viewer, party, state='ready-unavailable').rows
    }
    assert {m.id for m in lobbies} <= unavailable
    assert duel.id not in unavailable
    for state in ('ready-none', 'ready-one', 'ready-both'):
        listed = {r.match_id for r in _page(viewer, party, state=state).rows}
        assert not listed & {m.id for m in lobbies}, state
    assert duel.id in {
        r.match_id for r in _page(viewer, party, state='ready-none').rows
    }
    assert _page(viewer, party, state='review-open').rows == ()

    # Nobody can claim or revoke a side of a lobby, and the attempt leaves
    # no trace: no claim, hold, revision, pairing, invitation, audit entry
    # or stamp.
    lobby = lobbies[0]
    facts = _facts(lobby.id)
    assert facts.pairing_id is None and facts.readiness_revision == 0
    entries = _log_types(contested)
    episodes = _episodes(contested)
    mark = len(clock.log)
    for operation in (
        readiness.claim_ready_flush,
        readiness.revoke_ready_flush,
    ):
        for side in (MatchSide.A, MatchSide.B):
            result = operation(
                lobby.id,
                side,
                players[0].id,
                expected_pairing_generation=0,
                expected_readiness_revision=0,
            )
            assert result.unwrap_err() == 'readiness_format_unsupported'
            repo.rollback_session()
    assert clock.log[mark:] == []
    after = _facts(lobby.id)
    assert after == facts
    assert after.ready_at_a is None and after.ready_at_b is None
    assert after.ready_by_a is None and after.ready_by_b is None
    assert after.invitation_hold_a is False and after.invitation_hold_b is False
    assert after.last_changed_at == LONG_AGO
    assert _log_types(contested) == entries
    assert not [t for t in _log_types(contested) if t.startswith('match-ready')]
    assert _episodes(contested) == episodes
    assert not _read(
        'SELECT 1 FROM lan_tournament_match_pairings WHERE match_id = :id',
        id=lobby.id,
    )
    assert not _read(
        'SELECT 1 FROM lan_tournament_match_invitations WHERE match_id = :id',
        id=lobby.id,
    )
    assert set(_stamps(contested).values()) == {LONG_AGO}
