"""
tests.unit.services.lan_tournament.test_tournament_deletion
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for CASCADE deletion behavior in tournament service layer.
"""

from dataclasses import replace
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest


from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.tournament import Tournament, TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Ok

from tests.helpers import generate_uuid


def _tournament_with_winning_team(team):
    return Tournament(
        id=team.tournament_id, party_id=PartyID('test-party'),
        name='Team winner cleanup', game=None, description=None,
        image_url=None, ruleset=None, start_time=None, created_at=datetime(2025, 6, 15),
        min_players=None, max_players=None, min_teams=None, max_teams=None,
        min_players_in_team=None, max_players_in_team=None,
        contestant_type=ContestantType.TEAM,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        game_format=None, elimination_mode=None, winner_team_id=team.id,
    )


@pytest.fixture(autouse=True)
def invitation_dispatch():
    pending_ids: tuple[MatchInvitationID, ...] = ()
    with patch(
        'byceps.services.lan_tournament.tournament_readiness_service'
        '.dispatch_pending_invitations', return_value=Ok(None),
    ) as dispatch:
        yield dispatch, pending_ids


@pytest.fixture
def roster_lock_reads():
    """Explicit empty assignments for the imported roster locking collaborator."""
    prefix = 'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
    with (
        patch(f'{prefix}.get_team_for_update') as team,
        patch(f'{prefix}.get_participants_for_team', return_value=[]) as members,
        patch(f'{prefix}.get_contestants_for_tournament', return_value={}) as assignments,
        patch(f'{prefix}.get_matches_for_tournament', return_value=[]) as matches,
        patch(f'{prefix}.get_participants_for_update') as participant,
        patch(f'{prefix}.lock_matches_for_update') as lock,
    ):
        yield team, members, assignments, matches, participant, lock


@pytest.fixture(autouse=True)
def mock_participant_audit_log():
    with patch(
        'byceps.services.lan_tournament.tournament_participant_service.tournament_log_service'
    ) as mock_log:
        yield mock_log.create_log_entry


@pytest.fixture(autouse=True)
def mock_seeding_repository():
    with patch(
        'byceps.services.lan_tournament.tournament_service.tournament_seeding_repository'
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_qualification_repository():
    with patch(
        'byceps.services.lan_tournament.tournament_service.tournament_qualification_repository'
    ) as mock:
        yield mock


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_request_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_cascades_all_dependencies(
    mock_create_log_entry,
    mock_signals,
    mock_repository,
    mock_orga_repository,
    mock_request_repository,
    mock_seeding_repository,
    mock_qualification_repository,
):
    """Test that delete_tournament() deletes all dependent entities in correct order."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())
    mock_request_repository.unlink_created_tournament_flush.return_value = []

    order = []

    def record_audit(*args, **kwargs):
        assert mock_repository.method_calls == [
            call.lock_tournament_for_update(tournament_id),
            call.get_tournament(tournament_id, fresh=True),
        ]
        assert kwargs['commit'] is False
        order.append('audit')

    mock_create_log_entry.side_effect = record_audit
    mock_repository.commit_session.side_effect = lambda: order.append('commit')
    mock_signals.tournament_deleted.send.side_effect = (
        lambda *args, **kwargs: order.append('signal')
    )

    # Execute deletion
    tournament_service.delete_tournament(tournament_id)

    # Verify deletion calls in correct order (children first, then
    # parent). Log entries are not deleted.
    expected_calls = [
        call.lock_tournament_for_update(tournament_id),
        call.get_tournament(tournament_id, fresh=True),
        call.delete_submissions_for_tournament(tournament_id, commit=False),
        call.delete_comments_for_tournament(tournament_id, commit=False),
        call.delete_contestants_for_tournament(tournament_id, commit=False),
        call.delete_matches_for_tournament(tournament_id, commit=False),
        call.clear_winner_for_tournament(tournament_id, commit=False),
        call.delete_participants_for_tournament(tournament_id, commit=False),
        call.delete_teams_for_tournament(tournament_id, commit=False),
        call.delete_tournament(tournament_id, commit=False),
        call.commit_session(),
    ]

    assert mock_repository.method_calls == expected_calls

    assert mock_orga_repository.method_calls == [
        call.delete_orgas_for_tournament(tournament_id, commit=False)
    ]

    assert mock_seeding_repository.method_calls == [
        call.delete_seedings_for_tournament(tournament_id)
    ]

    assert mock_qualification_repository.method_calls == [
        call.delete_decisions_for_tournament(tournament_id)
    ]

    # workspace-dim0.17: the request link is cleared as part of the
    # same cascade, before the tournament row itself is deleted.
    mock_request_repository.unlink_created_tournament_flush.assert_called_once_with(
        tournament_id
    )

    # A tournament-deleted entry is written.
    assert mock_create_log_entry.call_args.args[0] == 'tournament-deleted'

    # Verify event emitted
    assert mock_signals.tournament_deleted.send.called
    mock_signals.tournament_deleted.send.assert_called_once()
    assert order == ['audit', 'commit', 'signal']
    mock_repository.rollback_session.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_request_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_removes_orgas_before_the_tournament_row(
    mock_create_log_entry,
    mock_signals,
    mock_repository,
    mock_orga_repository,
    mock_request_repository,
    mock_seeding_repository,
):
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())
    mock_request_repository.unlink_created_tournament_flush.return_value = []

    # Attach the mocks to one parent to record their relative order.
    parent = MagicMock()
    parent.attach_mock(mock_repository, 'repo')
    parent.attach_mock(mock_orga_repository, 'orga_repo')
    parent.attach_mock(mock_seeding_repository, 'seeding_repo')

    tournament_service.delete_tournament(tournament_id)

    call_names = [c[0] for c in parent.mock_calls]
    orga_idx = call_names.index('orga_repo.delete_orgas_for_tournament')
    tournament_idx = call_names.index('repo.delete_tournament')

    assert orga_idx < tournament_idx, (
        'orga assignments must be deleted before the tournament row'
    )
    seeding_idx = call_names.index('seeding_repo.delete_seedings_for_tournament')
    assert seeding_idx < tournament_idx, (
        'seeding drafts must be deleted before the tournament row'
    )

    _, kwargs = mock_orga_repository.delete_orgas_for_tournament.call_args
    assert kwargs == {'commit': False}


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_request_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_does_not_delete_log_entries(
    mock_create_log_entry,
    mock_signals,
    mock_repository,
    mock_orga_repository,
    mock_request_repository,
):
    """Deleting a tournament leaves its log entries in place."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())
    mock_request_repository.unlink_created_tournament_flush.return_value = []

    tournament_service.delete_tournament(tournament_id)

    method_names = [c[0] for c in mock_repository.method_calls]

    assert 'delete_log_entries_for_tournament' not in method_names
    mock_repository.delete_log_entries_for_tournament.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_request_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_writes_tournament_deleted_entry(
    mock_create_log_entry,
    mock_signals,
    mock_repository,
    mock_orga_repository,
    mock_request_repository,
):
    """Write a `tournament-deleted` entry with the tournament's context."""
    from byceps.services.lan_tournament.models.contestant_type import (
        ContestantType,
    )
    from byceps.services.lan_tournament.models.tournament import Tournament
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )
    from byceps.services.party.models import PartyID
    from byceps.services.lan_tournament import tournament_service
    from datetime import datetime

    tournament_id = TournamentID(generate_uuid())
    party_id = PartyID('doomed-party')
    initiator_id = UserID(generate_uuid())

    mock_tournament = Tournament(
        id=tournament_id,
        party_id=party_id,
        name='Doomed Tournament',
        game='Quake',
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
        game_format=None,
        elimination_mode=None,
    )
    mock_repository.get_tournament.return_value = mock_tournament
    mock_request_repository.unlink_created_tournament_flush.return_value = []

    tournament_service.delete_tournament(
        tournament_id, initiator_id=initiator_id
    )

    mock_create_log_entry.assert_called_once_with(
        'tournament-deleted',
        tournament_id,
        initiator_id,
        data={
            'name': 'Doomed Tournament',
            'party_id': str(party_id),
            'game': 'Quake',
            'tournament_status': TournamentStatus.COMPLETED.name,
        },
        commit=False,
    )
    mock_repository.get_tournament.assert_called_once_with(
        tournament_id, fresh=True
    )


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_request_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_writes_log_entry_before_cascade_deletes(
    mock_create_log_entry,
    mock_signals,
    mock_repository,
    mock_orga_repository,
    mock_request_repository,
):
    """Stage the `tournament-deleted` entry before the cascade runs."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())
    mock_request_repository.unlink_created_tournament_flush.return_value = []

    # One parent mock records both mocks' calls in one sequence.
    parent = MagicMock()
    parent.attach_mock(mock_repository, 'repo')
    parent.attach_mock(mock_create_log_entry, 'log')

    tournament_service.delete_tournament(tournament_id)

    call_names = [c[0] for c in parent.mock_calls]
    log_idx = call_names.index('log')
    first_delete_idx = call_names.index(
        'repo.delete_submissions_for_tournament'
    )

    assert log_idx < first_delete_idx, (
        'tournament-deleted entry must be staged before the cascade'
    )


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_request_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_with_winner_clears_winner_before_children(
    mock_create_log_entry,
    mock_signals,
    mock_repository,
    mock_orga_repository,
    mock_request_repository,
):
    """Test that clear_winner_for_tournament() is called before
    participant and team deletion to avoid FK violations."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())
    mock_request_repository.unlink_created_tournament_flush.return_value = []

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
    'byceps.services.lan_tournament.tournament_service.tournament_orga_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_delete_tournament_rolls_back_on_failure(
    mock_create_log_entry, mock_signals, mock_repository, mock_orga_repository
):
    """DB error mid-cascade -> rollback called, event NOT emitted."""
    from byceps.services.lan_tournament import tournament_service

    tournament_id = TournamentID(generate_uuid())

    mock_repository.delete_matches_for_tournament.side_effect = Exception(
        'simulated'
    )

    with pytest.raises(Exception, match='simulated'):
        tournament_service.delete_tournament(tournament_id)

    mock_repository.rollback_session.assert_called_once()
    mock_repository.commit_session.assert_not_called()
    mock_signals.tournament_deleted.send.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_team_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_team_service.signals')
def test_delete_team_removes_references_before_deletion(
    mock_signals, mock_repository, roster_lock_reads, invitation_dispatch
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
    mock_repository.get_team_for_update.return_value = mock_team
    mock_repository.get_tournament.return_value = _tournament_with_winning_team(mock_team)
    mock_repository.get_tournament_for_update.return_value = mock_repository.get_tournament.return_value
    roster_lock_reads[0].return_value = mock_team

    order = []
    dispatch = invitation_dispatch[0]
    mock_repository.commit_session.side_effect = lambda: order.append('commit')
    mock_signals.team_deleted.send.side_effect = lambda *a, **kw: order.append('signal')
    dispatch.side_effect = lambda ids: order.append('dispatch') or Ok(None)

    # Execute deletion (admin bypass - no current_user_id check)
    result = tournament_team_service.delete_team(team_id)

    # Verify success
    assert result.is_ok()

    # Verify deletion calls in correct order
    expected_calls = [
        call.find_team(team_id),
        call.get_tournament_for_update(tournament_id),
        call.get_team_for_update(team_id),
        # The owner reads the tournament once for its operation time.
        call.get_tournament(tournament_id, fresh=True),
        call.remove_team_from_participants_flush(team_id),
        call.remove_team_from_contestants_flush(team_id),
        call.get_tournament(tournament_id, fresh=True),
        call.clear_winner_for_tournament(tournament_id, commit=False),
        call.delete_team_flush(team_id),
        call.commit_session(),
    ]

    assert mock_repository.method_calls == expected_calls

    # Verify event emitted
    assert mock_signals.team_deleted.send.called
    roster_lock_reads[0].assert_called_once_with(team_id)
    roster_lock_reads[2].assert_called_once_with(tournament_id)
    roster_lock_reads[5].assert_called_once_with([])
    invitation_dispatch[0].assert_called_once_with(())
    assert order == ['commit', 'signal', 'dispatch']
    mock_repository.rollback_session.assert_not_called()



@patch(
    'byceps.services.lan_tournament.tournament_team_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_team_service.signals')
def test_delete_team_clears_winner_reference_before_deletion(
    mock_signals, mock_repository, roster_lock_reads
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
    mock_repository.get_team_for_update.return_value = mock_team
    mock_repository.get_tournament.return_value = _tournament_with_winning_team(mock_team)
    mock_repository.get_tournament_for_update.return_value = mock_repository.get_tournament.return_value
    roster_lock_reads[0].return_value = mock_team

    tournament_team_service.delete_team(team_id)

    method_names = [c[0] for c in mock_repository.method_calls]

    clear_idx = method_names.index('clear_winner_for_tournament')
    delete_idx = method_names.index('delete_team_flush')

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
    mock_repository.get_team_for_update.return_value = mock_team

    # Execute deletion as non-captain
    result = tournament_team_service.delete_team(
        team_id, current_user_id=other_user_id
    )

    # Verify failure
    assert result.is_err()
    assert result.unwrap_err() == 'Only the team captain can delete this team.'

    # Verify NO deletion occurred
    mock_repository.delete_team.assert_not_called()
    mock_repository.delete_team_flush.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
def test_admin_remove_participant_clears_winner_before_hard_delete(
    mock_signals, mock_repository, mock_participant_audit_log, invitation_dispatch
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
    mock_repository.find_participant_fresh.return_value = mock_participant
    mock_repository.get_participant_for_update.return_value = mock_participant
    mock_repository.get_contestants_for_tournament.return_value = {}
    mock_repository.get_matches_for_tournament.return_value = []
    mock_repository.find_contestant_entries_for_participant_in_tournament.return_value = []

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
        game_format=None,
        elimination_mode=None,
    )
    mock_repository.get_tournament_for_update.return_value = mock_tournament

    order = []
    dispatch = invitation_dispatch[0]

    def cleanup(participant_id):
        mock_repository.commit_session.assert_not_called()
        dispatch.assert_not_called()
        order.append('clear')

    mock_repository.clear_winner_participant_reference_flush.side_effect = cleanup
    mock_repository.delete_participants_by_ids.side_effect = lambda ids: order.append('delete')
    mock_repository.commit_session.side_effect = lambda: order.append('commit')
    mock_signals.participant_left.send.side_effect = lambda *a, **kw: order.append('signal')
    dispatch.side_effect = lambda ids: order.append('dispatch') or Ok(None)

    result = tournament_participant_service.admin_remove_participant(
        tournament_id, participant_id
    )
    assert result.is_ok()
    mock_repository.find_participant_fresh.assert_called_once_with(participant_id)
    mock_repository.clear_winner_participant_reference_flush.assert_called_once_with(participant_id)
    mock_repository.delete_participants_by_ids.assert_called_once_with({participant_id})
    mock_repository.commit_session.assert_called_once_with()
    mock_repository.rollback_session.assert_not_called()
    dispatch.assert_called_once_with(())
    assert order == ['clear', 'delete', 'commit', 'signal', 'dispatch']

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
    mock_signals, mock_repository, roster_lock_reads, invitation_dispatch
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
    mock_repository.find_participant_fresh.return_value = mock_participant

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
    mock_repository.get_team_for_update.return_value = mock_team
    roster_lock_reads[0].return_value = mock_team
    roster_lock_reads[1].return_value = [mock_participant]
    roster_lock_reads[4].return_value = mock_participant

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
        game_format=None,
        elimination_mode=None,
    )
    mock_repository.get_tournament.return_value = mock_tournament
    mock_tournament = replace(mock_tournament, winner_team_id=team_id)
    mock_repository.get_tournament.return_value = mock_tournament

    # First call (captain check): 1 member → captain can leave
    # Second call (auto-delete check): 0 remaining → trigger delete
    mock_repository.get_participants_for_team.side_effect = [
        [mock_participant],
        [],
    ]

    tournament_team_service.leave_team(participant_id)

    method_names = [c[0] for c in mock_repository.method_calls]

    clear_idx = method_names.index('clear_winner_for_tournament')
    delete_idx = method_names.index('delete_team_flush')

    assert clear_idx < delete_idx, (
        'clear_winner_team_reference must precede delete_team in leave_team'
    )
    mock_repository.clear_winner_for_tournament.assert_called_once_with(
        tournament_id, commit=False,
    )
    mock_repository.update_participant_flush.assert_called_once_with(
        replace(mock_participant, team_id=None),
    )
    mock_repository.commit_session.assert_called_once_with()
    roster_lock_reads[4].assert_called_once_with([participant_id])
    invitation_dispatch[0].assert_called_once_with(())


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
    mock_repository.get_match_for_update.return_value = mock_match

    # Execute deletion
    order = []
    mock_repository.commit_session.side_effect = lambda: order.append('commit')
    with patch('byceps.services.lan_tournament.signals.match_deleted') as signal:
        signal.send.side_effect = lambda *a, **kw: order.append('signal')
        tournament_match_service.delete_match(match_id)
    assert order == ['commit', 'signal']
    assert signal.send.call_args.kwargs['event'].match_id == match_id

    # Verify deletion calls in correct order (children first, then parent)
    expected_calls = [
        call.get_match(match_id),
        call.lock_tournament_for_update(tournament_id),
        call.get_match_for_update(match_id),
        call.delete_comments_for_match_flush(match_id),
        call.get_match(match_id),
        call.delete_contestants_for_match_flush(match_id),
        call.get_match(match_id),
        call.delete_match_flush(match_id),
        call.commit_session(),
    ]

    assert mock_repository.method_calls == expected_calls
    mock_repository.rollback_session.assert_not_called()


# fmt: off
@pytest.mark.parametrize('failure', [
    'delete_comments_for_match_flush', 'delete_contestants_for_match_flush',
    'delete_match_flush', 'commit_session',
])
# fmt: on
def test_delete_match_failure_rolls_back_without_signal(failure):
    from datetime import datetime

    from byceps.services.lan_tournament import tournament_match_service
    from byceps.services.lan_tournament.models.tournament_match import TournamentMatch

    match_id = TournamentMatchID(generate_uuid())
    match = TournamentMatch(
        id=match_id, tournament_id=TournamentID(generate_uuid()),
        group_order=None, match_order=0, round=None, next_match_id=None,
        confirmed_by=None, created_at=datetime(2025, 6, 15),
    )
    with (
        patch('byceps.services.lan_tournament.tournament_match_service'
              '.tournament_repository') as repo,
        patch('byceps.services.lan_tournament.signals.match_deleted') as signal,
    ):
        repo.get_match.return_value = match
        repo.get_match_for_update.return_value = match
        getattr(repo, failure).side_effect = RuntimeError('delete unavailable')
        with pytest.raises(RuntimeError, match='delete unavailable'):
            tournament_match_service.delete_match(match_id)
    repo.rollback_session.assert_called_once_with()
    signal.send.assert_not_called()
    if failure != 'commit_session':
        repo.commit_session.assert_not_called()
    else:
        repo.commit_session.assert_called_once_with()


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


# Central retirement of the dashboard state


DELETION_TIME = datetime(2031, 5, 6, 18, 30, 0)
GIVEN_TIME = datetime(2031, 5, 6, 19, 45, 0)


def _statements(session):
    """Return (SQL, parameters) of every statement executed, in order."""
    from sqlalchemy.dialects import postgresql

    found = []
    for executed in session.execute.call_args_list:
        compiled = executed.args[0].compile(dialect=postgresql.dialect())
        found.append((str(compiled), compiled.params))
    return found


@pytest.fixture
def deletion_session():
    from byceps.services.lan_tournament import tournament_repository as repo

    session = MagicMock()
    with (
        patch.object(repo.db, 'session', session),
        patch.object(repo, '_retire_match_pairing_flush') as pairing,
        patch.object(repo, 'lock_tournament_for_update'),
        patch.object(repo, 'lock_matches_for_update'),
        patch.object(repo, 'null_self_referential_fks'),
        patch.object(
            repo, 'get_operation_time', return_value=DELETION_TIME
        ) as clock,
    ):
        session.pairing = pairing
        session.clock = clock
        yield session


def _delete_one(match_ids, **kwargs):
    from byceps.services.lan_tournament import tournament_repository as repo

    repo.delete_match_flush(match_ids[0], **kwargs)


def _delete_all(match_ids, **kwargs):
    from byceps.services.lan_tournament import tournament_repository as repo

    matches = [MagicMock(id=match_id) for match_id in match_ids]
    with patch.object(
        repo, 'get_matches_for_tournament_ordered_fresh', return_value=matches
    ):
        repo.delete_matches_for_tournament(
            TournamentID(generate_uuid()), commit=False
        )


# fmt: off
@pytest.mark.parametrize('delete, count', [(_delete_one, 1), (_delete_all, 3)])
# fmt: on
def test_match_deletion_retires_the_dashboard_state_before_the_row_goes(
    deletion_session, delete, count
):
    match_ids = [TournamentMatchID(generate_uuid()) for _ in range(count)]

    delete(match_ids)

    statements = _statements(deletion_session)
    episodes = [
        i for i, (sql, _) in enumerate(statements)
        if sql.startswith('UPDATE lan_tournament_match_due_episodes')
    ]
    pins = [
        i for i, (sql, _) in enumerate(statements)
        if sql.startswith('DELETE FROM lan_tournament_match_dashboard_annotations')
    ]
    rows = [
        i for i, (sql, _) in enumerate(statements)
        if sql.startswith('DELETE FROM lan_tournament_matches')
    ]
    # One close and one drop for all matches, both before the one delete.
    assert len(episodes) == len(pins) == len(rows) == 1
    assert episodes[0] < pins[0] < rows[0]
    # The history is closed, never deleted.
    assert not [
        sql for sql, _ in statements
        if sql.startswith('DELETE')
        and ('due_episodes' in sql or 'escalation_acks' in sql)
    ]
    closed = statements[episodes[0]][1]
    assert closed['closed_at'] == DELETION_TIME
    assert deletion_session.commit.call_count == 0


# fmt: off
@pytest.mark.parametrize('delete', [_delete_one, _delete_all])
# fmt: on
def test_a_deletion_samples_the_server_clock_once_after_the_locks(
    deletion_session, delete
):
    match_ids = [TournamentMatchID(generate_uuid()) for _ in range(3)]

    delete(match_ids)

    deletion_session.clock.assert_called_once_with()
    # The pairing backstop took the locks before the time was read.
    assert deletion_session.pairing.call_count >= 1


def test_an_owner_time_closes_the_episodes_without_a_second_reading(
    deletion_session,
):
    match_ids = [TournamentMatchID(generate_uuid())]

    _delete_one(match_ids, changed_at=GIVEN_TIME)

    deletion_session.clock.assert_not_called()
    (closed,) = [
        params for sql, params in _statements(deletion_session)
        if sql.startswith('UPDATE lan_tournament_match_due_episodes')
    ]
    assert closed['closed_at'] == GIVEN_TIME


def test_a_tournament_without_matches_retires_and_samples_nothing(
    deletion_session,
):
    _delete_all([])

    deletion_session.clock.assert_not_called()
    assert not [
        sql for sql, _ in _statements(deletion_session)
        if 'due_episodes' in sql or 'dashboard_annotations' in sql
    ]


def test_the_pairing_backstop_runs_before_the_dashboard_retirement(
    deletion_session,
):
    order = []
    deletion_session.pairing.side_effect = lambda _: order.append('pairing')
    deletion_session.execute.side_effect = lambda _: order.append('statement')

    _delete_one([TournamentMatchID(generate_uuid())])

    assert order[0] == 'pairing'
    assert 'statement' in order


def _clearing_world():
    from byceps.services.lan_tournament import tournament_match_service as engine

    matches = [
        MagicMock(id=TournamentMatchID(generate_uuid()), phase=1, confirmed_by=None)
        for _ in range(2)
    ]
    repository = MagicMock()
    repository.get_matches_for_tournament_ordered_fresh.return_value = matches
    repository.get_contestants_for_matches.return_value = {}
    return engine, repository, matches


# fmt: off
@pytest.mark.parametrize('given', [None, GIVEN_TIME])
# fmt: on
def test_clearing_the_bracket_hands_its_operation_time_to_every_deletion(given):
    engine, repository, matches = _clearing_world()
    tournament_id = TournamentID(generate_uuid())
    keyword = {} if given is None else {'changed_at': given}

    with (
        patch.object(engine, 'tournament_repository', repository),
        patch.object(engine, 'create_log_entry'),
        patch.object(engine, '_delete_contestants_for_match_flush') as cleanup,
    ):
        events = engine.clear_bracket(tournament_id, **keyword)

    assert len(events) == 2
    assert repository.delete_match_flush.call_args_list == [
        call(match.id, **keyword) for match in matches
    ]
    assert cleanup.call_args_list == [
        call(match.id, **keyword) for match in matches
    ]


# fmt: off
@pytest.mark.parametrize('given', [None, GIVEN_TIME])
# fmt: on
def test_the_lobby_deletion_hands_its_operation_time_to_every_deletion(given):
    engine, repository, matches = _clearing_world()
    tournament_id = TournamentID(generate_uuid())
    keyword = {} if given is None else {'changed_at': given}

    with (
        patch.object(engine, 'tournament_repository', repository),
        patch.object(engine, '_delete_contestants_for_match_flush') as cleanup,
    ):
        events = engine._delete_matches_flush(tournament_id, matches, **keyword)

    assert len(events) == 2
    assert repository.delete_match_flush.call_args_list == [
        call(match.id, **keyword) for match in matches
    ]
    assert cleanup.call_args_list == [
        call(match.id, **keyword) for match in matches
    ]


# fmt: off
@pytest.mark.parametrize('given', [None, GIVEN_TIME])
# fmt: on
def test_the_first_round_regeneration_hands_its_operation_time_to_the_clearing(
    given,
):
    from byceps.services.lan_tournament import tournament_match_service as engine
    from byceps.util.result import Err

    tournament_id = TournamentID(generate_uuid())
    repository = MagicMock()
    repository.get_matches_for_tournament.return_value = [MagicMock()]
    keyword = {} if given is None else {'changed_at': given}

    with (
        patch.object(engine, 'tournament_repository', repository),
        patch.object(engine, '_ffa_elimination_mode'),
        patch.object(engine, '_ffa_phase', return_value=1),
        patch.object(engine, 'clear_bracket', return_value=[]) as clear,
        patch.object(
            engine, '_generate_ffa_round_impl', return_value=Err('stop')
        ),
    ):
        result = engine._generate_ffa_initial_impl(
            tournament_id, True, **keyword
        )

    assert result == Err('stop')
    clear.assert_called_once_with(tournament_id, initiator_id=None, **keyword)


# fmt: off
@pytest.mark.parametrize('given', [None, GIVEN_TIME])
# fmt: on
def test_the_preamble_hands_its_operation_time_to_the_clearing(given):
    from types import SimpleNamespace

    from byceps.services.lan_tournament import tournament_match_service as engine

    tournament_id = TournamentID(generate_uuid())
    team = SimpleNamespace(id=generate_uuid(), tournament_id=tournament_id)
    repository = MagicMock()
    repository.get_matches_for_tournament.return_value = [MagicMock(phase=2)]
    repository.get_tournament.return_value = _tournament_with_winning_team(team)
    keyword = {} if given is None else {'changed_at': given}

    with (
        patch.object(engine, 'tournament_repository', repository),
        patch.object(engine, 'clear_bracket', return_value=[]) as clear,
    ):
        result = engine._prepare_bracket_generation(
            tournament_id, True, phase=2, roster=['a', 'b'], **keyword
        )

    assert result.is_ok(), result
    clear.assert_called_once_with(
        tournament_id, phase=2, initiator_id=None, **keyword
    )


# fmt: off
@pytest.mark.parametrize('given', [None, GIVEN_TIME])
@pytest.mark.parametrize('name', [
    '_generate_single_elimination_impl', '_generate_double_elimination_impl',
    '_generate_round_robin_impl',
])
# fmt: on
def test_the_bracket_generators_hand_their_operation_time_to_the_preamble(
    name, given
):
    from byceps.services.lan_tournament import tournament_match_service as engine
    from byceps.util.result import Err

    keyword = {} if given is None else {'changed_at': given}

    with patch.object(
        engine, '_prepare_bracket_generation', return_value=Err('stop')
    ) as prepare:
        result = getattr(engine, name)(
            TournamentID(generate_uuid()), True, **keyword
        )

    assert result == Err('stop')
    assert prepare.call_args.kwargs.get('changed_at') == given
    assert ('changed_at' in prepare.call_args.kwargs) == (given is not None)
