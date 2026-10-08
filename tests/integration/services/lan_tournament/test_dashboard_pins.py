from dataclasses import replace
from datetime import datetime, timedelta
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    signals,
    tournament_dashboard_coordination_service as service,
    tournament_dashboard_service as dashboard_service,
    tournament_log_service,
    tournament_orga_service as orgas,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 8, 12, 0, 0)
MINUTE_US = 60_000_000
CLOCK_START = NOW - timedelta(hours=3)
CLOCK_AT_NOW_US = 180 * MINUTE_US
LAST_CHANGED = datetime(2026, 1, 15, 8, 0, 0)

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

ONGOING = TournamentStatus.ONGOING

# Server-side and client-side bounds of the races, in seconds.
LOCK_TIMEOUT = '8s'
STATEMENT_TIMEOUT = '12s'
PARK_SECONDS = 15
BLOCK_SECONDS = 10
JOIN_SECONDS = 15


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03Pin{i:02d}') for i in range(8)]


@pytest.fixture(scope='module')
def confirmer(make_user):
    return make_user('F03PinConfirmer')


@pytest.fixture(scope='module')
def admin(make_admin):
    return make_admin(
        {'lan_tournament.administrate'}, screen_name='F03PinAdmin'
    )


@pytest.fixture(autouse=True)
def _context(admin_app):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


def _viewer(user, *claimed: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(claimed))


class Scene:
    """Builds committed tournaments of one party, with matches and orgas."""

    def __init__(self, party_id: PartyID, confirmer) -> None:
        self.party_id = party_id
        self.confirmer = confirmer
        self._joined: dict[tuple, TournamentParticipantID] = {}
        self._orders = iter(range(1, 10_000))

    def tournament(
        self, *, status: TournamentStatus = ONGOING, name: str | None = None
    ) -> TournamentID:
        tournament_id = TournamentID(uuid7())
        running = status is ONGOING
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=self.party_id,
                name=name or f'Pins {tournament_id}',
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
                tournament_status=status,
                game_format=GameFormat.ONE_V_ONE,
                elimination_mode=EliminationMode.SINGLE_ELIMINATION,
                group_size_max=None,
                operational_clock_elapsed_us=(
                    0 if running else CLOCK_AT_NOW_US
                ),
                operational_clock_running_since=(
                    CLOCK_START if running else None
                ),
                operational_clock_activated_at=CLOCK_START,
            )
        )
        db.session.commit()
        return tournament_id

    def assign(self, tournament_id: TournamentID, user) -> None:
        db.session.add(
            DbTournamentOrga(
                TournamentOrgaID(uuid7()), tournament_id, user.id, NOW
            )
        )
        db.session.commit()

    def duel(
        self,
        tournament_id: TournamentID,
        player_a,
        player_b,
        *,
        confirmed: bool = False,
        wait_minutes: float | None = 10,
    ) -> TournamentMatchID:
        """Create a match of two people; with `wait_minutes` it is due."""
        match_id = TournamentMatchID(uuid7())
        db.session.add(
            DbTournamentMatch(
                match_id,
                tournament_id,
                NOW - timedelta(hours=1),
                match_order=next(self._orders),
                round=1,
                confirmed_by=self.confirmer.id if confirmed else None,
                phase=1,
                occupied_since=NOW - timedelta(minutes=30),
                last_changed_at=LAST_CHANGED,
            )
        )
        db.session.flush()
        for user in (player_a, player_b):
            db.session.add(
                DbTournamentMatchToContestant(
                    TournamentMatchToContestantID(uuid7()),
                    match_id,
                    NOW - timedelta(hours=1),
                    participant_id=self._join(tournament_id, user),
                )
            )
        db.session.flush()
        if wait_minutes is not None:
            repo.open_due_episode_flush(
                MatchDueEpisode(
                    id=MatchDueEpisodeID(uuid7()),
                    tournament_id=tournament_id,
                    match_id=match_id,
                    pairing_key='key',
                    opened_at=NOW - timedelta(minutes=wait_minutes),
                    opened_clock_us=CLOCK_AT_NOW_US
                    - int(wait_minutes * MINUTE_US),
                )
            )
        db.session.commit()
        return match_id

    def acknowledge(self, match_id: TournamentMatchID, tournament_id) -> None:
        """Append one acknowledgement to the open episode of the match."""
        (episode,) = repo.list_open_due_episodes(tournament_id)
        assert episode.match_id == match_id
        repo.create_escalation_ack_flush(
            MatchEscalationAcknowledgement(
                id=MatchEscalationAcknowledgementID(uuid7()),
                episode_id=episode.id,
                tournament_id=tournament_id,
                match_id=match_id,
                revision=1,
                occurred_at=NOW - timedelta(minutes=5),
                clock_us=CLOCK_AT_NOW_US - 5 * MINUTE_US,
                actor_id=self.confirmer.id,
                comment='Captains called',
            )
        )
        db.session.commit()

    def _join(self, tournament_id: TournamentID, user):
        key = (tournament_id, user.id)
        if key not in self._joined:
            participant = DbTournamentParticipant(
                TournamentParticipantID(uuid7()), user.id, tournament_id, NOW
            )
            db.session.add(participant)
            db.session.commit()
            self._joined[key] = participant.id
        return self._joined[key]


@pytest.fixture
def make_scene(make_party, brand, confirmer):
    """Provide a factory of scenes, each in a party of its own."""

    def _make() -> Scene:
        party_id = PartyID(f'f03p-{uuid4().hex[:12]}')
        make_party(brand, party_id, f'F03 pins {party_id}')
        return Scene(party_id, confirmer)

    return _make


class Duel:
    """One due match of a tournament with two orgas, in a party of its own."""

    def __init__(self, make_scene, users, admin) -> None:
        self.scene = make_scene()
        self.party_id = self.scene.party_id
        self.tournament_id = self.scene.tournament()
        self.ada, self.bob = users[0], users[1]
        self.scene.assign(self.tournament_id, self.ada)
        self.scene.assign(self.tournament_id, self.bob)
        self.match_id = self.scene.duel(self.tournament_id, users[2], users[3])
        self.users = users
        self.admin = admin

    def pin(self, viewer, *, pinned=True, expected=0, match_id=None):
        return service.set_match_pin(
            _as_viewer(viewer),
            self.party_id,
            self.match_id if match_id is None else match_id,
            pinned=pinned,
            expected_revision=expected,
        )

    def page(self, viewer):
        result = dashboard_service.get_dashboard_page(
            _as_viewer(viewer),
            self.party_id,
            DashboardQuery(scope='all', view='all', per_page=50),
            settings=SETTINGS,
            now=NOW,
        )
        return result.unwrap()

    def row(self, viewer):
        (row,) = [
            row
            for row in self.page(viewer).rows
            if row.match_id == self.match_id
        ]
        return row


@pytest.fixture
def duel(make_scene, users, admin):
    return Duel(make_scene, users, admin)


def _as_viewer(user_or_viewer) -> CurrentUser:
    if isinstance(user_or_viewer, CurrentUser):
        return user_or_viewer

    return _viewer(user_or_viewer)


def _admin_viewer(admin) -> CurrentUser:
    return _viewer(admin, 'lan_tournament.administrate')


# -- raw reads through a connection of their own: only what is committed --


def _read(sql: str, **params) -> list[dict]:
    with db.engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _execute(sql: str, **params) -> None:
    with db.engine.begin() as connection:
        connection.execute(text(sql), params)


def _pin_rows(match_id) -> list[dict]:
    return _read(
        'SELECT * FROM lan_tournament_match_dashboard_annotations'
        ' WHERE match_id = :id',
        id=match_id,
    )


def _pin_audit(tournament_id) -> list[tuple]:
    return [
        (entry.event_type, entry.initiator_id, entry.data)
        for entry in tournament_log_service.get_entries_for_tournament(
            tournament_id
        )
        if entry.event_type
        in (service.MATCH_PINNED_EVENT, service.MATCH_UNPINNED_EVENT)
    ]


def _operational_facts(match_id, tournament_id) -> dict:
    """Everything a pin must leave alone, as committed."""
    return {
        'match': _read(
            'SELECT * FROM lan_tournament_matches WHERE id = :id',
            id=match_id,
        ),
        'tournament': _read(
            'SELECT tournament_status, operational_clock_elapsed_us,'
            ' operational_clock_running_since,'
            ' operational_clock_activated_at'
            ' FROM lan_tournaments WHERE id = :id',
            id=tournament_id,
        ),
        'episodes': _read(
            'SELECT * FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :id ORDER BY id',
            id=match_id,
        ),
        'acknowledgements': _read(
            'SELECT * FROM lan_tournament_match_escalation_acks'
            ' WHERE match_id = :id ORDER BY id',
            id=match_id,
        ),
    }


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


def _without_pin(row):
    return replace(row, pin_revision=0, pinned_at=None, pinned_by_name=None)


# -- the five named tests --


def test_shared_pin_survives_reload_and_unpin(duel):
    ada, bob, admin = duel.ada, duel.bob, duel.admin
    admin_viewer = _admin_viewer(admin)

    pinned = duel.pin(ada, expected=0).unwrap()

    assert (pinned.revision, pinned.pinned_by, pinned.updated_by) == (
        1,
        ada.id,
        ada.id,
    )
    # Committed: a connection of its own reads the pin, and the other
    # orga sees whom it belongs to on the page.
    (stored,) = _pin_rows(duel.match_id)
    assert stored['revision'] == 1
    assert stored['pinned_by'] == ada.id
    assert stored['pinned_at'] is not None
    assert stored['tournament_id'] == duel.tournament_id
    row = duel.row(bob)
    assert (row.pin_revision, row.pinned_by_name) == (1, ada.screen_name)
    assert row.pinned_at == stored['pinned_at']

    # Any authorized orga takes it back, at the revision they saw.
    unpinned = duel.pin(bob, pinned=False, expected=1).unwrap()

    assert (unpinned.revision, unpinned.pinned_at, unpinned.pinned_by) == (
        2,
        None,
        None,
    )
    (stored,) = _pin_rows(duel.match_id)
    assert (stored['revision'], stored['pinned_at'], stored['pinned_by']) == (
        2,
        None,
        None,
    )
    row = duel.row(ada)
    assert (row.pin_revision, row.pinned_at, row.pinned_by_name) == (
        2,
        None,
        None,
    )

    # A global administrator pins it again; the old row is reused.
    assert duel.pin(admin_viewer, expected=2).unwrap().revision == 3
    row = duel.row(admin_viewer)
    assert (row.pin_revision, row.pinned_by_name) == (3, admin.screen_name)
    assert len(_pin_rows(duel.match_id)) == 1

    assert [
        (event, initiator)
        for event, initiator, _ in _pin_audit(duel.tournament_id)
    ] == [
        ('match-pinned', ada.id),
        ('match-unpinned', bob.id),
        ('match-pinned', admin.id),
    ]
    assert [data for _, _, data in _pin_audit(duel.tournament_id)] == [
        {'match_id': str(duel.match_id), 'revision': revision}
        for revision in (1, 2, 3)
    ]


def test_pin_cas_and_noop_do_not_duplicate(duel, monkeypatch):
    ada, bob = duel.ada, duel.bob

    # Nothing to unpin: no row, no audit entry.
    assert duel.pin(ada, pinned=False, expected=0).unwrap() is None
    assert _pin_rows(duel.match_id) == []
    assert _pin_audit(duel.tournament_id) == []

    first = duel.pin(ada, expected=0).unwrap()
    (before,) = _pin_rows(duel.match_id)

    # The same request again, with the revision it was built from.
    duplicate = duel.pin(bob, expected=0)
    assert duplicate.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    # A request that is stale or invented never writes.
    for expected in (-1, 2, 10**9):
        result = duel.pin(bob, pinned=False, expected=expected)
        assert result.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    assert _pin_rows(duel.match_id) == [before]

    # Pinned already, at the revision it has: nothing changes, down to
    # the timestamps, and the caller still learns the current state.
    noop = duel.pin(bob, expected=1).unwrap()
    assert noop == first
    assert _pin_rows(duel.match_id) == [before]
    assert len(_pin_audit(duel.tournament_id)) == 1

    # Unpinned already, at its revision: the same.
    duel.pin(bob, pinned=False, expected=1).unwrap()
    (unpinned,) = _pin_rows(duel.match_id)
    assert unpinned['revision'] == 2
    assert duel.pin(ada, pinned=False, expected=2).unwrap().revision == 2
    assert _pin_rows(duel.match_id) == [unpinned]
    assert [event for event, _, _ in _pin_audit(duel.tournament_id)] == [
        'match-pinned',
        'match-unpinned',
    ]

    # Someone else got in between the read and the write: first an
    # insert, then a revision bump. Neither request writes, audits or
    # leaves a lock behind.
    other_tournament = duel.scene.tournament()
    duel.scene.assign(other_tournament, ada)
    other_match = duel.scene.duel(
        other_tournament, duel.users[4], duel.users[5]
    )
    real_find = repo.find_match_pin_state
    competitor = []

    def read_then_lose(match_id):
        state = real_find(match_id)
        competitor.pop()()
        return state

    monkeypatch.setattr(repo, 'find_match_pin_state', read_then_lose)

    competitor.append(
        lambda: _execute(
            'INSERT INTO lan_tournament_match_dashboard_annotations'
            ' (match_id, tournament_id, revision, pinned_at, pinned_by,'
            '  updated_at, updated_by)'
            ' VALUES (:m, :t, 1, :at, :by, :at, :by)',
            m=other_match,
            t=other_tournament,
            at=NOW,
            by=bob.id,
        )
    )
    inserted = duel.pin(ada, expected=0, match_id=other_match)

    assert inserted.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    (winner,) = _pin_rows(other_match)
    assert (winner['revision'], winner['pinned_by']) == (1, bob.id)
    assert _pin_audit(other_tournament) == []
    _assert_tournament_unlocked(other_tournament)

    competitor.append(
        lambda: _execute(
            'UPDATE lan_tournament_match_dashboard_annotations'
            ' SET revision = 2, pinned_at = NULL, pinned_by = NULL'
            ' WHERE match_id = :m',
            m=other_match,
        )
    )
    bumped = duel.pin(ada, pinned=False, expected=1, match_id=other_match)

    assert bumped.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    (winner,) = _pin_rows(other_match)
    assert (winner['revision'], winner['pinned_at']) == (2, None)
    assert _pin_audit(other_tournament) == []
    _assert_tournament_unlocked(other_tournament)


def test_pin_cannot_change_match_clock_or_last_change(duel):
    ada, bob = duel.ada, duel.bob
    duel.scene.acknowledge(duel.match_id, duel.tournament_id)
    facts = _operational_facts(duel.match_id, duel.tournament_id)
    assert facts['match'][0]['last_changed_at'] == LAST_CHANGED
    assert len(facts['episodes']) == 1
    assert len(facts['acknowledgements']) == 1
    page_before = _without_pin(duel.row(ada))
    heard = Mock()
    receivers = [
        signal
        for signal in vars(signals).values()
        if hasattr(signal, 'connect') and hasattr(signal, 'send')
    ]
    assert receivers, 'no signal to listen to'

    try:
        for signal in receivers:
            signal.connect(heard, weak=False)
        duel.pin(ada, expected=0).unwrap()
        duel.pin(ada, expected=1).unwrap()  # no-op
        assert duel.pin(bob, expected=0).is_err()  # duplicate
        duel.pin(bob, pinned=False, expected=1).unwrap()
        duel.pin(ada, expected=2).unwrap()
    finally:
        for signal in receivers:
            signal.disconnect(heard)

    # Match, clock, episode and acknowledgement are what they were, and
    # so is every derived wait, tier and last change on the page. The
    # pin is the one visible difference, and it moves no row.
    assert _operational_facts(duel.match_id, duel.tournament_id) == facts
    page_after = duel.row(bob)
    assert page_after.pin_revision == 3
    assert page_after.pinned_at is not None
    assert _without_pin(page_after) == page_before
    assert page_after.last_changed_at == LAST_CHANGED
    # No notification, no demand and no result went out.
    heard.assert_not_called()


def test_pin_audit_failure_rolls_back(duel, monkeypatch):
    ada = duel.ada
    saved = []
    real_save = repo.save_match_pin_flush

    def spy_save(*args, **kwargs):
        state = real_save(*args, **kwargs)
        saved.append(state)
        return state

    monkeypatch.setattr(repo, 'save_match_pin_flush', spy_save)
    rollbacks = []
    real_rollback = repo.rollback_session

    def spy_rollback():
        rollbacks.append(True)
        real_rollback()

    monkeypatch.setattr(repo, 'rollback_session', spy_rollback)

    def failing_audit(*args, **kwargs):
        raise RuntimeError('audit unavailable')

    real_log = service.tournament_log_service
    monkeypatch.setattr(
        service,
        'tournament_log_service',
        SimpleNamespace(create_log_entry=failing_audit),
    )

    # A new pin was written inside the transaction, and is gone.
    with pytest.raises(RuntimeError, match='audit unavailable'):
        duel.pin(ada, expected=0)

    assert [state.revision for state in saved] == [1]
    assert rollbacks == [True]
    assert _pin_rows(duel.match_id) == []
    assert _pin_audit(duel.tournament_id) == []
    _assert_tournament_unlocked(duel.tournament_id)

    # A change of an existing pin goes back to the pin that was.
    monkeypatch.setattr(service, 'tournament_log_service', real_log)
    duel.pin(ada, expected=0).unwrap()
    (before,) = _pin_rows(duel.match_id)
    monkeypatch.setattr(
        service,
        'tournament_log_service',
        SimpleNamespace(create_log_entry=failing_audit),
    )
    saved.clear()
    rollbacks.clear()

    with pytest.raises(RuntimeError, match='audit unavailable'):
        duel.pin(ada, pinned=False, expected=1)

    assert [state.revision for state in saved] == [2]
    assert rollbacks == [True]
    assert _pin_rows(duel.match_id) == [before]
    assert len(_pin_audit(duel.tournament_id)) == 1

    # The audit entry is staged with the pin and not before the commit:
    # when that commit fails, neither the pin nor its entry is kept.
    monkeypatch.setattr(service, 'tournament_log_service', real_log)

    def failing_commit():
        raise RuntimeError('commit failed')

    monkeypatch.setattr(repo, 'commit_session', failing_commit)
    saved.clear()
    rollbacks.clear()

    with pytest.raises(RuntimeError, match='commit failed'):
        duel.pin(ada, pinned=False, expected=1)

    assert [state.revision for state in saved] == [2]
    assert rollbacks == [True]
    assert _pin_rows(duel.match_id) == [before]
    assert len(_pin_audit(duel.tournament_id)) == 1
    _assert_tournament_unlocked(duel.tournament_id)


def test_revoke_orga_takes_tournament_lock_first(duel, admin_app):
    ada, bob = duel.ada, duel.bob
    with db.engine.connect() as holder:
        holder.execute(
            text('SELECT id FROM lan_tournaments WHERE id = :id FOR UPDATE'),
            {'id': duel.tournament_id},
        )
        holder_pid = holder.scalar(text('SELECT pg_backend_pid()'))
        worker = Worker(
            admin_app,
            lambda: orgas.revoke_orga(duel.tournament_id, ada.id, bob.id),
        )
        worker.start()
        try:
            _wait_until_blocked(holder, worker, holder_pid)

            # The first and only statement it got to is the lock: it has
            # neither read nor written the assignment before.
            assert len(worker.statements) == 1
            assert 'FROM lan_tournaments' in worker.statements[0]
            assert 'FOR UPDATE' in worker.statements[0]
            assert not any(
                'lan_tournament_orgas' in statement
                for statement in worker.statements
            )

            # Waiting for the tournament, it has not touched the grant:
            # nobody else holds the assignment row, and it is still there.
            rows = holder.execute(
                text(
                    'SELECT id FROM lan_tournament_orgas'
                    ' WHERE tournament_id = :t AND user_id = :u'
                    ' FOR UPDATE NOWAIT'
                ),
                {'t': duel.tournament_id, 'u': ada.id},
            ).all()
            assert len(rows) == 1
        finally:
            holder.rollback()

        result = worker.result()

    assert result.is_ok()
    assert _assignments(duel.tournament_id, ada) == 0
    assert _assignments(duel.tournament_id, bob) == 1
    assert [
        (entry.event_type, entry.initiator_id, entry.data)
        for entry in tournament_log_service.get_entries_for_tournament(
            duel.tournament_id
        )
        if entry.event_type == 'tournament-orga-revoked'
    ] == [('tournament-orga-revoked', bob.id, {'user_id': str(ada.id)})]


# -- a revocation and a pin in flight --


def test_a_pin_in_flight_blocks_the_revocation_until_it_commits(
    duel, admin_app, monkeypatch
):
    ada, bob = duel.ada, duel.bob
    parked, release = threading.Event(), threading.Event()
    real_create = tournament_log_service.create_log_entry

    def park_before_the_audit(*args, **kwargs):
        # The authority is checked, the tournament is locked, the pin is
        # written and not committed.
        parked.set()
        assert release.wait(PARK_SECONDS)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        service,
        'tournament_log_service',
        SimpleNamespace(create_log_entry=park_before_the_audit),
    )
    pinning = Worker(admin_app, lambda: duel.pin(ada, pinned=True, expected=0))
    revoking = Worker(
        admin_app,
        lambda: orgas.revoke_orga(duel.tournament_id, ada.id, bob.id),
    )
    try:
        pinning.start()
        assert parked.wait(PARK_SECONDS)
        revoking.start()
        with db.engine.connect() as observer:
            _wait_until_blocked(observer, revoking, pinning.pid)
            assert _assignments(duel.tournament_id, ada) == 1
            assert _pin_rows(duel.match_id) == []
    finally:
        release.set()

    pinned = pinning.result()
    revoked = revoking.result()

    # The pin went through with the authority it had when it checked it.
    assert pinned.unwrap().revision == 1
    assert revoked.is_ok()
    assert len(_pin_rows(duel.match_id)) == 1
    # The next request of the revoked orga finds the grant gone.
    monkeypatch.setattr(
        service, 'tournament_log_service', tournament_log_service
    )
    refused = duel.pin(ada, pinned=False, expected=1)
    assert refused.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert len(_pin_rows(duel.match_id)) == 1
    assert _pin_rows(duel.match_id)[0]['revision'] == 1


def test_a_revocation_committed_first_defeats_a_pin_that_passed_its_early_read(
    duel, admin_app, monkeypatch
):
    ada, bob = duel.ada, duel.bob
    parked, release = threading.Event(), threading.Event()
    real_lock = repo.lock_tournament_for_update

    def park_before_the_lock(tournament_id):
        # Only the pin waits here. It has looked the match up and holds
        # nothing yet; the revocation takes the same lock unhindered.
        if threading.current_thread() is pinning.thread:
            parked.set()
            assert release.wait(PARK_SECONDS)
        real_lock(tournament_id)

    monkeypatch.setattr(
        repo, 'lock_tournament_for_update', park_before_the_lock
    )
    pinning = Worker(admin_app, lambda: duel.pin(ada, pinned=True, expected=0))
    try:
        pinning.start()
        assert parked.wait(PARK_SECONDS)
        assert orgas.revoke_orga(duel.tournament_id, ada.id, bob.id).is_ok()
    finally:
        release.set()

    refused = pinning.result()

    assert refused.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert _pin_rows(duel.match_id) == []
    assert _pin_audit(duel.tournament_id) == []
    _assert_tournament_unlocked(duel.tournament_id)


def _assignments(tournament_id, user) -> int:
    (row,) = _read(
        'SELECT count(*) AS n FROM lan_tournament_orgas'
        ' WHERE tournament_id = :t AND user_id = :u',
        t=tournament_id,
        u=user.id,
    )
    return row['n']


class Worker:
    """Runs one call in its own thread, application context and session."""

    def __init__(self, app, call) -> None:
        self.app = app
        self.call = call
        self.pid: int | None = None
        self.statements: list[str] = []
        self.outcome = None
        self.error: BaseException | None = None
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name='pin-race', daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def _on_begin(self, session, transaction, connection) -> None:
        connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        )
        self.pid = connection.scalar(text('SELECT pg_backend_pid()'))
        event.listen(connection, 'before_cursor_execute', self._on_statement)

    def _on_statement(self, connection, cursor, statement, *args) -> None:
        self.statements.append(statement)

    def _run(self) -> None:
        try:
            with self.app.app_context():
                event.listen(db.session(), 'after_begin', self._on_begin)
                try:
                    self.outcome = self.call()
                finally:
                    db.session.rollback()
                    db.session.remove()
        except BaseException as error:  # reported by the test, never lost
            self.error = error
        finally:
            self.finished.set()

    def result(self):
        self.thread.join(JOIN_SECONDS)
        assert not self.thread.is_alive(), 'the worker did not finish'
        if self.error is not None:
            raise self.error
        return self.outcome


def _wait_until_blocked(connection, worker: Worker, blocker_pid: int) -> None:
    """Wait until PostgreSQL reports the worker blocked by that backend."""
    deadline = time.monotonic() + BLOCK_SECONDS
    while time.monotonic() < deadline:
        if worker.finished.is_set():
            break
        if worker.pid is not None:
            blockers = connection.scalar(
                text('SELECT pg_blocking_pids(:pid)'), {'pid': worker.pid}
            )
            if blocker_pid in (blockers or []):
                return
        time.sleep(0.05)

    raise AssertionError(
        'the worker was not blocked by the expected transaction'
        f' (finished: {worker.finished.is_set()}, error: {worker.error!r})'
    )


# -- authority --


def test_only_authorized_orgas_and_administrators_can_pin(
    make_scene, users, admin
):
    scene = make_scene()
    elsewhere = make_scene()
    mine = scene.tournament()
    sibling = scene.tournament()
    other_party = elsewhere.tournament()
    ada, eve, stranger, forger = users[0], users[1], users[2], users[3]
    scene.assign(mine, ada)
    scene.assign(sibling, eve)
    elsewhere.assign(other_party, ada)
    match_id = scene.duel(mine, users[4], users[5])
    foreign_match = elsewhere.duel(other_party, users[4], users[5])

    def pin(viewer, party_id=scene.party_id, match=match_id, expected=0):
        return service.set_match_pin(
            viewer, party_id, match, pinned=True, expected_revision=expected
        )

    forbidden = dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    missing = service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    anonymous = CurrentUser.create_anonymous(None)

    assert (
        pin(anonymous).unwrap_err()
        == dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR
    )
    assert pin(_viewer(stranger)).unwrap_err() == forbidden
    # A claim of the permission on the viewer object is not authority.
    assert (
        pin(_viewer(forger, 'lan_tournament.administrate')).unwrap_err()
        == forbidden
    )
    # An orga of a sibling tournament of the party learns nothing more
    # than the one who asks for a match that does not exist.
    assert pin(_viewer(eve)).unwrap_err() == missing
    assert (
        pin(_viewer(eve), match=TournamentMatchID(uuid7())).unwrap_err()
        == missing
    )
    # The party in the request does not widen anything.
    assert (
        pin(_viewer(ada), party_id=elsewhere.party_id).unwrap_err() == missing
    )
    assert pin(_viewer(ada), match=foreign_match).unwrap_err() == missing
    assert (
        pin(_admin_viewer(admin), party_id=elsewhere.party_id).unwrap_err()
        == missing
    )
    assert (
        pin(_admin_viewer(admin), match=foreign_match).unwrap_err() == missing
    )
    assert _pin_rows(match_id) == []
    assert _pin_rows(foreign_match) == []

    # The assigned orga pins; the administrator reaches every tournament
    # of the party without an assignment.
    assert pin(_viewer(ada)).unwrap().revision == 1
    assert pin(_admin_viewer(admin), expected=1).unwrap() is not None
    unassigned = scene.duel(sibling, users[4], users[5])
    assert pin(_admin_viewer(admin), match=unassigned).unwrap().revision == 1
    assert pin(_viewer(ada), match=unassigned).unwrap_err() == missing
    for tournament_id in (mine, sibling, other_party):
        _assert_tournament_unlocked(tournament_id)


def test_a_revoked_orga_cannot_pin_and_a_string_id_is_accepted(duel):
    ada, bob = duel.ada, duel.bob

    assert duel.pin(ada, match_id=str(duel.match_id)).unwrap().revision == 1
    assert orgas.revoke_orga(duel.tournament_id, ada.id, bob.id).is_ok()

    refused = duel.pin(ada, pinned=False, expected=1)

    assert refused.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert _pin_rows(duel.match_id)[0]['revision'] == 1


@pytest.mark.parametrize('match_id', ['not-a-uuid', '', '12345', 'None'])
def test_a_malformed_match_id_is_not_found_and_never_a_500(duel, match_id):
    result = duel.pin(duel.ada, match_id=match_id)

    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR


def test_a_deleted_match_is_not_found(duel):
    gone = duel.scene.duel(duel.tournament_id, duel.users[4], duel.users[5])
    for sql in (
        'DELETE FROM lan_tournament_match_due_episodes WHERE match_id = :m',
        'DELETE FROM lan_tournament_match_contestants WHERE tournament_match_id = :m',
        'DELETE FROM lan_tournament_matches WHERE id = :m',
    ):
        _execute(sql, m=gone)

    result = duel.pin(duel.ada, match_id=gone)

    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    _assert_tournament_unlocked(duel.tournament_id)


@pytest.mark.parametrize(
    'status, confirmed',
    [
        (ONGOING, True),
        (TournamentStatus.COMPLETED, False),
        (TournamentStatus.CANCELLED, False),
        (TournamentStatus.COMPLETED, True),
    ],
)
def test_terminal_matches_and_tournaments_cannot_be_pinned_or_unpinned(
    make_scene, users, status, confirmed
):
    scene = make_scene()
    ada = users[0]
    tournament_id = scene.tournament(status=status)
    scene.assign(tournament_id, ada)
    match_id = scene.duel(
        tournament_id,
        users[2],
        users[3],
        confirmed=confirmed,
        wait_minutes=None,
    )
    # A pin set while the match was running stays, and is read only.
    _execute(
        'INSERT INTO lan_tournament_match_dashboard_annotations'
        ' (match_id, tournament_id, revision, pinned_at, pinned_by,'
        '  updated_at, updated_by)'
        ' VALUES (:m, :t, 1, :at, :by, :at, :by)',
        m=match_id,
        t=tournament_id,
        at=NOW,
        by=ada.id,
    )
    (before,) = _pin_rows(match_id)

    for pinned in (True, False):
        result = service.set_match_pin(
            _viewer(ada),
            scene.party_id,
            match_id,
            pinned=pinned,
            expected_revision=1,
        )
        assert result.unwrap_err() == service.DASHBOARD_MATCH_TERMINAL_ERROR

    assert _pin_rows(match_id) == [before]
    assert _pin_audit(tournament_id) == []
    _assert_tournament_unlocked(tournament_id)


@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.DRAFT,
        TournamentStatus.REGISTRATION_CLOSED,
        ONGOING,
        TournamentStatus.PAUSED,
    ],
)
def test_matches_of_a_running_or_upcoming_tournament_can_be_pinned(
    make_scene, users, status
):
    scene = make_scene()
    ada = users[0]
    tournament_id = scene.tournament(status=status)
    scene.assign(tournament_id, ada)
    match_id = scene.duel(tournament_id, users[2], users[3], wait_minutes=None)

    result = service.set_match_pin(
        _viewer(ada), scene.party_id, match_id, pinned=True, expected_revision=0
    )

    assert result.unwrap().revision == 1
    _assert_tournament_unlocked(tournament_id)
