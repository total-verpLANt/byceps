"""
byceps.services.chair_optout.models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from byceps.services.seating.models import SeatID
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
    ticket_id: TicketID
    user_id: UserID
    full_name: str | None
    screen_name: str | None
    ticket_code: str
    seat_id: SeatID | None
    seat_area_slug: str | None
    seat_label: str | None
    has_seat: bool
    brings_own_chair: bool | None


@dataclass(frozen=True, kw_only=True)
class ChairInformationSummary:
    brings_own_chair: int
    needs_provided_chair: int
    not_specified: int
    no_seat: int
