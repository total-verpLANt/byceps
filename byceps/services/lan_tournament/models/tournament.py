from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from .tournament_status import TournamentStatus
from .tournament_mode import TournamentMode

TournamentId = NewType('TournamentId', UUID)

@dataclass(kw_only=True)
class Tournament:
    id: TournamentId
    party_id: PartyID
    name: str
    description: str | None
    image_url: str | None
    ruleset: str | None
    min_players: int | None
    max_players: int | None
    min_teams: int | None
    max_teams: int | None
    min_players_in_team: int | None
    max_players_in_team: int | None
    tournament_status: TournamentStatus | TournamentStatus.none
    tournament_mode: TournamentMode | TournamentMode.none
