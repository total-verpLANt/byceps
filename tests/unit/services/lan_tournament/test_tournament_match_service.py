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
    TournamentMatch,
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


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_4_players_total_matches(mock_repo):
    """4 players => bracket_size 4 => 3 total matches."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 3  # 4-1 = 3 matches


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_8_teams_total_matches(mock_repo):
    """8 teams => bracket_size 8 => 7 total matches."""
    tournament = _create_tournament(contestant_type=ContestantType.TEAM)
    teams = [
        _create_mock_team(TournamentTeamID(generate_uuid())) for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = teams
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 7  # 8-1 = 7 matches


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_all_rounds_created(mock_repo):
    """4 players => 2 rounds (round 0 + final)."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    # Check that create_match was called 3 times (3 matches)
    assert mock_repo.create_match.call_count == 3

    # Verify round assignments in created matches
    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]
    rounds = sorted(m.round for m in created_matches)
    # 2 round-0 matches + 1 final (round 1)
    assert rounds == [0, 0, 1]


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_linkage_correct(mock_repo):
    """Round 0 matches should link to the final match."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    # Final match (created first due to reverse order) has
    # no next_match_id
    final_matches = [m for m in created_matches if m.round == 1]
    assert len(final_matches) == 1
    assert final_matches[0].next_match_id is None

    # Round 0 matches should point to the final
    round0_matches = [m for m in created_matches if m.round == 0]
    assert len(round0_matches) == 2
    for m in round0_matches:
        assert m.next_match_id == final_matches[0].id


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_bye_handling(mock_repo):
    """5 players => 3 BYEs; auto-advance solo contestants."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(5)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    # Track contestants created per match to simulate BYE
    # detection. For BYE matches, return only 1 contestant.
    contestant_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ] = {}

    def track_contestant(contestant):
        mid = contestant.tournament_match_id
        if mid not in contestant_by_match:
            contestant_by_match[mid] = []
        contestant_by_match[mid].append(contestant)

    mock_repo.create_match_contestant.side_effect = track_contestant

    def get_contestants(match_id):
        return contestant_by_match.get(match_id, [])

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 7  # 8-1 = 7 matches total


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_without_contestant_type(mock_repo):
    """Bracket generation fails without contestant type set."""
    tournament = _create_tournament(contestant_type=None)

    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert 'contestant type' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_with_one_contestant(mock_repo):
    """Bracket generation fails with only 1 contestant."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert '2 contestants' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# set_score
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_with_valid_score(mock_repo):
    """Test setting a valid score."""
    contestant_id = TournamentParticipantID(generate_uuid())
    contestant = _create_match_contestant(
        participant_id=contestant_id, score=None
    )

    mock_repo.find_contestant_for_match.return_value = contestant

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, 10)

    assert result.is_ok()
    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 10)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_with_zero(mock_repo):
    """Test setting score to zero is valid."""
    contestant_id = TournamentParticipantID(generate_uuid())
    contestant = _create_match_contestant(
        participant_id=contestant_id, score=None
    )

    mock_repo.find_contestant_for_match.return_value = contestant

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, 0)

    assert result.is_ok()
    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 0)


def test_set_score_with_negative_score():
    """Test that negative scores are rejected."""
    contestant_id = TournamentParticipantID(generate_uuid())

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, -1)

    assert result.is_err()
    assert 'negative' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_for_team(mock_repo):
    """Test setting score for a team contestant."""
    team_id = TournamentTeamID(generate_uuid())
    contestant = _create_match_contestant(team_id=team_id, score=None)

    # First lookup (as participant) returns None, second (as team)
    # succeeds
    mock_repo.find_contestant_for_match.side_effect = [
        None,
        contestant,
    ]

    result = tournament_match_service.set_score(MATCH_ID, team_id, 5)

    assert result.is_ok()
    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 5)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_for_nonexistent_contestant(mock_repo):
    """Test that setting score for nonexistent contestant fails."""
    contestant_id = TournamentParticipantID(generate_uuid())

    # Both lookups return None
    mock_repo.find_contestant_for_match.side_effect = [None, None]

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, 10)

    assert result.is_err()
    assert 'not found' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# confirm_match
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_with_scores(mock_repo):
    """Test confirming a match with scores set."""
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_without_scores(mock_repo):
    """Test that confirming fails if contestants lack scores."""
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=None,
        ),
    ]

    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'scores' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_already_confirmed(mock_repo):
    """Test that double-confirmation is rejected."""
    match = _create_match(confirmed_by=UserID(generate_uuid()))

    mock_repo.get_match.return_value = match

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'already confirmed' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_with_less_than_two_contestants(mock_repo):
    """Test that confirming fails with less than 2 contestants."""
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        )
    ]

    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert '2 contestants' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_nonexistent_match(mock_repo):
    """Test that confirming nonexistent match raises."""
    mock_repo.get_match.side_effect = ValueError(
        f'Unknown match ID "{MATCH_ID}"'
    )

    with pytest.raises(ValueError, match='Unknown match ID'):
        tournament_match_service.confirm_match(MATCH_ID, USER_ID)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_advances_winner_to_next_match(mock_repo):
    """Test that confirming advances the winner to the next match."""
    next_match_id = TournamentMatchID(generate_uuid())
    match = _create_match(confirmed_by=None, next_match_id=next_match_id)

    winner_participant_id = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(
            participant_id=winner_participant_id, score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID)

    # Verify contestant was created in the next match
    mock_repo.create_match_contestant.assert_called_once()
    created = mock_repo.create_match_contestant.call_args[0][0]
    assert created.tournament_match_id == next_match_id
    assert created.participant_id == winner_participant_id


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_no_advancement_without_next_match(mock_repo):
    """Test that confirming without next_match_id does not advance."""
    match = _create_match(confirmed_by=None, next_match_id=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.create_match_contestant.assert_not_called()


# -------------------------------------------------------------------- #
# unconfirm_match
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_success(mock_repo):
    """Test unconfirming a confirmed match."""
    confirmed_by = UserID(generate_uuid())
    match = _create_match(confirmed_by=confirmed_by)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.unconfirm_match.assert_called_once_with(MATCH_ID)
    mock_repo.commit_session.assert_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_not_confirmed_returns_err(mock_repo):
    """Test that unconfirming an unconfirmed match returns Err."""
    match = _create_match(confirmed_by=None)

    mock_repo.get_match.return_value = match

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'not confirmed' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_not_found_returns_err(mock_repo):
    """Test that unconfirming a nonexistent match returns Err."""
    mock_repo.get_match.return_value = None

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'not found' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_retracts_advanced_contestant(mock_repo):
    """Test that unconfirming retracts the advanced contestant."""
    next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by, next_match_id=next_match_id
    )

    winner_participant_id = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(
            participant_id=winner_participant_id, score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    # Next match is not confirmed, so no cascade needed
    next_match = _create_match(match_id=next_match_id, confirmed_by=None)

    mock_repo.get_match.side_effect = [match, next_match]
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Verify contestant was removed from the next match
    mock_repo.delete_contestant_from_match.assert_called_once_with(
        next_match_id,
        team_id=None,
        participant_id=winner_participant_id,
    )


# -------------------------------------------------------------------- #
# add_comment
# -------------------------------------------------------------------- #


@patch('byceps.util.uuid.generate_uuid7')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_add_comment_valid(mock_repo, mock_uuid):
    """Test adding a valid comment."""
    mock_uuid.return_value = generate_uuid()

    result = tournament_match_service.add_comment(
        MATCH_ID, USER_ID, 'Great match!'
    )

    assert result.is_ok()
    mock_repo.create_match_comment.assert_called_once()
    call_args = mock_repo.create_match_comment.call_args[0][0]
    assert call_args.tournament_match_id == MATCH_ID
    assert call_args.created_by == USER_ID
    assert call_args.comment == 'Great match!'


def test_add_comment_too_long():
    """Test that comments exceeding 1000 chars are rejected."""
    long_comment = 'x' * 1001

    result = tournament_match_service.add_comment(
        MATCH_ID, USER_ID, long_comment
    )

    assert result.is_err()
    assert '1000' in result.unwrap_err()


def test_add_comment_at_limit():
    """Test that exactly 1000 char comment is accepted."""
    limit_comment = 'x' * 1000

    with (
        patch(
            'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
        ) as mock_repo,
        patch('byceps.util.uuid.generate_uuid7'),
    ):
        result = tournament_match_service.add_comment(
            MATCH_ID, USER_ID, limit_comment
        )

        assert result.is_ok()
        mock_repo.create_match_comment.assert_called_once()


# -------------------------------------------------------------------- #
# update_comment
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_update_comment_valid(mock_repo):
    """Test updating a comment with valid text."""
    comment_id = TournamentMatchCommentID(generate_uuid())

    result = tournament_match_service.update_comment(comment_id, 'New comment')

    assert result.is_ok()
    mock_repo.update_match_comment.assert_called_once_with(
        comment_id, 'New comment'
    )


def test_update_comment_too_long():
    """Test that updating with too long text fails."""
    comment_id = TournamentMatchCommentID(generate_uuid())
    long_comment = 'x' * 1001

    result = tournament_match_service.update_comment(comment_id, long_comment)

    assert result.is_err()
    assert '1000' in result.unwrap_err()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_update_nonexistent_comment(mock_repo):
    """Test that updating nonexistent comment raises."""
    comment_id = TournamentMatchCommentID(generate_uuid())

    mock_repo.update_match_comment.side_effect = ValueError(
        f'Unknown comment ID "{comment_id}"'
    )

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


def _create_match(
    *,
    match_id: TournamentMatchID | None = None,
    confirmed_by: UserID | None = None,
    round: int | None = None,
    next_match_id: TournamentMatchID | None = None,
) -> TournamentMatch:
    """Create a tournament match for testing."""
    if match_id is None:
        match_id = MATCH_ID
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=round,
        next_match_id=next_match_id,
        confirmed_by=confirmed_by,
        created_at=NOW,
    )


def _create_mock_participant(participant_id):
    """Create a mock participant for testing."""
    mock = Mock()
    mock.id = participant_id
    return mock


def _create_mock_team(team_id):
    """Create a mock team for testing."""
    mock = Mock()
    mock.id = team_id
    return mock


def _create_match_contestant(
    contestant_id=None,
    participant_id=None,
    team_id=None,
    score=None,
):
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
