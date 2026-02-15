from datetime import UTC, datetime
from unittest.mock import patch

from byceps.services.lan_tournament import (
    tournament_participant_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models.user import UserID
from byceps.util.result import Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')


# -------------------------------------------------------------------- #
# join_tournament — soft-delete re-join
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_rejoin_reactivates_soft_deleted_participant(
    mock_domain, mock_repo, mock_ticket, mock_signals
):
    """A soft-deleted participant is reactivated instead of
    creating a new row."""
    user_id = UserID(generate_uuid())
    old_participant_id = TournamentParticipantID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    soft_deleted = _create_participant(
        id=old_participant_id,
        user_id=user_id,
        removed_at=NOW,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = True
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = (
        soft_deleted
    )

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    participant, event = result.unwrap()
    assert participant.id == old_participant_id
    mock_repo.reactivate_participant.assert_called_once()
    mock_repo.create_participant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_rejoin_updates_fields_on_reactivation(
    mock_domain, mock_repo, mock_ticket, mock_signals
):
    """Reactivated participant gets new team_id,
    substitute_player, created_at."""
    user_id = UserID(generate_uuid())
    team_id = TournamentTeamID(generate_uuid())
    old_participant_id = TournamentParticipantID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    soft_deleted = _create_participant(
        id=old_participant_id,
        user_id=user_id,
        removed_at=NOW,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = True
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = (
        soft_deleted
    )

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID,
        user_id,
        substitute_player=True,
        team_id=team_id,
    )

    assert result.is_ok()
    call_kwargs = (
        mock_repo.reactivate_participant.call_args
    )
    assert call_kwargs[1]['substitute_player'] is True
    assert call_kwargs[1]['team_id'] == team_id


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_rejoin_fires_participant_joined_event(
    mock_domain, mock_repo, mock_ticket, mock_signals
):
    """ParticipantJoinedEvent fires on reactivation."""
    user_id = UserID(generate_uuid())
    old_participant_id = TournamentParticipantID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    soft_deleted = _create_participant(
        id=old_participant_id,
        user_id=user_id,
        removed_at=NOW,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = True
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = (
        soft_deleted
    )

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    mock_signals.participant_joined.send.assert_called_once()
    event = mock_signals.participant_joined.send.call_args[1]['event']
    assert event.participant_id == old_participant_id


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_rejoin_blocked_when_tournament_full(
    mock_domain, mock_repo, mock_ticket
):
    """Reactivation is blocked when tournament is at capacity."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        max_players=2,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = True
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 2
    mock_domain.validate_participant_count.return_value = (
        Ok(None).__class__.__mro__[0].__call__  # trick
    )
    # Use real Err to block
    from byceps.util.result import Err

    mock_domain.validate_participant_count.return_value = Err(
        'Tournament is full.'
    )

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID, user_id
    )

    assert result.is_err()
    assert 'full' in result.unwrap_err().lower()
    mock_repo.reactivate_participant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_join_creates_new_when_no_soft_deleted_row(
    mock_domain, mock_repo, mock_ticket, mock_signals
):
    """Normal INSERT path when no soft-deleted row exists."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = True
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = None

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    mock_repo.create_participant.assert_called_once()
    mock_repo.reactivate_participant.assert_not_called()


# -------------------------------------------------------------------- #
# admin_add_participant — status checks
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_admin_add_works_with_registration_closed(
    mock_domain, mock_repo, mock_signals
):
    """admin_add_participant works with REGISTRATION_CLOSED status."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = None

    result = tournament_participant_service.admin_add_participant(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    mock_repo.create_participant.assert_called_once()


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_admin_add_fails_with_ongoing_status(mock_repo):
    """admin_add_participant fails with ONGOING status."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.ONGOING,
    )

    mock_repo.get_tournament_for_update.return_value = tournament

    result = tournament_participant_service.admin_add_participant(
        TOURNAMENT_ID, user_id
    )

    assert result.is_err()
    assert 'registration' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# admin_add_participant — soft-delete re-join
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_admin_add_reactivates_soft_deleted_participant(
    mock_domain, mock_repo, mock_signals
):
    """admin_add_participant reactivates a soft-deleted participant."""
    user_id = UserID(generate_uuid())
    old_participant_id = TournamentParticipantID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    soft_deleted = _create_participant(
        id=old_participant_id,
        user_id=user_id,
        removed_at=NOW,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = (
        soft_deleted
    )

    result = tournament_participant_service.admin_add_participant(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    participant, _event = result.unwrap()
    assert participant.id == old_participant_id
    mock_repo.reactivate_participant.assert_called_once()
    mock_repo.create_participant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.signals'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_admin_add_records_initiator_on_event(
    mock_domain, mock_repo, mock_signals
):
    """admin_add_participant records initiator on event."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = None

    # Use a sentinel for the initiator
    initiator = object()

    result = tournament_participant_service.admin_add_participant(
        TOURNAMENT_ID, user_id, initiator=initiator
    )

    assert result.is_ok()
    event = mock_signals.participant_joined.send.call_args[1]['event']
    assert event.initiator is initiator


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_tournament(**kwargs) -> Tournament:
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


def _create_participant(**kwargs) -> TournamentParticipant:
    defaults = {
        'id': TournamentParticipantID(generate_uuid()),
        'user_id': UserID(generate_uuid()),
        'tournament_id': TOURNAMENT_ID,
        'substitute_player': False,
        'team_id': None,
        'created_at': NOW,
        'removed_at': None,
    }
    defaults.update(kwargs)
    return TournamentParticipant(**defaults)
