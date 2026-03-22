"""
byceps.services.pizza_delivery.dbmodels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .models import PizzaDeliveryEntryID, PizzaDeliveryStatus


class DbPizzaDeliveryEntry(db.Model):
    """A pizza delivery entry."""

    __tablename__ = 'pizza_delivery_entries'
    __table_args__ = (
        db.UniqueConstraint('party_id', 'number'),
    )

    id: Mapped[PizzaDeliveryEntryID] = mapped_column(
        db.Uuid, primary_key=True
    )
    party_id: Mapped[PartyID] = mapped_column(
        db.UnicodeText, db.ForeignKey('parties.id'), index=True
    )
    number: Mapped[str] = mapped_column(db.UnicodeText)
    user_id: Mapped[UserID | None] = mapped_column(
        db.Uuid, db.ForeignKey('users.id'), nullable=True
    )
    status: Mapped[str] = mapped_column(db.UnicodeText, default=PizzaDeliveryStatus.REGISTERED)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    created_by_id: Mapped[UserID] = mapped_column(
        db.Uuid, db.ForeignKey('users.id')
    )

    def __init__(
        self,
        entry_id: PizzaDeliveryEntryID,
        party_id: PartyID,
        number: str,
        user_id: UserID | None,
        created_at: datetime,
        updated_at: datetime,
        created_by_id: UserID,
        status: str = PizzaDeliveryStatus.REGISTERED,
    ) -> None:
        self.id = entry_id
        self.party_id = party_id
        self.number = number
        self.user_id = user_id
        self.status = status
        self.created_at = created_at
        self.updated_at = updated_at
        self.created_by_id = created_by_id
