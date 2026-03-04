"""
tests.unit.services.lan_tournament.test_tournament_score_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, UTC, timedelta
from unittest.mock import patch

from byceps.services.lan_tournament.models.score_submission import (
    ScoreSubmissionID,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.lan_tournament import tournament_score_service
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')


def _create_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'Test Highscore Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'updated_at': None,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': None,
        'tournament_mode': TournamentMode.HIGHSCORE,
        'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _make_db_submission(
    *,
    sub_id=None,
    tournament_id=None,
    participant_id=None,
    team_id=None,
    score: int,
    submitted_at=None,
    submitted_by=None,
    is_official: bool = True,
    note=None,
):
    from types import SimpleNamespace

    sub = SimpleNamespace()
    sub.id = sub_id or ScoreSubmissionID(generate_uuid())
    sub.tournament_id = tournament_id or TOURNAMENT_ID
    sub.participant_id = participant_id
    sub.team_id = team_id
    sub.score = score
    sub.submitted_at = submitted_at or NOW
    sub.submitted_by = submitted_by
    sub.is_official = is_official
    sub.note = note
    return sub


# -------------------------------------------------------------------- #
# submit_score


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_submit_score_valid(mock_repo):
    """Valid score submission returns Ok(ScoreSubmission)."""
    tournament = _create_tournament()
    mock_repo.get_tournament.return_value = tournament
    pid = TournamentParticipantID(generate_uuid())

    with patch(
        'byceps.services.lan_tournament.tournament_score_service'
        '.DbScoreSubmission'
    ) as MockDbSub:
        mock_sub = _make_db_submission(score=42, participant_id=pid)
        MockDbSub.return_value = mock_sub

        result = tournament_score_service.submit_score(
            TOURNAMENT_ID,
            42,
            participant_id=pid,
        )

    assert result.is_ok()
    submission = result.unwrap()
    assert submission.score == 42
    assert submission.participant_id == pid


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_submit_score_negative_rejected(mock_repo):
    """Negative score returns Err."""
    tournament = _create_tournament()
    mock_repo.get_tournament.return_value = tournament
    pid = TournamentParticipantID(generate_uuid())

    result = tournament_score_service.submit_score(
        TOURNAMENT_ID,
        -5,
        participant_id=pid,
    )

    assert result.is_err()
    assert 'negative' in result.unwrap_err().lower()


def test_submit_score_both_ids_none_returns_error():
    """Both participant_id and team_id being None returns Err."""
    result = tournament_score_service.submit_score(
        TOURNAMENT_ID,
        100,
        participant_id=None,
        team_id=None,
    )
    assert result.is_err()
    assert 'participant_id or team_id' in result.unwrap_err()


def test_submit_score_both_ids_provided_returns_error():
    """Both participant_id and team_id being set returns Err."""
    pid = TournamentParticipantID(generate_uuid())
    tid = TournamentTeamID(generate_uuid())

    result = tournament_score_service.submit_score(
        TOURNAMENT_ID,
        100,
        participant_id=pid,
        team_id=tid,
    )
    assert result.is_err()
    assert 'Only one of' in result.unwrap_err()


# -------------------------------------------------------------------- #
# get_leaderboard


def _build_leaderboard_mocks(
    mock_repo,
    *,
    score_ordering: ScoreOrdering = ScoreOrdering.HIGHER_IS_BETTER,
    submissions: list,
):
    tournament = _create_tournament(score_ordering=score_ordering)
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_official_submissions_for_tournament.return_value = submissions


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_get_leaderboard_higher_is_better(mock_repo):
    """Higher score first when score_ordering = higher_is_better."""
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    subs = [
        _make_db_submission(score=10, participant_id=pid_a),
        _make_db_submission(score=50, participant_id=pid_b),
    ]
    _build_leaderboard_mocks(
        mock_repo,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        submissions=subs,
    )

    result = tournament_score_service.get_leaderboard(TOURNAMENT_ID)

    assert result.is_ok()
    board = result.unwrap()
    assert board[0].score == 50
    assert board[1].score == 10


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_get_leaderboard_lower_is_better(mock_repo):
    """Lower score first when score_ordering = lower_is_better."""
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    subs = [
        _make_db_submission(score=10, participant_id=pid_a),
        _make_db_submission(score=50, participant_id=pid_b),
    ]
    _build_leaderboard_mocks(
        mock_repo,
        score_ordering=ScoreOrdering.LOWER_IS_BETTER,
        submissions=subs,
    )

    result = tournament_score_service.get_leaderboard(TOURNAMENT_ID)

    assert result.is_ok()
    board = result.unwrap()
    assert board[0].score == 10
    assert board[1].score == 50


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_get_leaderboard_tiebreak_by_time(mock_repo):
    """Same score => earlier submission wins."""
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    earlier = NOW - timedelta(hours=1)
    later = NOW
    subs = [
        _make_db_submission(
            score=100, participant_id=pid_a, submitted_at=later
        ),
        _make_db_submission(
            score=100, participant_id=pid_b, submitted_at=earlier
        ),
    ]
    _build_leaderboard_mocks(
        mock_repo,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        submissions=subs,
    )

    result = tournament_score_service.get_leaderboard(TOURNAMENT_ID)

    assert result.is_ok()
    board = result.unwrap()
    assert board[0].participant_id == pid_b  # earlier wins


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_get_leaderboard_best_score_only(mock_repo):
    """Multiple submissions from same contestant: only best counts."""
    pid_a = TournamentParticipantID(generate_uuid())
    subs = [
        _make_db_submission(
            score=30,
            participant_id=pid_a,
            submitted_at=NOW - timedelta(hours=2),
        ),
        _make_db_submission(
            score=80,
            participant_id=pid_a,
            submitted_at=NOW - timedelta(hours=1),
        ),
        _make_db_submission(
            score=50,
            participant_id=pid_a,
            submitted_at=NOW,
        ),
    ]
    _build_leaderboard_mocks(
        mock_repo,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        submissions=subs,
    )

    result = tournament_score_service.get_leaderboard(TOURNAMENT_ID)

    assert result.is_ok()
    board = result.unwrap()
    # Only 1 entry per contestant with best score (80)
    assert len(board) == 1
    assert board[0].score == 80


# -------------------------------------------------------------------- #
# delete_scores_for_tournament


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_delete_scores_for_tournament_calls_repository(mock_repo):
    """delete_scores_for_tournament delegates to repository and returns Ok."""
    result = tournament_score_service.delete_scores_for_tournament(
        TOURNAMENT_ID
    )
    assert result.is_ok()
    mock_repo.delete_submissions_for_tournament.assert_called_once_with(
        TOURNAMENT_ID
    )


# -------------------------------------------------------------------- #
# additional edge-case tests


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_submit_score_non_highscore_mode_err(mock_repo):
    """Submit score on non-HIGHSCORE tournament returns Err."""
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )
    mock_repo.get_tournament.return_value = tournament
    pid = TournamentParticipantID(generate_uuid())

    result = tournament_score_service.submit_score(
        TOURNAMENT_ID,
        42,
        participant_id=pid,
    )

    assert result.is_err()
    assert 'HIGHSCORE' in result.unwrap_err()


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_submit_score_with_team_id(mock_repo):
    """Score submission with team_id returns Ok(ScoreSubmission)."""
    tournament = _create_tournament()
    mock_repo.get_tournament.return_value = tournament
    tid = TournamentTeamID(generate_uuid())

    with patch(
        'byceps.services.lan_tournament.tournament_score_service'
        '.DbScoreSubmission'
    ) as MockDbSub:
        mock_sub = _make_db_submission(score=99, team_id=tid)
        MockDbSub.return_value = mock_sub

        result = tournament_score_service.submit_score(
            TOURNAMENT_ID,
            99,
            team_id=tid,
        )

    assert result.is_ok()
    submission = result.unwrap()
    assert submission.score == 99
    assert submission.team_id == tid


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_get_leaderboard_non_highscore_mode_err(mock_repo):
    """Leaderboard on non-HIGHSCORE tournament returns Err."""
    tournament = _create_tournament(
        tournament_mode=TournamentMode.ROUND_ROBIN,
    )
    mock_repo.get_tournament.return_value = tournament

    result = tournament_score_service.get_leaderboard(TOURNAMENT_ID)

    assert result.is_err()
    assert 'HIGHSCORE' in result.unwrap_err()


@patch(
    'byceps.services.lan_tournament.tournament_score_service'
    '.tournament_repository'
)
def test_get_leaderboard_empty_submissions(mock_repo):
    """Leaderboard with no submissions returns Ok([])."""
    _build_leaderboard_mocks(
        mock_repo,
        submissions=[],
    )

    result = tournament_score_service.get_leaderboard(TOURNAMENT_ID)

    assert result.is_ok()
    assert result.unwrap() == []
