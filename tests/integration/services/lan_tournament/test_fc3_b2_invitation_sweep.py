"""Sweep, batched dispatch and handler hand-over against PostgreSQL (fix cycle 3, B2)."""

from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from byceps.database import db
from byceps.services.email.models import Message, NameAndAddress
from byceps.services.lan_tournament import (
    notification_handlers as handlers,
    signals,
    tournament_invitation_service as service,
    tournament_notification_service as messages,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service as lifecycle,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.match_readiness import (
    InvitationStatus,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Ok
from byceps.util.uuid import uuid7


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('fc3-b2-sweep'), 'FC3 B2 sweep')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Fc3B2Sweep{i}') for i in range(4)]


def make_tournament(party, users, status, matches):
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Sweep {uuid7()}',
        now,
        game_format='ONE_V_ONE',
        elimination_mode='SINGLE_ELIMINATION',
        tournament_status=status,
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, now)
        for user in users
    ]
    db.session.add_all(participants)
    match_ids = []
    for order in range(matches):
        match = DbTournamentMatch(
            uuid7(), tournament.id, now, match_order=order, round=order
        )
        db.session.add(match)
        db.session.flush()
        for participant in participants[order * 2 : order * 2 + 2]:
            db.session.add(
                DbTournamentMatchToContestant(
                    uuid7(),
                    match.id,
                    now,
                    participant_id=participant.id,
                )
            )
        db.session.flush()
        match_ids.append(match.id)
    db.session.flush()
    return tournament.id, match_ids, now


@pytest.fixture
def mail(monkeypatch):
    smtp = SimpleNamespace(suppress_send=False)
    monkeypatch.setattr(
        service,
        'get_current_byceps_app',
        lambda: SimpleNamespace(byceps_config=SimpleNamespace(smtp=smtp)),
    )
    monkeypatch.setattr(
        messages,
        'build_match_invitation_message',
        Mock(
            side_effect=lambda _tid, _mid, uid: Ok(
                Message(
                    sender=NameAndAddress('LAN', 'lan@example.test'),
                    recipients=[f'{uid}@example.test'],
                    subject='Invitation',
                    body='Play',
                )
            )
        ),
    )
    send = Mock()
    scheduled = Mock()
    monkeypatch.setattr(service.email_service, 'send_email', send)
    monkeypatch.setattr(service.jobqueue, 'enqueue_at', scheduled)
    return SimpleNamespace(send=send, scheduled=scheduled)


def seed_ongoing(party, users, matches=2):
    tournament_id, match_ids, now = make_tournament(
        party, users, 'ONGOING', matches
    )
    ids = {}
    for match_id in match_ids:
        repo.get_tournament_for_update(tournament_id)
        repo.get_match_for_update(match_id)
        assert repo.refresh_match_pairing_flush(
            match_id, occurred_at=now
        ).is_ok()
        contestants = repo.get_contestants_for_match(match_id)
        recipients = [
            db.session.get(DbTournamentParticipant, c.participant_id).user_id
            for c in contestants
        ]
        ids[match_id] = repo.ensure_invitation_intents_flush(
            match_id,
            recipients,
            occurred_at=now,
        )
    db.session.commit()
    return tournament_id, match_ids, ids


def status_of(invitation_id):
    db.session.expire_all()
    return repo.get_match_invitation(invitation_id)


def test_expired_queued_row_is_redispatched_by_an_unrelated_dispatch(
    party, users, mail
):
    tournament_id, match_ids, ids = seed_ongoing(party, users)
    stuck_match, live_match = match_ids
    for invitation_id in ids[stuck_match]:
        row = db.session.get(DbMatchInvitation, invitation_id)
        row.status = InvitationStatus.QUEUED.value
        row.dispatch_token = uuid7()
        row.attempts = 1
        row.lease_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=5
        )
    db.session.commit()
    # No status change and no start/resume handler is involved.
    assert service.dispatch_match_invitations(ids[live_match]).unwrap() == 2
    for invitation_id in [*ids[stuck_match], *ids[live_match]]:
        assert status_of(invitation_id).status == InvitationStatus.ACCEPTED
    assert mail.send.call_count == 4
    # One follow-up per tournament for the batch; the sweep it ran added none.
    assert mail.scheduled.call_count == 1
    due, function, scheduled_for = mail.scheduled.call_args.args
    assert (
        function is service.sweep_tournament_invitations
        and scheduled_for == tournament_id
    )
    assert (
        timedelta(seconds=140)
        < due - datetime.now(UTC)
        <= timedelta(seconds=150)
    )


def test_the_follow_up_job_recovers_what_a_lost_job_left_behind(
    party, users, mail
):
    tournament_id, match_ids, ids = seed_ongoing(party, users, matches=1)
    for invitation_id in ids[match_ids[0]]:
        row = db.session.get(DbMatchInvitation, invitation_id)
        row.status = InvitationStatus.DISPATCHING.value
        row.dispatch_token = uuid7()
        row.attempts = 1
        row.lease_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
    db.session.commit()
    assert service.sweep_tournament_invitations(tournament_id).unwrap() == 2
    assert all(
        status_of(i).status == InvitationStatus.ACCEPTED
        for i in ids[match_ids[0]]
    )
    assert mail.send.call_count == 2
    assert mail.scheduled.call_count == 1
    # Nothing left: a second sweep dispatches nothing and schedules nothing.
    assert service.sweep_tournament_invitations(tournament_id).unwrap() == 0
    assert mail.send.call_count == 2
    assert mail.scheduled.call_count == 1


def test_sweep_failure_does_not_escape_the_dispatch_job(
    party, users, mail, monkeypatch
):
    tournament_id, match_ids, ids = seed_ongoing(party, users, matches=1)
    monkeypatch.setattr(
        repo,
        'recover_expired_invitations_flush',
        Mock(side_effect=RuntimeError('private database detail')),
    )
    assert service.dispatch_match_invitations(ids[match_ids[0]]).unwrap() == 2
    assert all(
        status_of(i).status == InvitationStatus.ACCEPTED
        for i in ids[match_ids[0]]
    )


def test_post_commit_dispatch_runs_one_job_for_the_whole_batch(
    party, users, mail, monkeypatch
):
    tournament_id, match_ids, ids = seed_ongoing(party, users)
    every = [i for match_id in match_ids for i in ids[match_id]]
    real_enqueue = service.jobqueue.enqueue
    seen = []

    def enqueue(function, *args, **kwargs):
        seen.append(function)
        return real_enqueue(function, *args, **kwargs)

    monkeypatch.setattr(service.jobqueue, 'enqueue', enqueue)
    assert readiness.dispatch_pending_invitations(every).is_ok()
    assert seen.count(service.dispatch_match_invitations) == 1
    assert seen.count(service.deliver_match_invitation) == 4
    assert mail.send.call_count == 4
    assert all(status_of(i).status == InvitationStatus.ACCEPTED for i in every)


def test_failed_batch_enqueue_leaves_pending_rows_for_the_sweep(
    party, users, mail, monkeypatch
):
    tournament_id, match_ids, ids = seed_ongoing(party, users, matches=1)
    real_enqueue = service.jobqueue.enqueue
    monkeypatch.setattr(
        service.jobqueue,
        'enqueue',
        Mock(side_effect=ConnectionError('private redis')),
    )
    result = readiness.dispatch_pending_invitations(ids[match_ids[0]])
    assert result.unwrap_err() == 'readiness_dispatch_failed'
    assert all(
        status_of(i).status == InvitationStatus.PENDING
        for i in ids[match_ids[0]]
    )
    mail.send.assert_not_called()
    monkeypatch.setattr(service.jobqueue, 'enqueue', real_enqueue)
    assert service.sweep_tournament_invitations(tournament_id).unwrap() == 2
    assert all(
        status_of(i).status == InvitationStatus.ACCEPTED
        for i in ids[match_ids[0]]
    )


def test_resume_hands_over_to_the_owner_and_delivers_each_invitation_once(
    party, users, mail, monkeypatch
):
    tournament_id, match_ids, now = make_tournament(party, users, 'PAUSED', 2)
    db.session.commit()
    reconciled = Mock(wraps=service.reconcile_match_invitations_flush)
    monkeypatch.setattr(
        service, 'reconcile_match_invitations_flush', reconciled
    )
    signals.tournament_status_changed.connect(
        handlers._on_tournament_status_changed
    )
    try:
        # The pairing rows the engine would have written when the matches were set.
        for match_id in match_ids:
            repo.get_tournament_for_update(tournament_id)
            repo.get_match_for_update(match_id)
            assert repo.refresh_match_pairing_flush(
                match_id, occurred_at=now
            ).is_ok()
        db.session.commit()
        result = lifecycle.change_status(
            tournament_id, TournamentStatus.ONGOING
        )
        assert result.is_ok(), result
    finally:
        signals.tournament_status_changed.disconnect(
            handlers._on_tournament_status_changed
        )
    # Only the owner's lifecycle reconcile ran: one per match, none from the handler.
    assert [c.args[0] for c in reconciled.call_args_list] == sorted(
        match_ids, key=str
    )
    db.session.expire_all()
    rows = db.session.scalars(
        select(DbMatchInvitation).where(
            DbMatchInvitation.tournament_id == tournament_id
        )
    ).all()
    assert len(rows) == 4
    assert {r.status for r in rows} == {InvitationStatus.ACCEPTED.value}
    assert {r.attempts for r in rows} == {1}
    assert mail.send.call_count == 4


def test_ffa_match_has_no_recipient_work_for_the_match_ready_handler_to_miss(
    party, users
):
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'FFA {uuid7()}',
        now,
        game_format='FREE_FOR_ALL',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, now)
        for user in users
    ]
    db.session.add_all(participants)
    match = DbTournamentMatch(
        uuid7(), tournament.id, now, match_order=0, round=0
    )
    db.session.add(match)
    db.session.flush()
    for participant in participants:
        db.session.add(
            DbTournamentMatchToContestant(
                uuid7(),
                match.id,
                now,
                participant_id=participant.id,
            )
        )
    db.session.commit()
    result = service.reconcile_match_invitations_flush(
        match.id, occurred_at=now
    )
    db.session.commit()
    assert result.unwrap() == ()
    assert (
        db.session.scalars(
            select(DbMatchInvitation).where(
                DbMatchInvitation.match_id == match.id
            )
        ).all()
        == []
    )
