from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models.user import UserID
from .tournament_match import TournamentMatchID

TournamentMatchCommentID = NewType('TournamentMatchCommentID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentMatchComment:
    id: TournamentMatchCommentID
    tournament_match_id: TournamentMatchID
    created_by: UserID
    comment: str
    created_at: datetime
