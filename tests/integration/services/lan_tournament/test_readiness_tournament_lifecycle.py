"""Real lifecycle transactions and scoped revocation on private PostgreSQL.

Intents are explicitly seeded; complete writer-intent integration is Issue 15.
"""

from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock, call

from flask import current_app
import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_invitation_service as invitations,
    tournament_orga_repository as orga_repo,
    tournament_orga_service as orgas,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service as service,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import DbTournamentMatchToContestant
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation, DbMatchPairing
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import DbTournamentLogEntry
from byceps.services.lan_tournament.models.match_readiness import InvitationStatus
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import uuid7


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('issue10-1-lifecycle'), 'Lifecycle')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'LifecycleRecipient{i}') for i in range(3)]


@pytest.fixture
def setup(party, users, monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(
        uuid7(), party.id, f'Lifecycle {uuid7()}', now,
        game_format='ONE_V_ONE', elimination_mode='SINGLE_ELIMINATION',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, now)
        for user in users[:2]
    ]
    db.session.add_all(participants)
    matches = []
    ids = []
    for order in range(2):
        match = DbTournamentMatch(
            uuid7(), tournament.id, now, match_order=order, round=order,
        )
        db.session.add(match)
        db.session.flush()
        for participant in participants:
            db.session.add(DbTournamentMatchToContestant(
                uuid7(), match.id, now, participant_id=participant.id,
            ))
        db.session.flush()
        repo.get_tournament_for_update(tournament.id)
        repo.get_match_for_update(match.id)
        assert repo.refresh_match_pairing_flush(match.id, occurred_at=now).is_ok()
        ids.extend(repo.ensure_invitation_intents_flush(
            match.id, [user.id for user in users[:2]], occurred_at=now,
        ))
        matches.append(match.id)
    db.session.commit()
    status_signal = Mock()
    deleted_signal = Mock()
    revoked_signal = Mock()
    monkeypatch.setattr(signals.tournament_status_changed, 'send', status_signal)
    monkeypatch.setattr(signals.tournament_deleted, 'send', deleted_signal)
    monkeypatch.setattr(signals.tournament_orga_revoked, 'send', revoked_signal)
    enqueue = Mock()
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', enqueue)
    # A dispatched batch schedules its follow-up sweep; keep it out of Redis.
    enqueue_at = Mock()
    monkeypatch.setattr(invitations.jobqueue, 'enqueue_at', enqueue_at)
    yield SimpleNamespace(
        tournament_id=tournament.id, match_ids=matches, ids=ids,
        user_ids=[user.id for user in users], now=now,
        status_signal=status_signal, deleted_signal=deleted_signal,
        revoked_signal=revoked_signal, enqueue=enqueue, enqueue_at=enqueue_at,
    )
    db.session.rollback()


def seed_work(setup, status):
    for invitation_id in setup.ids:
        row = db.session.get(DbMatchInvitation, invitation_id)
        row.status = status.value
        row.attempts = 1
        row.dispatch_token = uuid7()
        row.lease_until = (
            setup.now + timedelta(minutes=2)
            if status in {InvitationStatus.DISPATCHING, InvitationStatus.QUEUED,
                          InvitationStatus.SENDING} else None
        )
        row.accepted_at = setup.now if status == InvitationStatus.ACCEPTED else None
        row.next_attempt_at = (
            setup.now + timedelta(seconds=30)
            if status == InvitationStatus.FAILED else None
        )
        row.last_error = 'definite_failure' if status == InvitationStatus.FAILED else None
    db.session.commit()


def work_snapshot(setup):
    return tuple(repo.get_match_invitation(item) for item in setup.ids)


def persisted_snapshot(setup):
    """Independent session sees only committed facts, not service identity maps."""
    with Session(db.engine) as observer:
        tournament = observer.get(DbTournament, setup.tournament_id)
        matches = observer.scalars(select(DbTournamentMatch).where(
            DbTournamentMatch.id.in_(setup.match_ids)
        ).order_by(DbTournamentMatch.id)).all()
        pairs = observer.scalars(select(DbMatchPairing).where(
            DbMatchPairing.match_id.in_(setup.match_ids)
        ).order_by(DbMatchPairing.id)).all()
        work = observer.scalars(select(DbMatchInvitation).where(
            DbMatchInvitation.id.in_(setup.ids)
        ).order_by(DbMatchInvitation.id)).all()
        logs = observer.scalars(select(DbTournamentLogEntry).where(
            DbTournamentLogEntry.tournament_id == setup.tournament_id
        ).order_by(DbTournamentLogEntry.id)).all()
        return (
            (tournament.name, tournament.tournament_status) if tournament else None,
            tuple((m.id, m.pairing_id, m.pairing_generation, m.readiness_revision,
                   m.ready_at_a, m.ready_by_a, m.invitation_hold_a,
                   m.occupied_since) for m in matches),
            tuple((p.id, p.started_at, p.ended_at, p.side_a_id, p.side_b_id) for p in pairs),
            tuple((w.id, w.status, w.dispatch_token, w.lease_until, w.accepted_at,
                   w.next_attempt_at, w.last_error, w.attempts) for w in work),
            tuple((entry.id, entry.event_type, entry.data) for entry in logs),
        )


# fmt: off
@pytest.mark.parametrize('new_status', [TournamentStatus.PAUSED, TournamentStatus.CANCELLED, TournamentStatus.COMPLETED])
@pytest.mark.parametrize('work_status', list(InvitationStatus))
# fmt: on
def test_lifecycle_suppresses_only_retractable_work(setup, new_status, work_status):
    seed_work(setup, work_status)
    before = work_snapshot(setup)
    facts = persisted_snapshot(setup)
    result = service.change_status(setup.tournament_id, new_status, setup.user_ids[2])
    assert result.is_ok(), result
    assert result.unwrap()[0].tournament_status == new_status
    after = work_snapshot(setup)
    if work_status in {InvitationStatus.ACCEPTED, InvitationStatus.SENDING,
                       InvitationStatus.DELIVERY_UNKNOWN}:
        assert after == before
    elif new_status == TournamentStatus.PAUSED and work_status == InvitationStatus.FAILED:
        # Definite failures retain their classification/delay but cannot reserve.
        assert after == before
        assert repo.claim_invitation_dispatch_flush(
            setup.ids[0], expected_token=before[0].dispatch_token,
            now=setup.now + timedelta(seconds=31),
        ).unwrap_err() == 'tournament_paused'
        repo.rollback_session()
    else:
        reason = 'tournament_paused' if new_status == TournamentStatus.PAUSED else 'tournament_terminal'
        assert all(w.status == InvitationStatus.SUPPRESSED and w.last_error == reason for w in after)
        assert all(w.dispatch_token is None and w.lease_until is None and w.next_attempt_at is None for w in after)
    current = persisted_snapshot(setup)
    assert current[1:3] == facts[1:3]  # No claims/holds/pairing/occupancy rewrite.
    setup.status_signal.assert_called_once()
    setup.enqueue.assert_not_called()


@pytest.mark.parametrize('retained_status', ['accepted', 'delivery_unknown'])
def test_resume_reconciles_paused_work_without_clearing_holds_or_accepted(setup, retained_status):
    row = db.session.get(DbMatchInvitation, setup.ids[0])
    row.status = retained_status
    row.accepted_at = setup.now if retained_status == 'accepted' else None
    row.attempts = 1
    row.dispatch_token = uuid7()  # Retained token from the completed sending attempt.
    row.lease_until = row.next_attempt_at = row.last_error = None
    db.session.commit()
    accepted = repo.get_match_invitation(setup.ids[0])
    assert service.pause_tournament(setup.tournament_id).is_ok()
    held_match = db.session.get(DbTournamentMatch, setup.match_ids[1])
    held_match.invitation_hold_a = True
    db.session.commit()
    commits = []
    session = db.session()

    def committed(session):
        commits.append('commit')

    def observed(*args, **kwargs):
        assert commits == ['commit']  # One owner commit before any worker stage.
        snapshot = persisted_snapshot(setup)
        assert snapshot[0][1] == 'ONGOING'
        work = {item[0]: item for item in snapshot[3]}
        assert work[setup.ids[1]][1] == 'pending'
        assert all(work[i][6] == 'readiness_hold' for i in setup.ids[2:])
        assert repo.get_match_invitation(setup.ids[0]) == accepted

    def queued(function, invitation_id, token):
        assert function is invitations.deliver_match_invitation
        assert invitation_id == setup.ids[1]
        assert len(commits) == 2  # Separate committed worker reservation.
        snapshot = persisted_snapshot(setup)
        assert snapshot[0][1] == 'ONGOING'
        work = {item[0]: item for item in snapshot[3]}
        assert work[invitation_id][1:3] == ('dispatching', token)
        assert work[invitation_id][3] is not None
        assert work[setup.ids[0]][1] == retained_status
        assert all(work[i][1] == 'suppressed' and work[i][6] == 'readiness_hold'
                   for i in setup.ids[2:])

    def run_batch_inline(function, *args):
        if function is invitations.dispatch_match_invitations:
            return function(*args)  # Synchronous RQ runs the batch job in place.
        return queued(function, *args)

    setup.status_signal.side_effect = observed
    setup.enqueue.side_effect = run_batch_inline
    event.listen(session, 'after_commit', committed)
    try:
        assert service.resume_tournament(setup.tournament_id).is_ok()
    finally:
        event.remove(session, 'after_commit', committed)
    # Owner, reservation, QUEUED bookkeeping, then the batch's own sweep.
    assert len(commits) == 4
    assert repo.get_match_invitation(setup.ids[0]) == accepted
    eligible = repo.get_match_invitation(setup.ids[1])
    assert eligible.status == InvitationStatus.QUEUED
    assert eligible.dispatch_token is not None and eligible.lease_until is not None
    assert eligible.accepted_at is None and eligible.attempts == 1
    assert all(repo.get_match_invitation(i).last_error == 'readiness_hold' for i in setup.ids[2:])
    assert repo.get_match(setup.match_ids[1]).invitation_hold_a
    assert setup.enqueue.call_args_list == [
        call(invitations.dispatch_match_invitations, (eligible.id,)),
        call(invitations.deliver_match_invitation, eligible.id, eligible.dispatch_token),
    ]
    setup.enqueue_at.assert_called_once()


def test_pause_resume_preserves_permanent_failure(setup):
    seed_work(setup, InvitationStatus.FAILED)
    for item in setup.ids:
        row = db.session.get(DbMatchInvitation, item)
        row.next_attempt_at = None
        row.last_error = 'invalid_address'
    db.session.commit()
    before = work_snapshot(setup)
    assert service.pause_tournament(setup.tournament_id).is_ok()
    assert service.resume_tournament(setup.tournament_id).is_ok()
    assert work_snapshot(setup) == before
    assert repo.select_invitation_retry_ids_flush(
        setup.tournament_id, now=datetime.now(UTC)
    ) == []
    repo.rollback_session()


def test_completed_reopen_keeps_terminal_work_retired(setup):
    assert service.end_tournament(setup.tournament_id).is_ok()
    before = work_snapshot(setup)
    refused = service.resume_tournament(setup.tournament_id)
    assert refused.is_err()
    assert service.change_status(
        setup.tournament_id, TournamentStatus.ONGOING,
        allow_completed_reopen=True,
    ).is_ok()
    assert work_snapshot(setup) == before


# fmt: off
@pytest.mark.parametrize('intervening_pause', [False, True])
# fmt: on
def test_terminal_retirement_survives_later_hold_or_pause_reconciliation(setup, intervening_pause):
    assert service.end_tournament(setup.tournament_id).is_ok()
    retired = work_snapshot(setup)
    if not intervening_pause:
        # An outstanding readiness hold must not relabel terminal retirement.
        match = db.session.get(DbTournamentMatch, setup.match_ids[0])
        match.invitation_hold_a = True
        db.session.commit()
    assert service.change_status(
        setup.tournament_id, TournamentStatus.ONGOING,
        allow_completed_reopen=True,
    ).is_ok()
    if intervening_pause:
        assert service.pause_tournament(setup.tournament_id).is_ok()
        assert service.resume_tournament(setup.tournament_id).is_ok()
    else:
        match = db.session.get(DbTournamentMatch, setup.match_ids[0])
        match.invitation_hold_a = False
        db.session.commit()
        assert invitations.reconcile_match_invitations_flush(
            setup.match_ids[0], occurred_at=datetime.now(UTC),
        ).is_ok()
        repo.commit_session()
    assert work_snapshot(setup) == retired


def test_status_reads_fresh_under_tournament_first_ordered_locks(setup, monkeypatch):
    cached = db.session.get(DbTournament, setup.tournament_id)
    assert cached.tournament_status == 'ONGOING'
    with Session(db.engine) as other:
        other.get(DbTournament, setup.tournament_id).tournament_status = 'PAUSED'
        other.commit()
    calls = []
    lock_tournament = repo.lock_tournament_for_update
    lock_matches = repo.lock_matches_for_update

    def tournament_lock(tournament_id):
        calls.append(('tournament', tournament_id))
        lock_tournament(tournament_id)

    def match_locks(match_ids):
        calls.append(('matches', tuple(match_ids)))
        lock_matches(match_ids)

    monkeypatch.setattr(repo, 'lock_tournament_for_update', tournament_lock)
    monkeypatch.setattr(repo, 'lock_matches_for_update', match_locks)
    assert service.resume_tournament(setup.tournament_id).is_ok()
    assert calls[0] == ('tournament', setup.tournament_id)
    # Bulk lock covers all matches before any per-match reconciliation/work lock.
    assert set(calls[1][1]) == set(setup.match_ids)
    assert setup.status_signal.call_args.kwargs['event'].old_status == TournamentStatus.PAUSED


# fmt: off
@pytest.mark.parametrize('failure', ['audit', 'reconcile', 'reconcile_err', 'commit'])
# fmt: on
def test_status_failure_rolls_back_work_status_and_audit(setup, monkeypatch, failure):
    before = persisted_snapshot(setup)
    real_reconcile = invitations.reconcile_match_invitations_flush
    real_audit = service.create_log_entry

    def failed_audit(*args, **kwargs):
        real_audit(*args, **kwargs)
        raise RuntimeError('audit failure after flush')

    def failed_reconcile(*args, **kwargs):
        result = real_reconcile(*args, **kwargs)
        assert result.is_ok()
        # Another transaction cannot see the flushed work/status/audit.
        assert persisted_snapshot(setup) == before
        if failure == 'reconcile_err':
            return Err('invitation_audit_failed')
        raise RuntimeError('reconcile failure after flush')

    def failed_commit():
        assert persisted_snapshot(setup) == before
        raise RuntimeError('commit failure')

    if failure == 'audit':
        monkeypatch.setattr(service, 'create_log_entry', failed_audit)
    elif failure.startswith('reconcile'):
        monkeypatch.setattr(invitations, 'reconcile_match_invitations_flush', failed_reconcile)
    else:
        monkeypatch.setattr(repo, 'commit_session', failed_commit)
    if failure == 'reconcile_err':
        assert service.pause_tournament(setup.tournament_id).unwrap_err() == 'invitation_audit_failed'
    else:
        with pytest.raises(RuntimeError):
            service.pause_tournament(setup.tournament_id)
    assert persisted_snapshot(setup) == before
    setup.status_signal.assert_not_called()
    setup.enqueue.assert_not_called()


def test_status_commits_once_then_signal_observes_facts(setup):
    commits = []

    def committed(session):
        commits.append(session)

    def observed(*args, **kwargs):
        assert len(commits) == 1
        snapshot = persisted_snapshot(setup)
        assert snapshot[0][1] == 'PAUSED'
        assert all(item[1] == 'suppressed' for item in snapshot[3])
        assert snapshot[4][-1][1] == 'tournament-status-changed'

    setup.status_signal.side_effect = observed
    event.listen(db.session(), 'after_commit', committed)
    try:
        assert service.pause_tournament(setup.tournament_id).is_ok()
    finally:
        event.remove(db.session(), 'after_commit', committed)
    assert len(commits) == 1


# fmt: off
@pytest.mark.parametrize('work_status', list(InvitationStatus))
# fmt: on
def test_delete_retires_history_without_live_fk_blockers(setup, work_status):
    seed_work(setup, work_status)
    before = persisted_snapshot(setup)
    work = work_snapshot(setup)
    commits = []

    def committed(session):
        commits.append(session)

    def observed(*args, **kwargs):
        assert len(commits) == 1
        assert persisted_snapshot(setup)[0] is None

    setup.deleted_signal.side_effect = observed
    event.listen(db.session(), 'after_commit', committed)
    try:
        service.delete_tournament(setup.tournament_id, setup.user_ids[2])
    finally:
        event.remove(db.session(), 'after_commit', committed)
    after = persisted_snapshot(setup)
    assert len(commits) == 1
    assert after[0] is None and after[1] == ()
    assert len(after[2]) == len(before[2])
    for old, retired in zip(before[2], after[2], strict=True):
        assert retired[:2] == old[:2] and retired[3:] == old[3:]
        assert retired[2] is not None
    if work_status in {InvitationStatus.ACCEPTED, InvitationStatus.SENDING,
                       InvitationStatus.DELIVERY_UNKNOWN}:
        assert work_snapshot(setup) == work
    else:
        assert all(w.status == InvitationStatus.SUPPRESSED and w.last_error == 'pairing_retired' for w in work_snapshot(setup))
    assert after[4][-1][1] == 'tournament-deleted'
    # Purgeable audit rows are not the retained occupancy/pairing history.
    db.session.execute(delete(DbTournamentLogEntry).where(
        DbTournamentLogEntry.tournament_id == setup.tournament_id
    ))
    db.session.commit()
    assert persisted_snapshot(setup)[2:4] == after[2:4]
    setup.enqueue.assert_not_called()


# fmt: off
@pytest.mark.parametrize('failure', ['audit', 'after_retire', 'after_live_delete', 'commit'])
# fmt: on
def test_delete_failure_restores_occupancy_pairings_work_and_audit(setup, monkeypatch, failure):
    before = persisted_snapshot(setup)
    if failure == 'audit':
        target, name = service, 'create_log_entry'
    elif failure == 'after_retire':
        target, name = repo, 'delete_contestants_for_tournament'
    elif failure == 'after_live_delete':
        target, name = repo, 'delete_tournament'
    else:
        target, name = repo, 'commit_session'
    original = getattr(target, name)

    def fail(*args, **kwargs):
        if failure != 'commit':
            original(*args, **kwargs)
        if failure == 'after_retire':
            assert all(repo.get_match_pairing_history(mid)[0].ended_at for mid in setup.match_ids)
            assert all(w.last_error == 'pairing_retired' for w in work_snapshot(setup))
        if failure == 'after_live_delete':
            assert repo.find_tournament(setup.tournament_id) is None
        assert persisted_snapshot(setup) == before
        raise RuntimeError('failure after flush')

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(RuntimeError):
        service.delete_tournament(setup.tournament_id, setup.user_ids[2])
    assert persisted_snapshot(setup) == before
    setup.deleted_signal.assert_not_called()


def test_delete_audits_fresh_name_and_status(setup):
    cached = db.session.get(DbTournament, setup.tournament_id)
    assert cached.tournament_status == 'ONGOING'
    with Session(db.engine) as other:
        row = other.get(DbTournament, setup.tournament_id)
        row.name = f'Fresh {uuid7()}'
        row.tournament_status = 'CANCELLED'
        name = row.name
        other.commit()
    service.delete_tournament(setup.tournament_id, setup.user_ids[2])
    entry = persisted_snapshot(setup)[4][-1]
    assert entry[2]['name'] == name
    assert entry[2]['tournament_status'] == 'CANCELLED'


def test_orga_revocation_serializes(setup, monkeypatch):
    actor = setup.user_ids[2]  # Not a player/captain and has no global grant.
    assert orgas.assign_orga(setup.tournament_id, actor, actor).is_ok()
    # Warm appointment/authority facts before the independent revoker runs.
    assert orga_repo.find_orga_for_tournament_and_user(setup.tournament_id, actor)
    match = repo.get_match(setup.match_ids[0])
    assert readiness.claim_ready_flush(
        match.id, MatchSide.A, actor,
        expected_pairing_generation=match.pairing_generation,
        expected_readiness_revision=match.readiness_revision,
    ).is_ok()
    repo.rollback_session()
    started = Event()
    deleted = Event()
    outcomes = []
    pids = []
    app = current_app._get_current_object()
    original_lock = repo.lock_tournament_for_update
    original_delete = orga_repo.delete_orga

    def locking(tournament_id):
        pids.append(db.session.scalar(text('SELECT pg_backend_pid()')))
        started.set()
        original_lock(tournament_id)

    def deleting(orga_id):
        deleted.set()
        original_delete(orga_id)

    monkeypatch.setattr(repo, 'lock_tournament_for_update', locking)
    monkeypatch.setattr(orga_repo, 'delete_orga', deleting)

    def revoke():
        with app.app_context():
            try:
                outcomes.append(orgas.revoke_orga(setup.tournament_id, actor, actor))
            except Exception as exc:
                outcomes.append(exc)
            finally:
                db.session.remove()

    with Session(db.engine) as holder:
        holder.execute(select(DbTournament.id).where(
            DbTournament.id == setup.tournament_id
        ).with_for_update()).one()
        thread = Thread(target=revoke)
        thread.start()
        try:
            assert started.wait(5)
            deadline = monotonic() + 5
            while monotonic() < deadline:
                blocked = holder.scalar(text('SELECT cardinality(pg_blocking_pids(:pid))'), {'pid': pids[0]})
                if blocked:
                    break
                sleep(0.01)
            assert blocked, 'Revoker did not actually wait on the independent tournament lock'
            assert not deleted.is_set()  # Appointment delete cannot precede lock.
        finally:
            holder.rollback()
            thread.join(10)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and outcomes[0].is_ok(), outcomes
    assert deleted.is_set()
    assert not orgas.is_orga_for_tournament(actor, setup.tournament_id)
    # The same previously authorized actor now fails the real scope checks.
    match = repo.get_match(setup.match_ids[0])
    before = persisted_snapshot(setup)
    assert readiness.claim_ready_flush(
        match.id, MatchSide.A, actor,
        expected_pairing_generation=match.pairing_generation,
        expected_readiness_revision=match.readiness_revision,
    ).unwrap_err() == 'readiness_forbidden'
    repo.rollback_session()
    assert persisted_snapshot(setup) == before
    setup.revoked_signal.assert_called_once()


def test_revoke_audit_failure_restores_assignment_and_emits_nothing(setup, monkeypatch):
    actor = setup.user_ids[2]
    assert orgas.assign_orga(setup.tournament_id, actor, actor).is_ok()
    before = persisted_snapshot(setup)
    original = orgas.tournament_log_service.create_log_entry

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        assert not orgas.is_orga_for_tournament(actor, setup.tournament_id)
        with Session(db.engine) as other:
            assert other.scalar(text(
                'SELECT count(*) FROM lan_tournament_orgas WHERE tournament_id = :tid AND user_id = :uid'
            ), {'tid': setup.tournament_id, 'uid': actor}) == 1
        raise RuntimeError('audit failed after flush')

    monkeypatch.setattr(orgas.tournament_log_service, 'create_log_entry', fail)
    with pytest.raises(RuntimeError):
        orgas.revoke_orga(setup.tournament_id, actor, actor)
    assert orgas.is_orga_for_tournament(actor, setup.tournament_id)
    assert persisted_snapshot(setup) == before
    setup.revoked_signal.assert_not_called()
