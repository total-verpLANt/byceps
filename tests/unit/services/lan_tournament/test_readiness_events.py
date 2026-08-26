"""Effect contracts only; actual operation transactions belong to Issue 8."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime
from typing import get_type_hints
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import signals
from byceps.services.lan_tournament.events import (
    MatchBothReadyEvent,
    MatchReadyClaimedEvent,
    MatchReadyRevokedEvent,
)
from byceps.services.lan_tournament.models.match_readiness import (
    MatchReadiness,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.readiness_change import (
    ReadinessChange,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    MatchPairingID,
    MatchSide,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.user.models import UserID
from byceps.util.uuid import uuid7


@pytest.fixture
def claim():
    return MatchReadyClaimedEvent(
        occurred_at=datetime(2026, 10, 5, 12),
        initiator=None,
        tournament_id=TournamentID(uuid7()),
        match_id=TournamentMatchID(uuid7()),
        side=MatchSide.B,
        claimed_by=UserID(uuid7()),
        actor_role='captain',
        pairing_id=MatchPairingID(uuid7()),
        pairing_generation=3,
        readiness_revision=7,
    )


@pytest.fixture
def change(claim):
    match = TournamentMatch(
        id=claim.match_id,
        tournament_id=claim.tournament_id,
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=None,
        created_at=claim.occurred_at,
        ready_at_b=claim.occurred_at,
        ready_by_b=claim.claimed_by,
        pairing_id=claim.pairing_id,
        pairing_generation=claim.pairing_generation,
        readiness_revision=claim.readiness_revision,
    )
    return ReadinessChange(
        match=match,
        readiness=MatchReadiness(
            status=ReadinessDisplayStatus.PARTIALLY_READY,
            ready_sides=(MatchSide.B,),
        ),
        actor_role=claim.actor_role,
        events=(claim,),
        pending_invitation_ids=(MatchInvitationID(uuid7()),),
    )


def test_events_are_immutable_and_generation_bound(claim, change):
    revoked = MatchReadyRevokedEvent(
        occurred_at=claim.occurred_at,
        initiator=None,
        tournament_id=claim.tournament_id,
        match_id=claim.match_id,
        side=claim.side,
        revoked_by=claim.claimed_by,
        actor_role=claim.actor_role,
        previous_display_status='both_ready',
        pairing_id=claim.pairing_id,
        pairing_generation=3,
        readiness_revision=8,
    )
    both = MatchBothReadyEvent.from_transition(
        change.readiness,
        MatchReadiness(
            status=ReadinessDisplayStatus.BOTH_READY,
            ready_sides=(MatchSide.A, MatchSide.B),
        ),
        claim=claim,
    )
    assert both is not None
    for event in (claim, revoked, both):
        assert event.match_id == change.match.id
        assert event.tournament_id == change.match.tournament_id
        assert event.pairing_id == change.match.pairing_id
        assert event.pairing_generation == 3
        assert event.side == MatchSide.B
        assert event.actor_role == 'captain'
        assert event.occurred_at == claim.occurred_at
        hints = get_type_hints(type(event))
        assert hints['match_id'] is TournamentMatchID
        assert hints['tournament_id'] is TournamentID
        assert hints['pairing_id'] in (MatchPairingID, MatchPairingID | None)
        with pytest.raises(FrozenInstanceError):
            event.readiness_revision = 99
    assert both.claimed_by == claim.claimed_by == change.match.ready_by_b
    assert revoked.revoked_by == claim.claimed_by
    assert both.readiness_revision == claim.readiness_revision == 7
    assert revoked.readiness_revision == 8
    assert get_type_hints(MatchReadyClaimedEvent)['claimed_by'] is UserID
    assert get_type_hints(MatchReadyRevokedEvent)['revoked_by'] is UserID
    assert (
        get_type_hints(ReadinessChange)['pending_invitation_ids']
        == tuple[MatchInvitationID, ...]
    )
    for snapshot, field in (
        (change, 'events'),
        (change.match, 'readiness_revision'),
        (change.readiness, 'status'),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(snapshot, field, None)
    events = [claim]
    invitations = list(change.pending_invitation_ids)
    collected = replace(
        change, events=events, pending_invitation_ids=invitations
    )
    events.clear()
    invitations.clear()
    assert collected.events == (claim,)
    assert collected.pending_invitation_ids == change.pending_invitation_ids


# fmt: off
@pytest.mark.parametrize(('before', 'after', 'expected'), [
    (ReadinessDisplayStatus.OPEN, ReadinessDisplayStatus.PARTIALLY_READY, False),
    (ReadinessDisplayStatus.PARTIALLY_READY, ReadinessDisplayStatus.BOTH_READY, True),
    (ReadinessDisplayStatus.OPEN, ReadinessDisplayStatus.BOTH_READY, True),
    (ReadinessDisplayStatus.BOTH_READY, ReadinessDisplayStatus.BOTH_READY, False),
    (ReadinessDisplayStatus.BOTH_READY, ReadinessDisplayStatus.PARTIALLY_READY, False),
])
# fmt: on
def test_both_ready_event_only_on_transition(claim, change, before, after, expected):
    """Pure projection filtering, not proof of the unwritten operation."""
    sides = {
        ReadinessDisplayStatus.OPEN: (),
        ReadinessDisplayStatus.PARTIALLY_READY: (MatchSide.A,),
        ReadinessDisplayStatus.BOTH_READY: (MatchSide.A, MatchSide.B),
    }
    for marker in (None, claim.occurred_at):
        snapshot = replace(change.match, both_ready_notified_at=marker)
        event = MatchBothReadyEvent.from_transition(
            MatchReadiness(status=before, ready_sides=sides[before]),
            MatchReadiness(status=after, ready_sides=sides[after]),
            claim=claim,
        )
        assert (event is not None) is expected
        assert snapshot.both_ready_notified_at == marker


def test_effects_not_dispatched_on_rollback(claim, change):
    """Discarding collected effects is inert; no DB rollback is simulated."""
    listeners = [Mock(), Mock(), Mock()]
    domain_signals = [
        signals.match_ready_claimed,
        signals.match_ready_revoked,
        signals.match_both_ready,
    ]
    for signal, listener in zip(domain_signals, listeners, strict=True):
        signal.connect(listener, weak=False)
    try:
        collected = replace(change, events=(claim,))
        del collected  # Caller discards effects when its transaction rolls back.
        for listener in listeners:
            listener.assert_not_called()
        # Explicit caller send is observable; the DTO never performs it.
        signals.match_ready_claimed.send(None, event=claim)
        listeners[0].assert_called_once_with(None, event=claim)
        listeners[1].assert_not_called()
        listeners[2].assert_not_called()
    finally:
        for signal, listener in zip(domain_signals, listeners, strict=True):
            signal.disconnect(listener)


def test_legacy_readiness_event_constructors_remain_compatible(claim):
    context = dict(
        occurred_at=claim.occurred_at,
        initiator=None,
        tournament_id=claim.tournament_id,
        match_id=claim.match_id,
    )
    both = MatchBothReadyEvent(**context)
    revoked = MatchReadyRevokedEvent(
        **context, side=MatchSide.A, revoked_by=claim.claimed_by,
        actor_role='orga', previous_display_status='both_ready',
    )
    for event in (both, revoked):
        assert event.pairing_id is None
        assert event.pairing_generation == event.readiness_revision == 0


def test_revoked_event_has_no_reason_field():
    assert 'reason' not in {f.name for f in fields(MatchReadyRevokedEvent)}
