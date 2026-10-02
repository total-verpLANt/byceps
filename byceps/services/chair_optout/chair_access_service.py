"""
byceps.services.chair_optout.chair_access_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import TicketID
from byceps.services.user.models import UserID


def lock_participant_ticket(
    party_id: PartyID, ticket_id: TicketID, user_id: UserID
) -> DbTicket | None:
    """Validate fresh eligibility, keeping the row lock until Core commits."""
    ticket = db.session.scalar(
        select(DbTicket)
        .where(
            DbTicket.id == ticket_id,
            DbTicket.party_id == party_id,
            DbTicket.revoked.is_(False),
            DbTicket.user_checked_in.is_(False),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket is None or not (
        ticket.is_used_by(user_id) or ticket.is_user_managed_by(user_id)
    ):
        return None
    return ticket
