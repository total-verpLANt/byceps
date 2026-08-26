from dataclasses import replace
from datetime import datetime, UTC
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_readiness_authorization_service as service,
)
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    MatchPairing,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchPairingID,
    MatchSide,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.user.models import UserID
from byceps.util import authz
from byceps.util.uuid import generate_uuid7


@pytest.fixture
def roster(monkeypatch):
    tournament_id = TournamentID(generate_uuid7())
    users = [UserID(generate_uuid7()) for _ in range(4)]
    now = datetime.now(UTC)
    teams = [
        TournamentTeam(
            id=TournamentTeamID(generate_uuid7()), tournament_id=tournament_id,
            name=f'Team {index}', tag=None, description=None, image_url=None,
            captain_user_id=users[index], join_code=None, created_at=now,
        )
        for index in range(2)
    ]
    participants = [
        TournamentParticipant(
            id=TournamentParticipantID(generate_uuid7()), user_id=user_id,
            tournament_id=tournament_id, substitute_player=False,
            team_id=teams[index if index < 2 else 0].id, created_at=now,
        )
        for index, user_id in enumerate(users[:3])
    ]
    pairing = MatchPairing(
        id=MatchPairingID(generate_uuid7()),
        match_id=TournamentMatchID(generate_uuid7()),
        tournament_id=tournament_id, generation=1,
        side_a=ContestantIdentity(kind='team', id=teams[0].id),
        side_b=ContestantIdentity(kind='team', id=teams[1].id),
    )
    permissions = Mock(return_value=frozenset())
    orga = Mock(return_value=False)
    monkeypatch.setattr(service, 'get_permissions_for_user', permissions)
    monkeypatch.setattr(service.tournament_orga_service, 'is_orga_for_tournament', orga)
    monkeypatch.setattr(
        service.tournament_team_service, 'find_team',
        lambda team_id: next((t for t in teams if t.id == team_id), None),
    )
    monkeypatch.setattr(
        service.tournament_team_service, 'get_team_members',
        lambda team_id: [p for p in participants if p.team_id == team_id],
    )
    monkeypatch.setattr(
        service.tournament_participant_service, 'find_participant',
        lambda participant_id: next(
            (p for p in participants if p.id == participant_id), None
        ),
    )
    return tournament_id, pairing, users, teams, participants, permissions, orga


def test_direct_service_rejects_forged_orga(roster):
    tid, pairing, users, _, _, _, _ = roster
    with pytest.raises(TypeError, match='is_orga'):
        service.authorize_readiness_side(
            tid, pairing, MatchSide.A, users[3], is_orga=True
        )
    assert service.authorize_readiness_side(
        tid, pairing, MatchSide.A, users[3]
    ).unwrap_err() == 'readiness_forbidden'


# fmt: off
@pytest.mark.parametrize('grants', [
    frozenset(), frozenset({'*'}), frozenset({'lan_tournament.*'}),
    frozenset({'lan_tournament.result_enter'}), frozenset({'other.administrate'}),
])
# fmt: on
def test_unassigned_orga_and_unrelated_permission_refused(roster, grants):
    tid, pairing, users, _, _, permissions, orga = roster
    permissions.return_value = grants
    assert service.authorize_readiness_side(
        tid, pairing, MatchSide.B, users[2]
    ).unwrap_err() == 'readiness_forbidden'
    assert service.get_user_readiness_sides(tid, pairing, users[2]).unwrap() == frozenset()
    permissions.assert_called_with(users[2])
    orga.assert_called_with(users[2], tid)


def test_assigned_non_captain_orga_override(roster):
    tid, pairing, users, _, _, _, orga = roster
    orga.side_effect = lambda user_id, tournament_id: (
        user_id == users[2] and tournament_id == tid
    )
    for side in MatchSide:
        assert service.authorize_readiness_side(tid, pairing, side, users[2]).unwrap() == 'orga'
    assert service.get_user_readiness_sides(tid, pairing, users[2]).unwrap() == frozenset(MatchSide)
    orga.side_effect = None
    orga.return_value = False
    assert service.get_user_readiness_sides(tid, pairing, users[2]).unwrap() == frozenset()


# fmt: off
@pytest.mark.parametrize(('actor', 'expected'), [
    (0, frozenset({MatchSide.A})), (1, frozenset({MatchSide.B})),
    (2, frozenset()), (3, frozenset()),
])
@pytest.mark.parametrize('kind', ['participant', 'team'])
# fmt: on
def test_solo_captain_member_matrix(roster, actor, expected, kind):
    tid, pairing, users, _, participants, _, _ = roster
    if kind == 'participant':
        participants[:] = [replace(p, team_id=None) for p in participants]
        pairing = replace(
            pairing,
            side_a=ContestantIdentity(kind=kind, id=participants[0].id),
            side_b=ContestantIdentity(kind=kind, id=participants[1].id),
        )
    assert service.get_user_readiness_sides(tid, pairing, users[actor]).unwrap() == expected
    for side in MatchSide:
        result = service.authorize_readiness_side(tid, pairing, side, users[actor])
        if side in expected:
            assert result.unwrap() == 'player'
        else:
            assert result.unwrap_err() == 'readiness_forbidden'


def test_global_administrate_override(roster):
    tid, pairing, users, _, _, permissions, _ = roster
    permissions.return_value = frozenset({'lan_tournament.administrate'})
    assert service.get_user_readiness_sides(tid, pairing, users[3]).unwrap() == frozenset(MatchSide)
    for side in MatchSide:
        assert service.authorize_readiness_side(tid, pairing, side, users[3]).unwrap() == 'orga'


def test_authority_uses_actual_registered_permission_resolution(roster, monkeypatch):
    tid, pairing, users, _, _, _, _ = roster
    registry = authz.PermissionRegistry()
    permission_id = authz.PermissionID('lan_tournament.administrate')
    monkeypatch.setattr(authz, 'permission_registry', registry)
    lookup = Mock(return_value={permission_id})
    monkeypatch.setattr(authz.authz_service, 'get_permission_ids_for_user', lookup)
    monkeypatch.setattr(service, 'get_permissions_for_user', authz.get_permissions_for_user)
    # A grant must be registered, not merely supplied by a backend or client.
    assert service.authorize_readiness_side(
        tid, pairing, MatchSide.A, users[3]
    ).unwrap_err() == 'readiness_forbidden'
    registry.register_permission(permission_id, 'Administrate tournaments')
    assert service.authorize_readiness_side(
        tid, pairing, MatchSide.A, users[3]
    ).unwrap() == 'orga'
    lookup.assert_called_with(users[3])


def test_captain_transfer_and_membership_are_rechecked(roster):
    tid, pairing, users, teams, participants, _, _ = roster
    assert service.authorize_readiness_side(tid, pairing, MatchSide.A, users[0]).is_ok()
    teams[0] = replace(teams[0], captain_user_id=users[2])
    assert service.authorize_readiness_side(tid, pairing, MatchSide.A, users[0]).is_err()
    assert service.authorize_readiness_side(tid, pairing, MatchSide.A, users[2]).unwrap() == 'player'
    participants[2] = replace(participants[2], removed_at=datetime.now(UTC))
    assert service.authorize_readiness_side(tid, pairing, MatchSide.A, users[2]).is_err()
    participants[2] = replace(participants[2], removed_at=None, tournament_id=TournamentID(generate_uuid7()))
    assert service.get_user_readiness_sides(tid, pairing, users[2]).unwrap() == frozenset()


# fmt: off
@pytest.mark.parametrize('invalid', [
    'foreign_pairing', 'closed_pairing', 'duplicate_sides', 'foreign_team',
    'removed_team', 'missing_team', 'unknown_kind', 'foreign_participant',
    'removed_participant', 'missing_participant', 'team_member_as_solo',
])
# fmt: on
def test_invalid_pairing_refused_even_for_global_orga(roster, invalid):
    tid, pairing, users, teams, participants, permissions, _ = roster
    permissions.return_value = frozenset({'lan_tournament.administrate'})
    foreign_tid = TournamentID(generate_uuid7())
    expected_error = 'readiness_pairing_invalid'
    if invalid == 'foreign_pairing':
        pairing = replace(pairing, tournament_id=foreign_tid)
        expected_error = 'pairing_tournament_mismatch'
    elif invalid == 'closed_pairing':
        pairing = replace(pairing, ended_at=datetime.now(UTC))
    elif invalid == 'duplicate_sides':
        pairing = replace(pairing, side_b=pairing.side_a)
    elif invalid == 'foreign_team':
        teams[0] = replace(teams[0], tournament_id=foreign_tid)
    elif invalid == 'removed_team':
        teams[0] = replace(teams[0], removed_at=datetime.now(UTC))
    elif invalid == 'missing_team':
        teams.clear()
    elif invalid == 'unknown_kind':
        pairing = replace(pairing, side_a=ContestantIdentity(kind='wildcard', id=generate_uuid7()))
    else:
        participants[:] = [replace(p, team_id=None) for p in participants]
        pairing = replace(pairing, side_a=ContestantIdentity(kind='participant', id=participants[0].id))
        if invalid == 'foreign_participant':
            participants[0] = replace(participants[0], tournament_id=foreign_tid)
        elif invalid == 'removed_participant':
            participants[0] = replace(participants[0], removed_at=datetime.now(UTC))
        elif invalid == 'missing_participant':
            participants.clear()
        elif invalid == 'team_member_as_solo':
            participants[0] = replace(participants[0], team_id=teams[0].id)
    assert service.authorize_readiness_side(tid, pairing, MatchSide.A, users[3]).unwrap_err() == expected_error
    assert service.get_user_readiness_sides(tid, pairing, users[3]).unwrap_err() == expected_error


def test_invalid_side_refused(roster):
    tid, pairing, users, _, _, _, _ = roster
    assert service.authorize_readiness_side(tid, pairing, 'a', users[0]).unwrap_err() == 'invalid_match_side'
