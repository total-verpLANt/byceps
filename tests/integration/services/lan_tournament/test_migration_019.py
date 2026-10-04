"""
tests.integration.services.lan_tournament.test_migration_019
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import os
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from byceps.database import db


pytestmark = pytest.mark.usefixtures('admin_app')

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)

FORWARD_SQL_PATH = MIGRATIONS_DIR / '019_add_tournament_seedings.sql'
ROLLBACK_SQL_PATH = MIGRATIONS_DIR / 'rollback_019.sql'

TABLE = 'lan_tournament_seedings'
CONSTRAINT_NAMES = {
    'lan_tournament_seedings_pkey',
    'fk_lan_tournament_seedings_tournament_id',
    'fk_lan_tournament_seedings_updated_by',
    'uq_lan_tournament_seedings_tournament_target',
    'ck_lan_tournament_seedings_version',
    'ck_lan_tournament_seedings_target',
}


def _strip_transaction_control(sql: str) -> str:
    """Remove the `BEGIN;` and `COMMIT;` lines from a migration."""
    return '\n'.join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


def _assert_isolated_test_database() -> None:
    """Refuse to run DDL against anything but a `byceps_test*` database.

    Parallel lanes use their own `POSTGRES_DB` (`byceps_test_f10a`), so
    only the prefix is checked.
    """
    print(f'db.engine.url = {db.engine.url!r}')
    expected_db_name = os.environ.get('POSTGRES_DB', 'byceps_test')
    assert db.engine.url.database == expected_db_name
    assert expected_db_name.startswith('byceps_test')


def _table_exists(connection) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_name = :table_name'
            ')'
        ),
        {'table_name': TABLE},
    ).scalar_one()


def _schema_signature(connection) -> dict[str, object]:
    """Describe columns, constraints and indexes of the seedings table."""
    columns = connection.execute(
        text(
            'SELECT column_name, data_type, character_maximum_length, '
            'is_nullable, column_default '
            'FROM information_schema.columns '
            'WHERE table_name = :table_name ORDER BY column_name'
        ),
        {'table_name': TABLE},
    ).all()
    constraints = connection.execute(
        text(
            'SELECT conname, contype, pg_get_constraintdef(oid) '
            'FROM pg_constraint '
            'WHERE conrelid = CAST(:table_name AS regclass) '
            'ORDER BY conname'
        ),
        {'table_name': TABLE},
    ).all()
    indexes = connection.execute(
        text(
            'SELECT indexname, indexdef FROM pg_indexes '
            'WHERE tablename = :table_name ORDER BY indexname'
        ),
        {'table_name': TABLE},
    ).all()
    return {
        'columns': [tuple(row) for row in columns],
        'constraints': [tuple(row) for row in constraints],
        'indexes': [tuple(row) for row in indexes],
    }


@pytest.mark.parametrize(
    'sql_path', [FORWARD_SQL_PATH, ROLLBACK_SQL_PATH], ids=['forward', 'rollback']
)
def test_migration_019_sets_a_lock_timeout_first(sql_path):
    lines = sql_path.read_text().splitlines()
    begin_index = lines.index('BEGIN;')

    assert lines[begin_index + 1] == "SET LOCAL lock_timeout = '5s';"


def test_migration_019_gives_up_on_a_held_lock():
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as holder, db.engine.connect() as contender:
        try:
            holder.exec_driver_sql(
                'LOCK TABLE lan_tournament_seedings IN ACCESS SHARE MODE'
            )
            contender.exec_driver_sql("SET LOCAL statement_timeout = '20s';")

            started_at = monotonic()
            with pytest.raises(OperationalError) as exc_info:
                contender.exec_driver_sql(rollback_sql)
            elapsed = monotonic() - started_at

            assert exc_info.value.orig.sqlstate == '55P03'
            assert 4 <= elapsed < 15
        finally:
            contender.rollback()
            holder.rollback()


def test_migration_019_creates_table_and_constraints():
    """The SQL builds the same table as the dbmodel's `create_all`."""
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())

    # Release the session's locks, or the DDL below blocks.
    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            from_dbmodel = _schema_signature(connection)
            assert {c[0] for c in from_dbmodel['constraints']} == (
                CONSTRAINT_NAMES
            )

            connection.exec_driver_sql(f'DROP TABLE {TABLE}')
            assert not _table_exists(connection)

            connection.exec_driver_sql(forward_sql)
            from_sql = _schema_signature(connection)

            assert from_sql == from_dbmodel
            assert {c[0] for c in from_sql['constraints']} == CONSTRAINT_NAMES
            assert {c[0] for c in from_sql['columns']} == {
                'id',
                'tournament_id',
                'target',
                'seed_code',
                'version',
                'generated_seed_code',
                'generated_at',
                'updated_by',
                'roster_snapshot',
                'created_at',
                'updated_at',
            }
        finally:
            transaction.rollback()


def test_migration_019_is_idempotent():
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(f'DROP TABLE {TABLE}')
            connection.exec_driver_sql(forward_sql)
            after_first = _schema_signature(connection)

            connection.exec_driver_sql(forward_sql)

            assert _schema_signature(connection) == after_first
        finally:
            transaction.rollback()


def test_rollback_019_removes_all_objects():
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(rollback_sql)

            assert not _table_exists(connection)
            leftovers = connection.execute(
                text(
                    'SELECT COUNT(*) FROM pg_constraint '
                    'WHERE conname LIKE :name_pattern'
                ),
                {'name_pattern': '%lan_tournament_seedings%'},
            ).scalar_one()
            assert leftovers == 0

            # Idempotent: a second rollback is a no-op.
            connection.exec_driver_sql(rollback_sql)
        finally:
            transaction.rollback()
