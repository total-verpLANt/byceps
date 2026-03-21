from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from .contestant_type import ContestantType
from .tournament_status import TournamentStatus
from .score_ordering import ScoreOrdering
from .game_format import GameFormat
from .elimination_mode import EliminationMode

if TYPE_CHECKING:
    from .tournament_participant import TournamentParticipantID
    from .tournament_team import TournamentTeamID

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
    game_format: GameFormat | None
    elimination_mode: EliminationMode | None
    score_ordering: ScoreOrdering | None = None
    point_table: list[int] | None = None
    advancement_count: int | None = None
    group_size_min: int | None = None
    group_size_max: int | None = None
    points_carry_to_losers: bool | None = None
    use_bracket_reset: bool = True
    winner_team_id: 'TournamentTeamID | None' = None
    winner_participant_id: 'TournamentParticipantID | None' = None
