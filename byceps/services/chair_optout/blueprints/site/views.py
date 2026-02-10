"""
byceps.services.chair_optout.blueprints.site.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from uuid import UUID

from flask import abort, g, request
from flask_babel import gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.ticketing import ticket_service
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_notice, flash_success
from byceps.util.framework.templating import templated
from byceps.util.views import login_required, redirect_to

from .forms import ChairOptoutForm


blueprint = create_blueprint('chair_optout', __name__)


@blueprint.get('/')
@login_required
@templated
def index(erroneous_form=None):
    """Show the chair opt-out page."""
    party = _get_current_party_or_404()

    tickets = ticket_service.get_tickets_used_by_user(g.user.id, party.id)
    if not tickets:
        abort(403)

    optouts_by_ticket_id = _get_optouts_by_ticket_id(party.id)

    form = erroneous_form if erroneous_form else ChairOptoutForm()
    form.ticket_ids.choices = _get_ticket_id_choices(tickets)

    if erroneous_form is None:
        form.ticket_ids.data = _get_selected_ticket_id_strings(
            tickets, optouts_by_ticket_id
        )

    selected_ticket_id_strings = set(form.ticket_ids.data or [])

    ticket_optouts = [
        _build_ticket_optout(ticket, selected_ticket_id_strings)
        for ticket in tickets
    ]

    return {
        'ticket_optouts': ticket_optouts,
        'form': form,
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

    form = ChairOptoutForm(request.form)
    form.ticket_ids.choices = _get_ticket_id_choices(tickets)
    if not form.validate():
        return index(form)

    try:
        selected_ticket_ids = {
            UUID(ticket_id) for ticket_id in (form.ticket_ids.data or [])
        }
    except ValueError:
        form.ticket_ids.errors.append(gettext('Ungültige Ticket-ID.'))
        return index(form)

    allowed_ticket_ids = {ticket.id for ticket in tickets}
    if not selected_ticket_ids.issubset(allowed_ticket_ids):
        form.ticket_ids.errors.append(gettext('Ungültige Ticket-ID.'))
        return index(form)

    changed = False
    toggleable_seat_count = 0

    for ticket in tickets:
        if ticket.occupied_seat is None:
            continue

        toggleable_seat_count += 1
        brings_own_chair = ticket.id in selected_ticket_ids
        existing_optout = optouts_by_ticket_id.get(str(ticket.id))

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
            changed = True

    if changed:
        flash_success(gettext('Änderungen gespeichert.'))
    elif toggleable_seat_count == 0:
        flash_notice(
            gettext('Keine Sitzplätze vorhanden - nichts zu speichern.')
        )
    else:
        flash_notice(gettext('Keine Änderungen.'))
    return redirect_to('.index')


def _build_ticket_optout(ticket, selected_ticket_id_strings):
    has_seat = ticket.occupied_seat is not None

    return {
        'id': ticket.id,
        'code': ticket.code,
        'seat_label': chair_optout_service.resolve_seat_label_for_ticket(
            ticket
        ),
        'has_seat': has_seat,
        'brings_own_chair': str(ticket.id) in selected_ticket_id_strings,
    }


def _get_optouts_by_ticket_id(party_id):
    optouts = chair_optout_service.list_optouts_for_party(
        party_id, only_true=False
    )
    return {str(optout.ticket_id): optout for optout in optouts}


def _get_ticket_id_choices(tickets):
    return [(str(ticket.id), ticket.code) for ticket in tickets]


def _get_selected_ticket_id_strings(tickets, optouts_by_ticket_id):
    selected_ticket_id_strings = []
    for ticket in tickets:
        optout = optouts_by_ticket_id.get(str(ticket.id))
        if (optout is not None) and optout.brings_own_chair:
            selected_ticket_id_strings.append(str(ticket.id))

    return selected_ticket_id_strings


def _get_current_party_or_404():
    party = g.party

    if party is None:
        abort(404)

    return party
