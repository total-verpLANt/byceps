"""Private PostgreSQL proof of pairing/readiness repository facts."""

from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import DbTournamentMatchToContestant
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchInvitation
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import DbTournamentLogEntry
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5, 12)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('issue4-readiness-repository'), 'Issue 4 repository')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Issue4Repository{i}') for i in range(3)]


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(uuid7(), party.id, f'Pairing {uuid7()}', NOW,
                              game_format='ONE_V_ONE', tournament_status='ONGOING')
    db.session.add(tournament)
    db.session.flush()
    participants = [DbTournamentParticipant(uuid7(), user.id, tournament.id, NOW) for user in users]
    db.session.add_all(participants)
    match = DbTournamentMatch(uuid7(), tournament.id, NOW)
    db.session.add(match)
    db.session.flush()
    # Timestamp ties: intentionally insert the larger association ID first.
    contestants = [DbTournamentMatchToContestant(
        UUID(int=value), match.id, NOW, participant_id=participant.id,
    ) for value, participant in zip((uuid7().int, uuid7().int), participants[:2], strict=True)]
    contestants.sort(key=lambda row: row.id, reverse=True)
    db.session.add_all(contestants)
    db.session.commit()
    yield SimpleNamespace(tournament=tournament, match=match, participants=participants,
                          contestants=contestants, users=users)
    db.session.rollback()


def _refresh(match_id, when=NOW):
    repo.get_tournament_for_update(repo.get_match(match_id).tournament_id)
    repo.get_match_for_update(match_id)
    result = repo.refresh_match_pairing_flush(match_id, occurred_at=when)
    assert result.is_ok()
    return result.unwrap()


def test_fresh_mapper_roundtrips_readiness_facts(setup):
    match_id, tournament_id = setup.match.id, setup.tournament.id
    assert _refresh(match_id)
    db.session.commit()
    pairing_id = repo.get_match_pairing(match_id).id
    before = repo.find_match(match_id)
    old_tournament = repo.get_tournament(tournament_id)
    # Hold strong ORM references: SQLAlchemy identity maps are weak-reference maps.
    cached_match = db.session.get(DbTournamentMatch, match_id)
    cached_tournament = db.session.get(DbTournament, tournament_id)
    facts = dict(pairing_generation=2**40, readiness_revision=2**41,
                 pairing_id=pairing_id, invitation_hold_a=True, invitation_hold_b=True,
                 ready_at_a=NOW, ready_at_b=NOW, ready_by_a=setup.users[0].id,
                 ready_by_b=setup.users[1].id, both_ready_notified_at=NOW)
    with Session(db.engine) as other:
        other.execute(update(DbTournamentMatch).where(DbTournamentMatch.id == match_id).values(**facts))
        other.execute(update(DbTournament).where(DbTournament.id == tournament_id).values(tournament_status='PAUSED'))
        other.commit()
    assert repo.find_match(match_id) == before
    assert repo.get_tournament(tournament_id) == old_tournament
    locked_tournament = repo.get_tournament_for_update(tournament_id)
    assert locked_tournament.tournament_status == TournamentStatus.PAUSED
    locked = repo.get_match_for_update(match_id)
    for key, value in facts.items():
        assert getattr(locked, key) == value
    assert cached_match.readiness_revision == 2**41
    assert cached_tournament.tournament_status == 'PAUSED'


def test_pair_order_is_deterministic(setup):
    match_id = setup.match.id
    assert _refresh(match_id)
    first = repo.get_match_pairing(match_id)
    ordered = sorted(setup.contestants, key=lambda row: row.id)
    assert first.side_a.id == ordered[0].participant_id
    assert first.side_b.id == ordered[1].participant_id
    assert first.generation == 1 and first.started_at == NOW
    assert repo.get_match_pairings_for_matches([match_id]) == {match_id: first}
    repo.set_side_ready_flush(match_id, MatchSide.A, NOW, setup.users[0].id)
    repo.set_side_invitation_hold_flush(match_id, MatchSide.B, True)
    token = uuid7()
    work = DbMatchInvitation(
        uuid7(), match_id, setup.tournament.id, first.generation, setup.users[0].id,
        'queued', 1, dispatch_token=token, lease_until=NOW + timedelta(hours=1),
    )
    db.session.add(work)
    db.session.flush()
    before = repo.find_match_fresh(match_id)
    # Reassociate/reorder rows while both logical identities stay assigned.
    # Explicit deletion is a different contract: it retires the pairing.
    for index, row in enumerate(reversed(ordered)):
        row.id = uuid7()
        row.created_at = NOW + timedelta(seconds=index)
    db.session.flush()
    assert [row.participant_id for row in repo.get_contestants_for_match(match_id)] == [
        first.side_b.id, first.side_a.id,
    ]
    assert _refresh(match_id, NOW + timedelta(minutes=1)) is False
    assert _refresh(match_id, NOW + timedelta(minutes=1)) is False
    assert repo.get_match_pairing(match_id) == first
    assert repo.get_match_pairing_history(match_id) == [first]
    match = repo.find_match_fresh(match_id)
    assert match == before
    assert match.ready_at_a == NOW and match.invitation_hold_b is True
    db.session.refresh(work)
    assert work.status == 'queued' and work.dispatch_token == token
    assert work.lease_until == NOW + timedelta(hours=1)
    # Replace one logical opponent without an intervening deletion.
    for row in ordered:
        if row.participant_id == setup.participants[1].id:
            row.participant_id = setup.participants[2].id
    db.session.flush()
    assert _refresh(match_id, NOW + timedelta(minutes=2))
    replacement = repo.get_match_pairing(match_id)
    assert replacement.id != first.id and replacement.generation == 2
    assert {replacement.side_a.id, replacement.side_b.id} == {setup.participants[0].id, setup.participants[2].id}
    match = repo.find_match_fresh(match_id)
    assert match.readiness_revision == 2
    assert match.ready_at_a is match.ready_at_b is match.ready_by_a is match.ready_by_b is None
    assert match.invitation_hold_a is match.invitation_hold_b is False
    assert match.occupied_since == NOW
    history = repo.get_match_pairing_history(match_id)
    assert len(history) == 2 and history[0].id == first.id
    assert history[0].ended_at == NOW + timedelta(minutes=2)
    assert history[1] == replacement
    db.session.refresh(work)
    assert work.status == 'suppressed'
    assert work.dispatch_token is work.lease_until is None


def test_explicit_delete_retires_pairing_before_same_identities_are_reseated(setup):
    match_id, tournament_id = setup.match.id, setup.tournament.id
    assert _refresh(match_id)
    first = repo.get_match_pairing(match_id)
    repo.set_side_ready_flush(match_id, MatchSide.A, NOW, setup.users[0].id)
    repo.set_side_ready_flush(match_id, MatchSide.B, NOW, setup.users[1].id)
    repo.set_side_invitation_hold_flush(match_id, MatchSide.B, True)
    work = DbMatchInvitation(
        uuid7(), match_id, tournament_id, first.generation, setup.users[0].id,
        'queued', 1, dispatch_token=uuid7(), lease_until=NOW + timedelta(hours=1),
        next_attempt_at=NOW + timedelta(minutes=1),
    )
    db.session.add(work)
    db.session.commit()
    work_id = work.id
    before = repo.find_match_fresh(match_id)
    with patch.object(db.session, 'commit', wraps=db.session.commit) as commit:
        repo.delete_contestants_for_match_flush(match_id)
        retired = repo.find_match_fresh(match_id)
        assert retired.pairing_id is None
        assert retired.pairing_generation == before.pairing_generation + 1
        assert retired.readiness_revision == before.readiness_revision + 1
        assert retired.ready_at_a is retired.ready_at_b is retired.ready_by_a is retired.ready_by_b is None
        assert retired.invitation_hold_a is retired.invitation_hold_b is False
        assert retired.occupied_since == NOW
        history = repo.get_match_pairing_history(match_id)
        assert len(history) == 1 and history[0].id == first.id
        assert history[0].ended_at is not None
        db.session.refresh(work)
        assert work.status == 'suppressed' and work.last_error == 'pairing_retired'
        assert work.dispatch_token is work.lease_until is work.next_attempt_at is None
        assert repo.get_contestants_for_match(match_id) == []
        assert _refresh(match_id, history[0].ended_at) is False
        # Another session sees none of the uncommitted deletion/cleanup.
        with Session(db.engine) as other:
            persisted = other.get(DbTournamentMatch, match_id)
            assert persisted.pairing_id == first.id
            assert persisted.ready_at_a == persisted.ready_at_b == NOW
            assert other.get(DbMatchInvitation, work_id).status == 'queued'
        for identity in (first.side_b, first.side_a):
            db.session.add(DbTournamentMatchToContestant(
                uuid7(), match_id, NOW, participant_id=identity.id,
            ))
        db.session.flush()
        assert _refresh(match_id, history[0].ended_at + timedelta(seconds=1))
        replacement = repo.get_match_pairing(match_id)
        assert replacement.id != first.id and replacement.generation == 3
        assert {replacement.side_a, replacement.side_b} == {first.side_a, first.side_b}
        assert len(repo.get_match_pairing_history(match_id)) == 2
        reseated = repo.find_match_fresh(match_id)
        assert reseated.readiness_revision == before.readiness_revision + 2
        assert reseated.ready_at_a is reseated.ready_at_b is reseated.ready_by_a is reseated.ready_by_b is None
        assert reseated.invitation_hold_a is reseated.invitation_hold_b is False
        assert reseated.occupied_since == NOW
        db.session.refresh(work)
        assert work.status == 'suppressed' and work.dispatch_token is None
        commit.assert_not_called()
    db.session.rollback()
    assert repo.find_match_fresh(match_id) == before
    assert repo.get_match_pairing_history(match_id) == [first]
    assert len(repo.get_contestants_for_match(match_id)) == 2
    db.session.refresh(work)
    assert work.status == 'queued' and work.dispatch_token is not None


def test_pair_history_survives_regeneration(setup):
    match_id = setup.match.id
    assert _refresh(match_id)
    original = repo.get_match_pairing(match_id)
    with patch.object(repo, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = (NOW + timedelta(minutes=1)).replace(tzinfo=UTC)
        repo.delete_contestants_for_match_flush(match_id)
    assert repo.get_match_pairing(match_id) is None
    assert repo.get_match_pairing_history(match_id)[0].ended_at == NOW + timedelta(minutes=1)
    assert _refresh(match_id, NOW + timedelta(minutes=1)) is False
    for participant in setup.participants[1:]:
        db.session.add(DbTournamentMatchToContestant(uuid7(), match_id, NOW, participant_id=participant.id))
    db.session.flush()
    assert _refresh(match_id, NOW + timedelta(minutes=2))
    assert repo.find_match_fresh(match_id).occupied_since == NOW
    history = repo.get_match_pairing_history(match_id)
    assert len(history) == 2
    assert history[0].id == original.id and history[0].started_at == NOW
    assert history[0].ended_at == NOW + timedelta(minutes=1)
    assert history[1].started_at == NOW + timedelta(minutes=2)
    with patch.object(repo, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = (NOW + timedelta(minutes=3)).replace(tzinfo=UTC)
        repo.delete_contestants_for_match_flush(match_id)
    assert repo.get_match_pairing(match_id) is None
    assert _refresh(match_id, NOW + timedelta(minutes=3)) is False
    repo.delete_match_flush(match_id)
    repo.delete_participants_for_tournament_flush(setup.tournament.id)
    db.session.execute(delete(DbTournamentLogEntry).where(DbTournamentLogEntry.tournament_id == setup.tournament.id))
    db.session.execute(delete(DbTournament).where(DbTournament.id == setup.tournament.id))
    db.session.commit()
    retained = repo.get_match_pairing_history(match_id)
    assert retained[0] == history[0]
    assert retained[1].side_a == history[1].side_a and retained[1].side_b == history[1].side_b
    assert retained[1].started_at == history[1].started_at
    assert retained[1].ended_at == NOW + timedelta(minutes=3)
    assert repo.get_match_pairing(match_id) is None


# fmt: off
@pytest.mark.parametrize('game_format,phase,playoff_format,expected', [
    ('FREE_FOR_ALL', 1, None, False), ('HIGHSCORE', 1, None, False),
    ('ONE_V_ONE', 2, 'ONE_V_ONE', True), ('HIGHSCORE', 2, 'FREE_FOR_ALL', False),
    ('ONE_V_ONE', 2, None, False),
])
# fmt: on
def test_pair_creation_respects_effective_phase_format(setup, game_format, phase, playoff_format, expected):
    setup.tournament.game_format = game_format
    setup.tournament.playoff_game_format = playoff_format
    if playoff_format is not None:
        setup.tournament.playoff_elimination_mode = 'SINGLE_ELIMINATION'
        setup.tournament.playoff_release_mode = 'MANUAL'
        if game_format == 'ONE_V_ONE':
            setup.tournament.elimination_mode = 'ROUND_ROBIN'
            setup.tournament.playoff_group_count = 2
            setup.tournament.playoff_qualifiers_per_group = 1
        else:
            setup.tournament.playoff_qualifier_count = 2
    setup.match.phase = phase
    db.session.flush()
    assert _refresh(setup.match.id) is expected
    assert (repo.get_match_pairing(setup.match.id) is not None) is expected


def test_removed_or_duplicate_opponents_do_not_create_pair(setup):
    setup.participants[0].removed_at = NOW
    db.session.flush()
    assert _refresh(setup.match.id) is False
    setup.participants[0].removed_at = None
    for row in setup.contestants:
        row.participant_id = setup.participants[0].id
    db.session.flush()
    assert _refresh(setup.match.id) is False


def test_flush_storage_can_be_rolled_back_without_internal_commits(setup):
    match_id = setup.match.id
    with patch.object(db.session, 'commit', wraps=db.session.commit) as commit:
        assert _refresh(match_id)
        repo.set_side_ready_flush(match_id, MatchSide.A, NOW, setup.users[0].id)
        repo.set_side_invitation_hold_flush(match_id, MatchSide.B, True)
        repo.clear_match_readiness_flush(match_id, increment_revision=True)
        repo.update_participant_flush(repo.get_participant(setup.participants[0].id))
        repo.delete_contestants_for_tournament_flush(setup.tournament.id)
        repo.delete_match_flush(match_id)
        repo.delete_participants_for_tournament_flush(setup.tournament.id)
        commit.assert_not_called()
    db.session.rollback()
    assert repo.get_match(match_id).pairing_generation == 0
    assert repo.get_match_pairing_history(match_id) == []
    assert len(repo.get_contestants_for_match(match_id)) == 2


def test_team_pairing_membership_freshness_and_flush_compatibility(setup):
    tournament_id, match_id = setup.tournament.id, setup.match.id
    teams = [DbTournamentTeam(uuid7(), tournament_id, f'Team {uuid7()}', user.id, NOW)
             for user in setup.users[:2]]
    db.session.add_all(teams)
    db.session.flush()
    repo.delete_contestants_for_match_flush(match_id)
    for team in teams:
        db.session.add(DbTournamentMatchToContestant(uuid7(), match_id, NOW, team_id=team.id))
    db.session.flush()
    assert _refresh(match_id)
    pairing = repo.get_match_pairing(match_id)
    assert pairing.side_a.kind == pairing.side_b.kind == 'team'
    assert {pairing.side_a.id, pairing.side_b.id} == {team.id for team in teams}
    db.session.commit()
    team_id, participant_id = teams[0].id, setup.participants[0].id
    cached_team = db.session.get(DbTournamentTeam, team_id)
    cached_participant = db.session.get(DbTournamentParticipant, participant_id)
    old_team = repo.get_team(team_id)
    old_participant = repo.get_participant(participant_id)
    with Session(db.engine) as other:
        other.execute(update(DbTournamentTeam).where(DbTournamentTeam.id == team_id)
                      .values(captain_user_id=setup.users[2].id))
        other.execute(update(DbTournamentParticipant).where(DbTournamentParticipant.id == participant_id)
                      .values(team_id=team_id, substitute_player=True))
        other.commit()
    assert repo.get_team(team_id) == old_team
    assert repo.get_participant(participant_id) == old_participant
    repo.get_tournament_for_update(tournament_id)
    fresh_team = repo.get_team_for_update(team_id)
    fresh_participant = repo.find_participant_fresh(participant_id)
    assert fresh_team.captain_user_id == setup.users[2].id
    assert fresh_participant.team_id == team_id and fresh_participant.substitute_player
    assert repo.get_participant_for_update(participant_id) == fresh_participant
    assert cached_team.captain_user_id == setup.users[2].id
    assert cached_participant.team_id == team_id
    with patch.object(db.session, 'commit', wraps=db.session.commit) as commit:
        repo.update_team_captain_flush(team_id, setup.users[0].id)
        repo.remove_team_from_participants_flush(team_id)
        assert repo.find_participant_fresh(participant_id).team_id is None
        repo.remove_team_from_contestants_flush(team_id)
        assert repo.get_match_pairing(match_id) is None
        assert repo.get_match_pairing_history(match_id)[0].ended_at is not None
        assert _refresh(match_id, NOW + timedelta(minutes=1)) is False
        repo.delete_contestants_for_tournament_flush(tournament_id)
        repo.delete_teams_for_tournament_flush(tournament_id)
        commit.assert_not_called()
    db.session.rollback()
    assert repo.get_team(team_id).captain_user_id == setup.users[2].id
    assert repo.get_match_pairing(match_id) == pairing


def test_pair_time_rejection_leaves_snapshot_unchanged(setup):
    match_id = setup.match.id
    assert _refresh(match_id)
    old = repo.get_match_pairing(match_id)
    repo.set_side_ready_flush(match_id, MatchSide.A, NOW, setup.users[0].id)
    repo.set_side_invitation_hold_flush(match_id, MatchSide.B, True)
    token = uuid7()
    work = DbMatchInvitation(
        uuid7(), match_id, setup.tournament.id, old.generation, setup.users[0].id,
        'queued', 1, dispatch_token=token, lease_until=NOW + timedelta(hours=1),
    )
    db.session.add(work)
    db.session.flush()
    before = repo.find_match_fresh(match_id)
    # A logical replacement still has an active pairing to reject an old time.
    # Explicit deletion would already have retired it at the deletion boundary.
    setup.contestants[0].participant_id = setup.participants[2].id
    db.session.flush()
    result = repo.refresh_match_pairing_flush(match_id, occurred_at=NOW - timedelta(seconds=1))
    assert result.is_err() and result.unwrap_err() == 'pairing_time_before_start'
    assert repo.get_match_pairing(match_id) == old
    assert repo.find_match_fresh(match_id) == before
    assert repo.get_match_pairing_history(match_id) == [old]
    db.session.refresh(work)
    assert work.status == 'queued' and work.dispatch_token == token
    assert work.lease_until == NOW + timedelta(hours=1)
