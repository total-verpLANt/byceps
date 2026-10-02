"""
byceps.services.chair_optout.blueprints.admin.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from urllib.parse import quote

from flask import abort, g, request
from flask_babel import gettext

from byceps.services.chair_optout import (
    chair_optout_service,
    chair_setting_service,
)
from byceps.services.chair_optout.presentation import get_chair_source_label
from byceps.services.party import party_service, party_setting_service
from byceps.services.party.models import Party, PartyID
from byceps.services.seating import seating_area_service, seat_service
from byceps.services.site import site_service
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.util.export import serialize_tuples_to_csv
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_success
from byceps.util.framework.templating import templated
from byceps.util.templating import SITES_PATH
from byceps.util.views import permission_required, redirect_to, textified

from .forms import RentalSelectionForm


blueprint = create_blueprint('chair_optout_admin', __name__)
blueprint.add_app_template_global(get_chair_source_label, 'chair_source_label')

_VALID_FILTERS = frozenset(
    {
        'all',
        'own_chair',
        'provided_chair',
        'rented_chair',
        'not_specified',
        'no_seat',
    }
)


@blueprint.get('/for_party/<party_id>')
@permission_required('seating.view')
@templated('admin/chair_optout/seat_management')
def index(party_id):
    """Show the seat management landing page for a party."""
    party = _get_party_or_404(party_id)

    return {'party': party}


@blueprint.get('/for_party/<party_id>/chair_information')
@permission_required('seating.view')
@templated('admin/chair_optout/index')
def chair_information(party_id):
    """Show the participant chair information list for a party."""
    party = _get_party_or_404(party_id)
    report_entries = chair_optout_service.get_report_entries_for_party(party.id)
    summary = chair_optout_service.summarize_report_entries(report_entries)
    selected_filter = _get_selected_filter()
    filtered_report_entries = _filter_report_entries(
        report_entries, selected_filter
    )
    site_server_name = _find_site_server_name_for_party(party)
    seat_urls_by_ticket_id = _build_seat_urls_by_ticket_id(
        filtered_report_entries, site_server_name
    )

    return {
        'party': party,
        'report_entries': filtered_report_entries,
        'summary': summary,
        'selected_filter': selected_filter,
        'seat_urls_by_ticket_id': seat_urls_by_ticket_id,
        'show_rental_information': chair_setting_service.should_show_rental_information(
            party.id, has_rented_chair=summary.rented_chair > 0
        ),
    }


@blueprint.get('/for_party/<party_id>/chair_information/rental_selection')
@permission_required('seating.view')
@templated('admin/chair_optout/rental_selection')
def rental_selection(party_id):
    """Show the party-local rental selection setting."""
    party = _get_party_or_404(party_id)
    enabled = chair_setting_service.is_rental_selection_enabled(party.id)
    form = (
        RentalSelectionForm(data={'enabled': 'true' if enabled else 'false'})
        if g.user.has_permission('party.update')
        else None
    )

    return {
        'party': party,
        'rental_selection_enabled': enabled,
        'rental_selection_form': form,
        'selected_filter': _get_selected_filter(),
    }


@blueprint.post('/for_party/<party_id>/chair_information/rental_selection')
@permission_required('party.update')
def update_rental_selection(party_id):
    """Enable or disable new rental selections for this party."""
    party = _get_party_or_404(party_id)
    form = RentalSelectionForm(request.form)
    if not form.validate():
        abort(400)

    chair_setting_service.set_rental_selection_enabled(
        party.id, form.enabled.data == 'true'
    )
    flash_success(gettext('Rental chair selection has been updated.'))
    return redirect_to('.rental_selection', party_id=party.id)


@blueprint.get('/for_party/<party_id>/chair_information/seating_plan')
@permission_required('seating.view')
@templated('admin/chair_optout/seating_plan')
def chair_information_seating_plan(party_id):
    """Show chair information on the party's graphical seating plans."""
    party = _get_party_or_404(party_id)
    chair_sources = chair_optout_service.get_chair_sources_for_party(party.id)
    areas_with_seats = [
        (area, seat_service.get_area_seats(area.id))
        for area in seating_area_service.get_areas_for_party(party.id)
    ]
    selected_filter = _get_selected_filter(include_no_seat=False)
    matching_ticket_ids = {
        ticket_id
        for ticket_id, source in chair_sources.items()
        if _matches_filter(source, True, selected_filter)
    }

    return {
        'party': party,
        'areas_with_seats': areas_with_seats,
        'chair_sources_by_ticket_id': chair_sources,
        'matching_ticket_ids': matching_ticket_ids,
        'show_rental_information': chair_setting_service.should_show_rental_information(
            party.id,
            has_rented_chair=ChairSource.rental in chair_sources.values(),
        ),
        'seat_stylesheet_site_id': _find_seat_stylesheet_site_id(party.id),
        'selected_filter': selected_filter,
    }


@blueprint.get('/for_party/<party_id>/export.csv')
@permission_required('seating.view')
@textified
def export_as_csv(party_id):
    """Export the chair opt-out report for a party as CSV."""
    party = _get_party_or_404(party_id)

    report_entries = chair_optout_service.get_report_entries_for_party(party.id)

    header_row = (
        gettext('Name'),
        gettext('Nickname'),
        gettext('Ticket number'),
        gettext('Seat label'),
        gettext('Chair information'),
    )

    data_rows = [
        (
            entry.full_name or '',
            entry.screen_name or '',
            entry.ticket_code,
            (entry.seat_label or gettext('unnamed'))
            if entry.has_seat
            else gettext('no seat'),
            get_chair_source_label(entry.chair_source),
        )
        for entry in report_entries
    ]

    rows = [
        tuple(_escape_csv_cell(value) for value in row)
        for row in [header_row, *data_rows]
    ]

    return serialize_tuples_to_csv(rows)


def _escape_csv_cell(value: str) -> str:
    return (
        "'" + value
        if value.startswith(('=', '+', '-', '@', '\t', '\r'))
        else value
    )


def _get_selected_filter(*, include_no_seat: bool = True) -> str:
    selected_filter = request.args.get('filter', 'all')
    if not include_no_seat and selected_filter == 'no_seat':
        return 'all'
    return selected_filter if selected_filter in _VALID_FILTERS else 'all'


def _filter_report_entries(report_entries, selected_filter: str):
    return [
        entry
        for entry in report_entries
        if _matches_filter(entry.chair_source, entry.has_seat, selected_filter)
    ]


def _matches_filter(
    source: ChairSource, has_seat: bool, selected_filter: str
) -> bool:
    match selected_filter:
        case 'own_chair':
            return source is ChairSource.user
        case 'provided_chair':
            return source is ChairSource.venue
        case 'rented_chair':
            return source is ChairSource.rental
        case 'not_specified':
            return source is ChairSource.unknown
        case 'no_seat':
            return not has_seat
        case _:
            return True


def _find_seat_stylesheet_site_id(party_id: PartyID) -> str | None:
    site_id = party_setting_service.find_setting_value(
        party_id, 'primary_party_site_id'
    )
    if site_id is not None:
        return site_id if _seat_stylesheet_exists(site_id) else None

    site_ids = [
        site.id
        for site in site_service.get_all_sites()
        if site.party_id == party_id and _seat_stylesheet_exists(site.id)
    ]
    return site_ids[0] if len(site_ids) == 1 else None


def _seat_stylesheet_exists(site_id: str) -> bool:
    if (SITES_PATH / site_id).name != site_id:
        return False

    sites_path = SITES_PATH.resolve()
    stylesheet_path = (
        SITES_PATH / site_id / 'static/style/seating.css'
    ).resolve()
    if not stylesheet_path.is_relative_to(sites_path):
        return False

    return stylesheet_path.is_file()


def _find_site_server_name_for_party(party: Party) -> str | None:
    sites = [
        site
        for site in site_service.get_current_sites(party.brand_id)
        if site.party_id == party.id
    ]
    primary_site_id = party_setting_service.find_setting_value(
        party.id, 'primary_party_site_id'
    )
    if primary_site_id is not None:
        return next(
            (site.server_name for site in sites if site.id == primary_site_id),
            None,
        )

    return sites[0].server_name if len(sites) == 1 else None


def _build_seat_urls_by_ticket_id(report_entries, site_server_name):
    if site_server_name is None:
        return {}

    return {
        entry.ticket_id: (
            f'https://{site_server_name}/seating/areas/'
            f'{quote(entry.seat_area_slug, safe="")}#seat-{entry.seat_id}'
        )
        for entry in report_entries
        if entry.seat_id is not None and entry.seat_area_slug is not None
    }


def _get_party_or_404(party_id) -> Party:
    party = party_service.find_party(party_id)

    if party is None:
        abort(404)

    return party
