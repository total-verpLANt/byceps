from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from byceps.services.user.models.user import UserID

from .tournament_team import TournamentTeamId
from .tournament import TournamentId

TournamentParticipantId = NewType('TournamentParticipantId', UUID)

@dataclass(kw_only=True)
class TournamentParticipant:
    id: TournamentParticipantId
    user_id: UserID
    tournament_id: TournamentId
    substitute_player : bool
    team_id: TournamentTeamId | None
