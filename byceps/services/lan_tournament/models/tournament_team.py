from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

TournamentTeamId = NewType('TournamentTeamId', UUID)

@dataclass(kw_only=True)
class TournamentTeam:
    id: TournamentTeamId
    tag : str | None
    description : str | None
    image_url : str | None
