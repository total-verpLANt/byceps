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


@dataclass(frozen=True, kw_only=True)
class MatchUserRole:
    contestant: "TournamentMatchToContestant | None"
    is_loser: bool
    can_confirm: bool
    can_submit: bool
