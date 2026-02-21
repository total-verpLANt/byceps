from datetime import datetime, UTC

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_service
from byceps.services.user.models.user import User, UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_domain_service,
    tournament_match_service,
    tournament_repository,
)
from .events import (
    ContestantAdvancedEvent,
    ParticipantJoinedEvent,
    ParticipantLeftEvent,
    TeamDeletedEvent,
    TeamMemberLeftEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament import Tournament, TournamentID
from .models.tournament_team import TournamentTeam, TournamentTeamID


def _create_or_reactivate_participant(
    tournament_id: TournamentID,
    user_id: UserID,
    *,
    substitute_player: bool,
    team_id: TournamentTeamID | None,
) -> TournamentParticipant:
    """Create a new participant or reactivate a soft-deleted one.

    Flushes only — caller is responsible for committing.
    """
    now = datetime.now(UTC)

    soft_deleted = tournament_repository.find_soft_deleted_participant_by_user(
        tournament_id, user_id
    )
    if soft_deleted is not None:
        tournament_repository.reactivate_participant(
            soft_deleted.id,
            substitute_player=substitute_player,
            team_id=team_id,
            created_at=now,
        )
        return TournamentParticipant(
            id=soft_deleted.id,
            user_id=user_id,
            tournament_id=tournament_id,
            substitute_player=substitute_player,
            team_id=team_id,
            created_at=now,
        )

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
    return participant


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

    has_ticket = ticket_service.uses_any_ticket_for_party(
        user_id, tournament.party_id
    )
    if not has_ticket:
        return Err('You must have a valid ticket for this party to join.')

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

    participant = _create_or_reactivate_participant(
        tournament_id,
        user_id,
        substitute_player=substitute_player,
        team_id=team_id,
    )
    tournament_repository.commit_session()

    event = ParticipantJoinedEvent(
        occurred_at=participant.created_at,
        initiator=None,
        tournament_id=tournament_id,
        participant_id=participant.id,
    )
    signals.participant_joined.send(None, event=event)

    return Ok((participant, event))


def admin_add_participant(
    tournament_id: TournamentID,
    user_id: UserID,
    *,
    substitute_player: bool = False,
    team_id: TournamentTeamID | None = None,
    initiator: User | None = None,
) -> Result[tuple[TournamentParticipant, ParticipantJoinedEvent], str]:
    """Add a participant by admin (no ticket check)."""
    tournament = tournament_repository.get_tournament_for_update(tournament_id)

    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        return Err('Participants can only be added during registration.')

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

    participant = _create_or_reactivate_participant(
        tournament_id,
        user_id,
        substitute_player=substitute_player,
        team_id=team_id,
    )
    tournament_repository.commit_session()

    event = ParticipantJoinedEvent(
        occurred_at=participant.created_at,
        initiator=initiator,
        tournament_id=tournament_id,
        participant_id=participant.id,
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

    if participant.tournament_id != tournament_id:
        return Err('Participant does not belong to this tournament.')

    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        return Err(
            'You can only leave during the registration period. '
            'Contact an admin to be removed.'
        )

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


def _remove_single_participant_bracket_aware(
    tournament: Tournament,
    participant: TournamentParticipant,
    now: datetime,
) -> tuple[list[ContestantAdvancedEvent], TournamentTeamID | None]:
    """Remove one participant, handling bracket defwins if needed.

    Flushes but does NOT commit — caller owns the transaction.
    Returns (defwin_events, deleted_team_id).
    deleted_team_id is non-None only when removing this participant
    caused their team to become empty and the team was soft-deleted.
    """
    is_team_tournament = (
        tournament.contestant_type == ContestantType.TEAM
    )
    bracket_is_active = tournament.tournament_status in (
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
    )

    defwin_events: list[ContestantAdvancedEvent] = []
    deleted_team_id: TournamentTeamID | None = None

    # Step 1: remove the participant row (soft or hard).
    if bracket_is_active:
        tournament_repository.soft_delete_participants_by_ids(
            {participant.id}, now
        )
    else:
        tournament_repository.delete_participants_by_ids({participant.id})

    # Step 2: handle bracket consequences.
    if bracket_is_active and not is_team_tournament:
        defwin_events.extend(
            tournament_match_service.handle_defwin_for_removed_participant(
                tournament.id, participant.id
            )
        )
    elif (
        bracket_is_active
        and is_team_tournament
        and participant.team_id is not None
    ):
        # Only forfeit/delete the team if it is now empty.
        remaining = tournament_repository.get_participants_for_team(
            participant.team_id
        )
        if not remaining:
            defwin_events.extend(
                tournament_match_service.handle_defwin_for_removed_team(
                    tournament.id, participant.team_id
                )
            )
            tournament_repository.remove_team_from_participants_flush(
                participant.team_id
            )
            tournament_repository.soft_delete_team_flush(
                participant.team_id, now
            )
            deleted_team_id = participant.team_id

    return defwin_events, deleted_team_id


def admin_remove_participant(
    tournament_id: TournamentID,
    participant_id: TournamentParticipantID,
    *,
    initiator: User | None = None,
) -> Result[ParticipantLeftEvent, str]:
    """Remove a participant from a tournament (admin action).

    Bracket-aware: handles defwins when bracket is active.
    Emits TeamMemberLeftEvent and TeamDeletedEvent when applicable.
    """
    participant = tournament_repository.find_participant(participant_id)
    if participant is None:
        return Err('Participant not found.')

    if participant.tournament_id != tournament_id:
        return Err('Participant does not belong to this tournament.')

    team_id = participant.team_id  # capture before removal

    tournament = tournament_repository.get_tournament_for_update(tournament_id)

    now = datetime.now(UTC)
    defwin_events, deleted_team_id = _remove_single_participant_bracket_aware(
        tournament, participant, now
    )
    tournament_repository.commit_session()

    for event in defwin_events:
        signals.contestant_advanced.send(None, event=event)

    if team_id is not None:
        signals.team_member_left.send(
            None,
            event=TeamMemberLeftEvent(
                occurred_at=now,
                initiator=initiator,
                tournament_id=tournament_id,
                team_id=team_id,
                participant_id=participant_id,
            ),
        )

    if deleted_team_id is not None:
        signals.team_deleted.send(
            None,
            event=TeamDeletedEvent(
                occurred_at=now,
                initiator=initiator,
                tournament_id=tournament_id,
                team_id=deleted_team_id,
            ),
        )

    left_event = ParticipantLeftEvent(
        occurred_at=now,
        initiator=initiator,
        tournament_id=tournament_id,
        participant_id=participant_id,
    )
    signals.participant_left.send(None, event=left_event)

    return Ok(left_event)


def remove_participants_without_tickets(
    tournament_id: TournamentID,
    party_id: PartyID,
) -> Result[int, str]:
    """Remove all participants who don't have valid tickets.

    For team tournaments: transfers captain roles away from
    ticketless captains and cleans up teams left empty.
    """
    # Row-level lock to prevent concurrent modifications
    tournament_repository.lock_tournament_for_update(tournament_id)

    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.tournament_status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
    ):
        return Err('Cannot remove participants in this tournament status.')

    now = datetime.now(UTC)

    participants = tournament_repository.get_participants_for_tournament(
        tournament_id
    )
    if not participants:
        return Ok(0)

    participant_user_ids = {p.user_id for p in participants}
    users_with_tickets = ticket_service.select_ticket_users_for_party(
        participant_user_ids, party_id
    )
    ticketless = [
        p for p in participants if p.user_id not in users_with_tickets
    ]
    if not ticketless:
        return Ok(0)

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    # Team tournaments: transfer captains + identify empty teams
    teams_to_delete: list[TournamentTeamID] = []
    if is_team_tournament:
        teams_to_delete = _handle_team_captains(
            tournament_id, ticketless, participants
        )

    defwin_events: list[ContestantAdvancedEvent] = []

    if not is_team_tournament:
        # Solo: delegate per-participant bracket logic to helper
        for p in ticketless:
            p_defwin_events, _ = _remove_single_participant_bracket_aware(
                tournament, p, now
            )
            defwin_events.extend(p_defwin_events)
    else:
        # Team: bulk-remove participant rows, then clean up empty teams
        bracket_is_active = tournament.tournament_status in (
            TournamentStatus.ONGOING,
            TournamentStatus.PAUSED,
        )
        ids_to_remove = {p.id for p in ticketless}
        if bracket_is_active:
            # Soft-delete: preserve match contestant FKs
            tournament_repository.soft_delete_participants_by_ids(
                ids_to_remove, now
            )
        else:
            # Hard-delete: no match history to preserve
            tournament_repository.delete_participants_by_ids(ids_to_remove)

        # Clean up empty teams (defwin + soft/hard delete)
        for team_id in teams_to_delete:
            if bracket_is_active:
                defwin_events.extend(
                    tournament_match_service.handle_defwin_for_removed_team(
                        tournament_id, team_id
                    )
                )
            tournament_repository.remove_team_from_participants_flush(team_id)
            if bracket_is_active:
                tournament_repository.soft_delete_team_flush(team_id, now)
            else:
                tournament_repository.delete_team_flush(team_id)

    # Single atomic commit
    tournament_repository.commit_session()

    # Dispatch all events after commit.
    # `ticketless` holds frozen domain model instances whose
    # attributes remain valid after DB deletion, so iterating
    # them here is safe.

    for event in defwin_events:
        signals.contestant_advanced.send(None, event=event)

    for p in ticketless:
        if is_team_tournament and p.team_id is not None:
            signals.team_member_left.send(
                None,
                event=TeamMemberLeftEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    team_id=p.team_id,
                    participant_id=p.id,
                ),
            )
        signals.participant_left.send(
            None,
            event=ParticipantLeftEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
                participant_id=p.id,
            ),
        )

    for team_id in teams_to_delete:
        signals.team_deleted.send(
            None,
            event=TeamDeletedEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
                team_id=team_id,
            ),
        )

    return Ok(len(ticketless))


def _handle_team_captains(
    tournament_id: TournamentID,
    ticketless: list[TournamentParticipant],
    all_participants: list[TournamentParticipant],
) -> list[TournamentTeamID]:
    """Transfer captain role away from ticketless captains.

    Returns list of team IDs that will be empty after participant
    deletion (for subsequent cleanup by caller).
    Does NOT commit — caller handles the transaction.
    """
    ticketless_ids = {p.id for p in ticketless}
    teams_to_delete: list[TournamentTeamID] = []

    # Group ticketless participants by team
    teams_affected: dict[TournamentTeamID, list[TournamentParticipant]] = {}
    for p in ticketless:
        if p.team_id is not None:
            teams_affected.setdefault(p.team_id, []).append(p)

    if not teams_affected:
        return teams_to_delete

    # Build members-by-team from already-fetched participants
    members_by_team: dict[TournamentTeamID, list[TournamentParticipant]] = {}
    for p in all_participants:
        if p.team_id is not None:
            members_by_team.setdefault(p.team_id, []).append(p)

    # Single batch query for all affected teams
    teams_by_id = {
        t.id: t
        for t in tournament_repository.get_teams_by_ids(
            set(teams_affected.keys())
        )
    }

    for team_id, removed_members in teams_affected.items():
        team = teams_by_id.get(team_id)
        if team is None:
            continue  # Skip teams not found (data integrity edge case)
        all_members = members_by_team.get(team_id, [])
        remaining = sorted(
            (m for m in all_members if m.id not in ticketless_ids),
            key=lambda m: m.created_at,
        )

        if not remaining:
            # All members ticketless — team will be empty
            teams_to_delete.append(team_id)
            continue

        # Transfer captain if being removed
        captain_being_removed = any(
            p.user_id == team.captain_user_id for p in removed_members
        )
        if captain_being_removed:
            tournament_repository.update_team_captain(
                team_id, remaining[0].user_id
            )

    return teams_to_delete


def get_ticket_status_for_participants(
    tournament_id: TournamentID,
    party_id: PartyID,
    *,
    participants: list[TournamentParticipant] | None = None,
) -> tuple[set[UserID], list[TournamentParticipant]]:
    """Return (users_with_tickets, participants_without_tickets)."""
    if participants is None:
        participants = tournament_repository.get_participants_for_tournament(
            tournament_id
        )
    participant_user_ids = {p.user_id for p in participants}
    users_with_tickets = ticket_service.select_ticket_users_for_party(
        participant_user_ids, party_id
    )
    participants_without_tickets = [
        p for p in participants if p.user_id not in users_with_tickets
    ]
    return users_with_tickets, participants_without_tickets


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


def get_teams_below_minimum_size(
    tournament_id: TournamentID,
    *,
    tournament: Tournament | None = None,
) -> list[tuple[TournamentTeam, int]]:
    """Return teams whose member count is below
    min_players_in_team."""
    if tournament is None:
        tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.min_players_in_team is None:
        return []

    teams = tournament_repository.get_teams_for_tournament(tournament_id)
    if not teams:
        return []

    counts = tournament_repository.get_team_member_counts(tournament_id)
    result: list[tuple[TournamentTeam, int]] = []
    for team in teams:
        member_count = counts.get(team.id, 0)
        if member_count < tournament.min_players_in_team:
            result.append((team, member_count))
    return result


def get_seats_for_users(
    user_ids: set[UserID],
    party_id: PartyID,
) -> dict[UserID, str]:
    """Return seat labels for users at the given party.

    Issues a single batch query. Users without seats are omitted.
    """
    if not user_ids:
        return {}

    # Local imports to avoid circular dependency with seating/ticketing modules.
    from byceps.services.seating.dbmodels.seat import DbSeat
    from byceps.services.ticketing.dbmodels.ticket import DbTicket

    stmt = (
        select(DbTicket)
        .filter(DbTicket.party_id == party_id)
        .filter(DbTicket.used_by_id.in_(user_ids))
        .filter(DbTicket.revoked.is_(False))
        .filter(DbTicket.occupied_seat_id.is_not(None))
        .options(
            db.joinedload(DbTicket.occupied_seat).joinedload(DbSeat.area),
        )
    )
    tickets = db.session.scalars(stmt).all()

    result: dict[UserID, str] = {}
    for ticket in tickets:
        if ticket.used_by_id is None:
            continue
        if ticket.used_by_id in result:
            continue  # take first seat per user
        seat = ticket.occupied_seat
        if seat is None:
            continue
        label = seat.label or (seat.area.title if seat.area else None)
        if label is None:
            continue
        result[ticket.used_by_id] = label
    return result
