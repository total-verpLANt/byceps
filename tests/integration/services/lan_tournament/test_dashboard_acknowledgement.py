from datetime import datetime, timedelta
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    signals,
    tournament_dashboard_coordination_service as service,
    tournament_dashboard_service as dashboard_service,
    tournament_dashboard_settings_service as settings_service,
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
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    AckUnavailableReason,
    DashboardQuery,
    DashboardRowState,
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


MINUTE_US = 60_000_000
MINUTE = timedelta(minutes=1)
MICROSECOND = timedelta(microseconds=1)
NOW = datetime(2026, 10, 8, 12, 0, 0)
CLOCK_START = NOW - timedelta(hours=3)
CLOCK_AT_NOW_US = 180 * MINUTE_US
WAIT_MINUTES = 20
# A fixed date long before any server clock: a stamp from the acknowledgement
# path could not hide behind `GREATEST`.
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
BLOCK_SECONDS = 10
JOIN_SECONDS = 15


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03Ack{i:02d}') for i in range(8)]


@pytest.fixture(scope='module')
def confirmer(make_user):
    return make_user('F03AckConfirmer')


@pytest.fixture(scope='module')
def admin(make_admin):
    return make_admin(
        {'lan_tournament.administrate'}, screen_name='F03AckAdmin'
    )


@pytest.fixture(autouse=True)
def _context(admin_app):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


def _viewer(user, *claimed: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(claimed))


def _as_viewer(user_or_viewer) -> CurrentUser:
    if isinstance(user_or_viewer, CurrentUser):
        return user_or_viewer

    return _viewer(user_or_viewer)


def _admin_viewer(admin) -> CurrentUser:
    return _viewer(admin, 'lan_tournament.administrate')


class Scene:
    """Builds committed tournaments of one party, with matches and orgas."""

    def __init__(self, party_id: PartyID, confirmer) -> None:
        self.party_id = party_id
        self.confirmer = confirmer
        self._joined: dict[tuple, TournamentParticipantID] = {}
        self._orders = iter(range(1, 10_000))

    def tournament(
        self,
        *,
        status: TournamentStatus = ONGOING,
        known_clock: bool = True,
    ) -> TournamentID:
        """Create a tournament that has run for three hours at `NOW`.

        A stopped one has the same time on its frozen clock. Without a
        `known_clock` it started before the feature: no history.
        """
        tournament_id = TournamentID(uuid7())
        running = status is ONGOING
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=self.party_id,
                name=f'Acks {tournament_id}',
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
                    0 if running or not known_clock else CLOCK_AT_NOW_US
                ),
                operational_clock_running_since=(
                    CLOCK_START if running and known_clock else None
                ),
                operational_clock_activated_at=(
                    CLOCK_START if known_clock else None
                ),
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
        wait_minutes: float | None = WAIT_MINUTES,
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
            self.open_episode(tournament_id, match_id, wait_minutes)
        db.session.commit()
        return match_id

    def open_episode(
        self, tournament_id, match_id, wait_minutes: float
    ) -> MatchDueEpisodeID:
        episode_id = MatchDueEpisodeID(uuid7())
        repo.open_due_episode_flush(
            MatchDueEpisode(
                id=episode_id,
                tournament_id=tournament_id,
                match_id=match_id,
                pairing_key='key',
                opened_at=NOW - timedelta(minutes=wait_minutes),
                opened_clock_us=CLOCK_AT_NOW_US - int(wait_minutes * MINUTE_US),
            )
        )
        return episode_id

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
        party_id = PartyID(f'f03a-{uuid4().hex[:12]}')
        make_party(brand, party_id, f'F03 acks {party_id}')
        return Scene(party_id, confirmer)

    return _make


class Duel:
    """One due match of a running tournament with two orgas."""

    def __init__(
        self, make_scene, users, admin, monkeypatch, **tournament
    ) -> None:
        self.scene = make_scene()
        self.party_id = self.scene.party_id
        self.tournament_id = self.scene.tournament(**tournament)
        self.ada, self.bob = users[0], users[1]
        self.scene.assign(self.tournament_id, self.ada)
        self.scene.assign(self.tournament_id, self.bob)
        self.match_id = self.scene.duel(self.tournament_id, users[2], users[3])
        self.users = users
        self.admin = admin
        self._monkeypatch = monkeypatch
        self.at(NOW)

    def at(self, when: datetime) -> None:
        """Make `when` the server time of the next transaction."""
        self._monkeypatch.setattr(repo, 'get_operation_time', lambda: when)

    def page(self, viewer, *, when=NOW, settings=SETTINGS):
        result = dashboard_service.get_dashboard_page(
            _as_viewer(viewer),
            self.party_id,
            DashboardQuery(scope='all', view='all', per_page=50),
            settings=settings,
            now=when,
        )
        return result.unwrap()

    def row(self, viewer, *, when=NOW, settings=SETTINGS):
        (row,) = [
            row
            for row in self.page(viewer, when=when, settings=settings).rows
            if row.match_id == self.match_id
        ]
        return row

    def acknowledge(
        self,
        viewer,
        *,
        when: datetime | None = None,
        comment=None,
        match_id=None,
        party_id=None,
        episode_id='form',
        revision='form',
    ):
        """Acknowledge with the episode and revision the page showed."""
        if when is not None:
            self.at(when)
        stored = _open_episode(self.match_id)
        return service.acknowledge_match(
            _as_viewer(viewer),
            self.party_id if party_id is None else party_id,
            self.match_id if match_id is None else match_id,
            expected_episode_id=(
                (stored['id'] if stored else MatchDueEpisodeID(uuid7()))
                if episode_id == 'form'
                else episode_id
            ),
            expected_ack_revision=(
                (stored['ack_revision'] if stored else 0)
                if revision == 'form'
                else revision
            ),
            comment=comment,
        )

    def attempt(self, viewer, when: datetime, *, comment=None):
        """Read the page, then act on it as a browser would.

        The row says whether the acknowledgement is offered. What the
        transaction answers must agree.
        """
        row = self.row(viewer, when=when)
        self.at(when)
        result = service.acknowledge_match(
            _as_viewer(viewer),
            self.party_id,
            self.match_id,
            expected_episode_id=row.episode_id,
            expected_ack_revision=row.ack_revision,
            comment=comment,
        )
        if row.ack_unavailable_reason is None:
            assert result.is_ok(), result
        else:
            assert result.unwrap_err() == (
                f'dashboard_ack_{row.ack_unavailable_reason.value}'
            )
        return result


@pytest.fixture
def duel(make_scene, users, admin, monkeypatch):
    return Duel(make_scene, users, admin, monkeypatch)


# -- raw reads through a connection of their own: only what is committed --


def _read(sql: str, **params) -> list[dict]:
    with db.engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _execute(sql: str, **params) -> None:
    with db.engine.begin() as connection:
        connection.execute(text(sql), params)


def _open_episode(match_id) -> dict | None:
    rows = _read(
        'SELECT * FROM lan_tournament_match_due_episodes'
        ' WHERE match_id = :id AND closed_at IS NULL',
        id=match_id,
    )
    return rows[0] if rows else None


def _acks(match_id) -> list[dict]:
    return _read(
        'SELECT * FROM lan_tournament_match_escalation_acks'
        ' WHERE match_id = :id ORDER BY revision, id',
        id=match_id,
    )


def _ack_audit(tournament_id) -> list[tuple]:
    return [
        (entry.event_type, entry.initiator_id, entry.data)
        for entry in tournament_log_service.get_entries_for_tournament(
            tournament_id
        )
        if entry.event_type == service.MATCH_ACKNOWLEDGED_EVENT
    ]


def _facts(match_id, tournament_id) -> dict:
    """Everything an acknowledgement may or may not change, as committed."""
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
        'acks': _acks(match_id),
        'pins': _read(
            'SELECT * FROM lan_tournament_match_dashboard_annotations'
            ' WHERE match_id = :id',
            id=match_id,
        ),
        'audit': _ack_audit(tournament_id),
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


def _without_episode_revision(episode: dict) -> dict:
    return {k: v for k, v in episode.items() if k != 'ack_revision'}


# -- the five named tests --


def test_ack_keeps_occupancy_and_total_wait(duel):
    ada, bob = duel.ada, duel.bob
    facts = _facts(duel.match_id, duel.tournament_id)
    (episode,) = facts['episodes']
    assert facts['match'][0]['last_changed_at'] == LAST_CHANGED
    assert facts['acks'] == []
    before = duel.row(ada)
    assert before.tier.value == 'yellow'
    assert before.total_active_wait_us == WAIT_MINUTES * MINUTE_US
    assert before.alert_interval_us == WAIT_MINUTES * MINUTE_US
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
        ack = duel.acknowledge(
            ada, when=NOW, comment='Contacted both captains'
        ).unwrap()
    finally:
        for signal in receivers:
            signal.disconnect(heard)

    after = _facts(duel.match_id, duel.tournament_id)
    # The match, the clock and the pin are what they were, down to the
    # last change and the occupancy.
    assert after['match'] == facts['match']
    assert after['match'][0]['last_changed_at'] == LAST_CHANGED
    assert after['match'][0]['occupied_since'] == NOW - timedelta(minutes=30)
    assert after['tournament'] == facts['tournament']
    assert after['pins'] == facts['pins'] == []
    # The episode is the same one and differs in the revision only: it
    # opened when and at what clock value it did.
    (episode_after,) = after['episodes']
    assert episode_after['ack_revision'] == 1
    assert _without_episode_revision(episode_after) == (
        _without_episode_revision(episode)
    )
    assert (episode_after['opened_at'], episode_after['opened_clock_us']) == (
        NOW - timedelta(minutes=WAIT_MINUTES),
        CLOCK_AT_NOW_US - WAIT_MINUTES * MINUTE_US,
    )
    assert (episode_after['closed_at'], episode_after['closed_clock_us']) == (
        None,
        None,
    )
    # One acknowledgement was appended, at the clock value of the request.
    (stored,) = after['acks']
    assert (stored['revision'], stored['clock_us']) == (1, CLOCK_AT_NOW_US)
    assert (stored['actor_id'], stored['comment']) == (
        ada.id,
        'Contacted both captains',
    )
    assert stored['occurred_at'] == NOW
    assert (stored['episode_id'], stored['match_id']) == (
        episode['id'],
        duel.match_id,
    )
    assert ack.id == stored['id']
    assert after['audit'] == [
        (
            'match-acknowledged',
            ada.id,
            {
                'match_id': str(duel.match_id),
                'episode_id': str(episode['id']),
                'revision': 1,
            },
        )
    ]
    heard.assert_not_called()

    # What the page derives from it: the wait and the history are as they
    # were, and only the alert interval started over.
    page = duel.row(bob)
    assert page.total_active_wait_us == before.total_active_wait_us
    assert page.episode_opened_at == before.episode_opened_at
    assert page.occupied_since == before.occupied_since
    assert page.last_changed_at == before.last_changed_at == LAST_CHANGED
    assert page.has_prior_episode is before.has_prior_episode
    assert (page.alert_interval_us, page.tier.value) == (0, 'green')


def test_ack_reescalates_after_active_time_only(duel):
    ada = duel.ada
    tournament_id = duel.tournament_id
    first = duel.attempt(ada, NOW).unwrap()
    assert first.clock_us == CLOCK_AT_NOW_US

    # Right after the check the interval starts over.
    row = duel.row(ada, when=NOW)
    assert (row.alert_interval_us, row.tier.value) == (0, 'green')
    assert (
        row.ack_unavailable_reason is AckUnavailableReason.RECENTLY_ACKNOWLEDGED
    )
    ten = NOW + 10 * MINUTE
    assert duel.attempt(ada, ten).is_err()

    # The tournament pauses for an hour: it neither counts nor can anybody
    # acknowledge in it.
    repo.set_tournament_status_flush(
        tournament_id, TournamentStatus.PAUSED, changed_at=ten
    ).unwrap()
    db.session.commit()
    after_pause = ten + 60 * MINUTE
    paused = duel.row(ada, when=after_pause)
    assert paused.state is DashboardRowState.PAUSED
    assert paused.ack_unavailable_reason is AckUnavailableReason.PAUSED
    assert duel.attempt(ada, after_pause).unwrap_err() == (
        service.DASHBOARD_ACK_PAUSED_ERROR
    )
    repo.set_tournament_status_flush(
        tournament_id, ONGOING, changed_at=after_pause
    ).unwrap()
    db.session.commit()

    # 74:59.999999 of wall time after the check, but only 14:59.999999 of
    # active time: still quiet.
    almost = after_pause + 5 * MINUTE - MICROSECOND
    row = duel.row(ada, when=almost)
    assert row.alert_interval_us == 15 * MINUTE_US - 1
    assert row.tier.value == 'green'
    assert duel.attempt(ada, almost).unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )

    # At exactly 15:00 of active time it is yellow again and offered.
    due_again = after_pause + 5 * MINUTE
    row = duel.row(ada, when=due_again)
    assert (row.alert_interval_us, row.tier.value) == (
        15 * MINUTE_US,
        'yellow',
    )
    assert row.ack_unavailable_reason is None
    second = duel.attempt(ada, due_again, comment='Again').unwrap()
    assert second.revision == 2
    assert second.clock_us == CLOCK_AT_NOW_US + 15 * MINUTE_US
    # The episode-open baseline and the total wait are still the original.
    row = duel.row(ada, when=due_again)
    assert row.total_active_wait_us == (WAIT_MINUTES + 15) * MINUTE_US
    assert row.alert_interval_us == 0
    # The new interval runs from the second check and not from the first.
    assert duel.attempt(
        ada, due_again + 15 * MINUTE - MICROSECOND
    ).unwrap_err() == (service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR)

    # Left alone for 45 active minutes it is red, and acknowledged as such.
    red = due_again + 45 * MINUTE
    assert duel.row(ada, when=red).tier.value == 'red'
    assert duel.attempt(ada, red).unwrap().revision == 3
    assert [stored['revision'] for stored in _acks(duel.match_id)] == [1, 2, 3]
    assert [a['clock_us'] for a in _acks(duel.match_id)] == [
        CLOCK_AT_NOW_US,
        CLOCK_AT_NOW_US + 15 * MINUTE_US,
        CLOCK_AT_NOW_US + 60 * MINUTE_US,
    ]
    _assert_tournament_unlocked(tournament_id)


# fmt: off
REFUSALS = [
    # id,                tournament,     episode,   revision,  setup,          error
    ('stale-revision',   {},             'form',    0,         'acked-once',   service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('ahead',            {},             'form',    2,         'acked-once',   service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('negative',         {},             'form',    -1,        None,           service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('huge',             {},             'form',    2**40,     None,           service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('other-episode',    {},             'other',   'form',    None,           service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('malformed',        {},             'nonsense', 'form',   None,           service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('no-episode-id',    {},             None,      'form',    None,           service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('replaced-episode', {},             'old',     'form',    'new-episode',  service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('green-new',        {},             'form',    'form',    'green',        service.DASHBOARD_ACK_BELOW_THRESHOLD_ERROR),
    ('green-acked',      {},             'form',    'form',    'acked-once',   service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR),
    ('paused',           {},             'form',    'form',    'paused',       service.DASHBOARD_ACK_PAUSED_ERROR),
    ('not-due',          {},             'form',    'form',    'no-episode',   service.DASHBOARD_ACK_NOT_DUE_ERROR),
    ('unknown-clock',    {'known_clock': False}, 'form', 'form', 'no-episode', service.DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR),
    ('registration',     {'status': TournamentStatus.REGISTRATION_CLOSED}, 'form', 'form', None, service.DASHBOARD_ACK_NOT_DUE_ERROR),
    ('confirmed',        {},             'form',    'form',    'confirmed',    service.DASHBOARD_ACK_TERMINAL_ERROR),
    ('completed',        {'status': TournamentStatus.COMPLETED}, 'form', 'form', None, service.DASHBOARD_ACK_TERMINAL_ERROR),
    ('cancelled',        {'status': TournamentStatus.CANCELLED}, 'form', 'form', None, service.DASHBOARD_ACK_TERMINAL_ERROR),
]
# fmt: on


@pytest.mark.parametrize(
    'tournament, episode, revision, setup, error',
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_stale_episode_revision_and_green_ack_refused(
    make_scene,
    users,
    admin,
    monkeypatch,
    tournament,
    episode,
    revision,
    setup,
    error,
):
    duel = Duel(make_scene, users, admin, monkeypatch, **tournament)
    ada = duel.ada
    old_episode_id = None
    if setup == 'acked-once':
        duel.acknowledge(ada, when=NOW).unwrap()
        duel.at(NOW + 5 * MINUTE)
    elif setup == 'green':
        _execute(
            'UPDATE lan_tournament_match_due_episodes'
            ' SET opened_clock_us = :clock WHERE match_id = :m',
            clock=CLOCK_AT_NOW_US - 14 * MINUTE_US,
            m=duel.match_id,
        )
    elif setup == 'new-episode':
        old_episode_id = _open_episode(duel.match_id)['id']
        repo.close_due_episodes_flush(
            [duel.match_id], occurred_at=NOW, clock_us=CLOCK_AT_NOW_US
        )
        duel.scene.open_episode(duel.tournament_id, duel.match_id, 20)
        db.session.commit()
    elif setup == 'paused':
        repo.set_tournament_status_flush(
            duel.tournament_id, TournamentStatus.PAUSED, changed_at=NOW
        ).unwrap()
        db.session.commit()
    elif setup == 'no-episode':
        _execute(
            'DELETE FROM lan_tournament_match_due_episodes WHERE match_id = :m',
            m=duel.match_id,
        )
    elif setup == 'confirmed':
        _execute(
            'UPDATE lan_tournament_matches SET confirmed_by = :u WHERE id = :m',
            u=ada.id,
            m=duel.match_id,
        )
    if episode == 'other':
        episode = MatchDueEpisodeID(uuid7())
    elif episode == 'old':
        episode = old_episode_id
    facts = _facts(duel.match_id, duel.tournament_id)
    when = NOW + 5 * MINUTE if setup == 'acked-once' else NOW
    row_before = duel.row(ada, when=when)

    result = duel.acknowledge(
        ada,
        when=when,
        episode_id=episode if episode != 'form' else 'form',
        revision=revision,
    )

    assert result.unwrap_err() == error
    # Nothing was written or audited, and no lock outlives the call.
    assert _facts(duel.match_id, duel.tournament_id) == facts
    # So no refusal starts a new quiet time or changes anything on the page.
    assert duel.row(ada, when=when) == row_before
    _assert_tournament_unlocked(duel.tournament_id)


def test_a_duplicate_adds_no_second_ack_and_no_longer_quiet_time(duel):
    ada, bob = duel.ada, duel.bob
    form = duel.row(bob)
    assert (form.ack_revision, form.ack_unavailable_reason) == (0, None)

    first = duel.acknowledge(ada, when=NOW, revision=0).unwrap()
    duplicate = duel.acknowledge(
        bob,
        when=NOW + 14 * MINUTE,
        episode_id=form.episode_id,
        revision=form.ack_revision,
    )

    assert duplicate.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    (stored,) = _acks(duel.match_id)
    assert stored['id'] == first.id
    assert len(_ack_audit(duel.tournament_id)) == 1
    assert _open_episode(duel.match_id)['ack_revision'] == 1
    # It is due again 15 minutes after the first check, not after the
    # duplicate.
    almost = NOW + 15 * MINUTE - MICROSECOND
    assert duel.row(bob, when=almost).ack_unavailable_reason is (
        AckUnavailableReason.RECENTLY_ACKNOWLEDGED
    )
    assert duel.row(bob, when=NOW + 15 * MINUTE).ack_unavailable_reason is None
    assert duel.attempt(bob, NOW + 15 * MINUTE).unwrap().revision == 2


def test_ack_audit_failure_rolls_back_every_fact(duel, monkeypatch):
    ada = duel.ada
    saved = []
    advanced = []
    real_save = repo.create_escalation_ack_flush
    real_advance = repo.advance_episode_ack_revision_flush

    def spy_save(ack):
        real_save(ack)
        saved.append(ack)
        # Flushed, so the insert is visible to the transaction that wrote it.
        (stored,) = db.session.execute(
            text(
                'SELECT revision FROM lan_tournament_match_escalation_acks'
                ' WHERE match_id = :m AND revision = :r'
            ),
            {'m': duel.match_id, 'r': ack.revision},
        ).all()
        assert stored[0] == ack.revision

    def spy_advance(episode_id, *, expected_revision):
        result = real_advance(episode_id, expected_revision=expected_revision)
        advanced.append(result)
        return result

    monkeypatch.setattr(repo, 'create_escalation_ack_flush', spy_save)
    monkeypatch.setattr(repo, 'advance_episode_ack_revision_flush', spy_advance)
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
    facts = _facts(duel.match_id, duel.tournament_id)

    # The revision was raised and the record appended inside the
    # transaction; both are gone.
    with pytest.raises(RuntimeError, match='audit unavailable'):
        duel.acknowledge(ada, when=NOW, comment='Lost')

    assert advanced == [True]
    assert [ack.revision for ack in saved] == [1]
    assert rollbacks == [True]
    assert _facts(duel.match_id, duel.tournament_id) == facts
    assert _open_episode(duel.match_id)['ack_revision'] == 0
    assert _acks(duel.match_id) == []
    # The baseline did not move either: the page still shows the whole wait.
    row = duel.row(ada)
    assert row.alert_interval_us == WAIT_MINUTES * MINUTE_US
    assert row.latest_acknowledgement is None
    _assert_tournament_unlocked(duel.tournament_id)

    # With an earlier acknowledgement in place, it is that one that stays.
    monkeypatch.setattr(service, 'tournament_log_service', real_log)
    duel.acknowledge(ada, when=NOW, comment='Kept').unwrap()
    facts = _facts(duel.match_id, duel.tournament_id)
    monkeypatch.setattr(
        service,
        'tournament_log_service',
        SimpleNamespace(create_log_entry=failing_audit),
    )
    saved.clear()
    advanced.clear()
    rollbacks.clear()
    later = NOW + 15 * MINUTE

    with pytest.raises(RuntimeError, match='audit unavailable'):
        duel.acknowledge(ada, when=later, comment='Lost too')

    assert advanced == [True]
    assert [ack.revision for ack in saved] == [2]
    assert rollbacks == [True]
    assert _facts(duel.match_id, duel.tournament_id) == facts
    row = duel.row(ada, when=later)
    assert row.alert_interval_us == 15 * MINUTE_US
    assert row.latest_acknowledgement.comment == 'Kept'
    assert row.ack_revision == 1

    # The audit entry is staged with the facts and not before the commit:
    # when that commit fails, neither they nor the entry are kept.
    monkeypatch.setattr(service, 'tournament_log_service', real_log)

    def failing_commit():
        raise RuntimeError('commit failed')

    monkeypatch.setattr(repo, 'commit_session', failing_commit)
    rollbacks.clear()

    with pytest.raises(RuntimeError, match='commit failed'):
        duel.acknowledge(ada, when=later, comment='Lost three')

    assert rollbacks == [True]
    assert _facts(duel.match_id, duel.tournament_id) == facts
    _assert_tournament_unlocked(duel.tournament_id)


def test_second_orga_sees_first_actor_time_comment_and_can_follow_up_after_reescalation(
    duel,
):
    ada, bob = duel.ada, duel.bob
    form_of_bob = duel.row(bob)

    first = duel.acknowledge(
        ada, when=NOW, comment='Contacted both captains; waiting for a player'
    ).unwrap()

    # Bob reads the record of Ada on his next load or poll.
    row = duel.row(bob, when=NOW + 5 * MINUTE)
    latest = row.latest_acknowledgement
    assert (latest.actor_display_name, latest.revision) == (ada.screen_name, 1)
    assert latest.occurred_at == NOW
    assert latest.comment == 'Contacted both captains; waiting for a player'
    assert row.acknowledgement_count == 1
    assert (
        row.ack_unavailable_reason is AckUnavailableReason.RECENTLY_ACKNOWLEDGED
    )
    # Nothing marks it as Ada's or Bob's: both read the same row.
    assert duel.row(ada, when=NOW + 5 * MINUTE) == row
    # Bob's form of before is stale, and there is nothing for him to do.
    stale = duel.acknowledge(
        bob,
        when=NOW + 5 * MINUTE,
        episode_id=form_of_bob.episode_id,
        revision=form_of_bob.ack_revision,
    )
    assert stale.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert duel.acknowledge(bob, when=NOW + 5 * MINUTE).unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )
    assert [a['id'] for a in _acks(duel.match_id)] == [first.id]

    # Fifteen active minutes later it is escalated again, still showing
    # Ada's check, and Bob follows up with his own.
    again = NOW + 15 * MINUTE
    row = duel.row(bob, when=again)
    assert row.ack_unavailable_reason is None
    assert row.tier.value == 'yellow'
    assert row.latest_acknowledgement.actor_display_name == ada.screen_name
    second = duel.attempt(bob, again, comment='Player is on the way').unwrap()

    row = duel.row(ada, when=again)
    assert row.acknowledgement_count == 2
    assert [
        (a.revision, a.actor_display_name, a.comment, a.occurred_at)
        for a in row.recent_acknowledgements
    ] == [
        (2, bob.screen_name, 'Player is on the way', again),
        (
            1,
            ada.screen_name,
            'Contacted both captains; waiting for a player',
            NOW,
        ),
    ]
    assert row.latest_acknowledgement.id == second.id
    assert duel.row(bob, when=again) == row
    # Both checks stay, with their own actor, and so do their audit entries.
    assert [(a['revision'], a['actor_id']) for a in _acks(duel.match_id)] == [
        (1, ada.id),
        (2, bob.id),
    ]
    assert [
        (event, who) for event, who, _ in _ack_audit(duel.tournament_id)
    ] == [
        ('match-acknowledged', ada.id),
        ('match-acknowledged', bob.id),
    ]


# -- what it does and does not inherit --


def test_a_new_episode_never_inherits_an_old_ack(duel):
    ada = duel.ada
    old = duel.acknowledge(ada, when=NOW, comment='Old check').unwrap()
    old_facts = _acks(duel.match_id)
    # The demand ends and a new one starts for the same match.
    repo.close_due_episodes_flush(
        [duel.match_id], occurred_at=NOW, clock_us=CLOCK_AT_NOW_US
    )
    new_episode = duel.scene.open_episode(duel.tournament_id, duel.match_id, 20)
    db.session.commit()

    row = duel.row(ada)
    assert row.episode_id == new_episode
    assert (row.ack_revision, row.acknowledgement_count) == (0, 0)
    assert row.latest_acknowledgement is None
    assert row.alert_interval_us == 20 * MINUTE_US
    assert row.ack_unavailable_reason is None
    # The old episode is not a way in.
    stale = duel.acknowledge(
        ada, when=NOW, episode_id=old.episode_id, revision=1
    )
    assert stale.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert _acks(duel.match_id) == old_facts

    fresh = duel.attempt(ada, NOW, comment='New check').unwrap()

    assert (fresh.revision, fresh.episode_id) == (1, new_episode)
    assert fresh.episode_id != old.episode_id
    episodes = {
        e['id']: e['ack_revision']
        for e in _read(
            'SELECT id, ack_revision FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :m',
            m=duel.match_id,
        )
    }
    assert episodes == {old.episode_id: 1, new_episode: 1}


def test_a_stored_override_of_the_party_decides_the_tier(duel):
    ada = duel.ada
    repo.set_party_thresholds_flush(
        duel.party_id,
        yellow_minutes=30,
        red_minutes=90,
        expected_revision=0,
        expected_updated_at=None,
        updated_at=NOW,
        updated_by=ada.id,
    )
    db.session.commit()
    effective = settings_service.get_effective_dashboard_settings(
        duel.party_id
    ).unwrap()
    assert (effective.yellow_minutes, effective.threshold_source) == (
        30,
        'party',
    )
    # Twenty minutes waited: yellow by the default, green by the override.
    row = duel.row(ada, settings=effective)
    assert row.tier.value == 'green'
    assert row.ack_unavailable_reason is AckUnavailableReason.BELOW_THRESHOLD

    refused = duel.acknowledge(ada, when=NOW)
    assert refused.unwrap_err() == service.DASHBOARD_ACK_BELOW_THRESHOLD_ERROR

    at_threshold = NOW + 10 * MINUTE
    assert (
        duel.row(
            ada, when=at_threshold, settings=effective
        ).ack_unavailable_reason
        is None
    )
    assert duel.acknowledge(ada, when=at_threshold).unwrap().revision == 1


def test_the_offer_of_the_page_and_the_transaction_agree(
    make_scene, users, admin, monkeypatch
):
    """Whatever the page does not offer, the transaction refuses, by name."""
    situations = [
        ('due', {}, None),
        ('paused', {'status': TournamentStatus.PAUSED}, None),
        ('completed', {'status': TournamentStatus.COMPLETED}, None),
        ('unknown', {'known_clock': False}, 'no-episode'),
        (
            'registration',
            {'status': TournamentStatus.REGISTRATION_OPEN},
            'no-episode',
        ),
    ]
    offered = {}
    for name, tournament, setup in situations:
        duel = Duel(make_scene, users, admin, monkeypatch, **tournament)
        if setup == 'no-episode':
            _execute(
                'DELETE FROM lan_tournament_match_due_episodes'
                ' WHERE match_id = :m',
                m=duel.match_id,
            )
        row = duel.row(duel.ada)
        offered[name] = row.ack_unavailable_reason
        result = duel.attempt(duel.ada, NOW)
        assert result.is_ok() is (name == 'due'), name

    assert offered == {
        'due': None,
        'paused': AckUnavailableReason.PAUSED,
        'completed': AckUnavailableReason.TERMINAL,
        'unknown': AckUnavailableReason.CLOCK_UNKNOWN,
        'registration': AckUnavailableReason.NOT_DUE,
    }


# -- the comment --


def test_a_comment_is_stored_and_shown_as_plain_text(duel):
    ada = duel.ada
    markup = '  <script>alert(1)</script> & "Müller" {{ x }}\r\nline two  '

    ack = duel.acknowledge(ada, when=NOW, comment=markup).unwrap()

    expected = '<script>alert(1)</script> & "Müller" {{ x }}\nline two'
    assert ack.comment == expected
    assert _acks(duel.match_id)[0]['comment'] == expected
    # The page hands it on as it is; escaping is the template's job.
    row = duel.row(ada)
    assert row.latest_acknowledgement.comment == expected


def test_a_blank_comment_is_no_comment(duel):
    ack = duel.acknowledge(duel.ada, when=NOW, comment='  \n\t ').unwrap()

    assert ack.comment is None
    assert _acks(duel.match_id)[0]['comment'] is None


@pytest.mark.parametrize(
    'comment',
    ['x' * 501, 'a\x00b', 'a\u202eb', 'a\u2028b', 12],
    ids=['too-long', 'nul', 'bidi', 'line-separator', 'not-text'],
)
def test_an_invalid_comment_writes_nothing(duel, comment):
    facts = _facts(duel.match_id, duel.tournament_id)

    result = duel.acknowledge(duel.ada, when=NOW, comment=comment)

    assert result.unwrap_err() == service.DASHBOARD_ACK_COMMENT_INVALID_ERROR
    assert _facts(duel.match_id, duel.tournament_id) == facts
    _assert_tournament_unlocked(duel.tournament_id)


def test_a_comment_of_exactly_500_characters_fits_the_column(duel):
    ack = duel.acknowledge(duel.ada, when=NOW, comment='ä' * 500).unwrap()

    assert len(_acks(duel.match_id)[0]['comment']) == 500
    assert ack.comment == 'ä' * 500


# -- authority --


def test_only_authorized_orgas_and_administrators_can_acknowledge(
    make_scene, users, admin, monkeypatch
):
    monkeypatch.setattr(repo, 'get_operation_time', lambda: NOW)
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
    episode = _open_episode(match_id)

    def acknowledge(
        viewer,
        party_id=scene.party_id,
        match=match_id,
        episode_id=None,
        comment=None,
    ):
        return service.acknowledge_match(
            viewer,
            party_id,
            match,
            expected_episode_id=episode_id or episode['id'],
            expected_ack_revision=0,
            comment=comment,
        )

    forbidden = dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    missing = service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    anonymous = CurrentUser.create_anonymous(None)

    assert (
        acknowledge(anonymous).unwrap_err()
        == dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR
    )
    assert acknowledge(_viewer(stranger)).unwrap_err() == forbidden
    # Somebody without authority is told nothing about their comment either.
    assert (
        acknowledge(_viewer(stranger), comment='x' * 501).unwrap_err()
        == forbidden
    )
    # A claim of the permission on the viewer object is not authority.
    assert (
        acknowledge(_viewer(forger, 'lan_tournament.administrate')).unwrap_err()
        == forbidden
    )
    # An orga of a sibling tournament learns no more than somebody who
    # asks for a match that does not exist.
    assert acknowledge(_viewer(eve)).unwrap_err() == missing
    assert (
        acknowledge(_viewer(eve), match=TournamentMatchID(uuid7())).unwrap_err()
        == missing
    )
    # The party in the request does not widen anything.
    assert (
        acknowledge(_viewer(ada), party_id=elsewhere.party_id).unwrap_err()
        == missing
    )
    assert (
        acknowledge(_viewer(ada), match=foreign_match).unwrap_err() == missing
    )
    assert _acks(match_id) == []
    assert _acks(foreign_match) == []
    _assert_tournament_unlocked(mine)

    # Their own orga and a global administrator, who needs no assignment.
    assert acknowledge(_viewer(ada)).unwrap().actor_id == ada.id
    other = _open_episode(match_id)
    admin_ack = service.acknowledge_match(
        _admin_viewer(admin),
        scene.party_id,
        match_id,
        expected_episode_id=other['id'],
        expected_ack_revision=1,
        comment=None,
    )
    # The first check was just now, so for the administrator it is green.
    assert admin_ack.unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )


def test_an_administrator_without_an_assignment_can_acknowledge(duel, admin):
    ack = duel.acknowledge(_admin_viewer(admin), when=NOW).unwrap()

    assert ack.actor_id == admin.id
    row = duel.row(_admin_viewer(admin))
    assert row.latest_acknowledgement.actor_display_name == admin.screen_name


def test_a_revoked_orga_cannot_acknowledge_and_a_string_id_is_accepted(duel):
    ada, bob = duel.ada, duel.bob
    episode = _open_episode(duel.match_id)

    assert orgas.revoke_orga(duel.tournament_id, ada.id, bob.id).is_ok()
    refused = duel.acknowledge(ada, when=NOW)

    assert refused.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert _acks(duel.match_id) == []
    # The one who is left sends the IDs the way a form does: as text.
    ack = service.acknowledge_match(
        _viewer(bob),
        duel.party_id,
        str(duel.match_id),
        expected_episode_id=str(episode['id']),
        expected_ack_revision=0,
        comment=None,
    ).unwrap()
    assert ack.match_id == duel.match_id


@pytest.mark.parametrize('match_id', ['not-a-uuid', '', '12345', None])
def test_a_malformed_match_id_is_not_found_and_never_a_500(duel, match_id):
    result = service.acknowledge_match(
        _viewer(duel.ada),
        duel.party_id,
        match_id,
        expected_episode_id=MatchDueEpisodeID(uuid7()),
        expected_ack_revision=0,
        comment=None,
    )

    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    assert _acks(duel.match_id) == []
    _assert_tournament_unlocked(duel.tournament_id)


def test_a_deleted_match_is_not_found(duel):
    gone = TournamentMatchID(uuid7())

    result = duel.acknowledge(duel.ada, when=NOW, match_id=gone)

    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR


# -- the last lines of defence --


@pytest.mark.parametrize('write', ['bump', 'close'])
def test_a_writer_outside_the_lock_is_a_lost_compare_and_set(
    duel, monkeypatch, write
):
    """Somebody changes the episode between the read and the write."""
    statements = {
        'bump': (
            (
                'UPDATE lan_tournament_match_due_episodes'
                ' SET ack_revision = ack_revision + 1 WHERE id = :id'
            ),
            {},
        ),
        'close': (
            (
                'UPDATE lan_tournament_match_due_episodes'
                ' SET closed_at = :at, closed_clock_us = :clock'
                ' WHERE id = :id'
            ),
            {'at': NOW, 'clock': CLOCK_AT_NOW_US},
        ),
    }
    sql, params = statements[write]
    real_find = repo.find_latest_escalation_ack

    def read_then_lose(episode_id):
        latest = real_find(episode_id)
        _execute(sql, id=episode_id, **params)
        return latest

    monkeypatch.setattr(repo, 'find_latest_escalation_ack', read_then_lose)

    result = duel.acknowledge(duel.ada, when=NOW)

    # Neither a revision somebody else raised nor an episode somebody
    # else closed takes an acknowledgement.
    assert result.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert _acks(duel.match_id) == []
    (episode,) = _read(
        'SELECT ack_revision, closed_at FROM lan_tournament_match_due_episodes'
        ' WHERE match_id = :m',
        m=duel.match_id,
    )
    assert (episode['ack_revision'], episode['closed_at'] is not None) == (
        (1, False) if write == 'bump' else (0, True)
    )
    assert _ack_audit(duel.tournament_id) == []
    _assert_tournament_unlocked(duel.tournament_id)


def test_the_unique_constraint_is_the_last_backstop(duel):
    """A drifted revision cannot make a second record of the same one."""
    episode = _open_episode(duel.match_id)
    _execute(
        'INSERT INTO lan_tournament_match_escalation_acks'
        ' (id, episode_id, tournament_id, match_id, actor_id, revision,'
        '  occurred_at, clock_us, comment)'
        ' VALUES (:id, :e, :t, :m, :a, 1, :at, :clock, NULL)',
        id=uuid7(),
        e=episode['id'],
        t=duel.tournament_id,
        m=duel.match_id,
        a=duel.ada.id,
        at=NOW - 30 * MINUTE,
        clock=CLOCK_AT_NOW_US - 30 * MINUTE_US,
    )
    facts = _facts(duel.match_id, duel.tournament_id)

    with pytest.raises(
        IntegrityError, match='uq_lan_tournament_escalation_ack'
    ):
        duel.acknowledge(duel.ada, when=NOW)

    assert _facts(duel.match_id, duel.tournament_id) == facts
    assert _open_episode(duel.match_id)['ack_revision'] == 0
    assert len(_acks(duel.match_id)) == 1
    _assert_tournament_unlocked(duel.tournament_id)


class Worker:
    """Runs one call in its own thread, application context and session."""

    def __init__(self, app, call) -> None:
        self.app = app
        self.call = call
        self.pid: int | None = None
        self.outcome = None
        self.error: BaseException | None = None
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name='ack-race', daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def _on_begin(self, session, transaction, connection) -> None:
        connection.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        )
        self.pid = connection.scalar(text('SELECT pg_backend_pid()'))

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


def _wait_until_blocked(connection, worker: Worker) -> None:
    """Wait until PostgreSQL reports the worker as waiting for a lock."""
    deadline = time.monotonic() + BLOCK_SECONDS
    while time.monotonic() < deadline:
        if worker.finished.is_set():
            break
        if worker.pid is not None:
            blockers = connection.scalar(
                text('SELECT pg_blocking_pids(:pid)'), {'pid': worker.pid}
            )
            if blockers:
                return
        time.sleep(0.05)

    raise AssertionError(
        'the worker was not blocked'
        f' (finished: {worker.finished.is_set()}, error: {worker.error!r})'
    )


def test_two_orgas_acknowledging_at_once_write_one_acknowledgement(
    duel, admin_app
):
    ada, bob = duel.ada, duel.bob
    form = duel.row(ada)
    duel.at(NOW)

    def acknowledge(viewer):
        return lambda: service.acknowledge_match(
            _viewer(viewer),
            duel.party_id,
            duel.match_id,
            expected_episode_id=form.episode_id,
            expected_ack_revision=form.ack_revision,
            comment=f'by {viewer.screen_name}',
        )

    with db.engine.connect() as holder:
        holder.execute(
            text('SELECT id FROM lan_tournaments WHERE id = :id FOR UPDATE'),
            {'id': duel.tournament_id},
        )
        workers = [
            Worker(admin_app, acknowledge(ada)),
            Worker(admin_app, acknowledge(bob)),
        ]
        try:
            for worker in workers:
                worker.start()
                _wait_until_blocked(holder, worker)
        finally:
            holder.rollback()

        outcomes = [worker.result() for worker in workers]

    # Both built their request from the same page, so one wins and the
    # other, reading afresh under the lock, finds it stale.
    winners = [o for o in outcomes if o.is_ok()]
    losers = [o for o in outcomes if o.is_err()]
    assert (len(winners), len(losers)) == (1, 1)
    assert losers[0].unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    (stored,) = _acks(duel.match_id)
    assert stored['id'] == winners[0].unwrap().id
    assert _open_episode(duel.match_id)['ack_revision'] == 1
    assert len(_ack_audit(duel.tournament_id)) == 1
    _assert_tournament_unlocked(duel.tournament_id)
