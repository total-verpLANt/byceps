from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models.user import UserID

from .tournament_team import TournamentTeamID
from .tournament import TournamentID

TournamentParticipantID = NewType('TournamentParticipantID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentParticipant:
    id: TournamentParticipantID
    user_id: UserID
    tournament_id: TournamentID
    substitute_player: bool
    team_id: TournamentTeamID | None
    created_at: datetime
