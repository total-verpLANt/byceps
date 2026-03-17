"""
tests.integration.services.lan_tournament.test_tournament_team_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    TournamentMode,
    TournamentStatus,
)
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2024-team')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Team')


@pytest.fixture(scope='module')
def captain1(make_user):
    return make_user('TeamCaptain1')


@pytest.fixture(scope='module')
def captain2(make_user):
    return make_user('TeamCaptain2')


@pytest.fixture(scope='module')
def member1(make_user):
    return make_user('TeamMember1')


@pytest.fixture(scope='module')
def member2(make_user):
    return make_user('TeamMember2')


@pytest.fixture(scope='module')
def member3(make_user):
    return make_user('TeamMember3')


def test_create_team(party, captain1):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Creation Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    team_name = 'Test Team 1'
    join_code = 'secret123'

    result = tournament_team_service.create_team(
        tournament.id,
        team_name,
        captain1.id,
        join_code=join_code,
    )

    assert result.is_ok()
    team, event = result.unwrap()

    assert team is not None
    assert team.tournament_id == tournament.id
    assert team.name == team_name
    assert team.captain_user_id == captain1.id


def test_create_team_hashes_join_code(party, captain1):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Join Code Hash Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    join_code = 'secret456'

    result = tournament_team_service.create_team(
        tournament.id,
        'Hash Test Team',
        captain1.id,
        join_code=join_code,
    )

    assert result.is_ok()
    team, _ = result.unwrap()

    # Verify join code was hashed (should not be plaintext)
    # We can't directly access the hash, but we can verify with verify function
    assert tournament_team_service.verify_team_join_code(
        team.id, join_code
    ) is True
    assert tournament_team_service.verify_team_join_code(
        team.id, 'wrong_code'
    ) is False


def test_update_team(party, captain1):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Update Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    result = tournament_team_service.create_team(
        tournament.id,
        'Original Team Title',
        captain1.id,
        join_code='code123',
    )
    team, _ = result.unwrap()

    new_name = 'Updated Team Title'
    new_join_code = 'newcode456'

    updated = tournament_team_service.update_team(
        team.id,
        name=new_name,
        tag=None,
        description=None,
        image_url=None,
        join_code=new_join_code,
    )

    assert updated.id == team.id
    assert updated.name == new_name

    # Verify new join code works
    assert tournament_team_service.verify_team_join_code(
        team.id, new_join_code
    ) is True
    # Verify old join code doesn't work
    assert tournament_team_service.verify_team_join_code(
        team.id, 'code123'
    ) is False


def test_join_team(party, captain1, member1):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Join Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    join_code = 'joinme123'
    team_result = tournament_team_service.create_team(
        tournament.id,
        'Join Test Team',
        captain1.id,
        join_code=join_code,
    )
    team, _ = team_result.unwrap()

    # Create participant first
    participant_result = tournament_participant_service.join_tournament(
        tournament.id, member1.id
    )
    participant, _ = participant_result.unwrap()

    # Member joins with correct code
    join_result = tournament_team_service.join_team(
        participant.id, team.id, join_code
    )
    assert join_result.is_ok()


def test_join_team_wrong_code_fails(party, captain1, member2):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Join Wrong Code Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    join_code = 'correctcode'
    team_result = tournament_team_service.create_team(
        tournament.id,
        'Secure Team',
        captain1.id,
        join_code=join_code,
    )
    team, _ = team_result.unwrap()

    # Create participant
    participant_result = tournament_participant_service.join_tournament(
        tournament.id, member2.id
    )
    participant, _ = participant_result.unwrap()

    # Try to join with wrong code
    join_result = tournament_team_service.join_team(
        participant.id, team.id, 'wrongcode'
    )
    assert join_result.is_err()
    assert 'Invalid join code' in join_result.unwrap_err()


def test_join_team_when_full_fails(party, captain1, member1, member2):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Full Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=2,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    join_code = 'fullteam'
    team_result = tournament_team_service.create_team(
        tournament.id,
        'Small Team',
        captain1.id,
        join_code=join_code,
    )
    team, _ = team_result.unwrap()

    # Create participants
    participant1_result = tournament_participant_service.join_tournament(
        tournament.id, member1.id
    )
    participant1, _ = participant1_result.unwrap()

    participant2_result = tournament_participant_service.join_tournament(
        tournament.id, member2.id
    )
    participant2, _ = participant2_result.unwrap()

    # Join first member (team now has captain + 1 member = 2 = max)
    join_result1 = tournament_team_service.join_team(
        participant1.id, team.id, join_code
    )
    assert join_result1.is_ok()

    # Try to join second member - should fail (max_players_in_team is 2)
    join_result2 = tournament_team_service.join_team(
        participant2.id, team.id, join_code
    )
    assert join_result2.is_err()
    assert 'full' in join_result2.unwrap_err()


def test_leave_team(party, captain1, member1):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Leave Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    join_code = 'leaveme'
    team_result = tournament_team_service.create_team(
        tournament.id,
        'Leave Test Team',
        captain1.id,
        join_code=join_code,
    )
    team, _ = team_result.unwrap()

    # Create participant and join team
    participant_result = tournament_participant_service.join_tournament(
        tournament.id, member1.id
    )
    participant, _ = participant_result.unwrap()

    tournament_team_service.join_team(participant.id, team.id, join_code)

    # Member leaves
    leave_result = tournament_team_service.leave_team(participant.id)
    assert leave_result.is_ok()


def test_delete_team(party, captain1):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Delete Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    team_result = tournament_team_service.create_team(
        tournament.id,
        'Delete Me Team',
        captain1.id,
        join_code='deleteme',
    )
    team, _ = team_result.unwrap()

    team_id = team.id

    # Verify team exists
    teams = tournament_team_service.get_teams_for_tournament(tournament.id)
    assert any(t.id == team_id for t in teams)

    # Delete team
    delete_result = tournament_team_service.delete_team(team_id)
    assert delete_result.is_ok()

    # Verify team is gone
    teams = tournament_team_service.get_teams_for_tournament(tournament.id)
    assert not any(t.id == team_id for t in teams)


def test_get_teams_for_tournament(party, captain1, captain2):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Get Teams Test',
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=5,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    team1_result = tournament_team_service.create_team(
        tournament.id,
        'Team Alpha',
        captain1.id,
        join_code='alpha',
    )
    team1, _ = team1_result.unwrap()

    team2_result = tournament_team_service.create_team(
        tournament.id,
        'Team Bravo',
        captain2.id,
        join_code='bravo',
    )
    team2, _ = team2_result.unwrap()

    teams = tournament_team_service.get_teams_for_tournament(tournament.id)

    assert len(teams) >= 2
    team_ids = [t.id for t in teams]
    assert team1.id in team_ids
    assert team2.id in team_ids
