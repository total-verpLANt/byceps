"""Flush-only readiness operations; the caller owns rollback, commit and effects."""

import logging
from collections.abc import Collection
from datetime import UTC, datetime

from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result

from . import tournament_readiness_authorization_service as authority
from . import tournament_repository as repository
from .events import (
    MatchBothReadyEvent,
    MatchReadyClaimedEvent,
    MatchReadyRevokedEvent,
)
from .models.game_format import GameFormat
from .models.match_readiness import derive_match_readiness
from .models.readiness_change import ReadinessChange
from .models.tournament_match import MatchInvitationID, MatchSide, TournamentMatchID
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeamID
from .signals import match_both_ready, match_ready_claimed, match_ready_revoked
from .tournament_domain_service import game_format_for_phase
from .tournament_log_service import create_log_entry

logger = logging.getLogger(__name__)
MAX_REVISION = (1 << 63) - 1


def _locked_facts(match_id):
    subject = repository.find_match(match_id)
    if subject is None:
        return Err('match_not_found')
    try:
        tournament = repository.get_tournament_for_update(subject.tournament_id)
        match = repository.get_match_for_update(match_id)
    except ValueError:
        return Err('readiness_subject_not_found')
    if match.tournament_id != tournament.id:
        return Err('match_tournament_mismatch')
    contestants = repository.get_contestants_for_match(match_id)
    pairing = repository.get_match_pairing(match_id)
    supports = (
        game_format_for_phase(tournament, match.phase) == GameFormat.ONE_V_ONE
    )
    return Ok((tournament, match, contestants, pairing, supports))


def _projection(facts):
    _, match, contestants, pairing, supports = facts
    return derive_match_readiness(
        match, contestants, pairing=pairing, supports_readiness=supports
    )


def _fresh_authority_inputs(pairing):
    """Refresh cached roster rows before the authority service's ordinary reads.

    The tournament lock serializes sanctioned roster writers. Team member SELECT
    predicates use current database membership, but cached returned row attributes
    still require populate_existing. Never reuse pre-lock role/roster DTOs.
    """
    try:
        for identity in sorted(
            (pairing.side_a, pairing.side_b), key=lambda i: str(i.id)
        ):
            if identity.kind == 'participant':
                if (
                    repository.find_participant_fresh(
                        TournamentParticipantID(identity.id)
                    )
                    is None
                ):
                    return Err('readiness_pairing_invalid')
            elif identity.kind == 'team':
                team_id = TournamentTeamID(identity.id)
                repository.get_team_for_update(team_id)
                for member in repository.get_participants_for_team(team_id):
                    repository.find_participant_fresh(member.id)
            else:
                return Err('readiness_pairing_invalid')
    except ValueError:
        return Err('readiness_pairing_invalid')
    return Ok(None)


def _validate(match_id, side, initiator_id, generation, revision):
    if not isinstance(side, MatchSide):
        return Err('invalid_match_side')
    if any(
        type(value) is not int or not 0 <= value <= MAX_REVISION
        for value in (generation, revision)
    ):
        return Err('invalid_readiness_revision')
    result = _locked_facts(match_id)
    if result.is_err():
        return result
    facts = result.unwrap()
    tournament, match, _, pairing, supports = facts
    if tournament.tournament_status != TournamentStatus.ONGOING:
        return Err('tournament_not_ongoing')
    if match.confirmed_by is not None:
        return Err('match_confirmed')
    if not supports:
        return Err('readiness_format_unsupported')
    if (
        match.pairing_generation != generation
        or match.readiness_revision != revision
    ):
        return Err('readiness_conflict')
    if not _projection(facts).pairing_valid:
        return Err('readiness_pairing_invalid')
    if revision == MAX_REVISION:
        return Err('readiness_revision_exhausted')
    fresh = _fresh_authority_inputs(pairing)
    if fresh.is_err():
        return fresh
    authorized = authority.authorize_readiness_side(
        tournament.id, pairing, side, initiator_id
    )
    if authorized.is_err():
        return authorized
    return Ok((facts, authorized.unwrap()))


def _audit(event_type, facts, actor, data):
    try:
        create_log_entry(
            event_type, facts[1].tournament_id, actor, data=data, commit=False
        )
    except Exception:
        logger.exception('Readiness audit failed; owning caller must roll back')
        return Err('readiness_audit_failed')
    return Ok(None)


def _audit_data(facts, side, role, now):
    _, match, _, pairing, _ = facts
    identity = pairing.side_a if side == MatchSide.A else pairing.side_b
    return {
        'match_id': str(match.id),
        'side': side.value,
        'actor_role': role,
        'pairing_id': str(pairing.id),
        'pairing_generation': match.pairing_generation,
        'readiness_revision': match.readiness_revision + 1,
        'contestant_kind': identity.kind,
        'contestant_id': str(identity.id),
        'previous_display_status': _projection(facts).status.value,
        'occurred_at': now.isoformat(),
    }


def claim_ready_flush(
    match_id: TournamentMatchID,
    side: MatchSide,
    initiator_id: UserID,
    *,
    expected_pairing_generation: int,
    expected_readiness_revision: int,
) -> Result[ReadinessChange, str]:
    validated = _validate(
        match_id,
        side,
        initiator_id,
        expected_pairing_generation,
        expected_readiness_revision,
    )
    if validated.is_err():
        return Err(validated.unwrap_err())
    facts, role = validated.unwrap()
    before = _projection(facts)
    if side in before.ready_sides:
        return Err('readiness_conflict')
    now = datetime.now(UTC).replace(tzinfo=None)
    data = _audit_data(facts, side, role, now)
    data['ready_at'] = now.isoformat()
    repository.set_side_ready_flush(match_id, side, now, initiator_id)
    repository.set_side_invitation_hold_flush(match_id, side, False)
    repository.set_readiness_revision_flush(
        match_id, expected_readiness_revision + 1
    )
    audit = _audit('match-ready-claimed', facts, initiator_id, data)
    if audit.is_err():
        return Err(audit.unwrap_err())
    pending = reconcile_invitations_flush((match_id,), occurred_at=now)
    if pending.is_err():
        return Err(pending.unwrap_err())
    fresh = _locked_facts(match_id).unwrap()
    after = _projection(fresh)
    event = MatchReadyClaimedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=facts[1].tournament_id,
        match_id=match_id,
        side=side,
        claimed_by=initiator_id,
        actor_role=role,
        pairing_id=facts[3].id,
        pairing_generation=after.pairing_generation,
        readiness_revision=after.readiness_revision,
    )
    both = MatchBothReadyEvent.from_transition(before, after, claim=event)
    return Ok(
        ReadinessChange(
            match=fresh[1],
            readiness=after,
            actor_role=role,
            events=(event, both) if both else (event,),
            pending_invitation_ids=pending.unwrap(),
        )
    )


def revoke_ready_flush(
    match_id: TournamentMatchID,
    side: MatchSide,
    initiator_id: UserID,
    *,
    expected_pairing_generation: int,
    expected_readiness_revision: int,
) -> Result[ReadinessChange, str]:
    validated = _validate(
        match_id,
        side,
        initiator_id,
        expected_pairing_generation,
        expected_readiness_revision,
    )
    if validated.is_err():
        return Err(validated.unwrap_err())
    facts, role = validated.unwrap()
    before = _projection(facts)
    if side not in before.ready_sides:
        return Err('readiness_conflict')
    now = datetime.now(UTC).replace(tzinfo=None)
    data = _audit_data(facts, side, role, now)
    data.update(revoked_at=now.isoformat())
    repository.clear_side_ready_flush(match_id, side)
    repository.set_side_invitation_hold_flush(match_id, side, True)
    repository.set_readiness_revision_flush(
        match_id, expected_readiness_revision + 1
    )
    audit = _audit('match-ready-revoked', facts, initiator_id, data)
    if audit.is_err():
        return Err(audit.unwrap_err())
    pending = reconcile_invitations_flush((match_id,), occurred_at=now)
    if pending.is_err():
        return Err(pending.unwrap_err())
    fresh = _locked_facts(match_id).unwrap()
    event = MatchReadyRevokedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=facts[1].tournament_id,
        match_id=match_id,
        side=side,
        revoked_by=initiator_id,
        actor_role=role,
        previous_display_status=before.status.value,
        pairing_id=facts[3].id,
        pairing_generation=fresh[1].pairing_generation,
        readiness_revision=fresh[1].readiness_revision,
    )
    return Ok(
        ReadinessChange(
            match=fresh[1],
            readiness=_projection(fresh),
            actor_role=role,
            events=(event,),
            pending_invitation_ids=pending.unwrap(),
        )
    )


def refresh_pairing_and_invitations_flush(
    match_id: TournamentMatchID,
    *,
    occurred_at: datetime,
) -> Result[ReadinessChange, str]:
    """Refresh pairing and recipient intents in the caller's transaction."""
    return _refresh_or_reset(match_id, occurred_at, reset=False)


def reset_readiness_flush(
    match_id: TournamentMatchID,
    *,
    occurred_at: datetime,
) -> Result[ReadinessChange, str]:
    return _refresh_or_reset(match_id, occurred_at, reset=True)


def _refresh_or_reset(match_id, occurred_at, *, reset):
    result = _locked_facts(match_id)
    if result.is_err():
        return result
    facts = result.unwrap()
    match = facts[1]
    if match.readiness_revision == MAX_REVISION or (
        not reset and match.pairing_generation == MAX_REVISION
    ):
        return Err('readiness_revision_exhausted')
    if reset:
        changed = bool(
            match.ready_at_a
            or match.ready_at_b
            or match.invitation_hold_a
            or match.invitation_hold_b
        )
        if changed:
            repository.clear_match_readiness_flush(
                match_id, increment_revision=True
            )
    else:
        refreshed = repository.refresh_match_pairing_flush(
            match_id, occurred_at=occurred_at
        )
        if refreshed.is_err():
            return Err(refreshed.unwrap_err())
        changed = refreshed.unwrap()
    # Nothing was written when nothing changed, so the first read still holds.
    fresh = _locked_facts(match_id).unwrap() if changed else facts
    if changed:
        audit = _audit(
            'match-readiness-reset' if reset else 'match-pairing-refreshed',
            facts,
            None,
            {
                'match_id': str(match_id),
                'previous_display_status': _projection(facts).status.value,
                'pairing_generation': fresh[1].pairing_generation,
                'readiness_revision': fresh[1].readiness_revision,
                'occurred_at': occurred_at.isoformat(),
            },
        )
        if audit.is_err():
            return Err(audit.unwrap_err())
    if facts[3] is None and fresh[3] is None:
        # No pairing before or after: no recipient can exist to reconcile.
        pending = Ok(())
    else:
        pending = reconcile_invitations_flush(
            (match_id,), occurred_at=occurred_at
        )
    if pending.is_err():
        return Err(pending.unwrap_err())
    return Ok(
        ReadinessChange(
            match=fresh[1], readiness=_projection(fresh), actor_role=None,
            pending_invitation_ids=pending.unwrap(),
        )
    )


def reconcile_invitations_flush(
    match_ids: Collection[TournamentMatchID], *, occurred_at: datetime,
) -> Result[tuple[MatchInvitationID, ...], str]:
    """Coalesce affected matches. Caller holds tournament/ordered match locks."""
    from . import tournament_invitation_service

    pending: set[MatchInvitationID] = set()
    for match_id in sorted(set(match_ids), key=str):
        result = tournament_invitation_service.reconcile_match_invitations_flush(
            match_id, occurred_at=occurred_at,
        )
        if result.is_err():
            return Err(result.unwrap_err())
        pending.update(result.unwrap())
    return Ok(tuple(sorted(pending, key=str)))


def dispatch_pending_invitations(
    invitation_ids: Collection[MatchInvitationID],
) -> Result[None, str]:
    """Post-commit only. One queue job per batch; a failed enqueue leaves
    `pending` rows for the invitation sweep.
    """
    from . import tournament_invitation_service

    pending = tuple(sorted(set(invitation_ids), key=str))
    if not pending:
        return Ok(None)
    try:
        result = tournament_invitation_service.enqueue_invitation_dispatch(
            pending
        )
        if result.is_ok():
            return Ok(None)
        error = 'invitation_dispatch_failed'
    except Exception:
        error = 'invitation_dispatch_exception'
    logger.error(
        'Post-commit invitation dispatch failed (%s); recover pending invitation IDs %s',
        error, [str(invitation_id) for invitation_id in pending],
    )
    return Err('readiness_dispatch_failed')


def dispatch_readiness_effects(change: ReadinessChange) -> Result[None, str]:
    """Call only after commit; attempt all events and durable invitation work."""
    failed = False
    for event in change.events:
        signal = (
            match_ready_claimed
            if isinstance(event, MatchReadyClaimedEvent)
            else match_ready_revoked
            if isinstance(event, MatchReadyRevokedEvent)
            else match_both_ready
        )
        try:
            signal.send(None, event=event)
        except Exception:
            failed = True
            logger.exception('Post-commit readiness effect failed')
    if dispatch_pending_invitations(change.pending_invitation_ids).is_err():
        failed = True
    return Err('readiness_dispatch_failed') if failed else Ok(None)
