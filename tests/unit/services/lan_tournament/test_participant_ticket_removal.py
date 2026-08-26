from datetime import UTC, datetime
from unittest.mock import patch

import pytest

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
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.services.lan_tournament.tournament_match_service import (
    DefwinResult,
)
from byceps.util.result import Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')


@pytest.fixture(autouse=True)
def audit_log():
    with patch(
        'byceps.services.lan_tournament.tournament_participant_service.tournament_log_service'
    ) as mock_log:
        yield mock_log.create_log_entry


def _assert_removals_staged(audit_log, count):
    assert audit_log.call_count == count
    for call in audit_log.call_args_list:
        assert call.args[0] == 'participant-removed'
        assert call.kwargs['commit'] is False


# -------------------------------------------------------------------- #
# join_tournament — ticket validation
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_join_tournament_without_ticket_fails(mock_repo, mock_ticket):
    """Joining a tournament without a valid ticket is rejected."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = False

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID, user_id
    )

    assert result.is_err()
    assert 'ticket' in result.unwrap_err().lower()


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_join_tournament_with_ticket_succeeds(
    mock_domain, mock_repo, mock_ticket, mock_signals
):
    """Joining a tournament with a valid ticket is accepted."""
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


# -------------------------------------------------------------------- #
# remove_participants_without_tickets — solo tournaments
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_solo_returns_count(
    mock_repo, mock_ticket, mock_match_svc, mock_signals, audit_log
):
    """Removes ticketless participants and returns their count."""
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
    )
    user1 = UserID(generate_uuid())
    user2 = UserID(generate_uuid())
    p1 = _create_participant(user_id=user1)
    p2 = _create_participant(user_id=user2)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [p1, p2]
    # Only user1 has a ticket
    _mock_locked_roster(mock_repo, [p1, p2])
    mock_ticket.select_ticket_users_for_party.return_value = {user1}

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 1
    mock_repo.delete_participants_by_ids.assert_called_once_with({p2.id})
    mock_repo.commit_session.assert_called_once()
    _assert_removals_staged(audit_log, result.unwrap())


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_all_have_tickets(
    mock_repo, mock_ticket, mock_match_svc, mock_signals
):
    """When all participants have tickets, returns Ok(0)."""
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
    )
    user1 = UserID(generate_uuid())
    p1 = _create_participant(user_id=user1)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [p1]
    mock_ticket.select_ticket_users_for_party.return_value = {user1}
    _mock_locked_roster(mock_repo, [p1])

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 0
    mock_repo.delete_participants_by_ids.assert_not_called()


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_no_participants(
    mock_repo, mock_ticket, mock_match_svc, mock_signals
):
    """When there are no participants at all, returns Ok(0)."""
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
    )

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = []

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 0


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_wrong_status_fails(mock_repo):
    """Cannot remove participants from a COMPLETED tournament."""
    tournament = _create_tournament(
        tournament_status=TournamentStatus.COMPLETED,
    )

    mock_repo.get_tournament.return_value = tournament

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_err()
    assert 'status' in result.unwrap_err().lower()


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@pytest.mark.parametrize(
    'status', [TournamentStatus.ONGOING, TournamentStatus.PAUSED]
)
def test_remove_ticketless_ongoing_triggers_defwins(
    mock_repo, mock_ticket, mock_match_svc, mock_signals, audit_log, status
):
    """In an active solo tournament, defwin logic is triggered
    for each removed participant and soft-delete is used."""
    tournament = _create_tournament(
        tournament_status=status,
        contestant_type=ContestantType.SOLO,
    )
    user1 = UserID(generate_uuid())
    p1 = _create_participant(user_id=user1)
    p2 = _create_participant()

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [p1, p2]
    mock_ticket.select_ticket_users_for_party.return_value = set()
    _mock_locked_roster(mock_repo, [p1, p2])

    def handle_defwin(*args, **kwargs):
        mock_repo.soft_delete_participants_by_ids.assert_called_once()
        assert mock_repo.soft_delete_participants_by_ids.call_args.args[0] == {
            p1.id,
            p2.id,
        }
        mock_repo.commit_session.assert_not_called()
        return DefwinResult([], [], [])

    mock_match_svc.handle_defwin_for_removed_participant.side_effect = handle_defwin

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 2
    assert mock_match_svc.handle_defwin_for_removed_participant.call_count == 2
    for participant in [p1, p2]:
        mock_match_svc.handle_defwin_for_removed_participant.assert_any_call(
            TOURNAMENT_ID, participant.id, initiator_id=None
        )
    # Soft-delete, not hard-delete
    mock_repo.soft_delete_participants_by_ids.assert_called_once()
    mock_repo.delete_participants_by_ids.assert_not_called()
    _assert_removals_staged(audit_log, result.unwrap())
    mock_repo.commit_session.assert_called_once_with()


# -------------------------------------------------------------------- #
# remove_participants_without_tickets — team tournaments
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_team_transfers_captain(
    mock_repo, mock_ticket, mock_match_svc, mock_signals, audit_log
):
    """When the team captain is ticketless but other members remain,
    captain is transferred to the oldest remaining member."""
    team_id = TournamentTeamID(generate_uuid())
    captain_user_id = UserID(generate_uuid())
    member_user_id = UserID(generate_uuid())

    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.TEAM,
    )
    captain = _create_participant(user_id=captain_user_id, team_id=team_id)
    member = _create_participant(user_id=member_user_id, team_id=team_id)
    team = _create_team(team_id=team_id, captain_user_id=captain_user_id)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [
        captain,
        member,
    ]
    # Only member has a ticket
    mock_ticket.select_ticket_users_for_party.return_value = {member_user_id}
    mock_repo.get_teams_by_ids.return_value = [team]
    _mock_locked_roster(mock_repo, [captain, member], [team])

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 1
    mock_repo.update_team_captain_flush.assert_called_once_with(
        team_id, member_user_id
    )
    mock_repo.update_team_captain.assert_not_called()
    mock_repo.commit_session.assert_called_once_with()
    _assert_removals_staged(audit_log, result.unwrap())


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_captain_handover_is_announced_after_the_commit(
    mock_repo, mock_ticket, mock_match_svc, mock_signals
):
    """The handover event goes out once, and only after the commit."""
    team_id = TournamentTeamID(generate_uuid())
    captain_user_id = UserID(generate_uuid())
    member_user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.TEAM,
    )
    captain = _create_participant(user_id=captain_user_id, team_id=team_id)
    member = _create_participant(user_id=member_user_id, team_id=team_id)
    team = _create_team(team_id=team_id, captain_user_id=captain_user_id)
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [captain, member]
    mock_ticket.select_ticket_users_for_party.return_value = {member_user_id}
    mock_repo.get_teams_by_ids.return_value = [team]
    _mock_locked_roster(mock_repo, [captain, member], [team])
    order = []
    mock_repo.commit_session.side_effect = lambda: order.append('commit')
    mock_signals.captain_transferred.send.side_effect = lambda *a, **kw: (
        order.append('captain_transferred')
    )

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.unwrap() == 1
    assert order == ['commit', 'captain_transferred']
    (call,) = mock_signals.captain_transferred.send.call_args_list
    event = call.kwargs['event']
    assert event.team_id == team_id
    assert event.old_captain_user_id == captain_user_id
    assert event.new_captain_user_id == member_user_id
    assert event.initiator is None


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_team_all_members_ticketless_deletes_team(
    mock_repo, mock_ticket, mock_match_svc, mock_signals, audit_log
):
    """When all team members are ticketless, the team is deleted."""
    team_id = TournamentTeamID(generate_uuid())
    captain_user_id = UserID(generate_uuid())

    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.TEAM,
    )
    captain = _create_participant(user_id=captain_user_id, team_id=team_id)
    team = _create_team(team_id=team_id, captain_user_id=captain_user_id)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [captain]
    mock_ticket.select_ticket_users_for_party.return_value = set()
    mock_repo.get_teams_by_ids.return_value = [team]
    _mock_locked_roster(mock_repo, [captain], [team])

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 1
    mock_repo.delete_team_flush.assert_called_once_with(team_id)
    mock_repo.soft_delete_team_flush.assert_not_called()
    mock_repo.remove_team_from_participants_flush.assert_called_once_with(team_id)
    mock_repo.commit_session.assert_called_once_with()
    _assert_removals_staged(audit_log, result.unwrap())


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@pytest.mark.parametrize(
    'status', [TournamentStatus.ONGOING, TournamentStatus.PAUSED]
)
def test_remove_ticketless_team_ongoing_soft_deletes(
    mock_repo, mock_ticket, mock_match_svc, mock_signals, audit_log, status
):
    """When all team members are ticketless in an active bracket,
    the team is soft-deleted."""
    team_id = TournamentTeamID(generate_uuid())
    captain_user_id = UserID(generate_uuid())

    tournament = _create_tournament(
        tournament_status=status,
        contestant_type=ContestantType.TEAM,
    )
    captain = _create_participant(user_id=captain_user_id, team_id=team_id)
    team = _create_team(team_id=team_id, captain_user_id=captain_user_id)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [captain]
    mock_ticket.select_ticket_users_for_party.return_value = set()
    mock_repo.get_teams_by_ids.return_value = [team]
    _mock_locked_roster(mock_repo, [captain], [team])

    def handle_defwin(*args, **kwargs):
        mock_repo.soft_delete_participants_by_ids.assert_called_once()
        mock_repo.soft_delete_team_flush.assert_called_once()
        assert mock_repo.soft_delete_team_flush.call_args.args[0] == team_id
        mock_repo.commit_session.assert_not_called()
        return DefwinResult([], [], [])

    mock_match_svc.handle_defwin_for_removed_team.side_effect = handle_defwin

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 1
    mock_repo.soft_delete_team_flush.assert_called_once()
    mock_repo.delete_team_flush.assert_not_called()
    # Participants are also soft-deleted (not hard-deleted)
    mock_repo.soft_delete_participants_by_ids.assert_called_once()
    mock_repo.delete_participants_by_ids.assert_not_called()
    _assert_removals_staged(audit_log, result.unwrap())
    mock_repo.remove_team_from_participants_flush.assert_called_once_with(team_id)
    mock_repo.commit_session.assert_called_once_with()


# -------------------------------------------------------------------- #
# get_ticket_status_for_participants
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_get_ticket_status_separates_participants(mock_repo, mock_ticket):
    """Returns users_with_tickets set and list of ticketless
    participants."""
    user1 = UserID(generate_uuid())
    user2 = UserID(generate_uuid())
    p1 = _create_participant(user_id=user1)
    p2 = _create_participant(user_id=user2)

    mock_repo.get_participants_for_tournament.return_value = [p1, p2]
    mock_ticket.select_ticket_users_for_party.return_value = {user1}

    with_tickets, without_tickets = (
        tournament_participant_service.get_ticket_status_for_participants(
            TOURNAMENT_ID, PARTY_ID
        )
    )

    assert with_tickets == {user1}
    assert without_tickets == [p2]


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_get_ticket_status_empty_tournament(mock_repo, mock_ticket):
    """Empty tournament returns empty sets."""
    mock_repo.get_participants_for_tournament.return_value = []
    mock_ticket.select_ticket_users_for_party.return_value = set()

    with_tickets, without_tickets = (
        tournament_participant_service.get_ticket_status_for_participants(
            TOURNAMENT_ID, PARTY_ID
        )
    )

    assert with_tickets == set()
    assert without_tickets == []


# -------------------------------------------------------------------- #
# _handle_team_captains (tested through
# remove_participants_without_tickets)
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_team_captain_not_removed_no_transfer(
    mock_repo, mock_ticket, mock_match_svc, mock_signals, audit_log
):
    """When a non-captain member is ticketless, captain stays."""
    team_id = TournamentTeamID(generate_uuid())
    captain_user_id = UserID(generate_uuid())
    member_user_id = UserID(generate_uuid())

    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.TEAM,
    )
    captain = _create_participant(user_id=captain_user_id, team_id=team_id)
    member = _create_participant(user_id=member_user_id, team_id=team_id)
    team = _create_team(team_id=team_id, captain_user_id=captain_user_id)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = [
        captain,
        member,
    ]
    # Only captain has a ticket; member does not
    mock_ticket.select_ticket_users_for_party.return_value = {captain_user_id}
    mock_repo.get_teams_by_ids.return_value = [team]
    _mock_locked_roster(mock_repo, [captain, member], [team])

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 1
    # Captain is not being removed, so no transfer
    mock_repo.update_team_captain.assert_not_called()
    mock_repo.update_team_captain_flush.assert_not_called()
    mock_repo.commit_session.assert_called_once_with()
    # Team is not empty so no deletion
    mock_repo.delete_team_flush.assert_not_called()
    _assert_removals_staged(audit_log, result.unwrap())


# -------------------------------------------------------------------- #
# get_ticket_status_for_participants — skip redundant query (FIX 1)
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_get_ticket_status_skips_query_when_participants_provided(
    mock_repo, mock_ticket
):
    """When participants are passed in, the repository query is
    skipped."""
    user1 = UserID(generate_uuid())
    p1 = _create_participant(user_id=user1)

    mock_ticket.select_ticket_users_for_party.return_value = {user1}

    tournament_participant_service.get_ticket_status_for_participants(
        TOURNAMENT_ID, PARTY_ID, participants=[p1]
    )

    mock_repo.get_participants_for_tournament.assert_not_called()


# -------------------------------------------------------------------- #
# admin_add_participant — no ticket check
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_domain_service'
)
def test_admin_add_participant_skips_ticket_check(
    mock_domain, mock_repo, mock_ticket, mock_signals
):
    """admin_add_participant() bypasses ticket validation."""
    user_id = UserID(generate_uuid())
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
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
    mock_ticket.uses_any_ticket_for_party.assert_not_called()
    mock_repo.create_participant.assert_called_once()


# -------------------------------------------------------------------- #
# get_teams_below_minimum_size
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_teams_below_minimum_returns_under_threshold(mock_repo):
    """Teams with fewer members than min_players_in_team are
    returned."""
    tournament = _create_tournament(
        contestant_type=ContestantType.TEAM,
        min_players_in_team=3,
    )
    team = _create_team(name='Small Team')

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = [team]
    mock_repo.get_team_member_counts.return_value = {team.id: 1}

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID
    )

    assert len(result) == 1
    assert result[0] == (team, 1)


@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_teams_below_minimum_returns_empty_when_no_min(mock_repo):
    """Returns empty list when min_players_in_team is None."""
    tournament = _create_tournament(
        contestant_type=ContestantType.TEAM,
        min_players_in_team=None,
    )

    mock_repo.get_tournament.return_value = tournament

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID
    )

    assert result == []
    mock_repo.get_teams_for_tournament.assert_not_called()


# -------------------------------------------------------------------- #
# remove_participants_without_tickets — lock hygiene
# -------------------------------------------------------------------- #


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_locks_the_participants_in_one_statement(
    mock_repo, mock_ticket, mock_match_svc, mock_signals
):
    """One ordered bulk lock, however many participants are removed."""
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
    )
    users = [UserID(generate_uuid()) for _ in range(5)]
    everyone = [_create_participant(user_id=u) for u in users]
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = everyone
    _mock_locked_roster(mock_repo, everyone)
    mock_ticket.select_ticket_users_for_party.return_value = {users[0]}

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.unwrap() == 4
    mock_repo.get_participants_for_update.assert_called_once()
    (locked_ids,) = mock_repo.get_participants_for_update.call_args.args
    assert sorted(locked_ids) == sorted(p.id for p in everyone)
    mock_repo.get_participant_for_update.assert_not_called()


@pytest.mark.parametrize('everyone_has_a_ticket', [False, True])
@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.ticket_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_participant_service.tournament_repository'
)
def test_remove_ticketless_releases_the_locks_before_returning_zero(
    mock_repo, mock_ticket, mock_signals, everyone_has_a_ticket
):
    """`Ok(0)` is no failure for the decorator, so the lock must go here."""
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        contestant_type=ContestantType.SOLO,
    )
    user = UserID(generate_uuid())
    participant = _create_participant(user_id=user)
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = (
        [participant] if everyone_has_a_ticket else []
    )
    _mock_locked_roster(mock_repo, [participant])
    mock_ticket.select_ticket_users_for_party.return_value = {user}

    result = tournament_participant_service.remove_participants_without_tickets(
        TOURNAMENT_ID, PARTY_ID
    )

    assert result.unwrap() == 0
    mock_repo.rollback_session.assert_called_once_with()
    mock_repo.commit_session.assert_not_called()


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _mock_locked_roster(mock_repo, participants, teams=()):
    """Return scoped frozen snapshots for every fresh membership lock."""
    participants_by_id = {p.id: p for p in participants}
    teams_by_id = {t.id: t for t in teams}

    def locked_participants(participant_ids):
        found = [participants_by_id[i] for i in sorted(participant_ids)]
        assert all(p.tournament_id == TOURNAMENT_ID for p in found)
        mock_repo.lock_tournament_for_update.assert_called_once_with(
            TOURNAMENT_ID
        )
        return found

    def locked_team(team_id):
        team = teams_by_id[team_id]
        assert team.tournament_id == TOURNAMENT_ID
        mock_repo.lock_tournament_for_update.assert_called_once_with(TOURNAMENT_ID)
        return team

    def team_members(team_id):
        assert team_id in teams_by_id
        return [p for p in participants if p.team_id == team_id]

    mock_repo.get_participants_for_update.side_effect = locked_participants
    mock_repo.get_team_for_update.side_effect = locked_team
    mock_repo.get_participants_for_team.side_effect = team_members
    # These fixtures have no generated match assignments to refresh.
    mock_repo.get_contestants_for_tournament.return_value = {}
    mock_repo.get_matches_for_tournament.return_value = []
    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = []
    mock_repo.find_contestant_entries_for_team_in_tournament.return_value = []


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
        'game_format': None,
        'elimination_mode': None,
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


def _create_team(**kwargs) -> TournamentTeam:
    defaults = {
        'id': TournamentTeamID(generate_uuid()),
        'tournament_id': TOURNAMENT_ID,
        'name': 'Test Team',
        'tag': None,
        'description': None,
        'image_url': None,
        'captain_user_id': UserID(generate_uuid()),
        'join_code': None,
        'created_at': NOW,
        'updated_at': None,
        'removed_at': None,
    }
    if 'team_id' in kwargs:
        kwargs['id'] = kwargs.pop('team_id')
    defaults.update(kwargs)
    return TournamentTeam(**defaults)
