"""
tests.integration.services.lan_tournament.test_tournament_orga_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session as SqlaSession

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_orga_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2024-orga')

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Orga')


@pytest.fixture(scope='module')
def initiator(make_user):
    return make_user('OrgaAssignInitiator')


@pytest.fixture(scope='module')
def orga_user(make_user):
    return make_user('OrgaUser')


@pytest.fixture(scope='module')
def tournament(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Orga Assignment Test Tournament',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    return tournament


def _read_orga_via_fresh_connection(tournament_id, user_id):
    """Read the committed orga assignment through a separate session."""
    with SqlaSession(bind=db.engine) as fresh:
        return fresh.execute(
            select(DbTournamentOrga).filter_by(
                tournament_id=tournament_id, user_id=user_id
            )
        ).scalar_one_or_none()


def test_orga_round_trips_through_database(tournament, orga_user, initiator):
    assign_result = tournament_orga_service.assign_orga(
        tournament.id,
        orga_user.id,
        initiator.id,
        duties='Bracket admin',
    )
    assert assign_result.is_ok()
    orga, _event = assign_result.unwrap()

    db_orga = _read_orga_via_fresh_connection(tournament.id, orga_user.id)
    assert db_orga is not None
    assert db_orga.id == orga.id
    assert db_orga.assigned_by_id == initiator.id
    assert db_orga.duties == 'Bracket admin'

    log_entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert any(
        entry.event_type == 'tournament-orga-assigned' for entry in log_entries
    )

    revoke_result = tournament_orga_service.revoke_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert revoke_result.is_ok()

    assert _read_orga_via_fresh_connection(tournament.id, orga_user.id) is None

    log_entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert any(
        entry.event_type == 'tournament-orga-revoked' for entry in log_entries
    )


def test_unique_constraint_rejects_duplicate_assignment(
    tournament, orga_user, initiator
):
    first_result = tournament_orga_service.assign_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert first_result.is_ok()

    second_result = tournament_orga_service.assign_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert second_result.is_err()
    assert second_result.unwrap_err() == (
        'User is already an orga of this tournament.'
    )

    cleanup_result = tournament_orga_service.revoke_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert cleanup_result.is_ok()


def _strip_transaction_control(sql: str) -> str:
    """Remove the `BEGIN;` and `COMMIT;` lines from a migration."""
    return '\n'.join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


def _table_exists(connection, table_name: str) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_name = :table_name'
            ')'
        ),
        {'table_name': table_name},
    ).scalar_one()


def _index_exists(connection, index_name: str) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM pg_indexes '
            'WHERE indexname = :index_name'
            ')'
        ),
        {'index_name': index_name},
    ).scalar_one()


def test_deleting_a_tournament_removes_its_orga_assignments(
    party, orga_user, initiator
):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Orga Deletion Cascade Tournament',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    doomed, _ = result.unwrap()

    assert tournament_orga_service.assign_orga(
        doomed.id, orga_user.id, initiator.id
    ).is_ok()
    assert _read_orga_via_fresh_connection(doomed.id, orga_user.id) is not None

    tournament_service.delete_tournament(doomed.id, initiator.id)

    assert tournament_service.find_tournament(doomed.id) is None
    assert _read_orga_via_fresh_connection(doomed.id, orga_user.id) is None


def test_rollback_014_removes_all_objects():
    """Run migration 014 and its rollback in a transaction rolled back."""
    forward_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / '014_add_tournament_orga.sql').read_text()
    )
    rollback_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / 'rollback_014.sql').read_text()
    )

    # Release the session's locks, or the DROP TABLE below blocks.
    db.session.close()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)

            assert _table_exists(connection, 'lan_tournament_orgas')
            assert _index_exists(
                connection, 'ix_lan_tournament_orgas_tournament_id'
            )
            assert _index_exists(connection, 'ix_lan_tournament_orgas_user_id')

            connection.exec_driver_sql(rollback_sql)

            assert not _table_exists(connection, 'lan_tournament_orgas')
            assert not _index_exists(
                connection, 'ix_lan_tournament_orgas_tournament_id'
            )
            assert not _index_exists(
                connection, 'ix_lan_tournament_orgas_user_id'
            )
        finally:
            transaction.rollback()


def test_lost_race_on_duplicate_assignment_returns_err_not_500(
    tournament, orga_user, initiator, monkeypatch
):
    seed_result = tournament_orga_service.assign_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert seed_result.is_ok()

    monkeypatch.setattr(
        tournament_orga_service.tournament_orga_repository,
        'exists_orga_for_tournament_and_user',
        lambda tournament_id, user_id: False,
    )

    racing_result = tournament_orga_service.assign_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert racing_result.is_err()
    assert racing_result.unwrap_err() == (
        'User is already an orga of this tournament.'
    )

    monkeypatch.undo()

    # The session survived the rollback: a later statement still runs.
    assert tournament_orga_service.is_orga_for_tournament(
        orga_user.id, tournament.id
    )

    cleanup_result = tournament_orga_service.revoke_orga(
        tournament.id, orga_user.id, initiator.id
    )
    assert cleanup_result.is_ok()


def test_status_change_is_logged_with_its_initiator(tournament, initiator):
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )

    result = tournament_service.create_tournament(
        PARTY_ID,
        'Orga Status Log Tournament',
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.DRAFT,
    )
    assert result.is_ok()
    subject, _ = result.unwrap()

    change_result = tournament_service.change_status(
        subject.id, TournamentStatus.REGISTRATION_OPEN, initiator.id
    )
    assert change_result.is_ok()

    entries = [
        entry
        for entry in tournament_log_service.get_entries_for_tournament(
            subject.id
        )
        if entry.event_type == 'tournament-status-changed'
    ]
    assert len(entries) == 1
    assert entries[0].initiator_id == initiator.id
    assert entries[0].data == {
        'old_status': TournamentStatus.DRAFT.name,
        'new_status': TournamentStatus.REGISTRATION_OPEN.name,
    }
