"""
tests.unit.services.lan_tournament.test_tournament_orga_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from byceps.services.lan_tournament import tournament_orga_service
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrga,
    TournamentOrgaID,
)
from byceps.services.user.models import User, UserID

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())

MOCK_PREFIX = 'byceps.services.lan_tournament.tournament_orga_service'


def _create_orga(
    *,
    tournament_id=TOURNAMENT_ID,
    user_id=None,
    assigned_by_id=None,
    duties=None,
) -> TournamentOrga:
    return TournamentOrga(
        id=TournamentOrgaID(generate_uuid()),
        tournament_id=tournament_id,
        user_id=user_id if user_id is not None else UserID(generate_uuid()),
        assigned_at=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
        assigned_by_id=assigned_by_id,
        duties=duties,
    )


def _create_user(user_id, *, deleted=False) -> User:
    return User(
        id=user_id,
        screen_name='SomeUser',
        initialized=True,
        suspended=False,
        deleted=deleted,
        avatar_url='https://example.test/avatar.png',
    )


# -------------------------------------------------------------------- #
# assign_orga
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_persists_assignment(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())

    mock_repo.exists_orga_for_tournament_and_user.return_value = False

    result = tournament_orga_service.assign_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_ok()
    orga, event = result.unwrap()

    mock_repo.exists_orga_for_tournament_and_user.assert_called_once_with(
        TOURNAMENT_ID, user_id
    )
    mock_repo.create_orga.assert_called_once_with(orga)

    assert orga.tournament_id == TOURNAMENT_ID
    assert orga.user_id == user_id
    assert event.tournament_id == TOURNAMENT_ID
    assert event.user_id == user_id

    mock_signals.tournament_orga_assigned.send.assert_called_once_with(
        None, event=event
    )
    mock_db.session.commit.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_records_duties_and_initiator(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())

    mock_repo.exists_orga_for_tournament_and_user.return_value = False

    result = tournament_orga_service.assign_orga(
        TOURNAMENT_ID, user_id, initiator_id, duties='Bracket admin'
    )

    assert result.is_ok()
    orga, event = result.unwrap()

    assert orga.assigned_by_id == initiator_id
    assert orga.duties == 'Bracket admin'
    assert event.duties == 'Bracket admin'


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_twice_returns_err(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())

    mock_repo.exists_orga_for_tournament_and_user.return_value = True

    result = tournament_orga_service.assign_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'User is already an orga of this tournament.'
    )

    mock_repo.create_orga.assert_not_called()
    mock_log_service.create_log_entry.assert_not_called()
    mock_signals.tournament_orga_assigned.send.assert_not_called()
    mock_db.session.commit.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_writes_audit_entry(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())

    mock_repo.exists_orga_for_tournament_and_user.return_value = False

    result = tournament_orga_service.assign_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_ok()

    mock_log_service.create_log_entry.assert_called_once_with(
        'tournament-orga-assigned',
        TOURNAMENT_ID,
        initiator_id,
        data={'user_id': str(user_id)},
        commit=False,
    )


# -------------------------------------------------------------------- #
# revoke_orga
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_revoke_orga_removes_assignment(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())
    orga = _create_orga(user_id=user_id)

    mock_repo.find_orga_for_tournament_and_user.return_value = orga

    result = tournament_orga_service.revoke_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_ok()

    mock_repo.find_orga_for_tournament_and_user.assert_called_once_with(
        TOURNAMENT_ID, user_id
    )
    mock_repo.delete_orga.assert_called_once_with(orga.id)
    mock_signals.tournament_orga_revoked.send.assert_called_once()
    mock_db.session.commit.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_revoke_orga_when_absent_returns_err(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())

    mock_repo.find_orga_for_tournament_and_user.return_value = None

    result = tournament_orga_service.revoke_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_err()
    assert result.unwrap_err() == 'User is not an orga of this tournament.'

    mock_repo.delete_orga.assert_not_called()
    mock_log_service.create_log_entry.assert_not_called()
    mock_signals.tournament_orga_revoked.send.assert_not_called()
    mock_db.session.commit.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_revoke_orga_writes_audit_entry(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())
    orga = _create_orga(user_id=user_id)

    mock_repo.find_orga_for_tournament_and_user.return_value = orga

    result = tournament_orga_service.revoke_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_ok()

    mock_log_service.create_log_entry.assert_called_once_with(
        'tournament-orga-revoked',
        TOURNAMENT_ID,
        initiator_id,
        data={'user_id': str(user_id)},
        commit=False,
    )


# -------------------------------------------------------------------- #
# atomicity
# -------------------------------------------------------------------- #


@patch('byceps.database.db.session')
@patch(f'{MOCK_PREFIX}.signals')
def test_assign_orga_commits_once_covering_assignment_and_audit(
    mock_signals, mock_session
):
    """Commit the assignment and its audit entry together, once."""
    mock_session.scalar.return_value = False  # no pre-existing assignment

    user_id = UserID(generate_uuid())
    initiator_id = UserID(generate_uuid())

    result = tournament_orga_service.assign_orga(
        TOURNAMENT_ID, user_id, initiator_id
    )

    assert result.is_ok()

    assert mock_session.commit.call_count == 1

    call_names = [call[0] for call in mock_session.method_calls]
    assert call_names.count('add') == 2
    assert call_names.count('flush') == 2
    assert call_names.count('commit') == 1

    commit_index = call_names.index('commit')
    assert all(
        i < commit_index
        for i, name in enumerate(call_names)
        if name in ('add', 'flush')
    )

    mock_signals.tournament_orga_assigned.send.assert_called_once()


# -------------------------------------------------------------------- #
# get_public_orgas_for_tournament
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.user_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_public_orga_projection_excludes_deleted_users(
    mock_repo, mock_user_service, mock_db
):
    active_user_id = UserID(generate_uuid())
    deleted_user_id = UserID(generate_uuid())

    active_orga = _create_orga(user_id=active_user_id, duties='Streaming')
    deleted_orga = _create_orga(user_id=deleted_user_id, duties='Casting')

    mock_repo.get_orgas_for_tournament.return_value = [
        active_orga,
        deleted_orga,
    ]

    active_user = _create_user(active_user_id)
    deleted_user = _create_user(deleted_user_id, deleted=True)
    mock_user_service.get_users_indexed_by_id.return_value = {
        active_user_id: active_user,
        deleted_user_id: deleted_user,
    }

    result = tournament_orga_service.get_public_orgas_for_tournament(
        TOURNAMENT_ID
    )

    assert len(result) == 1
    assert result[0].user.id == active_user_id


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.user_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_public_orga_projection_carries_duties(
    mock_repo, mock_user_service, mock_db
):
    user_id = UserID(generate_uuid())
    orga = _create_orga(user_id=user_id, duties='Bracket admin')

    mock_repo.get_orgas_for_tournament.return_value = [orga]

    user = _create_user(user_id)
    mock_user_service.get_users_indexed_by_id.return_value = {
        user_id: user,
    }

    result = tournament_orga_service.get_public_orgas_for_tournament(
        TOURNAMENT_ID
    )

    assert len(result) == 1
    assert result[0].duties == 'Bracket admin'


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.user_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_public_orga_projection_carries_no_real_name(
    mock_repo, mock_user_service, mock_db
):
    """Keep an orga's real name off the public page."""
    user_id = UserID(generate_uuid())
    mock_repo.get_orgas_for_tournament.return_value = [
        _create_orga(user_id=user_id)
    ]
    mock_user_service.get_users_indexed_by_id.return_value = {
        user_id: _create_user(user_id),
    }

    result = tournament_orga_service.get_public_orgas_for_tournament(
        TOURNAMENT_ID
    )

    assert not hasattr(result[0], 'full_name')


@pytest.mark.parametrize(
    'constraint_name',
    [
        'uq_lan_tournament_orgas_tournament_user',
        'lan_tournament_orgas_tournament_id_user_id_key',
    ],
)
@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_duplicate_constraint_returns_err(
    mock_repo, mock_log_service, mock_signals, mock_db, constraint_name
):
    """Report a lost race on the pre-check as a duplicate."""
    mock_repo.exists_orga_for_tournament_and_user.return_value = False

    orig = Mock(constraint_name=constraint_name)
    mock_repo.create_orga.side_effect = IntegrityError('', {}, orig)

    result = tournament_orga_service.assign_orga(
        TOURNAMENT_ID, UserID(generate_uuid()), UserID(generate_uuid())
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'User is already an orga of this tournament.'
    )
    mock_db.session.rollback.assert_called_once()
    mock_signals.tournament_orga_assigned.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_unknown_constraint_reraises(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.exists_orga_for_tournament_and_user.return_value = False

    orig = Mock(constraint_name='fk_lan_tournament_orgas_tournament_id')
    mock_repo.create_orga.side_effect = IntegrityError('', {}, orig)

    with pytest.raises(IntegrityError):
        tournament_orga_service.assign_orga(
            TOURNAMENT_ID, UserID(generate_uuid()), UserID(generate_uuid())
        )

    mock_db.session.rollback.assert_called_once()
    mock_signals.tournament_orga_assigned.send.assert_not_called()


# -------------------------------------------------------------------- #
# session discipline on a non-integrity failure
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_assign_orga_rolls_back_on_non_integrity_failure(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.exists_orga_for_tournament_and_user.return_value = False
    mock_db.session.commit.side_effect = OperationalError('', {}, Exception())

    with pytest.raises(OperationalError):
        tournament_orga_service.assign_orga(
            TOURNAMENT_ID, UserID(generate_uuid()), UserID(generate_uuid())
        )

    mock_db.session.rollback.assert_called_once()
    mock_signals.tournament_orga_assigned.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_revoke_orga_rolls_back_on_failure(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.find_orga_for_tournament_and_user.return_value = _create_orga()
    mock_db.session.commit.side_effect = OperationalError('', {}, Exception())

    with pytest.raises(OperationalError):
        tournament_orga_service.revoke_orga(
            TOURNAMENT_ID, UserID(generate_uuid()), UserID(generate_uuid())
        )

    mock_db.session.rollback.assert_called_once()
    mock_signals.tournament_orga_revoked.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_orga_repository')
def test_revoke_orga_rolls_back_when_audit_entry_raises(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.find_orga_for_tournament_and_user.return_value = _create_orga()
    mock_log_service.create_log_entry.side_effect = OperationalError(
        '', {}, Exception()
    )

    with pytest.raises(OperationalError):
        tournament_orga_service.revoke_orga(
            TOURNAMENT_ID, UserID(generate_uuid()), UserID(generate_uuid())
        )

    mock_db.session.rollback.assert_called_once()
    mock_db.session.commit.assert_not_called()
    mock_signals.tournament_orga_revoked.send.assert_not_called()
