"""
byceps.services.chair_optout.blueprints.site.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask import abort, g, request
from flask_babel import gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.ticketing import ticket_service
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_success
from byceps.util.framework.templating import templated
from byceps.util.views import login_required, redirect_to


blueprint = create_blueprint('chair_optout', __name__)


@blueprint.get('/')
@login_required
@templated
def index():
    """Show the chair opt-out page."""
    party = _get_current_party_or_404()

    tickets = ticket_service.get_tickets_used_by_user(g.user.id, party.id)
    if not tickets:
        abort(403)

    optouts_by_ticket_id = _get_optouts_by_ticket_id(party.id)

    ticket_optouts = [
        _build_ticket_optout(ticket, optouts_by_ticket_id)
        for ticket in tickets
    ]

    return {
        'ticket_optouts': ticket_optouts,
    }


@blueprint.post('/')
@login_required
def update():
    """Update chair opt-out settings for the user's tickets."""
    party = _get_current_party_or_404()

    tickets = ticket_service.get_tickets_used_by_user(g.user.id, party.id)
    if not tickets:
        abort(403)

    optouts_by_ticket_id = _get_optouts_by_ticket_id(party.id)

    selected_ticket_ids = set(request.form.getlist('ticket_id'))

    for ticket in tickets:
        if ticket.occupied_seat is None:
            continue

        ticket_id_str = str(ticket.id)
        brings_own_chair = ticket_id_str in selected_ticket_ids
        existing_optout = optouts_by_ticket_id.get(ticket_id_str)

        if (existing_optout is None) and not brings_own_chair:
            continue

        if (existing_optout is None) or (
            existing_optout.brings_own_chair != brings_own_chair
        ):
            chair_optout_service.set_optout(
                party.id,
                ticket.id,
                g.user.id,
                brings_own_chair,
            )

    flash_success(gettext('Changes have been saved.'))
    return redirect_to('.index')


def _build_ticket_optout(ticket, optouts_by_ticket_id):
    optout = optouts_by_ticket_id.get(str(ticket.id))
    has_seat = ticket.occupied_seat is not None

    return {
        'id': ticket.id,
        'code': ticket.code,
        'seat_label': chair_optout_service.resolve_seat_label_for_ticket(
            ticket
        ),
        'has_seat': has_seat,
        'brings_own_chair': optout.brings_own_chair if optout else False,
    }


def _get_optouts_by_ticket_id(party_id):
    optouts = chair_optout_service.list_optouts_for_party(
        party_id, only_true=False
    )
    return {str(optout.ticket_id): optout for optout in optouts}


def _get_current_party_or_404():
    party = g.party

    if party is None:
        abort(404)

    return party
