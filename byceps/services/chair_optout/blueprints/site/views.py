"""
byceps.services.chair_optout.blueprints.site.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from uuid import UUID

from flask import g, request

from byceps.services.chair_optout import chair_optout_service
from byceps.services.chair_optout.chair_setting_service import (
    is_rental_selection_enabled,
)
from byceps.services.chair_optout.presentation import get_chair_source_label
from byceps.services.ticketing import ticket_service
from byceps.services.ticketing.models.ticket import TicketID
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.views import login_required, redirect_to

from .request_hooks import register_request_hooks


blueprint = create_blueprint('chair_optout', __name__)
register_request_hooks(blueprint)
blueprint.add_app_template_global(get_chair_source_label, 'chair_source_label')
blueprint.add_app_template_global(
    is_rental_selection_enabled, 'is_chair_rental_selection_enabled'
)


@blueprint.app_template_global()
def get_pending_chair_ticket_ids() -> list[TicketID]:
    """Return the current user's editable, unanswered participant tickets."""
    if (
        not g.user.authenticated
        or g.party is None
        or not g.party.ticket_management_enabled
    ):
        return []

    cache_key = 'byceps.pending_chair_ticket_ids'
    if cache_key not in request.environ:
        request.environ[cache_key] = (
            chair_optout_service.get_pending_chair_ticket_ids_for_user(
                g.party.id, g.user.id
            )
        )
    return request.environ[cache_key]


@blueprint.app_template_global()
def find_first_unanswered_chair_ticket_id() -> TicketID | None:
    ticket_ids = get_pending_chair_ticket_ids()
    return ticket_ids[0] if ticket_ids else None


@blueprint.app_template_global()
def can_edit_chair_information(ticket) -> bool:
    return (
        g.user.authenticated
        and g.party is not None
        and g.party.ticket_management_enabled
        and ticket is not None
        and ticket.party_id == g.party.id
        and (
            ticket.is_used_by(g.user.id) or ticket.is_user_managed_by(g.user.id)
        )
        and not ticket.revoked
        and not ticket.user_checked_in
    )


@blueprint.get('/')
@login_required
def index():
    """Redirect legacy chair links to the participant's ticket list."""
    anchor = None
    ticket_id_arg = request.args.get('ticket_id')
    if ticket_id_arg and g.party is not None:
        try:
            ticket_id = TicketID(UUID(ticket_id_arg))
        except ValueError:
            pass
        else:
            ticket = ticket_service.find_ticket(ticket_id)
            if (
                ticket is not None
                and ticket.party_id == g.party.id
                and (
                    ticket.is_used_by(g.user.id)
                    or ticket.is_user_managed_by(g.user.id)
                )
                and not ticket.revoked
            ):
                anchor = f'ticket-{ticket.id}'

    return redirect_to('ticketing.index_mine', _anchor=anchor)
