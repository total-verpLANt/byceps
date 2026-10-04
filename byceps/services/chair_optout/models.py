"""
byceps.services.chair_optout.models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from dataclasses import dataclass
from byceps.services.seating.models import SeatID
from byceps.services.ticketing.models.ticket import ChairSource, TicketID
from byceps.services.user.models import UserID


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
    chair_source: ChairSource


@dataclass(frozen=True, kw_only=True)
class ChairInformationSummary:
    brings_own_chair: int
    needs_provided_chair: int
    rented_chair: int
    not_specified: int
    no_seat: int
