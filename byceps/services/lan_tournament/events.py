from dataclasses import dataclass
from typing import Self

from byceps.services.core.events import BaseEvent
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .models.match_readiness import MatchReadiness, ReadinessDisplayStatus
from .models.tournament import TournamentID
from .models.tournament_match import (
    MatchPairingID,
    MatchSide,
    TournamentMatchID,
)
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_request import TournamentRequestID
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeamID


# tournament


@dataclass(frozen=True, kw_only=True)
class _BaseTournamentEvent(BaseEvent):
    tournament_id: TournamentID


@dataclass(frozen=True, kw_only=True)
class TournamentCreatedEvent(_BaseTournamentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentUpdatedEvent(_BaseTournamentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentDeletedEvent(_BaseTournamentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentStatusChangedEvent(_BaseTournamentEvent):
    old_status: TournamentStatus | None
    new_status: TournamentStatus | None


@dataclass(frozen=True, kw_only=True)
class TournamentCompletedEvent(_BaseTournamentEvent):
    winner_team_id: TournamentTeamID | None
    winner_participant_id: TournamentParticipantID | None


@dataclass(frozen=True, kw_only=True)
class TournamentUncompletedEvent(_BaseTournamentEvent):
    pass


# orga


@dataclass(frozen=True, kw_only=True)
class TournamentOrgaAssignedEvent(_BaseTournamentEvent):
    user_id: UserID
    duties: str | None


@dataclass(frozen=True, kw_only=True)
class TournamentOrgaRevokedEvent(_BaseTournamentEvent):
    user_id: UserID


# participant


@dataclass(frozen=True, kw_only=True)
class _BaseParticipantEvent(_BaseTournamentEvent):
    participant_id: TournamentParticipantID


@dataclass(frozen=True, kw_only=True)
class ParticipantJoinedEvent(_BaseParticipantEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class ParticipantLeftEvent(_BaseParticipantEvent):
    pass


# team


@dataclass(frozen=True, kw_only=True)
class _BaseTeamEvent(_BaseTournamentEvent):
    team_id: TournamentTeamID


@dataclass(frozen=True, kw_only=True)
class TeamCreatedEvent(_BaseTeamEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TeamDeletedEvent(_BaseTeamEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TeamMemberJoinedEvent(_BaseTeamEvent):
    participant_id: TournamentParticipantID


@dataclass(frozen=True, kw_only=True)
class TeamMemberLeftEvent(_BaseTeamEvent):
    participant_id: TournamentParticipantID


@dataclass(frozen=True, kw_only=True)
class CaptainTransferredEvent(_BaseTeamEvent):
    old_captain_user_id: UserID
    new_captain_user_id: UserID


# match


@dataclass(frozen=True, kw_only=True)
class _BaseMatchEvent(_BaseTournamentEvent):
    match_id: TournamentMatchID


@dataclass(frozen=True, kw_only=True)
class MatchCreatedEvent(_BaseMatchEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class MatchDeletedEvent(_BaseMatchEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class MatchConfirmedEvent(_BaseMatchEvent):
    winner_team_id: TournamentTeamID | None
    winner_participant_id: TournamentParticipantID | None


@dataclass(frozen=True, kw_only=True)
class MatchUnconfirmedEvent(_BaseMatchEvent):
    unconfirmed_by: UserID


@dataclass(frozen=True, kw_only=True)
class ContestantAdvancedEvent(_BaseMatchEvent):
    from_match_id: TournamentMatchID
    advanced_team_id: TournamentTeamID | None
    advanced_participant_id: TournamentParticipantID | None


@dataclass(frozen=True, kw_only=True)
class MatchReadyEvent(_BaseMatchEvent):
    pass


# tournament request


@dataclass(frozen=True, kw_only=True)
class _BaseTournamentRequestEvent(BaseEvent):
    request_id: TournamentRequestID
    party_id: PartyID
    proposer_id: UserID


@dataclass(frozen=True, kw_only=True)
class TournamentRequestSubmittedEvent(_BaseTournamentRequestEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentRequestEditedEvent(_BaseTournamentRequestEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentRequestWithdrawnEvent(_BaseTournamentRequestEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class TournamentRequestAcceptedEvent(_BaseTournamentRequestEvent):
    decided_by_id: UserID


@dataclass(frozen=True, kw_only=True)
class TournamentRequestRejectedEvent(_BaseTournamentRequestEvent):
    decided_by_id: UserID
    reason: str


@dataclass(frozen=True, kw_only=True)
class MatchReadyClaimedEvent(_BaseMatchEvent):
    """A new side claim, collected for dispatch after the owning commit."""

    side: MatchSide
    claimed_by: UserID
    actor_role: str
    pairing_id: MatchPairingID
    pairing_generation: int
    readiness_revision: int


@dataclass(frozen=True, kw_only=True)
class MatchBothReadyEvent(_BaseMatchEvent):
    """A transition to both-ready, never an invitation delivery marker.

    Defaults preserve legacy callers until their flush-only adapters land.
    New operations supply all context and use ``from_transition``.
    """

    side: MatchSide | None = None
    claimed_by: UserID | None = None
    actor_role: str | None = None
    pairing_id: MatchPairingID | None = None
    pairing_generation: int = 0
    readiness_revision: int = 0

    @classmethod
    def from_transition(
        cls,
        before: MatchReadiness,
        after: MatchReadiness,
        *,
        claim: MatchReadyClaimedEvent,
    ) -> Self | None:
        """Collect a fact only on entry to both-ready; perform no dispatch.

        The operation owns fresh projections and generation validation. This
        pure factory deliberately has no notification/delivery input.
        """
        if (
            before.status == ReadinessDisplayStatus.BOTH_READY
            or after.status != ReadinessDisplayStatus.BOTH_READY
        ):
            return None
        return cls(
            occurred_at=claim.occurred_at,
            initiator=claim.initiator,
            tournament_id=claim.tournament_id,
            match_id=claim.match_id,
            side=claim.side,
            claimed_by=claim.claimed_by,
            actor_role=claim.actor_role,
            pairing_id=claim.pairing_id,
            pairing_generation=claim.pairing_generation,
            readiness_revision=claim.readiness_revision,
        )


@dataclass(frozen=True, kw_only=True)
class MatchReadyRevokedEvent(_BaseMatchEvent):
    side: MatchSide
    revoked_by: UserID
    actor_role: str
    previous_display_status: str
    pairing_id: MatchPairingID | None = None
    pairing_generation: int = 0
    readiness_revision: int = 0
