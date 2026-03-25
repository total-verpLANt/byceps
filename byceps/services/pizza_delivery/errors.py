"""
byceps.services.pizza_delivery.errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from dataclasses import dataclass

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .models import PizzaDeliveryEntryID


@dataclass(frozen=True)
class PizzaDeliveryEntryNotFoundError:
    entry_id: PizzaDeliveryEntryID


@dataclass(frozen=True)
class DuplicateDeliveryNumberError:
    party_id: PartyID
    number: str


@dataclass(frozen=True)
class PizzaDeliveryEntryAlreadyDeliveredError:
    entry_id: PizzaDeliveryEntryID


@dataclass(frozen=True)
class PizzaDeliveryEntryNotDeliveredError:
    entry_id: PizzaDeliveryEntryID


@dataclass(frozen=True)
class PizzaDeliveryEntryAlreadyClaimedError:
    entry_id: PizzaDeliveryEntryID
    claimed_by_id: UserID


@dataclass(frozen=True)
class PizzaDeliveryNumberNotFoundError:
    party_id: PartyID
    number: str
