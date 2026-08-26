"""Actual PostgreSQL proof of engine pairing adapters and retained cleanup."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_match_service as engine
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import DbTournamentLogEntry
from byceps.services.lan_tournament.models.tournament_match import MatchSide, TournamentMatch
from byceps.services.lan_tournament.models.tournament_match_to_contestant import TournamentMatchToContestant
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('issue9-pairing'), 'Issue 9 pairing')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Issue9Pairing{i}') for i in range(4)]


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(
        uuid7(), party.id, f'Pairing {uuid7()}', NOW,
        game_format='ONE_V_ONE', tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, NOW)
        for user in users
    ]
    db.session.add_all(participants)
    match_id = uuid7()
    repo.create_match(TournamentMatch(
        id=match_id, tournament_id=tournament.id, created_at=NOW,
        group_order=None, match_order=0, round=0, next_match_id=None,
        confirmed_by=None,
    ))
    for participant in participants[:2]:
        _insert(match_id, participant.id)
    db.session.commit()
    # Keep immutable IDs: assertion queries do not depend on expired fixtures.
    yield SimpleNamespace(
        tournament_id=tournament.id, match_id=match_id,
        participant_ids=[p.id for p in participants],
        user_ids=[u.id for u in users],
    )
    db.session.rollback()


def _insert(match_id, participant_id):
    engine._create_match_contestant_flush(TournamentMatchToContestant(
        id=uuid7(), tournament_match_id=match_id, created_at=NOW,
        team_id=None, participant_id=participant_id, score=None,
    ))


def _lock(setup):
    repo.get_tournament_for_update(setup.tournament_id)
    return repo.get_match_for_update(setup.match_id)


def _claims(setup):
    _lock(setup)
    for side, user_id in zip(MatchSide, setup.user_ids[:2], strict=True):
        repo.set_side_ready_flush(setup.match_id, side, NOW, user_id)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    db.session.commit()


def _invitation(setup, status='pending'):
    match = repo.get_match(setup.match_id)
    # Engine assignment already owns the unique generation/recipient intent.
    row = db.session.scalars(select(DbMatchInvitation).where(
        DbMatchInvitation.match_id == setup.match_id,
        DbMatchInvitation.pairing_generation == match.pairing_generation,
        DbMatchInvitation.recipient_id == setup.user_ids[0],
    )).one()
    assert row.tournament_id == setup.tournament_id
    row.status = status
    row.expected_readiness_revision = match.readiness_revision
    row.accepted_at = NOW if status == 'accepted' else None
    reserved = status in {'dispatching', 'queued', 'sending'}
    attempted = reserved or status in {'accepted', 'delivery_unknown'}
    row.dispatch_token = uuid7() if attempted else None
    row.lease_until = NOW + timedelta(minutes=2) if reserved else None
    row.attempts = 1 if attempted else 0
    row.next_attempt_at = row.last_error = None
    db.session.commit()
    return row.id


def _replace(setup):
    _lock(setup)
    engine._delete_contestant_from_match_flush(
        setup.match_id, participant_id=setup.participant_ids[1],
    )
    _insert(setup.match_id, setup.participant_ids[2])
    db.session.commit()
    return repo.find_match_fresh(setup.match_id)


def test_replacement_invalidates_both_claims(setup):
    _claims(setup)
    invitation_id = _invitation(setup, 'queued')
    before = repo.get_match(setup.match_id)
    old_pair = repo.get_match_pairing(setup.match_id)
    after = _replace(setup)
    assert after.ready_at_a is after.ready_at_b is None
    assert after.ready_by_a is after.ready_by_b is None
    assert not after.invitation_hold_a and not after.invitation_hold_b
    assert after.pairing_generation > before.pairing_generation
    assert after.readiness_revision > before.readiness_revision
    pair = repo.get_match_pairing(setup.match_id)
    assert pair.id != old_pair.id
    assert {pair.side_a.id, pair.side_b.id} == {setup.participant_ids[0], setup.participant_ids[2]}
    with Session(db.engine) as independent:
        invitation = independent.get(DbMatchInvitation, invitation_id)
        assert invitation.status == 'suppressed'
        assert invitation.dispatch_token is invitation.lease_until is None
        live = independent.get(DbTournamentMatch, setup.match_id)
        assert live.ready_at_a is live.ready_at_b is None


def test_original_occupancy_survives_replacement(setup):
    before = repo.get_match(setup.match_id)
    first = repo.get_match_pairing(setup.match_id)
    after = _replace(setup)
    history = repo.get_match_pairing_history(setup.match_id)
    assert after.occupied_since == before.occupied_since
    assert len(history) == 2
    assert history[0].id == first.id
    assert history[0].side_a == first.side_a and history[0].side_b == first.side_b
    assert history[0].ended_at is not None
    assert history[1].started_at >= history[0].ended_at


def test_same_pair_reset_preserves_accepted_invitation(setup):
    _claims(setup)
    invitation_id = _invitation(setup, 'accepted')
    before = _lock(setup)
    pairing = repo.get_match_pairing(setup.match_id)
    repo.confirm_match(setup.match_id, setup.user_ids[0])
    engine._reset_match_readiness_flush(setup.match_id)
    db.session.commit()
    after = repo.find_match_fresh(setup.match_id)
    assert after.confirmed_by is None
    assert after.readiness_revision == before.readiness_revision + 1
    assert after.ready_at_a is after.ready_at_b is after.ready_by_a is after.ready_by_b is None
    assert after.occupied_since == before.occupied_since
    assert after.pairing_generation == before.pairing_generation
    assert repo.get_match_pairing(setup.match_id) == pairing
    assert len(repo.get_match_pairing_history(setup.match_id)) == 1
    with Session(db.engine) as independent:
        invitations = independent.scalars(select(DbMatchInvitation).where(
            DbMatchInvitation.match_id == setup.match_id,
            DbMatchInvitation.recipient_id == setup.user_ids[0],
            DbMatchInvitation.pairing_generation == before.pairing_generation,
        )).all()
        assert len(invitations) == 1
        assert invitations[0].id == invitation_id
        assert invitations[0].status == 'accepted' and invitations[0].accepted_at == NOW
        all_work = independent.scalars(select(DbMatchInvitation).where(
            DbMatchInvitation.match_id == setup.match_id,
            DbMatchInvitation.pairing_generation == before.pairing_generation,
        )).all()
        assert len(all_work) == 2
        assert {work.recipient_id for work in all_work} == set(setup.user_ids[:2])


def test_reset_without_claims_still_advances_revision(setup):
    before = _lock(setup)
    repo.confirm_match(setup.match_id, setup.user_ids[0])
    engine._reset_match_readiness_flush(setup.match_id)
    db.session.commit()
    assert repo.find_match_fresh(setup.match_id).readiness_revision == before.readiness_revision + 1


def test_same_pair_reset_retires_queued_token(setup, record_property):
    invitation_id = _invitation(setup, 'queued')
    old_token = repo.get_match_invitation(invitation_id).dispatch_token
    before = _lock(setup)
    pairing = repo.get_match_pairing(setup.match_id)
    repo.confirm_match(setup.match_id, setup.user_ids[0])
    engine._reset_match_readiness_flush(setup.match_id)
    db.session.commit()
    after = repo.find_match_fresh(setup.match_id)
    assert after.confirmed_by is None
    assert after.pairing_id == before.pairing_id
    assert after.pairing_generation == before.pairing_generation
    assert after.readiness_revision == before.readiness_revision + 1
    assert after.occupied_since == before.occupied_since
    assert repo.get_match_pairing(setup.match_id) == pairing
    assert len(repo.get_match_pairing_history(setup.match_id)) == 1
    invitation = db.session.get(DbMatchInvitation, invitation_id)
    # Approved option 1: cancel the old attempt, allow eligible unsent mail anew.
    # Pre-decision suppressed/readiness_reset failures remain in task evidence.
    record_property('reset_status', invitation.status)
    record_property('reset_last_error', str(invitation.last_error))
    record_property('reset_dispatch_token', str(invitation.dispatch_token))
    record_property('reset_lease_until', str(invitation.lease_until))
    assert invitation.dispatch_token is invitation.lease_until is None
    assert repo.claim_invitation_dispatch_flush(
        invitation_id, expected_token=old_token, now=NOW,
    ).unwrap_err() == 'invitation_conflict'
    assert invitation.status == 'pending'
    assert invitation.dispatch_token is invitation.lease_until is None
    assert invitation.last_error is None
    assert invitation.expected_readiness_revision == after.readiness_revision
    assert invitation.pairing_generation == after.pairing_generation
    assert invitation.accepted_at is None and invitation.next_attempt_at is None
    fresh = repo.claim_invitation_dispatch_flush(
        invitation_id, expected_token=None, now=NOW,
    ).unwrap()
    assert fresh.dispatch_token is not None and fresh.dispatch_token != old_token
    assert fresh.lease_until is not None
    assert fresh.expected_readiness_revision == after.readiness_revision
    repo.rollback_session()


@pytest.mark.parametrize('status', ['accepted', 'sending', 'delivery_unknown'])
def test_retirement_preserves_nonretractable_outcomes(setup, status):
    invitation_id = _invitation(setup, status)
    _replace(setup)
    assert db.session.get(DbMatchInvitation, invitation_id).status == status


@pytest.mark.parametrize('writer', ['match', 'tournament', 'contestant', 'specific'])
def test_bulk_delete_has_flush_cleanup(setup, writer):
    _claims(setup)
    invitation_id = _invitation(setup)
    match = _lock(setup)
    with patch.object(repo, 'commit_session', side_effect=AssertionError('early commit')):
        if writer == 'match':
            repo.delete_contestants_for_match_flush(setup.match_id)
        elif writer == 'tournament':
            repo.delete_contestants_for_tournament_flush(setup.tournament_id)
        elif writer == 'contestant':
            contestant = repo.get_contestants_for_match(setup.match_id)[0]
            repo.delete_match_contestant_flush(contestant.id)
        else:
            repo.delete_contestant_from_match(setup.match_id, participant_id=setup.participant_ids[0])
    after = repo.find_match_fresh(setup.match_id)
    assert after.pairing_id is None
    assert after.ready_at_a is after.ready_at_b is None
    assert after.occupied_since == match.occupied_since
    # A separate transaction still sees the old facts: backstops did not commit.
    with Session(db.engine) as independent:
        assert independent.get(DbTournamentMatch, setup.match_id).pairing_id == match.pairing_id
        assert independent.get(DbMatchInvitation, invitation_id).status == 'pending'
    db.session.commit()
    assert db.session.get(DbMatchInvitation, invitation_id).status == 'suppressed'


def test_audit_failure_rolls_back_pairing_and_contestant_delete(setup):
    _claims(setup)
    before = _lock(setup)
    contestants = repo.get_contestants_for_match(setup.match_id)
    with patch.object(engine, 'create_log_entry', side_effect=RuntimeError('audit failed')):
        with pytest.raises(RuntimeError, match='audit failed'):
            engine._delete_contestant_from_match_flush(
                setup.match_id, participant_id=setup.participant_ids[0],
            )
    assert repo.find_match_fresh(setup.match_id) == before
    assert repo.get_contestants_for_match(setup.match_id) == contestants


def test_insert_audit_failure_rolls_back_replacement(setup):
    from byceps.services.lan_tournament import tournament_readiness_service

    before = _lock(setup)
    contestants = repo.get_contestants_for_match(setup.match_id)
    engine._delete_contestant_from_match_flush(
        setup.match_id, participant_id=setup.participant_ids[1],
    )
    with patch.object(tournament_readiness_service, 'create_log_entry', side_effect=RuntimeError('audit failed')):
        with pytest.raises(ValueError, match='readiness_audit_failed'):
            _insert(setup.match_id, setup.participant_ids[2])
    assert repo.find_match_fresh(setup.match_id) == before
    assert repo.get_contestants_for_match(setup.match_id) == contestants


def test_bulk_match_row_delete_retains_history_and_work_without_commit(setup):
    invitation_id = _invitation(setup)
    before = _lock(setup)
    repo.delete_contestants_for_tournament_flush(setup.tournament_id)
    repo.delete_matches_for_tournament(setup.tournament_id, commit=False)
    with Session(db.engine) as independent:
        assert independent.get(DbTournamentMatch, setup.match_id).pairing_id == before.pairing_id
    db.session.commit()
    assert repo.find_match(setup.match_id) is None
    assert repo.get_match_pairing_history(setup.match_id)[0].ended_at is not None
    assert db.session.get(DbMatchInvitation, invitation_id).status == 'suppressed'


def test_engine_clear_bracket_retains_history_and_suppresses_work(setup):
    invitation_id = _invitation(setup)
    before = _lock(setup)
    events = engine.clear_bracket(setup.tournament_id)
    assert [event.match_id for event in events] == [setup.match_id]
    db.session.commit()
    assert repo.find_match(setup.match_id) is None
    history = repo.get_match_pairing_history(setup.match_id)
    assert len(history) == 1 and history[0].id == before.pairing_id
    assert history[0].ended_at is not None
    assert db.session.get(DbMatchInvitation, invitation_id).status == 'suppressed'
    logs = db.session.scalars(select(DbTournamentLogEntry).where(
        DbTournamentLogEntry.tournament_id == setup.tournament_id,
        DbTournamentLogEntry.event_type == 'match-pairing-invalidated',
    )).all()
    assert len(logs) == 1


# fmt: off
@pytest.mark.parametrize('mode,generator', [
    ('SINGLE_ELIMINATION', engine._generate_single_elimination_impl),
    ('DOUBLE_ELIMINATION', engine._generate_double_elimination_impl),
    ('ROUND_ROBIN', engine._generate_round_robin_impl),
])
# fmt: on
def test_actual_generators_initialize_pairings_without_ready(setup, mode, generator):
    _lock(setup)
    engine.clear_bracket(setup.tournament_id)
    tournament = db.session.get(DbTournament, setup.tournament_id)
    tournament.elimination_mode = mode
    db.session.flush()
    result = generator(setup.tournament_id)
    assert result.is_ok(), result.unwrap_err()
    outcome = result.unwrap()
    db.session.commit()
    assert len(repo.get_matches_for_tournament(setup.tournament_id)) == outcome.count
    assigned = 0
    for match in repo.get_matches_for_tournament(setup.tournament_id):
        assert match.ready_at_a is match.ready_at_b is None
        contestants = repo.get_contestants_for_match(match.id)
        pair = repo.get_match_pairing(match.id)
        assert (pair is not None) == (len(contestants) == 2)
        assigned += pair is not None
    assert assigned > 0


def test_actual_ffa_generation_has_no_two_side_pairing(setup):
    _lock(setup)
    engine.clear_bracket(setup.tournament_id)
    tournament = db.session.get(DbTournament, setup.tournament_id)
    tournament.game_format = 'FREE_FOR_ALL'
    tournament.group_size_min = 2
    tournament.group_size_max = 4
    db.session.flush()
    result = engine._generate_ffa_round_impl(setup.tournament_id, 0)
    assert result.is_ok(), result.unwrap_err()
    db.session.commit()
    matches = repo.get_matches_for_tournament(setup.tournament_id)
    assert len(matches) == result.unwrap()
    assert sum(len(repo.get_contestants_for_match(match.id)) for match in matches) == 4
    assert all(repo.get_match_pairing(match.id) is None for match in matches)
    assert all(match.ready_at_a is match.ready_at_b is None for match in matches)
