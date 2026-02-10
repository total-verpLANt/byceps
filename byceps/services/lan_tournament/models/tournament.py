from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from .contestant_type import ContestantType
from .tournament_status import TournamentStatus
from .tournament_mode import TournamentMode

TournamentID = NewType('TournamentID', UUID)


@dataclass(frozen=True, kw_only=True)
class Tournament:
    id: TournamentID
    party_id: PartyID
    name: str
    game: str | None
    description: str | None
    image_url: str | None
    ruleset: str | None
    start_time: datetime | None
    created_at: datetime
    updated_at: datetime | None = None
    min_players: int | None
    max_players: int | None
    min_teams: int | None
    max_teams: int | None
    min_players_in_team: int | None
    max_players_in_team: int | None
    contestant_type: ContestantType | None
    tournament_status: TournamentStatus | None
    tournament_mode: TournamentMode | None
