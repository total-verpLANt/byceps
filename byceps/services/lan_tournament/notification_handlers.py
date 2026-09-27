"""Connect lan_tournament signals to notification service."""

from __future__ import annotations

import structlog

from byceps.services.brand import brand_service
from byceps.services.party import party_service

from .models.tournament import TournamentID
from .models.tournament_status import TournamentStatus
from .signals import (
    match_ready,
    tournament_request_accepted,
    tournament_request_rejected,
    tournament_status_changed,
)
from . import (
    tournament_notification_service,
    tournament_repository,
    tournament_request_repository,
)

log = structlog.get_logger()


def _is_tournament_ongoing(tournament_id: TournamentID) -> bool:
    tournament = tournament_repository.get_tournament(tournament_id)
    return tournament.tournament_status == TournamentStatus.ONGOING


def _on_match_ready(sender, *, event=None) -> None:
    if event is None:
        return
    try:
        if not _is_tournament_ongoing(event.tournament_id):
            log.info(
                'Skipping match-ready email — tournament not running',
                match_id=str(event.match_id),
                tournament_id=str(event.tournament_id),
            )
            return
        tournament_notification_service.send_match_ready_emails(
            event.tournament_id, event.match_id,
        )
    except Exception:
        log.exception(
            'Failed to send match-ready emails',
            match_id=str(event.match_id),
            tournament_id=str(event.tournament_id),
        )


def _on_tournament_status_changed(sender, *, event=None) -> None:
    if event is None:
        return
    if event.new_status != TournamentStatus.ONGOING:
        return
    match_ids = tournament_repository.get_ready_unconfirmed_match_ids(
        event.tournament_id
    )
    log.info(
        'Tournament started — sending catch-up match-ready emails',
        tournament_id=str(event.tournament_id),
        match_count=len(match_ids),
    )
    for match_id in match_ids:
        try:
            tournament_notification_service.send_match_ready_emails(
                event.tournament_id, match_id,
            )
        except Exception:
            log.exception(
                'Failed to send catch-up match-ready email',
                match_id=str(match_id),
                tournament_id=str(event.tournament_id),
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
