from datetime import datetime, timedelta, UTC
from threading import current_thread, Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

from flask import current_app
import pytest
from sqlalchemy import delete, event, text
from sqlalchemy.exc import OperationalError

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_invitation_service as invitations,
    tournament_repository as repo,
    tournament_service as lifecycle,
    tournament_team_service as teams,
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
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


PARTY_ID = PartyID('f03-team-cleanup-atomicity')

UNKNOWN_TEAM = 'Unknown team.'
CAPTAIN_LEAVE_ERROR = (
    'Team captain cannot leave while team has other members. '
    'Transfer captain role first or have other members leave.'
)

SIGNALS = (
    'team_created',
    'team_deleted',
    'team_member_joined',
    'team_member_left',
    'captain_transferred',
    'participant_joined',
    'participant_left',
    'contestant_advanced',
    'match_confirmed',
    'tournament_completed',
)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 team cleanup atomicity')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'TeamCleanup{i}-{uuid7().hex[-8:]}') for i in range(4)]


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


def _purge(tournament_id):
    db.session.rollback()
    if repo.find_tournament(tournament_id) is not None:
        lifecycle.delete_tournament(tournament_id)
    for model in (DbMatchInvitation, DbMatchPairing, DbTournamentLogEntry):
        db.session.execute(
            delete(model).where(model.tournament_id == tournament_id)
        )
    db.session.commit()


@pytest.fixture
def sends(monkeypatch):
    """Replace every signal with a recorder, and stop invitation jobs."""
    recorded = {}
    for name in SIGNALS:
        recorded[name] = Mock()
        monkeypatch.setattr(getattr(signals, name), 'send', recorded[name])
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', Mock())
    return recorded


@pytest.fixture
def build_world(party, users, sends):
    """Two teams of one match; the first team is the one under test.

    The participants are the four users in index order. The members of the
    first team are chosen by index; the first user always holds its
    captaincy, whether they sit on its roster or not. The second team
    holds the last participant.
    """
    created = []

    def build(*, status='REGISTRATION_OPEN', members=(0, 1), winner=True):
        now = datetime.now(UTC).replace(tzinfo=None)
        tournament = DbTournament(
            uuid7(),
            party.id,
            f'Team cleanup {uuid7()}',
            now,
            contestant_type='TEAM',
            game_format='ONE_V_ONE',
            elimination_mode='SINGLE_ELIMINATION',
            tournament_status=status,
            max_players_in_team=8,
        )
        tournament_id = tournament.id
        created.append(tournament_id)
        db.session.add(tournament)
        db.session.flush()
        team_rows = [
            DbTournamentTeam(
                uuid7(),
                tournament_id,
                f'Cleanup team {i}',
                users[0 if i == 0 else 3].id,
                now,
            )
            for i in range(2)
        ]
        db.session.add_all(team_rows)
        db.session.flush()
        roster = [
            DbTournamentParticipant(
                uuid7(),
                user.id,
                tournament_id,
                now + timedelta(seconds=i),
                team_id=(
                    team_rows[0].id
                    if i in members
                    else team_rows[1].id
                    if i == 3
                    else None
                ),
            )
            for i, user in enumerate(users)
        ]
        db.session.add_all(roster)
        db.session.flush()
        match = DbTournamentMatch(
            uuid7(), tournament_id, now, match_order=0, round=0
        )
        match_id = match.id
        db.session.add(match)
        db.session.flush()
        for team in team_rows:
            db.session.add(
                DbTournamentMatchToContestant(
                    uuid7(), match_id, now, team_id=team.id
                )
            )
        db.session.flush()
        repo.get_tournament_for_update(tournament_id)
        repo.get_match_for_update(match_id)
        assert repo.refresh_match_pairing_flush(
            match_id, occurred_at=now
        ).is_ok()
        if winner:
            tournament.winner_team_id = team_rows[0].id
        db.session.commit()
        return SimpleNamespace(
            tournament_id=tournament_id,
            match_id=match_id,
            team_id=team_rows[0].id,
            other_team_id=team_rows[1].id,
            participant_ids=[p.id for p in roster],
            users=users,
        )

    yield build
    db.session.rollback()
    for tournament_id in created:
        _purge(tournament_id)


# -------------------------------------------------------------------- #
# observation: committed state, held locks, transactions
# -------------------------------------------------------------------- #


def _read(sql: str, **params):
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _durable(world):
    """Return everything committed that a team cleanup may touch."""
    return {
        'teams': _read(
            'SELECT id, removed_at FROM lan_tournament_teams'
            ' WHERE tournament_id = :id ORDER BY id',
            id=world.tournament_id,
        ),
        'participants': _read(
            'SELECT id, team_id, removed_at FROM lan_tournament_participants'
            ' WHERE tournament_id = :id ORDER BY id',
            id=world.tournament_id,
        ),
        'entries': _read(
            'SELECT id, team_id, participant_id'
            ' FROM lan_tournament_match_contestants'
            ' WHERE tournament_match_id = :id ORDER BY id',
            id=world.match_id,
        ),
        'tournament': _read(
            'SELECT winner_team_id, tournament_status FROM lan_tournaments'
            ' WHERE id = :id',
            id=world.tournament_id,
        ),
        'match': _read(
            'SELECT confirmed_by, readiness_revision, pairing_generation,'
            ' last_changed_at FROM lan_tournament_matches WHERE id = :id',
            id=world.match_id,
        ),
        'audit': [
            event_type
            for (event_type,) in _read(
                'SELECT event_type FROM lan_tournament_log_entries'
                ' WHERE tournament_id = :id ORDER BY id',
                id=world.tournament_id,
            )
        ],
    }


def _members(world, team_id=None):
    team_id = team_id or world.team_id
    return {
        row.id
        for row in _read(
            'SELECT id FROM lan_tournament_participants'
            ' WHERE team_id = :team AND removed_at IS NULL',
            team=team_id,
        )
    }


def _team_is_gone(world) -> bool:
    """Tell if the team row is deleted or soft-deleted."""
    rows = _read(
        'SELECT removed_at FROM lan_tournament_teams WHERE id = :id',
        id=world.team_id,
    )
    return not rows or rows[0].removed_at is not None


def _cleanup_is_durable(world) -> bool:
    """Tell if the team, its membership and its match entries are gone."""
    [(entries,)] = _read(
        'SELECT count(*) FROM lan_tournament_match_contestants'
        ' WHERE team_id = :id',
        id=world.team_id,
    )
    return _team_is_gone(world) and not _members(world) and entries == 0


def _assert_intact(world, *, members, winner=True):
    assert not _team_is_gone(world)
    assert _members(world) == {world.participant_ids[i] for i in members}
    [(entries,)] = _read(
        'SELECT count(*) FROM lan_tournament_match_contestants'
        ' WHERE team_id = :id',
        id=world.team_id,
    )
    assert entries == 1
    [(won, _status)] = _read(
        'SELECT winner_team_id, tournament_status FROM lan_tournaments'
        ' WHERE id = :id',
        id=world.tournament_id,
    )
    assert (won == world.team_id) is winner


_LOCKS = {
    'tournament': 'SELECT id FROM lan_tournaments WHERE id = :id'
    ' FOR UPDATE NOWAIT',
    'team': 'SELECT id FROM lan_tournament_teams WHERE id = :id'
    ' FOR UPDATE NOWAIT',
}


def _is_locked(table: str, row_id) -> bool:
    """Tell if another transaction holds the row lock."""
    with db.engine.connect() as connection:
        try:
            connection.execute(text(_LOCKS[table]), {'id': row_id})
        except OperationalError:
            return True
        finally:
            connection.rollback()
    return False


class _Transactions:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def reset(self):
        self.commits = 0
        self.rollbacks = 0


@pytest.fixture
def transactions():
    """Count the commits and rollbacks of this thread's session."""
    session = db.session()
    counts = _Transactions()

    def on_commit(_):
        counts.commits += 1

    def on_rollback(_):
        counts.rollbacks += 1

    event.listen(session, 'after_commit', on_commit)
    event.listen(session, 'after_rollback', on_rollback)
    yield counts
    event.remove(session, 'after_commit', on_commit)
    event.remove(session, 'after_rollback', on_rollback)


def _spy(monkeypatch, target, name, log=None):
    """Record calls to a function and run the real one."""
    real = getattr(target, name)
    calls = [] if log is None else log

    def spy(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, spy)
    return calls


# -------------------------------------------------------------------- #
# the operations that can empty a team
# -------------------------------------------------------------------- #


def _delete_team(world):
    return teams.delete_team(world.team_id)


def _leave_team(world):
    return teams.leave_team(world.participant_ids[0])


def _remove_member(world):
    return teams.remove_team_member(world.team_id, world.users[1].id)


# Each operation, the world it starts from and the signals that report it.
# fmt: off
OPERATIONS = {
    'delete_team': dict(
        call=_delete_team, status='REGISTRATION_OPEN', members=(0, 1),
        signals=('team_deleted',),
        last_write='delete_team_flush',
    ),
    'leave_team': dict(
        call=_leave_team, status='REGISTRATION_OPEN', members=(0,),
        signals=('team_member_left',),
        last_write='delete_team_flush',
    ),
    'remove_team_member_before_start': dict(
        call=_remove_member, status='REGISTRATION_OPEN', members=(1,),
        signals=('team_member_left', 'team_deleted'),
        last_write='delete_team_flush',
    ),
    'remove_team_member_while_ongoing': dict(
        call=_remove_member, status='ONGOING', members=(1,),
        signals=('team_member_left', 'team_deleted'),
        last_write='soft_delete_team_flush',
    ),
}
# fmt: on


def _world_for(build_world, spec, transactions):
    ongoing = spec['status'] == 'ONGOING'
    world = build_world(
        status=spec['status'], members=spec['members'], winner=not ongoing
    )
    _assert_intact(world, members=spec['members'], winner=not ongoing)
    transactions.reset()
    return world


# -------------------------------------------------------------------- #
# cleanup and event: one commit, then the signals
# -------------------------------------------------------------------- #


@pytest.mark.parametrize('operation', OPERATIONS)
def test_empty_team_cleanup_commits_before_signals(
    build_world, sends, transactions, monkeypatch, operation
):
    spec = OPERATIONS[operation]
    world = _world_for(build_world, spec, transactions)
    owner_commits = _spy(monkeypatch, repo, 'commit_session')
    observed = []

    def record(name):
        def on_send(*args, **kwargs):
            # Read through another connection: durable means committed.
            observed.append(
                (
                    name,
                    transactions.commits,
                    len(owner_commits),
                    _cleanup_is_durable(world),
                )
            )

        return on_send

    for name, mock in sends.items():
        mock.side_effect = record(name)

    result = spec['call'](world)

    assert result.is_ok(), result
    assert {name for name, *_ in observed} >= set(spec['signals'])
    # Whatever signal fired, the single commit had already made the whole
    # cleanup durable.
    assert all(commits == 1 for _, commits, _, _ in observed), observed
    assert all(owned == 1 for _, _, owned, _ in observed), observed
    assert all(durable for *_, durable in observed), observed
    assert len(owner_commits) == 1
    assert transactions.rollbacks == 0
    if spec['status'] != 'ONGOING':
        [(winner,)] = _read(
            'SELECT winner_team_id FROM lan_tournaments WHERE id = :id',
            id=world.tournament_id,
        )
        assert winner is None
    assert not _is_locked('tournament', world.tournament_id)


# -------------------------------------------------------------------- #
# a failure restores membership, match entries, winner and team
# -------------------------------------------------------------------- #


def _fail_late(monkeypatch, spec, failure):
    def boom(*args, **kwargs):
        raise RuntimeError('cleanup failed')

    if failure == 'team_row':
        # Membership and match entries are already flushed here.
        monkeypatch.setattr(repo, spec['last_write'], boom)
    else:
        # Everything is flushed; only the commit is still ahead.
        monkeypatch.setattr(teams, '_refresh_roster_matches_flush', boom)


@pytest.mark.parametrize('failure', ['team_row', 'refresh'])
@pytest.mark.parametrize('operation', OPERATIONS)
def test_cleanup_failure_restores_membership_and_match_entries(
    build_world, sends, transactions, monkeypatch, operation, failure
):
    spec = OPERATIONS[operation]
    world = _world_for(build_world, spec, transactions)
    before = _durable(world)
    rollbacks = _spy(monkeypatch, repo, 'rollback_session')
    commits = _spy(monkeypatch, repo, 'commit_session')
    _fail_late(monkeypatch, spec, failure)

    with pytest.raises(RuntimeError, match='cleanup failed'):
        spec['call'](world)

    # The public operation rolled back once and never committed.
    assert rollbacks == ['rollback_session']
    assert commits == []
    assert transactions.commits == 0
    # A fresh connection sees none of the staged cleanup.
    assert _durable(world) == before
    assert not _is_locked('tournament', world.tournament_id)
    assert not _is_locked('team', world.team_id)
    for mock in sends.values():
        mock.assert_not_called()


# -------------------------------------------------------------------- #
# lock order, read from the statements the database receives
# -------------------------------------------------------------------- #


def _join_team(world):
    return teams.join_team(world.participant_ids[2], world.team_id)


def _transfer_captain(world):
    return teams.transfer_captain(world.team_id, world.users[1].id)


def _add_member(world):
    return teams.admin_add_member(world.team_id, world.users[2].id)


# fmt: off
LOCK_ORDER_OPERATIONS = {
    'join_team': dict(call=_join_team, members=(0,)),
    'admin_add_member': dict(call=_add_member, members=(0,)),
    'transfer_captain': dict(call=_transfer_captain, members=(0, 1)),
    **{
        name: dict(call=spec['call'], members=spec['members'],
                   status=spec['status'])
        for name, spec in OPERATIONS.items()
    },
}
# fmt: on


def _lock_kind(statement: str) -> str | None:
    text_ = ' '.join(statement.lower().split())
    if ' for update' in text_:
        if 'from lan_tournaments ' in text_:
            return 'tournament'
        if 'from lan_tournament_teams ' in text_:
            return 'team'
    if text_.startswith(
        ('delete from lan_tournament_teams ', 'update lan_tournament_teams ')
    ):
        return 'team'
    return None


@pytest.fixture
def statements():
    """Record the SQL this thread sends, in order."""
    sent = []
    thread = current_thread()

    def on_execute(connection, cursor, statement, *args):
        if current_thread() is thread:
            sent.append(statement)

    event.listen(db.engine, 'before_cursor_execute', on_execute)
    yield sent
    event.remove(db.engine, 'before_cursor_execute', on_execute)


@pytest.mark.parametrize('operation', LOCK_ORDER_OPERATIONS)
def test_team_cleanup_does_not_introduce_lock_cycle(
    build_world, statements, operation
):
    spec = LOCK_ORDER_OPERATIONS[operation]
    ongoing = spec.get('status') == 'ONGOING'
    world = build_world(
        status=spec.get('status', 'REGISTRATION_OPEN'),
        members=spec['members'],
        winner=not ongoing,
    )
    del statements[:]

    result = spec['call'](world)

    assert result.is_ok(), result
    kinds = [_lock_kind(statement) for statement in statements]
    assert 'tournament' in kinds, statements
    assert 'team' in kinds, statements
    # No team lock and no team write, explicit or from DML, comes earlier.
    assert kinds.index('tournament') < kinds.index('team'), [
        kind for kind in kinds if kind
    ]


# -------------------------------------------------------------------- #
# join against the cleanup of the last member
# -------------------------------------------------------------------- #


class _Worker:
    """Run a call in a thread with its own app context, so its own session."""

    def __init__(self, name, call):
        self.pid = None
        self.result = None
        self.error = None
        self._started = Event()
        self._thread = Thread(
            target=self._run,
            args=(current_app._get_current_object(), call),
            name=name,
            daemon=True,
        )
        self._thread.start()
        assert self._started.wait(10), 'the worker never started'

    def _run(self, app, call):
        try:
            with app.app_context():
                self.pid = db.session.execute(
                    text('SELECT pg_backend_pid()')
                ).scalar_one()
                self._started.set()
                self.result = call()
                db.session.rollback()
        except BaseException as exc:
            self.error = exc
        finally:
            self._started.set()

    @property
    def alive(self):
        return self._thread.is_alive()

    def wait(self):
        self._thread.join(timeout=20)

    def finish(self):
        self.wait()
        assert not self._thread.is_alive(), 'the worker never finished'
        if self.error is not None:
            raise self.error
        return self.result


class _Gate:
    """Park one named thread at its first call of a function."""

    def __init__(self, thread_name):
        self.thread_name = thread_name
        self.entered = Event()
        self.proceed = Event()
        self._parked = False

    def wrap(self, real):
        def gated(*args, **kwargs):
            if current_thread().name == self.thread_name and not self._parked:
                self._parked = True
                self.entered.set()
                assert self.proceed.wait(20), 'the gate was never opened'
            return real(*args, **kwargs)

        return gated


def _wait_until_waiting(waiter, blocker) -> str:
    """Poll `pg_locks` until the waiter's backend waits on the blocker.

    Return the statement the waiter is stuck on.
    """
    sql = text(
        'SELECT EXISTS (SELECT 1 FROM pg_locks'
        '  WHERE pid = :waiter AND NOT granted),'
        ' :blocker = ANY (pg_blocking_pids(:waiter)),'
        ' (SELECT query FROM pg_stat_activity WHERE pid = :waiter)'
    )
    with db.engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as connection:
        deadline = monotonic() + 10
        while monotonic() < deadline:
            ungranted, blocked_by, query = connection.execute(
                sql, {'waiter': waiter.pid, 'blocker': blocker.pid}
            ).one()
            if ungranted and blocked_by:
                return query
            assert waiter.alive or waiter.error is None, waiter.error
            sleep(0.02)
    pytest.fail('the worker never waited on a lock held by the other')


def _assert_no_deadlock(*workers):
    for worker in workers:
        error = worker.error
        assert not (
            isinstance(error, OperationalError)
            and getattr(error.orig, 'sqlstate', None) == '40P01'
        ), f'PostgreSQL aborted a worker as a deadlock victim: {error}'


# fmt: off
CLEANUPS = {
    'delete_team': lambda world: teams.delete_team(world.team_id),
    'leave_team': lambda world: teams.leave_team(world.participant_ids[0]),
}
SCENARIOS = [
    pytest.param('cleanup', 'get_team_for_update', id='cleanup parks before its team lock'),
    pytest.param('cleanup', 'delete_team_flush', id='cleanup parks before its team write'),
    pytest.param('join', '_lock_roster_matches_flush', id='join parks holding both locks'),
]
# fmt: on


@pytest.mark.parametrize('cleanup', CLEANUPS)
@pytest.mark.parametrize('parked, where', SCENARIOS)
def test_join_versus_last_member_cleanup_cannot_deadlock(
    build_world, monkeypatch, cleanup, parked, where
):
    world = build_world(members=(0,))
    cleanup_call = CLEANUPS[cleanup]
    join_call = _join_team
    gate = _Gate(parked)
    target = teams if where == '_lock_roster_matches_flush' else repo
    monkeypatch.setattr(target, where, gate.wrap(getattr(target, where)))
    workers = []

    try:
        if parked == 'cleanup':
            # The cleanup holds the tournament row; the join must queue
            # behind it holding no team lock of its own.
            first = _Worker('cleanup', lambda: cleanup_call(world))
            workers.append(first)
            assert gate.entered.wait(10), 'the cleanup never reached its gate'
            second = _Worker('join', lambda: join_call(world))
            workers.append(second)
            waiting_on = _wait_until_waiting(second, first)
            assert 'lan_tournaments' in waiting_on
            assert 'lan_tournament_teams' not in waiting_on
            if where == 'get_team_for_update':
                assert not _is_locked('team', world.team_id), (
                    'the waiting join already holds the team row'
                )
        else:
            # The join holds both rows; the cleanup queues on the tournament.
            first = _Worker('join', lambda: join_call(world))
            workers.append(first)
            assert gate.entered.wait(10), 'the join never reached its gate'
            second = _Worker('cleanup', lambda: cleanup_call(world))
            workers.append(second)
            waiting_on = _wait_until_waiting(second, first)
            assert 'lan_tournaments' in waiting_on
            assert 'lan_tournament_teams' not in waiting_on
        gate.proceed.set()
        for worker in workers:
            worker.wait()
        _assert_no_deadlock(*workers)
    finally:
        gate.proceed.set()
        for worker in workers:
            worker.wait()

    cleanup_worker = first if parked == 'cleanup' else second
    join_worker = second if parked == 'cleanup' else first
    assert cleanup_worker.error is None, cleanup_worker.error
    assert join_worker.error is None, join_worker.error
    joiner = world.participant_ids[2]
    [(joiner_team,)] = _read(
        'SELECT team_id FROM lan_tournament_participants WHERE id = :id',
        id=joiner,
    )
    if parked == 'cleanup':
        # The cleanup won; the team is gone and the join is refused.
        assert cleanup_worker.result.is_ok(), cleanup_worker.result
        assert join_worker.result.unwrap_err() == UNKNOWN_TEAM
        assert _team_is_gone(world)
        assert joiner_team is None
        assert not _members(world)
    elif cleanup == 'delete_team':
        # The join won, then the delete took the new member off the team.
        assert join_worker.result.is_ok(), join_worker.result
        assert cleanup_worker.result.is_ok(), cleanup_worker.result
        assert _team_is_gone(world)
        assert joiner_team is None
        assert not _members(world)
    else:
        # The join won; the captain can no longer leave an occupied team.
        assert join_worker.result.is_ok(), join_worker.result
        assert cleanup_worker.result.unwrap_err() == CAPTAIN_LEAVE_ERROR
        assert not _team_is_gone(world)
        assert _members(world) == {
            world.participant_ids[0],
            joiner,
        }
    assert not _is_locked('tournament', world.tournament_id)
    assert not _is_locked('team', world.team_id)
