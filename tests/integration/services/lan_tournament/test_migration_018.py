"""
tests.integration.services.lan_tournament.test_migration_018
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.authz import authz_service
from byceps.services.authz.models import PermissionID


pytestmark = pytest.mark.usefixtures('admin_app')

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)

FORWARD_SQL_PATH = MIGRATIONS_DIR / '018_add_tournament_images.sql'
ROLLBACK_SQL_PATH = MIGRATIONS_DIR / 'rollback_018.sql'

NEW_INDEXES = (
    'uq_lan_tournaments_creation_token',
    'ix_lan_tournaments_image_id',
    'ix_lan_tournament_images_party_id_created_at',
)
ADMINISTRATE = PermissionID('lan_tournament.administrate')
MAINTAIN = PermissionID('lan_tournament.maintain')
NEW_COLUMNS = ('image_id', 'image_alt_text', 'creation_token')


def _strip_transaction_control(sql: str) -> str:
    """Remove the `BEGIN;` and `COMMIT;` lines from a migration."""
    return '\n'.join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


def _granted_role_ids(connection, permission_id: PermissionID) -> set[str]:
    rows = connection.execute(
        text(
            'SELECT role_id FROM authz_role_permissions '
            'WHERE permission_id = :permission_id'
        ),
        {'permission_id': permission_id},
    ).all()
    return {row[0] for row in rows}


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


def _index_count(connection, index_name: str) -> int:
    return connection.execute(
        text('SELECT COUNT(*) FROM pg_indexes WHERE indexname = :index_name'),
        {'index_name': index_name},
    ).scalar_one()


def _column_exists(connection, table_name: str, column_name: str) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table_name AND column_name = :column_name'
            ')'
        ),
        {'table_name': table_name, 'column_name': column_name},
    ).scalar_one()


def _constraint_count(connection, constraint_name: str) -> int:
    return connection.execute(
        text(
            'SELECT COUNT(*) FROM pg_constraint '
            'WHERE conname = :constraint_name'
        ),
        {'constraint_name': constraint_name},
    ).scalar_one()


def _constraint_names(
    connection, table_name: str, name_pattern: str
) -> set[str]:
    rows = connection.execute(
        text(
            'SELECT conname FROM pg_constraint '
            'WHERE conrelid = CAST(:table_name AS regclass) '
            'AND conname LIKE :name_pattern'
        ),
        {'table_name': table_name, 'name_pattern': name_pattern},
    ).all()
    return {row[0] for row in rows}


def _index_names(
    connection, table_name: str, definition_pattern: str
) -> set[str]:
    rows = connection.execute(
        text(
            'SELECT indexname FROM pg_indexes '
            'WHERE tablename = :table_name '
            'AND indexdef LIKE :definition_pattern'
        ),
        {'table_name': table_name, 'definition_pattern': definition_pattern},
    ).all()
    return {row[0] for row in rows}


def _assert_isolated_test_database() -> None:
    """Refuse to run DDL against anything but a `byceps_test*` database.

    Parallel lanes use their own `POSTGRES_DB` (`byceps_test_f18a`), so
    only the prefix is checked.
    """
    print(f'db.engine.url = {db.engine.url!r}')
    expected_db_name = os.environ.get('POSTGRES_DB', 'byceps_test')
    assert db.engine.url.database == expected_db_name
    assert expected_db_name.startswith('byceps_test')


def test_migration_018_applies_cleanly_on_create_all_schema():
    """Run migration 018 twice on the `create_all` schema, then roll back."""
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())

    # Release the session's locks, or the DDL below blocks.
    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(forward_sql)

            assert _table_exists(connection, 'lan_tournament_images')
            for column in NEW_COLUMNS:
                assert _column_exists(connection, 'lan_tournaments', column)
        finally:
            transaction.rollback()


def test_migration_018_adds_no_duplicate_constraints():
    """The dbmodel and migration 018 name every object identically.

    `create_all` builds the schema, then the migration runs on top: a
    differently named constraint or index would show up twice.
    """
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)

            assert _constraint_names(
                connection, 'lan_tournaments', '%image_id%'
            ) == {'fk_lan_tournaments_image_id'}
            assert _constraint_names(
                connection, 'lan_tournament_images', '%'
            ) == {
                'pk_lan_tournament_images',
                'fk_lan_tournament_images_party_id',
                'fk_lan_tournament_images_creator_id',
                'ck_lan_tournament_images_filename_length',
                'ck_lan_tournament_images_image_type',
                'ck_lan_tournament_images_dimensions',
                'ck_lan_tournament_images_byte_size',
            }
            assert _index_names(
                connection, 'lan_tournaments', '%(image_id)%'
            ) == {'ix_lan_tournaments_image_id'}
            assert _index_names(
                connection, 'lan_tournaments', '%(creation_token)%'
            ) == {'uq_lan_tournaments_creation_token'}
            assert _index_names(connection, 'lan_tournament_images', '%') == {
                'pk_lan_tournament_images',
                'ix_lan_tournament_images_party_id_created_at',
            }
            for index_name in NEW_INDEXES:
                assert _index_count(connection, index_name) == 1, index_name
        finally:
            transaction.rollback()


def test_rollback_018_removes_all_objects():
    """Run migration 018 and its rollback in a transaction rolled back."""
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(rollback_sql)

            assert not _table_exists(connection, 'lan_tournament_images')
            for column in NEW_COLUMNS:
                assert not _column_exists(connection, 'lan_tournaments', column)
            for index_name in NEW_INDEXES:
                assert _index_count(connection, index_name) == 0, index_name
            assert (
                _constraint_count(connection, 'fk_lan_tournaments_image_id')
                == 0
            )

            # Idempotent: a second rollback is a no-op.
            connection.exec_driver_sql(rollback_sql)
        finally:
            transaction.rollback()


def test_migration_018_grants_maintain_to_administrating_roles(make_role):
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())

    admin_like_role = make_role()
    authz_service.assign_permission_to_role(ADMINISTRATE, admin_like_role.id)
    viewer_role = make_role()
    authz_service.assign_permission_to_role(
        PermissionID('lan_tournament.view'), viewer_role.id
    )
    db.session.commit()

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            assert admin_like_role.id not in _granted_role_ids(
                connection, MAINTAIN
            )

            connection.exec_driver_sql(forward_sql)

            granted = _granted_role_ids(connection, MAINTAIN)
            assert admin_like_role.id in granted
            assert viewer_role.id not in granted
        finally:
            transaction.rollback()


def test_migration_018_rerun_is_idempotent(make_role):
    """Staging re-runs the whole updated 018: schema part and grant."""
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())

    admin_like_role = make_role()
    authz_service.assign_permission_to_role(ADMINISTRATE, admin_like_role.id)
    db.session.commit()

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(forward_sql)

            grant_rows = connection.execute(
                text(
                    'SELECT COUNT(*) FROM authz_role_permissions '
                    'WHERE role_id = :role_id AND permission_id = :permission_id'
                ),
                {'role_id': admin_like_role.id, 'permission_id': MAINTAIN},
            ).scalar_one()
            assert grant_rows == 1
            assert _table_exists(connection, 'lan_tournament_images')
            for index_name in NEW_INDEXES:
                assert _index_count(connection, index_name) == 1, index_name
            assert (
                _constraint_count(connection, 'fk_lan_tournaments_image_id')
                == 1
            )
        finally:
            transaction.rollback()


def test_rollback_018_revokes_maintain_only(make_role):
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())

    admin_like_role = make_role()
    authz_service.assign_permission_to_role(ADMINISTRATE, admin_like_role.id)
    db.session.commit()

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)
            assert admin_like_role.id in _granted_role_ids(connection, MAINTAIN)

            connection.exec_driver_sql(rollback_sql)

            assert _granted_role_ids(connection, MAINTAIN) == set()
            assert admin_like_role.id in _granted_role_ids(
                connection, ADMINISTRATE
            )
        finally:
            transaction.rollback()
