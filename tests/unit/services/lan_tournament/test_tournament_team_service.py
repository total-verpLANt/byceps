from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from byceps.services.lan_tournament import tournament_team_service
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')

MOCK_PREFIX = 'byceps.services.lan_tournament.tournament_team_service'


# -------------------------------------------------------------------- #
# create_team — tag normalization
# -------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('input_tag', 'expected_tag'),
    [
        ('abc', 'ABC'),  # lowercase → uppercase
        ('ABC', 'ABC'),  # already uppercase stays
        ('AbC', 'ABC'),  # mixed case → uppercase
        ('', None),  # empty string → None
        (None, None),  # None stays None
    ],
)
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_normalizes_tag(
    mock_domain,
    mock_repo,
    mock_signals,
    input_tag,
    expected_tag,
):
    """Tag is uppercased; empty string and None both become None."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None
    captain = _create_participant(user_id=captain_id)
    mock_repo.find_participant_by_user.return_value = captain
    mock_repo.get_participant_for_update.return_value = captain

    result = tournament_team_service.create_team(
        TOURNAMENT_ID,
        'Team Alpha',
        captain_id,
        tag=input_tag,
    )

    assert result.is_ok()
    team, _event = result.unwrap()
    assert team.tag == expected_tag
    mock_repo.get_participant_for_update.assert_called_once_with(captain.id)
    updated_captain = mock_repo.update_participant_flush.call_args.args[0]
    assert updated_captain.id == captain.id
    assert updated_captain.team_id == team.id
    assert captain.team_id is None
    mock_repo.update_participant_flush.assert_called_once()
    mock_repo.update_participant.assert_not_called()
    mock_repo.commit_session.assert_called_once_with()


@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_empty_tag_skips_duplicate_check(
    mock_domain,
    mock_repo,
    mock_signals,
):
    """Empty-string tag normalized to None skips the tag
    duplicate check entirely."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = None
    captain = _create_participant(user_id=captain_id)
    mock_repo.find_participant_by_user.return_value = captain
    mock_repo.get_participant_for_update.return_value = captain

    result = tournament_team_service.create_team(
        TOURNAMENT_ID,
        'Team Alpha',
        captain_id,
        tag='',
    )

    assert result.is_ok()
    mock_repo.find_active_team_by_tag.assert_not_called()


# -------------------------------------------------------------------- #
# update_team — tag normalization
# -------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('input_tag', 'expected_tag'),
    [
        ('xyz', 'XYZ'),  # lowercase → uppercase
        ('XYZ', 'XYZ'),  # already uppercase stays
        ('', None),  # empty string → None
        (None, None),  # None stays None
    ],
)
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_normalizes_tag(
    mock_repo,
    input_tag,
    expected_tag,
):
    """Tag is uppercased; empty string and None both become None."""
    team = _create_team(tag='OLD')
    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag=input_tag,
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.tag == expected_tag


# -------------------------------------------------------------------- #
# update_team — lock_tournament_for_update
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_calls_lock_not_get(mock_repo):
    """update_team uses lock_tournament_for_update (not
    get_tournament_for_update) since the tournament object
    is not needed."""
    team = _create_team()
    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag=team.tag,
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_ok()
    mock_repo.lock_tournament_for_update.assert_called_once_with(
        team.tournament_id
    )
    mock_repo.get_tournament_for_update.assert_not_called()


# -------------------------------------------------------------------- #
# update_team — no redundant .upper() on already-normalized tag
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_unchanged_uppercase_tag_skips_duplicate_check(
    mock_repo,
):
    """When the tag is unchanged (already uppercase), the duplicate
    tag check is skipped."""
    team = _create_team(tag='ABC')
    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag='abc',  # lowercase input, normalized to 'ABC'
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_ok()
    # Tag normalized to 'ABC' matches existing 'ABC' → skip check
    mock_repo.find_active_team_by_tag.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_changed_tag_triggers_duplicate_check(
    mock_repo,
):
    """When the tag changes, the duplicate tag check runs."""
    team = _create_team(tag='OLD')
    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag='new',  # normalized to 'NEW', differs from 'OLD'
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_ok()
    mock_repo.find_active_team_by_tag.assert_called_once()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_none_tag_skips_duplicate_check(mock_repo):
    """When tag is None, the duplicate tag check is skipped."""
    team = _create_team(tag='OLD')
    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag=None,
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_ok()
    mock_repo.find_active_team_by_tag.assert_not_called()


# -------------------------------------------------------------------- #
# create_team — duplicate detection error paths
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_duplicate_name_returns_err(
    mock_domain,
    mock_repo,
    mock_signals,
):
    """Creating a team with a duplicate name returns an Err."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())
    existing_team = _create_team(name='Team Alpha')

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = existing_team

    result = tournament_team_service.create_team(
        TOURNAMENT_ID,
        'Team Alpha',
        captain_id,
    )

    assert result.is_err()
    assert 'name already exists' in result.unwrap_err()


@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_duplicate_tag_returns_err(
    mock_domain,
    mock_repo,
    mock_signals,
):
    """Creating a team with a duplicate tag returns an Err."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())
    existing_team = _create_team(tag='ABC')

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = existing_team

    result = tournament_team_service.create_team(
        TOURNAMENT_ID,
        'Team Beta',
        captain_id,
        tag='ABC',
    )

    assert result.is_err()
    assert 'tag already exists' in result.unwrap_err()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_integrity_error_returns_err(
    mock_domain,
    mock_repo,
    mock_signals,
    mock_db,
):
    """IntegrityError from DB constraint returns an Err."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None
    captain = _create_participant(user_id=captain_id)
    mock_repo.find_participant_by_user.return_value = captain
    mock_repo.get_participant_for_update.return_value = captain

    orig = Mock(
        constraint_name='uq_lan_tournament_teams_active_name_ci',
    )
    mock_repo.create_team.side_effect = IntegrityError('', {}, orig)

    result = tournament_team_service.create_team(
        TOURNAMENT_ID,
        'Team Alpha',
        captain_id,
    )

    assert result.is_err()
    assert 'name already exists' in result.unwrap_err()
    mock_db.session.rollback.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_integrity_error_tag_returns_err(
    mock_domain,
    mock_repo,
    mock_signals,
    mock_db,
):
    """IntegrityError from tag DB constraint returns an Err."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None
    captain = _create_participant(user_id=captain_id)
    mock_repo.find_participant_by_user.return_value = captain
    mock_repo.get_participant_for_update.return_value = captain

    orig = Mock(
        constraint_name='uq_lan_tournament_teams_active_tag_ci',
    )
    mock_repo.create_team.side_effect = IntegrityError('', {}, orig)

    result = tournament_team_service.create_team(
        TOURNAMENT_ID,
        'Team Alpha',
        captain_id,
        tag='ABC',
    )

    assert result.is_err()
    assert 'tag already exists' in result.unwrap_err()
    mock_db.session.rollback.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
@patch(f'{MOCK_PREFIX}.tournament_domain_service')
def test_create_team_integrity_error_unknown_constraint_reraises(
    mock_domain,
    mock_repo,
    mock_signals,
    mock_db,
):
    """IntegrityError from an unknown constraint re-raises."""
    tournament = _create_tournament()
    captain_id = UserID(generate_uuid())

    mock_repo.get_tournament_for_update.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []
    mock_domain.validate_team_count.return_value = Ok(None)
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None
    captain = _create_participant(user_id=captain_id)
    mock_repo.find_participant_by_user.return_value = captain
    mock_repo.get_participant_for_update.return_value = captain

    orig = Mock(constraint_name='some_other_constraint')
    mock_repo.create_team.side_effect = IntegrityError('', {}, orig)

    with pytest.raises(IntegrityError):
        tournament_team_service.create_team(
            TOURNAMENT_ID,
            'Team Alpha',
            captain_id,
        )
    mock_db.session.rollback.assert_called_once_with()
    mock_repo.rollback_session.assert_called_once_with()
    mock_repo.commit_session.assert_not_called()


# -------------------------------------------------------------------- #
# update_team — duplicate detection error paths
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_duplicate_name_returns_err(mock_repo):
    """Updating a team with a duplicate name returns an Err."""
    team = _create_team(name='Original Name')
    other_team = _create_team(name='Taken Name')

    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = other_team

    result = tournament_team_service.update_team(
        team.id,
        name='Taken Name',
        tag=team.tag,
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_err()
    assert 'name already exists' in result.unwrap_err()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_duplicate_tag_returns_err(mock_repo):
    """Updating a team with a duplicate tag returns an Err."""
    team = _create_team(tag='OLD')
    other_team = _create_team(tag='TAKEN')

    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = other_team

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag='TAKEN',
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_err()
    assert 'tag already exists' in result.unwrap_err()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_integrity_error_returns_err(mock_repo, mock_db):
    """IntegrityError from DB constraint returns an Err."""
    team = _create_team()

    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    orig = Mock(
        constraint_name='uq_lan_tournament_teams_active_tag_ci',
    )
    mock_repo.update_team.side_effect = IntegrityError('', {}, orig)

    result = tournament_team_service.update_team(
        team.id,
        name=team.name,
        tag='NEW',
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_err()
    assert 'tag already exists' in result.unwrap_err()
    mock_db.session.rollback.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_integrity_error_name_returns_err(mock_repo, mock_db):
    """IntegrityError from name DB constraint returns an Err."""
    team = _create_team()

    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    orig = Mock(
        constraint_name='uq_lan_tournament_teams_active_name_ci',
    )
    mock_repo.update_team.side_effect = IntegrityError('', {}, orig)

    result = tournament_team_service.update_team(
        team.id,
        name='New Name',
        tag=team.tag,
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_err()
    assert 'name already exists' in result.unwrap_err()
    mock_db.session.rollback.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_integrity_error_unknown_constraint_reraises(
    mock_repo, mock_db
):
    """IntegrityError from an unknown constraint re-raises."""
    team = _create_team()

    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_name.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    orig = Mock(constraint_name='some_other_constraint')
    mock_repo.update_team.side_effect = IntegrityError('', {}, orig)

    with pytest.raises(IntegrityError):
        tournament_team_service.update_team(
            team.id,
            name='New Name',
            tag='NEW',
            description=None,
            image_url=None,
            join_code=None,
        )


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_unchanged_name_skips_name_duplicate_check(
    mock_repo,
):
    """When the name is unchanged, the duplicate name check is
    skipped."""
    team = _create_team(name='Same Name')
    mock_repo.get_team.return_value = team
    mock_repo.lock_tournament_for_update.return_value = None
    mock_repo.find_active_team_by_tag.return_value = None

    result = tournament_team_service.update_team(
        team.id,
        name='Same Name',
        tag=team.tag,
        description=None,
        image_url=None,
        join_code=None,
    )

    assert result.is_ok()
    mock_repo.find_active_team_by_name.assert_not_called()


# -------------------------------------------------------------------- #
# acting captain (site captain routes)
# -------------------------------------------------------------------- #

NOT_CAPTAIN = 'Only the team captain can update this team.'


def _roster_world(mock_repo):
    captain_user_id = UserID(generate_uuid())
    member = _create_participant()
    other = _create_participant()
    team = _create_team(captain_user_id=captain_user_id)
    mock_repo.find_team.return_value = team
    mock_repo.get_team_for_update.return_value = team
    mock_repo.get_participants_for_team.return_value = [member, other]
    return team, captain_user_id, member, other


def _call(operation, team, other, **kwargs):
    if operation == 'transfer':
        return tournament_team_service.transfer_captain(
            team.id, other.user_id, **kwargs
        )
    return tournament_team_service.remove_team_member(
        team.id, other.user_id, **kwargs
    )


@pytest.mark.parametrize('operation', ['transfer', 'remove'])
@patch(f'{MOCK_PREFIX}._lock_roster_matches_flush', return_value=[])
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_acting_captain_without_a_roster_row_is_refused(
    mock_repo, mock_signals, mock_lock, operation
):
    """The captaincy alone is not enough: the captain must still be a member."""
    team, captain_user_id, _member, other = _roster_world(mock_repo)

    result = _call(operation, team, other, acting_captain_id=captain_user_id)

    assert result.unwrap_err() == NOT_CAPTAIN
    mock_repo.update_team_captain_flush.assert_not_called()
    mock_repo.update_participant_flush.assert_not_called()
    mock_repo.commit_session.assert_not_called()
    mock_repo.rollback_session.assert_called_once_with()


@pytest.mark.parametrize('operation', ['transfer', 'remove'])
@patch(f'{MOCK_PREFIX}._lock_roster_matches_flush', return_value=[])
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_acting_user_who_is_not_the_captain_is_refused(
    mock_repo, mock_signals, mock_lock, operation
):
    team, _captain_user_id, member, other = _roster_world(mock_repo)

    result = _call(operation, team, other, acting_captain_id=member.user_id)

    assert result.unwrap_err() == NOT_CAPTAIN
    mock_repo.update_team_captain_flush.assert_not_called()
    mock_repo.update_participant_flush.assert_not_called()


@patch(f'{MOCK_PREFIX}._refresh_roster_matches_flush')
@patch(f'{MOCK_PREFIX}._lock_roster_matches_flush', return_value=[])
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_a_sitting_captain_may_transfer(
    mock_repo, mock_signals, mock_lock, mock_refresh
):
    team, _captain_user_id, member, other = _roster_world(mock_repo)
    sitting = _create_participant(user_id=team.captain_user_id)
    mock_repo.get_participants_for_team.return_value = [sitting, member, other]
    mock_refresh.return_value = Ok(())

    result = tournament_team_service.transfer_captain(
        team.id, other.user_id, acting_captain_id=team.captain_user_id
    )

    assert result.is_ok()
    mock_repo.update_team_captain_flush.assert_called_once_with(
        team.id, other.user_id
    )


# -------------------------------------------------------------------- #
# update_team — the captain is re-checked after the tournament lock
# -------------------------------------------------------------------- #


def _update(team, **kwargs):
    return tournament_team_service.update_team(
        team.id,
        name='New name',
        tag=None,
        description=None,
        image_url=None,
        join_code=None,
        **kwargs,
    )


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_rereads_the_captain_under_the_lock(mock_repo):
    """A captain demoted while the call waited for the lock is refused."""
    old_captain = UserID(generate_uuid())
    stale = _create_team(captain_user_id=old_captain)
    fresh = _create_team(id=stale.id, captain_user_id=UserID(generate_uuid()))
    mock_repo.get_team.return_value = stale
    mock_repo.get_team_for_update.return_value = fresh
    mock_repo.get_participants_for_team.return_value = [
        _create_participant(user_id=old_captain, team_id=stale.id),
        _create_participant(user_id=fresh.captain_user_id, team_id=stale.id),
    ]
    mock_repo.find_active_team_by_name.return_value = None

    result = _update(stale, current_user_id=old_captain)

    assert result.unwrap_err() == NOT_CAPTAIN
    mock_repo.update_team.assert_not_called()
    mock_repo.rollback_session.assert_called_once_with()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_refuses_a_captain_without_a_roster_row(mock_repo):
    team = _create_team()
    mock_repo.get_team.return_value = team
    mock_repo.get_team_for_update.return_value = team
    mock_repo.get_participants_for_team.return_value = [_create_participant()]

    result = _update(team, current_user_id=team.captain_user_id)

    assert result.unwrap_err() == NOT_CAPTAIN
    mock_repo.update_team.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_accepts_a_sitting_captain(mock_repo):
    team = _create_team()
    mock_repo.get_team.return_value = team
    mock_repo.get_team_for_update.return_value = team
    mock_repo.get_participants_for_team.return_value = [
        _create_participant(user_id=team.captain_user_id, team_id=team.id)
    ]
    mock_repo.find_active_team_by_name.return_value = None

    result = _update(team, current_user_id=team.captain_user_id)

    assert result.is_ok()
    mock_repo.update_team.assert_called_once()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_update_team_admin_bypass_skips_the_captain_check(mock_repo):
    team = _create_team()
    mock_repo.get_team.return_value = team
    mock_repo.find_active_team_by_name.return_value = None

    result = _update(team)

    assert result.is_ok()
    mock_repo.get_team_for_update.assert_not_called()


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'updated_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': None,
        'game_format': None,
        'elimination_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _create_team(**kwargs) -> TournamentTeam:
    defaults = {
        'id': TournamentTeamID(generate_uuid()),
        'tournament_id': TOURNAMENT_ID,
        'name': 'Test Team',
        'tag': None,
        'description': None,
        'image_url': None,
        'captain_user_id': UserID(generate_uuid()),
        'join_code': None,
        'created_at': NOW,
    }
    defaults.update(kwargs)
    return TournamentTeam(**defaults)


def _create_participant(**kwargs) -> TournamentParticipant:
    defaults = {
        'id': TournamentParticipantID(generate_uuid()),
        'user_id': UserID(generate_uuid()),
        'tournament_id': TOURNAMENT_ID,
        'substitute_player': False,
        'team_id': None,
        'created_at': NOW,
        'removed_at': None,
    }
    defaults.update(kwargs)
    return TournamentParticipant(**defaults)
