"""
byceps.services.seating.seat_deletion_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result

from . import seat_group_repository, seat_repository, seating_area_repository
from .dbmodels.seat import DbSeat
from .dbmodels.seat_group import DbSeatGroup, DbSeatGroupAssignment
from .errors import SeatingError


def delete_seats_for_party(party_id: PartyID) -> Result[dict[str, int], str]:
    """Delete all seats and seat groups for a party.

    Returns a dict with counts of deleted items on success.
    Returns an error if any seats or seat groups are occupied.
    """
    # Check if any seats are occupied
    occupied_seats_count = _count_occupied_seats_for_party(party_id)
    if occupied_seats_count > 0:
        return Err(
            f'Cannot delete seats: {occupied_seats_count} seat(s) are currently occupied by tickets.'
        )

    # Check if any seat groups are occupied
    occupied_groups_count = _count_occupied_seat_groups_for_party(party_id)
    if occupied_groups_count > 0:
        return Err(
            f'Cannot delete seat groups: {occupied_groups_count} seat group(s) are currently occupied.'
        )

    # Get counts before deletion for reporting
    seat_groups = seat_group_repository.get_groups_for_party(party_id)
    seat_group_count = len(seat_groups)

    areas = seating_area_repository.get_areas_for_party(party_id)
    seat_count = 0
    for db_area in areas:
        seat_count += len(db_area.seats)

    # Delete seat group assignments
    assignment_count = 0
    for db_group in seat_groups:
        group_assignment_count = len(db_group.assignments)
        for db_assignment in list(db_group.assignments):
            db.session.delete(db_assignment)
            assignment_count += 1

    # Delete seat groups
    for db_group in seat_groups:
        db.session.delete(db_group)

    # Delete seats
    for db_area in areas:
        for db_seat in list(db_area.seats):
            db.session.delete(db_seat)

    db.session.commit()

    return Ok({
        'seats': seat_count,
        'seat_groups': seat_group_count,
        'seat_group_assignments': assignment_count,
    })


def _count_occupied_seats_for_party(party_id: PartyID) -> int:
    """Count how many seats are occupied by tickets for this party."""
    from byceps.services.ticketing.dbmodels.ticket import DbTicket
    from byceps.services.ticketing.dbmodels.category import DbTicketCategory
    from byceps.services.seating.dbmodels.area import DbSeatingArea

    return (
        db.session.scalar(
            select(db.func.count(DbSeat.id))
            .join(DbTicket, DbSeat.id == DbTicket.occupied_seat_id)
            .join(DbSeatingArea, DbSeat.area_id == DbSeatingArea.id)
            .filter(DbSeatingArea.party_id == party_id)
            .filter(DbTicket.revoked == False)  # noqa: E712
        )
        or 0
    )


def _count_occupied_seat_groups_for_party(party_id: PartyID) -> int:
    """Count how many seat groups are occupied for this party."""
    from byceps.services.seating.dbmodels.seat_group import (
        DbSeatGroupOccupancy,
    )

    return (
        db.session.scalar(
            select(db.func.count(DbSeatGroupOccupancy.id))
            .join(DbSeatGroup, DbSeatGroupOccupancy.seat_group_id == DbSeatGroup.id)
            .filter(DbSeatGroup.party_id == party_id)
        )
        or 0
    )
