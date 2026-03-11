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
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.user.models.user import UserID

from tests.helpers import generate_uuid


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
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
        call.delete_submissions_for_tournament(tournament_id),
        call.delete_comments_for_tournament(tournament_id),
        call.delete_contestants_for_tournament(tournament_id),
        call.delete_matches_for_tournament(tournament_id),
        call.clear_winner_for_tournament(tournament_id),
        call.delete_participants_for_tournament(tournament_id),
        call.delete_teams_for_tournament(tournament_id),
        call.delete_tournament(tournament_id),
    ]

    assert mock_repository.method_calls == expected_calls

    # Verify event emitted
    assert mock_signals.tournament_deleted.send.called


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_delete_tournament_with_winner_clears_winner_before_children(
    mock_signals, mock_repository
):
    """Test that clear_winner_for_tournament() is called before
    participant and team deletion to avoid FK violations."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())

    tournament_service.delete_tournament(tournament_id)

    # Extract only the method names to verify ordering
    method_names = [c[0] for c in mock_repository.method_calls]

    clear_idx = method_names.index('clear_winner_for_tournament')
    participants_idx = method_names.index('delete_participants_for_tournament')
    teams_idx = method_names.index('delete_teams_for_tournament')

    assert clear_idx < participants_idx, (
        'clear_winner must precede participant deletion'
    )
    assert clear_idx < teams_idx, (
        'clear_winner must precede team deletion'
    )


@patch(
    'byceps.services.lan_tournament.tournament_team_service.tournament_repository'
)
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
        call.clear_winner_team_reference(team_id),
        call.delete_team(team_id),
    ]

    assert mock_repository.method_calls == expected_calls

    # Verify event emitted
    assert mock_signals.team_deleted.send.called



@patch(
    'byceps.services.lan_tournament.tournament_team_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_team_service.signals')
def test_delete_team_clears_winner_reference_before_deletion(
    mock_signals, mock_repository
):
    """Test that clear_winner_team_reference() is called before
    delete_team() to avoid FK violations when the team is the winner."""
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_team_service

    team_id = TournamentTeamID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    captain_id = UserID(generate_uuid())

    mock_team = TournamentTeam(
        id=team_id,
        tournament_id=tournament_id,
        name='Winner Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=captain_id,
        join_code=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.find_team.return_value = mock_team

    tournament_team_service.delete_team(team_id)

    method_names = [c[0] for c in mock_repository.method_calls]

    clear_idx = method_names.index('clear_winner_team_reference')
    delete_idx = method_names.index('delete_team')

    assert clear_idx < delete_idx, (
        'clear_winner_team_reference must precede delete_team'
    )


@patch(
    'byceps.services.lan_tournament.tournament_team_service.tournament_repository'
)
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


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
def test_admin_remove_participant_clears_winner_before_hard_delete(
    mock_signals, mock_repository
):
    """Test that clear_winner_participant_reference_flush() is called before
    hard-deleting a participant to avoid FK violations on winner_participant_id."""
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_participant_service
    from byceps.services.lan_tournament.models.contestant_type import (
        ContestantType,
    )
    from byceps.services.lan_tournament.models.tournament import Tournament
    from byceps.services.lan_tournament.models.tournament_participant import (
        TournamentParticipant,
    )
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )
    from byceps.services.party.models import PartyID

    tournament_id = TournamentID(generate_uuid())
    participant_id = TournamentParticipantID(generate_uuid())
    user_id = UserID(generate_uuid())
    party_id = PartyID('test-party')

    mock_participant = TournamentParticipant(
        id=participant_id,
        user_id=user_id,
        tournament_id=tournament_id,
        substitute_player=False,
        team_id=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.find_participant.return_value = mock_participant

    # COMPLETED status → bracket_is_active=False → hard-delete path
    mock_tournament = Tournament(
        id=tournament_id,
        party_id=party_id,
        name='Completed Tournament',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.COMPLETED,
        tournament_mode=None,
    )
    mock_repository.get_tournament_for_update.return_value = mock_tournament

    tournament_participant_service.admin_remove_participant(
        tournament_id, participant_id
    )

    method_names = [c[0] for c in mock_repository.method_calls]

    clear_idx = method_names.index('clear_winner_participant_reference_flush')
    delete_idx = method_names.index('delete_participants_by_ids')

    assert clear_idx < delete_idx, (
        'clear_winner_participant_reference_flush must precede '
        'delete_participants_by_ids'
    )


@patch(
    'byceps.services.lan_tournament.tournament_team_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_team_service.signals')
def test_leave_team_auto_delete_clears_winner_reference(
    mock_signals, mock_repository
):
    """Test that leave_team() clears winner_team_id before auto-deleting
    an empty team to avoid FK violations."""
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_team_service
    from byceps.services.lan_tournament.models.tournament import Tournament
    from byceps.services.lan_tournament.models.tournament_participant import (
        TournamentParticipant,
    )
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )
    from byceps.services.party.models import PartyID

    team_id = TournamentTeamID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    captain_id = UserID(generate_uuid())
    participant_id = TournamentParticipantID(generate_uuid())
    party_id = PartyID('test-party')

    mock_participant = TournamentParticipant(
        id=participant_id,
        user_id=captain_id,
        tournament_id=tournament_id,
        substitute_player=False,
        team_id=team_id,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.find_participant.return_value = mock_participant

    mock_team = TournamentTeam(
        id=team_id,
        tournament_id=tournament_id,
        name='Solo Captain Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=captain_id,
        join_code=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
    )
    mock_repository.get_team.return_value = mock_team

    mock_tournament = Tournament(
        id=tournament_id,
        party_id=party_id,
        name='Reg Open Tournament',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=None,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        tournament_mode=None,
    )
    mock_repository.get_tournament.return_value = mock_tournament

    # First call (captain check): 1 member → captain can leave
    # Second call (auto-delete check): 0 remaining → trigger delete
    mock_repository.get_participants_for_team.side_effect = [
        [mock_participant],
        [],
    ]

    tournament_team_service.leave_team(participant_id)

    method_names = [c[0] for c in mock_repository.method_calls]

    clear_idx = method_names.index('clear_winner_team_reference')
    delete_idx = method_names.index('delete_team')

    assert clear_idx < delete_idx, (
        'clear_winner_team_reference must precede delete_team in leave_team'
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
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
        round=None,
        next_match_id=None,
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
    assert hasattr(tournament_repository, 'delete_submissions_for_tournament')
    assert hasattr(tournament_repository, 'delete_teams_for_tournament')
    assert hasattr(tournament_repository, 'delete_participants_for_tournament')
    assert hasattr(tournament_repository, 'delete_matches_for_tournament')
    assert hasattr(tournament_repository, 'delete_contestants_for_tournament')
    assert hasattr(tournament_repository, 'delete_comments_for_tournament')

    # Check winner-clearing methods
    assert hasattr(tournament_repository, 'clear_winner_for_tournament')
    assert hasattr(tournament_repository, 'clear_winner_team_reference')
    assert hasattr(tournament_repository, 'clear_winner_participant_reference_flush')

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
