"""
byceps.services.chair_optout.blueprints.admin.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask import abort

from byceps.services.chair_optout import chair_optout_service
from byceps.services.party import party_service
from byceps.services.party.models import Party
from byceps.util.export import serialize_tuples_to_csv
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.templating import templated
from byceps.util.views import permission_required, textified


blueprint = create_blueprint('chair_optout_admin', __name__)


@blueprint.get('/for_party/<party_id>')
@permission_required('chair_optout.view_report')
@templated
def index(party_id):
    """Show the chair opt-out report for a party."""
    party = _get_party_or_404(party_id)

    report_entries = chair_optout_service.get_report_entries_for_party(
        party.id
    )

    return {
        'party': party,
        'report_entries': report_entries,
    }


@blueprint.get('/for_party/<party_id>/export.csv')
@permission_required('chair_optout.export_report')
@textified
def export_as_csv(party_id):
    """Export the chair opt-out report for a party as CSV."""
    party = _get_party_or_404(party_id)

    report_entries = chair_optout_service.get_report_entries_for_party(
        party.id
    )

    header_row = (
        'Name',
        'Nickname',
        'Ticketnummer',
        'Sitzplatz-Label',
    )

    data_rows = [
        (
            entry.full_name or '',
            entry.screen_name or '',
            entry.ticket_code,
            entry.seat_label or '',
        )
        for entry in report_entries
    ]

    rows = [header_row] + data_rows

    return serialize_tuples_to_csv(rows)


def _get_party_or_404(party_id) -> Party:
    party = party_service.find_party(party_id)

    if party is None:
        abort(404)

    return party
