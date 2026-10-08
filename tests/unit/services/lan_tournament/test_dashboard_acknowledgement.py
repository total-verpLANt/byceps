import ast
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    tournament_dashboard_coordination_service as service,
    tournament_dashboard_service as dashboard_service,
)
from byceps.services.lan_tournament.blueprints import dashboard_forms
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    OperationalClock,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    AckUnavailableReason,
    DashboardAcknowledgementSummary,
    DashboardScope,
    DashboardSettings,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (
    clock_value_us,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


PARTY = PartyID('gv-36-acks')
MINUTE_US = 60_000_000
MINUTE = timedelta(minutes=1)
MICROSECOND = timedelta(microseconds=1)

# The tournament started three hours before `NOW`, and ran ever since.
START = datetime(2026, 10, 8, 9, 0, 0)
NOW = datetime(2026, 10, 8, 12, 0, 0)
CLOCK_AT_NOW_US = 180 * MINUTE_US
# The due episode opened twenty active minutes before `NOW`: yellow.
WAIT_MINUTES = 20
DEPLOYMENT = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

MODULE = Path(service.__file__)
ACK_PATH = (
    'acknowledge_match',
    '_acknowledge',
    '_lock_authorized_match',
    '_find_match_unlocked',
    '_refuse_unknown_match',
    '_refuse',
    '_clock_of',
    '_is_same_id',
    '_normalize_comment',
    '_is_plain_text',
)

# The only repository functions an acknowledgement may call. None of them
# stamps the last change, moves the clock, opens or closes an episode,
# writes a pin, a result or a Ready claim.
ALLOWED_REPOSITORY_CALLS = frozenset(
    {
        'find_match',
        'lock_tournament_for_update',
        'lock_matches_for_update',
        'find_match_fresh',
        'get_tournament',
        'find_open_due_episode',
        'find_latest_escalation_ack',
        'get_operation_time',
        'advance_episode_ack_revision_flush',
        'create_escalation_ack_flush',
        'commit_session',
        'rollback_session',
    }
)


def _user(name: str) -> User:
    return User(
        id=UserID(generate_uuid()),
        screen_name=name,
        initialized=True,
        suspended=False,
        deleted=False,
        avatar_url='',
    )


def _viewer(user: User) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset())


class Database:
    """Committed and staged rows, so a rollback has something to undo."""

    def __init__(self) -> None:
        self.episodes: dict[TournamentMatchID, MatchDueEpisode] = {}
        self.acks: list[MatchEscalationAcknowledgement] = []
        self.entries: list[dict] = []
        self._staged: tuple[dict, list, list] | None = None

    def staged(self) -> tuple[dict, list, list]:
        if self._staged is None:
            self._staged = (
                dict(self.episodes),
                list(self.acks),
                list(self.entries),
            )
        return self._staged

    def visible(self) -> tuple[dict, list, list]:
        if self._staged is not None:
            return self._staged

        return self.episodes, self.acks, self.entries

    def commit(self) -> None:
        if self._staged is not None:
            self.episodes, self.acks, self.entries = self._staged
        self._staged = None

    def rollback(self) -> None:
        self._staged = None

    def snapshot(self) -> tuple:
        return (dict(self.episodes), list(self.acks), list(self.entries))


class FakeRepository:
    """Offers exactly `ALLOWED_REPOSITORY_CALLS`; anything else is an error."""

    def __init__(self, world: 'World') -> None:
        self._world = world

    def find_match(self, match_id):
        self._world.calls.append('find_match')
        return self._world.matches.get(match_id)

    def lock_tournament_for_update(self, tournament_id):
        self._world.calls.append('lock_tournament_for_update')
        self._world.locked.append(tournament_id)

    def lock_matches_for_update(self, match_ids):
        self._world.calls.append('lock_matches_for_update')
        assert self._world.locked, 'a match was locked before its tournament'
        self._world.locked.extend(match_ids)

    def find_match_fresh(self, match_id):
        self._world.calls.append('find_match_fresh')
        assert match_id in self._world.locked
        return self._world.matches.get(match_id)

    def get_tournament(self, tournament_id, *, fresh=False):
        self._world.calls.append('get_tournament')
        assert fresh, 'the tournament must be read afresh after the lock'
        return self._world.tournaments[tournament_id]

    def find_open_due_episode(self, match_id):
        self._world.calls.append('find_open_due_episode')
        assert self._world.locked, 'an episode was read without the locks'
        return self._world.database.visible()[0].get(match_id)

    def find_latest_escalation_ack(self, episode_id):
        self._world.calls.append('find_latest_escalation_ack')
        assert self._world.locked, 'an ack was read without the locks'
        acks = [
            ack
            for ack in self._world.database.visible()[1]
            if ack.episode_id == episode_id
        ]
        return max(acks, key=lambda ack: ack.revision, default=None)

    def get_operation_time(self):
        self._world.calls.append('get_operation_time')
        return self._world.now

    def advance_episode_ack_revision_flush(
        self, episode_id, *, expected_revision
    ):
        self._world.calls.append('advance_episode_ack_revision_flush')
        self._world.advances.append((episode_id, expected_revision))
        if self._world.lose_next_advance:
            self._world.lose_next_advance = False
            return False

        episodes, _, _ = self._world.database.staged()
        for match_id, episode in episodes.items():
            if (
                episode.id == episode_id
                and episode.closed_at is None
                and episode.ack_revision == expected_revision
            ):
                episodes[match_id] = replace(
                    episode, ack_revision=expected_revision + 1
                )
                return True

        return False

    def create_escalation_ack_flush(self, ack):
        self._world.calls.append('create_escalation_ack_flush')
        self._world.saved.append(ack)
        _, acks, _ = self._world.database.staged()
        if self._world.ack_insert_error is not None:
            raise self._world.ack_insert_error
        assert not any(
            (a.episode_id, a.revision) == (ack.episode_id, ack.revision)
            for a in acks
        ), 'the unique constraint would refuse this acknowledgement'
        acks.append(ack)

    def commit_session(self):
        self._world.calls.append('commit_session')
        if self._world.commit_error is not None:
            raise self._world.commit_error
        self._world.database.commit()

    def rollback_session(self):
        self._world.calls.append('rollback_session')
        self._world.locked.clear()
        self._world.database.rollback()


class FakeLog:
    def __init__(self, world: 'World') -> None:
        self._world = world

    def create_log_entry(
        self, event_type, tournament_id, initiator_id, *, data=None, commit
    ):
        self._world.calls.append('create_log_entry')
        assert commit is False, 'the audit must be staged, not committed'
        if self._world.audit_error is not None:
            raise self._world.audit_error
        self._world.database.staged()[2].append(
            dict(
                event_type=event_type,
                tournament_id=tournament_id,
                initiator_id=initiator_id,
                data=data,
            )
        )


class World:
    """One party with one running tournament and one due match."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.locked: list = []
        self.advances: list[tuple] = []
        self.saved: list[MatchEscalationAcknowledgement] = []
        self.scope_requests: list[tuple] = []
        self.settings_requests: list[PartyID] = []
        self.now = NOW
        self.settings: DashboardSettings | Err = DEPLOYMENT
        self.lose_next_advance = False
        self.old_episode_id: MatchDueEpisodeID | None = None
        self.commit_error: Exception | None = None
        self.audit_error: Exception | None = None
        self.ack_insert_error: Exception | None = None
        self.database = Database()
        self.tournaments: dict[TournamentID, Tournament] = {}
        self.matches: dict[TournamentMatchID, TournamentMatch] = {}

        self.tournament = self.add_tournament()
        self.match = self.add_match(self.tournament)
        self.episode = self.open_episode(self.match)
        # Viewer ID -> (party, tournament IDs, is a global administrator).
        self.grants: dict[UserID, tuple[PartyID, tuple, bool]] = {}

    def add_tournament(
        self,
        status: TournamentStatus = TournamentStatus.ONGOING,
        *,
        clock: OperationalClock | None = None,
    ) -> Tournament:
        clock = clock or OperationalClock(
            elapsed_us=0, running_since=START, activated_at=START
        )
        tournament = Tournament(
            id=TournamentID(generate_uuid()),
            party_id=PARTY,
            name='Kupfer-Cup',
            game=None,
            description=None,
            image_url=None,
            ruleset=None,
            start_time=None,
            created_at=START,
            min_players=None,
            max_players=None,
            min_teams=None,
            max_teams=None,
            min_players_in_team=None,
            max_players_in_team=None,
            contestant_type=None,
            tournament_status=status,
            game_format=None,
            elimination_mode=None,
            operational_clock_elapsed_us=clock.elapsed_us,
            operational_clock_running_since=clock.running_since,
            operational_clock_activated_at=clock.activated_at,
        )
        self.tournaments[tournament.id] = tournament
        return tournament

    def add_match(
        self, tournament: Tournament, *, confirmed_by: UserID | None = None
    ) -> TournamentMatch:
        match = TournamentMatch(
            id=TournamentMatchID(generate_uuid()),
            tournament_id=tournament.id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=confirmed_by,
            created_at=START,
            occupied_since=START + 30 * MINUTE,
            last_changed_at=START + 40 * MINUTE,
        )
        self.matches[match.id] = match
        return match

    def open_episode(
        self,
        match: TournamentMatch,
        *,
        wait_minutes: float = WAIT_MINUTES,
        ack_revision: int = 0,
    ) -> MatchDueEpisode:
        episode = MatchDueEpisode(
            id=MatchDueEpisodeID(generate_uuid()),
            tournament_id=match.tournament_id,
            match_id=match.id,
            pairing_key='key',
            opened_at=NOW - timedelta(minutes=wait_minutes),
            opened_clock_us=CLOCK_AT_NOW_US - int(wait_minutes * MINUTE_US),
            ack_revision=ack_revision,
        )
        self.database.episodes[match.id] = episode
        return episode

    def orga(self, name: str, *tournaments: Tournament) -> CurrentUser:
        user = _user(name)
        self.grants[user.id] = (PARTY, tuple(t.id for t in tournaments), False)
        return _viewer(user)

    def admin(self, name: str = 'Admin') -> CurrentUser:
        user = _user(name)
        self.grants[user.id] = (PARTY, tuple(self.tournaments), True)
        return _viewer(user)

    def stranger(self, name: str = 'Stranger') -> CurrentUser:
        return _viewer(_user(name))

    def resolve_scope(self, viewer, party_id, requested_scope):
        self.calls.append('resolve_dashboard_scope')
        self.scope_requests.append((viewer.id, party_id, requested_scope))
        if not viewer.authenticated:
            return Err(dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR)

        granted_party, tournament_ids, is_admin = self.grants.get(
            viewer.id, (None, (), False)
        )
        if not is_admin and (granted_party != party_id or not tournament_ids):
            return Err(dashboard_service.DASHBOARD_FORBIDDEN_ERROR)

        return Ok(
            DashboardScope(
                user_id=viewer.id,
                party_id=party_id,
                kind='all' if is_admin else 'assigned',
                tournament_ids=tournament_ids if party_id == PARTY else (),
                is_global_admin=is_admin,
            )
        )

    def effective_settings(self, party_id):
        self.calls.append('get_effective_dashboard_settings')
        self.settings_requests.append(party_id)
        if isinstance(self.settings, Err):
            return self.settings

        return Ok(self.settings)

    def acknowledge(
        self,
        viewer,
        *,
        at: datetime | None = None,
        episode_id='current',
        revision='current',
        comment=None,
        match_id=None,
        party=PARTY,
    ):
        """Acknowledge the way a form built from the stored state would."""
        if at is not None:
            self.now = at
        stored = self.episode_state
        current_id = (
            stored.id
            if stored is not None
            else MatchDueEpisodeID(generate_uuid())
        )
        current_revision = stored.ack_revision if stored is not None else 0
        return service.acknowledge_match(
            viewer,
            party,
            self.match.id if match_id is None else match_id,
            expected_episode_id=(
                current_id if episode_id == 'current' else episode_id
            ),
            expected_ack_revision=(
                current_revision if revision == 'current' else revision
            ),
            comment=comment,
        )

    @property
    def episode_state(self) -> MatchDueEpisode | None:
        return self.database.episodes.get(self.match.id)

    @property
    def acks(self) -> list[MatchEscalationAcknowledgement]:
        return self.database.acks

    @property
    def audit_events(self) -> list[str]:
        return [entry['event_type'] for entry in self.database.entries]

    def pause(self, at: datetime) -> None:
        """Freeze the clock, as the status setter does."""
        tournament = self.tournaments[self.tournament.id]
        self.tournaments[tournament.id] = replace(
            tournament,
            tournament_status=TournamentStatus.PAUSED,
            operational_clock_elapsed_us=clock_value_us(
                self._clock(tournament), at
            ),
            operational_clock_running_since=None,
        )

    def resume(self, at: datetime) -> None:
        tournament = self.tournaments[self.tournament.id]
        self.tournaments[tournament.id] = replace(
            tournament,
            tournament_status=TournamentStatus.ONGOING,
            operational_clock_running_since=at,
        )

    @staticmethod
    def _clock(tournament: Tournament) -> OperationalClock:
        return OperationalClock(
            elapsed_us=tournament.operational_clock_elapsed_us,
            running_since=tournament.operational_clock_running_since,
            activated_at=tournament.operational_clock_activated_at,
        )

    def clock_us(self, at: datetime) -> int:
        return clock_value_us(
            self._clock(self.tournaments[self.tournament.id]), at
        )

    def forget_calls(self) -> None:
        self.calls.clear()


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(service, 'tournament_repository', FakeRepository(world))
    monkeypatch.setattr(service, 'tournament_log_service', FakeLog(world))
    monkeypatch.setattr(service, 'resolve_dashboard_scope', world.resolve_scope)
    monkeypatch.setattr(
        service, 'get_effective_dashboard_settings', world.effective_settings
    )
    return world


# -- the five named tests --


def test_ack_keeps_occupancy_and_total_wait(world):
    ada = world.orga('Ada', world.tournament)
    match_before = world.match
    episode_before = world.episode
    tournament_before = world.tournaments[world.tournament.id]
    clock_before = world.clock_us(NOW)
    total_wait_before = clock_before - episode_before.opened_clock_us
    assert total_wait_before == WAIT_MINUTES * MINUTE_US

    ack = world.acknowledge(ada, at=NOW, comment='Captains called').unwrap()

    # The only thing that moved is the revision of the episode; what the
    # episode says about when and at which clock value it opened is as it was.
    stored = world.episode_state
    assert stored == replace(episode_before, ack_revision=1)
    assert (stored.opened_at, stored.opened_clock_us) == (
        episode_before.opened_at,
        episode_before.opened_clock_us,
    )
    assert (stored.closed_at, stored.closed_clock_us) == (None, None)
    # The total active wait, read at the same moment, is what it was.
    assert world.clock_us(NOW) - stored.opened_clock_us == total_wait_before
    # Occupancy, last change and the clock are not written, and cannot be:
    # the double offers no function that does.
    assert world.matches[world.match.id] is match_before
    assert match_before.occupied_since == START + 30 * MINUTE
    assert match_before.last_changed_at == START + 40 * MINUTE
    assert world.tournaments[world.tournament.id] == tournament_before
    assert set(world.calls) - {
        'create_log_entry',
        'resolve_dashboard_scope',
        'get_effective_dashboard_settings',
    } <= (ALLOWED_REPOSITORY_CALLS)
    assert _repository_calls(ACK_PATH) <= ALLOWED_REPOSITORY_CALLS
    assert _referenced_names(ACK_PATH).isdisjoint(
        {
            'signals',
            'tournament_notification_service',
            'tournament_operational_service',
            'tournament_match_service',
            'tournament_score_service',
            'tournament_readiness_service',
            'touch_matches_last_changed_flush',
            'save_match_pin_flush',
            'open_due_episode_flush',
            'close_due_episodes_flush',
            'set_tournament_status_flush',
        }
    )
    # What it adds is one acknowledgement at the current clock value.
    assert world.acks == [ack]
    assert ack.clock_us == clock_before
    assert (ack.episode_id, ack.match_id, ack.tournament_id) == (
        episode_before.id,
        world.match.id,
        world.tournament.id,
    )
    assert (ack.revision, ack.occurred_at, ack.actor_id, ack.comment) == (
        1,
        NOW,
        ada.id,
        'Captains called',
    )


def test_ack_reescalates_after_active_time_only(world):
    ada = world.orga('Ada', world.tournament)
    assert world.acknowledge(ada, at=NOW).unwrap().clock_us == (CLOCK_AT_NOW_US)

    # Right after it the interval starts over: green, nothing to check.
    refused = world.acknowledge(ada, at=NOW)
    assert refused.unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )

    # Ten active minutes later: still green.
    ten = NOW + 10 * MINUTE
    assert world.acknowledge(ada, at=ten).unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )

    # The tournament pauses for an hour. Pausing and the hour count for
    # nothing, and nobody may acknowledge a paused match.
    world.pause(ten)
    after_pause = ten + 60 * MINUTE
    assert world.acknowledge(ada, at=after_pause).unwrap_err() == (
        service.DASHBOARD_ACK_PAUSED_ERROR
    )
    world.resume(after_pause)

    # Four minutes and fifty-nine seconds after the resume the match has
    # waited 14:59 of active time since the check, 74:59 by the wall.
    almost = after_pause + 5 * MINUTE - MICROSECOND
    assert world.acknowledge(ada, at=almost).unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )

    # At exactly 15:00 of active time it is yellow again.
    due_again = after_pause + 5 * MINUTE
    second = world.acknowledge(ada, at=due_again, comment='Again').unwrap()

    assert second.revision == 2
    assert second.clock_us == CLOCK_AT_NOW_US + 15 * MINUTE_US
    assert world.episode_state.ack_revision == 2
    # The new interval starts at that value, and a request in the same
    # instant is a duplicate of one that was just served.
    assert world.acknowledge(ada, at=due_again).unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )
    assert world.acknowledge(
        ada, at=due_again + 15 * MINUTE - MICROSECOND
    ).unwrap_err() == (service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR)
    # Left alone for 45 active minutes it is red, and that is acknowledged
    # as well.
    world.acknowledge(ada, at=due_again + 45 * MINUTE).unwrap()
    assert [ack.revision for ack in world.acks] == [1, 2, 3]
    assert world.audit_events == ['match-acknowledged'] * 3


# fmt: off
REFUSALS = [
    # id,                 episode,   revision,  setup,            error
    ('stale-revision',    'current', 0,         'acked-once',     service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('ahead',             'current', 2,         'acked-once',     service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('negative',          'current', -1,        'fresh',          service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('huge',              'current', 10**9,     'fresh',          service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('other-episode',     'other',   'current', 'fresh',          service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('malformed-episode', 'nonsense', 'current', 'fresh',         service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('no-episode',        None,      'current', 'fresh',          service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('replaced-episode',  'old',     'current', 'new-episode',    service.DASHBOARD_ACK_CONFLICT_ERROR),
    ('green-new',         'current', 'current', 'green',          service.DASHBOARD_ACK_BELOW_THRESHOLD_ERROR),
    ('green-acked',       'current', 'current', 'acked-once',     service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR),
    ('paused',            'current', 'current', 'paused',         service.DASHBOARD_ACK_PAUSED_ERROR),
    ('not-due',           'current', 'current', 'no-episode',     service.DASHBOARD_ACK_NOT_DUE_ERROR),
    ('unknown-clock',     'current', 'current', 'unknown-clock',  service.DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR),
    ('registration',      'current', 'current', 'registration',   service.DASHBOARD_ACK_NOT_DUE_ERROR),
    ('confirmed',         'current', 'current', 'confirmed',      service.DASHBOARD_ACK_TERMINAL_ERROR),
    ('completed',         'current', 'current', 'completed',      service.DASHBOARD_ACK_TERMINAL_ERROR),
    ('cancelled',         'current', 'current', 'cancelled',      service.DASHBOARD_ACK_TERMINAL_ERROR),
]
# fmt: on


def _arrange(world: World, setup: str, ada: CurrentUser) -> None:
    """Bring the world into the state one refusal case is about."""
    tournament = world.tournaments[world.tournament.id]
    if setup == 'acked-once':
        world.acknowledge(ada, at=NOW).unwrap()
        world.now = NOW + 5 * MINUTE
    elif setup == 'green':
        world.episode = world.open_episode(world.match, wait_minutes=14)
    elif setup == 'new-episode':
        world.old_episode_id = world.episode.id
        world.episode = world.open_episode(world.match)
    elif setup == 'paused':
        world.pause(NOW)
    elif setup == 'no-episode':
        del world.database.episodes[world.match.id]
    elif setup == 'unknown-clock':
        del world.database.episodes[world.match.id]
        world.tournaments[tournament.id] = replace(
            tournament, operational_clock_activated_at=None
        )
    elif setup == 'registration':
        world.tournaments[tournament.id] = replace(
            tournament, tournament_status=TournamentStatus.REGISTRATION_CLOSED
        )
    elif setup == 'confirmed':
        world.matches[world.match.id] = replace(
            world.match, confirmed_by=ada.id
        )
    elif setup in ('completed', 'cancelled'):
        world.tournaments[tournament.id] = replace(
            tournament, tournament_status=TournamentStatus[setup.upper()]
        )
    else:
        assert setup in ('fresh',)


@pytest.mark.parametrize(
    'episode, revision, setup, error',
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_stale_episode_revision_and_green_ack_refused(
    world, episode, revision, setup, error
):
    ada = world.orga('Ada', world.tournament)
    _arrange(world, setup, ada)
    if episode == 'other':
        episode = MatchDueEpisodeID(generate_uuid())
    elif episode == 'old':
        episode = world.old_episode_id
    before = world.database.snapshot()
    acks_before = len(world.acks)
    world.forget_calls()

    result = world.acknowledge(ada, episode_id=episode, revision=revision)

    assert result.unwrap_err() == error
    # Nothing was written, nothing was audited, and no lock is kept.
    assert world.database.snapshot() == before
    assert len(world.acks) == acks_before
    assert len(world.saved) == (1 if setup == 'acked-once' else 0)
    assert 'create_escalation_ack_flush' not in world.calls
    assert 'create_log_entry' not in world.calls
    assert 'commit_session' not in world.calls
    assert world.calls[-1] == 'rollback_session'
    assert world.locked == []


def test_a_duplicate_adds_no_second_ack_and_no_longer_quiet_time(world):
    ada = world.orga('Ada', world.tournament)
    bob = world.orga('Bob', world.tournament)

    first = world.acknowledge(ada, at=NOW, revision=0).unwrap()
    # Bob's form was built at the same time from the same state.
    later = NOW + 14 * MINUTE
    duplicate = world.acknowledge(
        bob, at=later, episode_id=first.episode_id, revision=0
    )

    assert duplicate.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert world.acks == [first]
    assert world.audit_events == ['match-acknowledged']
    assert world.episode_state.ack_revision == 1
    # The quiet time still runs from the first check, so the match is due
    # again 15 minutes after it, not 15 minutes after the duplicate.
    assert world.acknowledge(
        bob, at=NOW + 15 * MINUTE - MICROSECOND
    ).unwrap_err() == (service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR)
    assert world.acknowledge(bob, at=NOW + 15 * MINUTE).unwrap().revision == 2


def test_a_lost_compare_and_set_is_a_conflict_and_leaves_no_trace(world):
    ada = world.orga('Ada', world.tournament)
    world.lose_next_advance = True
    before = world.database.snapshot()

    result = world.acknowledge(ada, at=NOW)

    assert result.unwrap_err() == service.DASHBOARD_ACK_CONFLICT_ERROR
    assert world.database.snapshot() == before
    assert world.saved == []
    assert world.audit_events == []
    assert world.calls[-1] == 'rollback_session'
    # The advance is the first write, so a lost one writes no record.
    assert world.advances == [(world.episode.id, 0)]
    assert 'create_escalation_ack_flush' not in world.calls


def test_ack_audit_failure_rolls_back_every_fact(world):
    ada = world.orga('Ada', world.tournament)
    before = world.database.snapshot()
    world.audit_error = RuntimeError('audit unavailable')

    with pytest.raises(RuntimeError, match='audit unavailable'):
        world.acknowledge(ada, at=NOW, comment='Captains called')

    # The revision was raised and the record appended inside the
    # transaction, and both are gone.
    assert world.advances == [(world.episode.id, 0)]
    assert [ack.revision for ack in world.saved] == [1]
    assert world.database.snapshot() == before
    assert world.episode_state.ack_revision == 0
    assert world.acks == []
    assert world.calls[-2:] == ['create_log_entry', 'rollback_session']
    assert 'commit_session' not in world.calls
    assert world.locked == []

    # Without the failure the same request goes through: the failed one
    # left no baseline and no revision behind.
    world.audit_error = None
    again = world.acknowledge(ada, at=NOW).unwrap()
    assert (again.revision, again.clock_us) == (1, CLOCK_AT_NOW_US)


@pytest.mark.parametrize('failing', ['commit', 'insert'])
def test_a_failing_commit_or_insert_rolls_back_the_revision_too(world, failing):
    ada = world.orga('Ada', world.tournament)
    before = world.database.snapshot()
    if failing == 'commit':
        world.commit_error = RuntimeError('commit failed')
    else:
        world.ack_insert_error = RuntimeError('insert failed')

    with pytest.raises(RuntimeError, match='failed'):
        world.acknowledge(ada, at=NOW)

    assert world.database.snapshot() == before
    assert world.episode_state.ack_revision == 0
    assert world.calls[-1] == 'rollback_session'
    assert world.locked == []
    if failing == 'commit':
        # The audit entry was staged with the rest and is gone with it.
        assert 'create_log_entry' in world.calls
    else:
        assert 'create_log_entry' not in world.calls


def test_second_orga_sees_first_actor_time_comment_and_can_follow_up_after_reescalation(
    world,
):
    ada = world.orga('Ada', world.tournament)
    bob = world.orga('Bob', world.tournament)

    first = world.acknowledge(
        ada, at=NOW, comment='  Contacted both captains  '
    ).unwrap()

    # What Bob reads is the shared record: who, when, what.
    (stored,) = world.acks
    assert stored == first
    assert (stored.actor_id, stored.occurred_at, stored.comment) == (
        ada.id,
        NOW,
        'Contacted both captains',
    )
    # Bob has no acknowledgement to give for the same intervention.
    assert world.acknowledge(bob, at=NOW + 5 * MINUTE).unwrap_err() == (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
    )
    assert world.acknowledge(
        bob, at=NOW + 5 * MINUTE, revision=0
    ).unwrap_err() == (service.DASHBOARD_ACK_CONFLICT_ERROR)
    assert world.acks == [first]

    # Fifteen active minutes later it is escalated again, and Bob follows up.
    second = world.acknowledge(
        bob, at=NOW + 15 * MINUTE, comment='Player is on the way'
    ).unwrap()

    assert [(a.revision, a.actor_id, a.comment) for a in world.acks] == [
        (1, ada.id, 'Contacted both captains'),
        (2, bob.id, 'Player is on the way'),
    ]
    assert second.occurred_at == NOW + 15 * MINUTE
    assert first.id != second.id
    assert world.episode_state.ack_revision == 2
    # Ada's record is untouched by Bob's.
    assert world.acks[0] == first
    assert [e['initiator_id'] for e in world.database.entries] == [
        ada.id,
        bob.id,
    ]
    # It is a record of what somebody checked and not an assignment: no
    # owner, claim, recipient or personal flag in what is stored or shown.
    expected = {
        'id',
        'episode_id',
        'tournament_id',
        'match_id',
        'revision',
        'occurred_at',
        'clock_us',
        'actor_id',
        'comment',
    }
    assert {f.name for f in fields(MatchEscalationAcknowledgement)} == expected
    assert {f.name for f in fields(DashboardAcknowledgementSummary)} == {
        'id',
        'revision',
        'actor_display_name',
        'occurred_at',
        'comment',
    }


# -- authority and order --


def test_the_transaction_runs_in_the_documented_order(world):
    ada = world.orga('Ada', world.tournament)
    world.forget_calls()

    world.acknowledge(ada, at=NOW).unwrap()

    # Tournament, then match, then fresh reads and the authority under the
    # lock; the clock is sampled once, after them; one commit at the end.
    assert world.calls == [
        'find_match',
        'lock_tournament_for_update',
        'lock_matches_for_update',
        'find_match_fresh',
        'get_tournament',
        'resolve_dashboard_scope',
        'get_effective_dashboard_settings',
        'find_open_due_episode',
        'get_operation_time',
        'find_latest_escalation_ack',
        'advance_episode_ack_revision_flush',
        'create_escalation_ack_flush',
        'create_log_entry',
        'commit_session',
    ]
    assert world.scope_requests == [(ada.id, PARTY, 'all')]
    assert world.settings_requests == [PARTY]


def test_the_audit_entry_names_the_acknowledgement_and_not_the_comment(world):
    ada = world.orga('Ada', world.tournament)

    ack = world.acknowledge(ada, at=NOW, comment='Very private').unwrap()

    (entry,) = world.database.entries
    assert entry == dict(
        event_type='match-acknowledged',
        tournament_id=world.tournament.id,
        initiator_id=ada.id,
        data={
            'match_id': str(world.match.id),
            'episode_id': str(ack.episode_id),
            'revision': 1,
        },
    )
    assert service.MATCH_ACKNOWLEDGED_EVENT == 'match-acknowledged'


def test_the_alert_interval_follows_the_thresholds_of_the_party(world):
    ada = world.orga('Ada', world.tournament)
    # Twenty minutes waited: yellow at 15, still green at 30.
    world.settings = replace(DEPLOYMENT, yellow_minutes=30, red_minutes=60)

    refused = world.acknowledge(ada, at=NOW)

    assert refused.unwrap_err() == service.DASHBOARD_ACK_BELOW_THRESHOLD_ERROR
    assert world.acknowledge(ada, at=NOW + 10 * MINUTE - MICROSECOND).is_err()
    assert world.acknowledge(ada, at=NOW + 10 * MINUTE).unwrap().revision == 1
    assert world.settings_requests == [PARTY] * 3


def test_a_broken_configuration_refuses_and_releases_the_locks(world):
    ada = world.orga('Ada', world.tournament)
    world.settings = Err('invalid_dashboard_yellow_minutes')

    result = world.acknowledge(ada, at=NOW)

    assert result.unwrap_err() == 'invalid_dashboard_yellow_minutes'
    assert world.acks == []
    assert world.calls[-1] == 'rollback_session'
    assert world.locked == []


def test_an_administrator_without_an_assignment_may_acknowledge(world):
    admin = world.admin()

    ack = world.acknowledge(admin, at=NOW).unwrap()

    assert ack.actor_id == admin.id


# fmt: off
@pytest.mark.parametrize('who, error', [
    ('anonymous', dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR),
    ('stranger',  dashboard_service.DASHBOARD_FORBIDDEN_ERROR),
    ('sibling',   service.DASHBOARD_MATCH_NOT_FOUND_ERROR),
])
# fmt: on
def test_nobody_without_authority_over_the_tournament_acknowledges(
    world, who, error
):
    sibling = world.add_tournament()
    viewer = {
        'anonymous': CurrentUser.create_anonymous(None),
        'stranger': world.stranger(),
        'sibling': world.orga('Eve', sibling),
    }[who]
    before = world.database.snapshot()
    world.forget_calls()

    result = world.acknowledge(viewer, at=NOW)

    assert result.unwrap_err() == error
    assert world.database.snapshot() == before
    # No episode, clock or settings was read for somebody who may not know.
    assert 'find_open_due_episode' not in world.calls
    assert 'get_effective_dashboard_settings' not in world.calls
    assert 'get_operation_time' not in world.calls
    if who == 'anonymous':
        assert world.calls == []
    else:
        assert world.calls[-1] == 'rollback_session'
    assert world.locked == []


def test_a_forged_party_cannot_widen_the_scope(world):
    ada = world.orga('Ada', world.tournament)

    result = world.acknowledge(ada, at=NOW, party=PartyID('elsewhere'))

    assert result.unwrap_err() == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    assert world.acks == []


def test_an_unknown_match_looks_like_a_foreign_one(world):
    ada = world.orga('Ada', world.tournament)

    unknown = world.acknowledge(
        ada, at=NOW, match_id=TournamentMatchID(generate_uuid())
    )

    assert unknown.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    assert 'find_open_due_episode' not in world.calls
    assert world.calls[-1] == 'rollback_session'


@pytest.mark.parametrize('match_id', ['not-a-uuid', '', '12345', None])
def test_a_malformed_match_id_is_not_found_and_never_a_500(world, match_id):
    ada = world.orga('Ada', world.tournament)
    stored = world.episode_state

    result = service.acknowledge_match(
        ada,
        PARTY,
        match_id,
        expected_episode_id=stored.id,
        expected_ack_revision=0,
        comment=None,
    )

    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    assert world.acks == []


def test_a_string_match_and_episode_id_is_accepted(world):
    ada = world.orga('Ada', world.tournament)
    world.now = NOW

    result = service.acknowledge_match(
        ada,
        PARTY,
        str(world.match.id),
        expected_episode_id=str(world.episode.id),
        expected_ack_revision=0,
        comment=None,
    )

    assert result.unwrap().revision == 1


def test_a_terminal_match_gets_its_own_refusal_and_the_pin_keeps_its_own(world):
    ada = world.orga('Ada', world.tournament)
    world.matches[world.match.id] = replace(world.match, confirmed_by=ada.id)

    acknowledged = world.acknowledge(ada, at=NOW)
    pinned = service.set_match_pin(
        ada, PARTY, world.match.id, pinned=True, expected_revision=0
    )

    assert acknowledged.unwrap_err() == service.DASHBOARD_ACK_TERMINAL_ERROR
    assert pinned.unwrap_err() == service.DASHBOARD_MATCH_TERMINAL_ERROR
    assert (
        service.DASHBOARD_ACK_TERMINAL_ERROR
        != service.DASHBOARD_MATCH_TERMINAL_ERROR
    )


# -- the comment --


# fmt: off
COMMENTS = [
    # id,              given,                              stored
    ('none',           None,                               None),
    ('empty',          '',                                 None),
    ('blank',          '   \t ',                           None),
    ('newlines',       '\n\r\n',                           None),
    ('stripped',       '  checked  ',                      'checked'),
    ('inner-newline',  'a\nb',                             'a\nb'),
    ('crlf',           'a\r\nb\rc',                        'a\nb\nc'),
    ('inner-tab',      'a\tb',                             'a\tb'),
    ('markup',         '<b>x</b> & "y" {{z}}',             '<b>x</b> & "y" {{z}}'),
    ('umlauts',        'Zuspätkommer: Müller',             'Zuspätkommer: Müller'),
    ('exactly-500',    'x' * 500,                          'x' * 500),
    ('500-after-strip', ' ' + 'x' * 500 + ' ',             'x' * 500),
]
# fmt: on


@pytest.mark.parametrize(
    'given, stored',
    [case[1:] for case in COMMENTS],
    ids=[case[0] for case in COMMENTS],
)
def test_a_comment_is_stripped_and_stored_as_plain_text(world, given, stored):
    ada = world.orga('Ada', world.tournament)

    ack = world.acknowledge(ada, at=NOW, comment=given).unwrap()

    assert ack.comment == stored
    assert world.acks[0].comment == stored


# fmt: off
BAD_COMMENTS = [
    ('too-long',       'x' * 501),
    ('way-too-long',   'x' * 5000),
    ('nul',            'a\x00b'),
    ('escape',         'a\x1bb'),
    ('bell',           'a\x07b'),
    ('delete',         'a\x7fb'),
    ('c1',             'a\x85b'),
    ('line-separator', 'a\u2028b'),
    ('para-separator', 'a\u2029b'),
    ('bidi-override',  'a\u202eb'),
    ('bidi-isolate',   'a\u2066b'),
    ('lone-surrogate', 'a\ud800b'),
    ('not-text',       b'bytes'),
    ('number',         12),
    ('list',           ['a']),
]
# fmt: on


@pytest.mark.parametrize(
    'given',
    [case[1] for case in BAD_COMMENTS],
    ids=[case[0] for case in BAD_COMMENTS],
)
def test_a_comment_that_is_not_plain_text_of_bounded_length_is_refused(
    world, given
):
    ada = world.orga('Ada', world.tournament)
    before = world.database.snapshot()

    result = world.acknowledge(ada, at=NOW, comment=given)

    assert result.unwrap_err() == service.DASHBOARD_ACK_COMMENT_INVALID_ERROR
    assert world.database.snapshot() == before
    assert world.saved == []
    assert world.calls[-1] == 'rollback_session'
    assert world.locked == []


def test_a_comment_is_judged_after_the_authority_and_before_the_state(world):
    stranger = world.stranger()
    ada = world.orga('Ada', world.tournament)

    # Somebody without authority learns nothing from the comment check.
    assert world.acknowledge(
        stranger, at=NOW, comment='x' * 501
    ).unwrap_err() == (dashboard_service.DASHBOARD_FORBIDDEN_ERROR)
    # An invalid comment does not reach the episode.
    world.forget_calls()
    assert world.acknowledge(ada, at=NOW, comment='x' * 501).unwrap_err() == (
        service.DASHBOARD_ACK_COMMENT_INVALID_ERROR
    )
    assert 'find_open_due_episode' not in world.calls


def test_the_comment_rule_equals_the_one_of_the_form():
    samples = [
        'plain',
        'a\nb\tc',
        'a\x00b',
        'a\x1bb',
        'a\x7fb',
        'a\x85b',
        'a\u2028b',
        'a\u2029b',
        'a\u202ab',
        'a\u202eb',
        'a\u2066b',
        'a\u2069b',
        'a\u200bb',
        'a\ufeffb',
        'a\ud800b',
        'Zuspätkommer',
        '\U0001f600',
    ]

    for sample in samples:
        assert service._is_plain_text(sample) is (
            dashboard_forms._is_plain_text(sample)
        ), repr(sample)
    assert service.MAX_COMMENT_LENGTH == dashboard_forms.MAX_COMMENT_LENGTH


# -- the codes --


def test_every_reason_the_read_side_gives_has_one_refusal():
    codes = {
        value
        for name, value in vars(service).items()
        if name.startswith('DASHBOARD_ACK_') and name.endswith('_ERROR')
    }

    for reason in AckUnavailableReason:
        assert f'dashboard_ack_{reason.value}' in codes
    assert codes - {
        f'dashboard_ack_{r.value}' for r in AckUnavailableReason
    } == {
        'dashboard_ack_conflict',
        'dashboard_ack_comment_invalid',
    }
    assert len(codes) == len(
        [name for name in vars(service) if name.startswith('DASHBOARD_ACK_')]
    )


def test_the_error_codes_are_module_constants_and_never_inline_literals():
    literals = [
        node.args[0].value
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == 'Err'
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]

    assert literals == []
    assert service.DASHBOARD_ACK_CONFLICT_ERROR == 'dashboard_ack_conflict'
    assert service.DASHBOARD_ACK_TERMINAL_ERROR == 'dashboard_ack_terminal'
    assert service.DASHBOARD_ACK_PAUSED_ERROR == 'dashboard_ack_paused'
    assert service.DASHBOARD_ACK_NOT_DUE_ERROR == 'dashboard_ack_not_due'
    assert (
        service.DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR
        == 'dashboard_ack_clock_unknown'
    )
    assert (
        service.DASHBOARD_ACK_BELOW_THRESHOLD_ERROR
        == 'dashboard_ack_below_threshold'
    )
    assert (
        service.DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
        == 'dashboard_ack_recently_acknowledged'
    )
    assert (
        service.DASHBOARD_ACK_COMMENT_INVALID_ERROR
        == 'dashboard_ack_comment_invalid'
    )


def test_the_acknowledgement_commits_once_through_the_repository_only():
    acknowledge = _function(MODULE, '_acknowledge')
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(acknowledge)
        if isinstance(node, ast.Call)
    ]

    assert calls.count('tournament_repository.commit_session') == 1
    assert 'db.session.commit' not in calls
    # The audit entry is staged before that one commit.
    order = [call for call in _calls_in_order(acknowledge)]
    assert order.index('create_log_entry') < order.index('commit_session')
    assert order.index('create_escalation_ack_flush') < order.index(
        'create_log_entry'
    )
    assert order.index('advance_episode_ack_revision_flush') < order.index(
        'create_escalation_ack_flush'
    )
    for node in ast.walk(acknowledge):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
            'create_log_entry'
        ):
            assert [
                ast.literal_eval(keyword.value)
                for keyword in node.keywords
                if keyword.arg == 'commit'
            ] == [False]
    # The clock is sampled once.
    assert calls.count('tournament_repository.get_operation_time') == 1


def test_a_failure_anywhere_rolls_back_and_is_raised():
    public = _function(MODULE, 'acknowledge_match')
    handlers = [
        node for node in ast.walk(public) if isinstance(node, ast.ExceptHandler)
    ]

    assert len(handlers) == 1
    (handler,) = handlers
    assert ast.unparse(handler.type) == 'Exception'
    assert [ast.unparse(statement) for statement in handler.body] == [
        'tournament_repository.rollback_session()',
        'raise',
    ]


# -- helpers --


def _tree() -> ast.Module:
    return ast.parse(MODULE.read_text())


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in {path}')


def _calls_in_order(function: ast.FunctionDef) -> list[str]:
    calls = [
        (
            node.lineno,
            node.col_offset,
            ast.unparse(node.func).rpartition('.')[2],
        )
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    ]
    return [name for _, _, name in sorted(calls)]


def _repository_calls(names) -> set[str]:
    found = set()
    for name in names:
        for node in ast.walk(_function(MODULE, name)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == 'tournament_repository'
            ):
                found.add(node.attr)
    return found


def _referenced_names(names) -> set[str]:
    found = set()
    for name in names:
        for node in ast.walk(_function(MODULE, name)):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
    return found
