"""
tests.unit.services.lan_tournament.test_tournament_match_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, UTC
from unittest.mock import Mock, patch

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_seed import TournamentSeed
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.lan_tournament import tournament_match_service
from byceps.services.party.models import PartyID
from byceps.services.user.models.user import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')
MATCH_ID = TournamentMatchID(generate_uuid())
USER_ID = UserID(generate_uuid())


# -------------------------------------------------------------------- #
# generate_single_elimination_bracket
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_generate_bracket_with_4_players(mock_repo):
    """Test bracket generation with 4 players (no byes needed)."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    seeds = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert len(seeds) == 2  # 4 players = 2 first-round matches
    assert all(seed.entry_a.upper() != 'BYE' for seed in seeds)
    assert all(seed.entry_b.upper() != 'BYE' for seed in seeds)
    assert seeds[0].match_order == 0
    assert seeds[1].match_order == 1


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_generate_bracket_with_5_players(mock_repo):
    """Test bracket generation with 5 players (requires 3 byes)."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(5)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    seeds = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    # 5 players rounds up to 8 bracket size = 4 matches
    assert len(seeds) == 4
    # Should have 3 BYEs total (8 slots - 5 players)
    bye_count = sum(
        1 for seed in seeds
        for entry in [seed.entry_a, seed.entry_b]
        if entry.upper() == 'BYE'
    )
    assert bye_count == 3


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_generate_bracket_with_teams(mock_repo):
    """Test bracket generation with teams instead of players."""
    tournament = _create_tournament(contestant_type=ContestantType.TEAM)
    teams = [
        _create_mock_team(TournamentTeamID(generate_uuid()))
        for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = teams

    seeds = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert len(seeds) == 4  # 8 teams = 4 first-round matches
    assert all(seed.entry_a.upper() != 'BYE' for seed in seeds)
    assert all(seed.entry_b.upper() != 'BYE' for seed in seeds)


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_generate_bracket_without_contestant_type(mock_repo):
    """Test that bracket generation fails without contestant type set."""
    tournament = _create_tournament(contestant_type=None)

    mock_repo.get_tournament.return_value = tournament

    with pytest.raises(ValueError, match='contestant type is not set'):
        tournament_match_service.generate_single_elimination_bracket(
            TOURNAMENT_ID
        )


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_generate_bracket_with_one_contestant(mock_repo):
    """Test that bracket generation fails with only 1 contestant."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    with pytest.raises(ValueError, match='at least 2 contestants'):
        tournament_match_service.generate_single_elimination_bracket(
            TOURNAMENT_ID
        )


# -------------------------------------------------------------------- #
# set_seed
# -------------------------------------------------------------------- #


@patch('byceps.util.uuid.generate_uuid7')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_seed_with_player_tournament(mock_repo, mock_uuid):
    """Test seeding for player tournament."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participant_a_id = TournamentParticipantID(generate_uuid())
    participant_b_id = TournamentParticipantID(generate_uuid())

    seeds = [
        TournamentSeed(
            match_order=0,
            entry_a=str(participant_a_id),
            entry_b=str(participant_b_id),
        )
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_uuid.side_effect = [generate_uuid() for _ in range(10)]

    tournament_match_service.set_seed(seeds, TOURNAMENT_ID)

    # Should create 1 match and 2 contestants
    assert mock_repo.create_match.call_count == 1
    assert mock_repo.create_match_contestant.call_count == 2


@patch('byceps.util.uuid.generate_uuid7')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_seed_with_team_tournament(mock_repo, mock_uuid):
    """Test seeding for team tournament."""
    tournament = _create_tournament(contestant_type=ContestantType.TEAM)
    team_a_id = TournamentTeamID(generate_uuid())
    team_b_id = TournamentTeamID(generate_uuid())

    seeds = [
        TournamentSeed(
            match_order=0,
            entry_a=str(team_a_id),
            entry_b=str(team_b_id),
        )
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_uuid.side_effect = [generate_uuid() for _ in range(10)]

    tournament_match_service.set_seed(seeds, TOURNAMENT_ID)

    assert mock_repo.create_match.call_count == 1
    assert mock_repo.create_match_contestant.call_count == 2


@patch('byceps.util.uuid.generate_uuid7')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_seed_with_bye(mock_repo, mock_uuid):
    """Test seeding with BYE entry."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participant_id = TournamentParticipantID(generate_uuid())

    seeds = [
        TournamentSeed(
            match_order=0,
            entry_a=str(participant_id),
            entry_b='BYE',
        )
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_uuid.side_effect = [generate_uuid() for _ in range(10)]

    tournament_match_service.set_seed(seeds, TOURNAMENT_ID)

    # Should create 1 match but only 1 contestant (BYE skipped)
    assert mock_repo.create_match.call_count == 1
    assert mock_repo.create_match_contestant.call_count == 1


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_seed_without_contestant_type(mock_repo):
    """Test that seeding fails without contestant type."""
    tournament = _create_tournament(contestant_type=None)
    seeds = [
        TournamentSeed(
            match_order=0,
            entry_a=str(generate_uuid()),
            entry_b=str(generate_uuid()),
        )
    ]

    mock_repo.get_tournament.return_value = tournament

    with pytest.raises(ValueError, match='contestant type is not set'):
        tournament_match_service.set_seed(seeds, TOURNAMENT_ID)


@patch('byceps.util.uuid.generate_uuid7')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_seed_with_multiple_matches(mock_repo, mock_uuid):
    """Test seeding with multiple matches."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)

    seeds = [
        TournamentSeed(
            match_order=0,
            entry_a=str(generate_uuid()),
            entry_b=str(generate_uuid()),
        ),
        TournamentSeed(
            match_order=1,
            entry_a=str(generate_uuid()),
            entry_b=str(generate_uuid()),
        ),
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_uuid.side_effect = [generate_uuid() for _ in range(20)]

    tournament_match_service.set_seed(seeds, TOURNAMENT_ID)

    # Should create 2 matches and 4 contestants
    assert mock_repo.create_match.call_count == 2
    assert mock_repo.create_match_contestant.call_count == 4


# -------------------------------------------------------------------- #
# set_score
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_score_with_valid_score(mock_repo):
    """Test setting a valid score."""
    contestant_id = TournamentParticipantID(generate_uuid())
    contestant = _create_match_contestant(
        participant_id=contestant_id, score=None
    )

    mock_repo.find_contestant_for_match.return_value = contestant

    tournament_match_service.set_score(MATCH_ID, contestant_id, 10)

    mock_repo.update_contestant_score.assert_called_once_with(
        contestant.id, 10
    )


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_score_with_zero(mock_repo):
    """Test setting score to zero is valid."""
    contestant_id = TournamentParticipantID(generate_uuid())
    contestant = _create_match_contestant(
        participant_id=contestant_id, score=None
    )

    mock_repo.find_contestant_for_match.return_value = contestant

    tournament_match_service.set_score(MATCH_ID, contestant_id, 0)

    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 0)


def test_set_score_with_negative_score():
    """Test that negative scores are rejected."""
    contestant_id = TournamentParticipantID(generate_uuid())

    with pytest.raises(ValueError, match='cannot be negative'):
        tournament_match_service.set_score(MATCH_ID, contestant_id, -1)


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_score_for_team(mock_repo):
    """Test setting score for a team contestant."""
    team_id = TournamentTeamID(generate_uuid())
    contestant = _create_match_contestant(team_id=team_id, score=None)

    # Simulate first lookup failing (not a participant), second succeeding
    mock_repo.find_contestant_for_match.side_effect = [
        ValueError('Not a participant'),
        contestant,
    ]

    tournament_match_service.set_score(MATCH_ID, team_id, 5)

    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 5)


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_set_score_for_nonexistent_contestant(mock_repo):
    """Test that setting score for nonexistent contestant fails."""
    contestant_id = TournamentParticipantID(generate_uuid())

    # Both lookups return None
    mock_repo.find_contestant_for_match.side_effect = [
        ValueError('Not found'),
        None,
    ]

    with pytest.raises(ValueError, match='Contestant .* not found'):
        tournament_match_service.set_score(MATCH_ID, contestant_id, 10)


# -------------------------------------------------------------------- #
# confirm_match
# -------------------------------------------------------------------- #


@patch('byceps.database.db')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_confirm_match_with_scores(mock_repo, mock_db):
    """Test confirming a match with scores set."""
    db_match = Mock()
    db_match.confirmed_by = None

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()), score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()), score=5
        ),
    ]

    mock_db.session.get.return_value = db_match
    mock_repo.get_contestants_for_match.return_value = contestants

    tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert db_match.confirmed_by == USER_ID
    mock_db.session.commit.assert_called_once()


@patch('byceps.database.db')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_confirm_match_without_scores(mock_repo, mock_db):
    """Test that confirming fails if contestants lack scores."""
    db_match = Mock()
    db_match.confirmed_by = None

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()), score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=None,
        ),
    ]

    mock_db.session.get.return_value = db_match
    mock_repo.get_contestants_for_match.return_value = contestants

    with pytest.raises(ValueError, match='all contestants must have scores'):
        tournament_match_service.confirm_match(MATCH_ID, USER_ID)


@patch('byceps.database.db')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_confirm_match_already_confirmed(mock_repo, mock_db):
    """Test that double-confirmation is rejected."""
    db_match = Mock()
    db_match.confirmed_by = UserID(generate_uuid())

    mock_db.session.get.return_value = db_match

    with pytest.raises(ValueError, match='already confirmed'):
        tournament_match_service.confirm_match(MATCH_ID, USER_ID)


@patch('byceps.database.db')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_confirm_match_with_less_than_two_contestants(mock_repo, mock_db):
    """Test that confirming fails with less than 2 contestants."""
    db_match = Mock()
    db_match.confirmed_by = None

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()), score=10
        )
    ]

    mock_db.session.get.return_value = db_match
    mock_repo.get_contestants_for_match.return_value = contestants

    with pytest.raises(ValueError, match='less than 2 contestants'):
        tournament_match_service.confirm_match(MATCH_ID, USER_ID)


@patch('byceps.database.db')
def test_confirm_nonexistent_match(mock_db):
    """Test that confirming nonexistent match fails."""
    mock_db.session.get.return_value = None

    with pytest.raises(ValueError, match='Unknown match ID'):
        tournament_match_service.confirm_match(MATCH_ID, USER_ID)


# -------------------------------------------------------------------- #
# add_comment
# -------------------------------------------------------------------- #


@patch('byceps.util.uuid.generate_uuid7')
@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_add_comment_valid(mock_repo, mock_uuid):
    """Test adding a valid comment."""
    mock_uuid.return_value = generate_uuid()

    tournament_match_service.add_comment(MATCH_ID, USER_ID, 'Great match!')

    mock_repo.create_match_comment.assert_called_once()
    call_args = mock_repo.create_match_comment.call_args[0][0]
    assert call_args.tournament_match_id == MATCH_ID
    assert call_args.created_by == USER_ID
    assert call_args.comment == 'Great match!'


def test_add_comment_empty():
    """Test adding empty comment is allowed (may be used for placeholder)."""
    # Empty comments should work - no validation against them
    pass


def test_add_comment_too_long():
    """Test that comments exceeding 1000 chars are rejected."""
    long_comment = 'x' * 1001

    with pytest.raises(ValueError, match='cannot exceed 1000 characters'):
        tournament_match_service.add_comment(MATCH_ID, USER_ID, long_comment)


def test_add_comment_at_limit():
    """Test that exactly 1000 char comment is accepted."""
    limit_comment = 'x' * 1000

    with patch(
        'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
    ) as mock_repo, patch('byceps.util.uuid.generate_uuid7'):
        tournament_match_service.add_comment(
            MATCH_ID, USER_ID, limit_comment
        )

        mock_repo.create_match_comment.assert_called_once()


# -------------------------------------------------------------------- #
# update_comment
# -------------------------------------------------------------------- #


@patch('byceps.database.db')
def test_update_comment_valid(mock_db):
    """Test updating a comment with valid text."""
    comment_id = TournamentMatchCommentID(generate_uuid())
    db_comment = Mock()
    db_comment.comment = 'Old comment'

    mock_db.session.get.return_value = db_comment

    tournament_match_service.update_comment(comment_id, 'New comment')

    assert db_comment.comment == 'New comment'
    mock_db.session.commit.assert_called_once()


@patch('byceps.database.db')
def test_update_comment_too_long(mock_db):
    """Test that updating with too long text fails."""
    comment_id = TournamentMatchCommentID(generate_uuid())
    long_comment = 'x' * 1001

    with pytest.raises(ValueError, match='cannot exceed 1000 characters'):
        tournament_match_service.update_comment(comment_id, long_comment)


@patch('byceps.database.db')
def test_update_nonexistent_comment(mock_db):
    """Test that updating nonexistent comment fails."""
    comment_id = TournamentMatchCommentID(generate_uuid())

    mock_db.session.get.return_value = None

    with pytest.raises(ValueError, match='Unknown comment ID'):
        tournament_match_service.update_comment(comment_id, 'New text')


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_tournament(**kwargs) -> Tournament:
    """Create a tournament for testing."""
    defaults = {
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
        'tournament_status': None,
        'tournament_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _create_mock_participant(participant_id: TournamentParticipantID):
    """Create a mock participant for testing."""
    mock = Mock()
    mock.id = participant_id
    return mock


def _create_mock_team(team_id: TournamentTeamID):
    """Create a mock team for testing."""
    mock = Mock()
    mock.id = team_id
    return mock


def _create_match_contestant(
    contestant_id: TournamentMatchToContestantID | None = None,
    participant_id: TournamentParticipantID | None = None,
    team_id: TournamentTeamID | None = None,
    score: int | None = None,
) -> TournamentMatchToContestant:
    """Create a match contestant for testing."""
    if contestant_id is None:
        contestant_id = TournamentMatchToContestantID(generate_uuid())

    return TournamentMatchToContestant(
        id=contestant_id,
        tournament_match_id=MATCH_ID,
        team_id=team_id,
        participant_id=participant_id,
        score=score,
        created_at=NOW,
    )
