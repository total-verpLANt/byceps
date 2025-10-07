"""
byceps.cli.command.delete_seats
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Delete seats and seat groups for a party.

:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import click
from flask.cli import with_appcontext

from byceps.services.party.models import PartyID
from byceps.services.seating import (
    seat_deletion_service,
    seat_group_service,
    seat_service,
    seating_area_service,
)


@click.command()
@click.argument('party_id')
@click.option(
    '--force',
    is_flag=True,
    help='Skip confirmation prompt',
)
@with_appcontext
def delete_seats(party_id: PartyID, force: bool) -> None:
    """Delete all seats and seat groups for a party."""
    # Get statistics before deletion
    seat_count = seat_service.count_seats_for_party(party_id)
    seat_group_count = seat_group_service.count_groups_for_party(party_id)
    area_count = seating_area_service.count_areas_for_party(party_id)

    if seat_count == 0 and seat_group_count == 0:
        click.secho(
            f'No seats or seat groups found for party "{party_id}".',
            fg='yellow',
        )
        return

    # Display what will be deleted
    click.echo(f'\nParty: {party_id}')
    click.echo(f'Seating areas: {area_count}')
    click.echo(f'Seats to delete: {seat_count}')
    click.echo(f'Seat groups to delete: {seat_group_count}')

    # Confirm deletion unless --force is used
    if not force:
        click.echo()
        if not click.confirm(
            'Do you want to delete all seats and seat groups for this party?'
        ):
            click.secho('Deletion cancelled.', fg='yellow')
            return

    # Perform deletion
    click.echo('\nDeleting seats and seat groups...')
    result = seat_deletion_service.delete_seats_for_party(party_id)

    if result.is_err():
        error_message = result.unwrap_err()
        click.secho(f'\nError: {error_message}', fg='red')
        return

    # Display results
    deleted_counts = result.unwrap()
    click.echo()
    click.secho(
        f'Successfully deleted {deleted_counts["seats"]} seat(s), '
        f'{deleted_counts["seat_groups"]} seat group(s), and '
        f'{deleted_counts["seat_group_assignments"]} seat group assignment(s).',
        fg='green',
    )
