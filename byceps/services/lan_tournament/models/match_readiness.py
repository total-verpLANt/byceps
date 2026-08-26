"""Single source of truth for the §25.3 match readiness display status.

Used by BOTH view layers (site and admin). The display status is
derived from the persisted per-side ready timestamps — it is never
persisted as parallel truth.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from byceps.services.user.models import UserID

from .tournament import TournamentID
from .tournament_match import (
    MatchInvitationID,
    MatchPairingID,
    MatchSide,
    TournamentMatch,
    TournamentMatchID,
)
from .tournament_match_to_contestant import TournamentMatchToContestant


class InvitationStatus(Enum):
    """Persisted recipient work stages, not match display states."""

    PENDING = 'pending'
    DISPATCHING = 'dispatching'
    QUEUED = 'queued'
    SENDING = 'sending'
    ACCEPTED = 'accepted'
    FAILED = 'failed'
    SUPPRESSED = 'suppressed'
    DELIVERY_UNKNOWN = 'delivery_unknown'


@dataclass(frozen=True, kw_only=True)
class ContestantIdentity:
    kind: str
    id: UUID


@dataclass(frozen=True, kw_only=True)
class MatchPairing:
    id: MatchPairingID
    match_id: TournamentMatchID
    tournament_id: TournamentID
    generation: int
    side_a: ContestantIdentity
    side_b: ContestantIdentity
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class MatchInvitation:
    id: MatchInvitationID
    match_id: TournamentMatchID
    tournament_id: TournamentID
    pairing_generation: int
    recipient_id: UserID
    status: InvitationStatus
    expected_readiness_revision: int
    attempts: int = 0
    dispatch_token: UUID | None = None
    next_attempt_at: datetime | None = None
    lease_until: datetime | None = None
    accepted_at: datetime | None = None
    last_error: str | None = None


class ReadinessDisplayStatus(Enum):
    """The four §25.3 display states."""

    NOT_YET_OCCUPIED = 'not_yet_occupied'
    OPEN = 'open'
    PARTIALLY_READY = 'partially_ready'
    BOTH_READY = 'both_ready'


READINESS_FILTER_BUCKETS = (
    'waiting',
    'not_ready',
    'partially_ready',
    'both_ready',
    'no_readiness',
    'finished',
)


@dataclass(frozen=True, kw_only=True)
class MatchReadiness:
    """Derived readiness state of a match."""

    status: ReadinessDisplayStatus
    ready_sides: tuple[MatchSide, ...]
    match_id: TournamentMatchID | None = None
    assignment_complete: bool = False
    assigned_contestant_count: int = 0
    original_occupied_since: datetime | None = None
    pairing_started_at: datetime | None = None
    pairing_id: MatchPairingID | None = None
    pairing_generation: int = 0
    readiness_revision: int = 0
    ready_at_a: datetime | None = None
    ready_at_b: datetime | None = None
    ready_by_a: UserID | None = None
    ready_by_b: UserID | None = None
    supports_readiness: bool = False
    pairing_valid: bool = False
    mutation_available: bool = False
    outcome: str | None = None

    @property
    def display_status(self) -> str:
        """Outcome labels override claims, without another persisted truth."""
        return self.outcome or self.status.value

    @property
    def filter_bucket(self) -> str:
        """Return the one list bucket of the match; outcomes come first."""
        if self.outcome is not None:
            return 'finished'
        if not self.supports_readiness:
            if self.assigned_contestant_count >= 2:
                return 'no_readiness'
            return 'waiting'
        if self.status is ReadinessDisplayStatus.NOT_YET_OCCUPIED:
            return 'waiting'
        if self.status is ReadinessDisplayStatus.OPEN:
            return 'not_ready'
        return self.status.value


def real_contestants(
    contestants: Sequence[TournamentMatchToContestant],
) -> list[TournamentMatchToContestant]:
    """Return real contestants in input order, excluding DEFWIN slots."""
    return [
        c for c in contestants if c.participant_id is not None or c.team_id is not None
    ]


def side_for_contestant(
    contestants: Sequence[TournamentMatchToContestant],
    contestant_id,
    *,
    pairing: MatchPairing | None = None,
) -> MatchSide | None:
    """Resolve stable logical sides when a pairing is supplied.

    The no-pair fallback preserves legacy callers, not claim interpretation.
    """
    real = real_contestants(contestants)
    if len(real) != 2:
        return None
    if pairing is not None:
        identities = [_identity(c) for c in real]
        if not _identities_match_pair(identities, pairing):
            return None
        for contestant, identity in zip(real, identities, strict=True):
            if contestant.id == contestant_id:
                return MatchSide.A if identity == pairing.side_a else MatchSide.B
        return None
    for index, contestant in enumerate(real):
        if contestant.id == contestant_id:
            return MatchSide.A if index == 0 else MatchSide.B
    return None


def derive_match_readiness(
    match: TournamentMatch,
    contestants: Sequence[TournamentMatchToContestant],
    *,
    pairing: MatchPairing | None,
    supports_readiness: bool,
) -> MatchReadiness:
    """Read current-pair claims only; never infer presence or elapsed time."""
    real = real_contestants(contestants)
    identities = [_identity(c) for c in real]
    assignment_complete = (
        supports_readiness
        and len(real) == 2
        and None not in identities
        and identities[0] != identities[1]
        and identities[0].kind == identities[1].kind
    )
    pairing_valid = bool(
        assignment_complete
        and pairing is not None
        and pairing.id == match.pairing_id
        and pairing.match_id == match.id
        and pairing.tournament_id == match.tournament_id
        and pairing.generation == match.pairing_generation
        and pairing.ended_at is None
        and _identities_match_pair(identities, pairing)
        and all(c.tournament_match_id == match.id for c in real)
    )

    ready_sides: list[MatchSide] = []
    if pairing_valid and match.ready_at_a is not None:
        ready_sides.append(MatchSide.A)
    if pairing_valid and match.ready_at_b is not None:
        ready_sides.append(MatchSide.B)

    if not assignment_complete:
        status = ReadinessDisplayStatus.NOT_YET_OCCUPIED
    elif len(ready_sides) == 2:
        status = ReadinessDisplayStatus.BOTH_READY
    elif len(ready_sides) == 1:
        status = ReadinessDisplayStatus.PARTIALLY_READY
    else:
        status = ReadinessDisplayStatus.OPEN

    outcome = None
    if match.confirmed_by is not None:
        outcome = 'defwin' if len(real) < 2 else 'confirmed'
    return MatchReadiness(
        status=status,
        ready_sides=tuple(ready_sides),
        match_id=match.id,
        assignment_complete=assignment_complete,
        assigned_contestant_count=len(real),
        original_occupied_since=match.occupied_since,
        pairing_started_at=pairing.started_at if pairing_valid else None,
        pairing_id=match.pairing_id,
        pairing_generation=match.pairing_generation,
        readiness_revision=match.readiness_revision,
        ready_at_a=match.ready_at_a if pairing_valid else None,
        ready_at_b=match.ready_at_b if pairing_valid else None,
        ready_by_a=match.ready_by_a if pairing_valid and match.ready_at_a else None,
        ready_by_b=match.ready_by_b if pairing_valid and match.ready_at_b else None,
        supports_readiness=supports_readiness,
        pairing_valid=pairing_valid,
        mutation_available=pairing_valid and outcome is None,
        outcome=outcome,
    )


def _identity(contestant: TournamentMatchToContestant) -> ContestantIdentity | None:
    if (contestant.participant_id is None) == (contestant.team_id is None):
        return None
    if contestant.participant_id is not None:
        return ContestantIdentity(kind='participant', id=contestant.participant_id)
    return ContestantIdentity(kind='team', id=contestant.team_id)


def _identities_match_pair(
    identities: Sequence[ContestantIdentity | None], pairing: MatchPairing,
) -> bool:
    return (
        len(identities) == 2
        and None not in identities
        and pairing.side_a != pairing.side_b
        and set(identities) == {pairing.side_a, pairing.side_b}
    )
