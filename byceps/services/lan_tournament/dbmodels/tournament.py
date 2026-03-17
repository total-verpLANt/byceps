from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
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
    tournament_mode: Mapped[str | None] = mapped_column(db.UnicodeText)

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
        tournament_mode: str | None = None,
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
        self.tournament_mode = tournament_mode

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('party_id')
            .add_with_lookup('name')
            .build()
        )
