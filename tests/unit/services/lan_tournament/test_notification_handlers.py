"""Assignment work listeners, post-commit ordering and retained request contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from byceps.services.lan_tournament import notification_handlers as handlers, signals
from byceps.services.lan_tournament.events import MatchReadyEvent, TournamentStatusChangedEvent
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


@pytest.fixture
def collaborators(monkeypatch):
    repo, invitations, messages = MagicMock(), MagicMock(), MagicMock()
    # The handlers no longer import the repository; a stray use would hit this mock.
    monkeypatch.setattr(handlers, 'tournament_repository', repo, raising=False)
    monkeypatch.setattr(handlers, 'tournament_invitation_service', invitations)
    monkeypatch.setattr(handlers, 'tournament_notification_service', messages)
    invitations.reconcile_match_invitations_flush.return_value = Ok((uuid7(),))
    invitations.enqueue_invitation_dispatch.return_value = Ok(None)
    invitations.enqueue_tournament_sweep.return_value = Ok(None)
    return repo, invitations, messages


def assignment():
    return MatchReadyEvent(
        occurred_at=datetime.now(UTC), initiator=None,
        tournament_id=uuid7(), match_id=uuid7(),
    )


def status(new_status):
    return TournamentStatusChangedEvent(
        occurred_at=datetime.now(UTC), initiator=None, tournament_id=uuid7(),
        old_status=TournamentStatus.PAUSED, new_status=new_status,
    )


def test_assignment_leaves_the_recipient_work_to_the_emitting_writer(
    collaborators,
):
    repo, invitations, messages = collaborators
    handlers._on_match_ready(None, event=assignment())
    assert invitations.mock_calls == []
    assert repo.mock_calls == []
    assert messages.mock_calls == []


def test_both_ready_sends_no_second_email(collaborators):
    repo, invitations, messages = collaborators
    handlers.enable_match_notifications()
    assert handlers._on_match_ready in signals.match_ready.receivers_for(None)
    assert not list(signals.match_both_ready.receivers_for(None))
    signals.match_both_ready.send(None, event=assignment())
    invitations.dispatch_match_invitations.assert_not_called()
    invitations.enqueue_invitation_dispatch.assert_not_called()
    messages.send_match_ready_emails.assert_not_called()
    repo.mark_matches_both_ready_notified.assert_not_called()


def test_start_resume_enqueues_one_sweep_and_leaves_reconcile_to_the_owner(
    collaborators,
):
    repo, invitations, messages = collaborators
    event = status(TournamentStatus.ONGOING)
    handlers._on_tournament_status_changed(None, event=event)
    invitations.enqueue_tournament_sweep.assert_called_once_with(
        event.tournament_id
    )
    # `change_status` already reconciled every match and dispatches its ids.
    invitations.reconcile_match_invitations_flush.assert_not_called()
    invitations.dispatch_match_invitations.assert_not_called()
    repo.get_matches_for_tournament.assert_not_called()
    repo.lock_tournament_for_update.assert_not_called()
    repo.recover_expired_invitations_flush.assert_not_called()
    repo.select_invitation_retry_ids_flush.assert_not_called()
    repo.commit_session.assert_not_called()
    repo.get_both_ready_unnotified_match_ids.assert_not_called()
    repo.mark_matches_both_ready_notified.assert_not_called()
    messages.send_match_ready_emails.assert_not_called()


@pytest.mark.parametrize(
    'failure',
    [Err('invitation_dispatch_failed'), RuntimeError('private redis secret')],
)
def test_start_resume_sweep_failure_is_swallowed(collaborators, failure):
    _, invitations, _ = collaborators
    if isinstance(failure, Exception):
        invitations.enqueue_tournament_sweep.side_effect = failure
    else:
        invitations.enqueue_tournament_sweep.return_value = failure
    handlers._on_tournament_status_changed(
        None, event=status(TournamentStatus.ONGOING)
    )
    invitations.enqueue_tournament_sweep.assert_called_once()


@pytest.mark.parametrize('new_status', [TournamentStatus.PAUSED, TournamentStatus.COMPLETED, TournamentStatus.CANCELLED])
def test_non_ongoing_status_is_inert(collaborators, new_status):
    repo, invitations, _ = collaborators
    handlers._on_tournament_status_changed(None, event=status(new_status))
    repo.get_matches_for_tournament.assert_not_called()
    invitations.dispatch_match_invitations.assert_not_called()
    invitations.enqueue_tournament_sweep.assert_not_called()


def test_missing_events_are_inert(collaborators):
    repo, invitations, _ = collaborators
    handlers._on_match_ready(None)
    handlers._on_tournament_status_changed(None)
    repo.commit_session.assert_not_called()
    invitations.reconcile_match_invitations_flush.assert_not_called()


def test_request_handlers_remain_registered():
    handlers.enable_match_notifications()
    assert handlers._on_tournament_request_accepted in signals.tournament_request_accepted.receivers_for(None)
    assert handlers._on_tournament_request_rejected in signals.tournament_request_rejected.receivers_for(None)


@pytest.mark.parametrize('decision', ['accepted', 'rejected'])
def test_request_notification_contract_unchanged(collaborators, monkeypatch, decision):
    _, _, messages = collaborators
    request_repo, parties, brands = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(handlers, 'tournament_request_repository', request_repo)
    monkeypatch.setattr(handlers, 'party_service', parties)
    monkeypatch.setattr(handlers, 'brand_service', brands)
    event = SimpleNamespace(request_id=uuid7(), party_id=uuid7())
    getattr(handlers, f'_on_tournament_request_{decision}')(None, event=event)
    getattr(messages, f'send_request_{decision}_email').assert_called_once_with(
        brands.get_brand.return_value, request_repo.find_request.return_value,
    )
