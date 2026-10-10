from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType
from uuid import UUID

from byceps.services.user.models import UserID
from .tournament_match import TournamentMatchID

TournamentMatchCommentID = NewType('TournamentMatchCommentID', UUID)


class MatchCommentContext(StrEnum):
    ORGA_CONFIRMATION = 'orga_confirmation'


@dataclass(frozen=True, kw_only=True)
class TournamentMatchComment:
    id: TournamentMatchCommentID
    tournament_match_id: TournamentMatchID
    created_by: UserID
    comment: str
    created_at: datetime
    context: MatchCommentContext | None = None
