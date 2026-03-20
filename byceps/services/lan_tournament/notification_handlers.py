"""Connect lan_tournament signals to notification service."""

from __future__ import annotations

import structlog

from .signals import match_ready
from . import tournament_notification_service

log = structlog.get_logger()


def _on_match_ready(sender, *, event=None) -> None:
    if event is None:
        return
    try:
        tournament_notification_service.send_match_ready_emails(
            event.tournament_id, event.match_id,
        )
    except Exception:
        log.exception(
            'Failed to send match-ready emails',
            match_id=str(event.match_id),
            tournament_id=str(event.tournament_id),
        )


def enable_match_notifications() -> None:
    """Register signal handlers for match notifications."""
    match_ready.connect(_on_match_ready)
