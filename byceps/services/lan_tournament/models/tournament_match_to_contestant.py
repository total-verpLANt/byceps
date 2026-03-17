from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from .tournament_participant import TournamentParticipantID
from .tournament_team import TournamentTeamID
from .tournament_match import TournamentMatchID

TournamentMatchToContestantID = NewType('TournamentMatchToContestantID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentMatchToContestant:
    id: TournamentMatchToContestantID
    tournament_match_id: TournamentMatchID
    team_id: TournamentTeamID | None
    participant_id: TournamentParticipantID | None
    score: int | None
    created_at: datetime
