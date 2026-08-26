"""Actual PostgreSQL ledger and module worker, with explicit seeded intents.

Lifecycle-owner intent wiring is Issue 15, not implied by these fixtures.
SMTP is observed at its public boundary; no inbox-delivery claim is made.

The tests from the restart section on use the real readiness operations and
read every persisted fact through a new session and connection; restarts run
in a thread of their own with its own application context.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from smtplib import SMTPAuthenticationError, SMTPDataError, SMTPResponseException, SMTPServerDisconnected
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.email.models import Message, NameAndAddress
from byceps.services.lan_tournament import (
    notification_handlers as handlers, signals,
    tournament_invitation_service as service,
    tournament_notification_service as messages,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service as lifecycle,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import DbTournamentMatchToContestant
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.events import MatchReadyEvent, TournamentStatusChangedEvent
from byceps.services.lan_tournament.models.match_readiness import InvitationStatus
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


REAL_BUILDER = messages.build_match_invitation_message
REAL_ENQUEUE = service.jobqueue.enqueue
REAL_ENQUEUE_AT = service.jobqueue.enqueue_at
REAL_SEND_EMAIL = service.email_service.send_email


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('issue14-worker'), 'Issue 14 worker')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Issue14Recipient{i}') for i in range(4)]


@pytest.fixture
def setup(party, users, monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(uuid7(), party.id, f'Worker {uuid7()}', now,
                              game_format='ONE_V_ONE', tournament_status='ONGOING')
    db.session.add(tournament)
    db.session.flush()
    participants = [DbTournamentParticipant(uuid7(), user.id, tournament.id, now) for user in users]
    db.session.add_all(participants)
    match = DbTournamentMatch(uuid7(), tournament.id, now, match_order=0, round=0)
    db.session.add(match)
    db.session.flush()
    for participant in participants[:2]:
        db.session.add(DbTournamentMatchToContestant(uuid7(), match.id, now, participant_id=participant.id))
    db.session.flush()
    repo.get_tournament_for_update(tournament.id)
    repo.get_match_for_update(match.id)
    assert repo.refresh_match_pairing_flush(match.id, occurred_at=now).is_ok()
    # Explicit Issue 13 ledger initialization, NOT future owner-transaction proof.
    ids = repo.ensure_invitation_intents_flush(match.id, [user.id for user in users[:2]], occurred_at=now)
    db.session.commit()
    smtp = SimpleNamespace(suppress_send=False)
    monkeypatch.setattr(service, 'get_current_byceps_app', lambda: SimpleNamespace(byceps_config=SimpleNamespace(smtp=smtp)))
    build = Mock(side_effect=lambda _tid, _mid, uid: Ok(Message(
        sender=NameAndAddress('LAN', 'lan@example.test'), recipients=[f'{uid}@example.test'],
        subject='Invitation', body='Opponent and seat; footer',
    )))
    send = Mock()
    scheduled = Mock()
    monkeypatch.setattr(messages, 'build_match_invitation_message', build)
    monkeypatch.setattr(service.email_service, 'send_email', send)
    monkeypatch.setattr(service.jobqueue, 'enqueue_at', scheduled)
    yield SimpleNamespace(
        tournament_id=tournament.id, match_id=match.id, ids=ids,
        user_ids=[user.id for user in users], participant_ids=[p.id for p in participants],
        smtp=smtp, build=build, send=send, scheduled=scheduled,
    )
    db.session.rollback()


@pytest.fixture(autouse=True)
def single_recipient_dispatch(monkeypatch):
    """Pin the per-recipient state machine apart from the activity sweep.

    A dispatch sweeps its whole tournament, so the unsent sibling these tests
    keep pending would be sent too. `test_fc3_b2_invitation_sweep.py` runs the
    sweep itself.
    """
    monkeypatch.setattr(service, '_sweep_touched', lambda batch: None)


def reserve(invitation_id):
    current = repo.get_match_invitation(invitation_id)
    claimed = repo.claim_invitation_dispatch_flush(invitation_id, expected_token=current.dispatch_token, now=datetime.now(UTC))
    assert claimed.is_ok(), claimed
    repo.commit_session()
    return claimed.unwrap().dispatch_token


def reconcile(setup):
    result = service.reconcile_match_invitations_flush(setup.match_id, occurred_at=datetime.now(UTC))
    assert result.is_ok(), result
    repo.commit_session()
    return result.unwrap()


def assignment(setup):
    return MatchReadyEvent(occurred_at=datetime.now(UTC), initiator=None,
                           tournament_id=setup.tournament_id, match_id=setup.match_id)


def resume(setup):
    handlers._on_tournament_status_changed(None, event=TournamentStatusChangedEvent(
        occurred_at=datetime.now(UTC), initiator=None, tournament_id=setup.tournament_id,
        old_status=TournamentStatus.PAUSED, new_status=TournamentStatus.ONGOING,
    ))


def test_assignment_invites_without_claims(setup):
    match = repo.get_match(setup.match_id)
    assert match.ready_at_a is None and match.ready_at_b is None
    # The emitting writer reconciles in its transaction and queues after the commit.
    assert readiness.dispatch_pending_invitations(reconcile(setup)).is_ok()
    assert setup.send.call_count == 2
    assert all(repo.get_match_invitation(i).status == InvitationStatus.ACCEPTED for i in setup.ids)
    assert repo.get_match(setup.match_id).both_ready_notified_at is None
    # The signal handler adds nothing on top of that.
    handlers._on_match_ready(None, event=assignment(setup))
    assert setup.send.call_count == 2


def test_both_ready_sends_no_second_email(setup):
    assert service.dispatch_match_invitations(setup.ids).unwrap() == 2
    signals.match_both_ready.send(None, event=assignment(setup))
    assert setup.send.call_count == 2
    assert repo.get_match(setup.match_id).both_ready_notified_at is None


def test_sync_worker_acceptance_not_overwritten_by_enqueue(setup):
    # Actual util.jobqueue.enqueue and RQ Queue(is_async=False), not a lambda worker.
    real_enqueue = service.jobqueue.enqueue
    with patch.object(service.jobqueue, 'enqueue', wraps=real_enqueue) as queued:
        assert service.dispatch_match_invitations(setup.ids).unwrap() == 2
    assert queued.call_count == 2
    assert all(c.args[0] is service.deliver_match_invitation for c in queued.call_args_list)
    for invitation_id in setup.ids:
        fact = repo.get_match_invitation(invitation_id)
        assert fact.status == InvitationStatus.ACCEPTED and fact.accepted_at is not None
        assert fact.attempts == 1 and fact.lease_until is None


def test_async_queue_records_queued_then_worker_acceptance(setup):
    from byceps.byceps_app import get_current_byceps_app
    from rq import Queue

    # Unique owned queue; actual util enqueue + asynchronous RQ job execution.
    queue = Queue(f'issue14-{uuid7()}', connection=get_current_byceps_app().redis_client, is_async=True)
    jobs = []
    try:
        with patch.object(service.jobqueue, 'get_queue', return_value=queue):
            assert service.dispatch_match_invitations(setup.ids).unwrap() == 2
        jobs = [queue.fetch_job(job_id) for job_id in queue.get_job_ids()]
        assert len(jobs) == 2
        assert all(repo.get_match_invitation(i).status == InvitationStatus.QUEUED for i in setup.ids)
        setup.send.assert_not_called()
        for job in jobs:
            assert job.func_name == f'{service.__name__}.deliver_match_invitation'
            assert job.perform().is_ok()
        assert setup.send.call_count == 2
        assert all(repo.get_match_invitation(i).status == InvitationStatus.ACCEPTED for i in setup.ids)
    finally:
        for job in jobs:
            queue.remove(job.id)
            job.delete()


@pytest.mark.parametrize('failure_stage', ['enqueue', 'smtp'])
def test_definite_failure_retries_at_bound(setup, failure_stage):
    invitation_id = setup.ids[0]
    if failure_stage == 'smtp':
        setup.send.side_effect = SMTPDataError(550, b'private rejection')
    real_datetime = service.datetime
    clock = datetime.now(UTC)
    class Clock:
        @staticmethod
        def now(_tz):
            return clock
    with patch.object(service, 'datetime', Clock):
        for attempt, delay in [(1, 30), (2, 120), (3, None)]:
            if failure_stage == 'enqueue':
                with patch.object(service.jobqueue, 'enqueue', side_effect=ConnectionError('private redis')):
                    assert service.dispatch_match_invitations([invitation_id]).is_err()
            else:
                assert service.dispatch_match_invitations([invitation_id]).unwrap() == 1
            fact = repo.get_match_invitation(invitation_id)
            assert fact.status == InvitationStatus.FAILED and fact.attempts == attempt
            expected = (clock + timedelta(seconds=delay)).replace(tzinfo=None) if delay else None
            assert fact.next_attempt_at == expected
            if delay:
                call = setup.scheduled.call_args
                assert call.args == (expected, service.dispatch_match_invitations, (invitation_id,))
                clock += timedelta(seconds=delay)
        assert service.dispatch_match_invitations([invitation_id]).unwrap() == 0
    assert service.datetime is real_datetime
    assert setup.scheduled.call_count == 2
    assert 'private' not in repo.get_match_invitation(invitation_id).last_error


@pytest.mark.parametrize('failure', ['render', 'config', 'address', 'auth', 'exception'])
def test_permanent_failure_does_not_spin(setup, failure):
    if failure in {'render', 'config', 'address'}:
        setup.build.side_effect = None
        setup.build.return_value = Err({'render': 'email_template_formatting_failed', 'config': 'email_config_missing', 'address': 'email_address_missing'}[failure])
    elif failure == 'auth':
        setup.send.side_effect = SMTPAuthenticationError(535, b'private credentials')
    else:
        setup.build.side_effect = RuntimeError('private lookup')
    invitation_id = setup.ids[0]
    assert service.dispatch_match_invitations([invitation_id]).unwrap() == 1
    fact = repo.get_match_invitation(invitation_id)
    assert fact.status == InvitationStatus.FAILED and fact.next_attempt_at is None
    assert service.dispatch_match_invitations([invitation_id]).unwrap() == 0
    assert repo.get_match_invitation(invitation_id).attempts == 1
    setup.scheduled.assert_not_called()
    assert 'private' not in fact.last_error


@pytest.mark.parametrize('exception', [TimeoutError('private'), SMTPServerDisconnected('private'), RuntimeError('private')])
def test_ambiguous_sending_is_delivery_unknown(setup, exception):
    setup.send.side_effect = exception
    invitation_id = setup.ids[0]
    assert service.dispatch_match_invitations([invitation_id]).unwrap() == 1
    assert repo.get_match_invitation(invitation_id).status == InvitationStatus.DELIVERY_UNKNOWN
    assert service.dispatch_match_invitations([invitation_id]).unwrap() == 0
    assert invitation_id not in reconcile(setup)
    assert invitation_id not in repo.select_invitation_retry_ids_flush(setup.tournament_id, now=datetime.now(UTC))
    setup.scheduled.assert_not_called()
    assert setup.send.call_count == 1


def test_smtp_suppressed_is_not_accepted(setup):
    setup.smtp.suppress_send = True
    assert service.dispatch_match_invitations(setup.ids).unwrap() == 2
    for invitation_id in setup.ids:
        fact = repo.get_match_invitation(invitation_id)
        assert fact.status == InvitationStatus.SUPPRESSED
        assert fact.last_error == 'smtp_suppressed' and fact.accepted_at is None
    setup.send.assert_not_called()
    assert reconcile(setup) == ()


def test_resume_does_not_repeat_accepted(setup):
    accepted, unsent = setup.ids
    assert service.dispatch_match_invitations([accepted]).unwrap() == 1
    tournament = db.session.get(DbTournament, setup.tournament_id)
    tournament.tournament_status = 'PAUSED'
    db.session.commit()
    assert reconcile(setup) == ()
    assert repo.get_match_invitation(unsent).last_error == 'tournament_paused'
    db.session.get(DbTournament, setup.tournament_id).tournament_status = 'ONGOING'
    db.session.commit()
    # `change_status` reconciles in the owning transaction; the handler sweeps.
    assert reconcile(setup) == (unsent,)
    resume(setup)
    assert setup.send.call_count == 2
    assert repo.get_match_invitation(accepted).attempts == 1
    assert repo.get_match_invitation(unsent).status == InvitationStatus.ACCEPTED
    resume(setup)
    assert setup.send.call_count == 2


def test_revoke_holds_until_fresh_claim(setup):
    accepted, held = setup.ids
    assert service.dispatch_match_invitations([accepted]).unwrap() == 1
    old_token = reserve(held)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.B, True)
    repo.set_readiness_revision_flush(setup.match_id, 2)
    db.session.commit()
    assert reconcile(setup) == ()
    assert service.deliver_match_invitation(held, old_token).is_err()
    resume(setup)
    assert setup.send.call_count == 1
    # Fresh-claim effects supplied explicitly; owner operations are future Issue15.
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, False)
    repo.set_readiness_revision_flush(setup.match_id, 3)
    db.session.commit()
    assert reconcile(setup) == ()
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.B, False)
    repo.set_readiness_revision_flush(setup.match_id, 4)
    db.session.commit()
    assert reconcile(setup) == (held,)
    assert service.deliver_match_invitation(held, old_token).is_err()
    assert service.dispatch_match_invitations([held]).unwrap() == 1
    assert setup.send.call_count == 2
    assert repo.get_match_invitation(accepted).attempts == 1


# fmt: off
@pytest.mark.parametrize('invalid', [
    'generation', 'revision', 'roster', 'confirmed', 'paused', 'terminal', 'hold',
])
# fmt: on
def test_stale_pairing_job_does_not_send(setup, invalid):
    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    match = db.session.get(DbTournamentMatch, setup.match_id)
    if invalid == 'generation':
        match.pairing_generation += 1
    elif invalid == 'revision':
        match.readiness_revision += 1
    elif invalid == 'roster':
        recipient = repo.get_match_invitation(invitation_id).recipient_id
        participant = db.session.scalar(select(DbTournamentParticipant).where(DbTournamentParticipant.tournament_id == setup.tournament_id, DbTournamentParticipant.user_id == recipient))
        participant.removed_at = datetime.now(UTC).replace(tzinfo=None)
    elif invalid == 'confirmed':
        match.confirmed_by = setup.user_ids[0]
    elif invalid in {'paused', 'terminal'}:
        db.session.get(DbTournament, setup.tournament_id).tournament_status = 'PAUSED' if invalid == 'paused' else 'CANCELLED'
    else:
        match.invitation_hold_a = True
    db.session.commit()
    assert service.deliver_match_invitation(invitation_id, token).is_err()
    setup.send.assert_not_called()
    assert repo.get_match_invitation(invitation_id).status != InvitationStatus.ACCEPTED


def test_revision_reconciliation_retires_old_token(setup):
    invitation_id = setup.ids[0]
    old = reserve(invitation_id)
    repo.set_readiness_revision_flush(setup.match_id, 2)
    db.session.commit()
    assert invitation_id in reconcile(setup)
    assert repo.get_match_invitation(invitation_id).dispatch_token is None
    assert service.deliver_match_invitation(invitation_id, old).is_err()
    assert service.dispatch_match_invitations([invitation_id]).unwrap() == 1
    assert setup.send.call_count == 1


@pytest.mark.parametrize('stage', [InvitationStatus.DISPATCHING, InvitationStatus.QUEUED, InvitationStatus.SENDING])
def test_process_restart_recovers_pending_intents(setup, stage):
    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    if stage != InvitationStatus.DISPATCHING:
        assert repo.record_invitation_outcome_flush(invitation_id, token, status=stage, now=datetime.now(UTC)).is_ok()
    row = db.session.get(DbMatchInvitation, invitation_id)
    row.lease_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db.session.commit()
    db.session.remove()  # persisted ledger survives an entirely fresh session
    resume(setup)
    expected = InvitationStatus.DELIVERY_UNKNOWN if stage == InvitationStatus.SENDING else InvitationStatus.ACCEPTED
    assert repo.get_match_invitation(invitation_id).status == expected
    assert service.deliver_match_invitation(invitation_id, token).is_err()
    assert setup.send.call_count == (1 if stage == InvitationStatus.SENDING else 2)


def test_partial_failure_keeps_other_recipient_accepted(setup):
    setup.send.side_effect = [None, SMTPDataError(550, b'rejected')]
    assert service.dispatch_match_invitations(setup.ids).unwrap() == 2
    accepted, failed = [repo.get_match_invitation(i) for i in sorted(setup.ids, key=str)]
    assert accepted.status == InvitationStatus.ACCEPTED
    assert failed.status == InvitationStatus.FAILED
    row = db.session.get(DbMatchInvitation, failed.id)
    row.next_attempt_at = datetime.now(UTC).replace(tzinfo=None)
    db.session.commit()
    setup.send.side_effect = None
    assert service.dispatch_match_invitations([failed.id]).unwrap() == 1
    assert repo.get_match_invitation(accepted.id).attempts == 1
    assert setup.send.call_count == 3


def test_external_call_releases_all_database_locks(setup):
    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    def send(*_args):
        assert not db.session().in_transaction()
        with Session(db.engine) as independent:
            for model, entity_id in [(DbTournament, setup.tournament_id), (DbTournamentMatch, setup.match_id), (DbMatchInvitation, invitation_id)]:
                assert independent.scalar(select(model).where(model.id == entity_id).with_for_update(nowait=True)) is not None
    setup.send.side_effect = send
    assert service.deliver_match_invitation(invitation_id, token).is_ok()
    assert repo.get_match_invitation(invitation_id).status == InvitationStatus.ACCEPTED


@pytest.mark.parametrize('later_change', ['pause', 'revoke', 'replacement', 'deletion'])
def test_inflight_outcome_is_own_historical_token(setup, later_change):
    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    generation = repo.get_match_invitation(invitation_id).pairing_generation
    def send(*_args):
        if later_change == 'pause':
            db.session.get(DbTournament, setup.tournament_id).tournament_status = 'PAUSED'
        elif later_change == 'revoke':
            repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
            repo.set_readiness_revision_flush(setup.match_id, 2)
        elif later_change == 'replacement':
            repo.delete_contestants_for_match_flush(setup.match_id)
        else:
            repo.delete_contestants_for_tournament_flush(setup.tournament_id)
            repo.delete_matches_for_tournament(setup.tournament_id, commit=False)
            repo.delete_participants_for_tournament_flush(setup.tournament_id)
            repo.delete_tournament(setup.tournament_id, commit=False)
        db.session.commit()
    setup.send.side_effect = send
    assert service.deliver_match_invitation(invitation_id, token).is_ok()
    fact = repo.get_match_invitation(invitation_id)
    assert fact.status == InvitationStatus.ACCEPTED
    assert fact.dispatch_token == token and fact.pairing_generation == generation
    if later_change == 'replacement':
        assert repo.get_match(setup.match_id).pairing_generation > generation
    if later_change == 'deletion':
        assert repo.find_match(setup.match_id) is None
        assert repo.find_tournament(setup.tournament_id) is None


def test_reconcile_preserves_caller_commit_and_rollback(setup):
    invitation_id = setup.ids[0]
    before = repo.get_match_invitation(invitation_id).expected_readiness_revision
    repo.set_readiness_revision_flush(setup.match_id, 2)
    with patch.object(repo, 'commit_session', side_effect=AssertionError('internal commit')):
        assert service.reconcile_match_invitations_flush(setup.match_id, occurred_at=datetime.now(UTC)).is_ok()
    with Session(db.engine) as independent:
        assert independent.get(DbMatchInvitation, invitation_id).expected_readiness_revision == before
    db.session.rollback()
    assert repo.get_match_invitation(invitation_id).expected_readiness_revision == before
    setup.send.assert_not_called()


def test_historical_unknown_excluded_from_catchup(setup):
    for invitation_id in setup.ids:
        db.session.get(DbMatchInvitation, invitation_id).status = 'delivery_unknown'
    db.session.commit()
    resume(setup)
    setup.send.assert_not_called()
    assert all(repo.get_match_invitation(i).status == InvitationStatus.DELIVERY_UNKNOWN for i in setup.ids)


def test_actual_message_builder_and_worker_preserve_standard_mail(setup, monkeypatch, email_config, brand, users):
    from byceps.services.snippet import snippet_service
    from byceps.services.snippet.models import SnippetScope

    messages.create_match_ready_email_snippets(brand, users[0])
    scope = SnippetScope.for_brand(brand.id)
    for language in ['en', 'de']:
        if snippet_service.find_current_version_of_snippet_with_name(scope, 'email_footer', language) is None:
            snippet_service.create_snippet(scope, 'email_footer', language, users[0], 'Standard footer')
    monkeypatch.setattr(messages, 'build_match_invitation_message', REAL_BUILDER)
    assert service.dispatch_match_invitations(setup.ids).unwrap() == 2
    for call in setup.send.call_args_list:
        sender, recipients, subject, body = call.args
        assert sender == email_config.sender.format()
        assert len(recipients) == 1 and recipients[0].endswith('@users.test')
        assert repo.get_tournament(setup.tournament_id).name in subject
        assert 'Standard footer' in body
    assert all(repo.get_match_invitation(i).status == InvitationStatus.ACCEPTED for i in setup.ids)


def test_actual_util_enqueue_at_schedules_only_module_dispatch(setup, monkeypatch):
    from byceps.byceps_app import get_current_byceps_app
    from rq.registry import ScheduledJobRegistry

    queue = service.jobqueue.get_queue(get_current_byceps_app())
    registry = ScheduledJobRegistry(queue=queue)
    before = set(registry.get_job_ids())
    monkeypatch.setattr(service.jobqueue, 'enqueue_at', REAL_ENQUEUE_AT)
    setup.send.side_effect = SMTPDataError(550, b'rejected')
    created = set()
    try:
        assert service.dispatch_match_invitations([setup.ids[0]]).unwrap() == 1
        created = set(registry.get_job_ids()) - before
        assert len(created) == 1
        job = queue.fetch_job(created.pop())
        created.add(job.id)
        assert job.func_name == f'{service.__name__}.dispatch_match_invitations'
        assert job.args == ((setup.ids[0],),)
        assert repo.get_match_invitation(setup.ids[0]).next_attempt_at is not None
    finally:
        # Remove ONLY this test's newly observed job, never the shared queue.
        for job_id in created:
            registry.remove(job_id, delete_job=True)


@pytest.mark.parametrize('failed_stage', ['pre_send', 'post_send'])
def test_bookkeeping_exception_recovery_is_stage_aware(setup, failed_stage):
    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    real_record = repo.record_invitation_outcome_flush
    def record(*args, **kwargs):
        if kwargs['status'] == (InvitationStatus.SENDING if failed_stage == 'pre_send' else InvitationStatus.ACCEPTED):
            raise RuntimeError('private database failure')
        return real_record(*args, **kwargs)
    with patch.object(repo, 'record_invitation_outcome_flush', side_effect=record):
        assert service.deliver_match_invitation(invitation_id, token).unwrap_err() == 'invitation_record_failed'
    assert setup.send.call_count == (0 if failed_stage == 'pre_send' else 1)
    db.session.get(DbMatchInvitation, invitation_id).lease_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db.session.commit()
    db.session.remove()
    resume(setup)
    fact = repo.get_match_invitation(invitation_id)
    assert fact.status == (InvitationStatus.ACCEPTED if failed_stage == 'pre_send' else InvitationStatus.DELIVERY_UNKNOWN)
    assert setup.send.call_count == 2
    assert service.deliver_match_invitation(invitation_id, token).is_err()


def test_catchup_selection_cannot_clear_hold_or_unknown(setup):
    db.session.get(DbMatchInvitation, setup.ids[0]).status = 'delivery_unknown'
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    db.session.commit()
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=datetime.now(UTC)) == []
    db.session.commit()
    assert repo.get_match(setup.match_id).invitation_hold_a
    assert repo.get_match_invitation(setup.ids[0]).status == InvitationStatus.DELIVERY_UNKNOWN
    assert repo.get_match_invitation(setup.ids[1]).attempts == 0


def test_late_smtp_success_after_lease_expiry_is_unknown(setup):
    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    clock = datetime.now(UTC)
    class Clock:
        @staticmethod
        def now(_tz):
            return clock
    def send(*_args):
        nonlocal clock
        clock += timedelta(seconds=121)
    setup.send.side_effect = send
    with patch.object(service, 'datetime', Clock):
        assert service.deliver_match_invitation(invitation_id, token).is_ok()
    fact = repo.get_match_invitation(invitation_id)
    assert fact.status == InvitationStatus.DELIVERY_UNKNOWN
    assert fact.last_error == 'sending_lease_expired' and fact.accepted_at is None
    assert invitation_id not in reconcile(setup)
    setup.scheduled.assert_not_called()


def test_worker_lock_order_uses_fresh_work_after_tournament_and_match(setup):
    from sqlalchemy import event

    invitation_id = setup.ids[0]
    token = reserve(invitation_id)
    locks = []
    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if 'FOR UPDATE' in statement:
            for table in ['lan_tournament_matches', 'lan_tournament_match_invitations', 'lan_tournaments']:
                if f'FROM {table} ' in statement or f'FROM {table}\n' in statement:
                    locks.append(table)
                    break
    event.listen(db.engine, 'before_cursor_execute', observe)
    try:
        assert service.deliver_match_invitation(invitation_id, token).is_ok()
    finally:
        event.remove(db.engine, 'before_cursor_execute', observe)
    assert locks[:3] == ['lan_tournaments', 'lan_tournament_matches', 'lan_tournament_match_invitations']
    assert locks[3:6] == locks[:3]


@pytest.mark.parametrize('smtp_outcome', ['accepted', 'auth', 'data_rejected', 'quit_after_acceptance'])
def test_public_core_sender_observes_real_smtp_stage_ambiguity(setup, monkeypatch, smtp_outcome):
    from byceps.byceps_app import get_current_byceps_app

    config = replace(get_current_byceps_app().byceps_config.smtp, suppress_send=False,
                     username='test-user', password='test-password')
    monkeypatch.setattr(service.email_service, 'get_current_byceps_app', lambda: SimpleNamespace(byceps_config=SimpleNamespace(smtp=config)))
    monkeypatch.setattr(service.email_service, 'send_email', REAL_SEND_EMAIL)
    smtp = MagicMock()
    constructor = MagicMock(return_value=smtp)
    monkeypatch.setattr(service.email_service, 'SMTP', constructor)
    connection = smtp.__enter__.return_value
    if smtp_outcome == 'auth':
        connection.login.side_effect = SMTPAuthenticationError(535, b'private credentials')
    elif smtp_outcome == 'data_rejected':
        connection.send_message.side_effect = SMTPDataError(550, b'private rejection')
    elif smtp_outcome == 'quit_after_acceptance':
        # send_message returned, but public send_email did not: phase is hidden.
        smtp.__exit__.side_effect = SMTPResponseException(500, b'private QUIT failure')
    invitation_id = setup.ids[0]
    assert service.dispatch_match_invitations([invitation_id]).unwrap() == 1
    constructor.assert_called_once_with(config.host, config.port)
    connection.login.assert_called_once_with('test-user', 'test-password')
    fact = repo.get_match_invitation(invitation_id)
    if smtp_outcome == 'accepted':
        assert fact.status == InvitationStatus.ACCEPTED
    elif smtp_outcome == 'quit_after_acceptance':
        connection.send_message.assert_called_once()
        assert fact.status == InvitationStatus.DELIVERY_UNKNOWN
        assert invitation_id not in reconcile(setup)
        setup.scheduled.assert_not_called()
    else:
        assert fact.status == InvitationStatus.FAILED
        assert (fact.next_attempt_at is not None) is (smtp_outcome == 'data_rejected')
    assert fact.last_error is None or 'private' not in fact.last_error


# -- restart, owner transactions, holds, deduplication and unknown outcomes --


def fresh_rows(match_id):
    """The work of a match, read through a new session and connection."""
    with Session(db.engine) as independent:
        rows = independent.scalars(
            select(DbMatchInvitation)
            .where(DbMatchInvitation.match_id == match_id)
            .order_by(DbMatchInvitation.id)
        ).all()
        return [
            SimpleNamespace(
                id=row.id, recipient=row.recipient_id, status=row.status,
                attempts=row.attempts, token=row.dispatch_token,
                error=row.last_error, accepted_at=row.accepted_at,
                next_attempt_at=row.next_attempt_at,
                generation=row.pairing_generation,
            )
            for row in rows
        ]


def fresh_state(invitation_id, match_id):
    (row,) = [r for r in fresh_rows(match_id) if r.id == invitation_id]
    return row.status, row.attempts, row.error


def fresh_match(match_id):
    with Session(db.engine) as independent:
        match = independent.get(DbTournamentMatch, match_id)
        return SimpleNamespace(
            generation=match.pairing_generation, revision=match.readiness_revision,
            ready_by_a=match.ready_by_a, ready_by_b=match.ready_by_b,
            hold_a=match.invitation_hold_a, hold_b=match.invitation_hold_b,
        )


def in_new_session(app, call, timeout=60):
    """Run `call` like a restarted process: own thread, context and session."""
    outcome = SimpleNamespace(value=None, error=None, session=None, pid=None)

    def run():
        try:
            with app.app_context():
                try:
                    outcome.session = id(db.session())
                    outcome.pid = db.session.scalar(text('SELECT pg_backend_pid()'))
                    outcome.value = call()
                finally:
                    db.session.rollback()
                    db.session.remove()
        except BaseException as error:  # re-raised in the calling thread
            outcome.error = error

    thread = threading.Thread(target=run, name='restarted-process', daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f'the restarted session did not finish within {timeout} s'
    if outcome.error is not None:
        raise outcome.error
    return outcome


def sent_to(setup, user_id):
    address = f'{user_id}@example.test'
    return sum(1 for call in setup.send.call_args_list if address in call.args[1])


def add_match(setup, participant_ids, *, order):
    now = datetime.now(UTC).replace(tzinfo=None)
    match = DbTournamentMatch(uuid7(), setup.tournament_id, now, match_order=order, round=0)
    match_id = match.id
    db.session.add(match)
    db.session.flush()
    for participant_id in participant_ids:
        db.session.add(DbTournamentMatchToContestant(uuid7(), match_id, now, participant_id=participant_id))
    db.session.commit()
    return match_id


def actors_of(setup, match_id):
    pairing = repo.get_match_pairing(match_id)
    db.session.rollback()
    user_of = dict(zip(setup.participant_ids, setup.user_ids, strict=True))
    return {MatchSide.A: user_of[pairing.side_a.id], MatchSide.B: user_of[pairing.side_b.id]}


def operate(match_id, operation, side, actor, *, effects=True, **kwargs):
    """One Ready operation in its own transaction: flush, one commit, effects."""
    current = fresh_match(match_id)
    result = operation(
        match_id, side, actor, **kwargs,
        expected_pairing_generation=current.generation,
        expected_readiness_revision=current.revision,
    )
    change = result.unwrap()
    repo.commit_session()
    if effects:
        readiness.dispatch_readiness_effects(change)
    return change


@pytest.mark.parametrize('stage', [InvitationStatus.DISPATCHING, InvitationStatus.QUEUED, InvitationStatus.SENDING])
def test_restart_recovers_persisted_work_in_a_new_session_and_connection(setup, admin_app, stage):
    recipient_of = {r.id: r.recipient for r in fresh_rows(setup.match_id)}
    invitation_id, other_id = setup.ids
    token = reserve(invitation_id)
    if stage != InvitationStatus.DISPATCHING:
        assert repo.record_invitation_outcome_flush(invitation_id, token, status=stage, now=datetime.now(UTC)).is_ok()
    db.session.get(DbMatchInvitation, invitation_id).lease_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db.session.commit()
    # Only committed facts are visible to another connection.
    assert fresh_state(invitation_id, setup.match_id)[0] == stage.value
    # This transaction stays open, so the restart cannot reuse its connection.
    main_pid = db.session.scalar(text('SELECT pg_backend_pid()'))
    restarted = in_new_session(admin_app, lambda: resume(setup))
    assert restarted.session != id(db.session())
    assert restarted.pid != main_pid
    db.session.rollback()
    expected = InvitationStatus.DELIVERY_UNKNOWN if stage == InvitationStatus.SENDING else InvitationStatus.ACCEPTED
    assert fresh_state(invitation_id, setup.match_id)[0] == expected.value
    assert fresh_state(other_id, setup.match_id)[0] == 'accepted'
    # A sending attempt of the dead process is never replayed.
    assert sent_to(setup, recipient_of[invitation_id]) == (0 if stage == InvitationStatus.SENDING else 1)
    assert sent_to(setup, recipient_of[other_id]) == 1
    assert service.deliver_match_invitation(invitation_id, token).is_err()
    assert sent_to(setup, recipient_of[invitation_id]) == (0 if stage == InvitationStatus.SENDING else 1)


@pytest.mark.parametrize('failure', ['signal', 'queue', 'both'])
def test_failed_signal_and_queue_cannot_erase_committed_owner_intents(setup, admin_app, monkeypatch, failure):
    match_id = add_match(setup, setup.participant_ids[2:], order=1)
    recipients = set(setup.user_ids[2:])
    # The owner transaction creates the pairing and both intents together.
    now = datetime.now(UTC).replace(tzinfo=None)
    change = readiness.refresh_pairing_and_invitations_flush(match_id, occurred_at=now).unwrap()
    assert len(change.pending_invitation_ids) == 2
    assert fresh_rows(match_id) == []  # not yet committed, so not visible
    repo.commit_session()
    committed = fresh_rows(match_id)
    assert {r.id for r in committed} == set(change.pending_invitation_ids)
    assert {r.recipient for r in committed} == recipients
    assert {(r.status, r.attempts, r.token) for r in committed} == {('pending', 0, None)}
    # A claim hands the pending intents over to the post-commit effects.
    actors = actors_of(setup, match_id)
    claimed = operate(match_id, readiness.claim_ready_flush, MatchSide.A, actors[MatchSide.A], effects=False)
    assert set(claimed.pending_invitation_ids) == {r.id for r in committed}

    def explode(sender, *, event):
        raise RuntimeError('private listener failure')

    if failure in ('signal', 'both'):
        signals.match_ready_claimed.connect(explode, weak=False)
    if failure in ('queue', 'both'):
        monkeypatch.setattr(service.jobqueue, 'enqueue', Mock(side_effect=ConnectionError('private redis failure')))
    try:
        result = readiness.dispatch_readiness_effects(claimed)
    finally:
        if failure in ('signal', 'both'):
            signals.match_ready_claimed.disconnect(explode)
    assert result.unwrap_err() == 'readiness_dispatch_failed'
    # The committed claim and every committed intent survived, in a new session.
    assert fresh_match(match_id).ready_by_a == actors[MatchSide.A]
    after = fresh_rows(match_id)
    assert {r.id for r in after} == {r.id for r in committed}
    if failure == 'signal':
        # A failing listener cannot keep the durable work from being queued.
        assert {(r.status, r.attempts) for r in after} == {('accepted', 1)}
        assert all(sent_to(setup, user) == 1 for user in recipients)
    else:
        # One job carries the batch; if it never reached the queue, no recipient
        # was claimed and the rows stay durable `pending`.
        assert {(r.status, r.attempts, r.error) for r in after} == {('pending', 0, None)}
        setup.scheduled.assert_not_called()
        assert all(sent_to(setup, user) == 0 for user in recipients)
        # The recovery needs no signal: a restarted process finds the work.
        monkeypatch.setattr(service.jobqueue, 'enqueue', REAL_ENQUEUE)
        in_new_session(admin_app, lambda: resume(setup))
        recovered = fresh_rows(match_id)
        assert {(r.status, r.attempts) for r in recovered} == {('accepted', 1)}
        assert all(sent_to(setup, user) == 1 for user in recipients)
    in_new_session(admin_app, lambda: resume(setup))
    assert all(sent_to(setup, user) == 1 for user in recipients)


@pytest.mark.parametrize('order', ['a-then-b', 'b-then-a'])
def test_multiple_holds_keep_unsent_work_until_every_side_claims_again(setup, admin_app, order):
    accepted, held = setup.ids
    assert service.dispatch_match_invitations([accepted]).unwrap() == 1
    recipient_of = {r.id: r.recipient for r in fresh_rows(setup.match_id)}
    actors = actors_of(setup, setup.match_id)
    sides = (MatchSide.A, MatchSide.B)
    # The queue is down in this test: the post-commit effects never run, so
    # the unsent work is still pending when the revocations arrive.
    for side in sides:
        operate(setup.match_id, readiness.claim_ready_flush, side, actors[side], effects=False)
    for side in sides:
        operate(setup.match_id, readiness.revoke_ready_flush, side, actors[side], effects=False)
    match = fresh_match(setup.match_id)
    assert match.hold_a and match.hold_b
    assert fresh_state(held, setup.match_id) == ('suppressed', 0, 'readiness_hold')
    assert setup.send.call_count == 1
    in_new_session(admin_app, lambda: resume(setup))
    assert setup.send.call_count == 1
    assert fresh_state(held, setup.match_id) == ('suppressed', 0, 'readiness_hold')
    first, second = sides if order == 'a-then-b' else sides[::-1]
    # One fresh claim clears only its own hold, so nothing is released.
    change = operate(setup.match_id, readiness.claim_ready_flush, first, actors[first])
    assert change.pending_invitation_ids == ()
    holds = fresh_match(setup.match_id)
    assert {MatchSide.A: holds.hold_a, MatchSide.B: holds.hold_b} == {first: False, second: True}
    in_new_session(admin_app, lambda: resume(setup))
    assert setup.send.call_count == 1
    assert fresh_state(held, setup.match_id) == ('suppressed', 0, 'readiness_hold')
    # The last hold clears: exactly the unsent work is handed over, once.
    change = operate(setup.match_id, readiness.claim_ready_flush, second, actors[second])
    assert change.pending_invitation_ids == (held,)
    assert fresh_state(held, setup.match_id) == ('accepted', 1, None)
    assert fresh_state(accepted, setup.match_id) == ('accepted', 1, None)
    assert sent_to(setup, recipient_of[held]) == 1
    assert sent_to(setup, recipient_of[accepted]) == 1
    in_new_session(admin_app, lambda: resume(setup))
    assert setup.send.call_count == 2


def test_accepted_recipients_are_deduplicated_across_calls_and_sessions(setup, admin_app):
    accepted, other = setup.ids
    # One row per match, pairing generation and recipient, however often the
    # same audience is handed over.
    audience = [*setup.user_ids[:2], *setup.user_ids[:2], setup.user_ids[0]]
    again = repo.ensure_invitation_intents_flush(setup.match_id, audience, occurred_at=datetime.now(UTC))
    repo.commit_session()
    assert sorted(again, key=str) == sorted(setup.ids, key=str)
    assert {r.id for r in fresh_rows(setup.match_id)} == set(setup.ids)
    # Repeated IDs are reserved and sent once each.
    assert service.dispatch_match_invitations([accepted, accepted, other, accepted, other]).unwrap() == 2
    assert setup.send.call_count == 2
    # A restarted dispatcher in another session finds nothing left to send.
    restarted = in_new_session(admin_app, lambda: service.dispatch_match_invitations(setup.ids))
    assert restarted.value.unwrap() == 0
    for _ in range(2):
        assert reconcile(setup) == ()
        handlers._on_match_ready(None, event=assignment(setup))
        resume(setup)
        in_new_session(admin_app, lambda: resume(setup))
    assert setup.send.call_count == 2
    rows = fresh_rows(setup.match_id)
    assert len(rows) == 2
    assert {(r.status, r.attempts, r.error) for r in rows} == {('accepted', 1, None)}
    assert all(r.accepted_at is not None and r.token is not None for r in rows)


def test_historical_unknown_outcomes_are_preserved_by_every_owner_path(setup, admin_app):
    match_id = add_match(setup, setup.participant_ids[2:], order=1)
    recipients = set(setup.user_ids[2:])
    now = datetime.now(UTC).replace(tzinfo=None)
    # The shape of the migration backfill: work of a legacy ONGOING match,
    # delivery_unknown, no evidence either way.
    repo.get_tournament_for_update(setup.tournament_id)
    repo.get_match_for_update(match_id)
    assert repo.refresh_match_pairing_flush(match_id, occurred_at=now).is_ok()
    repo.ensure_invitation_intents_flush(match_id, setup.user_ids[2:], occurred_at=now, historical_unknown=True)
    repo.commit_session()
    before = fresh_rows(match_id)
    assert {r.recipient for r in before} == recipients
    assert {(r.status, r.attempts, r.token, r.error) for r in before} == {('delivery_unknown', 0, None, None)}
    actors = actors_of(setup, match_id)
    sides = (MatchSide.A, MatchSide.B)
    # Ready operations: both sides claim, one revokes and claims again, then
    # a same-pair reset. Each one reconciles the recipients of the match.
    for side in sides:
        operate(match_id, readiness.claim_ready_flush, side, actors[side])
    operate(match_id, readiness.revoke_ready_flush, MatchSide.A, actors[MatchSide.A])
    operate(match_id, readiness.claim_ready_flush, MatchSide.A, actors[MatchSide.A])
    reset =readiness.reset_readiness_flush(match_id, occurred_at=datetime.now(UTC).replace(tzinfo=None)).unwrap()
    repo.commit_session()
    readiness.dispatch_readiness_effects(reset)
    # Reconciliation, the assignment event and the caught-up resume.
    assert service.reconcile_match_invitations_flush(match_id, occurred_at=datetime.now(UTC)).unwrap() == ()
    repo.commit_session()
    handlers._on_match_ready(None, event=MatchReadyEvent(occurred_at=datetime.now(UTC), initiator=None, tournament_id=setup.tournament_id, match_id=match_id))
    # The lifecycle owner: pause and resume.
    assert lifecycle.change_status(setup.tournament_id, TournamentStatus.PAUSED, setup.user_ids[0]).is_ok()
    assert lifecycle.change_status(setup.tournament_id, TournamentStatus.ONGOING, setup.user_ids[0]).is_ok()
    # The automatic selection takes no unknown outcome either.
    selected = repo.select_invitation_retry_ids_flush(setup.tournament_id, now=datetime.now(UTC))
    repo.commit_session()
    assert not {r.id for r in before} & set(selected)
    in_new_session(admin_app, lambda: resume(setup))
    # Nothing about the historical work changed, and nothing was sent for it.
    assert fresh_rows(match_id) == before
    assert all(sent_to(setup, user) == 0 for user in recipients)
