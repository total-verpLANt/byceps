"""
byceps.services.chair_optout.blueprints.site.request_hooks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from flask import abort, Blueprint, g, request

from byceps.services.chair_optout.chair_access_service import (
    lock_participant_ticket,
)
from byceps.services.chair_optout.chair_setting_service import (
    is_rental_selection_enabled,
)
from byceps.services.ticketing import ticket_service


def register_request_hooks(blueprint: Blueprint) -> None:
    blueprint.before_app_request(_guard_chair_source_update)


def _guard_chair_source_update() -> None:
    if (
        request.method != 'POST'
        or request.endpoint != 'ticketing.set_chair_source'
    ):
        return

    if not g.user.authenticated:
        # Let Core's login_required retain the regular login redirect.
        return
    if g.party is None or not g.party.ticket_management_enabled:
        abort(403)

    ticket_id = request.view_args['ticket_id']
    ticket = lock_participant_ticket(g.party.id, ticket_id, g.user.id)
    if ticket is None:
        ticket = ticket_service.find_ticket(ticket_id)
        if ticket is None or ticket.revoked:
            abort(404)
        abort(403)

    chair_source = request.view_args['chair_source']
    if chair_source == 'rental' and not is_rental_selection_enabled(g.party.id):
        abort(400)
