from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column, relationship

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
from byceps.services.user.dbmodels.user import DbUser
from byceps.services.user.models.user import UserID
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7

from .team import DbTournamentTeam
from .tournament import DbTournament


class DbTournamentParticipant(db.Model):
    """A participant in a LAN tournament."""

    __tablename__ = 'lan_tournament_participants'
    __table_args__ = (db.UniqueConstraint('tournament_id', 'user_id'),)

    id: Mapped[TournamentParticipantID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    user_id: Mapped[UserID] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
        index=True,
    )
    user: Mapped[DbUser] = relationship(DbUser)
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournaments.id'),
        index=True,
    )
    tournament: Mapped[DbTournament] = relationship(DbTournament)
    substitute_player: Mapped[bool] = mapped_column(default=False)
    team_id: Mapped[TournamentTeamID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_teams.id'),
    )
    team: Mapped[DbTournamentTeam | None] = relationship(DbTournamentTeam)
    created_at: Mapped[datetime]

    def __init__(
        self,
        participant_id: TournamentParticipantID,
        user_id: UserID,
        tournament_id: TournamentID,
        created_at: datetime,
        *,
        substitute_player: bool = False,
        team_id: TournamentTeamID | None = None,
    ) -> None:
        self.id = participant_id
        self.user_id = user_id
        self.tournament_id = tournament_id
        self.created_at = created_at
        self.substitute_player = substitute_player
        self.team_id = team_id

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('tournament_id')
            .add_with_lookup('user_id')
            .build()
        )
