"""
byceps.services.chair_optout.chair_optout_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from collections.abc import Sequence
from typing import cast

from sqlalchemy import or_, select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.seating.dbmodels.seat import DbSeat
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import ChairSource, TicketID
from byceps.services.user.dbmodels import DbUser
from byceps.services.user.models import UserID

from .models import ChairInformationSummary, ChairOptoutReportEntry


def get_report_entries_for_party(
    party_id: PartyID,
) -> list[ChairOptoutReportEntry]:
    """Return eligible tickets with their current user, seat, and source."""
    db_tickets = (
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
    return [_build_report_entry(db_ticket) for db_ticket in db_tickets]


def get_chair_sources_for_party(
    party_id: PartyID,
) -> dict[TicketID, ChairSource]:
    """Return chair sources without loading report users or seats."""
    rows = db.session.execute(
        select(DbTicket.id, DbTicket.__table__.c.chair_source).where(
            DbTicket.party_id == party_id,
            DbTicket.revoked.is_(False),
            DbTicket.used_by_id.is_not(None),
        )
    ).all()
    return {
        ticket_id: ChairSource.__members__.get(source, ChairSource.unknown)
        for ticket_id, source in rows
    }


def get_pending_chair_ticket_ids_for_user(
    party_id: PartyID, user_id: UserID
) -> list[TicketID]:
    """Return unanswered, active, unchecked-in tickets the user may edit."""
    return list(
        db.session.scalars(
            select(DbTicket.id)
            .where(
                DbTicket.party_id == party_id,
                DbTicket.used_by_id.is_not(None),
                or_(
                    DbTicket.used_by_id == user_id,
                    DbTicket.user_managed_by_id == user_id,
                    (DbTicket.user_managed_by_id.is_(None))
                    & (DbTicket.owned_by_id == user_id),
                ),
                DbTicket.revoked.is_(False),
                DbTicket.user_checked_in.is_(False),
                or_(
                    DbTicket.__table__.c.chair_source.is_(None),
                    DbTicket.__table__.c.chair_source.not_in(
                        ['user', 'venue', 'rental']
                    ),
                ),
            )
            .order_by(DbTicket.code)
        ).all()
    )


def summarize_report_entries(
    report_entries: Sequence[ChairOptoutReportEntry],
) -> ChairInformationSummary:
    """Summarize sources and the overlapping no-seat count."""
    return ChairInformationSummary(
        brings_own_chair=sum(
            entry.chair_source is ChairSource.user for entry in report_entries
        ),
        needs_provided_chair=sum(
            entry.chair_source is ChairSource.venue for entry in report_entries
        ),
        rented_chair=sum(
            entry.chair_source is ChairSource.rental for entry in report_entries
        ),
        not_specified=sum(
            entry.chair_source is ChairSource.unknown
            for entry in report_entries
        ),
        no_seat=sum(not entry.has_seat for entry in report_entries),
    )


def _build_report_entry(db_ticket: DbTicket) -> ChairOptoutReportEntry:
    user = cast(DbUser, db_ticket.used_by)
    seat = db_ticket.occupied_seat
    full_name = user.detail.full_name if user.detail else None

    return ChairOptoutReportEntry(
        ticket_id=db_ticket.id,
        user_id=user.id,
        full_name=full_name,
        screen_name=user.screen_name,
        ticket_code=db_ticket.code,
        seat_id=seat.id if seat is not None else None,
        seat_area_slug=seat.area.slug if seat is not None else None,
        seat_label=seat.label if seat is not None else None,
        has_seat=seat is not None,
        chair_source=db_ticket.chair_source,
    )
