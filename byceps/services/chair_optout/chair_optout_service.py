"""
byceps.services.chair_optout.chair_optout_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.seating.dbmodels.seat import DbSeat
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import TicketID
from byceps.services.user.dbmodels import DbUser
from byceps.services.user.models import UserID

from .dbmodels import DbPartyTicketChairOptout
from .models import (
    ChairInformationSummary,
    ChairOptoutReportEntry,
    PartyTicketChairOptout,
)


def get_optout(
    party_id: PartyID, ticket_id: TicketID
) -> PartyTicketChairOptout | None:
    """Return the current participant's answer for that ticket, if any."""
    db_ticket = db.session.get(DbTicket, ticket_id)
    if (
        db_ticket is None
        or db_ticket.party_id != party_id
        or db_ticket.revoked
        or db_ticket.used_by_id is None
    ):
        return None

    db_optout = _get_db_optout(party_id, ticket_id)

    if db_optout is None or db_optout.user_id != db_ticket.used_by_id:
        return None

    return _db_entity_to_optout(db_optout)


def set_optout(
    party_id: PartyID,
    ticket_id: TicketID,
    user_id: UserID,
    brings_own_chair: bool,
) -> PartyTicketChairOptout:
    """Store the current ticket user's chair answer."""
    db_ticket = _find_eligible_ticket(party_id, ticket_id, user_id)
    if db_ticket is None:
        raise ValueError('Ticket is not currently used by this user.')

    now = datetime.utcnow()
    db_optout = _get_db_optout(party_id, ticket_id)

    if db_optout is None:
        db_optout = DbPartyTicketChairOptout(
            party_id,
            ticket_id,
            user_id,
            now,
            brings_own_chair=brings_own_chair,
        )
        db.session.add(db_optout)
    else:
        db_optout.user_id = user_id
        db_optout.brings_own_chair = brings_own_chair
        db_optout.updated_at = now

    db.session.commit()

    return _db_entity_to_optout(db_optout)


def list_optouts_for_party(
    party_id: PartyID, *, only_true: bool = True
) -> list[PartyTicketChairOptout]:
    """Return valid answers from current participants of the party."""
    tickets = _get_eligible_tickets_for_party(party_id)
    optouts_by_ticket_id = get_current_optouts_for_tickets(tickets)
    optouts = list(optouts_by_ticket_id.values())
    if only_true:
        return [optout for optout in optouts if optout.brings_own_chair]
    return optouts


def get_current_optouts_for_tickets(
    tickets: Sequence[DbTicket],
) -> dict[TicketID, PartyTicketChairOptout]:
    """Return valid answers for the tickets' current participants."""
    eligible_tickets = {
        ticket.id: ticket
        for ticket in tickets
        if not ticket.revoked and ticket.used_by_id is not None
    }
    if not eligible_tickets:
        return {}

    db_optouts = _get_db_optouts_for_tickets(set(eligible_tickets))
    return {
        db_optout.ticket_id: _db_entity_to_optout(db_optout)
        for db_optout in db_optouts
        if (
            db_optout.party_id == eligible_tickets[db_optout.ticket_id].party_id
            and db_optout.user_id
            == eligible_tickets[db_optout.ticket_id].used_by_id
        )
    }


def get_report_entries_for_party(
    party_id: PartyID,
) -> list[ChairOptoutReportEntry]:
    """Return all eligible tickets with their current chair answer."""
    db_tickets = _get_eligible_tickets_for_party(party_id)
    optouts_by_ticket_id = get_current_optouts_for_tickets(db_tickets)
    return [
        _build_report_entry(db_ticket, optouts_by_ticket_id.get(db_ticket.id))
        for db_ticket in db_tickets
    ]


def summarize_report_entries(
    report_entries: Sequence[ChairOptoutReportEntry],
) -> ChairInformationSummary:
    """Summarize answer states and the additional no-seat count."""
    return ChairInformationSummary(
        brings_own_chair=sum(
            entry.brings_own_chair is True for entry in report_entries
        ),
        needs_provided_chair=sum(
            entry.brings_own_chair is False for entry in report_entries
        ),
        not_specified=sum(
            entry.brings_own_chair is None for entry in report_entries
        ),
        no_seat=sum(not entry.has_seat for entry in report_entries),
    )


def list_optouts_for_user(
    party_id: PartyID, user_id: UserID
) -> list[PartyTicketChairOptout]:
    """Return valid answers for tickets currently used by that user."""
    tickets = db.session.scalars(
        select(DbTicket)
        .filter_by(party_id=party_id, used_by_id=user_id, revoked=False)
        .order_by(DbTicket.code)
    ).all()
    return list(get_current_optouts_for_tickets(tickets).values())


def resolve_seat_label_for_ticket(ticket: DbTicket | None) -> str | None:
    """Resolve the seat label from the ticket's current seat."""
    if ticket is None or ticket.occupied_seat is None:
        return None

    return ticket.occupied_seat.label


def _get_db_optout(
    party_id: PartyID, ticket_id: TicketID
) -> DbPartyTicketChairOptout | None:
    return db.session.execute(
        select(DbPartyTicketChairOptout)
        .filter_by(party_id=party_id)
        .filter_by(ticket_id=ticket_id)
    ).scalar_one_or_none()


def _get_db_optouts_for_tickets(
    ticket_ids: set[TicketID],
) -> list[DbPartyTicketChairOptout]:
    return list(
        db.session.scalars(
            select(DbPartyTicketChairOptout).filter(
                DbPartyTicketChairOptout.ticket_id.in_(ticket_ids)
            )
        ).all()
    )


def _find_eligible_ticket(
    party_id: PartyID, ticket_id: TicketID, user_id: UserID
) -> DbTicket | None:
    return db.session.execute(
        select(DbTicket).filter_by(
            id=ticket_id,
            party_id=party_id,
            used_by_id=user_id,
            revoked=False,
        )
    ).scalar_one_or_none()


def _get_eligible_tickets_for_party(party_id: PartyID) -> list[DbTicket]:
    return list(
        db.session.scalars(
            select(DbTicket)
            .filter_by(party_id=party_id, revoked=False)
            .filter(DbTicket.used_by_id.is_not(None))
            .options(
                db.joinedload(DbTicket.occupied_seat).joinedload(DbSeat.area),
                db.joinedload(DbTicket.used_by).joinedload(DbUser.detail),
            )
            .order_by(DbTicket.code)
        )
        .unique()
        .all()
    )


def _db_entity_to_optout(
    db_optout: DbPartyTicketChairOptout,
) -> PartyTicketChairOptout:
    return PartyTicketChairOptout(
        id=db_optout.id,
        party_id=db_optout.party_id,
        ticket_id=db_optout.ticket_id,
        user_id=db_optout.user_id,
        brings_own_chair=db_optout.brings_own_chair,
        updated_at=db_optout.updated_at,
    )


def _build_report_entry(
    db_ticket: DbTicket, optout: PartyTicketChairOptout | None
) -> ChairOptoutReportEntry:
    user = cast(DbUser, db_ticket.used_by)
    seat = db_ticket.occupied_seat
    full_name = (
        user.detail.full_name if (user is not None and user.detail) else None
    )

    return ChairOptoutReportEntry(
        ticket_id=db_ticket.id,
        user_id=user.id,
        full_name=full_name,
        screen_name=user.screen_name,
        ticket_code=db_ticket.code,
        seat_id=seat.id if seat is not None else None,
        seat_area_slug=seat.area.slug if seat is not None else None,
        seat_label=resolve_seat_label_for_ticket(db_ticket),
        has_seat=db_ticket.occupied_seat is not None,
        brings_own_chair=(
            optout.brings_own_chair if optout is not None else None
        ),
    )
