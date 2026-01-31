from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from byceps.services.user.models.user import UserID as UserId
from .tournament_match import TournamentMatchId

TournamentMatchCommentId = NewType('TournamentMatchCommentId', UUID)
@dataclass(kw_only=True)
class TournamentMatchComment:
    id: TournamentMatchCommentId
    tournament_match_id: TournamentMatchId
    created_by : UserId
    comment: str
