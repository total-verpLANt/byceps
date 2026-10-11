from datetime import datetime, timedelta, UTC

from flask import abort, flash, g, redirect, render_template, request, url_for
from flask_babel import gettext

from byceps.services.pizza_delivery import pizza_delivery_service
from byceps.services.pizza_delivery.errors import (
    PizzaDeliveryEntryAlreadyClaimedError,
)
from byceps.services.pizza_delivery.models import (
    PizzaDeliveryEntry,
    PizzaDeliveryStatus,
)
from byceps.util.framework.blueprint import create_blueprint
from byceps.util.framework.flash import FlashMessage, flash_success
from byceps.util.views import login_required


FRESH_WINDOW = timedelta(minutes=10)
MAX_ECHOED_NUMBER_LENGTH = 64

blueprint = create_blueprint('pizza_delivery', __name__)


@blueprint.get('/')
def board():
    """Show the public pizza delivery board for the current party."""
    if not g.party:
        abort(404)

    entries = pizza_delivery_service.get_entries_for_party(
        g.party.id, status=PizzaDeliveryStatus.DELIVERED
    )
    delivered_total = pizza_delivery_service.count_entries_for_party(
        g.party.id, status=PizzaDeliveryStatus.DELIVERED
    )

    my_entries = []
    current_user_entry = None
    if g.user.authenticated:
        my_entries = pizza_delivery_service.get_claimed_entries_for_user(
            g.user.id, g.party.id
        )
        current_user_entry = next(
            (entry for entry in entries if entry.user_id == g.user.id), None
        )

    now = datetime.now(UTC)

    return render_template(
        'site/pizza_delivery/board.html',
        entries=entries,
        delivered_total=delivered_total,
        my_entries=_ready_first(my_entries),
        current_user_entry=current_user_entry,
        fresh_ids=_get_fresh_entry_ids(entries, now),
        now=now,
    )


@blueprint.get('/beamershow')
def beamershow():
    """Minimal delivered-numbers display for projector use."""
    if not g.party:
        abort(404)

    entries = pizza_delivery_service.get_entries_for_party(
        g.party.id, status=PizzaDeliveryStatus.DELIVERED
    )
    delivered_total = pizza_delivery_service.count_entries_for_party(
        g.party.id, status=PizzaDeliveryStatus.DELIVERED
    )

    now = datetime.now(UTC)

    return render_template(
        'site/pizza_delivery/beamershow.html',
        entries=entries,
        delivered_total=delivered_total,
        fresh_ids=_get_fresh_entry_ids(entries, now),
        now=now,
    )


@blueprint.get('/my-status')
@login_required
def my_status():
    """Show the current user's pizza delivery status."""
    if not g.party:
        abort(404)

    return _render_my_status()


@blueprint.post('/claim')
@login_required
def claim():
    """Claim a pizza delivery entry by number."""
    if not g.party:
        abort(404)

    number = request.form.get('number', '').strip()

    if not number:
        return _render_my_status_with_error('empty', number)

    if len(number) > 20:
        return _render_my_status_with_error('too_long', number)

    result = pizza_delivery_service.claim_entry_by_number(
        g.party.id, number, g.user.id, initiator=g.user
    )

    if result.is_err():
        if isinstance(
            result.unwrap_err(), PizzaDeliveryEntryAlreadyClaimedError
        ):
            return _render_my_status_with_error('already_claimed', number)

        return _render_my_status_with_error('not_found', number)

    entry = result.unwrap()
    if entry.status == PizzaDeliveryStatus.DELIVERED:
        _flash_warning(
            gettext(
                '%(number)s has already been called. Go to the food counter now.',
                number=entry.number,
            )
        )
    else:
        flash_success(
            gettext(
                'You are now following %(number)s. We will let you know as soon as it is called.',
                number=entry.number,
            )
        )

    # Only the board form posts `next`; never redirect to a client-given URL.
    if request.form.get('next') == 'board':
        return redirect(url_for('.board'))

    return redirect(url_for('.my_status'))


def _render_my_status(*, form_error: str | None = None, form_value: str = ''):
    entries = pizza_delivery_service.get_claimed_entries_for_user(
        g.user.id, g.party.id
    )

    return render_template(
        'site/pizza_delivery/my_status.html',
        entries=_ready_first(entries),
        party=g.party,
        now=datetime.now(UTC),
        form_error=form_error,
        form_value=form_value[:MAX_ECHOED_NUMBER_LENGTH],
    )


def _render_my_status_with_error(error: str, number: str):
    return (
        _render_my_status(form_error=error, form_value=number),
        422,
    )


def _ready_first(
    entries: list[PizzaDeliveryEntry],
) -> list[PizzaDeliveryEntry]:
    """Put called entries, newest call first, before the waiting ones."""
    called = sorted(
        (e for e in entries if e.status == PizzaDeliveryStatus.DELIVERED),
        key=lambda entry: entry.delivered_at or entry.updated_at,
        reverse=True,
    )
    waiting = [e for e in entries if e.status != PizzaDeliveryStatus.DELIVERED]

    return called + waiting


def _get_fresh_entry_ids(
    entries: list[PizzaDeliveryEntry], now: datetime
) -> frozenset:
    """Return the IDs of entries called less than ten minutes ago."""
    return frozenset(entry.id for entry in entries if _is_fresh(entry, now))


def _is_fresh(entry: PizzaDeliveryEntry, now: datetime) -> bool:
    called_at = entry.delivered_at
    if called_at is None:
        return False

    if called_at.tzinfo is None:
        called_at = called_at.replace(tzinfo=UTC)

    return timedelta(0) <= now - called_at < FRESH_WINDOW


def _flash_warning(message: str) -> None:
    """Flash a warning; core has no helper for that category."""
    flash(FlashMessage(message, False, 'warning', None))
