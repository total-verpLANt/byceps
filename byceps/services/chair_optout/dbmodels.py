"""
byceps.services.chair_optout.dbmodels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.ticketing.models.ticket import TicketID
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7

from .models import ChairOptoutID


class DbPartyTicketChairOptout(db.Model):
    """The chair opt-out setting for a party ticket."""

    __tablename__ = 'party_ticket_chair_optouts'
    __table_args__ = (db.UniqueConstraint('party_id', 'ticket_id'),)

    id: Mapped[ChairOptoutID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    party_id: Mapped[PartyID] = mapped_column(
        db.UnicodeText, db.ForeignKey('parties.id'), index=True
    )
    ticket_id: Mapped[TicketID] = mapped_column(
        db.Uuid, db.ForeignKey('tickets.id'), index=True
    )
    user_id: Mapped[UserID] = mapped_column(
        db.Uuid, db.ForeignKey('users.id'), index=True
    )
    brings_own_chair: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime]

    def __init__(
        self,
        party_id: PartyID,
        ticket_id: TicketID,
        user_id: UserID,
        updated_at: datetime,
        *,
        brings_own_chair: bool = False,
    ) -> None:
        self.party_id = party_id
        self.ticket_id = ticket_id
        self.user_id = user_id
        self.brings_own_chair = brings_own_chair
        self.updated_at = updated_at
