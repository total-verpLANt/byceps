import dataclasses
from datetime import datetime, UTC
from urllib.parse import urlparse

from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result

from . import (
    signals,
    tournament_domain_service,
    tournament_participant_service,
    tournament_repository,
    tournament_team_service,
)
from .events import (
    TournamentCreatedEvent,
    TournamentDeletedEvent,
    TournamentStatusChangedEvent,
    TournamentUpdatedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.score_ordering import ScoreOrdering
from .models.tournament_mode import TournamentMode
from .models.tournament_status import TournamentStatus


# Statuses where only cosmetic fields may be edited.
EDIT_LOCKED_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.ONGOING,
    TournamentStatus.PAUSED,
})


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
    score_ordering: ScoreOrdering | None = None,
) -> Result[tuple[Tournament, TournamentCreatedEvent], str]:
    """Create a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.create' permission).
    """
    # Validate name length
    if len(name.strip()) > 80:
        return Err('Tournament name must not exceed 80 characters.')

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
        score_ordering=score_ordering,
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
    score_ordering: ScoreOrdering | None = None,
) -> Result[Tournament, str]:
    """Update a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.update' permission).

    Note: tournament_status is not accepted here.  Status
    changes must go through ``change_status`` to enforce the
    state machine.
    """
    # Validate name length
    if len(name.strip()) > 80:
        return Err('Tournament name must not exceed 80 characters.')

    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    tournament = tournament_repository.get_tournament(tournament_id)

    # Reject structural changes while the tournament is in play.
    if tournament.tournament_status in EDIT_LOCKED_STATUSES:
        locked_changes: list[str] = []
        if name != tournament.name:
            locked_changes.append('name')
        if game != tournament.game:
            locked_changes.append('game')
        if start_time != tournament.start_time:
            locked_changes.append('start_time')
        if contestant_type != tournament.contestant_type:
            locked_changes.append('contestant_type')
        if tournament_mode != tournament.tournament_mode:
            locked_changes.append('tournament_mode')
        if score_ordering != tournament.score_ordering:
            locked_changes.append('score_ordering')
        if min_players != tournament.min_players:
            locked_changes.append('min_players')
        if max_players != tournament.max_players:
            locked_changes.append('max_players')
        if min_teams != tournament.min_teams:
            locked_changes.append('min_teams')
        if max_teams != tournament.max_teams:
            locked_changes.append('max_teams')
        if min_players_in_team != tournament.min_players_in_team:
            locked_changes.append('min_players_in_team')
        if max_players_in_team != tournament.max_players_in_team:
            locked_changes.append('max_players_in_team')
        if locked_changes:
            fields_str = ', '.join(locked_changes)
            return Err(
                f'Tournament is {tournament.tournament_status.name.lower()}. '
                f'Only description, image, and ruleset can be changed. '
                f'Attempted to change: {fields_str}.'
            )

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
        score_ordering=score_ordering,
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

    SECURITY NOTE: Authorization must be checked at blueprint layer
    before calling this function (requires
    'lan_tournament.administrate' permission).

    CASCADE HANDLING: Deletes all dependent entities in correct
    order:
    1. Score submissions (FK to participants/teams)
    2. Match comments
    3. Match contestants
    4. Matches
    5. Winner references (FK back to teams/participants)
    6. Participants
    7. Teams
    8. Tournament itself
    """
    # Delete in dependency order (children first, then parent).
    # All repo calls use commit=False so the entire cascade is a
    # single atomic transaction committed once at the end.
    # Wrapped in try/except to rollback on partial flush failure,
    # preventing session poisoning if a caller catches the exception.
    try:
        tournament_repository.delete_submissions_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_comments_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_contestants_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_matches_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.clear_winner_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_participants_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_teams_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_tournament(tournament_id, commit=False)

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

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


def get_participant_counts_for_tournaments(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, int]:
    """Return participant counts keyed by tournament ID."""
    return tournament_repository.get_participant_counts_for_tournaments(
        tournament_ids
    )


def _has_bracket_generated(
    tournament_id: TournamentID,
) -> bool:
    """Check if brackets have been generated for the tournament."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    return len(matches) > 0


def change_status(
    tournament_id: TournamentID,
    new_status: TournamentStatus,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Change the tournament status."""
    tournament = tournament_repository.get_tournament(tournament_id)

    # Validate state machine transition first
    result = tournament_domain_service.change_tournament_status(
        tournament, new_status
    )
    if result.is_err():
        return Err(result.unwrap_err())

    # Only after a valid transition: check bracket exists when starting
    if new_status == TournamentStatus.ONGOING:
        if tournament.tournament_mode and tournament.tournament_mode.requires_bracket:
            if not _has_bracket_generated(tournament_id):
                return Err(
                    'Cannot start tournament without generated brackets. '
                    'Generate brackets first.'
                )

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


def resolve_winner_display_name(tournament: Tournament) -> str | None:
    """Return a human-readable winner name for a completed tournament,
    or ``None`` if no winner is recorded or the tournament is not
    completed.
    """
    if tournament.tournament_status != TournamentStatus.COMPLETED:
        return None

    if tournament.winner_team_id:
        team = tournament_team_service.find_team(tournament.winner_team_id)
        if team is not None:
            return team.name
    elif tournament.winner_participant_id:
        from byceps.services.user import user_service

        participant = tournament_participant_service.find_participant(
            tournament.winner_participant_id
        )
        if participant is not None:
            user = user_service.get_user(participant.user_id)
            return user.screen_name

    return None
