"""Connect lan_tournament signals to notification service."""

from __future__ import annotations

import structlog

from byceps.services.brand import brand_service
from byceps.services.party import party_service

from .models.tournament_status import TournamentStatus
from .signals import (
    match_ready,
    tournament_request_accepted,
    tournament_request_rejected,
    tournament_status_changed,
)
from . import (
    tournament_invitation_service,
    tournament_notification_service,
    tournament_request_repository,
)

log = structlog.get_logger()


def _on_match_ready(sender, *, event=None) -> None:
    """Leave the recipient work of a ready match to the emitting writer.

    Every emitter reconciles its matches in its own transaction and queues the
    ids after the commit. The one exception, the FFA grand final, has no
    one-versus-one audience, so there is nothing for a catch-up to find.
    """


def _on_tournament_status_changed(sender, *, event=None) -> None:
    """Catch up stuck work on a start or resume.

    `tournament_service.change_status` has already reconciled every match in
    its own transaction and dispatches the ids after the commit. Only the sweep
    for rows a crash or a lost retry left behind is left to do.
    """
    if event is None:
        return
    if event.new_status != TournamentStatus.ONGOING:
        return
    try:
        result = tournament_invitation_service.enqueue_tournament_sweep(
            event.tournament_id,
        )
        if result.is_err():
            log.warning(
                'Invitation catch-up deferred',
                tournament_id=str(event.tournament_id),
                error=result.unwrap_err(),
            )
    except Exception:
        log.warning(
            'Invitation catch-up deferred',
            tournament_id=str(event.tournament_id),
            error='invitation_sweep_failed',
        )


def _on_tournament_request_accepted(sender, *, event=None) -> None:
    if event is None:
        return
    try:
        request = tournament_request_repository.find_request(
            event.request_id
        )
        if request is None:
            log.warning(
                'Tournament request not found, skipping accepted email',
                request_id=str(event.request_id),
            )
            return

        party = party_service.get_party(event.party_id)
        brand = brand_service.get_brand(party.brand_id)

        tournament_notification_service.send_request_accepted_email(
            brand, request,
        )
    except Exception:
        log.exception(
            'Failed to send tournament-request-accepted email',
            request_id=str(event.request_id),
        )


def _on_tournament_request_rejected(sender, *, event=None) -> None:
    if event is None:
        return
    try:
        request = tournament_request_repository.find_request(
            event.request_id
        )
        if request is None:
            log.warning(
                'Tournament request not found, skipping rejected email',
                request_id=str(event.request_id),
            )
            return

        party = party_service.get_party(event.party_id)
        brand = brand_service.get_brand(party.brand_id)

        tournament_notification_service.send_request_rejected_email(
            brand, request,
        )
    except Exception:
        log.exception(
            'Failed to send tournament-request-rejected email',
            request_id=str(event.request_id),
        )


def enable_match_notifications() -> None:
    """Register signal handlers for match notifications and for
    tournament-request accept/reject decisions.
    """
    match_ready.connect(_on_match_ready)
    tournament_status_changed.connect(_on_tournament_status_changed)
    tournament_request_accepted.connect(_on_tournament_request_accepted)
    tournament_request_rejected.connect(_on_tournament_request_rejected)
