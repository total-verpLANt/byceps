"""Invitation repository state machine: fix cycle 3 findings on PostgreSQL."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository as repo
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
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('fc3b1-invitations'), 'FC3 B1 invitations')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Fc3B1Recipient{i}') for i in range(6)]


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Fc3 B1 {uuid7()}',
        NOW,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), u.id, tournament.id, NOW)
        for u in users
    ]
    db.session.add_all(participants)
    match = DbTournamentMatch(
        uuid7(), tournament.id, NOW, match_order=0, round=0
    )
    db.session.add(match)
    db.session.flush()
    for p in participants[:2]:
        db.session.add(
            DbTournamentMatchToContestant(
                uuid7(), match.id, NOW, participant_id=p.id
            )
        )
    db.session.flush()
    repo.get_tournament_for_update(tournament.id)
    repo.get_match_for_update(match.id)
    assert repo.refresh_match_pairing_flush(match.id, occurred_at=NOW).is_ok()
    db.session.commit()
    yield SimpleNamespace(
        tournament_id=tournament.id,
        match_id=match.id,
        participant_ids=[p.id for p in participants],
        user_ids=[u.id for u in users],
    )
    db.session.rollback()


def intents(setup):
    return repo.ensure_invitation_intents_flush(
        setup.match_id, setup.user_ids[:2], occurred_at=NOW
    )


def reserve(invitation_id, now=NOW):
    token = repo.get_match_invitation(invitation_id).dispatch_token
    result = repo.claim_invitation_dispatch_flush(
        invitation_id, expected_token=token, now=now
    )
    assert result.is_ok(), result
    return result.unwrap().dispatch_token


# B1.0 -- bulk participant lock


def test_get_participants_for_update_locks_rows_in_id_order(setup):
    ids = list(setup.participant_ids)
    shuffled = [ids[3], ids[0], ids[5], ids[1]]
    participants = repo.get_participants_for_update(shuffled)
    assert [p.id for p in participants] == sorted(shuffled)
    assert all(p.tournament_id == setup.tournament_id for p in participants)
    with Session(db.engine) as independent:
        for participant_id in shuffled:
            with pytest.raises(Exception, match='could not obtain lock'):
                independent.scalar(
                    select(DbTournamentParticipant)
                    .where(DbTournamentParticipant.id == participant_id)
                    .with_for_update(nowait=True)
                )
            independent.rollback()
        free = ids[2]
        assert (
            independent.scalar(
                select(DbTournamentParticipant)
                .where(DbTournamentParticipant.id == free)
                .with_for_update(nowait=True)
            )
            is not None
        )
    db.session.rollback()


def test_get_participants_for_update_accepts_str_ids(setup):
    ids = [str(i) for i in setup.participant_ids[:3]]
    participants = repo.get_participants_for_update(ids)
    assert [p.id for p in participants] == sorted(setup.participant_ids[:3])
    db.session.rollback()


@contextmanager
def statements_executed():
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)


def test_get_participants_for_update_is_one_ordered_locking_select(setup):
    with statements_executed() as statements:
        repo.get_participants_for_update(setup.participant_ids)
    assert len(statements) == 1
    assert statements[0].rstrip().endswith('FOR UPDATE')
    assert 'ORDER BY lan_tournament_participants.id' in statements[0]
    db.session.rollback()


def test_get_participants_for_update_empty_input_runs_no_query(setup):
    with statements_executed() as statements:
        assert repo.get_participants_for_update([]) == []
    assert statements == []


def test_get_participants_for_update_refreshes_stale_rows(setup):
    participant_id = setup.participant_ids[0]
    row = db.session.get(DbTournamentParticipant, participant_id)
    with Session(db.engine) as other:
        other_row = other.get(DbTournamentParticipant, participant_id)
        other_row.substitute_player = True
        other.commit()
    assert row.substitute_player is False
    (participant,) = repo.get_participants_for_update([participant_id])
    assert participant.substitute_player is True
    db.session.rollback()
    with Session(db.engine) as other:
        other.get(
            DbTournamentParticipant, participant_id
        ).substitute_player = False
        other.commit()


# B1.1 -- a matching token is enough before SENDING, lease expiry included

LATE = NOW + timedelta(seconds=121)


def advance_to(invitation_id, stage):
    token = reserve(invitation_id)
    if stage != InvitationStatus.DISPATCHING:
        assert repo.record_invitation_outcome_flush(
            invitation_id, token, status=stage, now=NOW
        ).is_ok()
    return token


@pytest.mark.parametrize(
    'stage', [InvitationStatus.DISPATCHING, InvitationStatus.QUEUED]
)
def test_expired_lease_with_matching_token_may_reach_sending(setup, stage):
    a, _ = intents(setup)
    token = advance_to(a, stage)
    assert repo.get_match_invitation(a).lease_until <= LATE
    result = repo.record_invitation_outcome_flush(
        a, token, status=InvitationStatus.SENDING, now=LATE
    )
    assert result.is_ok(), result
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.SENDING
    assert fact.dispatch_token == token
    assert fact.lease_until == LATE + timedelta(seconds=120)


@pytest.mark.parametrize(
    'outcome_status', [InvitationStatus.FAILED, InvitationStatus.SUPPRESSED]
)
def test_expired_lease_with_matching_token_may_record_other_outcomes(
    setup, outcome_status
):
    a, _ = intents(setup)
    token = advance_to(a, InvitationStatus.QUEUED)
    result = repo.record_invitation_outcome_flush(
        a, token, status=outcome_status, now=LATE, error='late_outcome'
    )
    assert result.is_ok(), result
    fact = repo.get_match_invitation(a)
    assert fact.status == outcome_status
    assert fact.lease_until is None


def test_expired_lease_still_checks_eligibility(setup):
    a, _ = intents(setup)
    token = advance_to(a, InvitationStatus.QUEUED)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    result = repo.record_invitation_outcome_flush(
        a, token, status=InvitationStatus.SENDING, now=LATE
    )
    assert result.unwrap_err() == 'readiness_hold'
    assert repo.get_match_invitation(a).status == InvitationStatus.QUEUED
    db.session.rollback()


@pytest.mark.parametrize(
    'stage', [InvitationStatus.DISPATCHING, InvitationStatus.QUEUED]
)
def test_token_retired_by_recovery_gets_invitation_conflict(setup, stage):
    a, _ = intents(setup)
    old_token = advance_to(a, stage)
    assert repo.recover_expired_invitations_flush(
        setup.tournament_id, now=LATE
    ) == [a]
    result = repo.record_invitation_outcome_flush(
        a, old_token, status=InvitationStatus.SENDING, now=LATE
    )
    assert result.unwrap_err() == 'invitation_conflict'
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.PENDING
    assert fact.dispatch_token is None


def test_sending_with_expired_lease_is_delivery_unknown(setup):
    a, _ = intents(setup)
    token = advance_to(a, InvitationStatus.SENDING)
    result = repo.record_invitation_outcome_flush(
        a, token, status=InvitationStatus.ACCEPTED, now=LATE
    )
    assert result.is_ok(), result
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.DELIVERY_UNKNOWN
    assert fact.last_error == 'sending_lease_expired'
    assert fact.accepted_at is None


# B1.2 -- only work that reached SMTP consumes an attempt

MAX = repo.INVITATION_MAX_ATTEMPTS


def seed(invitation_id, **fields):
    row = db.session.get(DbMatchInvitation, invitation_id)
    for name, value in fields.items():
        setattr(row, name, value)
    db.session.flush()
    return row


def reconcile(setup):
    return repo.ensure_invitation_intents_flush(
        setup.match_id, setup.user_ids[:2], occurred_at=NOW
    )


def bump_revision(setup):
    match = db.session.get(DbTournamentMatch, setup.match_id)
    repo.set_readiness_revision_flush(
        setup.match_id, match.readiness_revision + 1
    )
    return reconcile(setup)


def test_revision_changes_while_queued_do_not_consume_attempts(setup):
    a, _ = intents(setup)
    for _ in range(3):
        token = reserve(a)
        assert repo.record_invitation_outcome_flush(
            a, token, status=InvitationStatus.QUEUED, now=NOW
        ).is_ok()
        assert a in bump_revision(setup)
        fact = repo.get_match_invitation(a)
        assert fact.status == InvitationStatus.PENDING
        assert fact.dispatch_token is None
        assert fact.attempts == 0
    token = reserve(a)
    assert repo.record_invitation_outcome_flush(
        a, token, status=InvitationStatus.SENDING, now=NOW
    ).is_ok()
    assert repo.get_match_invitation(a).attempts == 1


def test_revision_change_while_dispatching_refunds_once(setup):
    a, _ = intents(setup)
    reserve(a)
    assert repo.get_match_invitation(a).attempts == 1
    bump_revision(setup)
    assert repo.get_match_invitation(a).attempts == 0
    bump_revision(setup)
    assert repo.get_match_invitation(a).attempts == 0


def test_queued_lease_recovery_keeps_remaining_attempts(setup):
    a, _ = intents(setup)
    advance_to(a, InvitationStatus.QUEUED)
    assert repo.get_match_invitation(a).attempts == 1
    assert repo.recover_expired_invitations_flush(
        setup.tournament_id, now=LATE
    ) == [a]
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.PENDING
    assert fact.attempts == 0


def test_dispatching_lease_recovery_keeps_the_attempt_spent(setup):
    a, _ = intents(setup)
    advance_to(a, InvitationStatus.DISPATCHING)
    assert repo.recover_expired_invitations_flush(
        setup.tournament_id, now=LATE
    ) == [a]
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.PENDING
    assert fact.attempts == 1
    assert fact.last_error == 'pre_send_lease_expired'


def test_systematically_dying_claimer_exhausts_after_max_attempts(setup):
    a, _ = intents(setup)
    now = NOW
    for attempt in range(1, MAX + 1):
        reserve(a, now)
        assert repo.get_match_invitation(a).attempts == attempt
        now += timedelta(seconds=121)
        recovered = repo.recover_expired_invitations_flush(
            setup.tournament_id, now=now
        )
        assert recovered == ([a] if attempt < MAX else [])
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'
    assert fact.attempts == MAX
    assert fact.dispatch_token is None and fact.lease_until is None
    assert a not in repo.select_invitation_retry_ids_flush(
        setup.tournament_id, now=now + timedelta(days=1)
    )
    assert (
        repo.claim_invitation_dispatch_flush(
            a, expected_token=None, now=now + timedelta(days=1)
        ).unwrap_err()
        == 'invitation_conflict'
    )


def test_recovery_refunds_only_the_retired_token(setup):
    a, _ = intents(setup)
    seed(a, attempts=2)
    token = advance_to(a, InvitationStatus.QUEUED)
    assert repo.get_match_invitation(a).attempts == 3
    repo.recover_expired_invitations_flush(setup.tournament_id, now=LATE)
    assert repo.get_match_invitation(a).attempts == 2
    repo.recover_expired_invitations_flush(
        setup.tournament_id, now=LATE + timedelta(seconds=500)
    )
    assert repo.get_match_invitation(a).attempts == 2
    assert (
        repo.record_invitation_outcome_flush(
            a, token, status=InvitationStatus.SENDING, now=LATE
        ).unwrap_err()
        == 'invitation_conflict'
    )
    assert repo.get_match_invitation(a).attempts == 2


@pytest.mark.parametrize(
    'stage', [InvitationStatus.DISPATCHING, InvitationStatus.QUEUED]
)
def test_bulk_suppression_refunds_pre_send_tokens_only(setup, stage):
    a, b = intents(setup)
    advance_to(a, stage)
    token_b = advance_to(b, InvitationStatus.SENDING)
    repo.suppress_match_invitations_flush(
        setup.match_id, reason='readiness_hold'
    )
    repo.suppress_match_invitations_flush(
        setup.match_id, reason='readiness_hold'
    )
    db.session.expire_all()
    fact_a, fact_b = repo.get_match_invitation(a), repo.get_match_invitation(b)
    assert fact_a.status == InvitationStatus.SUPPRESSED
    assert fact_a.last_error == 'readiness_hold'
    assert fact_a.attempts == 0
    assert fact_b.status == InvitationStatus.SENDING
    assert fact_b.dispatch_token == token_b
    assert fact_b.attempts == 1


def test_bulk_suppression_keeps_loaded_rows_in_step(setup):
    a, _ = intents(setup)
    advance_to(a, InvitationStatus.QUEUED)
    loaded = db.session.get(DbMatchInvitation, a)
    assert loaded.attempts == 1
    repo.suppress_match_invitations_flush(
        setup.match_id, reason='readiness_hold'
    )
    assert (loaded.status, loaded.attempts) == ('suppressed', 0)
    db.session.flush()
    db.session.expire_all()
    assert repo.get_match_invitation(a).attempts == 0


def test_suppression_of_unsent_failed_work_does_not_refund(setup):
    a, _ = intents(setup)
    token = reserve(a)
    assert repo.record_invitation_outcome_flush(
        a,
        token,
        status=InvitationStatus.FAILED,
        now=NOW,
        error='invitation_enqueue_failed',
        retryable=True,
    ).is_ok()
    assert repo.get_match_invitation(a).attempts == 1
    repo.suppress_match_invitations_flush(
        setup.match_id, reason='readiness_hold'
    )
    db.session.expire_all()
    assert repo.get_match_invitation(a).attempts == 1


def test_enqueue_failure_still_counts(setup):
    a, _ = intents(setup)
    for expected in (1, 2, 3):
        token = reserve(a, NOW + timedelta(hours=expected))
        assert repo.record_invitation_outcome_flush(
            a,
            token,
            status=InvitationStatus.FAILED,
            now=NOW + timedelta(hours=expected),
            error='invitation_enqueue_failed',
            retryable=True,
        ).is_ok()
        assert repo.get_match_invitation(a).attempts == expected
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.next_attempt_at is None


def test_recovery_never_leaves_pending_work_without_attempts(setup):
    a, _ = intents(setup)
    advance_to(a, InvitationStatus.QUEUED)
    seed(a, attempts=MAX + 1)
    assert a not in repo.recover_expired_invitations_flush(
        setup.tournament_id, now=LATE
    )
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'
    assert fact.dispatch_token is None and fact.lease_until is None


def test_pending_row_with_spent_attempts_is_finalised_by_reconcile(setup):
    a, b = intents(setup)
    seed(a, attempts=MAX)
    assert a not in reconcile(setup)
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'
    assert fact.next_attempt_at is None and fact.dispatch_token is None
    assert repo.get_match_invitation(b).status == InvitationStatus.PENDING


def test_resumable_suppressed_row_with_spent_attempts_is_finalised(setup):
    a, _ = intents(setup)
    seed(a, status='suppressed', last_error='readiness_hold', attempts=MAX)
    assert a not in reconcile(setup)
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'


def test_non_resumable_suppressed_row_is_left_alone(setup):
    a, _ = intents(setup)
    seed(a, status='suppressed', last_error='smtp_suppressed', attempts=MAX)
    reconcile(setup)
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.SUPPRESSED
    assert fact.last_error == 'smtp_suppressed'


def test_selection_finalises_spent_rows_and_never_returns_them(setup):
    a, b = intents(setup)
    seed(a, attempts=MAX)
    assert repo.select_invitation_retry_ids_flush(
        setup.tournament_id, now=NOW
    ) == [b]
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'
    assert repo.select_invitation_retry_ids_flush(
        setup.tournament_id, now=NOW + timedelta(days=1)
    ) == [b]


def test_selection_finalises_resumable_suppressed_spent_rows(setup):
    a, b = intents(setup)
    seed(a, status='suppressed', last_error='tournament_paused', attempts=MAX)
    repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'


def test_exhausted_row_is_final_and_cannot_be_claimed_or_resumed(setup):
    a, _ = intents(setup)
    seed(a, attempts=MAX)
    repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
    for later in (NOW, NOW + timedelta(days=1)):
        assert a not in reconcile(setup)
        assert a not in repo.select_invitation_retry_ids_flush(
            setup.tournament_id, now=later
        )
        assert (
            repo.claim_invitation_dispatch_flush(
                a, expected_token=None, now=later
            ).unwrap_err()
            == 'invitation_conflict'
        )
    fact = repo.get_match_invitation(a)
    assert fact.status == InvitationStatus.FAILED
    assert fact.last_error == 'attempts_exhausted'


# B1.3 -- selection and eligibility cost

TOURNAMENT_READ = re.compile(r'FROM lan_tournaments\b')


def tournament_reads(statements):
    return [
        s
        for s in statements
        if TOURNAMENT_READ.search(s) and 'FOR UPDATE' not in s
    ]


def add_matches(setup, pairs):
    """Create further paired matches; return the invitation IDs of each."""
    ids = []
    repo.get_tournament_for_update(setup.tournament_id)
    for index, (a, b) in enumerate(pairs, start=1):
        match = DbTournamentMatch(
            uuid7(), setup.tournament_id, NOW, match_order=index, round=0
        )
        db.session.add(match)
        db.session.flush()
        for participant_id in (
            setup.participant_ids[a],
            setup.participant_ids[b],
        ):
            db.session.add(
                DbTournamentMatchToContestant(
                    uuid7(), match.id, NOW, participant_id=participant_id
                )
            )
        db.session.flush()
        repo.get_match_for_update(match.id)
        assert repo.refresh_match_pairing_flush(
            match.id, occurred_at=NOW
        ).is_ok()
        ids.extend(
            repo.ensure_invitation_intents_flush(
                match.id,
                [setup.user_ids[a], setup.user_ids[b]],
                occurred_at=NOW,
            )
        )
    db.session.commit()
    return ids


def add_terminal_rows(setup, quantity):
    """Seed rows that are not retryable; match IDs need no live match."""
    recipient = setup.user_ids[5]
    variants = [
        dict(status='failed', attempts=1, last_error='invitation_build_failed'),
        dict(
            status='failed',
            attempts=MAX,
            last_error='x',
            next_attempt_at=NOW - timedelta(days=1),
        ),
        dict(
            status='failed',
            attempts=1,
            last_error='pairing_retired',
            next_attempt_at=NOW - timedelta(days=1),
        ),
        dict(
            status='failed',
            attempts=1,
            last_error='x',
            next_attempt_at=NOW + timedelta(days=1),
        ),
    ]
    for index in range(quantity):
        fields = dict(variants[index % len(variants)])
        status = fields.pop('status')
        db.session.add(
            DbMatchInvitation(
                uuid7(),
                uuid7(),
                setup.tournament_id,
                0,
                recipient,
                status,
                0,
                **fields,
            )
        )
    db.session.commit()


def test_selection_ignores_terminal_rows_and_cost_does_not_grow(setup):
    add_matches(setup, [(0, 1), (2, 3), (4, 5)])
    rows = db.session.scalars(
        select(DbMatchInvitation)
        .where(DbMatchInvitation.tournament_id == setup.tournament_id)
        .order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
    ).all()
    assert len(rows) == 6
    seed(rows[0].id, status='accepted', accepted_at=NOW)
    db.session.commit()
    expected = [r.id for r in rows[1:]]
    with statements_executed() as baseline:
        assert (
            repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
            == expected
        )
    db.session.rollback()
    add_terminal_rows(setup, 130)
    with statements_executed() as grown:
        assert (
            repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
            == expected
        )
    db.session.rollback()
    assert len(grown) == len(baseline)
    assert len(tournament_reads(grown)) <= 1


def test_selection_does_not_starve_eligible_rows_behind_held_ones(setup):
    assert len(add_matches(setup, [(0, 1), (2, 3), (4, 5)])) == 6
    rows = db.session.scalars(
        select(DbMatchInvitation)
        .where(DbMatchInvitation.tournament_id == setup.tournament_id)
        .order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
    ).all()
    held_match = db.session.get(DbTournamentMatch, rows[0].match_id)
    held_match.invitation_hold_a = True
    db.session.flush()
    expected = [r.id for r in rows if r.match_id != held_match.id]
    assert len(expected) == 4
    assert (
        repo.select_invitation_retry_ids_flush(
            setup.tournament_id, now=NOW, limit=2
        )
        == expected[:2]
    )
    assert (
        repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
        == expected
    )
    db.session.rollback()


def test_selection_of_paused_tournament_returns_nothing(setup):
    intents(setup)
    db.session.get(
        DbTournament, setup.tournament_id
    ).tournament_status = 'PAUSED'
    db.session.flush()
    assert (
        repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
        == []
    )
    db.session.rollback()


def grid_rows(setup):
    recipient = setup.user_ids[5]
    for status in (
        'pending',
        'failed',
        'suppressed',
        'accepted',
        'delivery_unknown',
        'dispatching',
        'queued',
        'sending',
    ):
        for attempts in (0, MAX - 1, MAX):
            for next_attempt_at in (
                None,
                NOW - timedelta(minutes=1),
                NOW,
                NOW + timedelta(minutes=1),
            ):
                for last_error in (
                    None,
                    'x',
                    'pairing_retired',
                    'tournament_terminal',
                    'readiness_hold',
                    'pre_send_lease_expired',
                ):
                    fields = {}
                    if status in {'dispatching', 'queued', 'sending'}:
                        fields = dict(
                            dispatch_token=uuid7(),
                            lease_until=NOW + timedelta(minutes=2),
                        )
                    if status == 'accepted':
                        fields = dict(accepted_at=NOW)
                    yield DbMatchInvitation(
                        uuid7(),
                        uuid7(),
                        setup.tournament_id,
                        0,
                        recipient,
                        status,
                        0,
                        attempts=attempts,
                        next_attempt_at=next_attempt_at,
                        last_error=last_error,
                        **fields,
                    )


def test_sql_predicates_match_the_python_predicates(setup):
    rows = list(grid_rows(setup))
    db.session.add_all(rows)
    db.session.flush()
    by_retryable = set(
        db.session.scalars(
            select(DbMatchInvitation.id).where(
                DbMatchInvitation.tournament_id == setup.tournament_id,
                repo._invitation_retryable_clause(NOW),
            )
        )
    )
    by_spent = set(
        db.session.scalars(
            select(DbMatchInvitation.id).where(
                DbMatchInvitation.tournament_id == setup.tournament_id,
                repo._invitation_spent_clause(),
            )
        )
    )
    assert by_retryable == {
        r.id for r in rows if repo._invitation_retryable(r, NOW)
    }
    assert by_spent == {r.id for r in rows if repo._invitation_spent(r)}
    assert by_retryable and by_spent and len(by_retryable) < len(rows)
    assert not by_retryable & by_spent
    db.session.rollback()


def claimed_rows(setup):
    ids = intents(setup) + add_matches(setup, [(2, 3), (4, 5)])
    for invitation_id in ids:
        reserve(invitation_id)
    db.session.commit()
    return ids


def test_recovery_reads_the_tournament_once_per_call(setup):
    ids = claimed_rows(setup)
    with statements_executed() as statements:
        recovered = repo.recover_expired_invitations_flush(
            setup.tournament_id, now=LATE
        )
    assert sorted(recovered, key=str) == sorted(ids, key=str)
    assert len(tournament_reads(statements)) <= 1
    db.session.rollback()


def test_reconcile_reads_the_tournament_once_per_call(setup):
    intents(setup)
    with statements_executed() as statements:
        repo.ensure_invitation_intents_flush(
            setup.match_id, setup.user_ids[:2], occurred_at=NOW
        )
    assert len(tournament_reads(statements)) <= 1
    db.session.rollback()


def test_selection_reads_the_tournament_once_per_call(setup):
    intents(setup)
    add_matches(setup, [(2, 3), (4, 5)])
    with statements_executed() as statements:
        assert (
            len(
                repo.select_invitation_retry_ids_flush(
                    setup.tournament_id, now=NOW
                )
            )
            == 6
        )
    assert len(tournament_reads(statements)) <= 1
    db.session.rollback()
