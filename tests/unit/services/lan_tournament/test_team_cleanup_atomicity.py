import ast
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, UTC
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_readiness_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
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
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')

OPEN = TournamentStatus.REGISTRATION_OPEN
ONGOING = TournamentStatus.ONGOING

# Repository helpers that commit on their own. No roster owner may call them.
COMMITTING_HELPERS = {
    'clear_winner_team_reference',
    'delete_team',
    'remove_team_from_contestants',
    'remove_team_from_participants',
    'update_participant',
    'update_team',
    'update_team_captain',
}


def _tournament(**kwargs) -> Tournament:
    defaults: dict[str, Any] = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'updated_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': OPEN,
        'game_format': None,
        'elimination_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _team(**kwargs) -> TournamentTeam:
    defaults: dict[str, Any] = {
        'id': TournamentTeamID(generate_uuid()),
        'tournament_id': TOURNAMENT_ID,
        'name': 'Test Team',
        'tag': None,
        'description': None,
        'image_url': None,
        'captain_user_id': UserID(generate_uuid()),
        'join_code': None,
        'created_at': NOW,
    }
    defaults.update(kwargs)
    return TournamentTeam(**defaults)


def _participant(**kwargs) -> TournamentParticipant:
    defaults: dict[str, Any] = {
        'id': TournamentParticipantID(generate_uuid()),
        'user_id': UserID(generate_uuid()),
        'tournament_id': TOURNAMENT_ID,
        'substitute_player': False,
        'team_id': None,
        'created_at': NOW,
    }
    defaults.update(kwargs)
    return TournamentParticipant(**defaults)


class _World:
    """One team, its roster and one match, behind a call-recording mock.

    Every repository call, signal and post-commit dispatch lands in one
    ordered list, so a test can read the order of locks, writes, commit and
    effects. The real roster lock and refresh helpers run on the same mock.
    """

    def __init__(self, monkeypatch, *, status, roster):
        self.manager = manager = MagicMock()
        self.repo = repo = manager.repo
        self.signals = manager.signals
        self.status = status

        self.captain = _participant()
        self.member = _participant()
        self.team = _team(captain_user_id=self.captain.user_id)
        self.tournament = _tournament(
            tournament_status=status, winner_team_id=self.team.id
        )
        self.match_id = generate_uuid()
        everyone = {'captain': self.captain, 'member': self.member}
        self.members = [
            replace(everyone[key], team_id=self.team.id) for key in roster
        ]
        self.captain, self.member = (
            next((m for m in self.members if m.id == p.id), p)
            for p in (self.captain, self.member)
        )

        repo.find_team.return_value = self.team
        repo.get_team_for_update.return_value = self.team
        repo.get_tournament_for_update.return_value = self.tournament
        repo.get_tournament.return_value = self.tournament
        repo.find_participant.side_effect = self._find_participant
        repo.find_participant_fresh.side_effect = self._find_participant
        repo.get_participants_for_team.side_effect = lambda team_id: list(
            self.members
        )
        repo.update_participant_flush.side_effect = self._update_participant
        repo.remove_team_from_participants_flush.side_effect = lambda team_id: (
            self.members.clear()
        )
        repo.get_contestants_for_tournament.return_value = {
            self.match_id: [
                SimpleNamespace(participant_id=None, team_id=self.team.id)
            ]
        }
        repo.get_matches_for_tournament.return_value = [
            SimpleNamespace(id=self.match_id)
        ]
        manager.defwin.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )
        manager.refresh.return_value = Ok(
            SimpleNamespace(pending_invitation_ids=())
        )

        @contextmanager
        def dispatch(pending):
            manager.dispatch.begin()
            try:
                yield
            finally:
                manager.dispatch.end()

        monkeypatch.setattr(
            tournament_team_service, 'tournament_repository', repo
        )
        monkeypatch.setattr(tournament_team_service, 'signals', manager.signals)
        monkeypatch.setattr(
            tournament_team_service,
            '_dispatch_roster_invitations_after_signals',
            dispatch,
        )
        monkeypatch.setattr(
            tournament_participant_service, 'tournament_repository', repo
        )
        monkeypatch.setattr(
            tournament_match_service,
            '_audit_engine_pairing_change_flush',
            manager.audit,
        )
        monkeypatch.setattr(
            tournament_match_service,
            'handle_defwin_for_removed_team',
            manager.defwin,
        )
        monkeypatch.setattr(
            tournament_readiness_service,
            'refresh_pairing_and_invitations_flush',
            manager.refresh,
        )

    def _find_participant(self, participant_id):
        for participant in (self.captain, self.member, *self.members):
            if participant.id == participant_id:
                return participant
        return None

    def _update_participant(self, participant):
        self.members[:] = [m for m in self.members if m.id != participant.id]
        if participant.team_id is not None:
            self.members.append(participant)

    def events(self) -> list[str]:
        return [str(call[0]) for call in self.manager.method_calls]

    def assert_in_order(self, expected: list[str]) -> None:
        events = iter(self.events())
        for name in expected:
            assert name in events, (name, self.events())

    def arm(self, target: str, error: BaseException) -> None:
        """Make one recorded helper raise."""
        mock = self.manager
        for part in target.split('.'):
            mock = getattr(mock, part)
        mock.side_effect = error


def _delete_team(world):
    return tournament_team_service.delete_team(world.team.id)


def _leave_team(world):
    return tournament_team_service.leave_team(world.captain.id)


def _remove_member(world):
    return tournament_team_service.remove_team_member(
        world.team.id, world.member.user_id
    )


# Each operation, the roster it starts from (a last member the operation
# removes), the tournament status, the helper names behind each failure
# point, the cleanup it must stage before its single commit and the
# signals that may follow it.
# fmt: off
OPERATIONS = {
    'delete_team': dict(
        call=_delete_team, status=OPEN, roster=('captain', 'member'),
        steps={
            'membership': 'repo.remove_team_from_participants_flush',
            'match_entries': 'repo.remove_team_from_contestants_flush',
            'team_row': 'repo.delete_team_flush',
        },
        cleanup=[
            'repo.get_tournament_for_update', 'repo.get_team_for_update',
            'repo.remove_team_from_participants_flush',
            'repo.remove_team_from_contestants_flush', 'audit',
            'repo.clear_winner_for_tournament', 'repo.delete_team_flush',
            'refresh',
        ],
        signals=['signals.team_deleted.send'],
    ),
    'leave_team': dict(
        call=_leave_team, status=OPEN, roster=('captain',),
        steps={
            'membership': 'repo.update_participant_flush',
            'match_entries': 'repo.remove_team_from_contestants_flush',
            'team_row': 'repo.delete_team_flush',
        },
        cleanup=[
            'repo.get_tournament_for_update', 'repo.get_team_for_update',
            'repo.update_participant_flush',
            'repo.remove_team_from_contestants_flush', 'audit',
            'repo.clear_winner_for_tournament', 'repo.delete_team_flush',
            'refresh',
        ],
        signals=['signals.team_member_left.send'],
    ),
    'remove_team_member_before_start': dict(
        call=_remove_member, status=OPEN, roster=('member',),
        steps={
            'membership': 'repo.update_participant_flush',
            'match_entries': 'repo.remove_team_from_contestants_flush',
            'team_row': 'repo.delete_team_flush',
        },
        cleanup=[
            'repo.get_tournament_for_update', 'repo.get_team_for_update',
            'repo.update_participant_flush',
            'repo.remove_team_from_participants_flush',
            'repo.remove_team_from_contestants_flush', 'audit',
            'repo.clear_winner_for_tournament', 'repo.delete_team_flush',
            'refresh',
        ],
        signals=['signals.team_member_left.send', 'signals.team_deleted.send'],
    ),
    'remove_team_member_while_ongoing': dict(
        call=_remove_member, status=ONGOING, roster=('member',),
        steps={
            'membership': 'repo.update_participant_flush',
            'match_entries': 'defwin',
            'team_row': 'repo.soft_delete_team_flush',
        },
        cleanup=[
            'repo.get_tournament_for_update', 'repo.get_team_for_update',
            'repo.update_participant_flush', 'defwin',
            'repo.remove_team_from_participants_flush',
            'repo.soft_delete_team_flush', 'refresh',
        ],
        signals=['signals.team_member_left.send', 'signals.team_deleted.send'],
    ),
}
# fmt: on


@pytest.fixture
def build(monkeypatch):
    def _build(operation):
        spec = OPERATIONS[operation]
        return _World(monkeypatch, status=spec['status'], roster=spec['roster'])

    return _build


@pytest.mark.parametrize('operation', OPERATIONS)
def test_empty_team_cleanup_commits_before_signals(build, operation):
    spec = OPERATIONS[operation]
    world = build(operation)

    result = spec['call'](world)

    assert result.is_ok(), result
    events = world.events()
    world.assert_in_order(
        [
            *spec['cleanup'],
            'repo.commit_session',
            'dispatch.begin',
            *spec['signals'],
            'dispatch.end',
        ]
    )
    assert events.count('repo.commit_session') == 1
    commit_at = events.index('repo.commit_session')
    last_cleanup = max(
        i for i, name in enumerate(events) if name in spec['cleanup']
    )
    assert last_cleanup < commit_at, events
    sent = [i for i, name in enumerate(events) if name.startswith('signals.')]
    assert all(i > commit_at for i in sent), events
    assert [events[i] for i in sent] == spec['signals']
    assert 'repo.rollback_session' not in events
    assert not {e.removeprefix('repo.') for e in events} & COMMITTING_HELPERS
    for call in world.repo.clear_winner_for_tournament.call_args_list:
        assert call.kwargs == {'commit': False}


@pytest.mark.parametrize('operation', OPERATIONS)
def test_cleanup_helpers_receive_the_team_and_its_matches(build, operation):
    """The cleanup reaches the real roster assignments, not an empty set."""
    spec = OPERATIONS[operation]
    world = build(operation)

    assert spec['call'](world).is_ok()

    if 'repo.remove_team_from_contestants_flush' in spec['cleanup']:
        world.repo.remove_team_from_contestants_flush.assert_called_once_with(
            world.team.id
        )
        world.manager.audit.assert_called_once()
    world.manager.refresh.assert_called_once()
    assert world.manager.refresh.call_args.args == (world.match_id,)


# fmt: off
FAILURES = ['membership', 'match_entries', 'team_row', 'refresh_err',
            'refresh_raises', 'commit']
# fmt: on


def _arm(world, spec, failure):
    boom = RuntimeError('boom')
    if failure in spec['steps']:
        world.arm(spec['steps'][failure], boom)
    elif failure == 'refresh_err':
        world.manager.refresh.return_value = Err('refresh_failed')
    elif failure == 'refresh_raises':
        world.manager.refresh.side_effect = boom
    else:
        world.repo.commit_session.side_effect = boom


@pytest.mark.parametrize('failure', FAILURES)
@pytest.mark.parametrize('operation', OPERATIONS)
def test_cleanup_failure_restores_membership_and_match_entries(
    build, operation, failure
):
    spec = OPERATIONS[operation]
    world = build(operation)
    _arm(world, spec, failure)

    if failure == 'refresh_err':
        result = spec['call'](world)
        assert result.unwrap_err() == 'refresh_failed'
    else:
        with pytest.raises(RuntimeError, match='boom'):
            spec['call'](world)

    events = world.events()
    # One rollback undoes every flushed membership, match and team change.
    assert events.count('repo.rollback_session') == 1, events
    committed = events.count('repo.commit_session')
    assert committed == (1 if failure == 'commit' else 0), events
    assert not [e for e in events if e.startswith('signals.')]
    assert 'dispatch.begin' not in events
    assert events[-1] == 'repo.rollback_session', events


TEAM_OPERATIONS = {
    'join_team': lambda w: tournament_team_service.join_team(
        w.member.id, w.team.id
    ),
    'delete_team': _delete_team,
    'leave_team': _leave_team,
    'remove_team_member': _remove_member,
    'transfer_captain': lambda w: tournament_team_service.transfer_captain(
        w.team.id, w.member.user_id
    ),
    'admin_add_member': lambda w: tournament_team_service.admin_add_member(
        w.team.id, w.member.user_id
    ),
}

# Calls that lock or write the team row, explicitly or through DML.
TEAM_ROW_CALLS = {
    'repo.get_team_for_update',
    'repo.delete_team_flush',
    'repo.soft_delete_team_flush',
    'repo.update_team_captain_flush',
}
TOURNAMENT_LOCKS = {
    'repo.get_tournament_for_update',
    'repo.lock_tournament_for_update',
}


def _lock_cycle_world(monkeypatch, operation):
    """Return a world in which the operation reaches its team lock."""
    if operation == 'join_team':
        return _World(monkeypatch, status=OPEN, roster=('captain',))
    if operation == 'admin_add_member':
        world = _World(monkeypatch, status=OPEN, roster=('captain',))
        world.repo.find_active_participant_by_user.return_value = world.member
        world.repo.get_participant_for_update.return_value = world.member
        return world
    if operation == 'transfer_captain':
        return _World(monkeypatch, status=OPEN, roster=('captain', 'member'))
    if operation == 'remove_team_member':
        return _World(monkeypatch, status=OPEN, roster=('member',))
    if operation == 'leave_team':
        return _World(monkeypatch, status=OPEN, roster=('captain',))
    return _World(monkeypatch, status=OPEN, roster=('captain', 'member'))


@pytest.mark.parametrize('operation', TEAM_OPERATIONS)
def test_team_cleanup_does_not_introduce_lock_cycle(monkeypatch, operation):
    """Every team lock or write follows the tournament row lock."""
    world = _lock_cycle_world(monkeypatch, operation)

    result = TEAM_OPERATIONS[operation](world)

    assert result.is_ok(), result
    events = world.events()
    first_team = min(
        i for i, name in enumerate(events) if name in TEAM_ROW_CALLS
    )
    locks = [i for i, name in enumerate(events) if name in TOURNAMENT_LOCKS]
    assert locks, events
    assert locks[0] < first_team, events
    assert events.count('repo.commit_session') == 1


def test_join_team_locks_the_tournament_before_the_team(monkeypatch):
    world = _lock_cycle_world(monkeypatch, 'join_team')

    assert tournament_team_service.join_team(
        world.member.id, world.team.id
    ).is_ok()

    events = world.events()
    # The unlocked read only learns the tournament; no lock is taken yet.
    assert events.index('repo.find_team') < events.index(
        'repo.get_tournament_for_update'
    )
    assert events.index('repo.get_tournament_for_update') < events.index(
        'repo.get_team_for_update'
    )
    world.repo.get_tournament_for_update.assert_called_once_with(TOURNAMENT_ID)


# fmt: off
LOST_TEAM_ERRORS = {
    'join_team': 'Unknown team.',
    'delete_team': 'Team not found.',
    'transfer_captain': 'Unknown team.',
    'admin_add_member': 'Unknown team.',
    'remove_team_member': 'Unknown team.',
}
# fmt: on

WRITES = {
    'repo.update_participant_flush',
    'repo.remove_team_from_participants_flush',
    'repo.remove_team_from_contestants_flush',
    'repo.delete_team_flush',
    'repo.soft_delete_team_flush',
    'repo.update_team_captain_flush',
    'repo.clear_winner_for_tournament',
}


def _assert_refused_without_effects(world):
    events = world.events()
    assert events.count('repo.rollback_session') == 1, events
    assert events[-1] == 'repo.rollback_session', events
    assert 'repo.commit_session' not in events
    assert not WRITES & set(events), events
    assert not [e for e in events if e.startswith('signals.')]


@pytest.mark.parametrize('operation', LOST_TEAM_ERRORS)
def test_a_team_deleted_while_waiting_for_the_tournament_lock_is_unknown(
    monkeypatch, operation
):
    """A cleanup that won the lock race leaves no team to reload."""
    world = _lock_cycle_world(monkeypatch, operation)
    world.repo.get_team_for_update.side_effect = ValueError(
        'Unknown team ID "x"'
    )

    result = TEAM_OPERATIONS[operation](world)

    assert result.unwrap_err() == LOST_TEAM_ERRORS[operation]
    _assert_refused_without_effects(world)


@pytest.mark.parametrize('operation', LOST_TEAM_ERRORS)
def test_a_team_of_another_tournament_is_unknown_after_the_lock(
    monkeypatch, operation
):
    world = _lock_cycle_world(monkeypatch, operation)
    world.repo.get_team_for_update.return_value = replace(
        world.team, tournament_id=TournamentID(generate_uuid())
    )

    result = TEAM_OPERATIONS[operation](world)

    assert result.unwrap_err() == LOST_TEAM_ERRORS[operation]
    _assert_refused_without_effects(world)


TEAM_LOCKS = TEAM_ROW_CALLS | {'repo.update_team'}
TOURNAMENT_LOCK_NAMES = TOURNAMENT_LOCKS | {'_find_locked_team'}
LOCKING_HELPERS = {'_lock_roster_matches_flush'}


def _first_lines(function: ast.FunctionDef) -> tuple[int | None, int | None]:
    tournament, team = [], []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func).replace(
            'tournament_repository.', 'repo.'
        )
        if callee in TOURNAMENT_LOCK_NAMES:
            tournament.append(node.lineno)
        if callee in TEAM_LOCKS | LOCKING_HELPERS:
            team.append(node.lineno)
    return min(tournament, default=None), min(team, default=None)


def test_every_team_lock_follows_a_tournament_lock_in_source_order():
    tree = ast.parse(inspect.getsource(tournament_team_service))
    checked = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        tournament, team = _first_lines(function)
        if team is None:
            continue
        checked.append(function.name)
        assert tournament is not None, function.name
        assert tournament < team, function.name
    assert {
        'update_team',
        'delete_team',
        'join_team',
        'leave_team',
        'transfer_captain',
        'admin_add_member',
        'remove_team_member',
        '_find_locked_team',
    } <= set(checked)
