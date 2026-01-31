from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from .tournament_match import TournamentMatchId

TournamentMatchToContestantId = NewType('TournamentMatchToContestantId', UUID)

@dataclass(kw_only=True)
class TournamentMatchToContestant:
    id: TournamentMatchToContestantId
    tournament_match_id: TournamentMatchId
    tournament_participant_id: str  # Due to either beeing a tournament_team_id or tournament_participant_id, we can't type this properly...
    score: int | None
