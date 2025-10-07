"""
:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.seating import (
    seat_deletion_service,
    seat_group_service,
    seat_service,
)


def test_delete_seats_for_party_without_groups(
    admin_app, party, seating_ticket_category, seating_area
):
    """Test deletion of seats without seat groups."""
    # Create some seats
    seat1 = seat_service.create_seat(
        seating_area.id, 10, 20, seating_ticket_category.id, label='A1'
    )
    seat2 = seat_service.create_seat(
        seating_area.id, 30, 40, seating_ticket_category.id, label='A2'
    )

    # Verify seats exist
    assert seat_service.count_seats_for_party(party.id) == 2

    # Delete seats
    result = seat_deletion_service.delete_seats_for_party(party.id)

    assert result.is_ok()
    deleted_counts = result.unwrap()
    assert deleted_counts['seats'] == 2
    assert deleted_counts['seat_groups'] == 0
    assert deleted_counts['seat_group_assignments'] == 0

    # Verify seats are deleted
    assert seat_service.count_seats_for_party(party.id) == 0


def test_delete_seats_for_party_with_groups(
    admin_app, party, seating_ticket_category, seating_area
):
    """Test deletion of seats with seat groups."""
    # Create some seats
    seat1 = seat_service.create_seat(
        seating_area.id, 50, 60, seating_ticket_category.id, label='B1'
    )
    seat2 = seat_service.create_seat(
        seating_area.id, 70, 80, seating_ticket_category.id, label='B2'
    )

    # Create a seat group
    group_result = seat_group_service.create_group(
        party.id, seating_ticket_category.id, 'Test Group', [seat1, seat2]
    )
    assert group_result.is_ok()

    # Verify seats and groups exist
    assert seat_service.count_seats_for_party(party.id) == 2
    assert seat_group_service.count_groups_for_party(party.id) == 1

    # Delete seats and groups
    result = seat_deletion_service.delete_seats_for_party(party.id)

    assert result.is_ok()
    deleted_counts = result.unwrap()
    assert deleted_counts['seats'] == 2
    assert deleted_counts['seat_groups'] == 1
    assert deleted_counts['seat_group_assignments'] == 2

    # Verify seats and groups are deleted
    assert seat_service.count_seats_for_party(party.id) == 0
    assert seat_group_service.count_groups_for_party(party.id) == 0


def test_delete_seats_for_party_with_occupied_seats(
    admin_app, party, seating_ticket_category, seating_area, make_user
):
    """Test that deletion fails when seats are occupied."""
    from byceps.services.ticketing import ticket_creation_service

    # Create a user
    user = make_user()

    # Create a seat
    seat = seat_service.create_seat(
        seating_area.id, 90, 100, seating_ticket_category.id, label='C1'
    )

    # Create a ticket and occupy the seat
    ticket = ticket_creation_service.create_ticket(
        seating_ticket_category, user
    )

    # Occupy the seat
    from byceps.services.ticketing import ticket_seat_management_service

    occupy_result = ticket_seat_management_service.occupy_seat(
        ticket.id, seat.id, user
    )
    assert occupy_result.is_ok()

    # Attempt to delete seats should fail
    result = seat_deletion_service.delete_seats_for_party(party.id)

    assert result.is_err()
    error_message = result.unwrap_err()
    assert 'occupied by tickets' in error_message

    # Verify seats still exist
    assert seat_service.count_seats_for_party(party.id) == 1

    # Clean up: release the seat
    release_result = ticket_seat_management_service.release_seat(
        ticket.id, user
    )
    assert release_result.is_ok()


def test_delete_seats_for_empty_party(admin_app, party):
    """Test deletion for a party with no seats."""
    # Ensure party has no seats
    seat_count = seat_service.count_seats_for_party(party.id)
    if seat_count > 0:
        seat_deletion_service.delete_seats_for_party(party.id)

    # Delete seats for empty party
    result = seat_deletion_service.delete_seats_for_party(party.id)

    assert result.is_ok()
    deleted_counts = result.unwrap()
    assert deleted_counts['seats'] == 0
    assert deleted_counts['seat_groups'] == 0
    assert deleted_counts['seat_group_assignments'] == 0
