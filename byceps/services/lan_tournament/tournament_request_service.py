"""
byceps.services.lan_tournament.tournament_request_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from datetime import datetime, UTC
from enum import Enum
import logging
from typing import cast

from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user import user_service
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_log_service,
    tournament_orga_service,
    tournament_request_domain_service,
    tournament_request_repository,
)
from .db_error_helpers import extract_constraint_name
from .events import (
    TournamentRequestAcceptedEvent,
    TournamentRequestEditedEvent,
    TournamentRequestRejectedEvent,
    TournamentRequestSubmittedEvent,
    TournamentRequestWithdrawnEvent,
)
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.tournament import TournamentID
from .models.tournament_log_entry import TournamentLogEntry
from .models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)


logger = logging.getLogger(__name__)


# Every audit entry this module writes -- including the closing entry
# in `link_created_tournament_flush` -- is filed under
# `TournamentID(request_id)`,
# never the real tournament's ID once one exists. Migration 013 dropped
# the log entries' FK to `lan_tournaments` precisely so an entry need
# not reference a live tournament row, and `get_request_history`
# depends on every entry for a request sharing that one ID: filing the
# closing entry under the real tournament instead would make it
# invisible there, ending the proposer's timeline at "accepted".

# Cap on how many requests one proposer may keep in `submitted` at
# once, per party.
MAX_OPEN_REQUESTS_PER_PROPOSER = 3

# The statuses each transition may start from, independent of whatever
# `expected_status` a caller passes. `expected_status` may only
# NARROW this set (pin the transition to one specific status within
# it) -- it must never widen it, or a caller could revive a rejected
# request, re-reject a withdrawn one, or re-link an already-rejected
# request just by passing the "wrong" status. See each function's own
# `status in _..._ALLOWED_STATUSES and (expected_status is None or
# status is expected_status)` check.
_UPDATE_ALLOWED_STATUSES = frozenset({TournamentRequestStatus.submitted})
# Admins may also edit `accepted` requests.
_ADMIN_UPDATE_ALLOWED_STATUSES = frozenset(
    {
        TournamentRequestStatus.submitted,
        TournamentRequestStatus.accepted,
    }
)
_WITHDRAW_ALLOWED_STATUSES = frozenset({TournamentRequestStatus.submitted})
_ACCEPT_ALLOWED_STATUSES = frozenset({TournamentRequestStatus.submitted})
_REJECT_ALLOWED_STATUSES = frozenset(
    {
        TournamentRequestStatus.submitted,
        TournamentRequestStatus.accepted,
    }
)
_LINK_ALLOWED_STATUSES = frozenset({TournamentRequestStatus.accepted})

_UNIQUE_NUMBER_CONSTRAINT_NAME = 'uq_lan_tournament_requests_party_number'

# The fields `update_request` may change; also what its audit entry's
# `changed_fields` is computed against.
_EDITABLE_FIELDS = [
    'name',
    'game',
    'game_format',
    'elimination_mode',
    'team_size',
    'participant_limit',
    'preferred_start_time',
    'preferred_end_time',
    'description',
    'special_rules',
    'notes',
    'desired_template',
]


def submit_request(
    party_id: PartyID,
    proposer_id: UserID,
    *,
    party_capacity: int | None,
    name: str,
    game: str,
    game_format: GameFormat,
    elimination_mode: EliminationMode,
    team_size: int,
    participant_limit: int,
    preferred_start_time: datetime,
    preferred_end_time: datetime,
    description: str,
    special_rules: str | None = None,
    notes: str | None = None,
    desired_template: str | None = None,
) -> Result[tuple[TournamentRequest, TournamentRequestSubmittedEvent], str]:
    """Submit a new tournament request in `submitted` status."""
    preferred_start_time = (
        tournament_request_domain_service.normalize_datetime_to_utc(
            preferred_start_time
        )
    )
    preferred_end_time = (
        tournament_request_domain_service.normalize_datetime_to_utc(
            preferred_end_time
        )
    )

    validation_result = (
        tournament_request_domain_service.validate_request_fields(
            name=name,
            game=game,
            team_size=team_size,
            participant_limit=participant_limit,
            party_capacity=party_capacity,
            preferred_start_time=preferred_start_time,
            preferred_end_time=preferred_end_time,
            description=description,
            game_format=game_format,
            elimination_mode=elimination_mode,
            special_rules=special_rules,
            notes=notes,
            desired_template=desired_template,
        )
    )
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    open_count = tournament_request_repository.count_open_requests_for_proposer(
        party_id, proposer_id
    )
    if open_count >= MAX_OPEN_REQUESTS_PER_PROPOSER:
        return Err('Too many open tournament requests.')

    special_rules = tournament_request_domain_service.normalize_optional_text(
        special_rules
    )
    notes = tournament_request_domain_service.normalize_optional_text(notes)
    desired_template = (
        tournament_request_domain_service.normalize_optional_text(
            desired_template
        )
    )

    now = datetime.now(UTC)
    request_id = TournamentRequestID(generate_uuid7())

    # `get_next_number_for_party` + insert is a read-then-write race
    # against a concurrent submit for the same party: two callers can
    # read the same MAX(number) before either commits. The UNIQUE
    # constraint on (party_id, number) catches that; on a collision,
    # re-read the number and retry exactly once.
    for _attempt in range(2):
        number = tournament_request_repository.get_next_number_for_party(
            party_id
        )

        candidate = TournamentRequest(
            id=request_id,
            party_id=party_id,
            number=number,
            proposer_id=proposer_id,
            created_at=now,
            status=TournamentRequestStatus.submitted,
            name=name,
            game=game,
            game_format=game_format,
            elimination_mode=elimination_mode,
            team_size=team_size,
            participant_limit=participant_limit,
            preferred_start_time=preferred_start_time,
            preferred_end_time=preferred_end_time,
            description=description,
            special_rules=special_rules,
            notes=notes,
            desired_template=desired_template,
        )

        try:
            tournament_request_repository.create_request(candidate)

            tournament_log_service.create_log_entry(
                'tournament-request-submitted',
                TournamentID(request_id),
                proposer_id,
                data={'number': number},
                commit=False,
            )

            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()

            if extract_constraint_name(e) == _UNIQUE_NUMBER_CONSTRAINT_NAME:
                continue
            raise
        else:
            event = TournamentRequestSubmittedEvent(
                occurred_at=now,
                initiator=None,
                request_id=candidate.id,
                party_id=candidate.party_id,
                proposer_id=candidate.proposer_id,
            )
            signals.tournament_request_submitted.send(None, event=event)

            return Ok((candidate, event))

    return Err('Could not allocate a request number.')


def _display_value(value: object) -> str | int | None:
    """Return `value` in a JSON-safe, log-friendly display form."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.name
    return cast('str | int', value)


def update_request(
    request_id: TournamentRequestID,
    editor_id: UserID,
    *,
    expected_status: TournamentRequestStatus | None = None,
    by: str = 'proposer',
    party_capacity: int | None,
    name: str,
    game: str,
    game_format: GameFormat,
    elimination_mode: EliminationMode,
    team_size: int,
    participant_limit: int,
    preferred_start_time: datetime,
    preferred_end_time: datetime,
    description: str,
    special_rules: str | None = None,
    notes: str | None = None,
    desired_template: str | None = None,
) -> Result[tuple[TournamentRequest, TournamentRequestEditedEvent], str]:
    """Edit an open tournament request's fields, re-validating them."""
    request = tournament_request_repository.get_request_for_update(request_id)

    allowed_statuses = (
        _ADMIN_UPDATE_ALLOWED_STATUSES
        if by == 'admin'
        else _UPDATE_ALLOWED_STATUSES
    )
    if not (
        request.status in allowed_statuses
        and (expected_status is None or request.status is expected_status)
    ):
        return Err('Request is no longer in the expected state.')

    if by != 'admin' and request.proposer_id != editor_id:
        return Err('Only the proposer may edit this request.')

    preferred_start_time = (
        tournament_request_domain_service.normalize_datetime_to_utc(
            preferred_start_time
        )
    )
    preferred_end_time = (
        tournament_request_domain_service.normalize_datetime_to_utc(
            preferred_end_time
        )
    )

    validation_result = (
        tournament_request_domain_service.validate_request_fields(
            name=name,
            game=game,
            team_size=team_size,
            participant_limit=participant_limit,
            party_capacity=party_capacity,
            preferred_start_time=preferred_start_time,
            preferred_end_time=preferred_end_time,
            description=description,
            game_format=game_format,
            elimination_mode=elimination_mode,
            special_rules=special_rules,
            notes=notes,
            desired_template=desired_template,
        )
    )
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    special_rules = tournament_request_domain_service.normalize_optional_text(
        special_rules
    )
    notes = tournament_request_domain_service.normalize_optional_text(notes)
    desired_template = (
        tournament_request_domain_service.normalize_optional_text(
            desired_template
        )
    )

    new_values = {
        'name': name,
        'game': game,
        'game_format': game_format,
        'elimination_mode': elimination_mode,
        'team_size': team_size,
        'participant_limit': participant_limit,
        'preferred_start_time': preferred_start_time,
        'preferred_end_time': preferred_end_time,
        'description': description,
        'special_rules': special_rules,
        'notes': notes,
        'desired_template': desired_template,
    }
    changed_fields = [
        field
        for field in _EDITABLE_FIELDS
        if getattr(request, field) != new_values[field]
    ]
    previous_values = {
        field: _display_value(getattr(request, field))
        for field in changed_fields
    }
    log_new_values = {
        field: _display_value(new_values[field])
        for field in changed_fields
        if field in ('team_size', 'participant_limit')
    }

    now = datetime.now(UTC)
    updated = dataclasses.replace(
        request,
        updated_at=now,
        name=name,
        game=game,
        game_format=game_format,
        elimination_mode=elimination_mode,
        team_size=team_size,
        participant_limit=participant_limit,
        preferred_start_time=preferred_start_time,
        preferred_end_time=preferred_end_time,
        description=description,
        special_rules=special_rules,
        notes=notes,
        desired_template=desired_template,
    )

    tournament_request_repository.update_request_flush(updated)

    tournament_log_service.create_log_entry(
        'tournament-request-edited',
        TournamentID(updated.id),
        editor_id,
        data={
            'changed_fields': changed_fields,
            'by': by,
            'previous_values': previous_values,
            'new_values': log_new_values,
        },
        commit=False,
    )

    db.session.commit()

    event = TournamentRequestEditedEvent(
        occurred_at=now,
        initiator=None,
        request_id=updated.id,
        party_id=updated.party_id,
        proposer_id=updated.proposer_id,
    )
    signals.tournament_request_edited.send(None, event=event)

    return Ok((updated, event))


def withdraw_request(
    request_id: TournamentRequestID,
    proposer_id: UserID,
    *,
    expected_status: TournamentRequestStatus | None = None,
) -> Result[TournamentRequest, str]:
    """Withdraw an open tournament request. Proposer-only."""
    request = tournament_request_repository.get_request_for_update(request_id)

    if request.proposer_id != proposer_id:
        return Err('Only the proposer may withdraw this request.')

    if not (
        request.status in _WITHDRAW_ALLOWED_STATUSES
        and (expected_status is None or request.status is expected_status)
    ):
        return Err('Request is no longer in the expected state.')

    now = datetime.now(UTC)
    updated = dataclasses.replace(
        request,
        status=TournamentRequestStatus.withdrawn,
        updated_at=now,
    )

    tournament_request_repository.update_request_flush(updated)

    tournament_log_service.create_log_entry(
        'tournament-request-withdrawn',
        TournamentID(updated.id),
        proposer_id,
        commit=False,
    )

    db.session.commit()

    event = TournamentRequestWithdrawnEvent(
        occurred_at=now,
        initiator=None,
        request_id=updated.id,
        party_id=updated.party_id,
        proposer_id=updated.proposer_id,
    )
    signals.tournament_request_withdrawn.send(None, event=event)

    return Ok(updated)


def accept_request(
    request_id: TournamentRequestID,
    decider_id: UserID,
    *,
    expected_status: TournamentRequestStatus | None = None,
) -> Result[tuple[TournamentRequest, TournamentRequestAcceptedEvent], str]:
    """Accept a tournament request.

    Creates NO tournament. Sets the request to `accepted`, a real,
    visible waiting state -- freezing its editability -- and stops
    there. The admin later creates the tournament through the normal
    `create_form` path; `tournament_service.create_tournament` calls
    `link_created_tournament_flush` from inside that same transaction
    to close the loop, and the view calls `appoint_proposer_orga`
    once it has committed.
    """
    request = tournament_request_repository.get_request_for_update(request_id)

    if not (
        request.status in _ACCEPT_ALLOWED_STATUSES
        and (expected_status is None or request.status is expected_status)
    ):
        return Err('Request is no longer in the expected state.')

    now = datetime.now(UTC)
    updated = dataclasses.replace(
        request,
        status=TournamentRequestStatus.accepted,
        decided_at=now,
        decided_by_id=decider_id,
        updated_at=now,
    )

    tournament_request_repository.update_request_flush(updated)

    tournament_log_service.create_log_entry(
        'tournament-request-accepted',
        TournamentID(updated.id),
        decider_id,
        commit=False,
    )

    db.session.commit()

    event = TournamentRequestAcceptedEvent(
        occurred_at=now,
        initiator=None,
        request_id=updated.id,
        party_id=updated.party_id,
        proposer_id=updated.proposer_id,
        decided_by_id=decider_id,
    )
    signals.tournament_request_accepted.send(None, event=event)

    return Ok((updated, event))


def reject_request(
    request_id: TournamentRequestID,
    decider_id: UserID,
    reason: str,
    *,
    expected_status: TournamentRequestStatus | None = None,
) -> Result[TournamentRequest, str]:
    """Reject a tournament request from `submitted` or `accepted`.

    Accepting `accepted` as a prior state too is a deliberate escape
    hatch: without it, a request already accepted but not yet turned
    into a tournament could never be closed out.
    """
    if not reason.strip():
        return Err('A reason is required to reject a request.')

    if tournament_request_domain_service.contains_disallowed_control_char(
        reason
    ):
        return Err('The reason must not contain control characters.')

    if (
        len(reason)
        > tournament_request_domain_service.MAX_REJECTION_REASON_LENGTH
    ):
        return Err('The reason must not exceed 2000 characters.')

    request = tournament_request_repository.get_request_for_update(request_id)

    if not (
        request.status in _REJECT_ALLOWED_STATUSES
        and (expected_status is None or request.status is expected_status)
    ):
        return Err('Request is no longer in the expected state.')

    now = datetime.now(UTC)
    updated = dataclasses.replace(
        request,
        status=TournamentRequestStatus.rejected,
        rejection_reason=reason,
        decided_at=now,
        decided_by_id=decider_id,
        updated_at=now,
    )

    tournament_request_repository.update_request_flush(updated)

    tournament_log_service.create_log_entry(
        'tournament-request-rejected',
        TournamentID(updated.id),
        decider_id,
        data={'reason': reason},
        commit=False,
    )

    db.session.commit()

    event = TournamentRequestRejectedEvent(
        occurred_at=now,
        initiator=None,
        request_id=updated.id,
        party_id=updated.party_id,
        proposer_id=updated.proposer_id,
        decided_by_id=decider_id,
        reason=reason,
    )
    signals.tournament_request_rejected.send(None, event=event)

    return Ok(updated)


def _is_linkable(
    request: TournamentRequest, expected_status: TournamentRequestStatus | None
) -> bool:
    """Return whether `request` may be linked to a newly created tournament.

    A plain narrowing check against `_LINK_ALLOWED_STATUSES` (the
    shape every other precondition in this module uses) is not enough
    here: a re-create after `delete_tournament` presents as
    `tournament_created` with its link already cleared, a status
    `_LINK_ALLOWED_STATUSES` (still just `accepted`) would never
    admit. So `tournament_created` is special-cased: it requires
    `tournament_deleted` (link cleared) AND still narrows against
    `expected_status`, the same as every other status's
    allowed-set-and-narrowing check. An explicit
    `expected_status=accepted` must not re-link a deleted-tournament
    request just because its link happens to be clear -- only
    `expected_status=None` or `expected_status=tournament_created`
    admit it.
    """
    if request.status is TournamentRequestStatus.tournament_created:
        return request.tournament_deleted and (
            expected_status is None
            or expected_status is TournamentRequestStatus.tournament_created
        )
    return request.status in _LINK_ALLOWED_STATUSES and (
        expected_status is None or request.status is expected_status
    )


def link_created_tournament_flush(
    request_id: TournamentRequestID,
    party_id: PartyID,
    tournament_id: TournamentID,
    decider_id: UserID,
    *,
    expected_status: TournamentRequestStatus | None = None,
) -> Result[TournamentRequest, str]:
    """Lock, re-check and link a request to its just-flushed tournament.

    Flush only -- called from inside
    `tournament_service.create_tournament`'s transaction, right after
    the tournament row itself was flushed (not yet committed), so the
    two either both land in the caller's single commit or neither
    does. Every precondition is re-run under the row lock rather than
    trusted from the caller's own, unlocked, read: another admin's
    `reject_request` may have committed in between, or the request may
    belong to a different party than the one creating the tournament
    (`from_request_id` is client-supplied). `request_id` not resolving
    to a row at all is one more way the precondition can fail, so it
    is folded into the same `Err` rather than left to raise.
    """
    try:
        request = tournament_request_repository.get_request_for_update(
            request_id
        )
    except ValueError:
        return Err('Request is no longer in the expected state.')

    if request.party_id != party_id:
        return Err('Request is no longer in the expected state.')

    if not _is_linkable(request, expected_status):
        return Err('Request is no longer in the expected state.')

    now = datetime.now(UTC)
    updated = dataclasses.replace(
        request,
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
        updated_at=now,
    )

    tournament_request_repository.update_request_flush(updated)

    # Keep this entry addressed by the request's own ID like every
    # other entry this service writes (see the module-level note on
    # `tournament_id`), not the real tournament's ID -- otherwise it
    # would be invisible to `get_request_history` and the proposer's
    # timeline would end at "accepted" instead of showing closure.
    tournament_log_service.create_log_entry(
        'tournament-request-tournament-created',
        TournamentID(request_id),
        decider_id,
        data={'tournament_id': str(tournament_id)},
        commit=False,
    )

    return Ok(updated)


def appoint_proposer_orga(
    tournament_id: TournamentID,
    proposer_id: UserID,
    decider_id: UserID,
) -> Result[None, str]:
    """Best-effort appoint the proposer as a tournament-scoped orga.

    Called by the view strictly after the tournament and its request
    link are already committed -- a failure here must never roll back
    the tournament or raise into the caller. Refuses a deleted or
    suspended proposer, the same guard the manual assign-orga route
    applies ("Do not grant tournament rights to an account that must
    not act."); every other failure (duplicate orga, `assign_orga`
    raising) is logged and folded into the same `Err` so the caller
    can flash one warning without needing to distinguish the cause.
    """
    try:
        proposer = user_service.find_user(proposer_id)
    except Exception:
        logger.warning(
            'Could not appoint proposer %s as orga of tournament %s: '
            'find_user raised.',
            proposer_id,
            tournament_id,
            exc_info=True,
        )
        return Err(
            'The proposer could not be appointed as orga of the tournament.'
        )

    if proposer is None or proposer.deleted or proposer.suspended:
        logger.warning(
            'Skipping orga appointment for proposer %s of tournament %s: '
            'account is missing, deleted, or suspended.',
            proposer_id,
            tournament_id,
        )
        return Err(
            'The proposer could not be appointed as orga of the tournament.'
        )

    try:
        orga_result = tournament_orga_service.assign_orga(
            tournament_id, proposer_id, decider_id
        )
    except Exception:
        logger.warning(
            'Could not appoint proposer %s as orga of tournament %s: '
            'assign_orga raised.',
            proposer_id,
            tournament_id,
            exc_info=True,
        )
        return Err(
            'The proposer could not be appointed as orga of the tournament.'
        )

    if orga_result.is_err():
        logger.warning(
            'Could not appoint proposer %s as orga of tournament %s: %s',
            proposer_id,
            tournament_id,
            orga_result.unwrap_err(),
        )
        return Err(
            'The proposer could not be appointed as orga of the tournament.'
        )

    return Ok(None)


def get_visible_requests_for_user(
    party_id: PartyID,
    user_id: UserID,
    *,
    is_admin: bool,
) -> list[TournamentRequest]:
    """Return the requests visible to that user for that party.

    Admins see every request for the party; everyone else sees only
    the requests they proposed themselves. Invisibility before
    acceptance is enforced here, once, rather than by a filter each
    caller could forget to apply.
    """
    if is_admin:
        return tournament_request_repository.get_requests_for_party(party_id)

    return tournament_request_repository.get_requests_for_proposer(
        party_id, user_id
    )


def get_request_history(
    request_id: TournamentRequestID,
) -> list[TournamentLogEntry]:
    """Return the audit log entries for that request, oldest first."""
    return tournament_log_service.get_entries_for_tournament(
        TournamentID(request_id)
    )
