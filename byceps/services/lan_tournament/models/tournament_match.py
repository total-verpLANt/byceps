from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, NewType
from uuid import UUID

from byceps.services.user.models import UserID
from .bracket import Bracket
from .tournament import TournamentID

if TYPE_CHECKING:
    from .tournament_match_to_contestant import TournamentMatchToContestant

TournamentMatchID = NewType('TournamentMatchID', UUID)
MatchPairingID = NewType('MatchPairingID', UUID)
MatchInvitationID = NewType('MatchInvitationID', UUID)


class MatchSide(Enum):
    """The two sides of a 1v1 match.

    Side A is the contestant created first (lowest ``created_at``),
    side B the second one.
    """

    A = 'a'
    B = 'b'


class CorrectionCase(Enum):
    """What a result correction affects downstream."""

    NO_DOWNSTREAM = 'no_downstream'
    UNCONFIRMED_DOWNSTREAM = 'unconfirmed_downstream'
    # Retracts confirmed downstream matches; needs an acknowledgement.
    CONFIRMED_DOWNSTREAM = 'confirmed_downstream'
    # Deletes the bracket-reset grand final; needs an acknowledgement.
    BRACKET_RESET_DELETION = 'bracket_reset_deletion'


@dataclass(frozen=True, kw_only=True)
class TournamentMatch:
    id: TournamentMatchID
    tournament_id: TournamentID
    group_order: int | None
    match_order: int | None
    round: int | None
    next_match_id: TournamentMatchID | None
    confirmed_by: UserID | None
    created_at: datetime
    bracket: Bracket | None = None
    loser_next_match_id: TournamentMatchID | None = None
    phase: int = 1
    seeding_target: str | None = None
    occupied_since: datetime | None = None
    ready_at_a: datetime | None = None
    ready_at_b: datetime | None = None
    ready_by_a: UserID | None = None
    ready_by_b: UserID | None = None
    both_ready_notified_at: datetime | None = None
    pairing_generation: int = 0
    readiness_revision: int = 0
    pairing_id: MatchPairingID | None = None
    invitation_hold_a: bool = False
    invitation_hold_b: bool = False


@dataclass(frozen=True, kw_only=True)
class MatchUserRole:
    contestant: "TournamentMatchToContestant | None"
    is_loser: bool
    can_confirm: bool
    can_submit: bool
