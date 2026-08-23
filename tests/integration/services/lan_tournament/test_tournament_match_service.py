"""
tests.integration.services.lan_tournament.test_tournament_match_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2024-match')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Match')


@pytest.fixture(scope='module')
def user1(make_user):
    return make_user('MatchUser1')


@pytest.fixture(scope='module')
def user2(make_user):
    return make_user('MatchUser2')


@pytest.fixture(scope='module')
def user3(make_user):
    return make_user('MatchUser3')


@pytest.fixture(scope='module')
def user4(make_user):
    return make_user('MatchUser4')


@pytest.fixture(scope='module')
def admin_user(make_user):
    return make_user('MatchAdmin')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Tournament Entry')


@pytest.fixture(scope='module')
def grant_ticket(ticket_category):
    """Give a user a valid (used) ticket for the party."""

    def _grant(user):
        return ticket_creation_service.create_ticket(
            ticket_category, user, user=user
        )

    return _grant


def _create_tournament(
    name,
    *,
    contestant_type,
    max_players=None,
    max_teams=None,
    min_players_in_team=None,
    max_players_in_team=None,
):
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=contestant_type,
        max_players=max_players,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()

    open_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    assert open_result.is_ok()

    return tournament


def _join_all(tournament, users, grant_ticket):
    participants = {}
    for user in users:
        grant_ticket(user)
        result = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert result.is_ok()
        participant, _ = result.unwrap()
        participants[user.id] = participant
    return participants


def test_generate_and_seed_bracket_for_solo_tournament(
    party, user1, user2, user3, user4, grant_ticket
):
    """Test full bracket workflow for solo player tournament."""
    # Create tournament
    tournament = _create_tournament(
        'Solo Bracket Test',
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )

    # Add participants and close registration
    _join_all(tournament, [user1, user2, user3, user4], grant_ticket)
    close_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    )
    assert close_result.is_ok()

    # Generate bracket (creates and seeds all matches)
    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    # 4 players = 2 matches in first round (+ semifinal wiring to
    # final, third-place match), no byes/DEFWINs.
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    first_round = [m for m in matches if m.round == 0]
    assert len(first_round) == 2

    for match in first_round:
        contestants = tournament_match_service.get_contestants_for_match(
            match.id
        )
        assert len(contestants) == 2

    assert all(match.confirmed_by is None for match in first_round)


def test_set_scores_and_confirm_match(party, user1, user2, admin_user, grant_ticket):
    """Test setting scores and confirming match results."""
    # Create tournament
    tournament = _create_tournament(
        'Score Test Tournament',
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    # Add participants
    participants = _join_all(tournament, [user1, user2], grant_ticket)
    participant1 = participants[user1.id]
    participant2 = participants[user2.id]

    # Generate bracket (creates one final match for 2 contestants)
    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    # Get the match
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    assert len(matches) == 1
    match = matches[0]

    # Set scores
    set_result1 = tournament_match_service.set_score(
        match.id, participant1.id, 10
    )
    assert set_result1.is_ok()
    set_result2 = tournament_match_service.set_score(
        match.id, participant2.id, 5
    )
    assert set_result2.is_ok()

    # Confirm match
    confirm_result = tournament_match_service.confirm_match(
        match.id, admin_user.id
    )
    assert confirm_result.is_ok()

    # Verify confirmation
    confirmed_match = tournament_match_service.get_match(match.id)
    assert confirmed_match.confirmed_by == admin_user.id


def test_cannot_confirm_match_without_scores(party, user1, user2, admin_user, grant_ticket):
    """Test that match cannot be confirmed without scores."""
    tournament = _create_tournament(
        'No Score Test',
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    _join_all(tournament, [user1, user2], grant_ticket)

    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    match = matches[0]

    # Try to confirm without setting scores
    result = tournament_match_service.confirm_match(match.id, admin_user.id)
    assert result.is_err()
    assert 'must have scores' in result.unwrap_err()


def test_cannot_set_negative_score(party, user1, user2, grant_ticket):
    """Test that negative scores are rejected."""
    tournament = _create_tournament(
        'Negative Score Test',
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    participants = _join_all(tournament, [user1, user2], grant_ticket)
    participant = participants[user1.id]

    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    match = matches[0]

    result = tournament_match_service.set_score(match.id, participant.id, -1)
    assert result.is_err()
    assert 'cannot be negative' in result.unwrap_err()


def test_match_comments_workflow(party, user1, user2, admin_user, grant_ticket):
    """Test adding, updating, and deleting match comments."""
    tournament = _create_tournament(
        'Comment Test Tournament',
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    _join_all(tournament, [user1, user2], grant_ticket)

    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    match = matches[0]

    # Add comment
    add_result = tournament_match_service.add_comment(
        match.id, admin_user.id, 'Great match!'
    )
    assert add_result.is_ok()

    # Get comments
    comments = tournament_match_service.get_comments_from_match(match.id)
    assert len(comments) == 1
    assert comments[0].comment == 'Great match!'
    assert comments[0].created_by == admin_user.id

    # Update comment
    comment_id = comments[0].id
    update_result = tournament_match_service.update_comment(
        comment_id, 'Amazing match!'
    )
    assert update_result.is_ok()

    # Verify update
    updated_comments = tournament_match_service.get_comments_from_match(
        match.id
    )
    assert updated_comments[0].comment == 'Amazing match!'

    # Delete comment
    delete_result = tournament_match_service.delete_comment(
        comment_id, match.id
    )
    assert delete_result.is_ok()

    # Verify deletion
    final_comments = tournament_match_service.get_comments_from_match(match.id)
    assert len(final_comments) == 0


def test_comment_length_validation(party, user1, user2, admin_user, grant_ticket):
    """Test that comments exceeding 1000 characters are rejected."""
    tournament = _create_tournament(
        'Long Comment Test',
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    _join_all(tournament, [user1, user2], grant_ticket)

    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    match = matches[0]

    # Try to add comment that's too long
    long_comment = 'x' * 1001
    result = tournament_match_service.add_comment(
        match.id, admin_user.id, long_comment
    )
    assert result.is_err()
    assert 'cannot exceed 1000 characters' in result.unwrap_err()

    # Comment at exactly 1000 chars should work
    limit_comment = 'x' * 1000
    limit_result = tournament_match_service.add_comment(
        match.id, admin_user.id, limit_comment
    )
    assert limit_result.is_ok()

    comments = tournament_match_service.get_comments_from_match(match.id)
    assert len(comments) == 1
    assert len(comments[0].comment) == 1000


def test_team_tournament_bracket_workflow(
    party, user1, user2, user3, user4, grant_ticket
):
    """Test bracket generation for team tournament."""
    tournament = _create_tournament(
        'Team Bracket Test',
        contestant_type=ContestantType.TEAM,
        max_teams=4,
        min_players_in_team=1,
        max_players_in_team=2,
    )

    # All team members must be registered participants.
    _join_all(tournament, [user1, user2, user3, user4], grant_ticket)

    # Create teams
    team1_result = tournament_team_service.create_team(
        tournament.id, 'Team Alpha', user1.id
    )
    team2_result = tournament_team_service.create_team(
        tournament.id, 'Team Beta', user3.id
    )

    assert team1_result.is_ok()
    assert team2_result.is_ok()

    team1, _ = team1_result.unwrap()
    team2, _ = team2_result.unwrap()

    # Add members to teams
    add_result1 = tournament_team_service.admin_add_member(team1.id, user2.id)
    assert add_result1.is_ok()
    add_result2 = tournament_team_service.admin_add_member(team2.id, user4.id)
    assert add_result2.is_ok()

    # Generate bracket (creates and seeds all matches)
    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    # 2 teams = 1 match
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    assert len(matches) == 1


def test_bracket_with_defwins(party, user1, user2, user3, grant_ticket):
    """Test bracket generation with DEFWIN (bye) entries."""
    tournament = _create_tournament(
        'DEFWIN Test Tournament',
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )

    # Add 3 participants (will require a bye to round up to 4)
    _join_all(tournament, [user1, user2, user3], grant_ticket)

    # Generate bracket
    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    # 3 players round up to a 4-slot bracket = 2 first-round matches
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    first_round = [m for m in matches if m.round == 0]
    assert len(first_round) == 2

    # Exactly one bye (DEFWIN): one first-round match has only one
    # contestant, whose opponent slot was a DEFWIN entry.
    defwin_count = sum(
        1
        for match in first_round
        if len(
            tournament_match_service.get_contestants_for_match(match.id)
        )
        == 1
    )
    assert defwin_count == 1


def test_reset_match(party, user1, user2, grant_ticket):
    """Test resetting a match."""
    tournament = _create_tournament(
        'Reset Match Test',
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    _join_all(tournament, [user1, user2], grant_ticket)

    generate_result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generate_result.is_ok()

    # Get match
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    assert len(matches) == 1
    match = matches[0]

    # Reset match
    tournament_match_service.reset_match(match.id)

    # Verify match deleted
    remaining_matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert len(remaining_matches) == 0


def test_bracket_generation_rejects_empty_team(
    party, user1, user2, user3, grant_ticket
):
    """Test that bracket generation rejects teams with no members."""
    tournament = _create_tournament(
        'Empty Team Reject Test',
        contestant_type=ContestantType.TEAM,
        max_teams=4,
        min_players_in_team=1,
        max_players_in_team=2,
    )

    # Register participants
    participants = _join_all(tournament, [user1, user2, user3], grant_ticket)
    participant2 = participants[user2.id]

    # Create two teams (captains are auto-assigned as members)
    team1_result = tournament_team_service.create_team(
        tournament.id, 'Full Team', user1.id
    )
    team2_result = tournament_team_service.create_team(
        tournament.id, 'Ghost Team', user2.id
    )
    assert team1_result.is_ok()
    assert team2_result.is_ok()

    # Add an extra member to team1 so it clearly has members
    team1, _ = team1_result.unwrap()
    add_result = tournament_team_service.admin_add_member(team1.id, user3.id)
    assert add_result.is_ok()

    # Make team2 empty by un-assigning its captain
    updated_participant = dataclasses.replace(participant2, team_id=None)
    tournament_repository.update_participant(updated_participant)

    # Attempt bracket generation -- should fail with Err
    result = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )

    assert result.is_err()
    error_msg = result.unwrap_err()
    assert 'Ghost Team' in error_msg
    assert 'no members' in error_msg
