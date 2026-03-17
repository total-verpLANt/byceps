from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models import UserID
from .bracket import Bracket
from .tournament import TournamentID

TournamentMatchID = NewType('TournamentMatchID', UUID)


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
