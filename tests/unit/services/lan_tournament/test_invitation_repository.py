"""Pure mocked storage contracts; PostgreSQL proof lives in integration tests."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation
from byceps.services.lan_tournament.models.match_readiness import InvitationStatus
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5)


@pytest.fixture
def storage(monkeypatch):
    session = Mock()
    monkeypatch.setattr(repo, 'db', SimpleNamespace(session=session))
    row = DbMatchInvitation(uuid7(), uuid7(), uuid7(), 1, uuid7(), 'pending', 1)
    monkeypatch.setattr(repo, '_lock_invitation', Mock(return_value=(row, Mock())))
    monkeypatch.setattr(repo, '_invitation_ineligibility', Mock(return_value=None))
    return row, session


def test_reservation_counts_enqueue_failure_and_is_frozen(storage):
    row, session = storage
    result = repo.claim_invitation_dispatch_flush(row.id, expected_token=None, now=NOW)
    assert result.is_ok()
    reserved = result.unwrap()
    assert reserved.attempts == 1 and reserved.status == InvitationStatus.DISPATCHING
    assert reserved.lease_until == NOW + timedelta(seconds=120)
    with pytest.raises(FrozenInstanceError):
        reserved.attempts = 2
    assert repo.record_invitation_outcome_flush(
        row.id, reserved.dispatch_token, status=InvitationStatus.FAILED,
        now=NOW, error='enqueue_failed', retryable=True,
    ).is_ok()
    assert row.attempts == 1 and row.next_attempt_at == NOW + timedelta(seconds=30)
    session.commit.assert_not_called()
    assert session.flush.call_count == 2


# fmt: off
@pytest.mark.parametrize(('stage', 'outcome', 'ok'), [
    ('dispatching', InvitationStatus.QUEUED, True),
    ('dispatching', InvitationStatus.ACCEPTED, False),
    ('queued', InvitationStatus.SENDING, True),
    ('queued', InvitationStatus.ACCEPTED, False),
    ('sending', InvitationStatus.ACCEPTED, True),
    ('accepted', InvitationStatus.QUEUED, False),
    ('delivery_unknown', InvitationStatus.FAILED, False),
])
# fmt: on
def test_stage_cas(storage, stage, outcome, ok):
    row, session = storage
    row.status, row.dispatch_token = stage, uuid7()
    row.lease_until = NOW + timedelta(seconds=120)
    result = repo.record_invitation_outcome_flush(
        row.id, row.dispatch_token, status=outcome, now=NOW,
    )
    assert result.is_ok() == ok
    assert session.flush.call_count == int(ok)
    session.commit.assert_not_called()


def test_stale_token_cannot_mutate(storage):
    row, session = storage
    row.status, row.dispatch_token = 'sending', uuid7()
    row.lease_until = NOW + timedelta(seconds=120)
    assert repo.record_invitation_outcome_flush(
        row.id, uuid7(), status=InvitationStatus.ACCEPTED, now=NOW,
    ).unwrap_err() == 'invitation_conflict'
    assert row.status == 'sending'
    session.flush.assert_not_called()


def test_expired_sending_late_acceptance_is_conservative_unknown(storage):
    row, session = storage
    row.status, row.dispatch_token, row.lease_until = 'sending', uuid7(), NOW
    assert repo.record_invitation_outcome_flush(
        row.id, row.dispatch_token, status=InvitationStatus.ACCEPTED, now=NOW,
    ).is_ok()
    assert row.status == 'delivery_unknown' and row.accepted_at is None
    assert row.last_error == 'sending_lease_expired' and row.lease_until is None
    session.commit.assert_not_called()


@pytest.mark.parametrize('attempts', [1, 2, 3])
def test_bounded_retry_delays(storage, attempts):
    row, _ = storage
    row.status, row.dispatch_token, row.attempts = 'sending', uuid7(), attempts
    row.lease_until = NOW + timedelta(seconds=120)
    assert repo.record_invitation_outcome_flush(
        row.id, row.dispatch_token, status=InvitationStatus.FAILED, now=NOW,
        error='x' * 1000, retryable=True,
    ).is_ok()
    expected = NOW + timedelta(seconds=30 if attempts == 1 else 120) if attempts < 3 else None
    assert row.next_attempt_at == expected and len(row.last_error) == 500


def test_permanent_failure_is_not_automatically_retryable(storage):
    row, _ = storage
    row.status = 'failed'
    assert not repo._invitation_retryable(row, NOW)
    for status in ('accepted', 'suppressed', 'delivery_unknown', 'sending'):
        row.status = status
        assert not repo._invitation_retryable(row, NOW)


def test_aware_times_normalize_to_plain_utc():
    assert repo._invitation_time(NOW.replace(tzinfo=UTC)) == NOW


def test_lock_order_and_fresh_work_read(monkeypatch):
    calls = []
    tournament_id, match_id, invitation_id = uuid7(), uuid7(), uuid7()
    session = Mock()
    session.execute.return_value.first.return_value = SimpleNamespace(tournament_id=tournament_id, match_id=match_id)
    session.get.side_effect = lambda *args, **kwargs: calls.append(('match_read', kwargs))
    session.scalar.side_effect = lambda query: calls.append(('work_read', str(query)))
    monkeypatch.setattr(repo, 'db', SimpleNamespace(session=session))
    monkeypatch.setattr(repo, 'lock_tournament_for_update', lambda id: calls.append(('tournament', id)))
    monkeypatch.setattr(repo, 'lock_matches_for_update', lambda ids: calls.append(('matches', ids)))
    repo._lock_invitation(invitation_id)
    assert [call[0] for call in calls] == ['tournament', 'matches', 'match_read', 'work_read']
    assert calls[2][1] == {'populate_existing': True}
    assert 'FOR UPDATE' in calls[3][1]


def test_reservation_refuses_expired_or_not_due_work(storage):
    row, session = storage
    row.status, row.next_attempt_at = 'failed', NOW + timedelta(seconds=30)
    assert repo.claim_invitation_dispatch_flush(row.id, expected_token=None, now=NOW).is_err()
    row.next_attempt_at = NOW
    row.attempts = 3
    assert repo.claim_invitation_dispatch_flush(row.id, expected_token=None, now=NOW).is_err()
    session.flush.assert_not_called()


@pytest.mark.parametrize('retirement', ['tournament_terminal', 'pairing_retired'])
@pytest.mark.parametrize('temporary', ['readiness_hold', 'tournament_paused', 'readiness_reset', 'readiness_revision_changed', 'recipient_not_current'])
def test_irreversible_suppression_preserves_provenance(storage, retirement, temporary):
    row, _ = storage
    row.status, row.last_error = 'suppressed', retirement
    before = repo._db_invitation_to_invitation(row)
    repo._suppress_invitation(row, temporary)
    assert repo._db_invitation_to_invitation(row) == before


@pytest.mark.parametrize('retirement', ['tournament_terminal', 'pairing_retired'])
def test_retired_work_is_not_retryable_even_when_due(storage, retirement):
    row, _ = storage
    row.status, row.last_error = 'failed', retirement
    row.next_attempt_at = NOW
    assert not repo._invitation_retryable(row, NOW)


@pytest.mark.parametrize('stage', ['dispatching', 'queued'])
def test_matching_token_moves_on_after_the_lease_expired(storage, stage):
    row, session = storage
    row.status, row.dispatch_token, row.attempts = stage, uuid7(), 1
    row.lease_until = NOW
    later = NOW + timedelta(seconds=121)
    assert repo.record_invitation_outcome_flush(
        row.id, row.dispatch_token, status=InvitationStatus.SENDING, now=later,
    ).is_ok()
    assert row.status == 'sending'
    assert row.lease_until == later + timedelta(seconds=120)
    session.flush.assert_called_once()


def test_late_job_with_a_retired_token_is_a_conflict(storage):
    row, session = storage
    row.status, row.dispatch_token, row.lease_until = 'pending', None, None
    assert repo.record_invitation_outcome_flush(
        row.id, uuid7(), status=InvitationStatus.SENDING,
        now=NOW + timedelta(seconds=121),
    ).unwrap_err() == 'invitation_conflict'
    session.flush.assert_not_called()


# fmt: off
@pytest.mark.parametrize(('status', 'attempts', 'expected'), [
    ('dispatching', 2, 1),
    ('queued', 1, 0),
    ('queued', 0, 0),
    ('pending', 2, 2),
    ('failed', 2, 2),
    ('sending', 2, 2),
])
# fmt: on
def test_only_pre_send_tokens_give_their_attempt_back(storage, status, attempts, expected):
    row, _ = storage
    row.status, row.attempts = status, attempts
    repo._suppress_invitation(row, 'readiness_hold')
    assert row.attempts == expected and row.status == 'suppressed'
    repo._suppress_invitation(row, 'readiness_hold')
    assert row.attempts == expected


# fmt: off
@pytest.mark.parametrize(('status', 'last_error', 'attempts', 'spent'), [
    ('pending', None, 3, True),
    ('pending', 'pre_send_lease_expired', 4, True),
    ('pending', None, 2, False),
    ('suppressed', 'readiness_hold', 3, True),
    ('suppressed', 'tournament_paused', 3, True),
    ('suppressed', 'smtp_suppressed', 3, False),
    ('suppressed', 'pairing_retired', 3, False),
    ('failed', 'attempts_exhausted', 3, False),
    ('failed', None, 3, False),
    ('queued', None, 3, False),
    ('sending', None, 3, False),
    ('accepted', None, 3, False),
])
# fmt: on
def test_spent_work_is_resumable_work_without_attempts(storage, status, last_error, attempts, spent):
    row, _ = storage
    row.status, row.last_error, row.attempts = status, last_error, attempts
    assert repo._invitation_spent(row) is spent


def test_exhausted_work_is_final(storage):
    row, _ = storage
    row.status, row.attempts, row.dispatch_token = 'pending', 3, uuid7()
    row.next_attempt_at = NOW
    repo._exhaust_invitation(row)
    assert (row.status, row.last_error) == ('failed', 'attempts_exhausted')
    assert row.dispatch_token is row.lease_until is row.next_attempt_at is None
    assert not repo._invitation_retryable(row, NOW + timedelta(days=1))
    assert not repo._invitation_spent(row)
