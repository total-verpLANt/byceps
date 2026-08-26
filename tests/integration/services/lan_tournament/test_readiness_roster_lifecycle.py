"""Real PostgreSQL roster transactions and durable post-commit handoffs."""

from datetime import UTC, datetime
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

from flask import current_app
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_invitation_service as invitations,
    tournament_log_service as log_service,
    tournament_match_service as engine,
    tournament_participant_service as participants,
    tournament_readiness_authorization_service as authority,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
    tournament_team_service as teams,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import DbTournamentMatchToContestant
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation, DbMatchPairing
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_participant import TournamentParticipant, TournamentParticipantID
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    slug = uuid7().hex[-12:]
    brand = make_brand(f'roster10-{slug}', 'Roster lifecycle')
    return make_party(brand, PartyID(f'roster10-{slug}'), 'Roster lifecycle')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Roster10-{uuid7().hex[-12:]}') for _ in range(9)]


@pytest.fixture
def world(party, users, monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(
        uuid7(), party.id, f'Roster {uuid7()}', now,
        contestant_type='TEAM', game_format='ONE_V_ONE',
        elimination_mode='SINGLE_ELIMINATION', tournament_status='REGISTRATION_OPEN',
        max_players_in_team=8,
    )
    db.session.add(tournament)
    db.session.flush()
    team_rows = [
        DbTournamentTeam(uuid7(), tournament.id, f'Team {i}', users[i * 2].id, now)
        for i in range(2)
    ]
    db.session.add_all(team_rows)
    db.session.flush()
    roster = [
        DbTournamentParticipant(
            uuid7(), user.id, tournament.id, now,
            team_id=team_rows[i // 2].id if i < 4 else None,
        )
        for i, user in enumerate(users[:5])
    ]
    db.session.add_all(roster)
    db.session.flush()
    match_ids = []
    invitation_ids = []
    for order in range(2):
        match = DbTournamentMatch(uuid7(), tournament.id, now, match_order=order, round=order)
        db.session.add(match)
        db.session.flush()
        for team in team_rows:
            db.session.add(DbTournamentMatchToContestant(uuid7(), match.id, now, team_id=team.id))
        db.session.flush()
        repo.get_tournament_for_update(tournament.id)
        repo.get_match_for_update(match.id)
        assert repo.refresh_match_pairing_flush(match.id, occurred_at=now).is_ok()
        repo.set_side_ready_flush(match.id, MatchSide.A, now, users[0].id)
        repo.set_side_ready_flush(match.id, MatchSide.B, now, users[2].id)
        invitation_ids.extend(repo.ensure_invitation_intents_flush(
            match.id, [u.id for u in users[:4]], occurred_at=now,
        ))
        match_ids.append(match.id)
    sends = {}
    for name in ('team_member_joined', 'team_member_left', 'team_deleted',
                 'captain_transferred', 'participant_joined', 'participant_left',
                 'contestant_advanced', 'match_confirmed', 'tournament_completed'):
        sends[name] = Mock()
        monkeypatch.setattr(getattr(signals, name), 'send', sends[name])
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', Mock())
    monkeypatch.setattr(participants.ticket_service, 'uses_any_ticket_for_party', lambda *_: True)
    # ensure returns dispatchable IDs, not every retained row. Registration
    # work is suppressed, so load its real IDs before seeding accepted facts.
    invitation_ids = list(db.session.scalars(select(DbMatchInvitation.id).where(
        DbMatchInvitation.tournament_id == tournament.id,
    )))
    assert len(invitation_ids) == 8
    db.session.commit()
    yield SimpleNamespace(
        tournament_id=tournament.id, party_id=party.id,
        team_ids=[t.id for t in team_rows], participant_ids=[p.id for p in roster],
        user_ids=[u.id for u in users], match_ids=sorted(match_ids),
        invitation_ids=invitation_ids, now=now, sends=sends,
    )
    db.session.rollback()


READINESS_LOG_PREFIXES = ('match-ready-', 'match-readiness-', 'match-pairing-')


def _readiness_logs(world):
    """Return the readiness audit entries per match, newest first."""
    entries = log_service.get_recent_entries_for_tournament(
        world.tournament_id, READINESS_LOG_PREFIXES, limit=10_000
    )
    return {
        match_id: [e for e in entries if e.data.get('match_id') == str(match_id)]
        for match_id in world.match_ids
    }


def _snapshot(world):
    """Independent committed facts, including claims, work and history."""
    with Session(db.engine) as session:
        roster = session.scalars(select(DbTournamentParticipant).where(
            DbTournamentParticipant.tournament_id == world.tournament_id,
        ).order_by(DbTournamentParticipant.id)).all()
        team_rows = session.scalars(select(DbTournamentTeam).where(
            DbTournamentTeam.tournament_id == world.tournament_id,
        ).order_by(DbTournamentTeam.id)).all()
        matches = session.scalars(select(DbTournamentMatch).where(
            DbTournamentMatch.tournament_id == world.tournament_id,
        ).order_by(DbTournamentMatch.id)).all()
        work = session.scalars(select(DbMatchInvitation).where(
            DbMatchInvitation.tournament_id == world.tournament_id,
        ).order_by(DbMatchInvitation.id)).all()
        pairs = session.scalars(select(DbMatchPairing).where(
            DbMatchPairing.tournament_id == world.tournament_id,
        ).order_by(DbMatchPairing.id)).all()
        return (
            [(p.id, p.user_id, p.team_id, p.removed_at, p.substitute_player) for p in roster],
            [(t.id, t.captain_user_id, t.removed_at) for t in team_rows],
            [(m.id, m.pairing_id, m.pairing_generation, m.readiness_revision,
              m.ready_at_a, m.ready_at_b, m.ready_by_a, m.ready_by_b,
              m.occupied_since, m.confirmed_by) for m in matches],
            [(w.id, w.recipient_id, w.status, w.dispatch_token, w.last_error,
              w.accepted_at, w.attempts, w.expected_readiness_revision) for w in work],
            [(p.id, p.generation, p.side_a_id, p.side_b_id, p.started_at, p.ended_at) for p in pairs],
        )


def _operation(world, name, monkeypatch):
    if name == 'join':
        return lambda: teams.join_team(world.participant_ids[4], world.team_ids[0])
    if name == 'admin_add_member':
        return lambda: teams.admin_add_member(world.team_ids[0], world.user_ids[4])
    if name == 'leave':
        return lambda: teams.leave_team(world.participant_ids[1])
    if name == 'delete':
        return lambda: teams.delete_team(world.team_ids[0])
    if name == 'remove_member':
        return lambda: teams.remove_team_member(world.team_ids[0], world.user_ids[1])
    if name == 'transfer':
        return lambda: teams.transfer_captain(world.team_ids[0], world.user_ids[1])
    if name == 'admin_remove':
        return lambda: participants.admin_remove_participant(world.tournament_id, world.participant_ids[1])
    if name == 'leave_tournament':
        return lambda: participants.leave_tournament(world.tournament_id, world.participant_ids[1])
    if name == 'join_tournament':
        return lambda: participants.join_tournament(world.tournament_id, world.user_ids[5], team_id=world.team_ids[0])
    if name == 'admin_add_participant':
        return lambda: participants.admin_add_participant(world.tournament_id, world.user_ids[5], team_id=world.team_ids[0])
    assert name == 'ticketless'
    monkeypatch.setattr(participants.ticket_service, 'select_ticket_users_for_party',
                        lambda *_: set(world.user_ids) - {world.user_ids[0]})
    return lambda: participants.remove_participants_without_tickets(world.tournament_id, world.party_id)


OPERATIONS = ['join', 'admin_add_member', 'leave', 'delete', 'remove_member',
              'transfer', 'admin_remove', 'leave_tournament', 'join_tournament',
              'admin_add_participant', 'ticketless']


@pytest.mark.parametrize('operation', OPERATIONS)
def test_roster_refresh_is_atomic_and_signals_follow_one_commit(world, monkeypatch, operation):
    before = _snapshot(world)
    run = _operation(world, operation, monkeypatch)
    seen = []
    commits = []
    refresh = participants._refresh_roster_matches_flush
    commit = repo.commit_session

    def checked_refresh(match_ids, *, occurred_at):
        assert sorted(match_ids) == world.match_ids
        assert _snapshot(world) == before
        result = refresh(match_ids, occurred_at=occurred_at)
        assert result.is_ok(), result.unwrap_err()
        assert _snapshot(world) == before
        seen.append(list(match_ids))
        return result

    def checked_commit():
        assert len(seen) == 1
        assert all(not send.called for send in world.sends.values())
        commits.append(True)
        commit()

    monkeypatch.setattr(participants, '_refresh_roster_matches_flush', checked_refresh)
    monkeypatch.setattr(teams, '_refresh_roster_matches_flush', checked_refresh)
    monkeypatch.setattr(repo, 'commit_session', checked_commit)
    result = run()
    assert result.is_ok(), result.unwrap_err()
    assert len(commits) == 1
    assert any(send.called for send in world.sends.values())
    after = _snapshot(world)
    assert after != before
    assert [m[8] for m in after[2]] == [m[8] for m in before[2]]
    assert all(not p[4] for p in after[0])
    if operation == 'delete':
        assert all(m[1] is None and m[4:8] == (None, None, None, None) for m in after[2])
        assert all(p[5] is not None for p in after[4])
    else:
        # A team roster/captain change is not a replacement opponent.
        assert after[2] == before[2]
        assert after[4] == before[4]


@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('failure', ['err', 'exception'])
def test_roster_refresh_failure_restores_membership_readiness_and_work(world, monkeypatch, operation, failure):
    before = _snapshot(world)
    run = _operation(world, operation, monkeypatch)
    refresh = participants._refresh_roster_matches_flush

    def failing_refresh(match_ids, *, occurred_at):
        result = refresh(match_ids, occurred_at=occurred_at)
        assert result.is_ok(), result.unwrap_err()
        # Ensure even staged changes to claims/revision are rolled back.
        repo.clear_match_readiness_flush(world.match_ids[0], increment_revision=True)
        assert _snapshot(world) == before
        if failure == 'exception':
            raise RuntimeError('injected roster refresh failure')
        return Err('injected_roster_refresh_failure')

    monkeypatch.setattr(participants, '_refresh_roster_matches_flush', failing_refresh)
    monkeypatch.setattr(teams, '_refresh_roster_matches_flush', failing_refresh)
    if failure == 'exception':
        with pytest.raises(RuntimeError, match='injected roster refresh failure'):
            run()
    else:
        assert run().unwrap_err() == 'injected_roster_refresh_failure'
    assert _snapshot(world) == before
    assert all(not send.called for send in world.sends.values())


@pytest.mark.parametrize('operation', ['join', 'remove_member', 'transfer', 'admin_remove', 'ticketless'])
def test_removal_and_captain_transfer_are_locked(world, monkeypatch, operation):
    run = _operation(world, operation, monkeypatch)
    locks = []
    ordered = []
    real_lock = repo.lock_matches_for_update

    def observe_matches(ids):
        ordered.append(list(ids))
        return real_lock(ids)

    def observe_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if 'FOR UPDATE' not in statement:
            return
        for table in ('lan_tournaments', 'lan_tournament_teams',
                      'lan_tournament_participants', 'lan_tournament_matches'):
            if f'FROM {table}' in statement:
                locks.append(table)
                break

    monkeypatch.setattr(repo, 'lock_matches_for_update', observe_matches)
    event.listen(db.engine, 'before_cursor_execute', observe_sql)
    try:
        assert run().is_ok()
    finally:
        event.remove(db.engine, 'before_cursor_execute', observe_sql)
    assert locks[0] == 'lan_tournaments'
    first_team = locks.index('lan_tournament_teams')
    first_member = locks.index('lan_tournament_participants')
    first_match = locks.index('lan_tournament_matches')
    assert first_team < first_member < first_match
    assert ordered[0] == world.match_ids
    assert all(ids == sorted(set(ids)) for ids in ordered)


@pytest.mark.parametrize('operation', ['transfer', 'remove_member'])
def test_roster_mutation_waits_for_actual_tournament_lock(world, operation, monkeypatch):
    app = current_app._get_current_object()
    ready = Event()
    finished = Event()
    pids = []
    outcomes = []
    before = _snapshot(world)

    def worker():
        with app.app_context():
            try:
                pids.append(db.session.execute(text('SELECT pg_backend_pid()')).scalar_one())
                ready.set()
                outcomes.append(_operation(world, operation, monkeypatch)())
            except BaseException as exc:
                outcomes.append(exc)
            finally:
                db.session.remove()
                finished.set()

    with Session(db.engine) as holder:
        holder.execute(select(DbTournament).where(
            DbTournament.id == world.tournament_id,
        ).with_for_update())
        holder_pid = holder.execute(text('SELECT pg_backend_pid()')).scalar_one()
        thread = Thread(target=worker, daemon=True)
        thread.start()
        assert ready.wait(5)
        deadline = monotonic() + 5
        blocked = False
        while monotonic() < deadline:
            with Session(db.engine) as probe:
                blockers = probe.execute(text('SELECT pg_blocking_pids(:pid)'), {'pid': pids[0]}).scalar_one()
            if holder_pid in blockers:
                blocked = True
                break
            sleep(0.02)
        try:
            assert blocked, 'PostgreSQL must show the worker waiting for the tournament lock'
            assert not finished.is_set()
            assert _snapshot(world) == before
        finally:
            holder.rollback()
    thread.join(10)
    assert finished.is_set()
    assert len(outcomes) == 1 and not isinstance(outcomes[0], BaseException), outcomes
    assert outcomes[0].is_ok()
    assert _snapshot(world) != before


def test_current_captain_authority_and_accepted_recipient_deduplication(world):
    for invitation_id in world.invitation_ids:
        work = db.session.get(DbMatchInvitation, invitation_id)
        work.status = 'accepted'
        work.accepted_at = world.now
    db.session.commit()
    before = _snapshot(world)
    assert all(w[2] == 'accepted' for w in before[3])
    pair = repo.get_match_pairing(world.match_ids[0])
    side = MatchSide.A if pair.side_a.id == world.team_ids[0] else MatchSide.B
    assert authority.authorize_readiness_side(world.tournament_id, pair, side, world.user_ids[0]).is_ok()
    assert authority.authorize_readiness_side(world.tournament_id, pair, side, world.user_ids[1]).unwrap_err() == 'readiness_forbidden'
    assert teams.transfer_captain(world.team_ids[0], world.user_ids[1]).is_ok()
    repo.get_tournament_for_update(world.tournament_id)
    pair = repo.get_match_pairing(world.match_ids[0])
    assert authority.authorize_readiness_side(world.tournament_id, pair, side, world.user_ids[1]).is_ok()
    assert authority.authorize_readiness_side(world.tournament_id, pair, side, world.user_ids[0]).unwrap_err() == 'readiness_forbidden'
    for match_id in world.match_ids:
        assert invitations.reconcile_match_invitations_flush(match_id, occurred_at=datetime.now(UTC)).is_ok()
    db.session.commit()
    after = _snapshot(world)
    assert after[2:] == before[2:]
    assert len(after[3]) == 8


@pytest.mark.parametrize('ticketless', [False, True])
def test_ready_mutation_uses_current_captain_after_roster_change(world, monkeypatch, ticketless):
    db.session.get(DbTournament, world.tournament_id).tournament_status = 'ONGOING'
    db.session.commit()
    if ticketless:
        run = _operation(world, 'ticketless', monkeypatch)
        assert run().is_ok()
    else:
        assert teams.transfer_captain(world.team_ids[0], world.user_ids[1]).is_ok()
    match_id = world.match_ids[0]
    pair = repo.get_match_pairing(match_id)
    side = MatchSide.A if pair.side_a.id == world.team_ids[0] else MatchSide.B
    match = repo.get_match(match_id)
    revoked = readiness.revoke_ready_flush(
        match_id, side, world.user_ids[1],
        expected_pairing_generation=match.pairing_generation,
        expected_readiness_revision=match.readiness_revision,
    )
    assert revoked.is_ok(), revoked.unwrap_err()
    repo.commit_session()
    match = repo.get_match(match_id)
    kwargs = dict(expected_pairing_generation=match.pairing_generation,
                  expected_readiness_revision=match.readiness_revision)
    refused = readiness.claim_ready_flush(match_id, side, world.user_ids[0], **kwargs)
    assert refused.unwrap_err() == 'readiness_forbidden'
    repo.rollback_session()
    claimed = readiness.claim_ready_flush(match_id, side, world.user_ids[1], **kwargs)
    assert claimed.is_ok(), claimed.unwrap_err()
    repo.commit_session()
    match = repo.get_match(match_id)
    actor = match.ready_by_a if side == MatchSide.A else match.ready_by_b
    assert actor == world.user_ids[1]


def test_fresh_join_code_and_membership_are_read_after_tournament_lock(world):
    # Prime this session's identity map, then change facts in another session.
    cached_team = db.session.get(DbTournamentTeam, world.team_ids[0])
    cached_member = db.session.get(DbTournamentParticipant, world.participant_ids[4])
    assert cached_team.join_code is None and cached_member.team_id is None
    with Session(db.engine) as independent:
        independent.get(DbTournamentTeam, world.team_ids[0]).join_code = 'fresh-code'
        independent.commit()
    assert cached_team.join_code is None
    before = _snapshot(world)
    assert teams.join_team(world.participant_ids[4], world.team_ids[0], 'old-code').unwrap_err() == 'Invalid join code.'
    assert _snapshot(world) == before
    assert teams.join_team(world.participant_ids[4], world.team_ids[0], 'fresh-code').is_ok()
    assert cached_member.team_id == world.team_ids[0]
    with Session(db.engine) as independent:
        independent.get(DbTournamentParticipant, world.participant_ids[4]).team_id = world.team_ids[1]
        independent.commit()
    assert cached_member.team_id == world.team_ids[0]
    before = _snapshot(world)
    assert teams.join_team(world.participant_ids[4], world.team_ids[0], 'fresh-code').unwrap_err() == 'Participant is already on a team.'
    assert _snapshot(world) == before


def test_team_creation_refuses_fresh_captain_membership(world):
    cached = db.session.get(DbTournamentParticipant, world.participant_ids[4])
    assert cached.team_id is None
    with Session(db.engine) as independent:
        independent.get(DbTournamentParticipant, cached.id).team_id = world.team_ids[0]
        independent.commit()
    assert cached.team_id is None
    before = _snapshot(world)
    result = teams.create_team(world.tournament_id, f'Fresh team {uuid7()}', world.user_ids[4])
    assert result.unwrap_err() == 'The team captain is already assigned to a team.'
    assert _snapshot(world) == before


def test_last_member_leave_cleans_generated_assignments_in_one_commit(world, monkeypatch):
    repo.delete_participants_by_ids({world.participant_ids[1]})
    repo.commit_session()
    before = _snapshot(world)
    assert teams.leave_team(world.participant_ids[0]).is_ok()
    after = _snapshot(world)
    assert all(t[0] != world.team_ids[0] for t in after[1])
    assert all(m[1] is None and m[4:8] == (None, None, None, None) for m in after[2])
    assert all(p[5] is not None for p in after[4])
    assert [m[8] for m in before[2]] == [m[8] for m in after[2]]


def test_confirmed_team_delete_retires_pairing_and_preserves_accepted_work(world):
    for invitation_id in world.invitation_ids:
        work = db.session.get(DbMatchInvitation, invitation_id)
        work.status = 'accepted'
        work.accepted_at = world.now
    for match_id in world.match_ids:
        match = db.session.get(DbTournamentMatch, match_id)
        match.confirmed_by = world.user_ids[0]
        match.confirmed_at = world.now
    db.session.commit()
    before = _snapshot(world)
    assert all(w[2] == 'accepted' for w in before[3])
    assert teams.delete_team(world.team_ids[0]).is_ok()
    after = _snapshot(world)
    assert after[3] == before[3]
    assert all(p[5] is not None for p in after[4])
    assert all(m[1] is None and m[4:8] == (None, None, None, None) for m in after[2])
    assert [m[8:] for m in after[2]] == [m[8:] for m in before[2]]
    for history in _readiness_logs(world).values():
        assert any(log.event_type == 'match-pairing-invalidated' for log in history)


@pytest.mark.parametrize('failure', ['commit', 'audit'])
def test_roster_commit_and_audit_failure_roll_back(world, monkeypatch, failure):
    before = _snapshot(world)

    def fail(*_args, **_kwargs):
        raise RuntimeError(f'injected {failure} failure')

    if failure == 'commit':
        monkeypatch.setattr(repo, 'commit_session', fail)
    else:
        monkeypatch.setattr(participants.tournament_log_service, 'create_log_entry', fail)
    with pytest.raises(RuntimeError, match=f'injected {failure} failure'):
        participants.admin_remove_participant(world.tournament_id, world.participant_ids[1])
    assert _snapshot(world) == before
    assert all(not send.called for send in world.sends.values())


def test_team_delete_pairing_audit_failure_rolls_back_every_fact(world, monkeypatch):
    before = _snapshot(world)
    histories = _readiness_logs(world)

    def fail(*_args, **_kwargs):
        raise RuntimeError('injected pairing audit failure')

    monkeypatch.setattr(engine, 'create_log_entry', fail)
    with pytest.raises(RuntimeError, match='injected pairing audit failure'):
        teams.delete_team(world.team_ids[0])
    assert _snapshot(world) == before
    assert _readiness_logs(world) == histories
    assert all(not send.called for send in world.sends.values())


@pytest.mark.parametrize('refusal', ['foreign_member', 'foreign_team', 'foreign_party',
                                  'foreign_admin_remove', 'foreign_leave',
                                  'removed_team', 'removed_participant',
                                  'leave_started', 'leave_team_started', 'captain_removal',
                                  'nonmember_captain', 'already_on_team'])
def test_negative_scope_and_state_preserve_all_facts(world, party, monkeypatch, refusal):
    foreign = DbTournament(uuid7(), party.id, f'Foreign {uuid7()}', world.now,
                           contestant_type='TEAM', tournament_status='REGISTRATION_OPEN')
    db.session.add(foreign)
    db.session.flush()
    foreign_team = DbTournamentTeam(uuid7(), foreign.id, 'Foreign team', world.user_ids[6], world.now)
    foreign_member = DbTournamentParticipant(uuid7(), world.user_ids[6], foreign.id, world.now)
    db.session.add_all([foreign_team, foreign_member])
    if refusal in {'leave_started', 'leave_team_started'}:
        db.session.get(DbTournament, world.tournament_id).tournament_status = 'ONGOING'
    if refusal == 'removed_team':
        db.session.get(DbTournamentTeam, world.team_ids[0]).removed_at = datetime.now(UTC)
    if refusal == 'removed_participant':
        db.session.get(DbTournamentParticipant, world.participant_ids[4]).removed_at = datetime.now(UTC)
    db.session.commit()
    foreign_world = SimpleNamespace(tournament_id=foreign.id)
    before = _snapshot(world)
    foreign_before = _snapshot(foreign_world)
    actions = {
        'foreign_member': lambda: teams.join_team(foreign_member.id, world.team_ids[0]),
        'foreign_team': lambda: participants.admin_add_participant(world.tournament_id, world.user_ids[7], team_id=foreign_team.id),
        'foreign_party': lambda: participants.remove_participants_without_tickets(world.tournament_id, PartyID('foreign-party')),
        'foreign_admin_remove': lambda: participants.admin_remove_participant(world.tournament_id, foreign_member.id),
        'foreign_leave': lambda: participants.leave_tournament(world.tournament_id, foreign_member.id),
        'removed_team': lambda: teams.join_team(world.participant_ids[4], world.team_ids[0]),
        'removed_participant': lambda: teams.join_team(world.participant_ids[4], world.team_ids[0]),
        'leave_started': lambda: participants.leave_tournament(world.tournament_id, world.participant_ids[1]),
        'leave_team_started': lambda: teams.leave_team(world.participant_ids[1]),
        'captain_removal': lambda: teams.remove_team_member(world.team_ids[0], world.user_ids[0]),
        'nonmember_captain': lambda: teams.transfer_captain(world.team_ids[0], world.user_ids[4]),
        'already_on_team': lambda: teams.join_team(world.participant_ids[1], world.team_ids[1]),
    }
    assert actions[refusal]().is_err()
    assert _snapshot(world) == before
    assert _snapshot(foreign_world) == foreign_before
    assert all(not send.called for send in world.sends.values())


@pytest.fixture
def generated_factory(party, users):
    def make(*, team=False, active=False):
        result = tournament_service.create_tournament(
            party.id, f'Generated roster {uuid7()}',
            contestant_type=ContestantType.TEAM if team else ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        roster = []
        team_ids = []
        for user in users[:4]:
            pid = TournamentParticipantID(uuid7())
            repo.create_participant(TournamentParticipant(
                id=pid, user_id=user.id, tournament_id=tournament.id,
                substitute_player=False, team_id=None, created_at=datetime.now(UTC),
            ))
            roster.append(pid)
        db.session.commit()
        if team:
            for user in users[:4]:
                created = teams.create_team(tournament.id, f'Generated team {uuid7()}', user.id)
                assert created.is_ok(), created.unwrap_err()
                team_ids.append(created.unwrap()[0].id)
        board = seeding.get_board(tournament.id).unwrap()
        generated = seeding.generate_from_seeding(
            tournament.id, expected_version=board.version, initiator_id=users[0].id,
        )
        assert generated.is_ok(), generated.unwrap_err()
        if active:
            started = tournament_service.change_status(tournament.id, TournamentStatus.ONGOING, users[0].id)
            assert started.is_ok(), started.unwrap_err()
        return SimpleNamespace(tournament_id=tournament.id, participant_ids=roster,
                               team_ids=team_ids, party_id=party.id, user_ids=[u.id for u in users])

    yield make
    db.session.rollback()


@pytest.mark.parametrize('team', [False, True])
@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('ticketless', [False, True])
def test_generated_and_active_removals_preserve_engine_rules(generated_factory, users, monkeypatch, team, active, ticketless):
    world = generated_factory(team=team, active=active)
    matches_before = repo.get_matches_for_tournament(world.tournament_id)
    occupied = {m.id: m.occupied_since for m in matches_before}
    if ticketless:
        monkeypatch.setattr(participants.ticket_service, 'select_ticket_users_for_party',
                            lambda *_: set(world.user_ids) - {world.user_ids[0]})
        result = participants.remove_participants_without_tickets(world.tournament_id, world.party_id, initiator_id=users[0].id)
        assert result.is_ok() and result.unwrap() == 1
    elif team and not active:
        result = teams.delete_team(world.team_ids[0])
        assert result.is_ok(), result.unwrap_err()
    else:
        result = participants.admin_remove_participant(world.tournament_id, world.participant_ids[0], initiator=users[0])
        assert result.is_ok(), result.unwrap_err()
    db.session.rollback()
    with Session(db.engine) as session:
        removed = session.get(DbTournamentParticipant, world.participant_ids[0])
        if active:
            assert removed is not None and removed.removed_at is not None
            if team:
                assert session.get(DbTournamentTeam, world.team_ids[0]).removed_at is not None
        elif team and not ticketless:
            assert removed is not None and removed.team_id is None
            assert session.get(DbTournamentTeam, world.team_ids[0]) is None
        else:
            assert removed is None
    after = repo.get_matches_for_tournament(world.tournament_id)
    entries = [c for m in after for c in repo.get_contestants_for_match(m.id)]
    if team:
        assert all(c.team_id != world.team_ids[0] for c in entries)
    else:
        assert all(c.participant_id != world.participant_ids[0] for c in entries)
    assert all(m.occupied_since == occupied[m.id] for m in after)
    if active:
        assert any(m.confirmed_by is not None for m in after)
        # No explicit Ready claim is required to award a defwin.
        assert all(m.ready_at_a is m.ready_at_b is None for m in after)
    else:
        assert all(m.confirmed_by is None for m in after)
        board = seeding.get_board(world.tournament_id).unwrap()
        assert board.stale
        victim = world.team_ids[0] if team else world.participant_ids[0]
        assert str(victim) in board.stale_leaver_ids
        start = tournament_service.change_status(world.tournament_id, TournamentStatus.ONGOING, users[0].id)
        assert start.is_err()


def test_generated_team_member_removal_keeps_original_team_contestant(generated_factory, users):
    world = generated_factory(team=True)
    before = _snapshot(world)
    assert participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=users[0],
    ).is_ok()
    after = _snapshot(world)
    assert all(p[0] != world.participant_ids[0] for p in after[0])
    assert after[1:] == before[1:]
    assert repo.get_participants_for_team(world.team_ids[0]) == []
    assert repo.find_team(world.team_ids[0]) is not None
    assert not seeding.get_board(world.tournament_id).unwrap().stale


@pytest.mark.parametrize('format_name', ['FREE_FOR_ALL', 'HIGHSCORE'])
def test_unsupported_formats_never_gain_side_readiness(world, format_name):
    tournament = db.session.get(DbTournament, world.tournament_id)
    tournament.game_format = format_name
    tournament.elimination_mode = 'SINGLE_ELIMINATION' if format_name == 'FREE_FOR_ALL' else 'NONE'
    db.session.commit()
    assert teams.join_team(world.participant_ids[4], world.team_ids[0]).is_ok()
    for match_id in world.match_ids:
        match = repo.get_match(match_id)
        assert match.pairing_id is None
        assert match.ready_at_a is match.ready_at_b is None
        assert repo.get_match_pairing_history(match_id)[0].ended_at is not None
    assert all(not p.substitute_player for p in repo.get_participants_for_tournament(world.tournament_id))


ACTIVE_OPERATIONS = ['join', 'admin_add_member', 'remove_member', 'transfer',
                     'admin_remove', 'ticketless']


def _activate_roster(world):
    db.session.get(DbTournament, world.tournament_id).tournament_status = 'ONGOING'
    db.session.commit()


@pytest.mark.parametrize('operation', OPERATIONS)
def test_owner_captures_exact_ids_before_commit_and_hands_off_after_signals(world, monkeypatch, operation):
    if operation in ACTIVE_OPERATIONS:
        _activate_roster(world)
    before = _snapshot(world)
    refresh = participants._refresh_roster_matches_flush
    commit = repo.commit_session
    captured = []
    commits = []
    handed_off = []

    def collect(match_ids, *, occurred_at):
        assert _snapshot(world) == before
        result = refresh(match_ids, occurred_at=occurred_at)
        assert result.is_ok(), result.unwrap_err()
        ids = result.unwrap()
        assert isinstance(ids, tuple) and ids == tuple(sorted(set(ids), key=str))
        captured.append(ids)
        assert _snapshot(world) == before
        return result

    def owning_commit():
        assert len(captured) == 1
        assert not handed_off
        assert all(not send.called for send in world.sends.values())
        commits.append(True)
        commit()

    def dispatch(ids):
        assert len(commits) == 1
        assert any(send.called for send in world.sends.values())
        assert tuple(ids) == captured[0]
        with Session(db.engine) as independent:
            for invitation_id in ids:
                row = independent.get(DbMatchInvitation, invitation_id)
                match = independent.get(DbTournamentMatch, row.match_id)
                assert row.status == 'pending'
                assert row.pairing_generation == match.pairing_generation
                assert row.expected_readiness_revision == match.readiness_revision
        handed_off.append(tuple(ids))
        return Ok(None)

    monkeypatch.setattr(participants, '_refresh_roster_matches_flush', collect)
    monkeypatch.setattr(teams, '_refresh_roster_matches_flush', collect)
    monkeypatch.setattr(repo, 'commit_session', owning_commit)
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations', dispatch)
    assert _operation(world, operation, monkeypatch)().is_ok()
    assert len(commits) == 1 and handed_off == captured
    assert bool(captured[0]) == (operation in ACTIVE_OPERATIONS)
    if operation in {'join', 'admin_add_member'}:
        assert set(captured[0]) - set(world.invitation_ids)
        with Session(db.engine) as independent:
            created = independent.scalars(select(DbMatchInvitation).where(
                DbMatchInvitation.tournament_id == world.tournament_id,
                DbMatchInvitation.recipient_id == world.user_ids[4],
            )).all()
            assert len(created) == 2
            assert {row.id for row in created} <= set(captured[0])
    if operation in {'join_tournament', 'admin_add_participant'}:
        with Session(db.engine) as independent:
            created = independent.scalars(select(DbMatchInvitation).where(
                DbMatchInvitation.tournament_id == world.tournament_id,
                DbMatchInvitation.recipient_id == world.user_ids[5],
            )).all()
            assert len(created) == 2 and all(row.status == 'suppressed' for row in created)
    assert not invitations.jobqueue.enqueue.called


@pytest.mark.parametrize('operation', ACTIVE_OPERATIONS)
@pytest.mark.parametrize('failure', ['signal', 'dispatch_err', 'dispatch_exception'])
def test_committed_roster_intents_survive_failure_and_new_session_restart(world, monkeypatch, caplog, operation, failure):
    _activate_roster(world)
    before = _snapshot(world)
    handed_off = []

    def record(ids):
        assert _snapshot(world) != before
        handed_off.append(tuple(ids))

    def enqueue(function, ids):
        # The post-commit hand-over is one queue job for the whole batch.
        assert function is invitations.dispatch_match_invitations
        record(ids)
        raise ConnectionError('private transport detail')

    def enqueue_batch(ids):
        record(ids)
        raise RuntimeError('private transport detail')

    if failure == 'dispatch_exception':
        monkeypatch.setattr(invitations, 'enqueue_invitation_dispatch', enqueue_batch)
    else:
        monkeypatch.setattr(invitations.jobqueue, 'enqueue', Mock(side_effect=enqueue))
    if failure == 'signal':
        for send in world.sends.values():
            send.side_effect = RuntimeError('injected signal failure')
        with pytest.raises(RuntimeError, match='injected signal failure'):
            _operation(world, operation, monkeypatch)()
    else:
        assert _operation(world, operation, monkeypatch)().is_ok()
    assert len(handed_off) == 1 and handed_off[0]
    after = _snapshot(world)
    assert after != before
    db.session.remove()
    recovered = repo.select_invitation_retry_ids_flush(world.tournament_id, now=datetime.now(UTC))
    assert set(recovered) == set(handed_off[0])
    repo.rollback_session()
    assert _snapshot(world) == after
    assert all(str(invitation_id) in caplog.text for invitation_id in handed_off[0])
    assert 'private transport detail' not in caplog.text
    # Only the batch job was ever offered to the queue: nothing was claimed or sent.
    assert all(
        call.args[0] is invitations.dispatch_match_invitations
        for call in invitations.jobqueue.enqueue.call_args_list
    )


@pytest.mark.parametrize('operation', ACTIVE_OPERATIONS)
@pytest.mark.parametrize('failure', ['intent_err', 'intent_exception', 'audit'])
def test_active_roster_intent_and_audit_failures_roll_back_without_dispatch(world, monkeypatch, operation, failure):
    _activate_roster(world)
    before = _snapshot(world)
    histories = _readiness_logs(world)
    dispatched = Mock()
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations', dispatched)
    reconcile = invitations.reconcile_match_invitations_flush

    def fail_intent(match_id, *, occurred_at):
        assert reconcile(match_id, occurred_at=occurred_at).is_ok()
        if failure == 'intent_err':
            return Err('injected_intent_failure')
        raise RuntimeError('injected intent failure')

    if failure == 'audit':
        # Even owners without a roster audit must discard newly staged work
        # when the transaction's audit boundary fails.
        refresh = participants._refresh_roster_matches_flush

        def fail_audit(match_ids, *, occurred_at):
            assert refresh(match_ids, occurred_at=occurred_at).is_ok()
            participants.tournament_log_service.create_log_entry(
                'roster-probe', world.tournament_id, None, commit=False,
            )
            raise RuntimeError('injected audit failure')

        monkeypatch.setattr(participants, '_refresh_roster_matches_flush', fail_audit)
        monkeypatch.setattr(teams, '_refresh_roster_matches_flush', fail_audit)
    else:
        monkeypatch.setattr(invitations, 'reconcile_match_invitations_flush', fail_intent)
    run = _operation(world, operation, monkeypatch)
    if failure == 'intent_err':
        assert run().unwrap_err() == 'injected_intent_failure'
    else:
        with pytest.raises(RuntimeError, match='injected (intent|audit) failure'):
            run()
    assert _snapshot(world) == before
    assert _readiness_logs(world) == histories
    assert not dispatched.called and not invitations.jobqueue.enqueue.called
    assert all(not send.called for send in world.sends.values())


@pytest.mark.parametrize('team', [False, True])
@pytest.mark.parametrize('ticketless', [False, True])
def test_defwin_destinations_are_in_final_reconciliation(generated_factory, users, monkeypatch, team, ticketless):
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations', lambda _ids: Ok(None))
    world = generated_factory(team=team, active=True)
    original = {
        mid for mid, contestants in repo.get_contestants_for_tournament(world.tournament_id).items()
        for contestant in contestants
        if (contestant.team_id == world.team_ids[0] if team
            else contestant.participant_id == world.participant_ids[0])
    }
    captured = []
    advanced = []
    refresh = participants._refresh_roster_matches_flush
    handler_name = 'handle_defwin_for_removed_team' if team else 'handle_defwin_for_removed_participant'
    handler = getattr(engine, handler_name)

    def observe_defwin(*args, **kwargs):
        result = handler(*args, **kwargs)
        advanced.extend(result.advanced)
        return result

    def observe_refresh(match_ids, *, occurred_at):
        captured.append(set(match_ids))
        return refresh(match_ids, occurred_at=occurred_at)

    monkeypatch.setattr(engine, handler_name, observe_defwin)
    monkeypatch.setattr(participants, '_refresh_roster_matches_flush', observe_refresh)
    if ticketless:
        monkeypatch.setattr(participants.ticket_service, 'select_ticket_users_for_party',
                            lambda *_: set(world.user_ids) - {world.user_ids[0]})
        result = participants.remove_participants_without_tickets(world.tournament_id, world.party_id, initiator_id=users[0].id)
    else:
        result = participants.admin_remove_participant(world.tournament_id, world.participant_ids[0], initiator=users[0])
    assert result.is_ok(), result.unwrap_err()
    destinations = {evt.match_id for evt in advanced}
    assert destinations - original
    assert captured == [original | destinations]


def test_create_and_reactivate_without_assignments_have_no_invitation_work(world, monkeypatch):
    dispatch = Mock(return_value=Ok(None))
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations', dispatch)
    created = teams.create_team(world.tournament_id, f'No assignments {uuid7()}', world.user_ids[4])
    assert created.is_ok(), created.unwrap_err()
    assert not dispatch.called
    pid = TournamentParticipantID(uuid7())
    repo.create_participant(TournamentParticipant(
        id=pid, user_id=world.user_ids[5], tournament_id=world.tournament_id,
        substitute_player=False, team_id=None, created_at=datetime.now(UTC),
    ))
    repo.soft_delete_participants_by_ids({pid}, datetime.now(UTC))
    repo.commit_session()
    before_work = _snapshot(world)[3]
    result = participants.admin_add_participant(world.tournament_id, world.user_ids[5])
    assert result.is_ok() and result.unwrap()[0].id == pid
    dispatch.assert_called_once_with(())
    assert _snapshot(world)[3] == before_work
    assert not invitations.jobqueue.enqueue.called


def test_roster_handoff_preserves_accepted_unknown_and_multiple_holds(world, monkeypatch):
    _activate_roster(world)
    rows = db.session.scalars(select(DbMatchInvitation).where(
        DbMatchInvitation.match_id == world.match_ids[0],
    ).order_by(DbMatchInvitation.id)).all()
    accepted, unknown = rows[:2]
    accepted.status = 'accepted'
    accepted.accepted_at = world.now
    unknown.status = 'delivery_unknown'
    unknown.dispatch_token = uuid7()
    unknown.last_error = 'ambiguous_send'
    held = db.session.get(DbTournamentMatch, world.match_ids[1])
    held.invitation_hold_a = held.invitation_hold_b = True
    db.session.commit()
    before = {w[0]: w for w in _snapshot(world)[3]}
    captured = []
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations',
                        lambda ids: captured.append(ids) or Ok(None))
    assert teams.join_team(world.participant_ids[4], world.team_ids[0]).is_ok()
    assert len(captured) == 1 and captured[0]
    assert accepted.id not in captured[0] and unknown.id not in captured[0]
    after = {w[0]: w for w in _snapshot(world)[3]}
    assert after[accepted.id] == before[accepted.id]
    assert after[unknown.id] == before[unknown.id]
    assert repo.get_match(world.match_ids[1]).invitation_hold_a
    assert repo.get_match(world.match_ids[1]).invitation_hold_b
    for invitation_id in captured[0]:
        assert repo.get_match_invitation(invitation_id).match_id == world.match_ids[0]
    db.session.remove()
    assert set(repo.select_invitation_retry_ids_flush(world.tournament_id, now=datetime.now(UTC))) == set(captured[0])
    repo.rollback_session()


def test_real_postcommit_queue_failure_leaves_retryable_roster_work(world, monkeypatch):
    _activate_roster(world)
    before = _snapshot(world)
    commits = []
    commit = repo.commit_session
    owner_commit_counts = []
    ids = []

    def count_commit():
        commits.append(True)
        commit()

    def refuse(function, pending):
        assert function is invitations.dispatch_match_invitations
        owner_commit_counts.append(len(commits))
        assert _snapshot(world) != before
        ids.extend(pending)
        raise RuntimeError('queue unavailable')

    monkeypatch.setattr(repo, 'commit_session', count_commit)
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', Mock(side_effect=refuse))
    monkeypatch.setattr(invitations.jobqueue, 'enqueue_at', Mock())
    assert teams.join_team(world.participant_ids[4], world.team_ids[0]).is_ok()
    assert owner_commit_counts == [1] and ids
    # The batch job never reached the queue, so no recipient was claimed:
    # the owner's commit is the only one and nothing was scheduled.
    assert len(commits) == 1
    invitations.jobqueue.enqueue_at.assert_not_called()
    with Session(db.engine) as independent:
        assert independent.get(DbTournamentParticipant, world.participant_ids[4]).team_id == world.team_ids[0]
        for invitation_id in ids:
            row = independent.get(DbMatchInvitation, invitation_id)
            assert row.status == 'pending' and row.attempts == 0
            assert row.dispatch_token is None and row.next_attempt_at is None
    db.session.remove()
    recovered = repo.select_invitation_retry_ids_flush(world.tournament_id, now=datetime.now(UTC))
    assert set(recovered) >= set(ids)
    repo.rollback_session()
    # The sweep finds the durable work and hands every row to the queue.
    delivered = Mock()
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', delivered)
    assert invitations.sweep_tournament_invitations(world.tournament_id).unwrap() >= len(ids)
    queued = {call.args[1] for call in delivered.call_args_list}
    assert set(ids) <= queued
    assert all(call.args[0] is invitations.deliver_match_invitation for call in delivered.call_args_list)
    with Session(db.engine) as independent:
        for invitation_id in ids:
            row = independent.get(DbMatchInvitation, invitation_id)
            assert row.status == 'queued' and row.attempts == 1


def test_retired_roster_generation_refuses_previously_reserved_worker_token(world, monkeypatch):
    _activate_roster(world)
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations', lambda _ids: Ok(None))
    assert teams.join_team(world.participant_ids[4], world.team_ids[0]).is_ok()
    invitation_id = repo.select_invitation_retry_ids_flush(world.tournament_id, now=datetime.now(UTC))[0]
    work = repo.get_match_invitation(invitation_id)
    reserved = repo.claim_invitation_dispatch_flush(
        invitation_id, expected_token=work.dispatch_token, now=datetime.now(UTC),
    )
    assert reserved.is_ok(), reserved.unwrap_err()
    token = reserved.unwrap().dispatch_token
    repo.commit_session()
    assert teams.delete_team(world.team_ids[0]).is_ok()
    db.session.remove()
    # The worker builds locally before its final locked validation. Enable the
    # transport boundary so refusal proves stale-token safety, not SMTP config.
    build = Mock(return_value=Ok(SimpleNamespace()))
    send = Mock()
    monkeypatch.setattr(invitations.messages, 'build_match_invitation_message', build)
    monkeypatch.setattr(invitations.email_service, 'send_email', send)
    monkeypatch.setattr(invitations, 'get_current_byceps_app', lambda: SimpleNamespace(
        byceps_config=SimpleNamespace(smtp=SimpleNamespace(suppress_send=False)),
    ))
    assert invitations.deliver_match_invitation(invitation_id, token).is_err()
    build.assert_called_once()
    assert not send.called
    assert repo.get_match_invitation(invitation_id).status.value == 'suppressed'
    assert repo.get_match_pairing(work.match_id) is None


def test_final_member_defwin_destinations_are_reconciled_by_team_owner(generated_factory, monkeypatch):
    monkeypatch.setattr(readiness, 'dispatch_pending_invitations', lambda _ids: Ok(None))
    world = generated_factory(team=True, active=True)
    # An admin can remove the only remaining non-captain member of a team
    # whose recorded captain is no longer in its roster.
    repo.update_team_captain_flush(world.team_ids[0], world.user_ids[8])
    repo.commit_session()
    original = {
        mid for mid, contestants in repo.get_contestants_for_tournament(world.tournament_id).items()
        for contestant in contestants if contestant.team_id == world.team_ids[0]
    }
    captured = []
    advanced = []
    handler = engine.handle_defwin_for_removed_team
    refresh = participants._refresh_roster_matches_flush

    def observe_defwin(*args, **kwargs):
        result = handler(*args, **kwargs)
        advanced.extend(result.advanced)
        return result

    def observe_refresh(match_ids, *, occurred_at):
        captured.append(set(match_ids))
        return refresh(match_ids, occurred_at=occurred_at)

    monkeypatch.setattr(engine, 'handle_defwin_for_removed_team', observe_defwin)
    monkeypatch.setattr(teams, '_refresh_roster_matches_flush', observe_refresh)
    result = teams.remove_team_member(world.team_ids[0], world.user_ids[0])
    assert result.is_ok(), result.unwrap_err()
    destinations = {evt.match_id for evt in advanced}
    assert destinations - original
    assert captured == [original | destinations]


def test_roster_refresh_coalesces_actual_ids_and_reconciles_each_match_once(world, monkeypatch):
    _activate_roster(world)
    before = _snapshot(world)
    reconciled = []
    reconcile = invitations.reconcile_match_invitations_flush

    def observe(match_id, *, occurred_at):
        result = reconcile(match_id, occurred_at=occurred_at)
        assert result.is_ok(), result.unwrap_err()
        reconciled.append((match_id, result.unwrap()))
        return result

    monkeypatch.setattr(invitations, 'reconcile_match_invitations_flush', observe)
    participants._lock_roster_matches_flush(world.tournament_id, team_ids=world.team_ids)
    result = participants._refresh_roster_matches_flush(
        [*reversed(world.match_ids), *world.match_ids], occurred_at=datetime.now(UTC),
    )
    assert result.is_ok(), result.unwrap_err()
    assert [mid for mid, _ids in reconciled] == world.match_ids
    assert result.unwrap() == tuple(sorted({iid for _mid, ids in reconciled for iid in ids}, key=str))
    assert len(result.unwrap()) == 8
    assert _snapshot(world) == before
    assert not invitations.jobqueue.enqueue.called
    repo.rollback_session()
    assert _snapshot(world) == before
