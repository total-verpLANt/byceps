"""
:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest
from click.testing import CliRunner

from byceps.cli.commands.delete_seats import delete_seats
from byceps.services.seating import (
    seat_group_service,
    seat_service,
)


@pytest.fixture
def cli_runner(admin_app):
    """Create a CLI runner for testing."""
    return CliRunner()


def test_delete_seats_with_confirmation(
    cli_runner, admin_app, cli_party, cli_ticket_category, cli_seating_area
):
    """Test delete-seats command with user confirmation."""
    with admin_app.app_context():
        # Create some test seats
        seat1 = seat_service.create_seat(
            cli_seating_area.id, 10, 20, cli_ticket_category.id, label='CLI-A1'
        )
        seat2 = seat_service.create_seat(
            cli_seating_area.id, 30, 40, cli_ticket_category.id, label='CLI-A2'
        )

        # Verify seats exist
        assert seat_service.count_seats_for_party(cli_party.id) == 2

        # Run the delete command with confirmation
        result = cli_runner.invoke(
            delete_seats, [str(cli_party.id)], input='y\n'
        )

        assert result.exit_code == 0
        assert 'Seats to delete: 2' in result.output
        assert 'Successfully deleted 2 seat(s)' in result.output

        # Verify seats are deleted
        assert seat_service.count_seats_for_party(cli_party.id) == 0


def test_delete_seats_with_force_flag(
    cli_runner, admin_app, cli_party, cli_ticket_category, cli_seating_area
):
    """Test delete-seats command with --force flag."""
    with admin_app.app_context():
        # Create some test seats
        seat1 = seat_service.create_seat(
            cli_seating_area.id, 50, 60, cli_ticket_category.id, label='CLI-B1'
        )
        seat2 = seat_service.create_seat(
            cli_seating_area.id, 70, 80, cli_ticket_category.id, label='CLI-B2'
        )

        # Verify seats exist
        assert seat_service.count_seats_for_party(cli_party.id) == 2

        # Run the delete command with --force flag
        result = cli_runner.invoke(delete_seats, [str(cli_party.id), '--force'])

        assert result.exit_code == 0
        assert 'Seats to delete: 2' in result.output
        assert 'Successfully deleted 2 seat(s)' in result.output

        # Verify seats are deleted
        assert seat_service.count_seats_for_party(cli_party.id) == 0


def test_delete_seats_cancelled(
    cli_runner, admin_app, cli_party, cli_ticket_category, cli_seating_area
):
    """Test delete-seats command when user cancels."""
    with admin_app.app_context():
        # Create some test seats
        seat1 = seat_service.create_seat(
            cli_seating_area.id, 90, 100, cli_ticket_category.id, label='CLI-C1'
        )

        # Verify seats exist
        initial_count = seat_service.count_seats_for_party(cli_party.id)
        assert initial_count >= 1

        # Run the delete command and cancel
        result = cli_runner.invoke(
            delete_seats, [str(cli_party.id)], input='n\n'
        )

        assert result.exit_code == 0
        assert 'Deletion cancelled' in result.output

        # Verify seats are NOT deleted
        assert seat_service.count_seats_for_party(cli_party.id) == initial_count


def test_delete_seats_with_groups(
    cli_runner, admin_app, cli_party, cli_ticket_category, cli_seating_area
):
    """Test delete-seats command with seat groups."""
    with admin_app.app_context():
        # Create some test seats
        seat1 = seat_service.create_seat(
            cli_seating_area.id,
            110,
            120,
            cli_ticket_category.id,
            label='CLI-D1',
        )
        seat2 = seat_service.create_seat(
            cli_seating_area.id,
            130,
            140,
            cli_ticket_category.id,
            label='CLI-D2',
        )

        # Create a seat group
        group_result = seat_group_service.create_group(
            cli_party.id,
            cli_ticket_category.id,
            'CLI Test Group',
            [seat1, seat2],
        )
        assert group_result.is_ok()

        # Verify seats and groups exist
        assert seat_service.count_seats_for_party(cli_party.id) == 2
        assert seat_group_service.count_groups_for_party(cli_party.id) == 1

        # Run the delete command with force
        result = cli_runner.invoke(delete_seats, [str(cli_party.id), '--force'])

        assert result.exit_code == 0
        assert 'Seats to delete: 2' in result.output
        assert 'Seat groups to delete: 1' in result.output
        assert 'Successfully deleted 2 seat(s), 1 seat group(s)' in result.output

        # Verify seats and groups are deleted
        assert seat_service.count_seats_for_party(cli_party.id) == 0
        assert seat_group_service.count_groups_for_party(cli_party.id) == 0


def test_delete_seats_for_empty_party(
    cli_runner, admin_app, cli_party
):
    """Test delete-seats command for a party with no seats."""
    with admin_app.app_context():
        # Ensure party has no seats
        if seat_service.count_seats_for_party(cli_party.id) > 0:
            from byceps.services.seating import seat_deletion_service

            seat_deletion_service.delete_seats_for_party(cli_party.id)

        # Run the delete command
        result = cli_runner.invoke(delete_seats, [str(cli_party.id), '--force'])

        assert result.exit_code == 0
        assert 'No seats or seat groups found' in result.output


def test_delete_seats_with_occupied_seats(
    cli_runner,
    admin_app,
    cli_party,
    cli_ticket_category,
    cli_seating_area,
    make_user,
):
    """Test delete-seats command fails when seats are occupied."""
    with admin_app.app_context():
        from byceps.services.ticketing import (
            ticket_creation_service,
            ticket_seat_management_service,
        )

        # Create a user
        user = make_user()

        # Create a seat
        seat = seat_service.create_seat(
            cli_seating_area.id,
            150,
            160,
            cli_ticket_category.id,
            label='CLI-E1',
        )

        # Create a ticket and occupy the seat
        ticket = ticket_creation_service.create_ticket(
            cli_ticket_category, user
        )

        occupy_result = ticket_seat_management_service.occupy_seat(
            ticket.id, seat.id, user
        )
        assert occupy_result.is_ok()

        # Run the delete command
        result = cli_runner.invoke(delete_seats, [str(cli_party.id), '--force'])

        assert result.exit_code == 0
        assert 'Error:' in result.output
        assert 'occupied by tickets' in result.output

        # Verify seats still exist
        assert seat_service.count_seats_for_party(cli_party.id) == 1

        # Clean up: release the seat
        release_result = ticket_seat_management_service.release_seat(
            ticket.id, user
        )
        assert release_result.is_ok()
