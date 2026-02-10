import dataclasses
from datetime import datetime, UTC
from urllib.parse import urlparse

from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result

from . import (
    signals,
    tournament_domain_service,
    tournament_repository,
)
from .events import (
    TournamentCreatedEvent,
    TournamentDeletedEvent,
    TournamentStatusChangedEvent,
    TournamentUpdatedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.tournament_mode import TournamentMode
from .models.tournament_status import TournamentStatus


def _validate_image_url(image_url: str | None) -> Result[None, str]:
    """Validate image URL to prevent XSS/SSRF attacks."""
    if image_url is None or image_url == '':
        return Ok(None)

    try:
        parsed = urlparse(image_url)
        if parsed.scheme not in ('http', 'https'):
            return Err('Image URL must use http or https scheme.')
        if not parsed.netloc:
            return Err('Image URL must have a valid domain.')
    except Exception:
        return Err('Invalid image URL format.')

    return Ok(None)


def create_tournament(
    party_id: PartyID,
    name: str,
    *,
    game: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    ruleset: str | None = None,
    start_time: datetime | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    min_teams: int | None = None,
    max_teams: int | None = None,
    min_players_in_team: int | None = None,
    max_players_in_team: int | None = None,
    contestant_type: ContestantType | None = None,
    tournament_status: TournamentStatus | None = None,
    tournament_mode: TournamentMode | None = None,
) -> Result[tuple[Tournament, TournamentCreatedEvent], str]:
    """Create a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.create' permission).
    """
    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    tournament, event = tournament_domain_service.create_tournament(
        party_id,
        name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_status=tournament_status,
        tournament_mode=tournament_mode,
    )

    tournament_repository.create_tournament(tournament)

    signals.tournament_created.send(None, event=event)

    return Ok((tournament, event))


def update_tournament(
    tournament_id: TournamentID,
    *,
    name: str,
    game: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    ruleset: str | None = None,
    start_time: datetime | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    min_teams: int | None = None,
    max_teams: int | None = None,
    min_players_in_team: int | None = None,
    max_players_in_team: int | None = None,
    contestant_type: ContestantType | None = None,
    tournament_mode: TournamentMode | None = None,
) -> Result[Tournament, str]:
    """Update a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.update' permission).

    Note: tournament_status is not accepted here.  Status
    changes must go through ``change_status`` to enforce the
    state machine.
    """
    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    tournament = tournament_repository.get_tournament(tournament_id)

    updated = dataclasses.replace(
        tournament,
        name=name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        updated_at=datetime.now(UTC),
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_mode=tournament_mode,
    )

    tournament_repository.update_tournament(updated)

    event = TournamentUpdatedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
    )
    signals.tournament_updated.send(None, event=event)

    return Ok(updated)


def delete_tournament(
    tournament_id: TournamentID,
) -> None:
    """Delete a tournament and all dependent entities.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.administrate' permission).

    CASCADE HANDLING: Deletes all dependent entities in correct order:
    1. Match comments
    2. Match contestants
    3. Matches
    4. Participants
    5. Teams
    6. Tournament itself
    """
    # Delete in dependency order (children first, then parent)
    tournament_repository.delete_comments_for_tournament(tournament_id)
    tournament_repository.delete_contestants_for_tournament(tournament_id)
    tournament_repository.delete_matches_for_tournament(tournament_id)
    tournament_repository.delete_participants_for_tournament(tournament_id)
    tournament_repository.delete_teams_for_tournament(tournament_id)
    tournament_repository.delete_tournament(tournament_id)

    event = TournamentDeletedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
    )
    signals.tournament_deleted.send(None, event=event)


def get_tournament(
    tournament_id: TournamentID,
) -> Tournament:
    """Return the tournament with that ID."""
    return tournament_repository.get_tournament(tournament_id)


def find_tournament(
    tournament_id: TournamentID,
) -> Tournament | None:
    """Return the tournament, or `None` if not found."""
    return tournament_repository.find_tournament(tournament_id)


def get_tournaments_for_party(
    party_id: PartyID,
) -> list[Tournament]:
    """Return all tournaments for that party."""
    return tournament_repository.get_tournaments_for_party(party_id)


def get_participant_count(
    tournament_id: TournamentID,
) -> int:
    """Return the number of participants."""
    return tournament_repository.get_participant_count(tournament_id)


def change_status(
    tournament_id: TournamentID,
    new_status: TournamentStatus,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Change the tournament status."""
    tournament = tournament_repository.get_tournament(tournament_id)

    result = tournament_domain_service.change_tournament_status(
        tournament, new_status
    )
    if result.is_err():
        return Err(result.unwrap_err())

    (event,) = result.unwrap()

    updated = dataclasses.replace(tournament, tournament_status=new_status)
    tournament_repository.update_tournament(updated)

    signals.tournament_status_changed.send(None, event=event)

    return Ok((updated, event))


def start_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Start a tournament."""
    return change_status(tournament_id, TournamentStatus.ONGOING)


def pause_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Pause a tournament."""
    return change_status(tournament_id, TournamentStatus.PAUSED)


def resume_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Resume a paused tournament."""
    return change_status(tournament_id, TournamentStatus.ONGOING)


def end_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """End a tournament."""
    return change_status(tournament_id, TournamentStatus.COMPLETED)
