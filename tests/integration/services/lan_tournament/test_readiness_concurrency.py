"""Real PostgreSQL races of the readiness, lifecycle and invitation flows.

Every contender runs in its own thread with its own application context,
session and database connection. The holder is parked inside its open
transaction at a seam of the real code path. The waiter is then observed in
PostgreSQL as blocked by exactly the holder's backend (`pg_blocking_pids`)
before the holder is released. All waits are bounded, and every exit path
opens the gates, aborts the barriers and joins the threads, so a failing
test cannot leave a parked transaction behind.

The database identity is not guarded here: the inherited integration fixtures
own the schema. Nothing in this module weakens a guard of another module.
"""

from collections import Counter
from datetime import datetime, UTC
import secrets
import threading
import time
import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_invitation_service as invitations,
    tournament_match_service as engine,
    tournament_orga_repository as orga_repository,
    tournament_orga_service as orgas,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service as lifecycle,
)
from byceps.services.lan_tournament.blueprints.readiness_csrf import (
    READINESS_CSRF_SESSION_KEY,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.uuid import uuid7

from tests.helpers import http_client, log_in_user


ENGLISH = {'Accept-Language': 'en'}
SITE_ROOT = 'http://www.acmecon.test/lan-tournaments'

# Server-side bounds, applied as `SET LOCAL` to every transaction of a worker.
LOCK_TIMEOUT = '8s'
STATEMENT_TIMEOUT = '12s'

# Client-side bounds in seconds.
GATE_SECONDS = 15
PARK_SECONDS = 20
EDGE_SECONDS = 8
JOIN_SECONDS = 15

_current = threading.local()


# -- harness --


class Worker:
    """One contender: own thread, application context, session and backend."""

    def __init__(self, race, label, call, gate):
        self.race = race
        self.label = label
        self.call = call
        self.gate = gate
        self.pid = None
        self.outcome = None
        self.error = None
        self.trace = None
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f'race-{label}', daemon=True
        )

    def _on_begin(self, session, transaction, connection):
        # `SET LOCAL` ends with the transaction, so every new transaction of
        # this session is bounded again. The backend is recorded each time:
        # the pool may hand out another connection for the next transaction.
        connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        )
        self.pid = connection.scalar(text('SELECT pg_backend_pid()'))

    def _run(self):
        _current.worker = self
        try:
            with self.race.app.app_context():
                event.listen(db.session(), 'after_begin', self._on_begin)
                try:
                    isolation = db.session.scalar(
                        text('SHOW transaction_isolation')
                    )
                    assert isolation == 'read committed', isolation
                    db.session.rollback()
                    if self.gate is not None:
                        self.gate.wait(GATE_SECONDS)
                    self.outcome = self.call()
                finally:
                    db.session.rollback()
                    db.session.remove()
        except BaseException as error:  # reported by the test, never lost
            self.error = error
            self.trace = traceback.format_exc()
            # Whoever waits at a gate for this contender, a fellow worker or
            # the test itself, fails fast instead of waiting for the timeout.
            self.race.abort_gates()
        finally:
            _current.worker = None
            self.finished.set()


class Seam:
    """Park the first matching worker right after `module.name` returned.

    The worker stays inside its open transaction, with all its locks, until
    the seam is released.
    """

    def __init__(self, monkeypatch, module, name, only):
        self.name = name
        self.only = only
        self.reached = threading.Event()
        self.release = threading.Event()
        self.holder = None
        self._claim = threading.Lock()
        real = getattr(module, name)

        def parked(*args, **kwargs):
            result = real(*args, **kwargs)
            worker = getattr(_current, 'worker', None)
            if worker is None or (only is not None and worker.label != only):
                return result
            with self._claim:
                if self.holder is not None:
                    return result
                self.holder = worker
            self.reached.set()
            if not self.release.wait(PARK_SECONDS):
                raise AssertionError(
                    f'{worker.label} was never released from seam {name}'
                )
            return result

        monkeypatch.setattr(module, name, parked)


class Race:
    """Start contenders, observe their wait edges, and always clean up."""

    def __init__(self, app, monkeypatch, *, join_seconds=JOIN_SECONDS):
        self.app = app
        self.monkeypatch = monkeypatch
        self.join_seconds = join_seconds
        self.workers = []
        self.gates = []
        self.seams = []

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        # Open everything first: no parked or gated thread may outlive a test
        # which has already failed. Then join, bounded, whatever happened.
        for seam in self.seams:
            seam.release.set()
        self.abort_gates()
        deadline = time.monotonic() + self.join_seconds
        stuck = []
        for worker in self.workers:
            worker.thread.join(max(0.0, deadline - time.monotonic()))
            if worker.thread.is_alive():
                stuck.append(worker.label)
        if stuck and kind is None:
            raise AssertionError(
                self.report(f'contenders still running after exit: {stuck}')
            )
        return False

    def gate(self, parties):
        gate = threading.Barrier(parties, timeout=GATE_SECONDS)
        self.gates.append(gate)
        return gate

    def abort_gates(self):
        # Aborting a barrier everybody has already passed is harmless.
        for gate in self.gates:
            gate.abort()

    def seam(self, module, name, *, only=None):
        seam = Seam(self.monkeypatch, module, name, only)
        self.seams.append(seam)
        return seam

    def start(self, label, call, *, gate=None):
        worker = Worker(self, label, call, gate)
        self.workers.append(worker)
        worker.thread.start()
        return worker

    def pass_gate(self, gate):
        try:
            gate.wait(GATE_SECONDS)
        except threading.BrokenBarrierError:
            pytest.fail(self.report('a contender never reached its gate'))

    def wait_parked(self, seam, *workers):
        """Return the worker parked at the seam; fail fast if none can be."""
        deadline = time.monotonic() + PARK_SECONDS
        while time.monotonic() < deadline:
            if seam.reached.wait(0.02):
                return seam.holder
            if workers and all(w.finished.is_set() for w in workers):
                break
        pytest.fail(self.report(f'nobody was parked at {seam.name}'))

    def blocked_by(self, waiter, holder):
        """Observe the exact edge `waiter` -> `holder` in `pg_blocking_pids`.

        Return `False` if the waiter finished without ever waiting, which
        is what a lockless implementation does: the caller then asserts the
        business outcome instead of timing out.
        """
        deadline = time.monotonic() + EDGE_SECONDS
        with db.engine.connect().execution_options(
            isolation_level='AUTOCOMMIT'
        ) as connection:
            while time.monotonic() < deadline:
                # Read both backends in every round: a worker's next
                # transaction may run on another pooled connection, and the
                # waiter's recorded backend is stale until its call begins.
                if (
                    waiter.pid is not None
                    and holder.pid is not None
                    and waiter.pid != holder.pid
                ):
                    blockers = connection.scalar(
                        text('SELECT pg_blocking_pids(:pid)'),
                        {'pid': waiter.pid},
                    )
                    if holder.pid in blockers:
                        return True
                if waiter.finished.wait(0.02):
                    return False
        pytest.fail(
            self.report(
                f'no wait edge {waiter.label} ({waiter.pid}) '
                f'-> {holder.label} ({holder.pid})'
            )
        )

    def hold_and_wait(self, seam, holder_call, waiter_call, waiter_label):
        """Park the named holder at the seam, then release the waiter.

        The waiter is fully started (session, backend, isolation verified)
        and waits at a barrier shared with this thread, so it begins its
        contested call strictly after the holder holds the locks.
        Return (holder, waiter, edge observed).
        """
        assert seam.only is not None, 'a deterministic holder needs a label'
        go = self.gate(2)
        holder = self.start(seam.only, holder_call)
        waiter = self.start(waiter_label, waiter_call, gate=go)
        self.wait_parked(seam, holder)
        self.pass_gate(go)
        return holder, waiter, self.blocked_by(waiter, holder)

    def simultaneous(self, seam, first_call, second_call, labels):
        """Start two contenders at one barrier; whoever locks first is held.

        Return (holder, waiter, edge observed).
        """
        go = self.gate(2)
        first = self.start(labels[0], first_call, gate=go)
        second = self.start(labels[1], second_call, gate=go)
        holder = self.wait_parked(seam, first, second)
        waiter = second if holder is first else first
        return holder, waiter, self.blocked_by(waiter, holder)

    def join(self, worker):
        """Return the worker's outcome; a hang or a crash fails the test."""
        if not worker.finished.wait(self.join_seconds):
            pytest.fail(
                self.report(
                    f'{worker.label} did not finish within '
                    f'{self.join_seconds} s'
                )
            )
        if worker.error is not None:
            pytest.fail(
                self.report(f'{worker.label} crashed: {worker.error!r}')
            )
        return worker.outcome

    def report(self, message):
        lines = [message]
        for worker in self.workers:
            state = 'finished' if worker.finished.is_set() else 'running'
            lines.append(f'  {worker.label}: {state}, backend {worker.pid}')
            if worker.trace:
                lines.append(worker.trace)
        lines.extend(self._activity())
        return '\n'.join(lines)

    def _activity(self):
        pids = [w.pid for w in self.workers if w.pid is not None]
        if not pids:
            return []
        try:
            with db.engine.connect().execution_options(
                isolation_level='AUTOCOMMIT'
            ) as connection:
                rows = connection.execute(
                    text(
                        'SELECT pid, state, wait_event_type, wait_event,'
                        ' pg_blocking_pids(pid) FROM pg_stat_activity'
                        ' WHERE pid = ANY(:pids)'
                    ),
                    {'pids': pids},
                ).all()
        except Exception as error:  # diagnostics must never mask the failure
            return [f'  pg_stat_activity unavailable: {error!r}']
        return [f'  pg_stat_activity: {tuple(row)}' for row in rows]


# -- contender bodies --


def _finish(result):
    """The routes' contract: Err rolls back, Ok commits once, then effects."""
    if result.is_err():
        repo.rollback_session()
        return SimpleNamespace(ok=False, error=result.unwrap_err(), change=None)
    change = result.unwrap()
    repo.commit_session()
    readiness.dispatch_readiness_effects(change)
    return SimpleNamespace(ok=True, error=None, change=change)


def claim(match_id, side, user_id, generation, revision):
    def call():
        return _finish(
            readiness.claim_ready_flush(
                match_id,
                side,
                user_id,
                expected_pairing_generation=generation,
                expected_readiness_revision=revision,
            )
        )

    return call


def revoke(match_id, side, user_id, generation, revision):
    def call():
        return _finish(
            readiness.revoke_ready_flush(
                match_id,
                side,
                user_id,
                expected_pairing_generation=generation,
                expected_readiness_revision=revision,
            )
        )

    return call


def pause(tournament_id, initiator_id):
    def call():
        result = lifecycle.change_status(
            tournament_id, TournamentStatus.PAUSED, initiator_id
        )
        return SimpleNamespace(
            ok=result.is_ok(),
            error=None if result.is_ok() else result.unwrap_err(),
            change=None,
        )

    return call


def replace_occupant(world, old_participant_id, new_participant_id):
    """Swap one occupant through the engine's own transactional adapters."""

    def call():
        repo.get_tournament_for_update(world.tournament_id)
        repo.lock_matches_for_update([world.match_id])
        engine._delete_contestant_from_match_flush(
            world.match_id, participant_id=old_participant_id
        )
        engine._create_match_contestant_flush(
            TournamentMatchToContestant(
                id=TournamentMatchToContestantID(uuid7()),
                tournament_match_id=world.match_id,
                team_id=None,
                participant_id=new_participant_id,
                score=None,
                created_at=datetime.now(UTC),
            )
        )
        repo.commit_session()
        return SimpleNamespace(ok=True, error=None, change=None)

    return call


def dispatch(invitation_ids):
    def call():
        result = invitations.dispatch_match_invitations(invitation_ids)
        return SimpleNamespace(
            ok=result.is_ok(),
            error=None if result.is_ok() else result.unwrap_err(),
            change=None,
        )

    return call


def catch_up(tournament_id):
    """The sweep job that the handler enqueues after a resume to ONGOING."""

    def call():
        result = invitations.sweep_tournament_invitations(tournament_id)
        return SimpleNamespace(ok=result.is_ok(), error=None, change=None)

    return call


def revoke_orga(tournament_id, user_id, initiator_id):
    def call():
        result = orgas.revoke_orga(tournament_id, user_id, initiator_id)
        return SimpleNamespace(
            ok=result.is_ok(),
            error=None if result.is_ok() else result.unwrap_err(),
            change=None,
        )

    return call


def post_claim(client, match_id, form):
    """A real authenticated POST through the decorated site route."""

    def call():
        response = client.post(
            f'{SITE_ROOT}/matches/{match_id}/ready/claim',
            data=form,
            headers=ENGLISH,
        )
        return SimpleNamespace(
            ok=response.status_code == 302,
            error=None,
            change=None,
            status=response.status_code,
        )

    return call


# -- persisted facts, always read through a connection of their own --


def _facts(match_id):
    with Session(db.engine) as session:
        row = session.get(DbTournamentMatch, match_id)
        return SimpleNamespace(
            generation=row.pairing_generation,
            revision=row.readiness_revision,
            pairing_id=row.pairing_id,
            ready_at_a=row.ready_at_a,
            ready_by_a=row.ready_by_a,
            ready_at_b=row.ready_at_b,
            ready_by_b=row.ready_by_b,
            hold_a=row.invitation_hold_a,
            hold_b=row.invitation_hold_b,
        )


def _revisions(match_id):
    facts = _facts(match_id)
    return facts.generation, facts.revision


def _audit(tournament_id, match_id, prefix='match-ready-'):
    with Session(db.engine) as session:
        entries = session.scalars(
            select(DbTournamentLogEntry)
            .where(DbTournamentLogEntry.tournament_id == tournament_id)
            .order_by(DbTournamentLogEntry.occurred_at, DbTournamentLogEntry.id)
        ).all()
        return [
            SimpleNamespace(type=e.event_type, data=e.data, actor=e.initiator_id)
            for e in entries
            if e.event_type.startswith(prefix)
            and e.data.get('match_id') == str(match_id)
        ]


def _types(tournament_id, match_id, prefix='match-ready-'):
    return [e.type for e in _audit(tournament_id, match_id, prefix)]


def _invitation_rows(match_id):
    with Session(db.engine) as session:
        rows = session.scalars(
            select(DbMatchInvitation)
            .where(DbMatchInvitation.match_id == match_id)
            .order_by(DbMatchInvitation.id)
        ).all()
        return [
            SimpleNamespace(
                id=r.id,
                recipient=r.recipient_id,
                generation=r.pairing_generation,
                status=r.status,
                attempts=r.attempts,
                token=r.dispatch_token,
                error=r.last_error,
            )
            for r in rows
        ]


def _tournament_status(tournament_id):
    with Session(db.engine) as session:
        return session.get(DbTournament, tournament_id).tournament_status


def _count_entries(tournament_id, event_type):
    with Session(db.engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(DbTournamentLogEntry)
            .where(
                DbTournamentLogEntry.tournament_id == tournament_id,
                DbTournamentLogEntry.event_type == event_type,
            )
        )


def _orga_count(tournament_id, user_id):
    with Session(db.engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(DbTournamentOrga)
            .where(
                DbTournamentOrga.tournament_id == tournament_id,
                DbTournamentOrga.user_id == user_id,
            )
        )


def _actors(world):
    """Map each side to the user who occupies it, from the live pairing."""
    pairing = repo.get_match_pairing(world.match_id)
    db.session.rollback()
    return {
        MatchSide.A: world.user_of[pairing.side_a.id],
        MatchSide.B: world.user_of[pairing.side_b.id],
    }


def _side_of(world, user_id):
    return next(s for s, u in _actors(world).items() if u == user_id)


# -- fixtures --


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Race23Player{i}') for i in range(4)]


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('Race23Orga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.administrate'})
    log_in_user(user.id)
    return user


def _purge(tournament_id):
    """Remove what the world and its threads committed outside any fixture."""
    db.session.rollback()
    if repo.find_tournament(tournament_id) is not None:
        lifecycle.delete_tournament(tournament_id)
    db.session.execute(
        delete(DbMatchInvitation).where(
            DbMatchInvitation.tournament_id == tournament_id
        )
    )
    db.session.execute(
        delete(DbMatchPairing).where(
            DbMatchPairing.tournament_id == tournament_id
        )
    )
    db.session.execute(
        delete(DbTournamentLogEntry).where(
            DbTournamentLogEntry.tournament_id == tournament_id
        )
    )
    db.session.commit()


@pytest.fixture
def world(admin_app, party, users):
    """An ONGOING 1v1 match of the first two players, pairing established.

    The pairing and the two recipient intents come from the sanctioned
    operation, in its own committed transaction, like in production.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Race 23 {uuid7()}',
        now,
        game_format='ONE_V_ONE',
        elimination_mode='SINGLE_ELIMINATION',
        tournament_status='ONGOING',
    )
    tournament_id = tournament.id
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament_id, now)
        for user in users
    ]
    db.session.add_all(participants)
    match = DbTournamentMatch(uuid7(), tournament_id, now, match_order=0, round=0)
    match_id = match.id
    db.session.add(match)
    db.session.flush()
    for participant in participants[:2]:
        db.session.add(
            DbTournamentMatchToContestant(
                uuid7(), match_id, now, participant_id=participant.id
            )
        )
    db.session.commit()
    world = SimpleNamespace(
        tournament_id=tournament_id,
        match_id=match_id,
        participant_ids=[p.id for p in participants],
        user_of={p.id: p.user_id for p in participants},
        user_ids=[user.id for user in users],
    )
    try:
        refreshed = readiness.refresh_pairing_and_invitations_flush(
            match_id, occurred_at=now
        )
        assert refreshed.is_ok(), refreshed
        db.session.commit()
        world.actors = _actors(world)
        yield world
    finally:
        _purge(tournament_id)


@pytest.fixture
def effects(monkeypatch):
    """Record the post-commit queueing instead of performing it.

    The hand-over is one queue job per batch, so the batch is what the job
    would carry.
    """
    state = SimpleNamespace(dispatched=[], lock=threading.Lock())

    def enqueue(function, *args, **kwargs):
        assert function is invitations.dispatch_match_invitations
        ids = tuple(args[0])
        with state.lock:
            state.dispatched.append(ids)

    monkeypatch.setattr(invitations.jobqueue, 'enqueue', enqueue)
    return state


@pytest.fixture
def queue(monkeypatch):
    """Observe the real dispatcher's queueing without running any job."""
    state = SimpleNamespace(enqueued=[], lock=threading.Lock())

    def enqueue(function, *args, **kwargs):
        with state.lock:
            state.enqueued.append((function, args))

    monkeypatch.setattr(invitations.jobqueue, 'enqueue', enqueue)
    monkeypatch.setattr(
        invitations.jobqueue, 'enqueue_at', lambda *args, **kwargs: None
    )
    return state


def _claim_now(world, side):
    """One sequential, committed claim of the setup state (not a race)."""
    generation, revision = _revisions(world.match_id)
    verdict = claim(
        world.match_id, side, world.actors[side], generation, revision
    )()
    assert verdict.ok, verdict.error
    db.session.rollback()
    return verdict


def _dispatched(change):
    """What the post-commit dispatch of this change must have handed over.

    An empty selection is never queued, so it leaves no dispatch record.
    """
    ids = change.pending_invitation_ids
    return [ids] if ids else []


# -- the named races --


def test_two_claims_have_one_change(admin_app, monkeypatch, world, effects):
    side = MatchSide.A
    actor = world.actors[side]
    generation, revision = _revisions(world.match_id)
    before = _facts(world.match_id)
    assert before.ready_at_a is None and before.ready_at_b is None
    with Race(admin_app, monkeypatch) as race:
        seam = race.seam(repo, 'set_readiness_revision_flush')
        holder, waiter, blocked = race.simultaneous(
            seam,
            claim(world.match_id, side, actor, generation, revision),
            claim(world.match_id, side, actor, generation, revision),
            ('first', 'second'),
        )
        seam.release.set()
        winner = race.join(holder)
        loser = race.join(waiter)
    assert blocked, 'the second claim never waited for the first'
    assert winner.ok, winner.error
    assert not loser.ok and loser.error == 'readiness_conflict'
    facts = _facts(world.match_id)
    assert facts.revision == revision + 1
    assert facts.generation == generation
    assert facts.ready_by_a == actor and facts.ready_at_a is not None
    assert facts.ready_at_b is None and facts.ready_by_b is None
    assert _types(world.tournament_id, world.match_id) == [
        'match-ready-claimed'
    ]
    # Only the winner produced effects; the loser dispatched nothing.
    assert winner.change.pending_invitation_ids
    assert effects.dispatched == _dispatched(winner.change)
    assert len(winner.change.events) == 1
    assert loser.change is None


def test_claim_revoke_has_one_valid_revision(
    admin_app, monkeypatch, world, effects
):
    side_a, side_b = MatchSide.A, MatchSide.B
    actor_a, actor_b = world.actors[side_a], world.actors[side_b]
    _claim_now(world, side_a)
    generation, revision = _revisions(world.match_id)
    effects.dispatched.clear()
    with Race(admin_app, monkeypatch) as race:
        seam = race.seam(repo, 'set_readiness_revision_flush')
        holder, waiter, blocked = race.simultaneous(
            seam,
            claim(world.match_id, side_b, actor_b, generation, revision),
            revoke(world.match_id, side_a, actor_a, generation, revision),
            ('claim', 'revoke'),
        )
        seam.release.set()
        winner = race.join(holder)
        loser = race.join(waiter)
    assert blocked, 'the second operation never waited for the first'
    assert winner.ok, winner.error
    assert not loser.ok and loser.error == 'readiness_conflict'
    facts = _facts(world.match_id)
    # One valid revision: exactly one of the two operations is in the facts.
    assert facts.revision == revision + 1 and facts.generation == generation
    types = _types(world.tournament_id, world.match_id)
    states = {(r.status, r.error) for r in _invitation_rows(world.match_id)}
    if holder.label == 'revoke':
        assert facts.ready_at_a is None and facts.ready_by_a is None
        assert facts.ready_at_b is None and facts.ready_by_b is None
        assert facts.hold_a and not facts.hold_b
        assert types == ['match-ready-claimed', 'match-ready-revoked']
        revoked = _audit(world.tournament_id, world.match_id)[-1]
        assert revoked.type == 'match-ready-revoked'
        assert revoked.actor == actor_a and 'reason' not in revoked.data
        assert len(winner.change.events) == 1
        # The revocation holds the unsent work, nothing is handed over.
        assert states == {('suppressed', 'readiness_hold')}
        assert winner.change.pending_invitation_ids == ()
    else:
        assert facts.ready_by_a == actor_a and facts.ready_by_b == actor_b
        assert not facts.hold_a and not facts.hold_b
        assert types == ['match-ready-claimed', 'match-ready-claimed']
        assert len(winner.change.events) == 2  # claimed and both ready
        assert states == {('pending', None)}
    assert effects.dispatched == _dispatched(winner.change)


@pytest.mark.parametrize('holder_label', ['pause', 'claim'])
def test_pause_claim_serializes(
    admin_app, monkeypatch, world, effects, admin, holder_label
):
    side = MatchSide.A
    actor = world.actors[side]
    generation, revision = _revisions(world.match_id)
    pausing = pause(world.tournament_id, admin.id)
    claiming = claim(world.match_id, side, actor, generation, revision)
    with Race(admin_app, monkeypatch) as race:
        if holder_label == 'pause':
            seam = race.seam(
                lifecycle, '_persist_status_change_flush', only='pause'
            )
            holder, waiter, blocked = race.hold_and_wait(
                seam, pausing, claiming, 'claim'
            )
        else:
            seam = race.seam(
                repo, 'set_readiness_revision_flush', only='claim'
            )
            holder, waiter, blocked = race.hold_and_wait(
                seam, claiming, pausing, 'pause'
            )
        seam.release.set()
        held = race.join(holder)
        waited = race.join(waiter)
    assert blocked, f'{waiter.label} never waited for {holder.label}'
    facts = _facts(world.match_id)
    assert _tournament_status(world.tournament_id) == 'PAUSED'
    assert facts.generation == generation
    if holder_label == 'pause':
        # The claim saw the committed pause under the lock and wrote nothing.
        assert held.ok and not waited.ok
        assert waited.error == 'tournament_not_ongoing'
        assert facts.ready_at_a is None and facts.ready_by_a is None
        assert facts.revision == revision
        assert _types(world.tournament_id, world.match_id) == []
        assert effects.dispatched == []
    else:
        # The claim committed first; the pause then suppressed unsent work.
        assert held.ok and waited.ok, (held.error, waited.error)
        assert facts.ready_by_a == actor and facts.revision == revision + 1
        assert _types(world.tournament_id, world.match_id) == [
            'match-ready-claimed'
        ]
        assert held.change.pending_invitation_ids
        assert effects.dispatched == _dispatched(held.change)
    rows = _invitation_rows(world.match_id)
    assert len(rows) == 2
    assert {(r.status, r.error) for r in rows} == {
        ('suppressed', 'tournament_paused')
    }
    assert all(r.token is None for r in rows)


@pytest.mark.parametrize('claimant', ['old_occupant', 'new_occupant'])
def test_pair_replace_claim_cannot_target_new_occupant(
    admin_app, monkeypatch, world, effects, claimant
):
    old_participant = world.participant_ids[0]
    new_participant = world.participant_ids[2]
    old_user = world.user_of[old_participant]
    new_user = world.user_of[new_participant]
    old_side = _side_of(world, old_user)
    generation, revision = _revisions(world.match_id)
    # The page the claimant saw before the replacement.
    stale_claimant = old_user if claimant == 'old_occupant' else new_user
    stale = claim(
        world.match_id, old_side, stale_claimant, generation, revision
    )
    replacing = replace_occupant(world, old_participant, new_participant)
    with Race(admin_app, monkeypatch) as race:
        seam = race.seam(
            engine, '_create_match_contestant_flush', only='replace'
        )
        holder, waiter, blocked = race.hold_and_wait(
            seam, replacing, stale, 'claim'
        )
        seam.release.set()
        replaced = race.join(holder)
        claimed = race.join(waiter)
    assert blocked, 'the stale claim never waited for the replacement'
    assert replaced.ok, replaced.error
    assert not claimed.ok and claimed.error == 'readiness_conflict'
    facts = _facts(world.match_id)
    assert facts.generation > generation and facts.revision > revision
    assert facts.ready_at_a is None and facts.ready_by_a is None
    assert facts.ready_at_b is None and facts.ready_by_b is None
    assert _types(world.tournament_id, world.match_id) == []
    assert effects.dispatched == []
    # The current pairing's recipients are the new pair, not the old one.
    current = {
        r.recipient
        for r in _invitation_rows(world.match_id)
        if r.generation == facts.generation
    }
    assert current == {world.user_of[world.participant_ids[1]], new_user}
    # Controls, sequential on purpose: only fresh state and current authority
    # can claim, and the claim lands on the new occupant alone.
    assert old_user not in current
    fresh_generation, fresh_revision = _revisions(world.match_id)
    refused = readiness.claim_ready_flush(
        world.match_id,
        old_side,
        old_user,
        expected_pairing_generation=fresh_generation,
        expected_readiness_revision=fresh_revision,
    )
    assert refused.unwrap_err() == 'readiness_forbidden'
    repo.rollback_session()
    new_side = _side_of(world, new_user)
    verdict = claim(
        world.match_id, new_side, new_user, fresh_generation, fresh_revision
    )()
    assert verdict.ok, verdict.error
    db.session.rollback()
    after = _facts(world.match_id)
    if new_side is MatchSide.A:
        assert after.ready_by_a == new_user and after.ready_at_b is None
    else:
        assert after.ready_by_b == new_user and after.ready_at_a is None


@pytest.mark.parametrize('holder_label', ['dispatch', 'catchup'])
def test_dispatch_catchup_reserves_once(
    admin_app, monkeypatch, world, queue, holder_label
):
    ids = [row.id for row in _invitation_rows(world.match_id)]
    assert len(ids) == 2
    dispatching = dispatch(ids)
    catching_up = catch_up(world.tournament_id)
    with Race(admin_app, monkeypatch) as race:
        if holder_label == 'dispatch':
            seam = race.seam(
                repo, 'claim_invitation_dispatch_flush', only='dispatch'
            )
            holder, waiter, blocked = race.hold_and_wait(
                seam, dispatching, catching_up, 'catchup'
            )
        else:
            seam = race.seam(
                repo, 'select_invitation_retry_ids_flush', only='catchup'
            )
            holder, waiter, blocked = race.hold_and_wait(
                seam, catching_up, dispatching, 'dispatch'
            )
        seam.release.set()
        race.join(holder)
        race.join(waiter)
    assert blocked, f'{waiter.label} never waited for {holder.label}'
    rows = _invitation_rows(world.match_id)
    # Each invitation was reserved exactly once and handed to the queue once,
    # whichever contender won it.
    assert {r.id for r in rows} == set(ids)
    assert [r.attempts for r in rows] == [1, 1]
    assert {r.status for r in rows} == {'queued'}
    assert all(r.token is not None for r in rows)
    enqueued = Counter(args[0] for _, args in queue.enqueued)
    assert enqueued == Counter(ids)
    assert {args[1] for _, args in queue.enqueued} == {r.token for r in rows}
    assert {function for function, _ in queue.enqueued} == {
        invitations.deliver_match_invitation
    }


@pytest.mark.parametrize('holder_label', ['revoke', 'request'])
def test_revoked_orga_request_cannot_commit(
    site_app, monkeypatch, world, effects, orga, admin, holder_label
):
    orgas.assign_orga(world.tournament_id, orga.id, admin.id).unwrap()
    side = MatchSide.A
    generation, revision = _revisions(world.match_id)
    before = _facts(world.match_id)
    token = secrets.token_urlsafe(32)
    form = {
        'csrf_token': token,
        'side': side.value,
        'expected_pairing_generation': str(generation),
        'expected_readiness_revision': str(revision),
    }
    revoking = revoke_orga(world.tournament_id, orga.id, admin.id)
    with http_client(site_app, user_id=orga.id) as client:
        with client.session_transaction() as session:
            session[READINESS_CSRF_SESSION_KEY] = {
                'user_id': str(orga.id),
                'token': token,
            }
        requesting = post_claim(client, world.match_id, form)
        with Race(site_app, monkeypatch) as race:
            if holder_label == 'revoke':
                seam = race.seam(
                    orga_repository, 'delete_orga', only='revoke'
                )
                holder, waiter, blocked = race.hold_and_wait(
                    seam, revoking, requesting, 'request'
                )
            else:
                seam = race.seam(
                    repo, 'set_readiness_revision_flush', only='request'
                )
                holder, waiter, blocked = race.hold_and_wait(
                    seam, requesting, revoking, 'revoke'
                )
            seam.release.set()
            held = race.join(holder)
            waited = race.join(waiter)
    assert blocked, f'{waiter.label} never waited for {holder.label}'
    request_reply = held if holder_label == 'request' else waited
    revocation = waited if holder_label == 'request' else held
    assert revocation.ok, revocation.error
    # The revocation committed in both orders, and it is audited once.
    assert _orga_count(world.tournament_id, orga.id) == 0
    assert _count_entries(world.tournament_id, 'tournament-orga-revoked') == 1
    facts = _facts(world.match_id)
    if holder_label == 'revoke':
        # The request passed its decorators while the appointment still
        # existed, but the service re-resolved the scope under the lock.
        assert request_reply.status == 403
        assert facts == before
        assert _types(world.tournament_id, world.match_id) == []
        assert effects.dispatched == []
    else:
        # Committed before the revocation: legitimate, and audited as orga.
        assert request_reply.status == 302
        assert facts.ready_by_a == orga.id and facts.revision == revision + 1
        (entry,) = _audit(world.tournament_id, world.match_id)
        assert entry.type == 'match-ready-claimed'
        assert entry.data['actor_role'] == 'orga' and entry.actor == orga.id
        assert len(effects.dispatched) == 1


# -- the harness itself must not leak parked transactions --


def test_a_failing_body_releases_parked_workers_and_gates(
    admin_app, monkeypatch
):
    parkable = SimpleNamespace(park=lambda: 'parked')
    gates = []
    workers = []
    with pytest.raises(RuntimeError, match='body failed'):
        with Race(admin_app, monkeypatch) as race:
            seam = race.seam(parkable, 'park', only='holder')
            gate = race.gate(2)
            gates.append(gate)
            workers.append(race.start('holder', parkable.park))
            workers.append(race.start('gated', lambda: 'late', gate=gate))
            race.wait_parked(seam, workers[0])
            raise RuntimeError('body failed')
    # Exit opened the seam and broke the gate; nobody is left parked.
    assert all(worker.finished.is_set() for worker in workers)
    assert gates[0].broken
    assert not any(worker.thread.is_alive() for worker in workers)


def test_a_crashing_contender_aborts_the_gate_instead_of_hanging(
    admin_app, monkeypatch
):
    def crash():
        raise RuntimeError('contender crashed')

    started = time.monotonic()
    with Race(admin_app, monkeypatch) as race:
        # Two parties, but only the waiter ever arrives: the crash of the
        # other contender must break the gate, not leave it to its timeout.
        gate = race.gate(2)
        waiting = race.start('waiting', lambda: 'never', gate=gate)
        crashing = race.start('crashing', crash)
        with pytest.raises(pytest.fail.Exception, match='crashed'):
            race.join(crashing)
        with pytest.raises(pytest.fail.Exception, match='crashed'):
            race.join(waiting)
    assert isinstance(crashing.error, RuntimeError)
    assert isinstance(waiting.error, threading.BrokenBarrierError)
    assert gate.broken
    assert time.monotonic() - started < GATE_SECONDS


def test_a_hanging_contender_is_reported_with_diagnostics(
    admin_app, monkeypatch
):
    release = threading.Event()
    try:
        with Race(admin_app, monkeypatch, join_seconds=2) as race:
            hanging = race.start('hanging', lambda: release.wait(30))
            with pytest.raises(pytest.fail.Exception) as report:
                race.join(hanging)
            # Let it end before the exit joins, so exit itself stays quiet.
            release.set()
    finally:
        release.set()
    message = str(report.value)
    assert 'hanging did not finish within 2 s' in message
    assert 'hanging: running' in message
    assert hanging.finished.is_set() and hanging.error is None
