import dataclasses
from datetime import datetime, UTC
from urllib.parse import urlparse


from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.user.models.user import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_domain_service,
    tournament_repository,
)
from .events import (
    TeamCreatedEvent,
    TeamDeletedEvent,
    TeamMemberJoinedEvent,
    TeamMemberLeftEvent,
)
from .models.tournament import TournamentID
from .models.tournament_participant import (
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeam, TournamentTeamID


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


def create_team(
    tournament_id: TournamentID,
    name: str,
    captain_user_id: UserID,
    *,
    tag: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    join_code: str | None = None,
) -> Result[tuple[TournamentTeam, TeamCreatedEvent], str]:
    """Create a team in a tournament."""
    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    # Use SELECT FOR UPDATE to prevent race conditions
    tournament = tournament_repository.get_tournament_for_update(tournament_id)

    current_count = len(
        tournament_repository.get_teams_for_tournament(tournament_id)
    )
    count_result = tournament_domain_service.validate_team_count(
        tournament, current_count
    )
    if count_result.is_err():
        return Err(count_result.unwrap_err())

    # Normalize tag to uppercase
    tag = tag.upper() if tag else None

    # Check for duplicate team name
    existing = tournament_repository.find_active_team_by_name(
        tournament_id, name
    )
    if existing is not None:
        return Err('A team with this name already exists in this tournament.')

    # Check for duplicate team tag
    if tag:
        existing = tournament_repository.find_active_team_by_tag(
            tournament_id, tag
        )
        if existing is not None:
            return Err(
                'A team with this tag already exists in this tournament.'
            )

    # Captain must be a registered participant in this tournament
    captain_participant = tournament_repository.find_participant_by_user(
        tournament_id, captain_user_id
    )
    if captain_participant is None:
        return Err(
            'The team captain must be a registered participant'
            ' in this tournament.'
        )

    now = datetime.now(UTC)
    team_id = TournamentTeamID(generate_uuid7())

    team = TournamentTeam(
        id=team_id,
        tournament_id=tournament_id,
        name=name,
        tag=tag,
        description=description,
        image_url=image_url,
        captain_user_id=captain_user_id,
        join_code=join_code,
        created_at=now,
    )

    try:
        tournament_repository.create_team(team)
    except IntegrityError as e:
        db.session.rollback()
        constraint = getattr(e.orig, 'constraint_name', '') or ''
        if 'uq_lan_tournament_teams_active_name_ci' in constraint:
            return Err(
                'A team with this name already exists in this tournament.'
            )
        if 'uq_lan_tournament_teams_active_tag_ci' in constraint:
            return Err(
                'A team with this tag already exists in this tournament.'
            )
        raise

    # Auto-assign captain to the new team
    updated_captain = dataclasses.replace(
        captain_participant, team_id=team_id
    )
    try:
        tournament_repository.update_participant(updated_captain)
    except Exception:
        db.session.rollback()
        raise

    event = TeamCreatedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
        team_id=team_id,
    )
    signals.team_created.send(None, event=event)

    return Ok((team, event))


def update_team(
    team_id: TournamentTeamID,
    *,
    name: str,
    tag: str | None,
    description: str | None,
    image_url: str | None,
    join_code: str | None,
    current_user_id: UserID | None = None,
) -> Result[TournamentTeam, str]:
    """Update a team.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function. This function includes business logic validation
    for team captain ownership when current_user_id is provided.
    """
    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    # Normalize tag to uppercase
    tag = tag.upper() if tag else None

    team = tournament_repository.get_team(team_id)

    # Serialize concurrent team updates within the same tournament
    tournament_repository.lock_tournament_for_update(team.tournament_id)

    # Business logic: Only team captain can update (unless admin bypass)
    if current_user_id is not None and team.captain_user_id != current_user_id:
        return Err('Only the team captain can update this team.')

    # Check for duplicate team name (skip if unchanged)
    if name.lower() != team.name.lower():
        existing = tournament_repository.find_active_team_by_name(
            team.tournament_id, name
        )
        if existing is not None and existing.id != team.id:
            return Err(
                'A team with this name already exists in this tournament.'
            )

    # Check for duplicate team tag (skip if unchanged or empty)
    if tag and tag != (team.tag.upper() if team.tag else ''):
        existing = tournament_repository.find_active_team_by_tag(
            team.tournament_id, tag
        )
        if existing is not None and existing.id != team.id:
            return Err(
                'A team with this tag already exists in this tournament.'
            )

    updated = dataclasses.replace(
        team,
        name=name,
        tag=tag,
        description=description,
        image_url=image_url,
        join_code=join_code,
        updated_at=datetime.now(UTC),
    )

    try:
        tournament_repository.update_team(updated)
    except IntegrityError as e:
        db.session.rollback()
        constraint = getattr(e.orig, 'constraint_name', '') or ''
        if 'uq_lan_tournament_teams_active_name_ci' in constraint:
            return Err(
                'A team with this name already exists in this tournament.'
            )
        if 'uq_lan_tournament_teams_active_tag_ci' in constraint:
            return Err(
                'A team with this tag already exists in this tournament.'
            )
        raise

    return Ok(updated)


def delete_team(
    team_id: TournamentTeamID,
    *,
    current_user_id: UserID | None = None,
) -> Result[TeamDeletedEvent, str]:
    """Delete a team and clean up references.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function. This function includes business logic validation
    for team captain ownership when current_user_id is provided.

    CASCADE HANDLING:
    - Participants: Sets team_id to NULL (team members remain as individuals)
    - Match contestants: Deletes contestant records referencing this team
    """
    team = tournament_repository.find_team(team_id)
    if team is None:
        return Err('Team not found.')

    # Business logic: Only team captain can delete (unless admin bypass)
    if current_user_id is not None and team.captain_user_id != current_user_id:
        return Err('Only the team captain can delete this team.')

    # Remove team references before deleting team
    tournament_repository.remove_team_from_participants(team_id)
    tournament_repository.remove_team_from_contestants(team_id)
    tournament_repository.delete_team(team_id)

    now = datetime.now(UTC)
    event = TeamDeletedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
    )
    signals.team_deleted.send(None, event=event)

    return Ok(event)


def find_team(
    team_id: TournamentTeamID,
) -> TournamentTeam | None:
    """Return the team, or `None` if not found."""
    return tournament_repository.find_team(team_id)


def get_team(
    team_id: TournamentTeamID,
) -> TournamentTeam:
    """Return the team."""
    return tournament_repository.get_team(team_id)


def get_teams_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentTeam]:
    """Return all teams for that tournament."""
    return tournament_repository.get_teams_for_tournament(tournament_id)


def get_team_member_counts(
    tournament_id: TournamentID,
) -> dict[TournamentTeamID, int]:
    """Return active member count per team in a single query."""
    return tournament_repository.get_team_member_counts(tournament_id)


def get_teams_by_ids(
    team_ids: set[TournamentTeamID],
) -> list[TournamentTeam]:
    """Return teams matching the given IDs."""
    return tournament_repository.get_teams_by_ids(team_ids)


def join_team(
    participant_id: TournamentParticipantID,
    team_id: TournamentTeamID,
    join_code: str | None = None,
) -> Result[TeamMemberJoinedEvent, str]:
    """Add a participant to a team."""
    # Use SELECT FOR UPDATE to prevent race conditions
    team = tournament_repository.get_team_for_update(team_id)

    participant = tournament_repository.find_participant(participant_id)
    if participant is None:
        return Err('Participant not found.')

    # Validate join code if team has one
    if team.join_code is not None:
        if join_code is None:
            return Err('Join code required.')
        if not verify_team_join_code(team_id, join_code):
            return Err('Invalid join code.')

    # Get tournament to check team capacity limits (with lock)
    tournament = tournament_repository.get_tournament_for_update(
        team.tournament_id
    )

    # Check team capacity if max_players_in_team is set
    if tournament.max_players_in_team is not None:
        current_members = tournament_repository.get_participants_for_team(
            team_id
        )
        if len(current_members) >= tournament.max_players_in_team:
            return Err('Team is full.')

    updated = dataclasses.replace(participant, team_id=team_id)
    tournament_repository.update_participant(updated)

    now = datetime.now(UTC)
    event = TeamMemberJoinedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
        participant_id=participant_id,
    )
    signals.team_member_joined.send(None, event=event)

    return Ok(event)


def leave_team(
    participant_id: TournamentParticipantID,
) -> Result[TeamMemberLeftEvent, str]:
    """Remove a participant from their team."""
    participant = tournament_repository.find_participant(participant_id)
    if participant is None:
        return Err('Participant not found.')

    if participant.team_id is None:
        return Err('Participant is not in a team.')

    team_id = participant.team_id
    team = tournament_repository.get_team(team_id)

    # Captain validation: Cannot leave if other members exist
    if team.captain_user_id == participant.user_id:
        members = tournament_repository.get_participants_for_team(team_id)
        if len(members) > 1:
            return Err(
                'Team captain cannot leave while team has other members. '
                'Transfer captain role first or have other members leave.'
            )
        # If captain is only member, will delete team after leaving (below)

    # Status validation: Cannot leave after tournament has started
    tournament = tournament_repository.get_tournament(participant.tournament_id)
    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        return Err(
            'Cannot leave team after tournament has started or completed.'
        )

    updated = dataclasses.replace(participant, team_id=None)
    tournament_repository.update_participant(updated)

    now = datetime.now(UTC)
    event = TeamMemberLeftEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=participant.tournament_id,
        team_id=team_id,
        participant_id=participant_id,
    )
    signals.team_member_left.send(None, event=event)

    # Auto-delete empty team: If captain was the last member, delete team
    remaining_members = tournament_repository.get_participants_for_team(team_id)
    if len(remaining_members) == 0:
        tournament_repository.delete_team(team_id)

    return Ok(event)


def verify_team_join_code(
    team_id: TournamentTeamID,
    join_code: str,
) -> bool:
    """Verify if the provided join code matches the team's join code."""
    team = tournament_repository.find_team(team_id)
    if team is None:
        return False

    if team.join_code is None:
        return False

    return team.join_code == join_code
