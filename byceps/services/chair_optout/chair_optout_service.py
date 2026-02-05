"""
byceps.services.chair_optout.chair_optout_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import TicketID
from byceps.services.user.models import UserID

from .dbmodels import DbPartyTicketChairOptout
from .models import ChairOptoutReportEntry, PartyTicketChairOptout


def get_optout(
    party_id: PartyID, ticket_id: TicketID
) -> PartyTicketChairOptout | None:
    """Return the opt-out setting for that ticket, if any."""
    db_optout = _get_db_optout(party_id, ticket_id)

    if db_optout is None:
        return None

    return _db_entity_to_optout(db_optout)


def set_optout(
    party_id: PartyID,
    ticket_id: TicketID,
    user_id: UserID,
    brings_own_chair: bool,
) -> PartyTicketChairOptout:
    """Create or update the opt-out setting for that ticket."""
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
    """Return the opt-out settings for the party."""
    db_optouts = _get_db_optouts_for_party(party_id)

    if only_true:
        db_optouts = [
            db_optout
            for db_optout in db_optouts
            if db_optout.brings_own_chair
        ]

    return [_db_entity_to_optout(db_optout) for db_optout in db_optouts]


def get_report_entries_for_party(
    party_id: PartyID,
) -> list[ChairOptoutReportEntry]:
    """Return report entries for tickets with chair opt-out enabled."""
    db_tickets = (
        db.session.scalars(
            select(DbTicket)
            .join(
                DbPartyTicketChairOptout,
                DbPartyTicketChairOptout.ticket_id == DbTicket.id,
            )
            .filter(DbPartyTicketChairOptout.party_id == party_id)
            .filter(DbPartyTicketChairOptout.brings_own_chair == True)  # noqa: E712
            .options(
                db.joinedload(DbTicket.occupied_seat),
                db.joinedload(DbTicket.used_by),
            )
            .order_by(DbTicket.code)
        )
        .unique()
        .all()
    )

    return [_build_report_entry(db_ticket) for db_ticket in db_tickets]


def list_optouts_for_user(
    party_id: PartyID, user_id: UserID
) -> list[PartyTicketChairOptout]:
    """Return the opt-out settings set by that user for the party."""
    db_optouts = _get_db_optouts_for_user(party_id, user_id)
    return [_db_entity_to_optout(db_optout) for db_optout in db_optouts]


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


def _get_db_optouts_for_party(
    party_id: PartyID,
) -> list[DbPartyTicketChairOptout]:
    return db.session.scalars(
        select(DbPartyTicketChairOptout).filter_by(party_id=party_id)
    ).all()


def _get_db_optouts_for_user(
    party_id: PartyID, user_id: UserID
) -> list[DbPartyTicketChairOptout]:
    return db.session.scalars(
        select(DbPartyTicketChairOptout)
        .filter_by(party_id=party_id)
        .filter_by(user_id=user_id)
    ).all()


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


def _build_report_entry(db_ticket: DbTicket) -> ChairOptoutReportEntry:
    user = db_ticket.used_by
    full_name = (
        user.detail.full_name if (user is not None and user.detail) else None
    )

    return ChairOptoutReportEntry(
        full_name=full_name,
        screen_name=user.screen_name if user is not None else None,
        ticket_code=db_ticket.code,
        seat_label=resolve_seat_label_for_ticket(db_ticket),
        has_seat=db_ticket.occupied_seat is not None,
    )
