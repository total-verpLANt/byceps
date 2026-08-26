from collections.abc import Collection
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, UTC
from functools import wraps
from uuid import UUID

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_service
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_domain_service,
    tournament_log_service,
    tournament_match_service,
    tournament_repository,
)
from .events import (
    CaptainTransferredEvent,
    ParticipantJoinedEvent,
    ParticipantLeftEvent,
    TeamDeletedEvent,
    TeamMemberLeftEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament_match import MatchInvitationID, TournamentMatchID
from .models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament import Tournament, TournamentID
from .models.tournament_team import TournamentTeam, TournamentTeamID


def _rollback_roster_on_failure(operation):
    """Release roster locks and discard staged facts on Err or exception."""

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


def _as_uuids(ids: Collection) -> set[UUID]:
    """Normalise IDs that may be plain `str` when they come from a URL."""
    return {i if isinstance(i, UUID) else UUID(str(i)) for i in ids}


def _lock_roster_matches_flush(
    tournament_id: TournamentID,
    *,
    participant_ids: Collection[TournamentParticipantID] = (),
    team_ids: Collection[TournamentTeamID] = (),
    members_locked: bool = False,
) -> list[TournamentMatchID]:
    """Lock teams, membership, then ordered matches under the tournament lock.

    Lock the existing graph before a removal can advance an opponent into a
    downstream match. Refresh only the affected contestants' existing matches.
    The caller sets `members_locked` after it locked every participant of
    the tournament in one statement. No commit, signals or queue operations
    occur here.
    """
    named_participants = {
        TournamentParticipantID(i) for i in _as_uuids(participant_ids)
    }
    named_teams = {TournamentTeamID(i) for i in _as_uuids(team_ids)}
    members = set(named_participants)
    for team_id in sorted(named_teams):
        tournament_repository.get_team_for_update(team_id)
        members.update(
            p.id for p in tournament_repository.get_participants_for_team(team_id)
        )
    if not members_locked:
        tournament_repository.get_participants_for_update(sorted(members))
    # Include confirmed assignments too: explicit team deletion historically
    # removes those rows as well, while ordinary roster changes preserve them.
    assignments = tournament_repository.get_contestants_for_tournament(
        tournament_id
    )
    affected = {
        match_id
        for match_id, contestants in assignments.items()
        for c in contestants
        if c.participant_id in named_participants or c.team_id in named_teams
    }
    matches = tournament_repository.get_matches_for_tournament(
        tournament_id
    )
    tournament_repository.lock_matches_for_update(
        sorted(match.id for match in matches)
    )
    return sorted(affected)


def _refresh_roster_matches_flush(
    match_ids: Collection[TournamentMatchID], *, occurred_at: datetime,
) -> Result[tuple[MatchInvitationID, ...], str]:
    """Refresh pairing/audience and collect durable IDs, without effects."""
    from . import tournament_readiness_service

    pending: set[MatchInvitationID] = set()
    for match_id in sorted(set(match_ids)):
        refreshed = tournament_readiness_service.refresh_pairing_and_invitations_flush(
            match_id, occurred_at=occurred_at
        )
        if refreshed.is_err():
            return Err(refreshed.unwrap_err())
        pending.update(refreshed.unwrap().pending_invitation_ids)
    return Ok(tuple(sorted(pending, key=str)))


@contextmanager
def _dispatch_roster_invitations_after_signals(
    invitation_ids: tuple[MatchInvitationID, ...],
):
    """Post-commit boundary: dispatch even when an existing listener fails."""
    from . import tournament_readiness_service

    try:
        yield
    finally:
        tournament_readiness_service.dispatch_pending_invitations(invitation_ids)


def _lock_optional_team_flush(
    tournament_id: TournamentID, team_id: TournamentTeamID | None,
) -> Result[list[TournamentMatchID], str]:
    if team_id is None:
        return Ok([])
    team = tournament_repository.find_team(team_id)
    if team is None or team.tournament_id != tournament_id:
        return Err('Team does not belong to this tournament.')
    team = tournament_repository.get_team_for_update(team_id)
    if team.removed_at is not None:
        return Err('Team does not belong to this tournament.')
    return Ok(_lock_roster_matches_flush(tournament_id, team_ids=[team_id]))


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


@_rollback_roster_on_failure
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

    locked = _lock_optional_team_flush(tournament_id, team_id)
    if locked.is_err():
        return Err(locked.unwrap_err())
    participant = _create_or_reactivate_participant(
        tournament_id,
        user_id,
        substitute_player=substitute_player,
        team_id=team_id,
    )
    refreshed = _refresh_roster_matches_flush(
        locked.unwrap(), occurred_at=participant.created_at
    )
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()

    event = ParticipantJoinedEvent(
        occurred_at=participant.created_at,
        initiator=None,
        tournament_id=tournament_id,
        participant_id=participant.id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        signals.participant_joined.send(None, event=event)

    return Ok((participant, event))


@_rollback_roster_on_failure
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

    locked = _lock_optional_team_flush(tournament_id, team_id)
    if locked.is_err():
        return Err(locked.unwrap_err())
    participant = _create_or_reactivate_participant(
        tournament_id,
        user_id,
        substitute_player=substitute_player,
        team_id=team_id,
    )
    refreshed = _refresh_roster_matches_flush(
        locked.unwrap(), occurred_at=participant.created_at
    )
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    tournament_repository.commit_session()

    event = ParticipantJoinedEvent(
        occurred_at=participant.created_at,
        initiator=initiator,
        tournament_id=tournament_id,
        participant_id=participant.id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        signals.participant_joined.send(None, event=event)

    return Ok((participant, event))


def _stage_participant_log_entry(
    event_type: str,
    tournament_id: TournamentID,
    participant: TournamentParticipant,
    initiator_id: UserID | None,
    *,
    roster_before: int,
    roster_after: int,
) -> None:
    """Stage an audit entry for a departed participant (no commit).

    The roster is the number of active participants.
    """
    tournament_log_service.create_log_entry(
        event_type,
        tournament_id,
        initiator_id,
        data={
            'participant_id': str(participant.id),
            'user_id': str(participant.user_id),
            'team_id': (
                str(participant.team_id)
                if participant.team_id is not None
                else None
            ),
            'roster_before': roster_before,
            'roster_after': roster_after,
        },
        commit=False,
    )


@_rollback_roster_on_failure
def leave_tournament(
    tournament_id: TournamentID,
    participant_id: TournamentParticipantID,
) -> Result[ParticipantLeftEvent, str]:
    """Remove a participant from a tournament."""
    tournament_repository.lock_tournament_for_update(tournament_id)

    participant = tournament_repository.find_participant_fresh(participant_id)
    if participant is None:
        tournament_repository.rollback_session()
        return Err('Participant not found.')

    if participant.tournament_id != tournament_id:
        tournament_repository.rollback_session()
        return Err('Participant does not belong to this tournament.')

    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)
    if tournament.tournament_status != TournamentStatus.REGISTRATION_OPEN:
        tournament_repository.rollback_session()
        return Err(
            'You can only leave during the registration period. '
            'Contact an admin to be removed.'
        )

    try:
        affected = _lock_roster_matches_flush(
            tournament_id, participant_ids=[participant_id],
            team_ids=[participant.team_id] if participant.team_id else [],
        )
        if participant.team_id is not None:
            team = tournament_repository.get_team(participant.team_id)
            others = [
                m
                for m in tournament_repository.get_participants_for_team(
                    team.id
                )
                if m.id != participant_id
            ]
            if team.captain_user_id == participant.user_id and others:
                return Err(
                    'Team captain cannot leave while team has other members. '
                    'Transfer captain role first or have other members leave.'
                )
        roster_before = tournament_repository.get_participant_count(
            tournament_id
        )
        _delete_unplayed_entries(tournament_id, participant_ids=[participant_id])
        tournament_repository.clear_winner_participant_reference_flush(participant_id)
        tournament_repository.delete_participants_by_ids({participant_id})
        refreshed = _refresh_roster_matches_flush(affected, occurred_at=datetime.now(UTC))
        if refreshed.is_err():
            return Err(refreshed.unwrap_err())
        pending = refreshed.unwrap()
        roster_after = tournament_repository.get_participant_count(
            tournament_id
        )
        _stage_participant_log_entry(
            'participant-left',
            tournament_id,
            participant,
            participant.user_id,
            roster_before=roster_before,
            roster_after=roster_after,
        )
        tournament_repository.commit_session()
    except BaseException:
        tournament_repository.rollback_session()
        raise

    now = datetime.now(UTC)
    event = ParticipantLeftEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
        participant_id=participant_id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        signals.participant_left.send(None, event=event)

    return Ok(event)


def _delete_unplayed_entries(
    tournament_id: TournamentID,
    *,
    participant_ids: Collection[TournamentParticipantID] = (),
    team_ids: Collection[TournamentTeamID] = (),
) -> None:
    """Delete these contestants' entries from the unconfirmed matches.

    A generated layout that was not started holds entries that reference
    the contestants; the rows cannot go while they exist. The tournament
    row must be locked by the caller, the matches are locked here in ID
    order. Nothing is decided: the roster then differs from the layout.
    """
    by_participant: list[tuple[TournamentMatchID, TournamentParticipantID]] = []
    for participant_id in participant_ids:
        found = tournament_repository.find_contestant_entries_for_participant_in_tournament(
            tournament_id, participant_id
        )
        by_participant.extend(
            (match.id, participant_id) for _contestant, match in found
        )
    by_team: list[tuple[TournamentMatchID, TournamentTeamID]] = []
    for team_id in team_ids:
        found = tournament_repository.find_contestant_entries_for_team_in_tournament(
            tournament_id, team_id
        )
        by_team.extend((match.id, team_id) for _contestant, match in found)

    match_ids: set[TournamentMatchID] = {m for m, _ in by_participant}
    match_ids.update(m for m, _ in by_team)
    tournament_repository.lock_matches_for_update(sorted(match_ids))

    for match_id, participant_id in by_participant:
        tournament_match_service._delete_contestant_from_match_flush(
            match_id, participant_id=participant_id
        )
    for match_id, team_id in by_team:
        tournament_match_service._delete_contestant_from_match_flush(
            match_id, team_id=team_id
        )


def _remove_single_participant_bracket_aware(
    tournament: Tournament,
    participant: TournamentParticipant,
    now: datetime,
    *,
    initiator_id: UserID | None = None,
    soft_deleted: bool = False,
) -> tuple[tournament_match_service.DefwinResult, TournamentTeamID | None]:
    """Remove one participant, handling bracket defwins if needed.

    Flushes but does NOT commit — caller owns the transaction.
    Returns (defwin_result, deleted_team_id).
    deleted_team_id is non-None only when removing this participant
    caused their team to become empty and the team was soft-deleted.
    """
    is_team_tournament = tournament.contestant_type == ContestantType.TEAM
    bracket_is_active = tournament.tournament_status in (
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
    )

    defwin = tournament_match_service.DefwinResult([], [], [])
    deleted_team_id: TournamentTeamID | None = None

    # Step 1: remove the participant row (soft or hard).
    if bracket_is_active:
        if not soft_deleted:
            tournament_repository.soft_delete_participants_by_ids(
                {participant.id}, now
            )
    else:
        _delete_unplayed_entries(
            tournament.id, participant_ids=[participant.id]
        )
        tournament_repository.clear_winner_participant_reference_flush(participant.id)
        tournament_repository.delete_participants_by_ids({participant.id})

    # Step 2: handle bracket consequences.
    if bracket_is_active and not is_team_tournament:
        result = tournament_match_service.handle_defwin_for_removed_participant(
            tournament.id, participant.id,
            initiator_id=initiator_id,
        )
        defwin.advanced.extend(result.advanced)
        defwin.confirmed.extend(result.confirmed)
        defwin.completed.extend(result.completed)
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
            result = tournament_match_service.handle_defwin_for_removed_team(
                tournament.id, participant.team_id,
                initiator_id=initiator_id,
            )
            defwin.advanced.extend(result.advanced)
            defwin.confirmed.extend(result.confirmed)
            defwin.completed.extend(result.completed)
            tournament_repository.remove_team_from_participants_flush(
                participant.team_id
            )
            tournament_repository.soft_delete_team_flush(
                participant.team_id, now
            )
            deleted_team_id = participant.team_id

    return defwin, deleted_team_id


def _try_auto_release_after_defwin(
    tournament_id: TournamentID,
    defwin: tournament_match_service.DefwinResult,
    initiator_id: UserID | None,
) -> None:
    """Release the playoffs if the committed removal made that due.

    Call it after the commit. A defwin that confirmed a match can settle
    the last group match, and a removal alone can dissolve a blocking
    tie; nothing else would stage the draft or release then. Without an
    initiator there is nobody to confirm the byes, so nothing happens.
    """
    if initiator_id is None:
        return

    tournament = tournament_repository.get_tournament(tournament_id)
    if not tournament.has_playoffs:
        return

    from . import tournament_qualification_service

    tournament_qualification_service.auto_release_after_commit(
        tournament_id, triggered_by=initiator_id
    )


@_rollback_roster_on_failure
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
    tournament = tournament_repository.get_tournament_for_update(tournament_id)
    participant = tournament_repository.find_participant_fresh(participant_id)
    if participant is None:
        return Err('Participant not found.')

    if participant.tournament_id != tournament_id:
        return Err('Participant does not belong to this tournament.')

    participant_id = participant.id
    team_id = participant.team_id  # capture before removal

    affected = _lock_roster_matches_flush(
        tournament_id, participant_ids=[participant_id],
        team_ids=[team_id] if team_id else [],
    )
    handovers: list[_CaptaincyHandover] = []
    if team_id is not None:
        # The members were locked above. A removed captain must not keep the
        # role: the team could not claim Ready and the ex-captain would keep
        # their kick rights.
        _emptied, handover = _hand_over_captaincy_flush(
            tournament_repository.get_team(team_id),
            [participant],
            tournament_repository.get_participants_for_team(team_id),
        )
        if handover is not None:
            handovers.append(handover)

    now = datetime.now(UTC)
    roster_before = tournament_repository.get_participant_count(tournament_id)
    defwin, deleted_team_id = _remove_single_participant_bracket_aware(
        tournament, participant, now,
        initiator_id=initiator.id if initiator is not None else None,
    )
    affected.extend(event.match_id for event in defwin.advanced)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=now)
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()
    _stage_participant_log_entry(
        'participant-removed',
        tournament_id,
        participant,
        initiator.id if initiator is not None else None,
        roster_before=roster_before,
        roster_after=tournament_repository.get_participant_count(tournament_id),
    )
    tournament_repository.commit_session()

    left_event = ParticipantLeftEvent(
        occurred_at=now,
        initiator=initiator,
        tournament_id=tournament_id,
        participant_id=participant_id,
    )
    with _dispatch_roster_invitations_after_signals(pending):
        for event in defwin.advanced:
            signals.contestant_advanced.send(None, event=event)
        for event in defwin.confirmed:
            signals.match_confirmed.send(None, event=event)
        for event in defwin.completed:
            signals.tournament_completed.send(None, event=event)

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

        _send_captain_transferred(
            handovers, tournament_id, occurred_at=now, initiator=initiator
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

        signals.participant_left.send(None, event=left_event)
        _try_auto_release_after_defwin(
            tournament_id, defwin, initiator.id if initiator is not None else None,
        )

    return Ok(left_event)


@_rollback_roster_on_failure
def remove_participants_without_tickets(
    tournament_id: TournamentID,
    party_id: PartyID,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Remove all participants who don't have valid tickets.

    For team tournaments: transfers captain roles away from
    ticketless captains and cleans up teams left empty.
    """
    # Row-level lock to prevent concurrent modifications
    tournament_repository.lock_tournament_for_update(tournament_id)

    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)
    if tournament.party_id != party_id:
        return Err('Party does not belong to this tournament.')
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
        tournament_repository.rollback_session()
        return Ok(0)

    # Reload cached membership under team/member locks before deciding who is
    # removed or who inherits captain authority.
    for team_id in sorted({p.team_id for p in participants if p.team_id is not None}):
        tournament_repository.get_team_for_update(team_id)
    participants = tournament_repository.get_participants_for_update(
        [p.id for p in participants]
    )

    participant_user_ids = {p.user_id for p in participants}
    users_with_tickets = ticket_service.select_ticket_users_for_party(
        participant_user_ids, party_id
    )
    ticketless = [
        p for p in participants if p.user_id not in users_with_tickets
    ]
    if not ticketless:
        tournament_repository.rollback_session()
        return Ok(0)

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    affected = _lock_roster_matches_flush(
        tournament_id, participant_ids=[p.id for p in ticketless],
        team_ids={p.team_id for p in ticketless if p.team_id is not None},
        members_locked=True,
    )

    # Team tournaments: transfer captains + identify empty teams
    teams_to_delete: list[TournamentTeamID] = []
    handovers: list[_CaptaincyHandover] = []
    if is_team_tournament:
        teams_to_delete, handovers = _handle_team_captains(
            tournament_id, ticketless, participants
        )

    defwin = tournament_match_service.DefwinResult([], [], [])

    bracket_is_active = tournament.tournament_status in (
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
    )
    if not is_team_tournament:
        if bracket_is_active:
            # Remove the whole pass before any defwin: a completion one
            # triggers must not crown a contestant this pass removes.
            tournament_repository.soft_delete_participants_by_ids(
                {p.id for p in ticketless}, now
            )
        # Solo: delegate per-participant bracket logic to helper
        for p in ticketless:
            p_defwin, _ = _remove_single_participant_bracket_aware(
                tournament, p, now,
                initiator_id=initiator_id,
                soft_deleted=bracket_is_active,
            )
            defwin.advanced.extend(p_defwin.advanced)
            defwin.confirmed.extend(p_defwin.confirmed)
            defwin.completed.extend(p_defwin.completed)
    else:
        # Team: bulk-remove participant rows, then clean up empty teams
        ids_to_remove = {p.id for p in ticketless}
        if bracket_is_active:
            # Soft-delete: preserve match contestant FKs
            tournament_repository.soft_delete_participants_by_ids(
                ids_to_remove, now
            )
            for team_id in teams_to_delete:
                tournament_repository.soft_delete_team_flush(team_id, now)
        else:
            # Hard-delete: only unplayed entries of a generated layout
            _delete_unplayed_entries(
                tournament_id,
                participant_ids=sorted(ids_to_remove),
                team_ids=teams_to_delete,
            )
            tournament_repository.delete_participants_by_ids(ids_to_remove)

        # Clean up empty teams (defwin + soft/hard delete)
        for team_id in teams_to_delete:
            if bracket_is_active:
                result = tournament_match_service.handle_defwin_for_removed_team(
                    tournament_id, team_id,
                    initiator_id=initiator_id,
                )
                defwin.advanced.extend(result.advanced)
                defwin.confirmed.extend(result.confirmed)
                defwin.completed.extend(result.completed)
            tournament_repository.remove_team_from_participants_flush(team_id)
            if not bracket_is_active:
                tournament_repository.delete_team_flush(team_id)

    affected.extend(event.match_id for event in defwin.advanced)
    refreshed = _refresh_roster_matches_flush(affected, occurred_at=now)
    if refreshed.is_err():
        return Err(refreshed.unwrap_err())
    pending = refreshed.unwrap()

    # One entry per removal, counted as if removed one by one.
    roster = len(participants)
    for p in ticketless:
        _stage_participant_log_entry(
            'participant-removed',
            tournament_id,
            p,
            initiator_id,
            roster_before=roster,
            roster_after=roster - 1,
        )
        roster -= 1

    # Single atomic commit
    tournament_repository.commit_session()

    # Dispatch all events after commit.
    # `ticketless` holds frozen domain model instances whose
    # attributes remain valid after DB deletion, so iterating
    # them here is safe.

    with _dispatch_roster_invitations_after_signals(pending):
        for event in defwin.advanced:
            signals.contestant_advanced.send(None, event=event)
        for event in defwin.confirmed:
            signals.match_confirmed.send(None, event=event)
        for event in defwin.completed:
            signals.tournament_completed.send(None, event=event)

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

        _send_captain_transferred(
            handovers, tournament_id, occurred_at=now, initiator=None
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

        _try_auto_release_after_defwin(tournament_id, defwin, initiator_id)

    return Ok(len(ticketless))


@dataclass(frozen=True)
class _CaptaincyHandover:
    team_id: TournamentTeamID
    old_captain_user_id: UserID
    new_captain_user_id: UserID


def _hand_over_captaincy_flush(
    team: TournamentTeam,
    removed: Collection[TournamentParticipant],
    members: Collection[TournamentParticipant],
) -> tuple[bool, _CaptaincyHandover | None]:
    """Give a leaving captain's role to the longest-standing remaining member.

    `members` are the team's active members before the removal. Return
    whether nobody remains, so the caller can clean up the team, and the
    handover that happened, if any. Flushes only; the caller owns the commit
    and announces the handover after it.
    """
    removed_ids = {p.id for p in removed}
    remaining = sorted(
        (m for m in members if m.id not in removed_ids),
        key=lambda m: m.created_at,
    )
    if not remaining:
        return True, None

    if any(p.user_id == team.captain_user_id for p in removed):
        new_captain_user_id = remaining[0].user_id
        tournament_repository.update_team_captain_flush(
            team.id, new_captain_user_id
        )
        return False, _CaptaincyHandover(
            team.id, team.captain_user_id, new_captain_user_id
        )
    return False, None


def _send_captain_transferred(
    handovers: Collection[_CaptaincyHandover],
    tournament_id: TournamentID,
    *,
    occurred_at: datetime,
    initiator: User | None,
) -> None:
    """Announce the handovers; call it after the commit only."""
    for handover in handovers:
        signals.captain_transferred.send(
            None,
            event=CaptainTransferredEvent(
                occurred_at=occurred_at,
                initiator=initiator,
                tournament_id=tournament_id,
                team_id=handover.team_id,
                old_captain_user_id=handover.old_captain_user_id,
                new_captain_user_id=handover.new_captain_user_id,
            ),
        )


def _handle_team_captains(
    tournament_id: TournamentID,
    ticketless: list[TournamentParticipant],
    all_participants: list[TournamentParticipant],
) -> tuple[list[TournamentTeamID], list[_CaptaincyHandover]]:
    """Transfer captain role away from ticketless captains.

    Returns the team IDs that will be empty after participant deletion
    (for subsequent cleanup by caller) and the handovers to announce
    after the commit.
    Does NOT commit — caller handles the transaction.
    """
    teams_to_delete: list[TournamentTeamID] = []
    handovers: list[_CaptaincyHandover] = []

    # Group ticketless participants by team
    teams_affected: dict[TournamentTeamID, list[TournamentParticipant]] = {}
    for p in ticketless:
        if p.team_id is not None:
            teams_affected.setdefault(p.team_id, []).append(p)

    if not teams_affected:
        return teams_to_delete, handovers

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
        emptied, handover = _hand_over_captaincy_flush(
            team, removed_members, members_by_team.get(team_id, [])
        )
        if emptied:
            # All members ticketless — team will be empty
            teams_to_delete.append(team_id)
        if handover is not None:
            handovers.append(handover)

    return teams_to_delete, handovers


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


def find_participant(
    participant_id: TournamentParticipantID,
) -> TournamentParticipant | None:
    """Return the participant, or `None` if not found."""
    return tournament_repository.find_participant(participant_id)


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
