"""
byceps.services.pizza_delivery.blueprints.site.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask import abort, g, redirect, request, url_for
from flask_babel import gettext

from byceps.services.pizza_delivery import pizza_delivery_service
from byceps.services.pizza_delivery.models import PizzaDeliveryStatus
from byceps.services.pizza_delivery.errors import (
    PizzaDeliveryEntryAlreadyClaimedError,
    PizzaDeliveryNumberNotFoundError,
)
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_error, flash_success
from byceps.util.framework.templating import templated
from byceps.util.views import login_required


blueprint = create_blueprint('pizza_delivery', __name__)


@blueprint.get('/')
@templated
def board():
    """Show the public pizza delivery board for the current party."""
    if not g.party:
        abort(404)

    entries = pizza_delivery_service.get_entries_for_party(
        g.party.id, status=PizzaDeliveryStatus.DELIVERED
    )

    current_user_entry = None
    if g.user.authenticated:
        for entry in entries:
            if entry.user_id == g.user.id:
                current_user_entry = entry
                break

    return {
        'entries': entries,
        'current_user_entry': current_user_entry,
    }


@blueprint.get('/beamershow')
@templated
def beamershow():
    """Minimal delivered-numbers display for projector use."""
    if not g.party:
        abort(404)

    entries = pizza_delivery_service.get_entries_for_party(
        g.party.id, status=PizzaDeliveryStatus.DELIVERED
    )

    return {
        'entries': entries,
    }


@blueprint.get('/my-status')
@login_required
@templated
def my_status():
    """Show the current user's pizza delivery status."""
    if not g.party:
        abort(404)

    entries = pizza_delivery_service.get_claimed_entries_for_user(
        g.user.id, g.party.id
    )

    return {
        'entries': entries,
        'party': g.party,
    }


@blueprint.post('/claim')
@login_required
def claim():
    """Claim a pizza delivery entry by number."""
    if not g.party:
        abort(404)

    number = request.form.get('number', '').strip()
    if not number:
        flash_error(gettext('Please enter a pizza number.'))
        return redirect(url_for('.my_status'))

    if len(number) > 20:
        flash_error(gettext('Pizza number is too long.'))
        return redirect(url_for('.my_status'))

    result = pizza_delivery_service.claim_entry_by_number(
        g.party.id, number, g.user.id, initiator=g.user
    )

    if result.is_ok():
        entry = result.unwrap()
        flash_success(
            gettext('Pizza #%(number)s claimed.', number=entry.number)
        )
    else:
        error = result.unwrap_err()
        if isinstance(error, PizzaDeliveryNumberNotFoundError):
            flash_error(gettext('Pizza number not found.'))
        elif isinstance(error, PizzaDeliveryEntryAlreadyClaimedError):
            flash_error(gettext('Pizza number already claimed.'))

    return redirect(url_for('.my_status'))
