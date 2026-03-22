"""
byceps.services.pizza_delivery.notification_handlers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Connect pizza_delivery signals to notification service.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from __future__ import annotations

import structlog

from . import pizza_delivery_email_service, pizza_delivery_service
from .signals import entry_delivered

log = structlog.get_logger()


def _on_entry_delivered(sender, *, event=None) -> None:
    """Handle entry_delivered signal by sending email notification."""
    if event is None:
        return

    if event.user_id is None:
        return

    try:
        entry = pizza_delivery_service.find_entry(event.entry_id)
        if entry is None:
            log.warning(
                'Pizza delivery entry not found for notification',
                entry_id=str(event.entry_id),
            )
            return

        pizza_delivery_email_service.send_notification(
            entry, event.party_id
        )
    except Exception:
        log.exception(
            'Failed to send pizza delivery notification',
            entry_id=str(event.entry_id),
        )


def enable_pizza_delivery_notifications() -> None:
    """Register signal handlers for pizza delivery notifications."""
    entry_delivered.connect(_on_entry_delivered)
