"""Actual PostgreSQL recipient ledger, CAS, recovery and retained-history proof."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import DbTournamentMatchToContestant
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation, DbMatchPairing
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.match_readiness import InvitationStatus
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('issue13-invitations'), 'Issue 13 invitations')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Issue13Recipient{i}') for i in range(6)]


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(
        uuid7(), party.id, f'Invitations {uuid7()}', NOW,
        game_format='ONE_V_ONE', tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [DbTournamentParticipant(uuid7(), u.id, tournament.id, NOW) for u in users]
    db.session.add_all(participants)
    match = DbTournamentMatch(uuid7(), tournament.id, NOW, match_order=0, round=0)
    db.session.add(match)
    db.session.flush()
    for p in participants[:2]:
        db.session.add(DbTournamentMatchToContestant(
            uuid7(), match.id, NOW, participant_id=p.id,
        ))
    db.session.flush()
    repo.get_tournament_for_update(tournament.id)
    repo.get_match_for_update(match.id)
    assert repo.refresh_match_pairing_flush(match.id, occurred_at=NOW).is_ok()
    db.session.commit()
    yield SimpleNamespace(
        tournament_id=tournament.id, match_id=match.id,
        participant_ids=[p.id for p in participants], user_ids=[u.id for u in users],
    )
    db.session.rollback()


def intents(setup, **kwargs):
    return repo.ensure_invitation_intents_flush(
        setup.match_id, setup.user_ids[:2], occurred_at=NOW, **kwargs,
    )


def reserve(invitation_id, now=NOW):
    token = repo.get_match_invitation(invitation_id).dispatch_token
    result = repo.claim_invitation_dispatch_flush(invitation_id, expected_token=token, now=now)
    assert result.is_ok(), result
    return result.unwrap().dispatch_token


def outcome(invitation_id, token, status, now=NOW, **kwargs):
    result = repo.record_invitation_outcome_flush(
        invitation_id, token, status=status, now=now, **kwargs,
    )
    assert result.is_ok(), result


def test_recipient_uniqueness_and_token_cas(setup):
    ids = intents(setup)
    assert len(ids) == 2 and intents(setup) == ids
    first = reserve(ids[0])
    outcome(ids[0], first, InvitationStatus.QUEUED)
    later = NOW + timedelta(seconds=121)
    assert repo.recover_expired_invitations_flush(setup.tournament_id, now=later) == [ids[0]]
    second = reserve(ids[0], later)
    assert second != first
    assert repo.record_invitation_outcome_flush(
        ids[0], first, status=InvitationStatus.SENDING, now=later,
    ).unwrap_err() == 'invitation_conflict'
    outcome(ids[0], second, InvitationStatus.SENDING, later)
    outcome(ids[0], second, InvitationStatus.ACCEPTED, later)
    assert repo.record_invitation_outcome_flush(
        ids[0], second, status=InvitationStatus.QUEUED, now=later,
    ).unwrap_err() == 'invitation_conflict'
    assert repo.get_match_invitation(ids[0]).status == InvitationStatus.ACCEPTED
    assert db.session.scalar(select(repo.func.count()).select_from(DbMatchInvitation).where(
        DbMatchInvitation.match_id == setup.match_id,
    )) == 2


def test_partial_failure_keeps_other_recipient_accepted(setup):
    a, b = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.SENDING)
    outcome(a, token, InvitationStatus.ACCEPTED)
    token = reserve(b)
    outcome(b, token, InvitationStatus.FAILED, retryable=True, error='enqueue_failed')
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW) == []
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW + timedelta(seconds=30)) == [b]
    assert repo.get_match_invitation(a).accepted_at == NOW


@pytest.mark.parametrize('stage', [InvitationStatus.DISPATCHING, InvitationStatus.QUEUED, InvitationStatus.SENDING])
def test_definite_and_ambiguous_lease_recovery(setup, stage):
    a, _ = intents(setup)
    token = reserve(a)
    if stage != InvitationStatus.DISPATCHING:
        outcome(a, token, stage)
    recovered = repo.recover_expired_invitations_flush(setup.tournament_id, now=NOW + timedelta(seconds=120))
    fact = repo.get_match_invitation(a)
    if stage == InvitationStatus.SENDING:
        assert a not in recovered and fact.status == InvitationStatus.DELIVERY_UNKNOWN
        assert a not in repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
    else:
        assert recovered == [a] and fact.status == InvitationStatus.PENDING
        assert fact.dispatch_token is None
    assert fact.lease_until is None


def test_revocation_hold_revision_and_fresh_claim_reconciliation(setup):
    a, b = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.QUEUED)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    repo.set_readiness_revision_flush(setup.match_id, 2)
    assert intents(setup) == []
    assert repo.get_match_invitation(a).dispatch_token is None
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW) == []
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, False)
    repo.set_readiness_revision_flush(setup.match_id, 3)
    assert intents(setup) == [a, b]
    assert repo.get_match_invitation(a).expected_readiness_revision == 3
    assert repo.record_invitation_outcome_flush(a, token, status=InvitationStatus.SENDING, now=NOW).is_err()


def test_revision_retires_queued_token_without_hold(setup):
    a, _ = intents(setup)
    old = reserve(a)
    outcome(a, old, InvitationStatus.QUEUED)
    repo.set_readiness_revision_flush(setup.match_id, 2)
    assert a in intents(setup)
    assert repo.get_match_invitation(a).dispatch_token is None
    assert reserve(a) != old


def test_historical_unknown_and_sending_preserved_by_reconciliation(setup):
    assert intents(setup, historical_unknown=True) == []
    rows = db.session.scalars(select(DbMatchInvitation).where(DbMatchInvitation.match_id == setup.match_id)).all()
    a, b = rows
    a.status = 'pending'
    db.session.flush()
    token = reserve(a.id)
    outcome(a.id, token, InvitationStatus.SENDING)
    repo.set_readiness_revision_flush(setup.match_id, 2)
    assert intents(setup) == []
    assert repo.get_match_invitation(a.id).expected_readiness_revision == 1
    assert repo.get_match_invitation(b.id).status == InvitationStatus.DELIVERY_UNKNOWN
    outcome(a.id, token, InvitationStatus.ACCEPTED)
    assert repo.get_match_invitation(a.id).accepted_at == NOW


def test_generation_and_active_deletion_preserve_inflight_history(setup):
    a, b = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.SENDING)
    old_generation = repo.get_match_invitation(a).pairing_generation
    repo.delete_contestants_for_match_flush(setup.match_id)
    assert repo.get_match_invitation(b).status == InvitationStatus.SUPPRESSED
    assert repo.claim_invitation_dispatch_flush(b, expected_token=None, now=NOW).is_err()
    assert repo.get_match(setup.match_id).pairing_generation > old_generation
    outcome(a, token, InvitationStatus.ACCEPTED)
    assert repo.get_match_invitation(a).pairing_generation == old_generation


def test_flush_only_visibility_and_rollback(setup):
    with patch.object(db.session, 'commit', side_effect=AssertionError('internal commit')):
        ids = intents(setup)
        token = reserve(ids[0])
        outcome(ids[0], token, InvitationStatus.FAILED, retryable=True)
        repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)
        repo.recover_expired_invitations_flush(setup.tournament_id, now=NOW)
    with Session(db.engine) as independent:
        assert independent.get(DbMatchInvitation, ids[0]) is None
    db.session.rollback()
    assert repo.get_match_invitation(ids[0]) is None


def test_attempt_bound_and_permanent_failure_stay_failed(setup):
    a, b = intents(setup)
    now = NOW
    for attempt in range(1, 4):
        token = reserve(a, now)
        outcome(a, token, InvitationStatus.FAILED, now, retryable=True)
        fact = repo.get_match_invitation(a)
        assert fact.attempts == attempt
        now += timedelta(seconds=30 if attempt == 1 else 120)
    assert a not in repo.select_invitation_retry_ids_flush(setup.tournament_id, now=now)
    assert repo.claim_invitation_dispatch_flush(a, expected_token=token, now=now).is_err()
    assert repo.get_match_invitation(a).status == InvitationStatus.FAILED
    token = reserve(b, now)
    outcome(b, token, InvitationStatus.FAILED, now, error='email_config_missing')
    assert b not in repo.select_invitation_retry_ids_flush(setup.tournament_id, now=now + timedelta(days=1))


# fmt: off
@pytest.mark.parametrize(('changes', 'constraint'), [
    ({'status': 'PENDING'}, 'status'),
    ({'status': 'sending'}, 'dispatch_stage'),
    ({'status': 'accepted'}, 'acceptance_stage'),
    ({'lease_until': NOW}, 'dispatch_stage'),
    ({'attempts': -1}, 'attempts'),
    ({'pairing_generation': -1}, 'pairing_generation'),
    ({'expected_readiness_revision': -1}, 'readiness_revision'),
])
# fmt: on
def test_persistence_checks(setup, changes, constraint):
    a, _ = intents(setup)
    with pytest.raises(IntegrityError) as error:
        with db.session.begin_nested():
            row = db.session.get(DbMatchInvitation, a)
            for field, value in changes.items():
                setattr(row, field, value)
            db.session.flush()
    assert f'ck_lan_tournament_match_invitations_{constraint}' in str(error.value)


def test_bigint_revisions_and_database_uniqueness(setup):
    repo.set_readiness_revision_flush(setup.match_id, 2**40)
    a, _ = intents(setup)
    fact = repo.get_match_invitation(a)
    assert fact.expected_readiness_revision == 2**40
    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(DbMatchInvitation(
                uuid7(), fact.match_id, fact.tournament_id, fact.pairing_generation,
                fact.recipient_id, 'pending', fact.expected_readiness_revision,
            ))
            db.session.flush()


@pytest.mark.parametrize('state', ['PAUSED', 'COMPLETED', 'CANCELLED'])
def test_lifecycle_ineligible(setup, state):
    a, _ = intents(setup)
    db.session.get(DbTournament, setup.tournament_id).tournament_status = state
    db.session.flush()
    assert repo.claim_invitation_dispatch_flush(a, expected_token=None, now=NOW).is_err()
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW) == []


def test_team_current_membership_scoped_and_reconciled(setup):
    # Use a separate new current match after the accepted deletion backstop.
    repo.delete_contestants_for_match_flush(setup.match_id)
    teams = [DbTournamentTeam(uuid7(), setup.tournament_id, f'Team {uuid7()}', setup.user_ids[i], NOW) for i in range(2)]
    db.session.add_all(teams)
    db.session.flush()
    for index, participant_id in enumerate(setup.participant_ids[:4]):
        db.session.get(DbTournamentParticipant, participant_id).team_id = teams[index % 2].id
    for team in teams:
        db.session.add(DbTournamentMatchToContestant(uuid7(), setup.match_id, NOW, team_id=team.id))
    db.session.flush()
    assert repo.refresh_match_pairing_flush(setup.match_id, occurred_at=NOW).is_ok()
    ids = repo.ensure_invitation_intents_flush(setup.match_id, setup.user_ids, occurred_at=NOW)
    assert {repo.get_match_invitation(i).recipient_id for i in ids} == set(setup.user_ids[:4])
    departed = db.session.get(DbTournamentParticipant, setup.participant_ids[2])
    departed.removed_at = NOW
    db.session.flush()
    ids = repo.ensure_invitation_intents_flush(setup.match_id, setup.user_ids, occurred_at=NOW)
    assert {repo.get_match_invitation(i).recipient_id for i in ids} == set(setup.user_ids[:2] + setup.user_ids[3:4])
    departed.team_id = teams[1].id
    departed.removed_at = None
    db.session.flush()
    assert len(repo.ensure_invitation_intents_flush(setup.match_id, setup.user_ids, occurred_at=NOW)) == 4


def test_permanent_failure_survives_hold_and_resume(setup):
    a, _ = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.FAILED, error='email_address_missing')
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.B, True)
    intents(setup)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.B, False)
    assert a not in intents(setup)
    assert repo.get_match_invitation(a).last_error == 'email_address_missing'
    assert a not in repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW)


def test_pause_resume_preserves_accepted_and_retires_queued_token(setup):
    a, b = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.SENDING)
    outcome(a, token, InvitationStatus.ACCEPTED)
    old = reserve(b)
    outcome(b, old, InvitationStatus.QUEUED)
    tournament = db.session.get(DbTournament, setup.tournament_id)
    tournament.tournament_status = 'PAUSED'
    db.session.flush()
    assert intents(setup) == []
    assert repo.get_match_invitation(b).last_error == 'tournament_paused'
    tournament.tournament_status = 'ONGOING'
    db.session.flush()
    assert intents(setup) == [b]
    assert reserve(b) != old
    assert repo.get_match_invitation(a).accepted_at == NOW


def test_fresh_roster_after_independent_membership_change(setup):
    a, _ = intents(setup)
    recipient = repo.get_match_invitation(a).recipient_id
    participant_id = setup.participant_ids[setup.user_ids.index(recipient)]
    cached = db.session.get(DbTournamentParticipant, participant_id)
    db.session.commit()
    # Deliberately repopulate a stale identity-map object before external change.
    assert cached.removed_at is None
    with Session(db.engine) as independent:
        independent.get(DbTournamentParticipant, participant_id).removed_at = NOW
        independent.commit()
    assert repo.claim_invitation_dispatch_flush(a, expected_token=None, now=NOW).unwrap_err() == 'recipient_not_current'
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW) == []


def test_old_sending_outcome_never_satisfies_new_pairing(setup):
    a, _ = intents(setup)
    old = repo.get_match_invitation(a)
    token = reserve(a)
    outcome(a, token, InvitationStatus.SENDING)
    repo.delete_contestants_for_match_flush(setup.match_id)
    for participant_id in setup.participant_ids[1:3]:
        db.session.add(DbTournamentMatchToContestant(uuid7(), setup.match_id, NOW, participant_id=participant_id))
    db.session.flush()
    assert repo.refresh_match_pairing_flush(setup.match_id, occurred_at=NOW).is_ok()
    new_ids = repo.ensure_invitation_intents_flush(setup.match_id, setup.user_ids, occurred_at=NOW)
    assert len(new_ids) == 2
    new_token = reserve(new_ids[0])
    outcome(a, token, InvitationStatus.ACCEPTED)
    assert repo.get_match_invitation(a).pairing_generation == old.pairing_generation
    assert repo.get_match_invitation(new_ids[0]).status == InvitationStatus.DISPATCHING
    assert repo.get_match_invitation(new_ids[0]).dispatch_token == new_token


def test_recovery_and_selection_are_deterministic_and_bounded_100(setup):
    ids = []
    # 102 eligible records across real current pairs, not fabricated foreign work.
    for _ in range(51):
        match = DbTournamentMatch(uuid7(), setup.tournament_id, NOW, pairing_generation=1, readiness_revision=1)
        db.session.add(match)
        db.session.flush()
        pair = DbMatchPairing(uuid7(), match.id, setup.tournament_id, 1,
                              'participant', setup.participant_ids[0],
                              'participant', setup.participant_ids[1], started_at=NOW)
        db.session.add(pair)
        db.session.flush()
        match.pairing_id = pair.id
        for participant_id, user_id in zip(setup.participant_ids[:2], setup.user_ids[:2], strict=True):
            db.session.add(DbTournamentMatchToContestant(uuid7(), match.id, NOW, participant_id=participant_id))
            row = DbMatchInvitation(uuid7(), match.id, setup.tournament_id, 1, user_id,
                                    'queued', 1, attempts=1, dispatch_token=uuid7(), lease_until=NOW)
            db.session.add(row)
            ids.append((match.id, row.id))
    db.session.flush()
    expected = [invitation_id for _, invitation_id in sorted(ids, key=lambda item: (str(item[0]), str(item[1])))]
    recovered = repo.recover_expired_invitations_flush(setup.tournament_id, now=NOW, limit=999)
    assert recovered == expected[:100]
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW, limit=999) == expected[:100]
    assert repo.recover_expired_invitations_flush(setup.tournament_id, now=NOW) == expected[100:]
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW, limit=1) == expected[:1]


@pytest.mark.parametrize('status', list(InvitationStatus))
def test_every_enum_value_roundtrips_actual_postgresql(setup, status):
    a, _ = intents(setup)
    row = db.session.get(DbMatchInvitation, a)
    row.status = status.value
    row.dispatch_token = uuid7()
    row.lease_until = NOW + timedelta(seconds=120) if status.value in {'dispatching', 'queued', 'sending'} else None
    row.accepted_at = NOW if status == InvitationStatus.ACCEPTED else None
    db.session.flush()
    assert repo.get_match_invitation(a).status == status


@pytest.mark.parametrize('change', ['confirmed', 'format', 'foreign_tournament', 'stale_revision', 'foreign_generation'])
def test_current_eligibility_guards(setup, change):
    a, _ = intents(setup)
    row = db.session.get(DbMatchInvitation, a)
    match = db.session.get(DbTournamentMatch, setup.match_id)
    if change == 'confirmed':
        match.confirmed_by = setup.user_ids[0]
    elif change == 'format':
        db.session.get(DbTournament, setup.tournament_id).game_format = 'FREE_FOR_ALL'
    elif change == 'foreign_tournament':
        row.tournament_id = uuid7()
    elif change == 'stale_revision':
        match.readiness_revision += 1
    else:
        row.pairing_generation += 1
    db.session.flush()
    assert repo.claim_invitation_dispatch_flush(a, expected_token=None, now=NOW).is_err()
    assert repo.get_match_invitation(a).attempts == 0


def test_sending_outcome_survives_live_tournament_deletion(setup):
    a, b = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.SENDING)
    repo.delete_contestants_for_match_flush(setup.match_id)
    repo.delete_matches_for_tournament(setup.tournament_id, commit=False)
    repo.delete_participants_for_tournament_flush(setup.tournament_id)
    db.session.execute(delete(DbTournament).where(DbTournament.id == setup.tournament_id))
    db.session.flush()
    assert repo.get_match_invitation(b).status == InvitationStatus.SUPPRESSED
    outcome(a, token, InvitationStatus.ACCEPTED)
    assert repo.get_match_invitation(a).accepted_at == NOW
    assert repo.recover_expired_invitations_flush(setup.tournament_id, now=NOW + timedelta(days=1)) == []


def test_team_membership_rejects_cross_tournament_participant(setup, party):
    repo.delete_contestants_for_match_flush(setup.match_id)
    teams = [DbTournamentTeam(uuid7(), setup.tournament_id, f'Team {uuid7()}', setup.user_ids[i], NOW) for i in range(2)]
    db.session.add_all(teams)
    db.session.flush()
    for participant_id, team in zip(setup.participant_ids[:2], teams, strict=True):
        db.session.get(DbTournamentParticipant, participant_id).team_id = team.id
        db.session.add(DbTournamentMatchToContestant(uuid7(), setup.match_id, NOW, team_id=team.id))
    foreign = DbTournament(uuid7(), party.id, f'Foreign {uuid7()}', NOW, game_format='ONE_V_ONE', tournament_status='ONGOING')
    db.session.add(foreign)
    db.session.flush()
    db.session.add(DbTournamentParticipant(uuid7(), setup.user_ids[4], foreign.id, NOW, team_id=teams[0].id))
    db.session.flush()
    assert repo.refresh_match_pairing_flush(setup.match_id, occurred_at=NOW).is_ok()
    ids = repo.ensure_invitation_intents_flush(setup.match_id, setup.user_ids, occurred_at=NOW)
    assert {repo.get_match_invitation(i).recipient_id for i in ids} == set(setup.user_ids[:2])


@pytest.mark.parametrize('terminal', ['COMPLETED', 'CANCELLED'])
@pytest.mark.parametrize('temporary', ['hold', 'pause'])
def test_terminal_retirement_survives_reopen_hold_or_pause_across_storage_apis(setup, terminal, temporary):
    a, b = intents(setup)
    token = reserve(a)
    outcome(a, token, InvitationStatus.QUEUED)
    tournament = db.session.get(DbTournament, setup.tournament_id)
    tournament.tournament_status = terminal
    db.session.flush()
    assert intents(setup) == []
    before = [repo.get_match_invitation(i) for i in [a, b]]
    assert all(f.status == InvitationStatus.SUPPRESSED and f.last_error == 'tournament_terminal' for f in before)
    tournament.tournament_status = 'ONGOING'
    db.session.flush()
    if temporary == 'hold':
        repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    else:
        tournament.tournament_status = 'PAUSED'
        db.session.flush()
    assert intents(setup) == []
    if temporary == 'hold':
        repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, False)
    else:
        tournament.tournament_status = 'ONGOING'
        db.session.flush()
    assert intents(setup) == []
    # Exercise the bulk reset/backstop, not only the per-row reconcile helper.
    repo.unconfirm_match(setup.match_id)
    repo.suppress_match_invitations_flush(setup.match_id, reason='tournament_paused')
    assert intents(setup) == []
    later = NOW + timedelta(days=1)
    assert repo.recover_expired_invitations_flush(setup.tournament_id, now=later) == []
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=later) == []
    for invitation_id in [a, b]:
        assert repo.claim_invitation_dispatch_flush(invitation_id, expected_token=None, now=later).is_err()
    assert repo.record_invitation_outcome_flush(a, token, status=InvitationStatus.SENDING, now=later).is_err()
    assert [repo.get_match_invitation(i) for i in [a, b]] == before
    repo.delete_contestants_for_match_flush(setup.match_id)
    for participant_id in setup.participant_ids[1:3]:
        db.session.add(DbTournamentMatchToContestant(uuid7(), setup.match_id, NOW, participant_id=participant_id))
    db.session.flush()
    assert repo.refresh_match_pairing_flush(setup.match_id, occurred_at=later).is_ok()
    new_ids = repo.ensure_invitation_intents_flush(setup.match_id, setup.user_ids, occurred_at=later)
    assert len(new_ids) == 2 and set(new_ids).isdisjoint({a, b})
    assert reserve(new_ids[0], later) is not None
    assert [repo.get_match_invitation(i) for i in [a, b]] == before


def test_pairing_retirement_survives_temporary_suppression_and_allows_new_generation(setup):
    old_ids = intents(setup)
    repo.delete_contestants_for_match_flush(setup.match_id)
    before = [repo.get_match_invitation(i) for i in old_ids]
    assert all(f.last_error == 'pairing_retired' for f in before)
    for reason in ['readiness_reset', 'tournament_paused', 'readiness_hold']:
        repo.suppress_match_invitations_flush(setup.match_id, reason=reason)
        assert [repo.get_match_invitation(i) for i in old_ids] == before
    for participant_id in setup.participant_ids[:2]:
        db.session.add(DbTournamentMatchToContestant(uuid7(), setup.match_id, NOW, participant_id=participant_id))
    db.session.flush()
    assert repo.refresh_match_pairing_flush(setup.match_id, occurred_at=NOW).is_ok()
    new_ids = intents(setup)
    assert len(new_ids) == 2 and set(new_ids).isdisjoint(old_ids)
    assert [repo.get_match_invitation(i) for i in old_ids] == before
    assert repo.select_invitation_retry_ids_flush(setup.tournament_id, now=NOW) == new_ids
    assert reserve(new_ids[0]) is not None


def test_same_pair_correction_still_reconciles_nonterminal_unsent_work(setup):
    ids = intents(setup)
    match = db.session.get(DbTournamentMatch, setup.match_id)
    generation = match.pairing_generation
    match.confirmed_by = setup.user_ids[0]
    db.session.flush()
    assert intents(setup) == []
    assert all(repo.get_match_invitation(i).last_error == 'match_confirmed' for i in ids)
    repo.unconfirm_match(setup.match_id)
    assert intents(setup) == ids
    assert repo.get_match(setup.match_id).pairing_generation == generation
