"""Pure mocked worker transaction, queue and public SMTP exception contracts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from smtplib import (
    SMTPAuthenticationError, SMTPConnectError, SMTPDataError, SMTPException,
    SMTPHeloError, SMTPNotSupportedError, SMTPRecipientsRefused,
    SMTPResponseException, SMTPSenderRefused, SMTPServerDisconnected,
)
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from byceps.services.email.models import Message, NameAndAddress
from byceps.services.lan_tournament import tournament_invitation_service as service
from byceps.services.lan_tournament.models.match_readiness import InvitationStatus, MatchInvitation
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


@pytest.fixture
def worker(monkeypatch):
    repo, queue, messages, email = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(service, 'repository', repo)
    monkeypatch.setattr(service, 'jobqueue', queue)
    monkeypatch.setattr(service, 'messages', messages)
    monkeypatch.setattr(service, 'email_service', email)
    config = SimpleNamespace(suppress_send=False)
    monkeypatch.setattr(service, 'get_current_byceps_app', lambda: SimpleNamespace(byceps_config=SimpleNamespace(smtp=config)))
    work = MatchInvitation(
        id=uuid7(), match_id=uuid7(), tournament_id=uuid7(), recipient_id=uuid7(),
        pairing_generation=1, expected_readiness_revision=0,
        status=InvitationStatus.DISPATCHING, attempts=1, dispatch_token=uuid7(),
    )
    repo.get_match_invitation.return_value = work
    repo.claim_invitation_dispatch_flush.return_value = Ok(work)
    repo.record_invitation_outcome_flush.return_value = Ok(None)
    message = Message(sender=NameAndAddress('LAN', 'lan@example.test'), recipients=['player@example.test'], subject='Invitation', body='Play')
    messages.build_match_invitation_message.return_value = Ok(message)
    return SimpleNamespace(repo=repo, queue=queue, messages=messages, email=email, work=work, config=config, message=message)


def test_public_sender_has_no_second_raw_queue_hop(worker):
    order = MagicMock()
    order.attach_mock(worker.repo.record_invitation_outcome_flush, 'record')
    order.attach_mock(worker.repo.commit_session, 'commit')
    order.attach_mock(worker.email.send_email, 'send')
    result = service.deliver_match_invitation(worker.work.id, worker.work.dispatch_token)
    assert result.is_ok()
    assert [c[0] for c in order.mock_calls] == ['record', 'commit', 'send', 'record', 'commit']
    worker.email.send_email.assert_called_once_with('LAN <lan@example.test>', ['player@example.test'], 'Invitation', 'Play')
    worker.email.enqueue_message.assert_not_called()
    worker.queue.enqueue.assert_not_called()


# fmt: off
@pytest.mark.parametrize(('exception', 'expected', 'retry'), [
    (TimeoutError('secret'), InvitationStatus.DELIVERY_UNKNOWN, False),
    (ConnectionError('secret'), InvitationStatus.DELIVERY_UNKNOWN, False),
    (SMTPServerDisconnected('secret'), InvitationStatus.DELIVERY_UNKNOWN, False),
    (SMTPException('secret'), InvitationStatus.DELIVERY_UNKNOWN, False),
    (SMTPResponseException(500, b'QUIT secret'), InvitationStatus.DELIVERY_UNKNOWN, False),
    (RuntimeError('secret'), InvitationStatus.DELIVERY_UNKNOWN, False),
    (SMTPRecipientsRefused({'player@example.test': (550, b'secret')}), InvitationStatus.FAILED, True),
    (SMTPRecipientsRefused({'other@example.test': (550, b'secret')}), InvitationStatus.DELIVERY_UNKNOWN, False),
    (SMTPDataError(550, b'secret'), InvitationStatus.FAILED, True),
    (SMTPSenderRefused(550, b'secret', 'lan@example.test'), InvitationStatus.FAILED, True),
    (SMTPConnectError(421, b'secret'), InvitationStatus.FAILED, True),
    (SMTPHeloError(550, b'secret'), InvitationStatus.FAILED, True),
    (SMTPAuthenticationError(535, b'secret'), InvitationStatus.FAILED, False),
    (SMTPNotSupportedError('secret'), InvitationStatus.FAILED, False),
])
# fmt: on
def test_ambiguous_sending_is_delivery_unknown(worker, exception, expected, retry):
    worker.email.send_email.side_effect = exception
    assert service.deliver_match_invitation(worker.work.id, worker.work.dispatch_token).is_ok()
    call = worker.repo.record_invitation_outcome_flush.call_args
    assert call.kwargs['status'] == expected
    assert call.kwargs['retryable'] is retry
    assert 'secret' not in call.kwargs['error']
    worker.queue.enqueue_at.assert_not_called()


def test_smtp_suppressed_is_not_accepted(worker):
    worker.config.suppress_send = True
    assert service.deliver_match_invitation(worker.work.id, worker.work.dispatch_token).is_ok()
    assert worker.repo.record_invitation_outcome_flush.call_args.kwargs['status'] == InvitationStatus.SUPPRESSED
    assert worker.repo.record_invitation_outcome_flush.call_args.kwargs['error'] == 'smtp_suppressed'
    worker.email.send_email.assert_not_called()


@pytest.mark.parametrize('error', ['email_config_missing', 'email_address_missing', 'email_template_formatting_failed'])
def test_permanent_builder_errors_do_not_spin(worker, error):
    worker.messages.build_match_invitation_message.return_value = Err(error)
    assert service.deliver_match_invitation(worker.work.id, worker.work.dispatch_token).is_ok()
    call = worker.repo.record_invitation_outcome_flush.call_args
    assert call.kwargs['status'] == InvitationStatus.FAILED
    assert call.kwargs['error'] == error and not call.kwargs['retryable']
    worker.email.send_email.assert_not_called()
    worker.queue.enqueue_at.assert_not_called()


def test_unexpected_pre_send_exception_is_definitely_unsent(worker):
    worker.messages.build_match_invitation_message.side_effect = RuntimeError('secret')
    assert service.deliver_match_invitation(worker.work.id, worker.work.dispatch_token).is_ok()
    assert worker.repo.record_invitation_outcome_flush.call_args.kwargs['error'] == 'invitation_build_failed'
    worker.email.send_email.assert_not_called()


@pytest.mark.parametrize('error', ['pairing_retired', 'readiness_revision_changed', 'readiness_hold', 'recipient_not_current', 'tournament_paused', 'match_confirmed', 'invitation_lease_expired', 'invitation_conflict'])
def test_final_locked_validation_refuses_stale_job(worker, error):
    worker.repo.record_invitation_outcome_flush.return_value = Err(error)
    assert service.deliver_match_invitation(worker.work.id, worker.work.dispatch_token).unwrap_err() == error
    worker.repo.commit_session.assert_not_called()
    worker.email.send_email.assert_not_called()


def test_dispatch_commits_reservation_before_queue(worker):
    order = MagicMock()
    order.attach_mock(worker.repo.claim_invitation_dispatch_flush, 'claim')
    order.attach_mock(worker.repo.commit_session, 'commit')
    order.attach_mock(worker.queue.enqueue, 'enqueue')
    assert service.dispatch_match_invitations([worker.work.id]).unwrap() == 1
    assert [c[0] for c in order.mock_calls][:3] == ['claim', 'commit', 'enqueue']
    worker.queue.enqueue.assert_called_once_with(service.deliver_match_invitation, worker.work.id, worker.work.dispatch_token)


def test_sync_worker_acceptance_not_overwritten_by_enqueue(worker):
    worker.repo.record_invitation_outcome_flush.return_value = Err('invitation_conflict')
    worker.repo.get_match_invitation.side_effect = [worker.work, replace(worker.work, status=InvitationStatus.ACCEPTED)]
    assert service.dispatch_match_invitations([worker.work.id]).unwrap() == 1
    assert worker.repo.record_invitation_outcome_flush.call_count == 1


def test_advanced_other_token_is_not_benign(worker):
    worker.repo.record_invitation_outcome_flush.return_value = Err('invitation_conflict')
    worker.repo.get_match_invitation.side_effect = [worker.work, replace(worker.work, status=InvitationStatus.ACCEPTED, dispatch_token=uuid7())]
    assert service.dispatch_match_invitations([worker.work.id]).is_err()


def test_definite_enqueue_failure_is_recorded_and_scheduled(worker):
    worker.queue.enqueue.side_effect = ConnectionError('redis secret')
    due = datetime.now(UTC) + timedelta(seconds=30)
    worker.repo.get_match_invitation.side_effect = [worker.work, replace(worker.work, status=InvitationStatus.FAILED, next_attempt_at=due)]
    assert service.dispatch_match_invitations([worker.work.id]).is_err()
    call = worker.repo.record_invitation_outcome_flush.call_args
    assert call.kwargs['status'] == InvitationStatus.FAILED and call.kwargs['retryable']
    worker.queue.enqueue_at.assert_called_once_with(due, service.dispatch_match_invitations, (worker.work.id,))


def test_dispatch_exception_does_not_abandon_other_recipient(worker):
    other = replace(worker.work, id=uuid7())
    worker.repo.get_match_invitation.side_effect = RuntimeError('secret')
    assert service.dispatch_match_invitations([worker.work.id, other.id]).is_err()
    assert worker.repo.get_match_invitation.call_count == 2
    assert worker.repo.rollback_session.call_count == 2


def test_reconcile_is_flush_only(worker):
    worker.repo.find_match.return_value = SimpleNamespace(tournament_id=worker.work.tournament_id)
    worker.repo.get_contestants_for_match.return_value = [object()]
    worker.messages._resolve_user_ids_for_contestant.return_value = [worker.work.recipient_id]
    worker.repo.ensure_invitation_intents_flush.return_value = [worker.work.id]
    now = datetime.now(UTC)
    assert service.reconcile_match_invitations_flush(worker.work.match_id, occurred_at=now).unwrap() == (worker.work.id,)
    worker.repo.ensure_invitation_intents_flush.assert_called_once_with(worker.work.match_id, {worker.work.recipient_id}, occurred_at=now)
    worker.repo.commit_session.assert_not_called()
    worker.queue.enqueue.assert_not_called()
