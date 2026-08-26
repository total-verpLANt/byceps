"""Durable recipient work around the existing BYCEPS queue and mail sender.

Flush APIs belong to the owning transaction. Dispatch and worker APIs are
post-commit operations and own their short bookkeeping transactions only.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from smtplib import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPDataError,
    SMTPHeloError,
    SMTPNotSupportedError,
    SMTPRecipientsRefused,
    SMTPSenderRefused,
)
from uuid import UUID

import structlog

from byceps.byceps_app import get_current_byceps_app
from byceps.services.email import email_service
from byceps.util import jobqueue
from byceps.util.result import Err, Ok, Result

from . import tournament_notification_service as messages
from . import tournament_repository as repository
from .models.match_readiness import InvitationStatus
from .models.tournament import TournamentID
from .models.tournament_match import MatchInvitationID, TournamentMatchID


log = structlog.get_logger()

SWEEP_LIMIT = 100
FOLLOW_UP_DELAY_SECONDS = repository.INVITATION_LEASE_SECONDS + 30


@dataclass(frozen=True)
class _Batch:
    count: int
    failed: bool
    touched: frozenset[TournamentID]
    dispatched: frozenset[TournamentID]


def reconcile_match_invitations_flush(
    match_id: TournamentMatchID, *, occurred_at: datetime,
) -> Result[tuple[MatchInvitationID, ...], str]:
    """Reconcile assignment audience; never commit, queue or emit signals."""
    match = repository.find_match(match_id)
    if match is None:
        return Err('match_not_found')
    repository.lock_tournament_for_update(match.tournament_id)
    repository.lock_matches_for_update([match_id])
    recipients = {
        user_id
        for contestant in repository.get_contestants_for_match(match_id)
        for user_id in messages._resolve_user_ids_for_contestant(contestant)
    }
    return Ok(tuple(repository.ensure_invitation_intents_flush(
        match_id, recipients, occurred_at=occurred_at,
    )))


def enqueue_invitation_dispatch(
    invitation_ids: Collection[MatchInvitationID],
) -> Result[None, str]:
    """Hand one batch to one queue job; a failed enqueue leaves `pending` rows."""
    pending = tuple(sorted(set(invitation_ids), key=str))
    if not pending:
        return Ok(None)
    try:
        jobqueue.enqueue(dispatch_match_invitations, pending)
    except Exception:
        log.warning(
            'Invitation dispatch job enqueue deferred',
            count=len(pending),
            error='invitation_dispatch_enqueue_failed',
        )
        return Err('invitation_dispatch_failed')
    return Ok(None)


def enqueue_tournament_sweep(tournament_id: TournamentID) -> Result[None, str]:
    """Hand one catch-up sweep to the queue; the next dispatch sweeps anyway."""
    try:
        jobqueue.enqueue(sweep_tournament_invitations, tournament_id)
    except Exception:
        log.warning(
            'Invitation sweep enqueue deferred',
            tournament_id=str(tournament_id),
            error='invitation_sweep_enqueue_failed',
        )
        return Err('invitation_dispatch_failed')
    return Ok(None)


def dispatch_match_invitations(
    invitation_ids: Collection[MatchInvitationID],
    *,
    follow_up: bool = True,
) -> Result[int, str]:
    """Job body: reserve and commit before enqueue, then sweep what was touched.

    A failed queue leaves durable work. `follow_up` is off for a batch that a
    sweep selected, so one sweep never starts another.
    """
    batch = _dispatch_batch(invitation_ids)
    if follow_up:
        _sweep_touched(batch)
    return (
        Err('invitation_dispatch_failed') if batch.failed else Ok(batch.count)
    )


def sweep_tournament_invitations(
    tournament_id: TournamentID,
) -> Result[int, str]:
    """Recover expired work and dispatch what is due; log errors, never raise."""
    try:
        swept = _sweep_once(tournament_id)
        if swept.is_ok() and swept.unwrap() > 0:
            _schedule_follow_up(tournament_id)
        return swept
    except Exception:
        repository.rollback_session()
        log.warning(
            'Invitation sweep deferred',
            tournament_id=str(tournament_id),
            error='invitation_sweep_failed',
        )
        return Err('invitation_dispatch_failed')


def _dispatch_batch(invitation_ids: Collection[MatchInvitationID]) -> _Batch:
    count = 0
    failed = False
    touched: set[TournamentID] = set()
    dispatched: set[TournamentID] = set()
    for invitation_id in sorted(set(invitation_ids), key=str):
        try:
            current = repository.get_match_invitation(invitation_id)
            if current is None:
                repository.rollback_session()
                continue
            touched.add(current.tournament_id)
            claimed = repository.claim_invitation_dispatch_flush(
                invitation_id,
                expected_token=current.dispatch_token,
                now=datetime.now(UTC),
            )
            if claimed.is_err():
                repository.rollback_session()
                continue
            work = claimed.unwrap()
            repository.commit_session()
            try:
                jobqueue.enqueue(
                    deliver_match_invitation, work.id, work.dispatch_token
                )
            except Exception:
                log.warning(
                    'Invitation enqueue deferred',
                    invitation_id=str(work.id),
                    error='invitation_enqueue_failed',
                )
                outcome = _record_outcome(
                    work.id,
                    work.dispatch_token,
                    InvitationStatus.FAILED,
                    error='invitation_enqueue_failed',
                    retryable=True,
                )
                if outcome.is_ok():
                    _schedule_retry(work.id)
                failed = True
                continue
            queued = _record_outcome(
                work.id, work.dispatch_token, InvitationStatus.QUEUED
            )
            if queued.is_err():
                # Synchronous RQ can already have completed the same reservation.
                current = repository.get_match_invitation(work.id)
                benign = (
                    current is not None
                    and current.dispatch_token == work.dispatch_token
                    and current.status
                    in {
                        InvitationStatus.SENDING,
                        InvitationStatus.ACCEPTED,
                        InvitationStatus.FAILED,
                        InvitationStatus.SUPPRESSED,
                        InvitationStatus.DELIVERY_UNKNOWN,
                    }
                )
                repository.rollback_session()
                if not benign:
                    log.warning(
                        'Invitation queue bookkeeping deferred',
                        invitation_id=str(work.id),
                        error=queued.unwrap_err(),
                    )
                    failed = True
                    continue
            count += 1
            dispatched.add(work.tournament_id)
        except Exception:
            repository.rollback_session()
            log.warning(
                'Invitation dispatch deferred',
                invitation_id=str(invitation_id),
                error='invitation_dispatch_failed',
            )
            failed = True
    return _Batch(count, failed, frozenset(touched), frozenset(dispatched))


def _sweep_once(tournament_id: TournamentID) -> Result[int, str]:
    """Recover and select under the tournament lock, commit, then dispatch."""
    try:
        now = datetime.now(UTC)
        repository.lock_tournament_for_update(tournament_id)
        recovered = repository.recover_expired_invitations_flush(
            tournament_id,
            now=now,
            limit=SWEEP_LIMIT,
        )
        due = repository.select_invitation_retry_ids_flush(
            tournament_id,
            now=now,
            limit=SWEEP_LIMIT,
        )
        repository.commit_session()
    except Exception:
        repository.rollback_session()
        log.warning(
            'Invitation sweep deferred',
            tournament_id=str(tournament_id),
            error='invitation_sweep_failed',
        )
        return Err('invitation_dispatch_failed')
    selected = sorted({*recovered, *due}, key=str)
    if not selected:
        return Ok(0)
    return Ok(_dispatch_batch(selected).count)


def _sweep_touched(batch: _Batch) -> None:
    """Activity-driven sweep: once per tournament, one follow-up per dispatch."""
    for tournament_id in sorted(batch.touched, key=str):
        try:
            swept = _sweep_once(tournament_id)
            worked = swept.is_ok() and swept.unwrap() > 0
            if tournament_id in batch.dispatched or worked:
                _schedule_follow_up(tournament_id)
        except Exception:
            repository.rollback_session()
            log.warning(
                'Invitation sweep deferred',
                tournament_id=str(tournament_id),
                error='invitation_sweep_failed',
            )


def _schedule_follow_up(tournament_id: TournamentID) -> None:
    """One-shot sweep after the lease; loss of it is repaired by the next dispatch."""
    try:
        due = datetime.now(UTC) + timedelta(seconds=FOLLOW_UP_DELAY_SECONDS)
        jobqueue.enqueue_at(due, sweep_tournament_invitations, tournament_id)
    except Exception:
        log.warning(
            'Invitation follow-up sweep scheduling deferred',
            tournament_id=str(tournament_id),
            error='invitation_sweep_schedule_failed',
        )


def _record_outcome(
    invitation_id: MatchInvitationID, dispatch_token: UUID, status: InvitationStatus,
    *, error: str | None = None, retryable: bool = False,
) -> Result[None, str]:
    try:
        result = repository.record_invitation_outcome_flush(
            invitation_id, dispatch_token, status=status, now=datetime.now(UTC),
            error=error, retryable=retryable,
        )
        if result.is_err():
            repository.rollback_session()
        else:
            repository.commit_session()
        return result
    except Exception:
        repository.rollback_session()
        log.warning(
            'Invitation outcome bookkeeping deferred',
            invitation_id=str(invitation_id), error='invitation_record_failed',
        )
        return Err('invitation_record_failed')


def _schedule_retry(invitation_id: MatchInvitationID) -> Result[None, str]:
    """One-shot scheduling; loss of this effect does not erase the due record."""
    try:
        work = repository.get_match_invitation(invitation_id)
        repository.rollback_session()
        if work is not None and work.next_attempt_at is not None:
            jobqueue.enqueue_at(work.next_attempt_at, dispatch_match_invitations, (work.id,))
        return Ok(None)
    except Exception:
        repository.rollback_session()
        log.warning(
            'Invitation retry scheduling deferred', invitation_id=str(invitation_id),
            error='invitation_dispatch_failed',
        )
        return Err('invitation_dispatch_failed')


def deliver_match_invitation(
    invitation_id: MatchInvitationID, dispatch_token: UUID,
) -> Result[None, str]:
    """Validate and commit SENDING, release locks, then call public core SMTP."""
    try:
        work = repository.get_match_invitation(invitation_id)
        if work is None:
            repository.rollback_session()
            return Err('invitation_not_found')
        # Build before the final locked validation; no external send has begun.
        message = messages.build_match_invitation_message(
            work.tournament_id, work.match_id, work.recipient_id,
        )
        suppressed = get_current_byceps_app().byceps_config.smtp.suppress_send
    except Exception:
        repository.rollback_session()
        return _record_outcome(
            invitation_id, dispatch_token, InvitationStatus.FAILED,
            error='invitation_build_failed',
        )
    if message.is_err():
        return _record_outcome(
            invitation_id, dispatch_token, InvitationStatus.FAILED,
            error=message.unwrap_err(),
        )
    if suppressed:
        return _record_outcome(
            invitation_id, dispatch_token, InvitationStatus.SUPPRESSED,
            error='smtp_suppressed',
        )
    sending = _record_outcome(invitation_id, dispatch_token, InvitationStatus.SENDING)
    if sending.is_err():
        log.warning(
            'Invitation sending bookkeeping refused',
            invitation_id=str(invitation_id),
            error=sending.unwrap_err(),
        )
        return sending
    built = message.unwrap()
    try:
        email_service.send_email(
            built.sender.format(), built.recipients, built.subject, built.body,
        )
    except (SMTPAuthenticationError, SMTPNotSupportedError):
        return _record_outcome(
            invitation_id, dispatch_token, InvitationStatus.FAILED,
            error='smtp_configuration_failed',
        )
    except SMTPRecipientsRefused as exc:
        definite = len(built.recipients) == 1 and set(exc.recipients) == set(built.recipients)
        return _finish_send_failure(invitation_id, dispatch_token, definite)
    except (SMTPConnectError, SMTPHeloError, SMTPSenderRefused, SMTPDataError):
        return _finish_send_failure(invitation_id, dispatch_token, True)
    except Exception:
        return _finish_send_failure(invitation_id, dispatch_token, False)
    return _record_outcome(invitation_id, dispatch_token, InvitationStatus.ACCEPTED)


def _finish_send_failure(
    invitation_id: MatchInvitationID, dispatch_token: UUID, definite: bool,
) -> Result[None, str]:
    result = _record_outcome(
        invitation_id, dispatch_token,
        InvitationStatus.FAILED if definite else InvitationStatus.DELIVERY_UNKNOWN,
        error='smtp_rejected' if definite else 'smtp_delivery_unknown',
        retryable=definite,
    )
    if result.is_ok() and definite:
        return _schedule_retry(invitation_id)
    return result

