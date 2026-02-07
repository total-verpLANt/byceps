from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from .tournament_participant import TournamentParticipantId
from .tournament_team import TournamentTeamId
from .tournament_match import TournamentMatchId

TournamentMatchToContestantId = NewType('TournamentMatchToContestantId', UUID)

@dataclass(frozen=True, kw_only=True)
class TournamentMatchToContestant:
    id: TournamentMatchToContestantId
    tournament_match_id: TournamentMatchId
    tournament_participant_id: TournamentTeamId | TournamentParticipantId
    score: int | None
