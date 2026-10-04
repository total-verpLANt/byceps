"""
byceps.services.chair_optout.blueprints.site.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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

from .forms import ChairInformationForm


blueprint = create_blueprint('chair_optout', __name__)


@blueprint.get('/')
@login_required
@templated
def index(erroneous_ticket_id=None, erroneous_form=None):
    """Show chair information for the current user's tickets."""
    party = _get_current_party_or_404()

    tickets = ticket_service.get_tickets_used_by_user(g.user.id, party.id)
    if not tickets:
        abort(403)

    optouts_by_ticket_id = chair_optout_service.get_current_optouts_for_tickets(
        tickets
    )
    ticket_information = [
        _build_ticket_information(
            ticket,
            optouts_by_ticket_id.get(ticket.id),
            erroneous_form if ticket.id == erroneous_ticket_id else None,
        )
        for ticket in tickets
    ]

    return {
        'ticket_information': ticket_information,
    }


@blueprint.post('/<uuid:ticket_id>')
@login_required
def update(ticket_id):
    """Update chair information for one currently used ticket."""
    party = _get_current_party_or_404()

    ticket = ticket_service.find_ticket(ticket_id)
    if not _is_ticket_editable_by_user(ticket, party.id, g.user.id):
        abort(403)

    form = ChairInformationForm(request.form, prefix=str(ticket_id))
    if not form.validate():
        return index(ticket_id, form)

    brings_own_chair = form.choice.data == 'own'
    try:
        chair_optout_service.set_optout(
            party.id, ticket.id, g.user.id, brings_own_chair
        )
    except ValueError:
        abort(403)

    flash_success(gettext('Changes have been saved.'))
    return redirect_to('.index', _anchor=f'ticket-{ticket.id}')


def _build_ticket_information(ticket, optout, erroneous_form):
    if erroneous_form is not None:
        form = erroneous_form
    else:
        choice = None
        if optout is not None:
            choice = 'own' if optout.brings_own_chair else 'provided'
        form = ChairInformationForm(
            prefix=str(ticket.id), data={'choice': choice}
        )

    return {
        'ticket': ticket,
        'seat_label': chair_optout_service.resolve_seat_label_for_ticket(
            ticket
        ),
        'brings_own_chair': (
            optout.brings_own_chair if optout is not None else None
        ),
        'form': form,
    }


def _is_ticket_editable_by_user(ticket, party_id, user_id) -> bool:
    return (
        ticket is not None
        and ticket.party_id == party_id
        and not ticket.revoked
        and ticket.used_by_id == user_id
    )


def _get_current_party_or_404():
    party = g.party

    if party is None:
        abort(404)

    return party
