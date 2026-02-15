from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models.user import UserID
from .tournament import TournamentID

TournamentTeamID = NewType('TournamentTeamID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentTeam:
    id: TournamentTeamID
    tournament_id: TournamentID
    name: str
    tag: str | None
    description: str | None
    image_url: str | None
    captain_user_id: UserID
    join_code: str | None
    created_at: datetime
    updated_at: datetime | None = None
    removed_at: datetime | None = None
