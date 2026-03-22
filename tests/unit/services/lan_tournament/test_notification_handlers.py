"""
tests.unit.services.lan_tournament.test_notification_handlers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for notification handler gating and catch-up logic
in ``notification_handlers.py``.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from byceps.services.lan_tournament.events import (
    MatchReadyEvent,
    TournamentStatusChangedEvent,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.notification_handlers import (
    _on_match_ready,
    _on_tournament_status_changed,
)

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())


def _make_match_ready_event(
    tournament_id: TournamentID | None = None,
    match_id: TournamentMatchID | None = None,
) -> MatchReadyEvent:
    return MatchReadyEvent(
        occurred_at=NOW,
        initiator=None,
        tournament_id=tournament_id or TOURNAMENT_ID,
        match_id=match_id or TournamentMatchID(generate_uuid()),
    )


def _make_status_changed_event(
    new_status: TournamentStatus,
    tournament_id: TournamentID | None = None,
) -> TournamentStatusChangedEvent:
    return TournamentStatusChangedEvent(
        occurred_at=NOW,
        initiator=None,
        tournament_id=tournament_id or TOURNAMENT_ID,
        old_status=TournamentStatus.REGISTRATION_CLOSED,
        new_status=new_status,
    )


# -------------------------------------------------------------------- #
# _on_match_ready
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_match_ready_sends_when_ongoing(mock_repo, mock_notif):
    """When tournament is ONGOING, match-ready emails are sent."""
    tournament = MagicMock()
    tournament.tournament_status = TournamentStatus.ONGOING
    mock_repo.get_tournament.return_value = tournament

    event = _make_match_ready_event()
    _on_match_ready(None, event=event)

    mock_notif.send_match_ready_emails.assert_called_once_with(
        event.tournament_id, event.match_id,
    )


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_match_ready_skips_when_not_ongoing(mock_repo, mock_notif):
    """When tournament is not ONGOING, no emails are sent."""
    tournament = MagicMock()
    tournament.tournament_status = TournamentStatus.REGISTRATION_CLOSED
    mock_repo.get_tournament.return_value = tournament

    event = _make_match_ready_event()
    _on_match_ready(None, event=event)

    mock_notif.send_match_ready_emails.assert_not_called()


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_match_ready_skips_when_event_is_none(mock_repo, mock_notif):
    """When event is None, handler returns without crashing or sending."""
    _on_match_ready(None, event=None)

    mock_repo.get_tournament.assert_not_called()
    mock_notif.send_match_ready_emails.assert_not_called()


# -------------------------------------------------------------------- #
# _on_tournament_status_changed
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_tournament_started_sends_catchup_emails(mock_repo, mock_notif):
    """Transition to ONGOING sends catch-up emails for ready matches."""
    match_id_1 = TournamentMatchID(generate_uuid())
    match_id_2 = TournamentMatchID(generate_uuid())
    mock_repo.get_ready_unconfirmed_match_ids.return_value = [
        match_id_1,
        match_id_2,
    ]

    event = _make_status_changed_event(TournamentStatus.ONGOING)
    _on_tournament_status_changed(None, event=event)

    assert mock_notif.send_match_ready_emails.call_count == 2
    mock_notif.send_match_ready_emails.assert_any_call(
        event.tournament_id, match_id_1,
    )
    mock_notif.send_match_ready_emails.assert_any_call(
        event.tournament_id, match_id_2,
    )


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_tournament_started_ignores_non_ongoing_transitions(
    mock_repo, mock_notif
):
    """Transition to PAUSED (non-ONGOING) does not send emails."""
    event = _make_status_changed_event(TournamentStatus.PAUSED)
    _on_tournament_status_changed(None, event=event)

    mock_repo.get_ready_unconfirmed_match_ids.assert_not_called()
    mock_notif.send_match_ready_emails.assert_not_called()


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_tournament_started_skips_confirmed_matches(mock_repo, mock_notif):
    """When all matches are confirmed (empty list), no emails are sent."""
    mock_repo.get_ready_unconfirmed_match_ids.return_value = []

    event = _make_status_changed_event(TournamentStatus.ONGOING)
    _on_tournament_status_changed(None, event=event)

    mock_notif.send_match_ready_emails.assert_not_called()


@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_notification_service'
)
@patch(
    'byceps.services.lan_tournament.notification_handlers.tournament_repository'
)
def test_on_tournament_started_handles_email_failure_gracefully(
    mock_repo, mock_notif
):
    """If one catch-up email fails, the remaining are still sent."""
    match_id_1 = TournamentMatchID(generate_uuid())
    match_id_2 = TournamentMatchID(generate_uuid())
    match_id_3 = TournamentMatchID(generate_uuid())
    mock_repo.get_ready_unconfirmed_match_ids.return_value = [
        match_id_1,
        match_id_2,
        match_id_3,
    ]

    # First call raises, second and third succeed.
    mock_notif.send_match_ready_emails.side_effect = [
        RuntimeError('SMTP down'),
        None,
        None,
    ]

    event = _make_status_changed_event(TournamentStatus.ONGOING)
    _on_tournament_status_changed(None, event=event)

    # All three were attempted despite the first failure.
    assert mock_notif.send_match_ready_emails.call_count == 3
    mock_notif.send_match_ready_emails.assert_any_call(
        event.tournament_id, match_id_1,
    )
    mock_notif.send_match_ready_emails.assert_any_call(
        event.tournament_id, match_id_2,
    )
    mock_notif.send_match_ready_emails.assert_any_call(
        event.tournament_id, match_id_3,
    )
