from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from byceps.services.user.models.user import UserID as UserId
from .tournament import TournamentId

TournamentMatchId = NewType('TournamentMatchId', UUID)
@dataclass(frozen=True, kw_only=True)
class TournamentMatch:
    id: TournamentMatchId
    tournament_id: TournamentId
    group_order: int | None
    match_order: int | None
    confirmed_by: UserId | None
