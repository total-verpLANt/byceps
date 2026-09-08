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
    """Classification of a result-correction situation."""

    CASE_A = 'case_a'   # no downstream match -> correct freely
    CASE_B = 'case_b'   # downstream exists, unconfirmed -> warn, then proceed
    CASE_C = 'case_c'   # downstream confirmed/started -> critical warning + ack


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


@dataclass(frozen=True, kw_only=True)
class MatchUserRole:
    contestant: "TournamentMatchToContestant | None"
    is_loser: bool
    can_confirm: bool
    can_submit: bool
