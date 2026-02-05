"""
byceps.services.chair_optout.models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from byceps.services.ticketing.models.ticket import TicketID
from byceps.services.user.models import UserID


ChairOptoutID = NewType('ChairOptoutID', UUID)


@dataclass(frozen=True, kw_only=True)
class PartyTicketChairOptout:
    id: ChairOptoutID
    party_id: PartyID
    ticket_id: TicketID
    user_id: UserID
    brings_own_chair: bool
    updated_at: datetime


@dataclass(frozen=True, kw_only=True)
class ChairOptoutReportEntry:
    full_name: str | None
    screen_name: str | None
    ticket_code: str
    seat_label: str | None
    has_seat: bool
