from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7


class DbTournament(db.Model):
    """A LAN tournament."""

    __tablename__ = 'lan_tournaments'

    id: Mapped[TournamentID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    party_id: Mapped[PartyID] = mapped_column(
        db.UnicodeText,
        db.ForeignKey('parties.id'),
        index=True,
    )
    name: Mapped[str] = mapped_column(db.UnicodeText)
    game: Mapped[str | None] = mapped_column(db.UnicodeText)
    description: Mapped[str | None] = mapped_column(db.UnicodeText)
    image_url: Mapped[str | None] = mapped_column(db.UnicodeText)
    ruleset: Mapped[str | None] = mapped_column(db.UnicodeText)
    start_time: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime | None]
    min_players: Mapped[int | None]
    max_players: Mapped[int | None]
    min_teams: Mapped[int | None]
    max_teams: Mapped[int | None]
    min_players_in_team: Mapped[int | None]
    max_players_in_team: Mapped[int | None]
    contestant_type: Mapped[str | None] = mapped_column(db.UnicodeText)
    tournament_status: Mapped[str | None] = mapped_column(db.UnicodeText)
    game_format: Mapped[str | None] = mapped_column(db.UnicodeText)
    elimination_mode: Mapped[str | None] = mapped_column(db.UnicodeText)
    score_ordering: Mapped[str | None] = mapped_column(db.UnicodeText)
    point_table: Mapped[str | None] = mapped_column(db.UnicodeText)  # JSON
    advancement_count: Mapped[int | None]
    group_size_min: Mapped[int | None]
    group_size_max: Mapped[int | None]
    points_carry_to_losers: Mapped[bool | None]
    use_bracket_reset: Mapped[bool] = mapped_column(
        db.Boolean, server_default=db.text('true'),
    )
    winner_team_id: Mapped[TournamentTeamID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_teams.id'),
    )
    winner_participant_id: Mapped[TournamentParticipantID | None] = (
        mapped_column(
            db.Uuid,
            db.ForeignKey('lan_tournament_participants.id'),
        )
    )

    def __init__(
        self,
        tournament_id: TournamentID,
        party_id: PartyID,
        name: str,
        created_at: datetime,
        *,
        game: str | None = None,
        description: str | None = None,
        image_url: str | None = None,
        ruleset: str | None = None,
        start_time: datetime | None = None,
        min_players: int | None = None,
        max_players: int | None = None,
        min_teams: int | None = None,
        max_teams: int | None = None,
        min_players_in_team: int | None = None,
        max_players_in_team: int | None = None,
        contestant_type: str | None = None,
        tournament_status: str | None = None,
        game_format: str | None = None,
        elimination_mode: str | None = None,
        score_ordering: str | None = None,
        point_table: str | None = None,
        advancement_count: int | None = None,
        group_size_min: int | None = None,
        group_size_max: int | None = None,
        points_carry_to_losers: bool | None = None,
        winner_team_id: TournamentTeamID | None = None,
        winner_participant_id: TournamentParticipantID | None = None,
    ) -> None:
        self.id = tournament_id
        self.party_id = party_id
        self.name = name
        self.created_at = created_at
        self.updated_at = None
        self.game = game
        self.description = description
        self.image_url = image_url
        self.ruleset = ruleset
        self.start_time = start_time
        self.min_players = min_players
        self.max_players = max_players
        self.min_teams = min_teams
        self.max_teams = max_teams
        self.min_players_in_team = min_players_in_team
        self.max_players_in_team = max_players_in_team
        self.contestant_type = contestant_type
        self.tournament_status = tournament_status
        self.game_format = game_format
        self.elimination_mode = elimination_mode
        self.score_ordering = score_ordering
        self.point_table = point_table
        self.advancement_count = advancement_count
        self.group_size_min = group_size_min
        self.group_size_max = group_size_max
        self.points_carry_to_losers = points_carry_to_losers
        self.winner_team_id = winner_team_id
        self.winner_participant_id = winner_participant_id

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('party_id')
            .add_with_lookup('name')
            .build()
        )
