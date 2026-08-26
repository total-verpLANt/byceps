from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import (
    tournament_participant_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.match_readiness import (
    MatchReadiness,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.readiness_change import ReadinessChange
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
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
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')


# -------------------------------------------------------------------- #
# join_tournament — soft-delete re-join
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
    mock_repo.find_soft_deleted_participant_by_user.return_value = soft_deleted

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    participant, event = result.unwrap()
    assert participant.id == old_participant_id
    mock_repo.reactivate_participant.assert_called_once()
    mock_repo.create_participant.assert_not_called()


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
@patch(
    'byceps.services.lan_tournament.tournament_readiness_service.dispatch_pending_invitations'
)
@patch(
    'byceps.services.lan_tournament.tournament_invitation_service.reconcile_match_invitations_flush'
)
@patch(
    'byceps.services.lan_tournament.tournament_readiness_service.refresh_pairing_and_invitations_flush'
)
def test_rejoin_updates_fields_on_reactivation(
    mock_refresh, mock_reconcile, mock_dispatch, mock_domain, mock_repo,
    mock_ticket, mock_signals
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
    mock_repo.find_soft_deleted_participant_by_user.return_value = soft_deleted

    team = TournamentTeam(
        id=team_id,
        tournament_id=TOURNAMENT_ID,
        name='Rejoin Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=user_id,
        join_code=None,
        created_at=NOW,
    )
    match_id = TournamentMatchID(generate_uuid())
    match = TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    contestant = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=None,
        score=None,
        created_at=NOW,
    )
    mock_repo.find_team.return_value = team
    mock_repo.get_team_for_update.return_value = team
    mock_repo.get_participants_for_team.return_value = []
    mock_repo.get_contestants_for_tournament.return_value = {match_id: [contestant]}
    mock_repo.get_matches_for_tournament.return_value = [match]

    pending_ids: tuple[MatchInvitationID, ...] = (
        MatchInvitationID(generate_uuid()),
        MatchInvitationID(generate_uuid()),
    )
    change = ReadinessChange(
        match=match,
        readiness=MatchReadiness(
            match_id=match_id,
            status=ReadinessDisplayStatus.NOT_YET_OCCUPIED,
            ready_sides=(),
        ),
        actor_role=None,
        pending_invitation_ids=pending_ids,
    )
    assert change.__dataclass_params__.frozen
    assert change.readiness.__dataclass_params__.frozen
    assert change.pending_invitation_ids == pending_ids
    order = []

    def refresh(mid, *, occurred_at):
        assert mid == match_id
        mock_repo.lock_matches_for_update.assert_called_once_with([match_id])
        mock_repo.reactivate_participant.assert_called_once()
        mock_repo.commit_session.assert_not_called()
        mock_refresh.assert_called_once_with(mid, occurred_at=occurred_at)
        # Retain the former reconciliation boundary's precommit safeguard.
        mock_repo.commit_session.assert_not_called()
        mock_dispatch.assert_not_called()
        mock_signals.participant_joined.send.assert_not_called()
        order.append('refresh')
        return Ok(change)

    mock_refresh.side_effect = refresh

    def commit():
        mock_dispatch.assert_not_called()
        mock_signals.participant_joined.send.assert_not_called()
        order.append('commit')

    def signal(sender, *, event):
        assert sender is None
        assert event.participant_id == old_participant_id
        mock_repo.commit_session.assert_called_once_with()
        mock_dispatch.assert_not_called()
        order.append('signal')

    def dispatch(invitation_ids):
        assert invitation_ids == tuple(sorted(pending_ids, key=str))
        mock_repo.commit_session.assert_called_once_with()
        mock_signals.participant_joined.send.assert_called_once()
        mock_repo.rollback_session.assert_not_called()
        order.append('dispatch')

    mock_repo.commit_session.side_effect = commit
    mock_signals.participant_joined.send.side_effect = signal
    mock_dispatch.side_effect = dispatch

    result = tournament_participant_service.join_tournament(
        TOURNAMENT_ID,
        user_id,
        substitute_player=True,
        team_id=team_id,
    )

    assert result.is_ok()
    call_kwargs = mock_repo.reactivate_participant.call_args
    assert call_kwargs[1]['substitute_player'] is True
    assert call_kwargs[1]['team_id'] == team_id
    participant, _event = result.unwrap()
    assert participant.id == old_participant_id
    assert participant.removed_at is None
    assert participant.team_id == team_id
    assert participant.substitute_player is True
    assert participant.created_at == call_kwargs.kwargs['created_at']
    assert participant.created_at > soft_deleted.created_at
    assert soft_deleted.removed_at == NOW
    mock_repo.create_participant.assert_not_called()
    mock_repo.find_team.assert_called_once_with(team_id)
    assert mock_repo.get_team_for_update.call_count == 2
    mock_repo.get_team_for_update.assert_called_with(team_id)
    mock_refresh.assert_called_once_with(
        match_id, occurred_at=participant.created_at
    )
    # Refresh owns reconciliation; a second direct call would duplicate it.
    mock_reconcile.assert_not_called()
    mock_dispatch.assert_called_once_with(tuple(sorted(pending_ids, key=str)))
    assert order == ['refresh', 'commit', 'signal', 'dispatch']
    mock_repo.get_participants_for_team.assert_called_once_with(team_id)
    mock_repo.rollback_session.assert_not_called()
    mock_repo.commit_session.assert_called_once_with()


@pytest.mark.parametrize(
    'failure', ['refresh_err', 'refresh_exception', 'commit_exception']
)
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
@patch(
    'byceps.services.lan_tournament.tournament_readiness_service.dispatch_pending_invitations'
)
@patch(
    'byceps.services.lan_tournament.tournament_readiness_service.refresh_pairing_and_invitations_flush'
)
def test_rejoin_failure_rolls_back_without_postcommit_effects(
    mock_refresh, mock_dispatch, mock_domain, mock_repo, mock_ticket,
    mock_signals, failure
):
    user_id = UserID(generate_uuid())
    team_id = TournamentTeamID(generate_uuid())
    soft_deleted = _create_participant(user_id=user_id, removed_at=NOW)
    tournament = _create_tournament(
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    team = TournamentTeam(
        id=team_id,
        tournament_id=TOURNAMENT_ID,
        name='Rejoin Failure Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=user_id,
        join_code=None,
        created_at=NOW,
    )
    match_id = TournamentMatchID(generate_uuid())
    match = TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    contestant = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=None,
        score=None,
        created_at=NOW,
    )
    pending_ids: tuple[MatchInvitationID, ...] = (
        MatchInvitationID(generate_uuid()),
    )
    change = ReadinessChange(
        match=match,
        readiness=MatchReadiness(
            match_id=match_id,
            status=ReadinessDisplayStatus.NOT_YET_OCCUPIED,
            ready_sides=(),
        ),
        actor_role=None,
        pending_invitation_ids=pending_ids,
    )
    mock_repo.get_tournament_for_update.return_value = tournament
    mock_ticket.uses_any_ticket_for_party.return_value = True
    mock_repo.find_participant_by_user.return_value = None
    mock_repo.get_participant_count.return_value = 0
    mock_domain.validate_participant_count.return_value = Ok(None)
    mock_repo.find_soft_deleted_participant_by_user.return_value = soft_deleted
    mock_repo.find_team.return_value = team
    mock_repo.get_team_for_update.return_value = team
    mock_repo.get_participants_for_team.return_value = []
    mock_repo.get_contestants_for_tournament.return_value = {match_id: [contestant]}
    mock_repo.get_matches_for_tournament.return_value = [match]

    def refresh(mid, *, occurred_at):
        assert mid == match_id
        assert occurred_at == mock_repo.reactivate_participant.call_args.kwargs['created_at']
        mock_repo.lock_matches_for_update.assert_called_once_with([match_id])
        mock_repo.commit_session.assert_not_called()
        mock_dispatch.assert_not_called()
        mock_signals.participant_joined.send.assert_not_called()
        if failure == 'refresh_err':
            return Err('readiness_refresh_failed')
        if failure == 'refresh_exception':
            raise RuntimeError('readiness_refresh_failed')
        return Ok(change)

    mock_refresh.side_effect = refresh
    if failure == 'commit_exception':
        mock_repo.commit_session.side_effect = RuntimeError('owning_commit_failed')

    if failure == 'refresh_err':
        result = tournament_participant_service.join_tournament(
            TOURNAMENT_ID, user_id, team_id=team_id,
        )
        assert result.is_err()
        assert result.unwrap_err() == 'readiness_refresh_failed'
    else:
        message = (
            'owning_commit_failed' if failure == 'commit_exception'
            else 'readiness_refresh_failed'
        )
        with pytest.raises(RuntimeError, match=message):
            tournament_participant_service.join_tournament(
                TOURNAMENT_ID, user_id, team_id=team_id,
            )

    mock_refresh.assert_called_once_with(
        match_id,
        occurred_at=mock_repo.reactivate_participant.call_args.kwargs['created_at'],
    )
    mock_repo.reactivate_participant.assert_called_once()
    mock_repo.create_participant.assert_not_called()
    assert soft_deleted.removed_at == NOW
    mock_repo.rollback_session.assert_called_once_with()
    if failure == 'commit_exception':
        mock_repo.commit_session.assert_called_once_with()
    else:
        mock_repo.commit_session.assert_not_called()
    mock_dispatch.assert_not_called()
    mock_signals.participant_joined.send.assert_not_called()


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
    mock_repo.find_soft_deleted_participant_by_user.return_value = soft_deleted

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


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
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


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
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
    mock_repo.find_soft_deleted_participant_by_user.return_value = soft_deleted

    result = tournament_participant_service.admin_add_participant(
        TOURNAMENT_ID, user_id
    )

    assert result.is_ok()
    participant, _event = result.unwrap()
    assert participant.id == old_participant_id
    mock_repo.reactivate_participant.assert_called_once()
    mock_repo.create_participant.assert_not_called()


@patch('byceps.services.lan_tournament.tournament_participant_service.signals')
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
