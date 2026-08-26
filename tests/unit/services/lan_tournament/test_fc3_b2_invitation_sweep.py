"""Sweep, follow-up scheduling and batched dispatch contracts (fix cycle 3, B2)."""

from dataclasses import replace
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from byceps.services.email.models import Message, NameAndAddress
from byceps.services.lan_tournament import (
    tournament_invitation_service as service,
    tournament_readiness_service as readiness,
)
from byceps.services.lan_tournament.models.match_readiness import (
    InvitationStatus,
    MatchInvitation,
)
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


@pytest.fixture
def worker(monkeypatch):
    repo, queue, messages, email = (
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    monkeypatch.setattr(service, 'repository', repo)
    monkeypatch.setattr(service, 'jobqueue', queue)
    monkeypatch.setattr(service, 'messages', messages)
    monkeypatch.setattr(service, 'email_service', email)
    config = SimpleNamespace(suppress_send=False)
    monkeypatch.setattr(
        service,
        'get_current_byceps_app',
        lambda: SimpleNamespace(byceps_config=SimpleNamespace(smtp=config)),
    )
    tournament_id = uuid7()
    works = {}

    def make(**changes):
        work = MatchInvitation(
            id=uuid7(),
            match_id=uuid7(),
            tournament_id=tournament_id,
            recipient_id=uuid7(),
            pairing_generation=1,
            expected_readiness_revision=0,
            status=InvitationStatus.PENDING,
            attempts=0,
            dispatch_token=uuid7(),
        )
        work = replace(work, **changes)
        works[work.id] = work
        return work

    repo.get_match_invitation.side_effect = works.get
    repo.claim_invitation_dispatch_flush.side_effect = (
        lambda invitation_id, **_: Ok(works[invitation_id])
    )
    repo.record_invitation_outcome_flush.return_value = Ok(None)
    repo.recover_expired_invitations_flush.return_value = []
    repo.select_invitation_retry_ids_flush.return_value = []
    message = Message(
        sender=NameAndAddress('LAN', 'lan@example.test'),
        recipients=['player@example.test'],
        subject='Invitation',
        body='Play',
    )
    messages.build_match_invitation_message.return_value = Ok(message)
    return SimpleNamespace(
        repo=repo,
        queue=queue,
        email=email,
        make=make,
        works=works,
        tournament_id=tournament_id,
    )


def test_unrelated_dispatch_sweeps_its_tournament_and_dispatches_recovered_work(
    worker,
):
    batch = worker.make()
    stuck = worker.make(status=InvitationStatus.PENDING)
    worker.repo.recover_expired_invitations_flush.return_value = [stuck.id]
    worker.repo.select_invitation_retry_ids_flush.return_value = [stuck.id]

    assert service.dispatch_match_invitations([batch.id]).unwrap() == 1

    worker.repo.recover_expired_invitations_flush.assert_called_once()
    call = worker.repo.recover_expired_invitations_flush.call_args
    assert call.args == (worker.tournament_id,) and call.kwargs['limit'] == 100
    worker.repo.select_invitation_retry_ids_flush.assert_called_once()
    assert (
        worker.repo.select_invitation_retry_ids_flush.call_args.kwargs['limit']
        == 100
    )
    claimed = [
        c.args[0]
        for c in worker.repo.claim_invitation_dispatch_flush.call_args_list
    ]
    assert claimed == [batch.id, stuck.id]
    delivered = [c.args[1] for c in worker.queue.enqueue.call_args_list]
    assert delivered == [batch.id, stuck.id]


def test_sweep_runs_once_per_touched_tournament(worker):
    first, second = worker.make(), worker.make()
    other = worker.make(tournament_id=uuid7())
    service.dispatch_match_invitations([first.id, second.id, other.id])
    swept = {
        c.args[0]
        for c in worker.repo.recover_expired_invitations_flush.call_args_list
    }
    assert swept == {worker.tournament_id, other.tournament_id}
    assert worker.repo.recover_expired_invitations_flush.call_count == 2
    assert worker.repo.select_invitation_retry_ids_flush.call_count == 2


def test_follow_up_is_enqueued_once_per_batch_with_expected_time(worker):
    ids = [worker.make().id for _ in range(3)]
    before = datetime.now(UTC)
    assert service.dispatch_match_invitations(ids).unwrap() == 3
    after = datetime.now(UTC)
    worker.queue.enqueue_at.assert_called_once()
    due, function, tournament_id = worker.queue.enqueue_at.call_args.args
    assert function is service.sweep_tournament_invitations
    assert tournament_id == worker.tournament_id
    lead = timedelta(seconds=150)
    assert service.FOLLOW_UP_DELAY_SECONDS == 150
    assert before + lead <= due <= after + lead


def test_sweep_originated_dispatch_does_not_chain_a_second_sweep(worker):
    first = worker.make()
    found = worker.make()
    worker.repo.select_invitation_retry_ids_flush.return_value = [found.id]
    assert service.dispatch_match_invitations([first.id]).unwrap() == 1
    # The swept batch was dispatched without its own sweep or follow-up.
    assert worker.repo.recover_expired_invitations_flush.call_count == 1
    assert worker.repo.select_invitation_retry_ids_flush.call_count == 1
    assert worker.queue.enqueue_at.call_count == 1
    assert [c.args[1] for c in worker.queue.enqueue.call_args_list] == [
        first.id,
        found.id,
    ]


def test_no_follow_up_when_nothing_was_dispatched(worker):
    gone = uuid7()
    assert service.dispatch_match_invitations([gone]).unwrap() == 0
    worker.queue.enqueue_at.assert_not_called()
    worker.repo.recover_expired_invitations_flush.assert_not_called()


def test_no_follow_up_when_every_claim_was_refused(worker):
    work = worker.make()
    worker.repo.claim_invitation_dispatch_flush.side_effect = None
    worker.repo.claim_invitation_dispatch_flush.return_value = Err(
        'invitation_conflict'
    )
    assert service.dispatch_match_invitations([work.id]).unwrap() == 0
    worker.queue.enqueue_at.assert_not_called()


def test_sweep_exception_does_not_propagate_into_the_dispatch_job(worker):
    work = worker.make()
    worker.repo.recover_expired_invitations_flush.side_effect = RuntimeError(
        'db secret'
    )
    assert service.dispatch_match_invitations([work.id]).unwrap() == 1
    worker.repo.rollback_session.assert_called()
    # The batch itself still gets its follow-up.
    worker.queue.enqueue_at.assert_called_once()


def test_failed_follow_up_scheduling_is_logged_and_ignored(worker):
    work = worker.make()
    worker.queue.enqueue_at.side_effect = ConnectionError('redis secret')
    with capture_logs() as logs:
        assert service.dispatch_match_invitations([work.id]).unwrap() == 1
    assert any(
        entry['log_level'] == 'warning' and 'secret' not in str(entry)
        for entry in logs
    )


def test_sweep_job_returns_error_instead_of_raising(worker):
    worker.repo.recover_expired_invitations_flush.side_effect = RuntimeError(
        'db secret'
    )
    result = service.sweep_tournament_invitations(worker.tournament_id)
    assert result.unwrap_err() == 'invitation_dispatch_failed'
    worker.repo.rollback_session.assert_called()
    worker.queue.enqueue_at.assert_not_called()


def test_sweep_job_survives_an_unexpected_dispatch_error(worker, monkeypatch):
    monkeypatch.setattr(
        service, '_dispatch_batch', MagicMock(side_effect=RuntimeError('boom'))
    )
    worker.repo.select_invitation_retry_ids_flush.return_value = [
        worker.make().id
    ]
    assert (
        service.sweep_tournament_invitations(worker.tournament_id).unwrap_err()
        == 'invitation_dispatch_failed'
    )
    worker.repo.rollback_session.assert_called()
    worker.queue.enqueue_at.assert_not_called()


def test_dispatch_job_survives_an_unexpected_sweep_error(worker, monkeypatch):
    work = worker.make()
    monkeypatch.setattr(
        service, '_sweep_once', MagicMock(side_effect=RuntimeError('boom'))
    )
    assert service.dispatch_match_invitations([work.id]).unwrap() == 1
    worker.repo.rollback_session.assert_called()


def test_sweep_job_without_work_neither_dispatches_nor_reschedules(worker):
    assert (
        service.sweep_tournament_invitations(worker.tournament_id).unwrap() == 0
    )
    worker.repo.commit_session.assert_called_once()
    worker.queue.enqueue.assert_not_called()
    worker.queue.enqueue_at.assert_not_called()


def test_sweep_job_with_work_schedules_exactly_one_follow_up(worker):
    due = [worker.make().id, worker.make().id]
    worker.repo.select_invitation_retry_ids_flush.return_value = due
    assert (
        service.sweep_tournament_invitations(worker.tournament_id).unwrap() == 2
    )
    assert worker.repo.recover_expired_invitations_flush.call_count == 1
    assert worker.queue.enqueue_at.call_count == 1
    assert (
        worker.queue.enqueue_at.call_args.args[1]
        is service.sweep_tournament_invitations
    )


def test_sweep_commits_before_dispatching(worker):
    order = MagicMock()
    order.attach_mock(worker.repo.recover_expired_invitations_flush, 'recover')
    order.attach_mock(worker.repo.select_invitation_retry_ids_flush, 'select')
    order.attach_mock(worker.repo.commit_session, 'commit')
    order.attach_mock(worker.repo.claim_invitation_dispatch_flush, 'claim')
    worker.repo.select_invitation_retry_ids_flush.return_value = [
        worker.make().id
    ]
    service.sweep_tournament_invitations(worker.tournament_id)
    assert [c[0] for c in order.mock_calls][:4] == [
        'recover',
        'select',
        'commit',
        'claim',
    ]


def test_deliver_logs_a_refused_sending_record(worker):
    work = worker.make()
    outcomes = {
        InvitationStatus.SENDING: Err('invitation_lease_expired'),
    }
    worker.repo.record_invitation_outcome_flush.side_effect = (
        lambda _id, _token, *, status, **_: outcomes.get(status, Ok(None))
    )
    with capture_logs() as logs:
        result = service.deliver_match_invitation(work.id, work.dispatch_token)
    assert result.unwrap_err() == 'invitation_lease_expired'
    warnings = [e for e in logs if e['log_level'] == 'warning']
    assert any(
        e.get('invitation_id') == str(work.id)
        and e.get('error') == 'invitation_lease_expired'
        for e in warnings
    )
    worker.email.send_email.assert_not_called()


def test_one_job_per_batch_carries_all_sorted_ids(worker):
    ids = [uuid7() for _ in range(5)]
    result = service.enqueue_invitation_dispatch([*ids, ids[0]])
    assert result.is_ok()
    worker.queue.enqueue.assert_called_once_with(
        service.dispatch_match_invitations,
        tuple(sorted(ids, key=str)),
    )
    worker.repo.claim_invitation_dispatch_flush.assert_not_called()


def test_empty_batch_enqueues_nothing(worker):
    assert service.enqueue_invitation_dispatch([]).is_ok()
    worker.queue.enqueue.assert_not_called()


def test_dispatch_job_enqueue_failure_is_an_error_not_an_exception(worker):
    worker.queue.enqueue.side_effect = ConnectionError('redis secret')
    with capture_logs() as logs:
        result = service.enqueue_invitation_dispatch([uuid7()])
    assert result.is_err()
    assert all('secret' not in str(entry) for entry in logs)


def test_sweep_job_enqueue_is_one_job_for_the_tournament(worker):
    assert service.enqueue_tournament_sweep(worker.tournament_id).is_ok()
    worker.queue.enqueue.assert_called_once_with(
        service.sweep_tournament_invitations,
        worker.tournament_id,
    )
    worker.repo.recover_expired_invitations_flush.assert_not_called()


def test_sweep_job_enqueue_failure_is_an_error_not_an_exception(worker):
    worker.queue.enqueue.side_effect = ConnectionError('redis secret')
    assert service.enqueue_tournament_sweep(worker.tournament_id).is_err()


def test_readiness_dispatch_enqueues_one_job_and_claims_nothing_in_process(
    worker,
):
    ids = tuple(uuid7() for _ in range(4))
    assert readiness.dispatch_pending_invitations(
        [*reversed(ids), ids[0]]
    ).is_ok()
    worker.queue.enqueue.assert_called_once_with(
        service.dispatch_match_invitations,
        tuple(sorted(ids, key=str)),
    )
    worker.repo.claim_invitation_dispatch_flush.assert_not_called()
    worker.repo.get_match_invitation.assert_not_called()
    worker.repo.commit_session.assert_not_called()


def test_readiness_dispatch_without_ids_is_inert(worker):
    assert readiness.dispatch_pending_invitations([]).is_ok()
    worker.queue.enqueue.assert_not_called()


def test_readiness_dispatch_enqueue_failure_returns_err_and_logs(
    worker, caplog
):
    worker.queue.enqueue.side_effect = ConnectionError('redis secret')
    ids = [uuid7(), uuid7()]
    with caplog.at_level('ERROR'):
        result = readiness.dispatch_pending_invitations(ids)
    assert result.unwrap_err() == 'readiness_dispatch_failed'
    assert 'invitation_dispatch_failed' in caplog.text
    assert all(str(i) in caplog.text for i in ids)
    assert 'secret' not in caplog.text


def test_readiness_dispatch_survives_an_unexpected_exception(
    worker, monkeypatch, caplog
):
    monkeypatch.setattr(
        service,
        'enqueue_invitation_dispatch',
        MagicMock(side_effect=RuntimeError('boom')),
    )
    with caplog.at_level('ERROR'):
        result = readiness.dispatch_pending_invitations([uuid7()])
    assert result.unwrap_err() == 'readiness_dispatch_failed'
    assert 'invitation_dispatch_exception' in caplog.text
