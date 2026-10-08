import ast
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_operational_service,
    tournament_participant_service,
    tournament_readiness_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
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
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (  # noqa: E501
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
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
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    derive_due_match_ids,
    pairing_key,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
# A tracked tournament started two hours ago; the readings follow it.
STARTED = datetime(2025, 6, 15, 12, 0, 0)
READING = datetime(2025, 6, 15, 14, 0, 0)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')

OPEN = TournamentStatus.REGISTRATION_OPEN
CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING

MODULES = {
    'participant': tournament_participant_service,
    'team': tournament_team_service,
}

# Repository writers that record a domain change of a match.
STAMPING = {
    'create_match_contestant',
    'confirm_match',
    'unconfirm_match',
    'delete_contestant_from_match',
    'delete_contestants_for_match_flush',
    'remove_team_from_contestants_flush',
    'touch_matches_last_changed_flush',
    'update_contestant_score',
    'update_contestant_scores',
}


def _tournament(*, tracked: bool, **kwargs) -> Tournament:
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
        'contestant_type': ContestantType.SOLO,
        'tournament_status': ONGOING,
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    if tracked:
        defaults.update(
            operational_clock_activated_at=STARTED,
            operational_clock_running_since=STARTED,
            operational_clock_elapsed_us=0,
        )
    defaults.update(kwargs)
    return Tournament(**defaults)


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


class _World:
    """One match, its roster and a repository behind one ordered call list.

    Every repository call, helper, signal and post-commit dispatch lands in
    one list, so a test reads the order of locks, the one operation time,
    the writes, the reconcile, the commit and the effects. The real roster
    lock and refresh helpers run on the same mock. Each sample of the
    operation time is recorded in `taken`.
    """

    def __init__(self, monkeypatch, *, status, tracked, solo, members=2):
        self.manager = manager = MagicMock()
        self.repo = repo = manager.repo
        self.solo = solo
        self.match_id = TournamentMatchID(generate_uuid())
        self.destination_id = TournamentMatchID(generate_uuid())
        self.taken: list[datetime] = []

        self.tournament = _tournament(
            tracked=tracked,
            tournament_status=status,
            contestant_type=(
                ContestantType.SOLO if solo else ContestantType.TEAM
            ),
        )
        if solo:
            self.target = _participant()
            self.other = _participant()
            self.team = None
            self.members: list[TournamentParticipant] = []
            held = [self.target, self.other]
            entries = [
                SimpleNamespace(participant_id=p.id, team_id=None) for p in held
            ]
        else:
            self.captain = _participant()
            self.outsider = UserID(generate_uuid())
            self.team = _team(captain_user_id=self.captain.user_id)
            self.members = [
                replace(_participant(), team_id=self.team.id)
                for _ in range(members)
            ]
            self.target = self.members[-1]
            self.captain = replace(self.captain, team_id=self.team.id)
            entries = [
                SimpleNamespace(participant_id=None, team_id=self.team.id)
            ]
            self.other = self.members[0]
        self.everyone = (
            [self.target, self.other] if solo else [self.captain, *self.members]
        )

        repo.get_operation_time.side_effect = self._reading
        repo.get_tournament.return_value = self.tournament
        repo.get_tournament_for_update.return_value = self.tournament
        repo.find_team.return_value = self.team
        repo.get_team_for_update.return_value = self.team
        repo.get_team.return_value = self.team
        repo.get_teams_by_ids.return_value = [self.team] if self.team else []
        repo.find_participant.side_effect = self._find_participant
        repo.find_participant_fresh.side_effect = self._find_participant
        repo.find_active_participant_by_user.side_effect = lambda t, u: next(
            (p for p in self.everyone if p.user_id == u), None
        )
        repo.get_participant_for_update.side_effect = self._find_participant
        repo.get_participants_for_team.side_effect = lambda team_id: list(
            self.members
        )
        repo.get_participants_for_tournament.side_effect = lambda tid: list(
            self.everyone
        )
        repo.get_participants_for_update.side_effect = lambda ids: [
            p for p in self.everyone if p.id in set(ids)
        ]
        repo.get_participant_count.return_value = 2
        repo.update_participant_flush.side_effect = self._update_participant
        repo.remove_team_from_participants_flush.side_effect = lambda tid: (
            self.members.clear()
        )
        repo.get_contestants_for_tournament.return_value = {
            self.match_id: entries
        }
        repo.get_matches_for_tournament.return_value = [
            SimpleNamespace(id=self.match_id),
            SimpleNamespace(id=self.destination_id),
        ]
        repo.get_match_for_update.side_effect = lambda mid: SimpleNamespace(
            id=mid
        )
        repo.find_contestant_entries_for_participant_in_tournament.return_value = [
            (SimpleNamespace(), SimpleNamespace(id=self.match_id))
        ]
        repo.find_contestant_entries_for_team_in_tournament.return_value = [
            (SimpleNamespace(), SimpleNamespace(id=self.match_id))
        ]
        self.result = tournament_match_service.DefwinResult(
            [
                SimpleNamespace(match_id=self.destination_id),
            ],
            [SimpleNamespace(match_id=self.match_id)],
            [SimpleNamespace(tournament_id=TOURNAMENT_ID)],
        )
        manager.defwin_participant.return_value = self.result
        manager.defwin_team.return_value = self.result
        manager.reconcile.return_value = Ok(None)
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

        for module in MODULES.values():
            monkeypatch.setattr(module, 'tournament_repository', repo)
            monkeypatch.setattr(module, 'signals', manager.signals)
            monkeypatch.setattr(
                module, '_dispatch_roster_invitations_after_signals', dispatch
            )
        monkeypatch.setattr(
            tournament_participant_service,
            'tournament_log_service',
            manager.log,
        )
        monkeypatch.setattr(
            tournament_participant_service.ticket_service,
            'select_ticket_users_for_party',
            lambda user_ids, party_id: {self.other.user_id},
        )
        monkeypatch.setattr(
            tournament_match_service,
            'handle_defwin_for_removed_participant',
            manager.defwin_participant,
        )
        monkeypatch.setattr(
            tournament_match_service,
            'handle_defwin_for_removed_team',
            manager.defwin_team,
        )
        monkeypatch.setattr(
            tournament_match_service,
            '_delete_contestant_from_match_flush',
            manager.strip,
        )
        monkeypatch.setattr(
            tournament_match_service,
            '_audit_engine_pairing_change_flush',
            manager.audit,
        )
        monkeypatch.setattr(
            tournament_operational_service,
            'reconcile_due_matches_flush',
            manager.reconcile,
        )
        monkeypatch.setattr(
            tournament_readiness_service,
            'refresh_pairing_and_invitations_flush',
            manager.refresh,
        )

    def _reading(self) -> datetime:
        reading = READING + timedelta(seconds=len(self.taken))
        self.taken.append(reading)
        return reading

    def _find_participant(self, participant_id):
        for participant in (*self.everyone, *self.members):
            if participant.id == participant_id:
                return participant
        return None

    def _update_participant(self, participant):
        self.members[:] = [m for m in self.members if m.id != participant.id]
        if participant.team_id is not None:
            self.members.append(participant)

    def events(self) -> list[str]:
        return [str(call[0]) for call in self.manager.method_calls]

    def first(self, name: str) -> int:
        events = self.events()
        assert name in events, (name, events)
        return events.index(name)

    def last(self, name: str) -> int:
        events = self.events()
        assert name in events, (name, events)
        return len(events) - 1 - events[::-1].index(name)

    def calls(self, name: str) -> list:
        return [
            call for call in self.manager.method_calls if str(call[0]) == name
        ]


@pytest.fixture
def build(monkeypatch):
    def _build(*, status=ONGOING, tracked=True, solo=True, members=2):
        return _World(
            monkeypatch,
            status=status,
            tracked=tracked,
            solo=solo,
            members=members,
        )

    return _build


# Every owner that may remove entries of a tracked tournament, how it runs
# and the call that removes the entries.
def _admin_remove(world, **kwargs):
    initiator = kwargs.get('initiator')
    return tournament_participant_service.admin_remove_participant(
        TOURNAMENT_ID, world.target.id, initiator=initiator
    )


def _ticketless(world, **kwargs):
    return tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID, initiator_id=kwargs.get('initiator_id')
    )


def _delete_team(world, **kwargs):
    return tournament_team_service.delete_team(world.team.id)


def _remove_member(world, **kwargs):
    return tournament_team_service.remove_team_member(
        world.team.id,
        world.target.user_id,
        initiator_id=kwargs.get('initiator_id'),
    )


def _leave_tournament(world, **kwargs):
    return tournament_participant_service.leave_tournament(
        TOURNAMENT_ID, world.target.id
    )


def _leave_team(world, **kwargs):
    return tournament_team_service.leave_team(world.target.id)


def _initiator() -> User:
    """Return a stand-in for the acting user, of which only `id` is read."""
    return SimpleNamespace(id=UserID(generate_uuid()))  # type: ignore[return-value]


# fmt: off
TRACKED_OWNERS = {
    'admin_remove_participant': dict(call=_admin_remove, solo=True,  writer='defwin_participant'),
    'remove_participants_without_tickets': dict(call=_ticketless, solo=True, writer='defwin_participant'),
    'remove_team_member': dict(call=_remove_member, solo=False, writer='defwin_team'),
}
# fmt: on


def _empty_the_team(world):
    """Let the team keep nobody once its one member goes."""
    world.members[:] = [world.target]
    world.team = replace(world.team, captain_user_id=world.outsider)
    world.repo.find_team.return_value = world.team
    world.repo.get_team_for_update.return_value = world.team
    world.repo.get_team.return_value = world.team


# -- pre-start: entries are stripped through the stamping writers --


# fmt: off
PRESTART = {
    'leave_tournament': dict(
        call=_leave_tournament, solo=True, status=OPEN,
        strip='strip'),
    'admin_remove_participant': dict(
        call=_admin_remove, solo=True, status=CLOSED,
        strip='strip'),
    'remove_participants_without_tickets': dict(
        call=_ticketless, solo=True, status=CLOSED,
        strip='strip'),
    'delete_team': dict(
        call=_delete_team, solo=False, status=CLOSED,
        strip='repo.remove_team_from_contestants_flush'),
    'leave_team': dict(
        call=_leave_team, solo=False, status=CLOSED,
        strip='repo.remove_team_from_contestants_flush'),
    'remove_team_member': dict(
        call=_remove_member, solo=False, status=CLOSED,
        strip='repo.remove_team_from_contestants_flush'),
}
# fmt: on


@pytest.mark.parametrize('owner', sorted(PRESTART))
def test_prestart_removal_strips_and_timestamps_entries(build, owner):
    spec = PRESTART[owner]
    world = build(status=spec['status'], tracked=False, solo=spec['solo'])
    if owner == 'leave_team':
        # The captain is the only member: leaving deletes the team.
        world.members[:] = [replace(world.captain, team_id=world.team.id)]
        world.target = world.members[0]
        world.team = replace(world.team, captain_user_id=world.target.user_id)
        world.repo.find_team.return_value = world.team
        world.repo.get_team_for_update.return_value = world.team
        world.repo.get_team.return_value = world.team
    elif owner == 'remove_team_member':
        _empty_the_team(world)

    result = spec['call'](world, initiator=_initiator())
    assert result.is_ok(), result

    # The generated layout is stripped before the one commit, and the
    # stripped match is stamped by the writer itself: the owner hands over
    # no time, as before, and asks the timing layer for nothing.
    events = world.events()
    assert events.count('repo.commit_session') == 1
    assert world.first(spec['strip']) < world.first('repo.commit_session')
    assert world.first('refresh') < world.first('repo.commit_session')
    assert 'repo.get_operation_time' not in events
    assert 'reconcile' not in events
    if spec['strip'] == 'strip':
        call = world.calls('strip')[0]
        assert call.args == (world.match_id,)
        assert set(call.kwargs) == {'participant_id'}
    else:
        call = world.calls('repo.remove_team_from_contestants_flush')[0]
        assert call.args == (world.team.id,)
        assert call.kwargs == {}
    assert world.calls('refresh')[0].args == (world.match_id,)


# -- a started tournament: one operation time, a reconcile before the commit --


@pytest.mark.parametrize(
    'owner',
    [
        'admin_remove_participant',
        'remove_participants_without_tickets',
        'remove_team_member',
        'delete_team',
    ],
)
def test_removal_walkover_covers_destination_matches(build, owner):
    solo = owner in (
        'admin_remove_participant',
        'remove_participants_without_tickets',
    )
    world = build(status=ONGOING, tracked=True, solo=solo)
    if owner == 'remove_team_member':
        _empty_the_team(world)
    if owner == 'delete_team':
        call = _delete_team
        writer = 'repo.remove_team_from_contestants_flush'
    else:
        call = TRACKED_OWNERS[owner]['call']
        writer = TRACKED_OWNERS[owner]['writer']

    result = call(world, initiator=_initiator(), initiator_id=generate_uuid())
    assert result.is_ok(), result

    events = world.events()
    # One operation time for the whole transaction ...
    assert events.count('repo.get_operation_time') == 1
    (reading,) = world.taken
    # ... sampled after the locks and before the first write ...
    assert world.first('repo.lock_matches_for_update') < world.first(
        'repo.get_operation_time'
    )
    assert world.first('repo.get_operation_time') < world.first(writer)
    # ... handed to the walkover, so its writers share it ...
    if owner != 'delete_team':
        (handler,) = world.calls(writer)
        assert handler.kwargs['changed_at'] == reading
    # ... and the demand follows the actual change: reconciled once at that
    # very time, after the last write and before refresh and commit.
    (reconcile,) = world.calls('reconcile')
    assert reconcile.args == (TOURNAMENT_ID,)
    assert reconcile.kwargs == {'occurred_at': reading}
    assert world.last(writer) < world.first('reconcile')
    assert world.first('reconcile') < world.first('refresh')
    assert world.first('reconcile') < world.first('repo.commit_session')
    # The walkover's destination is part of the pairing refresh, and the
    # effects of the result follow the commit.
    refreshed = {c.args[0] for c in world.calls('refresh')}
    assert world.match_id in refreshed
    if owner != 'delete_team':
        assert world.destination_id in refreshed
    commit = world.first('repo.commit_session')
    if owner != 'delete_team':
        results = [
            'signals.contestant_advanced.send',
            'signals.match_confirmed.send',
            'signals.tournament_completed.send',
        ]
        assert [e for e in events if e in results] == results
        assert all(world.first(e) > commit for e in results)
    if owner == 'delete_team':
        (cleanup,) = world.calls('repo.remove_team_from_contestants_flush')
        assert cleanup.args == (world.team.id,)
        assert cleanup.kwargs == {'changed_at': reading}


@pytest.mark.parametrize(
    'owner',
    [
        'admin_remove_participant',
        'remove_participants_without_tickets',
        'remove_team_member',
        'delete_team',
    ],
)
@pytest.mark.parametrize('raises', [False, True], ids=['err', 'raises'])
def test_a_failing_reconcile_rolls_the_removal_back(build, owner, raises):
    solo = owner in (
        'admin_remove_participant',
        'remove_participants_without_tickets',
    )
    world = build(status=ONGOING, tracked=True, solo=solo)
    if owner == 'remove_team_member':
        _empty_the_team(world)
    call = (
        _delete_team
        if owner == 'delete_team'
        else TRACKED_OWNERS[owner]['call']
    )
    if raises:
        world.manager.reconcile.side_effect = RuntimeError('boom')
    else:
        world.manager.reconcile.return_value = Err('reconcile_failed')

    if raises:
        with pytest.raises(RuntimeError):
            call(world, initiator=_initiator(), initiator_id=generate_uuid())
    else:
        result = call(
            world, initiator=_initiator(), initiator_id=generate_uuid()
        )
        assert result.is_err()
        assert result.unwrap_err() == 'reconcile_failed'

    events = world.events()
    assert events.count('repo.rollback_session') == 1
    assert 'repo.commit_session' not in events
    assert not [e for e in events if e.startswith('signals.')]
    assert 'refresh' not in events


# -- a roster change that keeps every contestant moves no match --


def _join(world):
    return tournament_team_service.join_team(world.joiner.id, world.team.id)


def _add_member(world):
    return tournament_team_service.admin_add_member(
        world.team.id, world.joiner.user_id
    )


def _transfer(world):
    return tournament_team_service.transfer_captain(
        world.team.id, world.target.user_id
    )


def _remove_one_of_two(world):
    return tournament_team_service.remove_team_member(
        world.team.id, world.target.user_id
    )


def _admin_remove_one_of_two(world):
    return tournament_participant_service.admin_remove_participant(
        TOURNAMENT_ID, world.target.id, initiator=_initiator()
    )


def _ticketless_one_of_two(world):
    return tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID, initiator_id=generate_uuid()
    )


# fmt: off
MEMBERSHIP_ONLY = {
    'join_team':                           dict(call=_join, writes='repo.update_participant_flush'),
    'admin_add_member':                    dict(call=_add_member, writes='repo.update_participant_flush'),
    'transfer_captain':                    dict(call=_transfer, writes='repo.update_team_captain_flush'),
    'remove_team_member':                  dict(call=_remove_one_of_two, writes='repo.update_participant_flush'),
    'admin_remove_participant':            dict(call=_admin_remove_one_of_two, writes='repo.soft_delete_participants_by_ids'),
    'remove_participants_without_tickets': dict(call=_ticketless_one_of_two, writes='repo.soft_delete_participants_by_ids'),
}
# fmt: on


@pytest.mark.parametrize('owner', sorted(MEMBERSHIP_ONLY))
def test_membership_only_change_affects_demand_not_match_timestamp(
    build, owner
):
    spec = MEMBERSHIP_ONLY[owner]
    world = build(status=ONGOING, tracked=True, solo=False, members=2)
    # A free participant for `join_team` and `admin_add_member`.
    world.joiner = _participant()
    world.everyone.append(world.joiner)
    world.team = replace(world.team, captain_user_id=world.members[0].user_id)
    for repo_name in ('find_team', 'get_team_for_update', 'get_team'):
        getattr(world.repo, repo_name).return_value = world.team
    world.other = world.members[0]
    world.target = world.members[1]
    world.manager.defwin_team.side_effect = AssertionError('no walkover')
    world.manager.defwin_participant.side_effect = AssertionError('no walkover')

    result = spec['call'](world)
    assert result.is_ok(), result

    # The roster moved, and with it the people the match asks for ...
    events = world.events()
    assert spec['writes'] in events
    assert world.first('refresh') < world.first('repo.commit_session')
    # ... but the match is no domain change: no stamp, no operation time,
    # no timing layer, no entry removed.
    assert STAMPING.isdisjoint(e.removeprefix('repo.') for e in events)
    assert 'repo.get_operation_time' not in events
    assert 'reconcile' not in events
    assert 'strip' not in events
    assert 'repo.touch_matches_last_changed_flush' not in events


# -- no initiator: nobody confirms, and a one-sided match is no demand --


@pytest.mark.parametrize(
    'owner',
    [
        'admin_remove_participant',
        'remove_participants_without_tickets',
        'remove_team_member',
    ],
)
def test_removal_without_initiator_has_no_phantom_demand(build, owner):
    world = build(
        status=ONGOING,
        tracked=True,
        solo=owner != 'remove_team_member',
    )
    if owner == 'remove_team_member':
        _empty_the_team(world)
    spec = TRACKED_OWNERS[owner]

    result = spec['call'](world, initiator=None, initiator_id=None)
    assert result.is_ok(), result

    # Nobody confirms the walkover, but the opponent still advanced, so the
    # demand of every touched match is recomputed in the same transaction.
    (defwin,) = world.calls(spec['writer'])
    assert defwin.kwargs['initiator_id'] is None
    assert defwin.kwargs['changed_at'] == world.taken[0]
    (reconcile,) = world.calls('reconcile')
    assert reconcile.kwargs == {'occurred_at': world.taken[0]}
    assert world.last(spec['writer']) < world.first('reconcile')
    assert world.first('reconcile') < world.first('repo.commit_session')
    assert not world.calls('repo.confirm_match')


def test_a_ticketless_team_walkover_takes_the_operation_time(
    build, monkeypatch
):
    # Every member of the team lacks a ticket, so the team empties.
    world = build(status=ONGOING, tracked=True, solo=False, members=1)
    monkeypatch.setattr(
        tournament_participant_service.ticket_service,
        'select_ticket_users_for_party',
        lambda user_ids, party_id: set(),
    )

    result = _ticketless(world, initiator_id=generate_uuid())
    assert result.is_ok(), result

    (reading,) = world.taken
    (handler,) = world.calls('defwin_team')
    assert handler.kwargs['changed_at'] == reading
    (reconcile,) = world.calls('reconcile')
    assert reconcile.kwargs == {'occurred_at': reading}
    assert world.last('defwin_team') < world.first('reconcile')
    assert world.first('reconcile') < world.first('repo.commit_session')


# -- the walkover writers inside the match service share that time --


INITIATOR_ID = UserID(generate_uuid())
NEXT_MATCH_ID = TournamentMatchID(generate_uuid())


@pytest.fixture
def defwin_repo(monkeypatch):
    """The repository of the match service, and its pairing collaborators."""
    repo = MagicMock()
    monkeypatch.setattr(tournament_match_service, 'tournament_repository', repo)
    monkeypatch.setattr(
        tournament_readiness_service,
        'refresh_pairing_and_invitations_flush',
        lambda match_id, *, occurred_at: Ok(None),
    )
    monkeypatch.setattr(
        tournament_match_service, 'create_log_entry', MagicMock()
    )
    return repo


def _defwin_board(repo, kind, *, next_match_id):
    """Return the removed identity of a match that its opponent now holds."""
    tournament = _tournament(tracked=True)
    match = _match(tournament, round_=0, next_match_id=next_match_id)
    if kind == 'participant':
        removed = TournamentParticipantID(generate_uuid())
        opponent = _entry(match)
        entries = [(_entry(match, removed), match)]
        repo.find_contestant_entries_for_participant_in_tournament.return_value = entries
    else:
        removed = TournamentTeamID(generate_uuid())
        opponent = _entry(match, team_id=TournamentTeamID(generate_uuid()))
        entries = [(_entry(match, team_id=removed), match)]
        repo.find_contestant_entries_for_team_in_tournament.return_value = (
            entries
        )
    repo.find_match.side_effect = lambda match_id: match
    repo.get_match.side_effect = lambda match_id: match
    repo.get_tournament.return_value = tournament
    repo.get_contestants_for_match.side_effect = lambda match_id: (
        [opponent] if match_id == match.id else []
    )
    return match, removed


def _handle(kind, removed, **kwargs):
    if kind == 'participant':
        return tournament_match_service.handle_defwin_for_removed_participant(
            TOURNAMENT_ID, removed, **kwargs
        )
    return tournament_match_service.handle_defwin_for_removed_team(
        TOURNAMENT_ID, removed, **kwargs
    )


@pytest.mark.parametrize('kind', ['participant', 'team'])
@pytest.mark.parametrize(
    'stamped', [True, False], ids=['shared_time', 'legacy_call']
)
def test_defwin_writers_share_the_operation_time(defwin_repo, kind, stamped):
    match, removed = _defwin_board(
        defwin_repo, kind, next_match_id=NEXT_MATCH_ID
    )
    stamp = {'changed_at': READING.replace(tzinfo=None)} if stamped else {}

    result = _handle(kind, removed, initiator_id=INITIATOR_ID, **stamp)

    assert len(result.advanced) == 1
    assert len(result.confirmed) == 1
    # The delete, the advance and the confirmation take the one time, or
    # keep the call they always made when there is none.
    identity = (
        {'team_id': None, 'participant_id': removed}
        if kind == 'participant'
        else {'team_id': removed, 'participant_id': None}
    )
    defwin_repo.delete_contestant_from_match.assert_called_once_with(
        match.id, **identity, **stamp
    )
    (advance,) = defwin_repo.create_match_contestant.call_args_list
    assert advance.args[0].tournament_match_id == NEXT_MATCH_ID
    assert advance.kwargs == stamp
    defwin_repo.confirm_match.assert_called_once_with(
        match.id, INITIATOR_ID, **stamp
    )


@pytest.mark.parametrize('outcome', ['decided', 'plain_round_robin'])
@pytest.mark.parametrize('kind', ['participant', 'team'])
def test_defwin_completion_edge_shares_the_operation_time(
    defwin_repo, monkeypatch, kind, outcome
):
    _, removed = _defwin_board(defwin_repo, kind, next_match_id=None)
    changed_at = READING.replace(tzinfo=None)
    decided = MagicMock(return_value=Ok(outcome == 'decided'))
    plain = MagicMock(return_value=Ok(None))
    monkeypatch.setattr(
        tournament_match_service, '_try_auto_complete_tournament', decided
    )
    monkeypatch.setattr(
        tournament_match_service, 'try_complete_plain_round_robin', plain
    )

    _handle(kind, removed, initiator_id=INITIATOR_ID, changed_at=changed_at)

    # The status edge, which freezes the clock, is the time of the walkover.
    assert decided.call_args.kwargs == {'changed_at': changed_at}
    if outcome == 'plain_round_robin':
        assert plain.call_args.kwargs == {'changed_at': changed_at}
    else:
        plain.assert_not_called()


@pytest.mark.parametrize(
    'owner',
    [
        'admin_remove_participant',
        'remove_participants_without_tickets',
        'remove_team_member',
    ],
)
def test_an_untracked_walkover_hands_over_no_time(build, owner):
    world = build(
        status=ONGOING,
        tracked=False,
        solo=owner != 'remove_team_member',
    )
    if owner == 'remove_team_member':
        _empty_the_team(world)
    spec = TRACKED_OWNERS[owner]

    result = spec['call'](world, initiator=_initiator(), initiator_id=None)
    assert result.is_ok(), result

    # A tournament without clock history keeps the call it always made.
    (defwin,) = world.calls(spec['writer'])
    assert set(defwin.kwargs) == {'initiator_id'}
    assert 'repo.get_operation_time' not in world.events()
    assert 'reconcile' not in world.events()


def _board_tournament(game_format, mode):
    return _tournament(
        tracked=True, game_format=game_format, elimination_mode=mode
    )


def _match(
    tournament,
    *,
    round_,
    confirmed=False,
    bracket=None,
    order=0,
    next_match_id=None,
):
    return TournamentMatch(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=tournament.id,
        group_order=None,
        match_order=order,
        round=round_,
        next_match_id=next_match_id,
        confirmed_by=UserID(generate_uuid()) if confirmed else None,
        created_at=NOW,
        bracket=bracket,
        occupied_since=STARTED,
    )


def _entry(match, participant_id=None, team_id=None):
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match.id,
        team_id=team_id,
        participant_id=(
            None
            if team_id is not None
            else TournamentParticipantID(participant_id or generate_uuid())
        ),
        score=None,
        created_at=NOW,
    )


# fmt: off
BOARDS = [
    (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION, None),
    (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN, None),
    (GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION, Bracket.WINNERS),
]
# fmt: on


@pytest.mark.parametrize(
    ('game_format', 'mode', 'bracket'),
    BOARDS,
    ids=['knockout', 'round_robin', 'free_for_all'],
)
def test_a_one_sided_unconfirmed_match_is_no_demand(game_format, mode, bracket):
    tournament = _board_tournament(game_format, mode)
    one_sided = _match(tournament, round_=0, bracket=bracket, order=0)
    playable = _match(tournament, round_=0, bracket=bracket, order=1)
    rows = {
        one_sided.id: [_entry(one_sided)],
        playable.id: [_entry(playable), _entry(playable)],
    }
    matches = [one_sided, playable]

    due = derive_due_match_ids(
        tournament, matches, rows, frozenset(m.id for m in matches)
    )

    # A lobby that kept one player, or a match that lost a side, waits for
    # no one: the occupancy of an FFA lobby does not make it demand.
    assert due == {playable.id}


class _FakeRepo:
    """The repository reads and the episode writes of one reconcile."""

    def __init__(self, tournament, matches, rows, open_episodes):
        self.tournament = tournament
        self.matches = matches
        self.rows = rows
        self.open = open_episodes
        self.closed: list = []
        self.opened: list = []

    def lock_tournament_for_update(self, tournament_id):
        return None

    def get_tournament(self, tournament_id, *, fresh=False):
        return self.tournament

    def get_matches_for_tournament_ordered_fresh(self, tournament_id):
        return list(self.matches)

    def lock_matches_for_update(self, match_ids):
        return None

    def get_contestants_for_matches(self, match_ids):
        return self.rows

    def list_open_due_episodes(self, tournament_id):
        return list(self.open)

    def close_due_episodes_flush(self, match_ids, *, occurred_at, clock_us):
        self.closed.append((sorted(match_ids), occurred_at, clock_us))

    def open_due_episode_flush(self, episode):
        self.opened.append(episode)


def test_reconcile_closes_the_episode_of_a_one_sided_match_and_opens_none(
    monkeypatch,
):
    tournament = _board_tournament(
        GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION
    )
    one_sided = _match(tournament, round_=0, order=0)
    playable = _match(tournament, round_=0, order=1)
    final = _match(tournament, round_=1)
    survivor = _entry(one_sided)
    rows = {
        one_sided.id: [survivor],
        playable.id: [_entry(playable), _entry(playable)],
        # The survivor advanced into the final: one side, no demand.
        final.id: [_entry(final, survivor.participant_id)],
    }
    previous_pair = [_entry(one_sided), survivor]

    def episode(match, entries):
        return MatchDueEpisode(
            id=MatchDueEpisodeID(generate_uuid()),
            tournament_id=tournament.id,
            match_id=match.id,
            pairing_key=pairing_key(entries),
            opened_at=STARTED,
            opened_clock_us=0,
        )

    repo = _FakeRepo(
        tournament,
        [one_sided, playable, final],
        rows,
        [
            episode(one_sided, previous_pair),
            episode(playable, rows[playable.id]),
        ],
    )
    monkeypatch.setattr(
        tournament_operational_service, 'tournament_repository', repo
    )

    result = tournament_operational_service.reconcile_due_matches_flush(
        tournament.id, occurred_at=READING.replace(tzinfo=None)
    )

    assert result.is_ok()
    # The pair broke up: its episode ends with the removal. The untouched
    # match keeps its episode, and neither one-sided match opens one.
    assert [ids for ids, _, _ in repo.closed] == [[one_sided.id]]
    assert repo.opened == []


# -- self-service leaving never reaches a started tournament --


@pytest.mark.parametrize(
    'owner',
    ['leave_tournament', 'leave_team'],
)
@pytest.mark.parametrize(
    'status', [ONGOING, TournamentStatus.PAUSED, TournamentStatus.COMPLETED]
)
def test_a_started_tournament_cannot_reach_the_self_service_leave_owners(
    build, owner, status
):
    world = build(status=status, tracked=True, solo=owner == 'leave_tournament')
    if owner == 'leave_team':
        world.members[:] = [world.target]
        world.team = replace(world.team, captain_user_id=world.target.user_id)
        for repo_name in ('find_team', 'get_team_for_update', 'get_team'):
            getattr(world.repo, repo_name).return_value = world.team

    call = _leave_tournament if owner == 'leave_tournament' else _leave_team
    result = call(world)

    assert result.is_err()
    events = world.events()
    assert 'repo.commit_session' not in events
    assert 'repo.get_operation_time' not in events
    assert 'strip' not in events
    assert not [e for e in events if 'remove_team_from_contestants' in e]


# -- structure: every owner that removes entries has a hook, or a reason --


SOURCES = {name: inspect.getsource(module) for name, module in MODULES.items()}

# What removes the entries of a match, directly or through a helper.
REMOVERS = {
    'handle_defwin_for_removed_participant',
    'handle_defwin_for_removed_team',
    '_remove_team_contestants_flush',
    '_delete_unplayed_entries',
    '_remove_single_participant_bracket_aware',
}
# The self-service owners refuse every started tournament (tested above), so
# a tournament with a clock never reaches them.
SELF_SERVICE = {'leave_tournament', 'leave_team'}
HOOKED = {
    'admin_remove_participant',
    'remove_participants_without_tickets',
    'delete_team',
    'remove_team_member',
}


def _calls_of(node: ast.AST) -> set[str]:
    return {
        n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, (ast.Attribute, ast.Name))
    }


def _functions(source: str) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    }


def test_every_removal_owner_reconciles_or_is_a_self_service_owner():
    owners: dict[str, ast.FunctionDef] = {}
    helpers: dict[str, ast.FunctionDef] = {}
    for source in SOURCES.values():
        for name, node in _functions(source).items():
            (helpers if name.startswith('_') else owners)[name] = node

    def reaches_removal(name: str, seen=frozenset()) -> bool:
        node = {**owners, **helpers}.get(name)
        if node is None or name in seen:
            return False
        called = _calls_of(node)
        return bool(called & REMOVERS) or any(
            reaches_removal(c, seen | {name}) for c in called if c in helpers
        )

    removing = {name for name in owners if reaches_removal(name)}
    assert removing == HOOKED | SELF_SERVICE
    for name in HOOKED:
        node = owners[name]
        calls = _calls_of(node)
        assert '_reconcile_roster_timing_flush' in calls, name
        assert '_operation_time_of' in calls, name
        lines = {
            call: [
                n.lineno
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and (
                    getattr(n.func, 'attr', None) == call
                    or getattr(n.func, 'id', None) == call
                )
            ]
            for call in (
                '_operation_time_of',
                '_reconcile_roster_timing_flush',
                '_refresh_roster_matches_flush',
                'commit_session',
            )
        }
        # Sample, then reconcile, then refresh and commit: the time exists
        # before the demand is recomputed, which precedes the commit.
        assert max(lines['_operation_time_of']) < min(
            lines['_reconcile_roster_timing_flush']
        ), name
        assert max(lines['_reconcile_roster_timing_flush']) < min(
            lines['_refresh_roster_matches_flush']
        ), name
        assert max(lines['_refresh_roster_matches_flush']) < min(
            lines['commit_session']
        ), name
    for name in SELF_SERVICE:
        assert '_reconcile_roster_timing_flush' not in _calls_of(owners[name])
