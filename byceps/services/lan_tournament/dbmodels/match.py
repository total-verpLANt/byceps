from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column, relationship

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.user.dbmodels.user import DbUser
from byceps.services.user.models.user import UserID
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7

from .tournament import DbTournament


class DbTournamentMatch(db.Model):
    """A match in a LAN tournament."""

    __tablename__ = 'lan_tournament_matches'

    id: Mapped[TournamentMatchID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournaments.id'),
        index=True,
    )
    tournament: Mapped[DbTournament] = relationship(DbTournament)
    group_order: Mapped[int | None]
    match_order: Mapped[int | None]
    round: Mapped[int | None]
    next_match_id: Mapped[TournamentMatchID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_matches.id'),
        index=True,
    )
    confirmed_by: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
    )
    confirmed_by_user: Mapped[DbUser | None] = relationship(DbUser)
    created_at: Mapped[datetime]

    def __init__(
        self,
        match_id: TournamentMatchID,
        tournament_id: TournamentID,
        created_at: datetime,
        *,
        group_order: int | None = None,
        match_order: int | None = None,
        round: int | None = None,
        next_match_id: TournamentMatchID | None = None,
        confirmed_by: UserID | None = None,
    ) -> None:
        self.id = match_id
        self.tournament_id = tournament_id
        self.created_at = created_at
        self.group_order = group_order
        self.match_order = match_order
        self.round = round
        self.next_match_id = next_match_id
        self.confirmed_by = confirmed_by

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('tournament_id')
            .add_with_lookup('round')
            .add_with_lookup('match_order')
            .build()
        )
