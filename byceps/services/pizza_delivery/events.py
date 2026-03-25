"""
byceps.services.pizza_delivery.events
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from dataclasses import dataclass

from byceps.services.core.events import BaseEvent
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .models import PizzaDeliveryEntryID


@dataclass(frozen=True, kw_only=True)
class _BasePizzaDeliveryEvent(BaseEvent):
    party_id: PartyID


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntryCreatedEvent(_BasePizzaDeliveryEvent):
    entry_id: PizzaDeliveryEntryID
    number: str
    user_id: UserID | None


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntryDeletedEvent(_BasePizzaDeliveryEvent):
    entry_id: PizzaDeliveryEntryID
    number: str


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntryDeliveredEvent(_BasePizzaDeliveryEvent):
    entry_id: PizzaDeliveryEntryID
    number: str
    user_id: UserID | None


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntryUndeliveredEvent(_BasePizzaDeliveryEvent):
    entry_id: PizzaDeliveryEntryID
    number: str
    user_id: UserID | None


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntryUpdatedEvent(_BasePizzaDeliveryEvent):
    entry_id: PizzaDeliveryEntryID
    number: str
    user_id: UserID | None


@dataclass(frozen=True, kw_only=True)
class PizzaDeliveryEntryClaimedEvent(_BasePizzaDeliveryEvent):
    entry_id: PizzaDeliveryEntryID
    number: str
    user_id: UserID
