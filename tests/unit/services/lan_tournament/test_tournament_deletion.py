"""
tests.unit.services.lan_tournament.test_tournament_deletion
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for CASCADE deletion behavior in tournament service layer.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from unittest.mock import call, patch


from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.user.models.user import UserID

from tests.helpers import generate_uuid


@patch('byceps.services.lan_tournament.tournament_service.tournament_repository')
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_delete_tournament_cascades_all_dependencies(
    mock_signals, mock_repository
):
    """Test that delete_tournament() deletes all dependent entities in correct order."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())

    # Execute deletion
    tournament_service.delete_tournament(tournament_id)

    # Verify deletion calls in correct order (children first, then parent)
    expected_calls = [
        call.delete_comments_for_tournament(tournament_id),
        call.delete_contestants_for_tournament(tournament_id),
        call.delete_matches_for_tournament(tournament_id),
        call.delete_participants_for_tournament(tournament_id),
        call.delete_teams_for_tournament(tournament_id),
        call.delete_tournament(tournament_id),
    ]

    assert mock_repository.method_calls == expected_calls

    # Verify event emitted
    assert mock_signals.tournament_deleted.send.called


@patch('byceps.services.lan_tournament.tournament_team_service.tournament_repository')
@patch('byceps.services.lan_tournament.tournament_team_service.signals')
def test_delete_team_removes_references_before_deletion(
    mock_signals, mock_repository
):
    """Test that delete_team() sets team_id to NULL on participants and contestants."""
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_team_service

    team_id = TournamentTeamID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    captain_id = UserID(generate_uuid())

    # Mock team lookup
    mock_team = TournamentTeam(
        id=team_id,
        tournament_id=tournament_id,
        name='Test Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=captain_id,
        join_code=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.find_team.return_value = mock_team

    # Execute deletion (admin bypass - no current_user_id check)
    result = tournament_team_service.delete_team(team_id)

    # Verify success
    assert result.is_ok()

    # Verify deletion calls in correct order
    expected_calls = [
        call.find_team(team_id),
        call.remove_team_from_participants(team_id),
        call.remove_team_from_contestants(team_id),
        call.delete_team(team_id),
    ]

    assert mock_repository.method_calls == expected_calls

    # Verify event emitted
    assert mock_signals.team_deleted.send.called


@patch('byceps.services.lan_tournament.tournament_team_service.tournament_repository')
def test_delete_team_enforces_captain_authorization(mock_repository):
    """Test that delete_team() only allows captain to delete (unless admin bypass)."""
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_team_service

    team_id = TournamentTeamID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    captain_id = UserID(generate_uuid())
    other_user_id = UserID(generate_uuid())

    # Mock team lookup
    mock_team = TournamentTeam(
        id=team_id,
        tournament_id=tournament_id,
        name='Test Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=captain_id,
        join_code=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.find_team.return_value = mock_team

    # Execute deletion as non-captain
    result = tournament_team_service.delete_team(
        team_id, current_user_id=other_user_id
    )

    # Verify failure
    assert result.is_err()
    assert result.unwrap_err() == 'Only the team captain can delete this team.'

    # Verify NO deletion occurred
    mock_repository.delete_team.assert_not_called()


@patch('byceps.services.lan_tournament.tournament_match_service.tournament_repository')
def test_delete_match_cascades_comments_and_contestants(mock_repository):
    """Test that delete_match() deletes comments and contestants before match."""
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_match_service
    from byceps.services.lan_tournament.models.tournament_match import (
        TournamentMatch,
    )

    match_id = TournamentMatchID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())

    # Mock match lookup
    mock_match = TournamentMatch(
        id=match_id,
        tournament_id=tournament_id,
        group_order=None,
        match_order=None,
        confirmed_by=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.get_match.return_value = mock_match

    # Execute deletion
    tournament_match_service.delete_match(match_id)

    # Verify deletion calls in correct order (children first, then parent)
    expected_calls = [
        call.get_match(match_id),
        call.delete_comments_for_match(match_id),
        call.delete_contestants_for_match(match_id),
        call.delete_match(match_id),
    ]

    assert mock_repository.method_calls == expected_calls
    # Note: Event emission testing skipped because signals is imported locally


# Repository bulk deletion tests


def test_repository_bulk_deletion_methods_exist():
    """Verify all required bulk deletion methods exist in repository."""
    from byceps.services.lan_tournament import tournament_repository

    # Check tournament bulk deletions
    assert hasattr(tournament_repository, 'delete_teams_for_tournament')
    assert hasattr(tournament_repository, 'delete_participants_for_tournament')
    assert hasattr(tournament_repository, 'delete_matches_for_tournament')
    assert hasattr(tournament_repository, 'delete_contestants_for_tournament')
    assert hasattr(tournament_repository, 'delete_comments_for_tournament')

    # Check match bulk deletions
    assert hasattr(tournament_repository, 'delete_contestants_for_match')
    assert hasattr(tournament_repository, 'delete_comments_for_match')

    # Check NULL-setting methods
    assert hasattr(tournament_repository, 'remove_team_from_participants')
    assert hasattr(tournament_repository, 'remove_team_from_contestants')

    # Check individual contestant deletion
    assert hasattr(tournament_repository, 'delete_match_contestant')


def test_events_exist():
    """Verify match deletion events are defined."""
    from byceps.services.lan_tournament.events import (
        MatchCreatedEvent,
        MatchDeletedEvent,
    )

    # Just importing verifies they exist
    assert MatchCreatedEvent is not None
    assert MatchDeletedEvent is not None


def test_signals_exist():
    """Verify match deletion signals are defined."""
    from byceps.services.lan_tournament import signals

    assert hasattr(signals, 'match_created')
    assert hasattr(signals, 'match_deleted')
