"""
byceps.services.chair_optout.blueprints.admin.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from urllib.parse import quote

from flask import abort, request
from flask_babel import gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.party import party_service
from byceps.services.party.models import Party
from byceps.services.seating import seating_area_service, seat_service
from byceps.services.site import site_service
from byceps.util.export import serialize_tuples_to_csv
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.templating import templated
from byceps.util.views import permission_required, textified


blueprint = create_blueprint('chair_optout_admin', __name__)

_VALID_FILTERS = frozenset(
    {'all', 'own_chair', 'provided_chair', 'not_specified', 'no_seat'}
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
    }


@blueprint.get('/for_party/<party_id>/chair_information/seating_plan')
@permission_required('seating.view')
@templated('admin/chair_optout/seating_plan')
def chair_information_seating_plan(party_id):
    """Show chair information on the party's graphical seating plans."""
    party = _get_party_or_404(party_id)
    report_entries = chair_optout_service.get_report_entries_for_party(party.id)
    areas_with_seats = [
        (area, seat_service.get_area_seats(area.id))
        for area in seating_area_service.get_areas_for_party(party.id)
    ]
    chair_information_by_ticket_id = {
        entry.ticket_id: entry.brings_own_chair for entry in report_entries
    }

    return {
        'party': party,
        'areas_with_seats': areas_with_seats,
        'chair_information_by_ticket_id': chair_information_by_ticket_id,
        'selected_filter': _get_selected_filter(),
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
            entry.seat_label or gettext('no seat'),
            _get_status_label(entry.brings_own_chair),
        )
        for entry in report_entries
    ]

    rows = [header_row] + data_rows

    return serialize_tuples_to_csv(rows)


def _get_status_label(brings_own_chair: bool | None) -> str:
    if brings_own_chair is True:
        return gettext('Brings own chair')
    if brings_own_chair is False:
        return gettext('Needs a provided chair')
    return gettext('Not specified yet')


def _get_selected_filter() -> str:
    selected_filter = request.args.get('filter', 'all')
    return selected_filter if selected_filter in _VALID_FILTERS else 'all'


def _filter_report_entries(report_entries, selected_filter: str):
    match selected_filter:
        case 'own_chair':
            return [
                entry
                for entry in report_entries
                if entry.brings_own_chair is True
            ]
        case 'provided_chair':
            return [
                entry
                for entry in report_entries
                if entry.brings_own_chair is False
            ]
        case 'not_specified':
            return [
                entry
                for entry in report_entries
                if entry.brings_own_chair is None
            ]
        case 'no_seat':
            return [entry for entry in report_entries if not entry.has_seat]
        case _:
            return list(report_entries)


def _find_site_server_name_for_party(party: Party) -> str | None:
    sites = [
        site
        for site in site_service.get_current_sites(party.brand_id)
        if site.party_id == party.id
    ]
    if not sites:
        return None

    return min(site.server_name for site in sites)


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
