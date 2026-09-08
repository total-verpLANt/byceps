"""
byceps.services.lan_tournament.dbmodels.tournament_log_entry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_log_entry import (
    TournamentLogEntryID,
)
from byceps.services.user.models import UserID


class DbTournamentLogEntry(db.Model):
    """A log entry for a LAN tournament."""

    __tablename__ = 'lan_tournament_log_entries'

    id: Mapped[TournamentLogEntryID] = mapped_column(
        db.Uuid, primary_key=True
    )
    occurred_at: Mapped[datetime]
    event_type: Mapped[str] = mapped_column(db.UnicodeText)
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid, db.ForeignKey('lan_tournaments.id'), index=True
    )
    initiator_id: Mapped[UserID | None] = mapped_column(
        db.Uuid, db.ForeignKey('users.id')
    )
    data: Mapped[dict[str, Any]] = mapped_column(db.JSONB)

    def __init__(
        self,
        entry_id: TournamentLogEntryID,
        occurred_at: datetime,
        event_type: str,
        tournament_id: TournamentID,
        initiator_id: UserID | None,
        data: dict[str, Any],
    ) -> None:
        self.id = entry_id
        self.occurred_at = occurred_at
        self.event_type = event_type
        self.tournament_id = tournament_id
        self.initiator_id = initiator_id
        self.data = data
