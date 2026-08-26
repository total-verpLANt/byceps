"""Real PostgreSQL readiness boundaries, not sequential operations called races."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_match_service as facade
from byceps.services.lan_tournament import tournament_invitation_service as invitations
from byceps.services.lan_tournament import (
    tournament_readiness_service as service,
)
from byceps.services.lan_tournament import tournament_log_service as log_service
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
)
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7
from byceps.util.result import Ok

NOW = datetime(2026, 10, 5, 12)
READINESS_LOG_PREFIXES = ('match-ready-', 'match-readiness-', 'match-pairing-')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand, PartyID('issue8-readiness-flow'), 'Issue 8 readiness'
    )


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Issue8Ready{i}') for i in range(4)]


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Ready {uuid7()}',
        NOW,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, NOW)
        for user in users
    ]
    db.session.add_all(participants)
    match = DbTournamentMatch(uuid7(), tournament.id, NOW)
    db.session.add(match)
    db.session.flush()
    contestants = [
        DbTournamentMatchToContestant(
            uuid7(), match.id, NOW, participant_id=p.id
        )
        for p in participants[:2]
    ]
    db.session.add_all(contestants)
    db.session.commit()
    match_id, tournament_id = match.id, tournament.id
    # Engine writer integration is Issue 9. Establish pairing via owned operation.
    assert service.refresh_pairing_and_invitations_flush(
        match_id, occurred_at=NOW
    ).is_ok()
    db.session.commit()
    pairing = repo.get_match_pairing(match_id)
    actors = {
        side: next(p.user_id for p in participants if p.id == identity.id)
        for side, identity in (
            (MatchSide.A, pairing.side_a),
            (MatchSide.B, pairing.side_b),
        )
    }
    yield SimpleNamespace(
        match_id=match_id,
        tournament_id=tournament_id,
        participants=participants,
        contestants=contestants,
        users=users,
        actors=actors,
    )
    db.session.rollback()


def claim(
    setup, side=MatchSide.A, user=None, *, generation=None, revision=None
):
    match = repo.get_match(setup.match_id)
    return facade.claim_ready(
        match.id,
        side,
        user or setup.actors[side],
        expected_pairing_generation=match.pairing_generation
        if generation is None
        else generation,
        expected_readiness_revision=match.readiness_revision
        if revision is None
        else revision,
    )


def revoke(setup, *, revision=None):
    match = repo.get_match(setup.match_id)
    return facade.revoke_ready(
        match.id,
        MatchSide.A,
        setup.actors[MatchSide.A],
        expected_pairing_generation=match.pairing_generation,
        expected_readiness_revision=match.readiness_revision
        if revision is None
        else revision,
    )


def logs(setup):
    """Return the match's readiness audit entries, newest first."""
    entries = log_service.get_recent_entries_for_tournament(
        setup.tournament_id, READINESS_LOG_PREFIXES, limit=1000
    )
    return [e for e in entries if e.data.get('match_id') == str(setup.match_id)]


def test_claim_returns_fresh_frozen_readiness(setup):
    before = repo.get_match(setup.match_id)
    change = claim(setup).unwrap()
    assert before.ready_at_a is None and before.ready_at_b is None
    assert change.match is not before
    assert change.match == repo.get_match(setup.match_id)
    assert change.readiness == facade.get_match_readiness(
        change.match, repo.get_contestants_for_match(setup.match_id)
    )
    assert change.readiness.ready_sides == (MatchSide.A,)
    with pytest.raises(FrozenInstanceError):
        change.match.ready_by_a = None
    with pytest.raises(FrozenInstanceError):
        change.readiness.status = None
    assert change.match.occupied_since == NOW


def test_repeat_claim_preserves_actor_time_and_audit(setup):
    first = claim(setup).unwrap()
    db.session.commit()
    original, entries = repo.get_match(setup.match_id), logs(setup)
    result = claim(setup)
    assert result.unwrap_err() == 'readiness_conflict'
    assert repo.get_match(setup.match_id) == original == first.match
    assert logs(setup) == entries
    assert (
        len([e for e in entries if e.event_type == 'match-ready-claimed']) == 1
    )
    repo.rollback_session()


def test_generation_and_revision_aba_preserve_all_facts(setup):
    original_revision = claim(setup).unwrap().match.readiness_revision
    assert revoke(setup).is_ok()
    assert claim(setup).is_ok()
    db.session.commit()
    before, entries = repo.get_match(setup.match_id), logs(setup)
    assert (
        revoke(setup, revision=original_revision).unwrap_err()
        == 'readiness_conflict'
    )
    assert (
        claim(setup, generation=before.pairing_generation - 1).unwrap_err()
        == 'readiness_conflict'
    )
    assert repo.get_match(setup.match_id) == before and logs(setup) == entries


def test_pairing_replacement_and_return_generation_aba(setup):
    original = claim(setup).unwrap().match
    contestant = setup.contestants[0]
    original_participant_id = contestant.participant_id
    contestant.participant_id = setup.participants[2].id
    db.session.flush()
    assert service.refresh_pairing_and_invitations_flush(
        setup.match_id, occurred_at=NOW + timedelta(minutes=1)
    ).is_ok()
    contestant.participant_id = original_participant_id
    db.session.flush()
    restored = (
        service.refresh_pairing_and_invitations_flush(
            setup.match_id, occurred_at=NOW + timedelta(minutes=2)
        )
        .unwrap()
        .match
    )
    assert restored.pairing_generation == original.pairing_generation + 2
    assert restored.ready_at_a is None and restored.ready_by_a is None
    assert restored.occupied_since == original.occupied_since
    entries = logs(setup)
    result = claim(
        setup,
        generation=original.pairing_generation,
        revision=restored.readiness_revision,
    )
    assert result.unwrap_err() == 'readiness_conflict'
    assert repo.get_match(setup.match_id) == restored and logs(setup) == entries


def test_revoke_audit_and_holds_are_atomic(setup):
    assert claim(setup).is_ok() and claim(setup, MatchSide.B).is_ok()
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.B, True)
    change = revoke(setup).unwrap()
    assert change.match.invitation_hold_a and change.match.invitation_hold_b
    assert change.match.ready_at_a is None and change.match.ready_by_a is None
    assert change.match.ready_at_b is not None
    revoked = next(e for e in logs(setup) if e.event_type == 'match-ready-revoked')
    assert revoked.initiator_id == setup.actors[MatchSide.A]
    data = revoked.data
    assert 'reason' not in data and data['revoked_at']
    assert data['previous_display_status'] == 'both_ready'
    assert data['side'] == 'a' and data['actor_role'] == 'player'
    assert data['contestant_id'] == str(repo.get_match_pairing(setup.match_id).side_a.id)
    assert data['contestant_kind'] == 'participant'
    assert data['pairing_generation'] == change.match.pairing_generation
    assert data['readiness_revision'] == change.match.readiness_revision
    assert change.events[0].previous_display_status == data['previous_display_status']
    assert change.events[0].revoked_by == revoked.initiator_id
    assert claim(setup).is_ok()
    assert not repo.get_match(setup.match_id).invitation_hold_a
    assert repo.get_match(setup.match_id).invitation_hold_b


@pytest.mark.parametrize('operation', ['claim', 'revoke', 'reset'])
def test_audit_failure_caller_rollback_restores_database(setup, monkeypatch, operation):
    if operation != 'claim':
        assert claim(setup).is_ok()
    before, entries = repo.get_match(setup.match_id), logs(setup)
    db.session.commit()
    actual_audit = service.create_log_entry

    def failing_audit(*args, **kwargs):
        actual_audit(*args, **kwargs)
        # Rollback must remove a genuinely flushed audit row and state changes.
        raise RuntimeError('failure after audit flush')

    monkeypatch.setattr(service, 'create_log_entry', failing_audit)
    if operation == 'claim':
        result = claim(setup)
    elif operation == 'revoke':
        result = revoke(setup)
    else:
        result = service.reset_readiness_flush(setup.match_id, occurred_at=NOW)
    assert result.unwrap_err() == 'readiness_audit_failed'
    repo.rollback_session()
    assert repo.find_match_fresh(setup.match_id) == before
    assert logs(setup) == entries


def test_owning_one_commit_then_effects_and_no_rollback_emission(setup, monkeypatch):
    commits, emissions = [], []
    def dispatch(invitation_ids):
        assert commits == ['commit'] and len(emissions) == 1
        assert invitation_ids == change.pending_invitation_ids
        assert invitation_ids
        with Session(db.engine) as observer:
            for invitation_id in invitation_ids:
                work = observer.get(DbMatchInvitation, invitation_id)
                assert work.match_id == setup.match_id and work.status == 'pending'
        return Ok(None)
    # This test proves the owning boundary, not the worker's later stage commits.
    dispatcher = Mock(side_effect=dispatch)
    monkeypatch.setattr(service, 'dispatch_pending_invitations', dispatcher)
    def committed(session):
        commits.append('commit')
    def sent(sender, *, event):
        with Session(db.engine) as observer:
            row = observer.get(DbTournamentMatch, setup.match_id)
            assert row.ready_by_a == event.claimed_by
            assert row.readiness_revision == event.readiness_revision
        emissions.append(event)
        assert commits == ['commit']
    monkeypatch.setattr(service.match_ready_claimed, 'send', sent)
    session = db.session()
    event.listen(session, 'after_commit', committed)
    try:
        change = claim(setup).unwrap()
        assert commits == [] and emissions == []
        with Session(db.engine) as observer:
            assert observer.get(DbTournamentMatch, setup.match_id).ready_at_a is None
        repo.rollback_session()
        assert commits == [] and emissions == []
        dispatcher.assert_not_called()
        change = claim(setup).unwrap()
        repo.commit_session()
        assert service.dispatch_readiness_effects(change).is_ok()
        assert len(emissions) == 1 and commits == ['commit']
        dispatcher.assert_called_once_with(change.pending_invitation_ids)
    finally:
        event.remove(session, 'after_commit', committed)


def test_failed_effect_preserves_committed_durable_work(setup, monkeypatch, caplog):
    change = claim(setup).unwrap()
    work = db.session.scalars(select(DbMatchInvitation).where(
        DbMatchInvitation.match_id == setup.match_id,
        DbMatchInvitation.pairing_generation == change.match.pairing_generation,
        DbMatchInvitation.recipient_id == setup.actors[MatchSide.A],
    )).one()
    invitation_id = work.id
    assert invitation_id in change.pending_invitation_ids
    assert work.status == 'pending' and work.attempts == 0
    assert work.dispatch_token is work.lease_until is work.accepted_at is None
    repo.commit_session()
    monkeypatch.setattr(service.match_ready_claimed, 'send', Mock(side_effect=RuntimeError('listener failure')))
    real_dispatch = invitations.dispatch_match_invitations
    refused = Mock(side_effect=ConnectionError('private redis'))
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', refused)
    assert service.dispatch_readiness_effects(change).unwrap_err() == 'readiness_dispatch_failed'
    refused.assert_called_once_with(real_dispatch, change.pending_invitation_ids)
    with Session(db.engine) as observer:
        assert observer.get(DbTournamentMatch, setup.match_id).ready_at_a is not None
        retained = observer.get(DbMatchInvitation, invitation_id)
        assert retained.status == 'pending' and retained.attempts == 0
    assert 'recover pending invitation IDs' in caplog.text
    assert all(str(item) in caplog.text for item in change.pending_invitation_ids)
    # A fresh scoped session recovers the actual committed IDs, not invented work.
    deliveries = []

    def enqueue(function, *args):
        if function is real_dispatch:
            return function(*args)  # Synchronous RQ runs the batch job in place.
        deliveries.append((function, args))

    monkeypatch.setattr(invitations.jobqueue, 'enqueue', enqueue)
    monkeypatch.setattr(invitations.jobqueue, 'enqueue_at', Mock())
    db.session.remove()
    assert service.dispatch_pending_invitations(change.pending_invitation_ids).is_ok()
    assert len(deliveries) == len(change.pending_invitation_ids)
    with Session(db.engine) as observer:
        for item in change.pending_invitation_ids:
            retained = observer.get(DbMatchInvitation, item)
            assert retained.status == 'queued' and retained.attempts == 1
            assert retained.dispatch_token is not None and retained.lease_until is not None
            assert retained.accepted_at is None


def test_tournament_first_lock_fresh_pause_and_err_release(setup):
    cached = db.session.get(DbTournament, setup.tournament_id)
    assert cached.tournament_status == 'ONGOING'
    with Session(db.engine) as other:
        other.execute(update(DbTournament).where(DbTournament.id == setup.tournament_id).values(tournament_status='PAUSED'))
        other.commit()
    assert cached.tournament_status == 'ONGOING'
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if 'FOR UPDATE' in statement:
            statements.append(statement)
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        assert claim(setup).unwrap_err() == 'tournament_not_ongoing'
        assert cached.tournament_status == 'PAUSED'
        assert 'lan_tournaments' in statements[0] and 'lan_tournament_matches' in statements[1]
        assert db.session().in_transaction()
        repo.rollback_session()
        with Session(db.engine) as other:
            other.execute(select(DbTournament).where(DbTournament.id == setup.tournament_id).with_for_update(nowait=True))
            other.execute(select(DbTournamentMatch).where(DbTournamentMatch.id == setup.match_id).with_for_update(nowait=True))
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)


@pytest.mark.parametrize('change', ['removed', 'foreign', 'team'])
def test_cached_solo_authority_is_refreshed(setup, change):
    pairing = repo.get_match_pairing(setup.match_id)
    cached = db.session.get(DbTournamentParticipant, pairing.side_a.id)
    before = repo.get_match(setup.match_id)
    values = {'removed_at': NOW} if change == 'removed' else {'team_id': uuid7()}
    if change == 'foreign':
        foreign = DbTournament(uuid7(), cached.tournament.party_id, f'Foreign {uuid7()}', NOW)
        db.session.add(foreign)
        db.session.commit()
        values = {'tournament_id': foreign.id}
        # Repopulate after commit so the independent update is genuinely cached.
        cached = db.session.get(DbTournamentParticipant, pairing.side_a.id)
    elif change == 'team':
        team = DbTournamentTeam(values['team_id'], setup.tournament_id, 'Moved team', cached.user_id, NOW)
        db.session.add(team)
        db.session.commit()
        cached = db.session.get(DbTournamentParticipant, pairing.side_a.id)
    with Session(db.engine) as other:
        other.execute(update(DbTournamentParticipant).where(DbTournamentParticipant.id == cached.id).values(**values))
        other.commit()
    assert claim(setup).unwrap_err() == 'readiness_pairing_invalid'
    assert repo.get_match(setup.match_id) == before


def _team_pair(setup):
    teams = [DbTournamentTeam(uuid7(), setup.tournament_id, f'Team {i}', setup.users[i].id, NOW)
             for i in range(2)]
    db.session.add_all(teams)
    db.session.flush()
    for participant, team in zip(setup.participants[:2], teams, strict=True):
        participant.team_id = team.id
    setup.participants[2].team_id = teams[0].id
    for contestant, team in zip(setup.contestants, teams, strict=True):
        contestant.participant_id = None
        contestant.team_id = team.id
    db.session.commit()
    assert service.refresh_pairing_and_invitations_flush(setup.match_id, occurred_at=NOW + timedelta(minutes=1)).is_ok()
    db.session.commit()
    pairing = repo.get_match_pairing(setup.match_id)
    for side, identity in ((MatchSide.A, pairing.side_a), (MatchSide.B, pairing.side_b)):
        setup.actors[side] = repo.get_team(TournamentTeamID(identity.id)).captain_user_id
    return teams


@pytest.mark.parametrize('change', ['captain', 'member_owner', 'member_removed'])
def test_cached_captain_and_member_authority_is_current(setup, change):
    teams = _team_pair(setup)
    side = next(s for s, u in setup.actors.items() if u == setup.users[0].id)
    team = db.session.get(DbTournamentTeam, teams[0].id)
    member = db.session.get(DbTournamentParticipant, setup.participants[0].id)
    assert team.captain_user_id == member.user_id == setup.users[0].id
    before, entries = repo.get_match(setup.match_id), logs(setup)
    with Session(db.engine) as other:
        if change == 'captain':
            other.execute(update(DbTournamentTeam).where(DbTournamentTeam.id == team.id).values(captain_user_id=setup.users[2].id))
        elif change == 'member_owner':
            # Still selected by the member query; the identity-map user attribute
            # must refresh rather than authorizing the previous cached captain.
            other.execute(delete(DbTournamentParticipant).where(DbTournamentParticipant.id == setup.participants[3].id))
            other.execute(update(DbTournamentParticipant).where(DbTournamentParticipant.id == member.id).values(user_id=setup.users[3].id))
        else:
            other.execute(update(DbTournamentParticipant).where(DbTournamentParticipant.id == member.id).values(removed_at=NOW))
        other.commit()
    assert claim(setup, side, setup.users[0].id).unwrap_err() == 'readiness_forbidden'
    assert repo.get_match(setup.match_id) == before and logs(setup) == entries
    repo.rollback_session()
    if change == 'captain':
        assert claim(setup, side, setup.users[2].id).unwrap().actor_role == 'player'


def test_team_member_scoped_orga_override_then_current_scope_removal(setup):
    _team_pair(setup)
    member_id = setup.users[2].id
    assert claim(setup, user=member_id).unwrap_err() == 'readiness_forbidden'
    repo.rollback_session()
    appointment = DbTournamentOrga(uuid7(), setup.tournament_id, member_id, NOW)
    db.session.add(appointment)
    db.session.commit()
    assert claim(setup, user=member_id).unwrap().actor_role == 'orga'
    repo.rollback_session()
    cached = db.session.get(DbTournamentOrga, appointment.id)
    with Session(db.engine) as other:
        other.execute(delete(DbTournamentOrga).where(DbTournamentOrga.id == cached.id))
        other.commit()
    assert claim(setup, user=member_id).unwrap_err() == 'readiness_forbidden'


def test_reset_and_same_pair_refresh_noop(setup):
    before = repo.get_match(setup.match_id)
    entries = logs(setup)
    assert service.refresh_pairing_and_invitations_flush(setup.match_id, occurred_at=NOW).unwrap().match == before
    assert logs(setup) == entries
    assert claim(setup).is_ok()
    reset = service.reset_readiness_flush(setup.match_id, occurred_at=NOW + timedelta(minutes=2)).unwrap()
    assert reset.match.ready_at_a is None
    assert reset.match.readiness_revision == before.readiness_revision + 2
    assert reset.match.pairing_generation == before.pairing_generation
    assert reset.match.occupied_since == before.occupied_since
    assert any(e.event_type == 'match-readiness-reset' for e in logs(setup))
