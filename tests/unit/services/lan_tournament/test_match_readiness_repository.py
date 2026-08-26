"""Pure/mocked repository contracts; PostgreSQL behavior has separate proof."""

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.models.tournament_match import MatchSide, TournamentMatch
from byceps.services.lan_tournament.models.tournament_participant import TournamentParticipant

from tests.helpers import generate_uuid


@pytest.fixture
def session():
    session = MagicMock()
    with patch.object(repo.db, 'session', session):
        yield session


def test_membership_helpers_flush_without_committing(session):
    participant = TournamentParticipant(
        id=generate_uuid(), user_id=generate_uuid(), tournament_id=generate_uuid(),
        created_at=datetime(2026, 10, 5), substitute_player=False, team_id=None,
    )
    row = SimpleNamespace(team_id=None, substitute_player=False)
    session.get.return_value = row
    updated = replace(participant, team_id=generate_uuid(), substitute_player=True)
    repo.update_participant_flush(updated)
    assert row.team_id == updated.team_id
    assert row.substitute_player is True
    captain = generate_uuid()
    repo.update_team_captain_flush(generate_uuid(), captain)
    assert row.captain_user_id == captain
    repo.remove_team_from_participants_flush(generate_uuid())
    assert session.flush.call_count == 3
    session.commit.assert_not_called()


def test_legacy_membership_wrappers_delegate_and_commit_once(session):
    with patch.object(repo, 'update_participant_flush') as update:
        participant = object()
        repo.update_participant(participant)
        update.assert_called_once_with(participant)
    session.commit.assert_called_once_with()
    session.commit.reset_mock()
    team_id, captain = generate_uuid(), generate_uuid()
    with patch.object(repo, 'update_team_captain_flush') as update:
        repo.update_team_captain(team_id, captain)
        update.assert_called_once_with(team_id, captain)
    session.commit.assert_called_once_with()


# fmt: off
@pytest.mark.parametrize('name', [
    'delete_teams_for_tournament_flush', 'delete_participants_for_tournament_flush',
    'delete_contestants_for_tournament_flush', 'remove_team_from_contestants_flush',
    'remove_team_from_participants_flush', 'delete_match_contestant_flush',
    'delete_team_flush',
])
# fmt: on
def test_delete_helpers_are_flush_only(session, name):
    getattr(repo, name)(generate_uuid())
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


# fmt: off
@pytest.mark.parametrize('name', [
    'delete_contestants_for_match_flush', 'delete_match_flush',
])
# fmt: on
def test_live_delete_helpers_flush_readiness_cleanup_without_committing(session, name):
    match = TournamentMatch(
        id=generate_uuid(), tournament_id=generate_uuid(), created_at=datetime(2026, 10, 5),
        group_order=None, match_order=0, round=0, next_match_id=None, confirmed_by=None,
    )
    row = SimpleNamespace(pairing_id=None, ready_at_a=None, ready_at_b=None,
                          invitation_hold_a=False, invitation_hold_b=False)
    session.get.return_value = row
    with patch.object(repo, 'find_match', return_value=match) as read, \
         patch.object(repo, 'lock_tournament_for_update') as lock, \
         patch.object(repo, 'get_match_for_update', return_value=match) as fresh:
        getattr(repo, name)(match.id)
    read.assert_called_once_with(match.id)
    lock.assert_called_once_with(match.tournament_id)
    fresh.assert_called_once_with(match.id)
    # Work suppression, readiness cleanup, and the owning delete each flush.
    assert session.flush.call_count == 3
    assert session.execute.call_count == 2
    suppression = str(session.execute.call_args_list[0].args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True},
    ))
    assert 'lan_tournament_match_invitations' in suppression
    assert "status='suppressed'" in suppression
    assert row.ready_at_a is row.ready_at_b is row.ready_by_a is row.ready_by_b is None
    session.commit.assert_not_called()


# fmt: off
@pytest.mark.parametrize('name', [
    'delete_teams_for_tournament', 'delete_participants_for_tournament',
    'delete_contestants_for_tournament',
])
@pytest.mark.parametrize('commit', [False, True])
# fmt: on
def test_legacy_bulk_delete_keeps_commit_compatibility(session, name, commit):
    tournament_id = generate_uuid()
    with patch.object(repo, f'{name}_flush') as flush:
        getattr(repo, name)(tournament_id, commit=commit)
        flush.assert_called_once_with(tournament_id)
    assert session.commit.call_count == int(commit)


# fmt: off
@pytest.mark.parametrize('name', [
    'remove_team_from_contestants', 'remove_team_from_participants',
    'delete_match_contestant',
])
# fmt: on
def test_legacy_remove_wrappers_delegate_and_commit_once(session, name):
    identity = generate_uuid()
    with patch.object(repo, f'{name}_flush') as flush:
        getattr(repo, name)(identity)
        flush.assert_called_once_with(identity)
    session.commit.assert_called_once_with()


def test_readiness_storage_helpers_preserve_occupancy_and_flush(session):
    occupied = datetime(2026, 10, 1)
    row = SimpleNamespace(occupied_since=occupied, readiness_revision=5, pairing_generation=7)
    session.get.return_value = row
    match_id, actor = generate_uuid(), generate_uuid()
    repo.set_side_ready_flush(match_id, MatchSide.A, occupied, actor)
    assert row.ready_at_a == occupied and row.ready_by_a == actor
    repo.set_side_ready_flush(match_id, MatchSide.B, occupied, actor)
    repo.set_side_invitation_hold_flush(match_id, MatchSide.A, True)
    repo.set_side_invitation_hold_flush(match_id, MatchSide.B, True)
    repo.clear_match_readiness_flush(match_id, increment_revision=True)
    assert row.ready_at_a is row.ready_at_b is None
    assert row.ready_by_a is row.ready_by_b is None
    assert row.invitation_hold_a is row.invitation_hold_b is False
    assert row.occupied_since == occupied
    assert row.readiness_revision == 6 and row.pairing_generation == 7
    repo.clear_match_readiness_flush(match_id, increment_revision=False)
    assert row.readiness_revision == 6
    repo.set_readiness_revision_flush(match_id, 2**40)
    assert row.readiness_revision == 2**40
    session.commit.assert_not_called()
    assert session.flush.call_count == 7


def test_invalid_side_and_revision_are_rejected_before_write(session):
    for writer, args in (
        (repo.set_side_ready_flush, (generate_uuid(), 'invalid', datetime.now(), generate_uuid())),
        (repo.clear_side_ready_flush, (generate_uuid(), 'invalid')),
        (repo.set_side_invitation_hold_flush, (generate_uuid(), 'invalid', True)),
        (repo.set_readiness_revision_flush, (generate_uuid(), -1)),
    ):
        with pytest.raises(ValueError):
            writer(*args)
    session.get.assert_not_called()
    session.flush.assert_not_called()


def test_pair_reads_empty_batch_avoids_query(session):
    assert repo.get_match_pairings_for_matches([]) == {}
    session.scalars.assert_not_called()


def test_missing_match_refresh_returns_error_without_writes(session):
    session.get.return_value = None
    result = repo.refresh_match_pairing_flush(generate_uuid(), occurred_at=datetime.now())
    assert result.is_err() and result.unwrap_err() == 'match_not_found'
    session.flush.assert_not_called()
    session.commit.assert_not_called()


def test_fresh_participant_read_requests_reload(session):
    session.get.return_value = None
    participant_id = generate_uuid()
    assert repo.find_participant_fresh(participant_id) is None
    session.get.assert_called_once_with(repo.DbTournamentParticipant, participant_id, populate_existing=True)
