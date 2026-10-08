"""
tests.integration.services.lan_tournament.test_dashboard_concurrency
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Real transactions race for the tournament lock: pins, acknowledgements,
episode replacement, pause against confirmation, orga revocation.

Every racer is a thread with an application context and a session of
its own. One transaction is parked right before its commit, and the
others are shown waiting for it in `pg_locks` before it is let go.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    signals,
    tournament_dashboard_coordination_service as service,
    tournament_dashboard_service as dashboard_service,
    tournament_match_service as matches,
    tournament_orga_service as orgas,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
    DashboardRow,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7, uuid7


# Far from the real time of every fixture, so that a fact which wrongly
# took the real clock cannot equal a fake one.
START = datetime(2031, 3, 4, 18, 0, 0)
SCHEDULED = datetime(2031, 3, 4, 15, 0, 0)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)
MICROSECOND = timedelta(microseconds=1)

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED

# Server-side and client-side bounds of the races, in seconds.
LOCK_TIMEOUT = '8s'
STATEMENT_TIMEOUT = '12s'
PARK_SECONDS = 15
BLOCK_SECONDS = 10
JOIN_SECONDS = 15


@pytest.fixture(scope='module', autouse=True)
def _database_identity(admin_app, database_config):
    """Fail closed unless the races run on a database made for tests."""
    assert database_config.database.startswith('byceps_test'), (
        f'refusing to race on {database_config.database!r}'
    )
    with db.engine.connect() as connection:
        assert (
            connection.scalar(text('SELECT current_database()'))
            == database_config.database
        )


@pytest.fixture(scope='module')
def users(make_user):
    suffix = uuid4().hex[:6]
    return [make_user(f'F03Race{i}{suffix}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    return make_admin(
        {'lan_tournament.administrate'},
        screen_name=f'F03RaceAdmin{uuid4().hex[:6]}',
    )


@pytest.fixture(autouse=True)
def _context(admin_app):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


def _viewer(user, *claimed: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(claimed))


class _Clock:
    """Stand in for the server operation time, one reading per call.

    Several threads read it, so every reading takes the lock.
    """

    def __init__(self, now: datetime) -> None:
        self._lock = threading.Lock()
        self.now = now
        self.step = timedelta(0)
        self.readings = 0

    def __call__(self) -> datetime:
        with self._lock:
            self.readings += 1
            value = self.now
            self.now += self.step
            return value

    def to(self, now: datetime, step: timedelta = timedelta(0)) -> None:
        """Make `now` the next reading, and let each reading add `step`."""
        with self._lock:
            self.now = now
            self.step = step


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(START)
    clock.step = SECOND
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


# -- reads: only what is committed, through a connection of their own --


def _read(sql: str, **params) -> list[dict]:
    with db.engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _tournament_row(tournament_id) -> dict:
    (row,) = _read(
        'SELECT tournament_status, operational_clock_elapsed_us,'
        ' operational_clock_running_since, operational_clock_activated_at'
        ' FROM lan_tournaments WHERE id = :id',
        id=tournament_id,
    )
    return row


def _episodes(match_id) -> list[dict]:
    return _read(
        'SELECT * FROM lan_tournament_match_due_episodes'
        ' WHERE match_id = :id ORDER BY opened_at, id',
        id=match_id,
    )


def _open_episode(match_id) -> dict | None:
    open_ones = [e for e in _episodes(match_id) if e['closed_at'] is None]
    assert len(open_ones) <= 1
    return open_ones[0] if open_ones else None


def _acks(match_id) -> list[dict]:
    return _read(
        'SELECT * FROM lan_tournament_match_escalation_acks'
        ' WHERE match_id = :id ORDER BY revision, id',
        id=match_id,
    )


def _pins(match_id) -> list[dict]:
    return _read(
        'SELECT * FROM lan_tournament_match_dashboard_annotations'
        ' WHERE match_id = :id',
        id=match_id,
    )


def _audit(tournament_id, event_type: str) -> list[tuple]:
    return [
        (row['initiator_id'], row['data'])
        for row in _read(
            'SELECT initiator_id, data FROM lan_tournament_log_entries'
            ' WHERE tournament_id = :t AND event_type = :e'
            ' ORDER BY occurred_at, id',
            t=tournament_id,
            e=event_type,
        )
    ]


def _assignments(tournament_id, user) -> int:
    (row,) = _read(
        'SELECT count(*) AS n FROM lan_tournament_orgas'
        ' WHERE tournament_id = :t AND user_id = :u',
        t=tournament_id,
        u=user.id,
    )
    return row['n']


def _microseconds(delta: timedelta) -> int:
    return delta // MICROSECOND


def _assert_tournament_unlocked(tournament_id) -> None:
    """Fail if another transaction still holds the tournament row."""
    with db.engine.connect() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '2s'"))
        rows = connection.execute(
            text(
                'SELECT id FROM lan_tournaments WHERE id = :id'
                ' FOR UPDATE NOWAIT'
            ),
            {'id': tournament_id},
        ).all()
        assert len(rows) == 1
        connection.rollback()


# -- the world: a running tournament of four players and two orgas --


class World:
    """A running single-elimination tournament of four, with two orgas."""

    def __init__(self, party_id, tournament_id, players, ada, bob, admin):
        self.party_id: PartyID = party_id
        self.tournament_id: TournamentID = tournament_id
        self.players = players
        self.ada = ada
        self.bob = bob
        self.admin = admin
        self.started_at: datetime = _tournament_row(tournament_id)[
            'operational_clock_activated_at'
        ]

    def match(self, *, round=0, order=0, bracket=None) -> TournamentMatch:
        db.session.rollback()
        found = [
            m
            for m in repo.get_matches_for_tournament(self.tournament_id)
            if m.bracket == bracket
            and m.round == round
            and m.match_order == order
        ]
        assert len(found) == 1, (bracket, round, order, found)
        return found[0]

    def members(self, match) -> list[str]:
        db.session.rollback()
        return sorted(
            str(c.participant_id or c.team_id)
            for c in repo.get_contestants_for_match(match.id)
        )

    def scores(self, match, winner: str | None = None, margin: int = 2):
        members = self.members(match)
        winner = winner or members[0]
        loser = next(m for m in members if m != winner)
        return {UUID(winner): margin, UUID(loser): 0}

    def play(self, match) -> None:
        """Let the lower ID win 2:0."""
        result = matches.admin_set_and_confirm_match(
            match.id, self.admin.id, self.scores(match)
        )
        assert result.is_ok(), result.unwrap_err()

    def row(self, viewer, match, *, when: datetime) -> DashboardRow:
        """Read the row of the match as the browser of `viewer` shows it."""
        page = dashboard_service.get_dashboard_page(
            _viewer(viewer),
            self.party_id,
            DashboardQuery(scope='all', view='all', per_page=50),
            settings=SETTINGS,
            now=when,
        ).unwrap()
        (row,) = [r for r in page.rows if r.match_id == match.id]
        return row

    def pinning(
        self, viewer, match, *, pinned: bool, expected: int
    ) -> Callable[[], Any]:
        def pin():
            return service.set_match_pin(
                _viewer(viewer),
                self.party_id,
                match.id,
                pinned=pinned,
                expected_revision=expected,
            )

        return pin

    def acknowledging(
        self, viewer, match, form: DashboardRow, *, comment='checked'
    ) -> Callable[[], Any]:
        """Acknowledge with the episode and revision the form showed."""

        def acknowledge():
            return service.acknowledge_match(
                _viewer(viewer),
                self.party_id,
                match.id,
                expected_episode_id=form.episode_id,
                expected_ack_revision=form.ack_revision,
                comment=comment,
            )

        return acknowledge

    def annotating(self, kind: str, viewer, match, form: DashboardRow):
        """Pin or acknowledge, as the form was built, for `viewer`."""
        if kind == 'pin':
            return self.pinning(
                viewer, match, pinned=True, expected=form.pin_revision
            )

        return self.acknowledging(viewer, match, form)

    def annotations_of(self, kind: str, match) -> list[dict]:
        return _pins(match.id) if kind == 'pin' else _acks(match.id)

    def annotation_audit(self, kind: str) -> list[tuple]:
        return _audit(
            self.tournament_id,
            (
                service.MATCH_PINNED_EVENT
                if kind == 'pin'
                else service.MATCH_ACKNOWLEDGED_EVENT
            ),
        )


@pytest.fixture
def make_world(make_party, brand, users, admin, clock):
    def _make() -> World:
        party_id = PartyID(f'f03r-{uuid4().hex[:12]}')
        make_party(brand, party_id, f'F03 races {party_id}')
        result = tournament_service.create_tournament(
            party_id,
            f'Races {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=CLOSED,
            start_time=SCHEDULED,
            max_players=16,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        players = users[:4]
        for user in players:
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
        ada, bob = users[4], users[5]
        for orga in (ada, bob):
            db.session.add(
                DbTournamentOrga(
                    TournamentOrgaID(uuid7()), tournament.id, orga.id, SCHEDULED
                )
            )
        db.session.commit()
        board = seeding.get_board(tournament.id, initiator_id=admin.id).unwrap()
        seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.version,
            initiator_id=admin.id,
        ).unwrap()

        # Past every time the generation sampled.
        clock.to(START + HOUR)
        started = tournament_service.change_status(
            tournament.id, ONGOING, admin.id
        )
        assert started.is_ok(), started.unwrap_err()
        clock.to(START + 2 * HOUR, SECOND)
        return World(party_id, tournament.id, players, ada, bob, admin)

    return _make


@pytest.fixture
def world(make_world) -> World:
    return make_world()


# -- the harness: threads, a gate before the commit, and lock evidence --


class Gate:
    """Parks one thread right before its commit until the test says go."""

    def __init__(self, only: str) -> None:
        self.only = only
        self.parked = threading.Event()
        self.release = threading.Event()
        self.owner: threading.Thread | None = None
        self._claim = threading.Lock()

    def reach(self) -> None:
        thread = threading.current_thread()
        if not thread.name.startswith(self.only):
            return

        with self._claim:
            if self.owner is not None:
                return
            self.owner = thread

        self.parked.set()
        if not self.release.wait(PARK_SECONDS):
            raise AssertionError('the parked transaction was never released')

    def wait_parked(self, workers: list['Worker']) -> None:
        deadline = time.monotonic() + PARK_SECONDS
        while time.monotonic() < deadline:
            if self.parked.wait(0.05):
                return
            if all(worker.finished.is_set() for worker in workers):
                break

        raise AssertionError(
            'no transaction reached its commit: '
            + ', '.join(f'{w.name}: {w.error!r}' for w in workers)
        )

    def open(self) -> None:
        self.release.set()


class Worker:
    """Runs one call in a thread with its own context and session."""

    def __init__(self, app, name: str, call: Callable[[], Any]) -> None:
        self.app = app
        self.name = name
        self.call = call
        self.pid: int | None = None
        self.statements: list[str] = []
        self.commits: list[float] = []
        self.rollbacks = 0
        self.outcome: Any = None
        self.error: BaseException | None = None
        self.collected = False
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f'race-{name}', daemon=True
        )

    def _on_begin(self, session, transaction, connection) -> None:
        connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        )
        self.pid = connection.scalar(text('SELECT pg_backend_pid()'))
        if not event.contains(
            connection, 'before_cursor_execute', self._on_statement
        ):
            event.listen(
                connection, 'before_cursor_execute', self._on_statement
            )

    def _on_statement(self, connection, cursor, statement, *args) -> None:
        self.statements.append(statement)

    def _on_commit(self, session) -> None:
        self.commits.append(time.monotonic())

    def _on_rollback(self, session) -> None:
        self.rollbacks += 1

    def _run(self) -> None:
        try:
            with self.app.app_context():
                session = db.session()
                event.listen(session, 'after_begin', self._on_begin)
                event.listen(session, 'after_commit', self._on_commit)
                event.listen(session, 'after_rollback', self._on_rollback)
                try:
                    self.outcome = self.call()
                finally:
                    db.session.rollback()
                    db.session.remove()
        except BaseException as error:  # reported by the test, never lost
            self.error = error
        finally:
            self.finished.set()

    def result(self) -> Any:
        self.thread.join(JOIN_SECONDS)
        assert not self.thread.is_alive(), f'{self.name} did not finish'
        self.collected = True
        if self.error is not None:
            raise self.error
        return self.outcome


@dataclass(frozen=True)
class Waiting:
    """What PostgreSQL says about a backend that waits for a lock."""

    blockers: list[int]
    wait_event_type: str | None
    query: str
    ungranted: list[dict]
    holder_xids: set[str]


@contextmanager
def _observer():
    """Yield a connection whose every poll sees the live lock state."""
    with db.engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as connection:
        yield connection


def _wait_until_blocked(connection, waiter: Worker, holder: Worker) -> Waiting:
    """Wait until PostgreSQL reports the waiter as blocked by the holder."""
    deadline = time.monotonic() + BLOCK_SECONDS
    while time.monotonic() < deadline:
        if waiter.finished.is_set():
            break
        if waiter.pid is not None and holder.pid is not None:
            blockers = connection.scalar(
                text('SELECT pg_blocking_pids(:pid)'), {'pid': waiter.pid}
            )
            if holder.pid in (blockers or []):
                return _waiting(connection, waiter, holder, blockers)
        time.sleep(0.05)

    raise AssertionError(
        f'{waiter.name} was not blocked by {holder.name}'
        f' (finished: {waiter.finished.is_set()}, error: {waiter.error!r})'
    )


def _waiting(connection, waiter, holder, blockers) -> Waiting:
    activity = connection.execute(
        text(
            'SELECT wait_event_type, query FROM pg_stat_activity'
            ' WHERE pid = :pid'
        ),
        {'pid': waiter.pid},
    ).one()
    ungranted = (
        connection.execute(
            text(
                'SELECT locktype, mode, transactionid::text AS xid,'
                ' relation::regclass::text AS relation'
                ' FROM pg_locks WHERE pid = :pid AND NOT granted'
            ),
            {'pid': waiter.pid},
        )
        .mappings()
        .all()
    )
    held = connection.execute(
        text(
            'SELECT transactionid::text FROM pg_locks'
            " WHERE pid = :pid AND locktype = 'transactionid' AND granted"
        ),
        {'pid': holder.pid},
    ).scalars()
    return Waiting(
        blockers=list(blockers),
        wait_event_type=activity.wait_event_type,
        query=activity.query,
        ungranted=[dict(row) for row in ungranted],
        holder_xids=set(held),
    )


def _assert_waits_for_the_tournament(
    connection, waiter: Worker, holder: Worker
) -> Waiting:
    """Prove the waiter is queued on the holder's tournament row lock.

    PostgreSQL names the holder as the blocker, the waiter's statement
    is the `FOR UPDATE` of the tournament row, and the lock it waits
    for is a transaction ID the holder holds.
    """
    waiting = _wait_until_blocked(connection, waiter, holder)

    assert waiting.wait_event_type == 'Lock'
    assert 'FROM lan_tournaments' in waiting.query
    assert 'FOR UPDATE' in waiting.query
    assert 'lan_tournament_matches' not in waiting.query
    waited_for = {
        lock['xid']
        for lock in waiting.ungranted
        if lock['locktype'] == 'transactionid'
    }
    assert waited_for, waiting.ungranted
    assert waited_for <= waiting.holder_xids
    return waiting


@dataclass(frozen=True)
class Contest:
    """The outcome of calls that raced for one tournament lock."""

    holder: Worker
    waiters: list[Worker]
    outcomes: dict[str, Any]

    @property
    def winner_outcome(self) -> Any:
        return self.outcomes[self.holder.name]

    @property
    def loser_outcomes(self) -> list[Any]:
        return [self.outcomes[w.name] for w in self.waiters]


class Race:
    """The workers and gates of one test, cleaned up whatever happens."""

    def __init__(self, app, monkeypatch) -> None:
        self._app = app
        self._monkeypatch = monkeypatch
        self._gate: Gate | None = None
        self._gates: list[Gate] = []
        self._workers: list[Worker] = []
        self._cleanups: list[Callable[[], None]] = []
        self._rollback_calls: list[str] = []
        self._lock = threading.Lock()
        real_commit, real_rollback = repo.commit_session, repo.rollback_session

        def commit_session() -> None:
            gate = self._gate
            if gate is not None:
                gate.reach()
            real_commit()

        def rollback_session() -> None:
            with self._lock:
                self._rollback_calls.append(threading.current_thread().name)
            real_rollback()

        monkeypatch.setattr(repo, 'commit_session', commit_session)
        monkeypatch.setattr(repo, 'rollback_session', rollback_session)

    def start(self, name: str, call: Callable[[], Any]) -> Worker:
        worker = Worker(self._app, name, call)
        self._workers.append(worker)
        worker.thread.start()
        return worker

    def park(self, only: str = 'race-') -> Gate:
        """Park the first matching thread right before its commit."""
        gate = Gate(only)
        self._gates.append(gate)
        self._gate = gate
        return gate

    def park_before(self, module, name: str, *, only: str) -> Gate:
        """Park a thread of `only` as it calls `module.name`."""
        gate = Gate(only)
        self._gates.append(gate)
        real = getattr(module, name)

        def parked(*args, **kwargs):
            gate.reach()
            return real(*args, **kwargs)

        self._monkeypatch.setattr(module, name, parked)
        return gate

    def park_at_audit(self, module, *, only: str) -> Gate:
        """Park a thread of `module` once its audit entry is staged."""
        gate = Gate(only)
        self._gates.append(gate)
        real = module.tournament_log_service.create_log_entry

        def create_log_entry(*args, **kwargs):
            entry = real(*args, **kwargs)
            gate.reach()
            return entry

        # Patch the name in the module under test, so that only its audit
        # entries park.
        self._monkeypatch.setattr(
            module,
            'tournament_log_service',
            SimpleNamespace(create_log_entry=create_log_entry),
        )
        return gate

    def contend(
        self,
        calls: dict[str, Callable[[], Any]],
        *,
        during: Callable[[Worker, list[Worker]], None] | None = None,
    ) -> Contest:
        """Run the calls at once; the first to reach its commit parks.

        Every other call must be shown waiting for the parked one's
        tournament lock. `during` looks at the database while that holds.
        """
        gate = self.park()
        workers = [self.start(name, call) for name, call in calls.items()]
        try:
            gate.wait_parked(workers)
            holder = next(w for w in workers if w.thread is gate.owner)
            waiters = [w for w in workers if w is not holder]
            with _observer() as observer:
                for waiter in waiters:
                    _assert_waits_for_the_tournament(observer, waiter, holder)
                if during is not None:
                    during(holder, waiters)
        finally:
            gate.open()

        return Contest(holder, waiters, {w.name: w.result() for w in workers})

    def rollbacks_of(self, worker: Worker) -> int:
        """Count the `rollback_session` calls the worker made."""
        with self._lock:
            return self._rollback_calls.count(worker.thread.name)

    def watch_signals(self) -> Mock:
        """Connect one receiver to every signal of the module."""
        heard = Mock()
        connected = [
            signal
            for signal in vars(signals).values()
            if hasattr(signal, 'connect') and hasattr(signal, 'send')
        ]
        assert connected, 'no signal to listen to'
        for signal in connected:
            signal.connect(heard, weak=False)
            self._cleanups.append(partial(signal.disconnect, heard))
        return heard

    def listen(self, signal, probe: Callable[[], Any]) -> list[tuple]:
        """Record, at every dispatch, the committed state `probe` reads."""
        heard: list[tuple] = []

        def receiver(sender, **kwargs) -> None:
            heard.append((threading.current_thread().name, probe()))

        signal.connect(receiver, weak=False)
        self._cleanups.append(partial(signal.disconnect, receiver))
        return heard

    def close(self) -> None:
        for gate in self._gates:
            gate.open()
        for cleanup in reversed(self._cleanups):
            cleanup()

        stuck = []
        for worker in self._workers:
            worker.thread.join(JOIN_SECONDS)
            if worker.thread.is_alive():
                stuck.append(worker)
        if stuck:
            with _observer() as observer:
                for worker in stuck:
                    if worker.pid is not None:
                        observer.execute(
                            text('SELECT pg_terminate_backend(:pid)'),
                            {'pid': worker.pid},
                        )
            for worker in stuck:
                worker.thread.join(5)

        unreported = [
            worker.name
            for worker in self._workers
            if worker.error is not None and not worker.collected
        ]
        assert not stuck, f'workers that never finished: {stuck}'
        assert not unreported, f'errors nobody asked for: {unreported}'


@pytest.fixture
def race(admin_app, monkeypatch):
    race = Race(admin_app, monkeypatch)
    try:
        yield race
    finally:
        race.close()


def _announced(heard: Mock) -> list[str]:
    """Name the events the watched signals carried, in order."""
    return [
        type(call.kwargs['event']).__name__ for call in heard.call_args_list
    ]


def _assert_committed_and_rolled_back(race: Race, contest: Contest) -> None:
    """The holder committed once; every other call rolled back, no commit."""
    assert len(contest.holder.commits) == 1
    for waiter in contest.waiters:
        assert waiter.commits == [], waiter.name
        assert race.rollbacks_of(waiter) >= 1, waiter.name
        assert waiter.rollbacks >= 1, waiter.name


# -- duplicate requests: one committed row --


def test_duplicate_ack_has_one_committed_row(world, race, clock):
    ada, bob = world.ada, world.bob
    match = world.match()
    clock.to(world.started_at + 20 * MINUTE)
    form = world.row(ada, match, when=clock.now)
    assert form.ack_unavailable_reason is None
    assert (form.ack_revision, _acks(match.id)) == (0, [])
    heard = race.watch_signals()

    def nothing_is_committed(holder, waiters):
        # The winner has flushed its rows and holds the lock; nobody else
        # can see them yet.
        assert _acks(match.id) == []
        assert _open_episode(match.id)['ack_revision'] == 0
        assert world.annotation_audit('acknowledgement') == []

    contest = race.contend(
        {
            'ack-first': world.acknowledging(ada, match, form),
            'ack-again': world.acknowledging(ada, match, form),
        },
        during=nothing_is_committed,
    )

    # Both built the request from the same page. The winner's commit made
    # the loser's form stale, and the loser reads that under the lock.
    acknowledged = contest.winner_outcome.unwrap()
    (refused,) = contest.loser_outcomes
    assert refused.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    (stored,) = _acks(match.id)
    assert (stored['id'], stored['revision'], stored['actor_id']) == (
        acknowledged.id,
        1,
        ada.id,
    )
    assert _open_episode(match.id)['ack_revision'] == 1
    assert len(world.annotation_audit('acknowledgement')) == 1
    _assert_committed_and_rolled_back(race, contest)
    _assert_tournament_unlocked(world.tournament_id)

    # A third copy of the same page, from the other orga, finds the same.
    late = world.acknowledging(bob, match, form)()
    assert late.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert len(_acks(match.id)) == 1
    heard.assert_not_called()


def test_pin_cas_serializes_shared_state(world, race):
    ada, bob = world.ada, world.bob
    match = world.match()
    assert _pins(match.id) == []
    heard = race.watch_signals()

    def nothing_is_committed(holder, waiters):
        assert _pins(match.id) == []
        assert world.annotation_audit('pin') == []

    # Round 1: two orgas pin from the same page, which shows revision 0.
    first = race.contend(
        {
            'pin-ada': world.pinning(ada, match, pinned=True, expected=0),
            'pin-bob': world.pinning(bob, match, pinned=True, expected=0),
        },
        during=nothing_is_committed,
    )

    pinned = first.winner_outcome.unwrap()
    (refused,) = first.loser_outcomes
    assert refused.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    winner = ada if first.holder.name == 'pin-ada' else bob
    (row,) = _pins(match.id)
    assert (row['revision'], row['pinned_by'], row['updated_by']) == (
        1,
        winner.id,
        winner.id,
    )
    assert pinned.revision == 1
    assert world.annotation_audit('pin') == [
        (winner.id, {'match_id': str(match.id), 'revision': 1})
    ]
    _assert_committed_and_rolled_back(race, first)

    # Round 2: both unpin from the revision they just saw. The compare and
    # set moves it by exactly one, whoever wins.
    def one_row_still(holder, waiters):
        assert [p['revision'] for p in _pins(match.id)] == [1]

    second = race.contend(
        {
            'unpin-ada': world.pinning(ada, match, pinned=False, expected=1),
            'unpin-bob': world.pinning(bob, match, pinned=False, expected=1),
        },
        during=one_row_still,
    )

    unpinned = second.winner_outcome.unwrap()
    (conflict,) = second.loser_outcomes
    assert conflict.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    (row,) = _pins(match.id)
    assert (row['revision'], row['pinned_at'], row['pinned_by']) == (
        2,
        None,
        None,
    )
    assert unpinned.revision == 2
    assert len(world.annotation_audit('pin')) == 1
    assert len(_audit(world.tournament_id, service.MATCH_UNPINNED_EVENT)) == 1
    _assert_committed_and_rolled_back(race, second)

    # Every orga reads the one state, and the lock is free.
    for viewer in (ada, bob):
        shown = world.row(viewer, match, when=world.started_at + MINUTE)
        assert (shown.pin_revision, shown.pinned_at) == (2, None)
    _assert_tournament_unlocked(world.tournament_id)
    heard.assert_not_called()


# -- an acknowledgement against the replacement of its episode --


def test_ack_racing_episode_replacement_is_rejected(
    world, race, clock, monkeypatch, admin
):
    ada = world.ada
    # The full correction path retracts and re-confirms, so the final is
    # paired again with the very same players: a new demand, a new episode,
    # and a revision that is 0 again.
    monkeypatch.setattr(
        matches, '_plan_in_place_correction', lambda *args, **kwargs: None
    )
    first, other = world.match(order=0), world.match(order=1)
    final = world.match(round=1)
    clock.to(world.started_at + 5 * MINUTE)
    world.play(first)
    world.play(other)
    old = _open_episode(final.id)
    assert old is not None and old['ack_revision'] == 0

    # Twenty minutes into the old demand: yellow, so the page offers it.
    form = world.row(ada, final, when=world.started_at + 25 * MINUTE)
    assert form.episode_id == old['id']
    assert form.ack_unavailable_reason is None
    pairing = world.members(final)
    # Same winner, other margin.
    corrected_scores = world.scores(
        first, winner=world.members(first)[0], margin=7
    )

    # Every reading of the clock moves 20 minutes on. When the stale
    # acknowledgement gets its turn, the new episode is old enough for it
    # to be acknowledged, if the request were taken for the new one.
    clock.to(world.started_at + 30 * MINUTE, 20 * MINUTE)
    gate = race.park(only='race-correct')
    correcting = race.start(
        'correct',
        lambda: matches.correct_match_result(
            first.id,
            admin.id,
            reason='Wrong margin',
            corrected_scores=corrected_scores,
        ),
    )
    gate.wait_parked([correcting])
    acknowledging = race.start('ack', world.acknowledging(ada, final, form))
    with _observer() as observer:
        _assert_waits_for_the_tournament(observer, acknowledging, correcting)
        # The replacement is flushed, not committed: nobody else sees it.
        assert _open_episode(final.id)['id'] == old['id']
        assert _acks(final.id) == []
    gate.open()

    corrected = correcting.result()
    refused = acknowledging.result()

    assert corrected.is_ok(), corrected
    assert refused.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert world.members(final) == pairing
    renewed = _open_episode(final.id)
    assert renewed['id'] != old['id']
    assert renewed['ack_revision'] == 0
    retired = [e for e in _episodes(final.id) if e['id'] == old['id']]
    assert [e['closed_at'] for e in retired] == [renewed['opened_at']]
    # The stale request wrote nothing at all: no acknowledgement, no
    # revision, no audit entry, and it gave the lock back.
    assert _acks(final.id) == []
    assert retired[0]['ack_revision'] == 0
    assert world.annotation_audit('acknowledgement') == []
    assert acknowledging.commits == []
    assert race.rollbacks_of(acknowledging) >= 1
    assert acknowledging.rollbacks >= 1
    _assert_tournament_unlocked(world.tournament_id)

    # The new alert interval runs on from the opening of the new episode;
    # nothing moved its baseline, and nothing was acknowledged yet.
    read_at = renewed['opened_at'] + 20 * MINUTE
    shown = world.row(ada, final, when=read_at)
    assert shown.episode_id == renewed['id']
    assert shown.latest_acknowledgement is None
    assert shown.acknowledgement_count == 0
    assert shown.alert_interval_us == _microseconds(20 * MINUTE)
    assert shown.ack_unavailable_reason is None

    # Its own form is taken, and the old one is still stale.
    clock.to(read_at)
    again = world.acknowledging(ada, final, shown)()
    assert again.is_ok(), again
    assert again.unwrap().revision == 1
    stale = world.acknowledging(ada, final, form)()
    assert stale.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert len(_acks(final.id)) == 1


# -- pause against confirmation: the tournament first, then the match --


def _signal_snapshot(world: World, match) -> Callable[[], dict]:
    def probe() -> dict:
        return {
            'status': _tournament_row(world.tournament_id)['tournament_status'],
            'confirmed': bool(
                _read(
                    'SELECT confirmed_by FROM lan_tournament_matches'
                    ' WHERE id = :id AND confirmed_by IS NOT NULL',
                    id=match.id,
                )
            ),
        }

    return probe


@pytest.mark.parametrize('park_at', ['clock_edge', 'commit'])
def test_pause_racing_confirmation_obeys_tournament_first(
    park_at, world, race, clock, admin
):
    first, other = world.match(order=0), world.match(order=1)
    # Each reading adds a second, so the later transaction samples later.
    clock.to(world.started_at + 10 * MINUTE, SECOND)
    probe = _signal_snapshot(world, first)
    status_heard = race.listen(signals.tournament_status_changed, probe)
    confirmed_heard = race.listen(signals.match_confirmed, probe)

    # The pause holds the tournament either before it has read the time of
    # its clock edge, or with everything written and not yet committed.
    if park_at == 'clock_edge':
        gate = race.park_before(
            repo, 'set_tournament_status_flush', only='race-pause'
        )
    else:
        gate = race.park(only='race-pause')
    pausing = race.start(
        'pause',
        lambda: tournament_service.change_status(
            world.tournament_id, PAUSED, admin.id
        ),
    )
    gate.wait_parked([pausing])
    confirming = race.start(
        'confirm',
        lambda: matches.admin_set_and_confirm_match(
            first.id, admin.id, world.scores(first)
        ),
    )
    with _observer() as observer:
        _assert_waits_for_the_tournament(observer, confirming, pausing)

        # The confirmation asked for the tournament before any match: its
        # statements so far read the match plainly and then ask for the
        # tournament row. A match lock taken first would meet the pause,
        # which takes the matches after the tournament, in a deadlock.
        issued = list(confirming.statements)
        assert 'FROM lan_tournaments' in issued[-1]
        assert not any(
            'lan_tournament_matches' in statement and 'FOR UPDATE' in statement
            for statement in issued
        )

        # The pause is not committed, and nothing was announced.
        assert _tournament_row(world.tournament_id)['tournament_status'] == (
            'ONGOING'
        )
        assert _episodes(first.id)[0]['closed_at'] is None
        assert status_heard == [] and confirmed_heard == []
    gate.open()

    assert pausing.result().is_ok()
    assert confirming.result().is_ok()

    # The pause committed first, and the confirmation read it under the
    # lock: it closed the demand at the clock the pause froze.
    assert pausing.commits[-1] < confirming.commits[-1]
    frozen = _tournament_row(world.tournament_id)
    assert frozen['tournament_status'] == 'PAUSED'
    assert frozen['operational_clock_running_since'] is None
    (closed,) = _episodes(first.id)
    assert closed['closed_clock_us'] == frozen['operational_clock_elapsed_us']
    # It sampled its time after the pause did, while the clock stood still
    # from then on: a clock read before the pause would have run on.
    waited = _microseconds(closed['closed_at'] - world.started_at)
    assert waited > frozen['operational_clock_elapsed_us']
    # The pause leaves the other demand open and as it was.
    assert _open_episode(other.id)['ack_revision'] == 0
    # Both announced after their commit, and each saw its own result.
    assert [(name, state['status']) for name, state in status_heard] == [
        ('race-pause', 'PAUSED')
    ]
    assert [(name, state['confirmed']) for name, state in confirmed_heard] == [
        ('race-confirm', True)
    ]
    _assert_tournament_unlocked(world.tournament_id)


def test_confirmation_in_flight_makes_the_pause_wait_for_the_tournament(
    world, race, clock, admin
):
    first, other = world.match(order=0), world.match(order=1)
    clock.to(world.started_at + 10 * MINUTE, SECOND)
    probe = _signal_snapshot(world, first)
    status_heard = race.listen(signals.tournament_status_changed, probe)
    confirmed_heard = race.listen(signals.match_confirmed, probe)

    gate = race.park(only='race-confirm')
    confirming = race.start(
        'confirm',
        lambda: matches.admin_set_and_confirm_match(
            first.id, admin.id, world.scores(first)
        ),
    )
    gate.wait_parked([confirming])
    pausing = race.start(
        'pause',
        lambda: tournament_service.change_status(
            world.tournament_id, PAUSED, admin.id
        ),
    )
    with _observer() as observer:
        _assert_waits_for_the_tournament(observer, pausing, confirming)

        # The pause wanted the tournament first and nothing else: it has
        # read and written nothing but that lock.
        assert len(pausing.statements) == 1
        assert 'FROM lan_tournaments' in pausing.statements[0]
        assert 'FOR UPDATE' in pausing.statements[0]
        assert _tournament_row(world.tournament_id)['tournament_status'] == (
            'ONGOING'
        )
        assert _episodes(first.id)[0]['closed_at'] is None
        assert status_heard == [] and confirmed_heard == []
    gate.open()

    assert confirming.result().is_ok()
    assert pausing.result().is_ok()

    # The confirmation committed first: it closed the demand on the running
    # clock, and the pause froze the clock after that.
    assert confirming.commits[-1] < pausing.commits[-1]
    frozen = _tournament_row(world.tournament_id)
    assert frozen['tournament_status'] == 'PAUSED'
    (closed,) = _episodes(first.id)
    assert closed['closed_clock_us'] == _microseconds(
        closed['closed_at'] - world.started_at
    )
    assert closed['closed_clock_us'] < frozen['operational_clock_elapsed_us']
    assert _open_episode(other.id)['ack_revision'] == 0
    assert [(name, state['confirmed']) for name, state in confirmed_heard] == [
        ('race-confirm', True)
    ]
    assert [(name, state['status']) for name, state in status_heard] == [
        ('race-pause', 'PAUSED')
    ]
    _assert_tournament_unlocked(world.tournament_id)


# -- orga revocation: the permission is checked under the lock --


@pytest.mark.parametrize('kind', ['pin', 'acknowledgement'])
def test_revocation_while_waiting_prevents_annotation(kind, world, race, clock):
    ada, bob = world.ada, world.bob
    match = world.match()
    clock.to(world.started_at + 20 * MINUTE)
    form = world.row(ada, match, when=clock.now)
    assert form.ack_unavailable_reason is None
    heard = race.watch_signals()
    revoked_heard = race.listen(
        signals.tournament_orga_revoked,
        lambda: _assignments(world.tournament_id, ada),
    )

    # The revocation holds the tournament with the grant deleted, and the
    # annotation arrives, passes its early look and has to wait.
    gate = race.park_at_audit(orgas, only='race-revoke')
    revoking = race.start(
        'revoke',
        lambda: orgas.revoke_orga(world.tournament_id, ada.id, bob.id),
    )
    gate.wait_parked([revoking])
    annotating = race.start(
        'annotate', world.annotating(kind, ada, match, form)
    )
    with _observer() as observer:
        _assert_waits_for_the_tournament(observer, annotating, revoking)
        assert _assignments(world.tournament_id, ada) == 1
        assert world.annotations_of(kind, match) == []
        assert revoked_heard == []
    gate.open()

    assert revoking.result().is_ok()
    refused = annotating.result()

    # The permission is read again under the lock, and it is gone.
    assert refused.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert _assignments(world.tournament_id, ada) == 0
    assert world.annotations_of(kind, match) == []
    assert world.annotation_audit(kind) == []
    assert _open_episode(match.id)['ack_revision'] == 0
    assert annotating.commits == []
    assert race.rollbacks_of(annotating) >= 1
    assert annotating.rollbacks >= 1
    assert len(revoking.commits) == 1
    assert len(_audit(world.tournament_id, 'tournament-orga-revoked')) == 1
    _assert_tournament_unlocked(world.tournament_id)

    # The other orga, still assigned, is not affected.
    allowed = world.annotating(kind, bob, match, form)()
    assert allowed.is_ok(), allowed
    assert len(world.annotations_of(kind, match)) == 1
    # The only announcement is the revocation, after its commit.
    assert revoked_heard == [('race-revoke', 0)]
    assert _announced(heard) == ['TournamentOrgaRevokedEvent']


@pytest.mark.parametrize('kind', ['pin', 'acknowledgement'])
def test_revocation_after_authorization_waits_for_annotation_commit(
    kind, world, race, clock
):
    ada, bob = world.ada, world.bob
    match = world.match()
    clock.to(world.started_at + 20 * MINUTE)
    form = world.row(ada, match, when=clock.now)
    assert form.ack_unavailable_reason is None
    heard = race.watch_signals()
    revoked_heard = race.listen(
        signals.tournament_orga_revoked,
        lambda: _assignments(world.tournament_id, ada),
    )

    # The annotation passed its check under the lock, wrote, and stops
    # right before its commit. The revocation arrives now.
    gate = race.park(only='race-annotate')
    annotating = race.start(
        'annotate', world.annotating(kind, ada, match, form)
    )
    gate.wait_parked([annotating])
    revoking = race.start(
        'revoke',
        lambda: orgas.revoke_orga(world.tournament_id, ada.id, bob.id),
    )
    with _observer() as observer:
        _assert_waits_for_the_tournament(observer, revoking, annotating)

        # All the revocation did was ask for the tournament: it has not
        # read or deleted the grant, and the annotation is not visible.
        assert len(revoking.statements) == 1
        assert not any(
            'lan_tournament_orgas' in statement
            for statement in revoking.statements
        )
        assert _assignments(world.tournament_id, ada) == 1
        assert world.annotations_of(kind, match) == []
        assert revoked_heard == []
    gate.open()

    assert annotating.result().is_ok()
    assert revoking.result().is_ok()

    # The annotation committed before the revocation did, and it is the
    # only one: the grant was alive for all of it.
    assert annotating.commits[-1] < revoking.commits[-1]
    assert _assignments(world.tournament_id, ada) == 0
    assert len(world.annotations_of(kind, match)) == 1
    assert len(world.annotation_audit(kind)) == 1
    assert len(_audit(world.tournament_id, 'tournament-orga-revoked')) == 1

    # Nothing of the revoked orga commits after the revocation did.
    later = world.row(bob, match, when=clock.now)
    refused = world.annotating(kind, ada, match, later)()
    assert refused.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert len(world.annotations_of(kind, match)) == 1
    assert len(world.annotation_audit(kind)) == 1
    assert revoked_heard == [('race-revoke', 0)]
    assert _announced(heard) == ['TournamentOrgaRevokedEvent']
    _assert_tournament_unlocked(world.tournament_id)
