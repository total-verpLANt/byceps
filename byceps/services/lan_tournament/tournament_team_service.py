import dataclasses
from collections.abc import Collection
from datetime import datetime, UTC
from functools import wraps
from urllib.parse import urlparse


from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_domain_service,
    tournament_repository,
)
from .db_error_helpers import extract_constraint_name
from .events import (
    CaptainTransferredEvent,
    TeamCreatedEvent,
    TeamDeletedEvent,
    TeamMemberJoinedEvent,
    TeamMemberLeftEvent,
)
from .models.tournament import TournamentID
from .models.tournament_match import TournamentMatchID
from .models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeam, TournamentTeamID
from .tournament_participant_service import (
    _dispatch_roster_invitations_after_signals,
    _lock_roster_matches_flush,
    _refresh_roster_matches_flush,
)


def _rollback_roster_on_failure(operation):
    """The public roster operation owns rollback, commit and later effects."""

    @wraps(operation)
    def wrapped(*args, **kwargs):
        try:
            result = operation(*args, **kwargs)
        except BaseException:
            tournament_repository.rollback_session()
            raise
        if result.is_err():
            tournament_repository.rollback_session()
        return result

    return wrapped


def _find_locked_team(team_id: TournamentTeamID) -> TournamentTeam | None:
    """Discover scope without locking; reload the team after tournament lock."""
    team = tournament_repository.find_team(team_id)
    if team is None:
        return None
    tournament_repository.get_tournament_for_update(team.tournament_id)
    return tournament_repository.get_team_for_update(team_id)


def _is_acting_captain(team: TournamentTeam, user_id: UserID) -> bool:
    """Return whether the user holds the captaincy and still sits on the team.

    Call it after the roster lock, so the membership is current.
    """
    return team.captain_user_id == user_id and any(
        m.user_id == user_id
        for m in tournament_repository.get_participants_for_team(team.id)
    )


def _clear_team_winner_flush(team: TournamentTeam) -> None:
    tournament = tournament_repository.get_tournament(
        team.tournament_id, fresh=True
    )
    if tournament.winner_team_id == team.id:
        tournament_repository.clear_winner_for_tournament(
            team.tournament_id, commit=False
        )


def _remove_team_contestants_flush(
    team_id: TournamentTeamID, match_ids: Collection[TournamentMatchID],
) -> None:
    """Keep the existing all-assignment cleanup and audit its backstop changes."""
    from . import tournament_match_service

    before = [
        tournament_repository.get_match_for_update(mid)
        for mid in sorted(set(match_ids))
    ]
    tournament_repository.remove_team_from_contestants_flush(team_id)
    for match in before:
        tournament_match_service._audit_engine_pairing_change_flush(match)


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


@_rollback_roster_on_failure
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

    captain_participant = tournament_repository.get_participant_for_update(
        captain_participant.id
    )
    # Captain must not already be on a team
    if captain_participant.team_id is not None:
        return Err(
            'The team captain is already assigned to a team.'
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
        constraint = extract_constraint_name(e)
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
    updated_captain = dataclasses.replace(captain_participant, team_id=team_id)
    try:
        tournament_repository.update_participant_flush(updated_captain)
        tournament_repository.commit_session()
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


@_rollback_roster_on_failure
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

    # Business logic: Only team captain can update (unless admin bypass).
    # The captaincy may have moved while this call waited for the lock.
    if current_user_id is not None:
        team = tournament_repository.get_team_for_update(team_id)
        if not _is_acting_captain(team, current_user_id):
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
        constraint = extract_constraint_name(e)
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


@_rollback_roster_on_failure
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
    - Tournament winner: Clears winner_team_id if this team is the winner
    """
    team = _find_locked_team(team_id)
    if team is None:
        return Err('Team not found.')

    # Business logic: Only team captain can delete (unless admin bypass)
    if current_user_id is not None and team.captain_user_id != current_user_id:
        return Err('Only the team captain can delete this team.')

    affected = _lock_roster_matches_flush(team.tournament_id, team_ids=[team_id])
    # Remove references without committing before pairing/history cleanup.
    tournament_repository.remove_team_from_participants_flush(team_id)
    _remove_team_contestants_flush(team_id, affected)
    _clear_team_winner_flush(team)
    tournament_repository.delete_team_flush(team_id)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=datetime.now(UTC))
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()

    now = datetime.now(UTC)
    event = TeamDeletedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
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


def get_team_members(
    team_id: TournamentTeamID,
) -> list[TournamentParticipant]:
    """Return active members of a team."""
    return tournament_repository.get_participants_for_team(team_id)


def get_team_member_counts(
    tournament_id: TournamentID,
) -> dict[TournamentTeamID, int]:
    """Return active member count per team in a single query."""
    return tournament_repository.get_team_member_counts(tournament_id)


def get_team_counts_for_tournaments(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, int]:
    """Return active team counts per tournament in a single query."""
    return tournament_repository.get_team_counts_for_tournaments(tournament_ids)


def get_teams_by_ids(
    team_ids: set[TournamentTeamID],
) -> list[TournamentTeam]:
    """Return teams matching the given IDs."""
    return tournament_repository.get_teams_by_ids(team_ids)


@_rollback_roster_on_failure
def join_team(
    participant_id: TournamentParticipantID,
    team_id: TournamentTeamID,
    join_code: str | None = None,
) -> Result[TeamMemberJoinedEvent, str]:
    """Add a participant to a team."""
    team = _find_locked_team(team_id)
    if team is None:
        return Err('Unknown team.')
    team_id = team.id

    participant = tournament_repository.find_participant_fresh(participant_id)
    if participant is None or participant.removed_at is not None:
        return Err('Participant not found.')
    if team.removed_at is not None:
        return Err('Unknown team.')
    if participant.tournament_id != team.tournament_id:
        return Err('Participant does not belong to this tournament.')
    if participant.team_id is not None:
        return Err('Participant is already on a team.')

    # Validate join code if team has one
    if team.join_code is not None:
        if join_code is None:
            return Err('Join code required.')
        if team.join_code != join_code:
            return Err('Invalid join code.')

    # Get tournament to check team capacity limits (with lock)
    tournament = tournament_repository.get_tournament(team.tournament_id, fresh=True)

    # Check team capacity if max_players_in_team is set
    if tournament.max_players_in_team is not None:
        current_members = tournament_repository.get_participants_for_team(
            team_id
        )
        if len(current_members) >= tournament.max_players_in_team:
            return Err('Team is full.')

    affected = _lock_roster_matches_flush(
        team.tournament_id, participant_ids=[participant_id], team_ids=[team_id]
    )
    updated = dataclasses.replace(participant, team_id=team_id)
    tournament_repository.update_participant_flush(updated)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=datetime.now(UTC))
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()

    now = datetime.now(UTC)
    event = TeamMemberJoinedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
        participant_id=participant_id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        signals.team_member_joined.send(None, event=event)

    return Ok(event)


@_rollback_roster_on_failure
def leave_team(
    participant_id: TournamentParticipantID,
) -> Result[TeamMemberLeftEvent, str]:
    """Remove a participant from their team."""
    participant = tournament_repository.find_participant(participant_id)
    if participant is None:
        return Err('Participant not found.')

    tournament_repository.get_tournament_for_update(participant.tournament_id)
    participant = tournament_repository.find_participant_fresh(participant_id)
    if participant is None:
        return Err('Participant not found.')
    if participant.team_id is None:
        return Err('Participant is not in a team.')

    team_id = participant.team_id
    team = tournament_repository.get_team_for_update(team_id)

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
    tournament = tournament_repository.get_tournament(participant.tournament_id, fresh=True)
    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        return Err(
            'Cannot leave team after tournament has started or completed.'
        )

    affected = _lock_roster_matches_flush(
        participant.tournament_id, participant_ids=[participant_id], team_ids=[team_id]
    )
    updated = dataclasses.replace(participant, team_id=None)
    tournament_repository.update_participant_flush(updated)

    now = datetime.now(UTC)
    event = TeamMemberLeftEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=participant.tournament_id,
        team_id=team_id,
        participant_id=participant_id,
    )
    # Auto-delete empty team: If captain was the last member, delete team
    remaining_members = tournament_repository.get_participants_for_team(team_id)
    if len(remaining_members) == 0:
        _remove_team_contestants_flush(team_id, affected)
        _clear_team_winner_flush(team)
        tournament_repository.delete_team_flush(team_id)

    refreshed = _refresh_roster_matches_flush(affected, occurred_at=now)
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()
    with _dispatch_roster_invitations_after_signals(pending):
        signals.team_member_left.send(None, event=event)

    return Ok(event)


@_rollback_roster_on_failure
def transfer_captain(
    team_id: TournamentTeamID,
    new_captain_user_id: UserID,
    *,
    acting_captain_id: UserID | None = None,
) -> Result[TournamentTeam, str]:
    """Transfer captain role to another team member.

    The site passes the `acting_captain_id`, who must hold the role and
    sit on the team once the locks are taken.
    """
    team = _find_locked_team(team_id)
    if team is None:
        return Err('Unknown team.')

    affected = _lock_roster_matches_flush(team.tournament_id, team_ids=[team_id])
    if acting_captain_id is not None and not _is_acting_captain(
        team, acting_captain_id
    ):
        return Err('Only the team captain can update this team.')
    if team.captain_user_id == new_captain_user_id:
        return Err('User is already the captain.')

    # Verify the new captain is a member of the team
    members = tournament_repository.get_participants_for_team(team_id)
    member_user_ids = {m.user_id for m in members}
    if new_captain_user_id not in member_user_ids:
        return Err('User is not a member of this team.')

    old_captain_user_id = team.captain_user_id
    tournament_repository.update_team_captain_flush(team_id, new_captain_user_id)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=datetime.now(UTC))
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()

    updated_team = tournament_repository.get_team(team_id)

    now = datetime.now(UTC)
    event = CaptainTransferredEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
        old_captain_user_id=old_captain_user_id,
        new_captain_user_id=new_captain_user_id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        signals.captain_transferred.send(None, event=event)

    return Ok(updated_team)


@_rollback_roster_on_failure
def admin_add_member(
    team_id: TournamentTeamID,
    user_id: UserID,
) -> Result[TeamMemberJoinedEvent, str]:
    """Admin: add a tournament participant to a team (no join code)."""
    team = _find_locked_team(team_id)
    if team is None or team.removed_at is not None:
        return Err('Unknown team.')

    participant = tournament_repository.find_active_participant_by_user(
        team.tournament_id, user_id
    )
    if participant is None:
        return Err('User is not a participant in this tournament.')

    participant = tournament_repository.get_participant_for_update(participant.id)
    if participant.team_id is not None:
        return Err('Participant is already on a team.')

    # Check team capacity
    tournament = tournament_repository.get_tournament(team.tournament_id, fresh=True)
    if tournament.max_players_in_team is not None:
        current_members = tournament_repository.get_participants_for_team(
            team_id
        )
        if len(current_members) >= tournament.max_players_in_team:
            return Err('Team is full.')

    affected = _lock_roster_matches_flush(
        team.tournament_id, participant_ids=[participant.id], team_ids=[team_id]
    )
    updated = dataclasses.replace(participant, team_id=team_id)
    tournament_repository.update_participant_flush(updated)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=datetime.now(UTC))
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()

    now = datetime.now(UTC)
    event = TeamMemberJoinedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
        participant_id=participant.id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        signals.team_member_joined.send(None, event=event)

    return Ok(event)


@_rollback_roster_on_failure
def remove_team_member(
    team_id: TournamentTeamID,
    user_id: UserID,
    *,
    initiator_id: UserID | None = None,
    acting_captain_id: UserID | None = None,
) -> Result[TeamMemberLeftEvent, str]:
    """Remove a non-captain member from a team.

    The `initiator_id` confirms the byes of an automatic playoff
    release that the removal makes due; it defaults to the captain.
    The site passes the `acting_captain_id`, who must hold the role and
    sit on the team once the locks are taken.
    """
    # Note: Unlike self-service `leave_team`, this function
    # intentionally skips tournament status checks — callers are
    # responsible for enforcing status constraints where appropriate
    # (e.g. site views do, admin views don't).
    team = _find_locked_team(team_id)
    if team is None:
        return Err('Unknown team.')

    if team.captain_user_id == user_id:
        return Err('Cannot remove the captain. Transfer captain role first.')

    affected = _lock_roster_matches_flush(team.tournament_id, team_ids=[team_id])
    if acting_captain_id is not None and not _is_acting_captain(
        team, acting_captain_id
    ):
        return Err('Only the team captain can update this team.')
    # Find the participant
    members = tournament_repository.get_participants_for_team(team_id)
    participant = None
    for m in members:
        if m.user_id == user_id:
            participant = m
            break

    if participant is None:
        return Err('User is not a member of this team.')

    updated = dataclasses.replace(participant, team_id=None)
    tournament_repository.update_participant_flush(updated)

    now = datetime.now(UTC)
    event = TeamMemberLeftEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=team.tournament_id,
        team_id=team_id,
        participant_id=participant.id,
    )
    # Auto-delete empty team
    release_due = False
    defwin = None
    team_deleted_event = None
    remaining_members = tournament_repository.get_participants_for_team(team_id)
    if len(remaining_members) == 0:
        tournament = tournament_repository.get_tournament(team.tournament_id, fresh=True)
        bracket_is_active = tournament.tournament_status in (
            TournamentStatus.ONGOING,
            TournamentStatus.PAUSED,
        )

        if bracket_is_active:
            # Defwin: advance opponents past the now-empty team
            from . import tournament_match_service

            defwin = tournament_match_service.handle_defwin_for_removed_team(
                team.tournament_id, team_id,
                initiator_id=initiator_id,
            )
            tournament_repository.remove_team_from_participants_flush(team_id)
            tournament_repository.soft_delete_team_flush(team_id, now)
            release_due = tournament.has_playoffs
        else:
            tournament_repository.remove_team_from_participants_flush(team_id)
            _remove_team_contestants_flush(team_id, affected)
            _clear_team_winner_flush(team)
            tournament_repository.delete_team_flush(team_id)

        team_deleted_event = TeamDeletedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=team.tournament_id,
            team_id=team_id,
        )
    if defwin is not None:
        affected.extend(event.match_id for event in defwin.advanced)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=now)
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()
    with _dispatch_roster_invitations_after_signals(pending):
        signals.team_member_left.send(None, event=event)
        if defwin is not None:
            for advanced_event in defwin.advanced:
                signals.contestant_advanced.send(None, event=advanced_event)
            for confirmed_event in defwin.confirmed:
                signals.match_confirmed.send(None, event=confirmed_event)
            for completed_event in defwin.completed:
                signals.tournament_completed.send(None, event=completed_event)
        if team_deleted_event is not None:
            signals.team_deleted.send(None, event=team_deleted_event)
        if release_due:
            from . import tournament_qualification_service

            tournament_qualification_service.auto_release_after_commit(
                team.tournament_id,
                triggered_by=initiator_id or team.captain_user_id,
            )

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
