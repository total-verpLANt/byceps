"""
tests.integration.services.lan_tournament.test_tournament_match_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
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


def test_generate_and_seed_bracket_for_solo_tournament(
    party, user1, user2, user3, user4, admin_user
):
    """Test full bracket workflow for solo player tournament."""
    # Create tournament
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Solo Bracket Test',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )

    # Open registration and add participants
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    result1 = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
    result2 = tournament_participant_service.join_tournament(
        tournament.id, user2.id
    )
    result3 = tournament_participant_service.join_tournament(
        tournament.id, user3.id
    )
    result4 = tournament_participant_service.join_tournament(
        tournament.id, user4.id
    )

    assert result1.is_ok()
    assert result2.is_ok()
    assert result3.is_ok()
    assert result4.is_ok()

    # Close registration and start tournament
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    )
    tournament_service.start_tournament(tournament.id)

    # Generate bracket
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )

    # 4 players = 2 matches in first round
    assert len(seeds) == 2
    assert all(seed.entry_a.upper() != 'DEFWIN' for seed in seeds)
    assert all(seed.entry_b.upper() != 'DEFWIN' for seed in seeds)

    # Set seeds to create matches
    tournament_match_service.set_seed(seeds, tournament.id)

    # Verify matches created
    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert len(matches) == 2
    assert all(match.confirmed_by is None for match in matches)


def test_set_scores_and_confirm_match(
    party, user1, user2, admin_user
):
    """Test setting scores and confirming match results."""
    # Create tournament
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Score Test Tournament',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    # Add participants
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    result1 = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
    result2 = tournament_participant_service.join_tournament(
        tournament.id, user2.id
    )
    participant1, _ = result1.unwrap()
    participant2, _ = result2.unwrap()

    # Start and seed
    tournament_service.start_tournament(tournament.id)
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    tournament_match_service.set_seed(seeds, tournament.id)

    # Get the match
    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    match = matches[0]

    # Set scores
    tournament_match_service.set_score(match.id, participant1.id, 10)
    tournament_match_service.set_score(match.id, participant2.id, 5)

    # Confirm match
    tournament_match_service.confirm_match(match.id, admin_user.id)

    # Verify confirmation
    confirmed_match = tournament_match_service.get_match(match.id)
    assert confirmed_match.confirmed_by == admin_user.id


def test_cannot_confirm_match_without_scores(
    party, user1, user2, admin_user
):
    """Test that match cannot be confirmed without scores."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'No Score Test',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    tournament_participant_service.join_tournament(tournament.id, user1.id)
    tournament_participant_service.join_tournament(tournament.id, user2.id)

    tournament_service.start_tournament(tournament.id)
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    tournament_match_service.set_seed(seeds, tournament.id)

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    match = matches[0]

    # Try to confirm without setting scores
    with pytest.raises(ValueError, match='must have scores'):
        tournament_match_service.confirm_match(match.id, admin_user.id)


def test_cannot_set_negative_score(party, user1, user2):
    """Test that negative scores are rejected."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Negative Score Test',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    result = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
    tournament_participant_service.join_tournament(tournament.id, user2.id)
    participant, _ = result.unwrap()

    tournament_service.start_tournament(tournament.id)
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    tournament_match_service.set_seed(seeds, tournament.id)

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    match = matches[0]

    with pytest.raises(ValueError, match='cannot be negative'):
        tournament_match_service.set_score(match.id, participant.id, -1)


def test_match_comments_workflow(party, user1, user2, admin_user):
    """Test adding, updating, and deleting match comments."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Comment Test Tournament',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    tournament_participant_service.join_tournament(tournament.id, user1.id)
    tournament_participant_service.join_tournament(tournament.id, user2.id)

    tournament_service.start_tournament(tournament.id)
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    tournament_match_service.set_seed(seeds, tournament.id)

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    match = matches[0]

    # Add comment
    tournament_match_service.add_comment(
        match.id, admin_user.id, 'Great match!'
    )

    # Get comments
    comments = tournament_match_service.get_comments_from_match(match.id)
    assert len(comments) == 1
    assert comments[0].comment == 'Great match!'
    assert comments[0].created_by == admin_user.id

    # Update comment
    comment_id = comments[0].id
    tournament_match_service.update_comment(comment_id, 'Amazing match!')

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


def test_comment_length_validation(party, user1, user2, admin_user):
    """Test that comments exceeding 1000 characters are rejected."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Long Comment Test',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    tournament_participant_service.join_tournament(tournament.id, user1.id)
    tournament_participant_service.join_tournament(tournament.id, user2.id)

    tournament_service.start_tournament(tournament.id)
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    tournament_match_service.set_seed(seeds, tournament.id)

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    match = matches[0]

    # Try to add comment that's too long
    long_comment = 'x' * 1001
    with pytest.raises(ValueError, match='cannot exceed 1000 characters'):
        tournament_match_service.add_comment(
            match.id, admin_user.id, long_comment
        )

    # Comment at exactly 1000 chars should work
    limit_comment = 'x' * 1000
    tournament_match_service.add_comment(
        match.id, admin_user.id, limit_comment
    )

    comments = tournament_match_service.get_comments_from_match(match.id)
    assert len(comments) == 1
    assert len(comments[0].comment) == 1000


def test_team_tournament_bracket_workflow(party, user1, user2, user3, user4):
    """Test bracket generation for team tournament."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Team Bracket Test',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.TEAM,
        max_teams=4,
        min_players_in_team=1,
        max_players_in_team=2,
    )

    # Open registration
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    # Create teams
    result1 = tournament_team_service.create_team(
        tournament.id, 'Team Alpha', user1.id
    )
    result2 = tournament_team_service.create_team(
        tournament.id, 'Team Beta', user3.id
    )

    assert result1.is_ok()
    assert result2.is_ok()

    team1, _ = result1.unwrap()
    team2, _ = result2.unwrap()

    # Add members to teams
    tournament_team_service.add_member_to_team(team1.id, user2.id)
    tournament_team_service.add_member_to_team(team2.id, user4.id)

    # Start tournament
    tournament_service.start_tournament(tournament.id)

    # Generate bracket
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )

    # 2 teams = 1 match
    assert len(seeds) == 1

    # Seed bracket
    tournament_match_service.set_seed(seeds, tournament.id)

    # Verify matches created
    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert len(matches) == 1


def test_bracket_with_defwins(party, user1, user2, user3):
    """Test bracket generation with DEFWIN entries."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'DEFWIN Test Tournament',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )

    # Add 3 participants (will require defwins to round up to 4)
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    tournament_participant_service.join_tournament(tournament.id, user1.id)
    tournament_participant_service.join_tournament(tournament.id, user2.id)
    tournament_participant_service.join_tournament(tournament.id, user3.id)

    tournament_service.start_tournament(tournament.id)

    # Generate bracket
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )

    # 3 players rounds up to 4 bracket size = 2 matches
    assert len(seeds) == 2

    # Count DEFWINs (should be 1 DEFWIN since we have 3 players in 4-slot bracket)
    defwin_count = sum(
        1
        for seed in seeds
        for entry in [seed.entry_a, seed.entry_b]
        if entry.upper() == 'DEFWIN'
    )
    assert defwin_count == 1


def test_reset_match(party, user1, user2):
    """Test resetting a match."""
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Reset Match Test',
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=4,
    )

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    tournament_participant_service.join_tournament(tournament.id, user1.id)
    tournament_participant_service.join_tournament(tournament.id, user2.id)

    tournament_service.start_tournament(tournament.id)
    seeds = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    tournament_match_service.set_seed(seeds, tournament.id)

    # Get match
    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert len(matches) == 1
    match = matches[0]

    # Reset match
    tournament_match_service.reset_match(match.id)

    # Verify match deleted
    remaining_matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert len(remaining_matches) == 0
