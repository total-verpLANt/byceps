"""Inert effects collected by flush-only readiness operations.

The owning caller commits first, then explicitly dispatches these facts and
pending invitation IDs. Constructing or discarding a change has no side effects.
"""

from dataclasses import dataclass

from byceps.services.lan_tournament.events import (
    MatchBothReadyEvent,
    MatchReadyClaimedEvent,
    MatchReadyRevokedEvent,
)
from .match_readiness import MatchReadiness
from .tournament_match import MatchInvitationID, TournamentMatch


type ReadinessEvent = (
    MatchReadyClaimedEvent | MatchReadyRevokedEvent | MatchBothReadyEvent
)


@dataclass(frozen=True, kw_only=True)
class ReadinessChange:
    """Fresh snapshots and immutable effects, not a dispatcher or mail result."""

    match: TournamentMatch
    readiness: MatchReadiness
    actor_role: str | None
    events: tuple[ReadinessEvent, ...] = ()
    pending_invitation_ids: tuple[MatchInvitationID, ...] = ()

    def __post_init__(self) -> None:
        # Snapshot even if an untyped caller supplied a mutable collection.
        object.__setattr__(self, 'events', tuple(self.events))
        object.__setattr__(
            self, 'pending_invitation_ids', tuple(self.pending_invitation_ids)
        )
