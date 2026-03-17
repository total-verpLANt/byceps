from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models import UserID

from .tournament import TournamentID
from .tournament_participant import TournamentParticipantID
from .tournament_team import TournamentTeamID

ScoreSubmissionID = NewType('ScoreSubmissionID', UUID)


@dataclass(frozen=True, kw_only=True)
class ScoreSubmission:
    id: ScoreSubmissionID
    tournament_id: TournamentID
    participant_id: TournamentParticipantID | None
    team_id: TournamentTeamID | None
    score: int
    submitted_at: datetime
    submitted_by: UserID | None
    is_official: bool
    note: str | None
