"""
byceps.cli.command.delete_party
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Delete a party and all related data.

:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import click
from flask.cli import with_appcontext
from sqlalchemy import delete, select

from byceps.database import db
from byceps.services.brand.dbmodels import DbBrandCurrentParty
from byceps.services.guest_server.dbmodels import DbGuestServer
from byceps.services.orga_presence.dbmodels import DbTimeSlot
from byceps.services.orga_team import orga_team_service
from byceps.services.orga_team.dbmodels import DbMembership, DbOrgaTeam
from byceps.services.party import party_service
from byceps.services.party.models import PartyID
from byceps.services.seating import (
    seat_deletion_service,
    seat_group_service,
    seat_service,
    seating_area_service,
)
from byceps.services.seating.dbmodels.area import DbSeatingArea
from byceps.services.seating.dbmodels.reservation import (
    DbSeatReservationPrecondition,
)
from byceps.services.site.dbmodels import DbSite
from byceps.services.ticketing import ticket_category_service
from byceps.services.ticketing.dbmodels.archived_attendance import (
    DbArchivedAttendance,
)
from byceps.services.ticketing.dbmodels.category import DbTicketCategory
from byceps.services.timetable.dbmodels import DbTimetable, DbTimetableItem
from byceps.services.tourney.dbmodels.tourney import DbTourney
from byceps.services.tourney.dbmodels.tourney_category import DbTourneyCategory
from byceps.services.user_group.dbmodels import DbUserGroup
from byceps.services.whereabouts.dbmodels import DbWhereabouts


@click.command()
@click.argument('party_id')
@click.option(
    '--force',
    is_flag=True,
    help='Skip confirmation prompt',
)
@with_appcontext
def delete_party(party_id: PartyID, force: bool) -> None:
    """Delete a party and all related data."""
    party = party_service.find_party(party_id)

    if party is None:
        click.secho(f'Party "{party_id}" not found.', fg='red')
        return

    # Get statistics before deletion
    stats = _gather_statistics(party_id)

    if not _has_any_data(stats):
        click.secho(
            f'Party "{party_id}" has no related data.',
            fg='yellow',
        )
        _confirm_and_delete_party(party_id, party.title, force)
        return

    click.echo(f'\nParty: {party.title} ({party_id})')
    click.echo('=' * 60)
    _display_statistics(stats)

    blockers = _check_blockers(party_id, stats)
    if blockers:
        click.echo()
        click.secho('Cannot delete party due to the following issues:', fg='red')
        for blocker in blockers:
            click.echo(f'  - {blocker}')
        return

    if not force:
        click.echo()
        if not click.confirm(
            'Do you want to delete this party and all related data?'
        ):
            click.secho('Deletion cancelled.', fg='yellow')
            return

    click.echo('\nDeleting party and related data...')
    result = _delete_party_data(party_id)

    if result['success']:
        click.echo()
        click.secho('Successfully deleted:', fg='green')
        for key, count in sorted(result['counts'].items()):
            if count > 0:
                click.echo(f'  - {count} {key}')
    else:
        click.echo()
        click.secho(f'Error: {result["error"]}', fg='red')


def _gather_statistics(party_id: PartyID) -> dict[str, int]:
    """Gather statistics about party-related data."""
    return {
        'sites': _count_sites_for_party(party_id),
        'brand_current_parties': _count_brand_current_parties_for_party(
            party_id
        ),
        'seating_areas': seating_area_service.count_areas_for_party(party_id),
        'seats': seat_service.count_seats_for_party(party_id),
        'seat_groups': seat_group_service.count_groups_for_party(party_id),
        'seat_reservation_preconditions': _count_seat_reservation_preconditions_for_party(
            party_id
        ),
        'ticket_categories': ticket_category_service.count_categories_for_party(
            party_id
        ),
        'orga_teams': orga_team_service.count_teams_for_party(party_id),
        'orga_memberships': orga_team_service.count_memberships_for_party(
            party_id
        ),
        'orga_time_slots': _count_orga_time_slots_for_party(party_id),
        'timetables': _count_timetables_for_party(party_id),
        'timetable_items': _count_timetable_items_for_party(party_id),
        'tourney_categories': _count_tourney_categories_for_party(party_id),
        'tourneys': _count_tourneys_for_party(party_id),
        'user_groups': _count_user_groups_for_party(party_id),
        'whereabouts': _count_whereabouts_for_party(party_id),
        'guest_servers': _count_guest_servers_for_party(party_id),
        'archived_attendances': _count_archived_attendances_for_party(party_id),
    }


def _has_any_data(stats: dict[str, int]) -> bool:
    """Check if there is any related data."""
    return any(count > 0 for count in stats.values())


def _display_statistics(stats: dict[str, int]) -> None:
    """Display statistics about what will be deleted."""
    click.echo('Related data that will be deleted:')
    for key, count in sorted(stats.items()):
        if count > 0:
            click.echo(f'  - {key}: {count}')


def _check_blockers(party_id: PartyID, stats: dict[str, int]) -> list[str]:
    """Check for conditions that prevent deletion."""
    blockers = []

    # Sites cannot reference a party that's being deleted
    if stats['sites'] > 0:
        blockers.append(
            f'{stats["sites"]} site(s) reference this party. '
            'Please reassign or delete these sites first.'
        )

    # Ticket categories cannot be deleted if they have tickets
    if stats['ticket_categories'] > 0:
        ticket_count = _count_tickets_for_party(party_id)
        if ticket_count > 0:
            blockers.append(
                f'{ticket_count} ticket(s) exist for this party. '
                'Cannot delete party with existing tickets.'
            )

    # Seats cannot be deleted if occupied
    occupied_seats = seat_service.count_occupied_seats_for_party(party_id)
    if occupied_seats > 0:
        blockers.append(
            f'{occupied_seats} seat(s) are currently occupied. '
            'Please release these seats first.'
        )

    return blockers


def _count_sites_for_party(party_id: PartyID) -> int:
    """Count sites that reference this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbSite.id)).filter_by(party_id=party_id)
        )
        or 0
    )


def _count_orga_time_slots_for_party(party_id: PartyID) -> int:
    """Count orga time slots (presences and tasks) for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbTimeSlot.id)).filter_by(party_id=party_id)
        )
        or 0
    )


def _count_timetables_for_party(party_id: PartyID) -> int:
    """Count timetables for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbTimetable.id)).filter_by(party_id=party_id)
        )
        or 0
    )


def _count_timetable_items_for_party(party_id: PartyID) -> int:
    """Count timetable items for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbTimetableItem.id))
            .join(DbTimetable)
            .filter(DbTimetable.party_id == party_id)
        )
        or 0
    )


def _count_tourney_categories_for_party(party_id: PartyID) -> int:
    """Count tourney categories for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbTourneyCategory.id)).filter_by(
                party_id=party_id
            )
        )
        or 0
    )


def _count_user_groups_for_party(party_id: PartyID) -> int:
    """Count user groups for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbUserGroup.id)).filter_by(party_id=party_id)
        )
        or 0
    )


def _count_whereabouts_for_party(party_id: PartyID) -> int:
    """Count whereabouts records for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbWhereabouts.party_id)).filter_by(
                party_id=party_id
            )
        )
        or 0
    )


def _count_guest_servers_for_party(party_id: PartyID) -> int:
    """Count guest servers for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbGuestServer.id)).filter_by(
                party_id=party_id
            )
        )
        or 0
    )


def _count_archived_attendances_for_party(party_id: PartyID) -> int:
    """Count archived attendances for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbArchivedAttendance.user_id)).filter_by(
                party_id=party_id
            )
        )
        or 0
    )


def _count_brand_current_parties_for_party(party_id: PartyID) -> int:
    """Count brand current party entries for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbBrandCurrentParty.brand_id)).filter_by(
                party_id=party_id
            )
        )
        or 0
    )


def _count_seat_reservation_preconditions_for_party(party_id: PartyID) -> int:
    """Count seat reservation preconditions for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbSeatReservationPrecondition.id)).filter_by(
                party_id=party_id
            )
        )
        or 0
    )


def _count_tourneys_for_party(party_id: PartyID) -> int:
    """Count tourneys for this party."""
    return (
        db.session.scalar(
            select(db.func.count(DbTourney.id)).filter_by(party_id=party_id)
        )
        or 0
    )


def _count_tickets_for_party(party_id: PartyID) -> int:
    """Count total tickets (including revoked) for this party."""
    from byceps.services.ticketing.dbmodels.ticket import DbTicket

    ticket_category_ids = db.session.scalars(
        select(DbTicketCategory.id).filter_by(party_id=party_id)
    ).all()

    if not ticket_category_ids:
        return 0

    return (
        db.session.scalar(
            select(db.func.count(DbTicket.id)).filter(
                DbTicket.category_id.in_(ticket_category_ids)
            )
        )
        or 0
    )


def _confirm_and_delete_party(
    party_id: PartyID, party_title: str, force: bool
) -> None:
    """Delete party with no related data after confirmation."""
    if not force:
        click.echo()
        if not click.confirm(
            f'Do you want to delete party "{party_title}"?'
        ):
            click.secho('Deletion cancelled.', fg='yellow')
            return

    party_service.delete_party(party_id)
    click.echo()
    click.secho(f'Party "{party_title}" has been deleted.', fg='green')


def _delete_party_data(party_id: PartyID) -> dict:
    """Delete all party-related data."""
    counts = {}

    try:
        # Delete brand current party entries (no dependencies)
        brand_current_parties_count = db.session.execute(
            delete(DbBrandCurrentParty).filter_by(party_id=party_id)
        ).rowcount
        counts['brand_current_parties'] = brand_current_parties_count

        # Delete whereabouts
        whereabouts_count = db.session.execute(
            delete(DbWhereabouts).filter_by(party_id=party_id)
        ).rowcount
        counts['whereabouts'] = whereabouts_count

        # Delete guest servers
        guest_servers_count = db.session.execute(
            delete(DbGuestServer).filter_by(party_id=party_id)
        ).rowcount
        counts['guest_servers'] = guest_servers_count

        # Delete archived attendances
        archived_attendances_count = db.session.execute(
            delete(DbArchivedAttendance).filter_by(party_id=party_id)
        ).rowcount
        counts['archived_attendances'] = archived_attendances_count

        # Delete timetable items and timetables
        # First get timetable IDs for this party
        timetable_ids = db.session.scalars(
            select(DbTimetable.id).filter_by(party_id=party_id)
        ).all()

        timetable_items_count = 0
        if timetable_ids:
            timetable_items_count = db.session.execute(
                delete(DbTimetableItem).filter(
                    DbTimetableItem.timetable_id.in_(timetable_ids)
                )
            ).rowcount

        timetables_count = db.session.execute(
            delete(DbTimetable).filter_by(party_id=party_id)
        ).rowcount

        counts['timetable_items'] = timetable_items_count
        counts['timetables'] = timetables_count

        # Delete tourneys FIRST (before categories they reference)
        tourneys_count = db.session.execute(
            delete(DbTourney).filter_by(party_id=party_id)
        ).rowcount
        counts['tourneys'] = tourneys_count

        # Delete tourney categories (after tourneys)
        tourney_categories_count = db.session.execute(
            delete(DbTourneyCategory).filter_by(party_id=party_id)
        ).rowcount
        counts['tourney_categories'] = tourney_categories_count

        # Delete user groups
        user_groups_count = db.session.execute(
            delete(DbUserGroup).filter_by(party_id=party_id)
        ).rowcount
        counts['user_groups'] = user_groups_count

        # Delete orga time slots (presences and tasks)
        orga_time_slots_count = db.session.execute(
            delete(DbTimeSlot).filter_by(party_id=party_id)
        ).rowcount
        counts['orga_time_slots'] = orga_time_slots_count

        # Delete orga team memberships and teams
        orga_teams = db.session.scalars(
            select(DbOrgaTeam).filter_by(party_id=party_id)
        ).all()

        memberships_count = 0
        for team in orga_teams:
            memberships_count += db.session.execute(
                delete(DbMembership).filter_by(orga_team_id=team.id)
            ).rowcount

        teams_count = db.session.execute(
            delete(DbOrgaTeam).filter_by(party_id=party_id)
        ).rowcount

        counts['orga_memberships'] = memberships_count
        counts['orga_teams'] = teams_count

        # Delete seat reservation preconditions (before seating areas)
        preconditions_count = db.session.execute(
            delete(DbSeatReservationPrecondition).filter_by(party_id=party_id)
        ).rowcount
        counts['seat_reservation_preconditions'] = preconditions_count

        # Delete seats, seat groups
        seats_result = seat_deletion_service.delete_seats_for_party(party_id)
        if seats_result.is_err():
            return {
                'success': False,
                'error': seats_result.unwrap_err(),
                'counts': {},
            }

        seat_counts = seats_result.unwrap()
        counts['seats'] = seat_counts.get('seats', 0)
        counts['seat_groups'] = seat_counts.get('seat_groups', 0)
        counts['seat_group_assignments'] = seat_counts.get(
            'seat_group_assignments', 0
        )

        # Delete seating areas (after seats)
        areas_count = db.session.execute(
            delete(DbSeatingArea).filter_by(party_id=party_id)
        ).rowcount
        counts['seating_areas'] = areas_count

        # Delete ticket categories
        ticket_categories_count = db.session.execute(
            delete(DbTicketCategory).filter_by(party_id=party_id)
        ).rowcount
        counts['ticket_categories'] = ticket_categories_count

        # Finally, delete the party itself (this also deletes party settings via cascade)
        party_service.delete_party(party_id)
        counts['parties'] = 1

        db.session.commit()

        return {
            'success': True,
            'error': None,
            'counts': counts,
        }

    except Exception as e:
        db.session.rollback()
        return {
            'success': False,
            'error': str(e),
            'counts': {},
        }
