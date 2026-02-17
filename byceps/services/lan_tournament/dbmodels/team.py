from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.dbmodels.user import DbUser
from byceps.services.user.models.user import UserID
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7

from .tournament import DbTournament


class DbTournamentTeam(db.Model):
    """A team in a LAN tournament."""

    __tablename__ = 'lan_tournament_teams'
    __table_args__ = (
        db.Index(
            'uq_lan_tournament_teams_active_name_ci',
            text('tournament_id, LOWER(name)'),
            unique=True,
            postgresql_where=text('removed_at IS NULL'),
        ),
        db.Index(
            'uq_lan_tournament_teams_active_tag_ci',
            text('tournament_id, UPPER(tag)'),
            unique=True,
            postgresql_where=text('removed_at IS NULL AND tag IS NOT NULL'),
        ),
        db.Index(
            'ix_lan_tournament_teams_active',
            'tournament_id',
            postgresql_where=text('removed_at IS NULL'),
        ),
    )

    id: Mapped[TournamentTeamID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournaments.id'),
        index=True,
    )
    tournament: Mapped[DbTournament] = relationship(DbTournament)
    name: Mapped[str] = mapped_column(db.UnicodeText)
    tag: Mapped[str | None] = mapped_column(db.UnicodeText)
    description: Mapped[str | None] = mapped_column(db.UnicodeText)
    image_url: Mapped[str | None] = mapped_column(db.UnicodeText)
    captain_user_id: Mapped[UserID] = mapped_column(
        db.Uuid, db.ForeignKey('users.id')
    )
    captain: Mapped[DbUser] = relationship(DbUser)
    join_code: Mapped[str | None] = mapped_column(db.UnicodeText)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime | None]
    removed_at: Mapped[datetime | None] = mapped_column(default=None)

    def __init__(
        self,
        team_id: TournamentTeamID,
        tournament_id: TournamentID,
        name: str,
        captain_user_id: UserID,
        created_at: datetime,
        *,
        tag: str | None = None,
        description: str | None = None,
        image_url: str | None = None,
        join_code: str | None = None,
    ) -> None:
        self.id = team_id
        self.tournament_id = tournament_id
        self.name = name
        self.captain_user_id = captain_user_id
        self.created_at = created_at
        self.updated_at = None
        self.tag = tag
        self.description = description
        self.image_url = image_url
        self.join_code = join_code

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('tournament_id')
            .add_with_lookup('name')
            .build()
        )
