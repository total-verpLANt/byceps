"""
byceps.services.pizza_delivery.models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID


PizzaDeliveryEntryID = NewType('PizzaDeliveryEntryID', UUID)


class PizzaDeliveryStatus:
    PENDING = 'pending'
    DELIVERED = 'delivered'


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntry:
    id: PizzaDeliveryEntryID
    party_id: PartyID
    number: str
    user_id: UserID | None
    status: str
    created_at: datetime
    updated_at: datetime
    created_by_id: UserID
