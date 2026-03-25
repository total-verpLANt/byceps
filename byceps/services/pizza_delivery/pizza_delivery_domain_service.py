"""
byceps.services.pizza_delivery.pizza_delivery_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, timezone

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7

from .models import PizzaDeliveryEntry, PizzaDeliveryEntryID, PizzaDeliveryStatus


def create_entry(
    party_id: PartyID,
    number: str,
    user_id: UserID | None,
    created_by_id: UserID,
) -> PizzaDeliveryEntry:
    """Create a pizza delivery entry."""
    entry_id = PizzaDeliveryEntryID(generate_uuid7())

    now = datetime.now(timezone.utc)
    return PizzaDeliveryEntry(
        id=entry_id,
        party_id=party_id,
        number=number,
        user_id=user_id,
        status=PizzaDeliveryStatus.PENDING,
        created_at=now,
        updated_at=now,
        created_by_id=created_by_id,
    )
