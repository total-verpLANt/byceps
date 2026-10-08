import ast
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    tournament_dashboard_coordination_service as service,
    tournament_dashboard_service as dashboard_service,
    tournament_orga_service,
)
from byceps.services.lan_tournament.models.operational_timing import (
    MatchPinState,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardScope,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrga,
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


PARTY = PartyID('gv-36-pins')
OTHER_PARTY = PartyID('gv-36-elsewhere')
CLOCK = datetime(2026, 10, 8, 12, 0, 0)

MODULE = Path(service.__file__)
PIN_PATH = (
    'set_match_pin',
    '_lock_authorized_match',
    '_find_match_unlocked',
    '_refuse_unknown_match',
    '_refuse',
)

# The only repository functions a pin may call. None of them stamps the
# last change, moves or reads a clock, opens or closes an episode, or
# writes an acknowledgement.
ALLOWED_REPOSITORY_CALLS = frozenset(
    {
        'find_match',
        'lock_tournament_for_update',
        'lock_matches_for_update',
        'find_match_fresh',
        'get_tournament',
        'find_match_pin_state',
        'get_operation_time',
        'save_match_pin_flush',
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
        self.pins: dict[TournamentMatchID, MatchPinState] = {}
        self.entries: list[dict] = []
        self._staged: tuple[dict, list] | None = None

    def staged(self) -> tuple[dict, list]:
        if self._staged is None:
            self._staged = (dict(self.pins), list(self.entries))
        return self._staged

    def visible(self) -> tuple[dict, list]:
        if self._staged is not None:
            return self._staged

        return self.pins, self.entries

    def commit(self) -> None:
        if self._staged is not None:
            self.pins, self.entries = self._staged
        self._staged = None

    def rollback(self) -> None:
        self._staged = None


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
        if self._world.vanish_on_lock:
            for match_id in match_ids:
                self._world.matches.pop(match_id, None)

    def find_match_fresh(self, match_id):
        self._world.calls.append('find_match_fresh')
        assert match_id in self._world.locked
        return self._world.matches.get(match_id)

    def get_tournament(self, tournament_id, *, fresh=False):
        self._world.calls.append('get_tournament')
        assert fresh, 'the tournament must be read afresh after the lock'
        return self._world.tournaments[tournament_id]

    def find_match_pin_state(self, match_id):
        self._world.calls.append('find_match_pin_state')
        return self._world.database.visible()[0].get(match_id)

    def get_operation_time(self):
        self._world.calls.append('get_operation_time')
        self._world.clock += timedelta(seconds=1)
        return self._world.clock

    def save_match_pin_flush(
        self,
        match_id,
        tournament_id,
        *,
        pinned_at,
        pinned_by,
        updated_at,
        updated_by,
        expected_revision,
    ):
        self._world.calls.append('save_match_pin_flush')
        self._world.saves.append(
            dict(
                match_id=match_id,
                tournament_id=tournament_id,
                pinned_at=pinned_at,
                pinned_by=pinned_by,
                updated_at=updated_at,
                updated_by=updated_by,
                expected_revision=expected_revision,
            )
        )
        if self._world.lose_next_save:
            self._world.lose_next_save = False
            return None

        pins, _ = self._world.database.staged()
        current = pins.get(match_id)
        if expected_revision == 0:
            if current is not None:
                return None
        elif current is None or current.revision != expected_revision:
            return None

        state = MatchPinState(
            match_id=match_id,
            tournament_id=tournament_id,
            revision=expected_revision + 1,
            pinned_at=pinned_at,
            pinned_by=pinned_by,
            updated_at=updated_at,
            updated_by=updated_by,
        )
        pins[match_id] = state
        return state

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
        self._world.database.staged()[1].append(
            dict(
                event_type=event_type,
                tournament_id=tournament_id,
                initiator_id=initiator_id,
                data=data,
            )
        )


class World:
    """One party with one tournament and one open match."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.locked: list = []
        self.saves: list[dict] = []
        self.scope_requests: list[tuple] = []
        self.clock = CLOCK
        self.lose_next_save = False
        self.vanish_on_lock = False
        self.commit_error: Exception | None = None
        self.audit_error: Exception | None = None
        self.database = Database()
        self.tournaments: dict[TournamentID, Tournament] = {}
        self.matches: dict[TournamentMatchID, TournamentMatch] = {}

        self.tournament = self.add_tournament()
        self.match = self.add_match(self.tournament)
        # Viewer ID -> (party, tournament IDs, is a global administrator).
        self.grants: dict[UserID, tuple[PartyID, tuple, bool]] = {}

    def add_tournament(
        self, status: TournamentStatus = TournamentStatus.ONGOING
    ) -> Tournament:
        tournament = Tournament(
            id=TournamentID(generate_uuid()),
            party_id=PARTY,
            name='Kupfer-Cup',
            game=None,
            description=None,
            image_url=None,
            ruleset=None,
            start_time=None,
            created_at=CLOCK,
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
            created_at=CLOCK,
        )
        self.matches[match.id] = match
        return match

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

    def pin(
        self,
        viewer,
        *,
        pinned=True,
        expected=0,
        match_id=None,
        party=PARTY,
    ):
        return service.set_match_pin(
            viewer,
            party,
            self.match.id if match_id is None else match_id,
            pinned=pinned,
            expected_revision=expected,
        )

    @property
    def state(self) -> MatchPinState | None:
        return self.database.pins.get(self.match.id)

    @property
    def audit_events(self) -> list[str]:
        return [entry['event_type'] for entry in self.database.entries]

    def forget_calls(self) -> None:
        self.calls.clear()


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(service, 'tournament_repository', FakeRepository(world))
    monkeypatch.setattr(service, 'tournament_log_service', FakeLog(world))
    monkeypatch.setattr(service, 'resolve_dashboard_scope', world.resolve_scope)
    return world


# -- the five named tests --


def test_shared_pin_survives_reload_and_unpin(world):
    ada = world.orga('Ada', world.tournament)
    bob = world.orga('Bob', world.tournament)
    admin = world.admin()

    pinned = world.pin(ada, expected=0).unwrap()

    assert (pinned.revision, pinned.pinned_by, pinned.updated_by) == (
        1,
        ada.id,
        ada.id,
    )
    assert pinned.pinned_at is not None
    # Committed: a reload reads the same state through the storage.
    assert world.state == pinned
    assert world.calls[-1] == 'commit_session'

    # The pin belongs to the team, not to the orga who set it.
    unpinned = world.pin(bob, pinned=False, expected=1).unwrap()

    assert (unpinned.revision, unpinned.pinned_at, unpinned.pinned_by) == (
        2,
        None,
        None,
    )
    assert unpinned.updated_by == bob.id
    assert world.state == unpinned

    repinned = world.pin(admin, expected=2).unwrap()

    assert (repinned.revision, repinned.pinned_by) == (3, admin.id)
    assert world.state == repinned
    assert world.audit_events == [
        'match-pinned',
        'match-unpinned',
        'match-pinned',
    ]
    assert [e['initiator_id'] for e in world.database.entries] == [
        ada.id,
        bob.id,
        admin.id,
    ]
    assert [e['data']['revision'] for e in world.database.entries] == [1, 2, 3]
    assert all(
        e['data']['match_id'] == str(world.match.id)
        for e in world.database.entries
    )


# fmt: off
CAS_CASES = [
    # id,              stored,         pin?,  expected, outcome,    revision
    ('pin-new',        None,           True,  0,        'written',  1),
    ('unpin-new',      None,           False, 0,        'noop',     None),
    ('pin-new-r1',     None,           True,  1,        'conflict', None),
    ('pin-new-neg',    None,           True,  -1,       'conflict', None),
    ('unpin-new-r1',   None,           False, 1,        'conflict', None),
    ('pin-pinned',     (1, True),      True,  1,        'noop',     1),
    ('pin-pinned-dup', (1, True),      True,  0,        'conflict', 1),
    ('pin-pinned-new', (1, True),      True,  2,        'conflict', 1),
    ('unpin-pinned',   (1, True),      False, 1,        'written',  2),
    ('unpin-stale',    (1, True),      False, 0,        'conflict', 1),
    ('unpin-ahead',    (1, True),      False, 2,        'conflict', 1),
    ('unpin-unpinned', (2, False),     False, 2,        'noop',     2),
    ('unpin-old',      (2, False),     False, 1,        'conflict', 2),
    ('pin-unpinned',   (2, False),     True,  2,        'written',  3),
    ('pin-unpinned-old', (2, False),   True,  1,       'conflict', 2),
]
# fmt: on


@pytest.mark.parametrize(
    'stored, pin, expected, outcome, revision',
    [case[1:] for case in CAS_CASES],
    ids=[case[0] for case in CAS_CASES],
)
def test_pin_cas_and_noop_do_not_duplicate(
    world, stored, pin, expected, outcome, revision
):
    ada = world.orga('Ada', world.tournament)
    before = None
    if stored is not None:
        stored_revision, stored_pinned = stored
        before = MatchPinState(
            match_id=world.match.id,
            tournament_id=world.tournament.id,
            revision=stored_revision,
            pinned_at=CLOCK if stored_pinned else None,
            pinned_by=ada.id if stored_pinned else None,
            updated_at=CLOCK,
            updated_by=ada.id,
        )
        world.database.pins[world.match.id] = before
    world.forget_calls()

    result = world.pin(ada, pinned=pin, expected=expected)

    if outcome == 'conflict':
        assert result.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    else:
        assert result.is_ok()
    # Only a written pin saves, audits and commits; everything else
    # writes nothing and gives the locks back.
    written = outcome == 'written'
    assert world.calls.count('save_match_pin_flush') == (1 if written else 0)
    assert ('create_log_entry' in world.calls) is written
    assert ('commit_session' in world.calls) is written
    assert ('rollback_session' in world.calls) is not written
    assert len(world.database.entries) == (1 if written else 0)
    if written:
        assert result.unwrap() == world.state
        assert world.state.revision == revision
    else:
        # Untouched, down to the timestamps.
        assert world.state == before
        if outcome == 'noop':
            assert result.unwrap() == before


def test_a_duplicate_request_adds_neither_a_pin_nor_an_audit_entry(world):
    ada = world.orga('Ada', world.tournament)

    first = world.pin(ada, expected=0)
    duplicate = world.pin(ada, expected=0)

    assert first.unwrap().revision == 1
    assert duplicate.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    assert world.state.revision == 1
    assert world.audit_events == ['match-pinned']
    assert len(world.saves) == 1


def test_a_lost_compare_and_set_is_a_conflict_and_leaves_no_trace(world):
    ada = world.orga('Ada', world.tournament)
    world.lose_next_save = True

    result = world.pin(ada, expected=0)

    assert result.unwrap_err() == service.DASHBOARD_PIN_CONFLICT_ERROR
    assert world.state is None
    assert world.audit_events == []
    assert world.calls[-1] == 'rollback_session'
    assert 'commit_session' not in world.calls


def test_pin_cannot_change_match_clock_or_last_change(world):
    ada = world.orga('Ada', world.tournament)

    world.pin(ada, expected=0).unwrap()
    world.pin(ada, expected=1).unwrap()  # no-op
    assert world.pin(ada, expected=0).is_err()  # stale
    world.pin(ada, pinned=False, expected=1).unwrap()
    world.pin(ada, expected=2).unwrap()

    # The double accepts nothing but the whitelisted functions, so a
    # stamp, a clock or an episode write would have failed above. The
    # source says the same, for every function a pin reaches.
    assert set(world.calls) - {
        'create_log_entry',
        'resolve_dashboard_scope',
    } <= (ALLOWED_REPOSITORY_CALLS)
    assert _repository_calls(PIN_PATH) <= ALLOWED_REPOSITORY_CALLS
    assert _referenced_names(PIN_PATH).isdisjoint(
        {
            'signals',
            'tournament_notification_service',
            'tournament_operational_service',
            'tournament_match_service',
            'tournament_score_service',
        }
    )

    # What is saved is the pin: no match, clock or episode value.
    assert [set(save) for save in world.saves] == [
        {
            'match_id',
            'tournament_id',
            'pinned_at',
            'pinned_by',
            'updated_at',
            'updated_by',
            'expected_revision',
        }
    ] * 3
    for save in world.saves:
        assert (
            save['updated_at'] == save['pinned_at'] or save['pinned_at'] is None
        )
        assert save['updated_by'] == ada.id


def test_pin_audit_failure_rolls_back(world):
    ada = world.orga('Ada', world.tournament)

    # A new pin: the row was saved, then the audit failed.
    world.audit_error = RuntimeError('audit unavailable')
    with pytest.raises(RuntimeError, match='audit unavailable'):
        world.pin(ada, expected=0)

    assert world.calls.index('save_match_pin_flush') < world.calls.index(
        'create_log_entry'
    )
    assert world.calls[-1] == 'rollback_session'
    assert 'commit_session' not in world.calls
    assert world.state is None
    assert world.audit_events == []

    # A change of an existing pin rolls back to the earlier one.
    world.audit_error = None
    pinned = world.pin(ada, expected=0).unwrap()
    world.forget_calls()
    world.audit_error = RuntimeError('audit unavailable')
    with pytest.raises(RuntimeError, match='audit unavailable'):
        world.pin(ada, pinned=False, expected=1)

    assert world.calls[-1] == 'rollback_session'
    assert 'commit_session' not in world.calls
    assert world.state == pinned
    assert world.audit_events == ['match-pinned']

    # A commit that fails is rolled back as well.
    world.audit_error = None
    world.commit_error = RuntimeError('commit failed')
    with pytest.raises(RuntimeError, match='commit failed'):
        world.pin(ada, pinned=False, expected=1)

    assert world.calls[-1] == 'rollback_session'
    assert world.state == pinned
    assert world.audit_events == ['match-pinned']


# `revoke_orga`, with the storage and the log replaced by one recorder.
ORGA_PATH = Path(tournament_orga_service.__file__)
TOURNAMENT_ID = TournamentID(generate_uuid())


def _revoke(*, assigned: bool):
    log: list[str] = []

    def record(name, result=None):
        def call(*args, **kwargs):
            log.append(name)
            return result

        return call

    orga = TournamentOrga(
        id=TournamentOrgaID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        user_id=UserID(generate_uuid()),
        assigned_at=CLOCK,
        assigned_by_id=None,
        duties=None,
    )
    tournament_repository = Mock(
        lock_tournament_for_update=record('lock_tournament_for_update'),
        rollback_session=record('rollback_session'),
    )
    orga_repository = Mock(
        find_orga_for_tournament_and_user=record(
            'find_orga_for_tournament_and_user', orga if assigned else None
        ),
        delete_orga=record('delete_orga'),
    )
    log_service = Mock(create_log_entry=record('create_log_entry'))
    db = Mock()
    db.session.commit.side_effect = record('commit')
    with (
        patch.object(tournament_orga_service, 'db', db),
        patch.object(tournament_orga_service, 'signals', Mock()),
        patch.object(
            tournament_orga_service,
            'tournament_repository',
            tournament_repository,
        ),
        patch.object(
            tournament_orga_service,
            'tournament_orga_repository',
            orga_repository,
        ),
        patch.object(
            tournament_orga_service, 'tournament_log_service', log_service
        ),
    ):
        result = tournament_orga_service.revoke_orga(
            TOURNAMENT_ID, orga.user_id, UserID(generate_uuid())
        )

    return result, log


def test_revoke_orga_takes_tournament_lock_first():
    result, log = _revoke(assigned=True)

    assert result.is_ok()
    assert log == [
        'lock_tournament_for_update',
        'find_orga_for_tournament_and_user',
        'delete_orga',
        'create_log_entry',
        'commit',
    ]

    # An unknown assignment still gives the lock back at once.
    result, log = _revoke(assigned=False)

    assert result.is_err()
    assert log == [
        'lock_tournament_for_update',
        'find_orga_for_tournament_and_user',
        'rollback_session',
    ]

    # In the source too: nothing that reads or deletes the assignment
    # precedes the lock, which is the first statement of the operation.
    function = _function(ORGA_PATH, 'revoke_orga')
    calls = _calls_in_order(function)
    assert calls[0] == 'lock_tournament_for_update'
    assert calls.index('lock_tournament_for_update') < min(
        calls.index('find_orga_for_tournament_and_user'),
        calls.index('delete_orga'),
        calls.index('create_log_entry'),
    )


# -- authority --


def test_authority_is_read_under_the_lock_and_scope_is_asked_for_the_party(
    world,
):
    ada = world.orga('Ada', world.tournament)

    world.pin(ada, expected=0).unwrap()

    assert world.calls == [
        'find_match',
        'lock_tournament_for_update',
        'lock_matches_for_update',
        'find_match_fresh',
        'get_tournament',
        'resolve_dashboard_scope',
        'find_match_pin_state',
        'get_operation_time',
        'save_match_pin_flush',
        'create_log_entry',
        'commit_session',
    ]
    # A scoped orga is clamped by the service itself: ask for the widest.
    assert world.scope_requests == [(ada.id, PARTY, 'all')]
    assert world.locked == [world.tournament.id, world.match.id]


@pytest.mark.parametrize(
    'who, error',
    [
        ('anonymous', dashboard_service.DASHBOARD_UNAUTHENTICATED_ERROR),
        ('stranger', dashboard_service.DASHBOARD_FORBIDDEN_ERROR),
        ('other_tournament', service.DASHBOARD_MATCH_NOT_FOUND_ERROR),
        ('revoked', dashboard_service.DASHBOARD_FORBIDDEN_ERROR),
    ],
)
def test_nobody_without_authority_over_the_tournament_pins(world, who, error):
    elsewhere = world.add_tournament()
    viewer = {
        'anonymous': CurrentUser.create_anonymous(None),
        'stranger': world.stranger(),
        'other_tournament': world.orga('Eve', elsewhere),
        'revoked': world.orga('Rex'),
    }[who]

    result = world.pin(viewer, expected=0)

    assert result.unwrap_err() == error
    assert world.state is None
    assert 'save_match_pin_flush' not in world.calls
    assert 'create_log_entry' not in world.calls
    assert 'commit_session' not in world.calls
    if who == 'anonymous':
        # Refused before anything is read or locked.
        assert world.calls == []
    else:
        assert world.calls[-1] == 'rollback_session'
        assert world.locked == []


def test_a_hidden_party_value_cannot_widen_the_scope(world):
    ada = world.orga('Ada', world.tournament)
    admin = world.admin()

    for viewer in (ada, admin):
        result = world.pin(viewer, expected=0, party=OTHER_PARTY)

        assert result.is_err()
        assert result.unwrap_err() in {
            service.DASHBOARD_MATCH_NOT_FOUND_ERROR,
            dashboard_service.DASHBOARD_FORBIDDEN_ERROR,
        }
        assert world.state is None


def test_an_unknown_match_looks_like_a_foreign_one(world):
    ada = world.orga('Ada', world.tournament)
    stranger = world.stranger()
    unknown = TournamentMatchID(generate_uuid())
    foreign = world.add_match(world.add_tournament())

    unknown_result = world.pin(ada, match_id=unknown)
    foreign_result = world.pin(ada, match_id=foreign.id)

    assert (
        unknown_result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    )
    assert (
        foreign_result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    )
    # Without any authority the answer is the one for the collection.
    assert (
        world.pin(stranger, match_id=unknown).unwrap_err()
        == dashboard_service.DASHBOARD_FORBIDDEN_ERROR
    )
    assert world.database.pins == {}


@pytest.mark.parametrize('match_id', ['not-a-uuid', '', '12345', None])
def test_a_malformed_match_id_is_not_found_and_never_a_500(world, match_id):
    ada = world.orga('Ada', world.tournament)

    result = world.pin(
        ada, match_id=match_id if match_id is not None else 'None'
    )

    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    assert 'lock_tournament_for_update' not in world.calls


def test_a_string_match_id_is_coerced_before_the_lookup(world):
    ada = world.orga('Ada', world.tournament)

    result = world.pin(ada, match_id=str(world.match.id))

    assert result.is_ok()
    assert result.unwrap().match_id == world.match.id


def test_a_match_deleted_while_waiting_for_the_lock_is_not_found(world):
    ada = world.orga('Ada', world.tournament)
    world.vanish_on_lock = True

    result = world.pin(ada)

    # The unlocked read still saw it; the read after the lock did not.
    assert world.calls[0] == 'find_match'
    assert result.unwrap_err() == service.DASHBOARD_MATCH_NOT_FOUND_ERROR
    assert world.state is None
    assert world.calls[-1] == 'rollback_session'


@pytest.mark.parametrize(
    'status, confirmed',
    [
        (TournamentStatus.ONGOING, True),
        (TournamentStatus.PAUSED, True),
        (TournamentStatus.COMPLETED, False),
        (TournamentStatus.CANCELLED, False),
        (TournamentStatus.COMPLETED, True),
    ],
)
def test_terminal_matches_and_tournaments_cannot_be_pinned_or_unpinned(
    world, status, confirmed
):
    ada = world.orga('Ada')
    tournament = world.add_tournament(status)
    match = world.add_match(
        tournament, confirmed_by=ada.id if confirmed else None
    )
    world.grants[ada.id] = (PARTY, (tournament.id,), False)
    # A pin set before the match ended stays, read only.
    earlier = MatchPinState(
        match_id=match.id,
        tournament_id=tournament.id,
        revision=1,
        pinned_at=CLOCK,
        pinned_by=ada.id,
        updated_at=CLOCK,
        updated_by=ada.id,
    )
    world.database.pins[match.id] = earlier

    for pinned in (True, False):
        result = world.pin(ada, pinned=pinned, expected=1, match_id=match.id)

        assert result.unwrap_err() == service.DASHBOARD_MATCH_TERMINAL_ERROR

    assert world.database.pins[match.id] == earlier
    assert world.database.entries == []
    assert 'save_match_pin_flush' not in world.calls


@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.DRAFT,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
    ],
)
def test_matches_of_every_running_or_upcoming_tournament_can_be_pinned(
    world, status
):
    ada = world.orga('Ada')
    tournament = world.add_tournament(status)
    match = world.add_match(tournament)
    world.grants[ada.id] = (PARTY, (tournament.id,), False)

    assert world.pin(ada, match_id=match.id).is_ok()


# -- source pins --


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
    assert (
        service.DASHBOARD_MATCH_NOT_FOUND_ERROR == 'dashboard_match_not_found'
    )
    assert service.DASHBOARD_MATCH_TERMINAL_ERROR == 'dashboard_match_terminal'
    assert service.DASHBOARD_PIN_CONFLICT_ERROR == 'dashboard_pin_conflict'


def test_the_module_commits_through_the_repository_only():
    names = {
        ast.unparse(node.func)
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
    }

    assert 'db.session.commit' not in names
    assert 'tournament_repository.commit_session' in names
    # Every audit entry is staged: the owner commits once.
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
            'create_log_entry'
        ):
            assert [
                ast.literal_eval(keyword.value)
                for keyword in node.keywords
                if keyword.arg == 'commit'
            ] == [False]
