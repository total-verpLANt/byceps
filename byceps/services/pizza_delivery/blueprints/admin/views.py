"""
byceps.services.pizza_delivery.blueprints.admin.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask import abort, g, request, url_for
from flask_babel import gettext

from byceps.services.brand import brand_service
from byceps.services.more.blueprints.admin import item_service
from byceps.services.more.blueprints.admin.item_service import MoreItem
from byceps.services.party import party_service
from byceps.services.party.models import PartyID
from byceps.services.pizza_delivery import (
    pizza_delivery_email_service,
    pizza_delivery_service,
)
from byceps.services.pizza_delivery.models import PizzaDeliveryEntryID
from byceps.services.user import user_service
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import flash_error, flash_notice, flash_success
from byceps.util.framework.templating import templated
from byceps.util.views import (
    permission_required,
    redirect_to,
    respond_no_content,
)

from .forms import CreateEntryForm, UpdateEntryForm


blueprint = create_blueprint('pizza_delivery_admin', __name__)


# --- Monkey-patch "More" party items to include Pizza Delivery ---
if not getattr(item_service.get_party_items, '_pizza_delivery_patched', False):
    _original_get_party_items = item_service.get_party_items

    def _get_party_items_with_pizza_delivery(party):
        items = _original_get_party_items(party)
        items.append(
            MoreItem(
                label=gettext('Pizza Delivery'),
                icon='shipping',
                url=url_for(
                    'pizza_delivery_admin.index_for_party',
                    party_id=party.id,
                ),
                required_permission='ticketing.checkin',
            )
        )
        return items

    _get_party_items_with_pizza_delivery._pizza_delivery_patched = True
    item_service.get_party_items = _get_party_items_with_pizza_delivery


@blueprint.get('/parties/<party_id>')
@permission_required('ticketing.checkin')
@templated
def index_for_party(party_id):
    """List pizza delivery entries for that party."""
    party = _get_party_or_404(party_id)

    entries = pizza_delivery_service.get_entries_for_party(party.id)

    # Resolve usernames for linked entries.
    user_ids = [e.created_by_id for e in entries]
    user_ids.extend(e.user_id for e in entries if e.user_id is not None)
    users_by_id = user_service.get_users_indexed_by_id(
        set(user_ids), include_avatars=False
    )

    brand = brand_service.get_brand(party.brand_id)
    email_templates_configured = (
        pizza_delivery_email_service.email_templates_exist(brand)
    )

    return {
        'party': party,
        'entries': entries,
        'users_by_id': users_by_id,
        'email_templates_configured': email_templates_configured,
    }


@blueprint.get('/parties/<party_id>/create')
@permission_required('ticketing.checkin')
@templated
def create_form(party_id, erroneous_form=None):
    """Show form to create a pizza delivery entry."""
    party = _get_party_or_404(party_id)

    form = erroneous_form if erroneous_form else CreateEntryForm()

    return {
        'party': party,
        'form': form,
    }


@blueprint.post('/parties/<party_id>')
@permission_required('ticketing.checkin')
def create(party_id):
    """Create a pizza delivery entry."""
    party = _get_party_or_404(party_id)

    form = CreateEntryForm(request.form)
    if not form.validate():
        return create_form(party.id, form)

    number = form.number.data.strip()
    username = form.username.data.strip() if form.username.data else None

    user_id = None
    if username:
        user = user_service.find_user_by_screen_name(username)
        if user is None:
            flash_error(gettext('User "%(username)s" not found.', username=username))
            return create_form(party.id, form)
        user_id = user.id

    result = pizza_delivery_service.create_entry(
        party.id, number, user_id, g.user.id, initiator=g.user
    )

    if result.is_err():
        flash_error(
            gettext(
                'Delivery number "%(number)s" already exists for this party.',
                number=number,
            )
        )
        return create_form(party.id, form)

    flash_success(gettext('Delivery number "%(number)s" has been registered.', number=number))

    return redirect_to('.index_for_party', party_id=party.id)


@blueprint.get('/entries/<uuid:entry_id>/edit')
@permission_required('ticketing.checkin')
@templated
def edit_form(entry_id, erroneous_form=None):
    """Show form to edit a pizza delivery entry."""
    entry = pizza_delivery_service.find_entry(PizzaDeliveryEntryID(entry_id))
    if entry is None:
        abort(404)

    party = _get_party_or_404(entry.party_id)

    if erroneous_form:
        form = erroneous_form
    else:
        # Pre-fill username if user is linked.
        username = None
        if entry.user_id is not None:
            user = user_service.find_user(entry.user_id)
            if user is not None:
                username = user.screen_name
        form = UpdateEntryForm(data={'username': username})

    return {
        'party': party,
        'entry': entry,
        'form': form,
    }


@blueprint.post('/entries/<uuid:entry_id>/update')
@permission_required('ticketing.checkin')
def update(entry_id):
    """Update a pizza delivery entry."""
    entry = pizza_delivery_service.find_entry(PizzaDeliveryEntryID(entry_id))
    if entry is None:
        abort(404)

    form = UpdateEntryForm(request.form)
    if not form.validate():
        return edit_form(entry_id, form)

    username = form.username.data.strip() if form.username.data else None

    user_id = None
    if username:
        user = user_service.find_user_by_screen_name(username)
        if user is None:
            flash_error(
                gettext('User "%(username)s" not found.', username=username)
            )
            return edit_form(entry_id, form)
        user_id = user.id

    result = pizza_delivery_service.update_entry_user(
        PizzaDeliveryEntryID(entry_id), user_id, initiator=g.user
    )

    if result.is_err():
        abort(404)

    flash_success(
        gettext(
            'Delivery number "%(number)s" has been updated.',
            number=entry.number,
        )
    )

    return redirect_to('.index_for_party', party_id=entry.party_id)


@blueprint.delete('/entries/<uuid:entry_id>')
@permission_required('ticketing.checkin')
@respond_no_content
def delete(entry_id):
    """Delete a pizza delivery entry."""
    result = pizza_delivery_service.delete_entry(
        PizzaDeliveryEntryID(entry_id), initiator=g.user
    )

    if result.is_err():
        abort(404)

    flash_success(gettext('Entry has been deleted.'))


@blueprint.post('/entries/<uuid:entry_id>/deliver')
@permission_required('ticketing.checkin')
@respond_no_content
def deliver(entry_id):
    """Mark a pizza delivery entry as delivered."""
    result = pizza_delivery_service.deliver_entry(
        PizzaDeliveryEntryID(entry_id), initiator=g.user
    )

    if result.is_err():
        flash_error(gettext('Could not mark entry as delivered.'))
        return

    entry = result.unwrap()
    flash_success(
        gettext(
            'Pizza #%(number)s marked as delivered.',
            number=entry.number,
        )
    )


@blueprint.post('/entries/<uuid:entry_id>/undeliver')
@permission_required('ticketing.checkin')
@respond_no_content
def undeliver(entry_id):
    """Revert a pizza delivery entry to pending."""
    result = pizza_delivery_service.undeliver_entry(
        PizzaDeliveryEntryID(entry_id), initiator=g.user
    )

    if result.is_err():
        flash_error(gettext('Could not revert entry.'))
        return

    entry = result.unwrap()
    flash_success(
        gettext(
            'Pizza #%(number)s reverted to pending.',
            number=entry.number,
        )
    )


@blueprint.post('/parties/<party_id>/setup_email_templates')
@permission_required('ticketing.checkin')
def setup_email_templates(party_id):
    """Create default pizza delivery email snippets for the party's brand."""
    party = _get_party_or_404(party_id)
    brand = brand_service.get_brand(party.brand_id)
    current_user = g.user

    created = pizza_delivery_email_service.create_pizza_delivery_email_snippets(
        brand, current_user,
    )
    if created:
        flash_success(
            gettext('Pizza delivery email templates created.')
        )
    else:
        flash_notice(
            gettext('Pizza delivery email templates already exist.')
        )

    return redirect_to('.index_for_party', party_id=party.id)


def _get_party_or_404(party_id):
    party = party_service.find_party(PartyID(party_id))
    if party is None:
        abort(404)
    return party
