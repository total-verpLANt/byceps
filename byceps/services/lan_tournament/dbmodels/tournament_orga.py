"""
byceps.services.lan_tournament.dbmodels.tournament_orga
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.user.models import UserID


class DbTournamentOrga(db.Model):
    """The assignment of a user as organizer ("orga") of a LAN tournament."""

    __tablename__ = 'lan_tournament_orgas'
    # Named as in migration 014, because `assign_orga` dispatches on it.
    __table_args__ = (
        db.UniqueConstraint(
            'tournament_id',
            'user_id',
            name='uq_lan_tournament_orgas_tournament_user',
        ),
    )

    id: Mapped[TournamentOrgaID] = mapped_column(db.Uuid, primary_key=True)
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid, db.ForeignKey('lan_tournaments.id'), index=True
    )
    user_id: Mapped[UserID] = mapped_column(
        db.Uuid, db.ForeignKey('users.id'), index=True
    )
    assigned_at: Mapped[datetime]
    assigned_by_id: Mapped[UserID | None] = mapped_column(
        db.Uuid, db.ForeignKey('users.id')
    )
    duties: Mapped[str | None] = mapped_column(db.UnicodeText)

    def __init__(
        self,
        orga_id: TournamentOrgaID,
        tournament_id: TournamentID,
        user_id: UserID,
        assigned_at: datetime,
        *,
        assigned_by_id: UserID | None = None,
        duties: str | None = None,
    ) -> None:
        self.id = orga_id
        self.tournament_id = tournament_id
        self.user_id = user_id
        self.assigned_at = assigned_at
        self.assigned_by_id = assigned_by_id
        self.duties = duties
