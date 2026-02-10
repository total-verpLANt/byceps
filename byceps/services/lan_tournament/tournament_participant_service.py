from datetime import datetime, UTC

from byceps.services.user.models.user import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_domain_service,
    tournament_repository,
)
from .events import ParticipantJoinedEvent, ParticipantLeftEvent
from .models.tournament import TournamentID
from .models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeamID


def join_tournament(
    tournament_id: TournamentID,
    user_id: UserID,
    *,
    substitute_player: bool = False,
    team_id: TournamentTeamID | None = None,
) -> Result[tuple[TournamentParticipant, ParticipantJoinedEvent], str]:
    """Register a user as participant in a tournament."""
    # Use SELECT FOR UPDATE to prevent race conditions
    tournament = tournament_repository.get_tournament_for_update(tournament_id)

    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        return Err('Registration is not open for this tournament.')

    existing = tournament_repository.find_participant_by_user(
        tournament_id, user_id
    )
    if existing is not None:
        return Err('User is already registered for this tournament.')

    current_count = tournament_repository.get_participant_count(tournament_id)
    count_result = tournament_domain_service.validate_participant_count(
        tournament, current_count
    )
    if count_result.is_err():
        return Err(count_result.unwrap_err())

    now = datetime.now(UTC)
    participant_id = TournamentParticipantID(generate_uuid7())

    participant = TournamentParticipant(
        id=participant_id,
        user_id=user_id,
        tournament_id=tournament_id,
        substitute_player=substitute_player,
        team_id=team_id,
        created_at=now,
    )

    tournament_repository.create_participant(participant)

    event = ParticipantJoinedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
        participant_id=participant_id,
    )
    signals.participant_joined.send(None, event=event)

    return Ok((participant, event))


def leave_tournament(
    tournament_id: TournamentID,
    participant_id: TournamentParticipantID,
) -> Result[ParticipantLeftEvent, str]:
    """Remove a participant from a tournament."""
    participant = tournament_repository.find_participant(participant_id)
    if participant is None:
        return Err('Participant not found.')

    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        return Err('Cannot leave tournament after it has started or completed.')

    tournament_repository.delete_participant(participant_id)

    now = datetime.now(UTC)
    event = ParticipantLeftEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
        participant_id=participant_id,
    )
    signals.participant_left.send(None, event=event)

    return Ok(event)


def get_participant(
    participant_id: TournamentParticipantID,
) -> TournamentParticipant:
    """Return the participant."""
    return tournament_repository.get_participant(participant_id)


def get_participants_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentParticipant]:
    """Return all participants for that tournament."""
    return tournament_repository.get_participants_for_tournament(tournament_id)
