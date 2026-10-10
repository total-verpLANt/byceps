"""
tests.integration.services.lan_tournament.test_migration_020
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import os
from pathlib import Path
import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import tournament_service
from byceps.services.lan_tournament.dbmodels import (  # noqa: F401
    qualification_decision,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


pytestmark = pytest.mark.usefixtures('admin_app')

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)

FORWARD_SQL_PATH = MIGRATIONS_DIR / '020_add_playoff_phase.sql'
ROLLBACK_SQL_PATH = MIGRATIONS_DIR / 'rollback_020.sql'

PARTY_ID = PartyID('lan-party-2024-migration-020')

DECISIONS_TABLE = 'lan_tournament_qualification_decisions'
TOURNAMENT_COLUMNS = (
    'playoff_game_format',
    'playoff_elimination_mode',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_release_mode',
    'playoff_auto_release_suspended',
    'playoff_released_at',
    'playoff_released_by',
    'leaderboard_closed_at',
)
TOURNAMENT_CONSTRAINTS = (
    'ck_lan_tournaments_playoff_config',
    'fk_lan_tournaments_playoff_released_by',
)
MATCH_CONSTRAINTS = ('ck_lan_tournament_matches_phase',)
MATCH_COLUMNS = ('phase', 'seeding_target')
MATCH_INDEXES = (
    'ix_lan_tournament_matches_tournament_phase',
    'ix_lan_tournament_matches_tournament_seeding_target',
)
DECISION_CONSTRAINTS = (
    'lan_tournament_qualification_decisions_pkey',
    'fk_lan_tournament_qualification_decisions_tournament_id',
    'fk_lan_tournament_qualification_decisions_decided_by',
    'uq_lan_tournament_qualification_decisions_scope',
    'ck_lan_tournament_qualification_decisions_reason',
)
ALL_CONSTRAINTS = (
    TOURNAMENT_CONSTRAINTS + MATCH_CONSTRAINTS + DECISION_CONSTRAINTS
)
ALL_INDEXES = MATCH_INDEXES + (
    'lan_tournament_qualification_decisions_pkey',
    'uq_lan_tournament_qualification_decisions_scope',
)

ROUND_ROBIN_PLAYOFF = {
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'ROUND_ROBIN',
    'playoff_game_format': 'ONE_V_ONE',
    'playoff_elimination_mode': 'SINGLE_ELIMINATION',
    'playoff_group_count': 4,
    'playoff_qualifiers_per_group': 2,
    'playoff_qualifier_count': None,
    'playoff_release_mode': 'MANUAL',
}
HIGHSCORE_PLAYOFF = {
    'game_format': 'HIGHSCORE',
    'elimination_mode': 'NONE',
    'playoff_game_format': 'FREE_FOR_ALL',
    'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
    'playoff_group_count': None,
    'playoff_qualifiers_per_group': None,
    'playoff_qualifier_count': 8,
    'playoff_release_mode': 'AUTOMATIC',
}
NO_PLAYOFF = {
    'playoff_game_format': None,
    'playoff_elimination_mode': None,
    'playoff_group_count': None,
    'playoff_qualifiers_per_group': None,
    'playoff_qualifier_count': None,
    'playoff_release_mode': None,
}


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('migration020brand', 'Migration 020 Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Migration 020')


@pytest.fixture(scope='module')
def orga(make_user):
    return make_user('Migration020Orga')


@pytest.fixture
def tournament_id(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Migration 020 Tournament {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    yield tournament.id
    db.session.rollback()
    db.session.execute(
        text('DELETE FROM lan_tournaments WHERE id = :id'),
        {'id': tournament.id},
    )
    db.session.commit()


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


def _column_names(connection, table_name: str, names) -> set[str]:
    rows = connection.execute(
        text(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_name = :table_name AND column_name = ANY(:names)'
        ),
        {'table_name': table_name, 'names': list(names)},
    ).all()
    return {row[0] for row in rows}


def _constraint_count(connection) -> int:
    return connection.execute(
        text('SELECT COUNT(*) FROM pg_constraint WHERE conname = ANY(:names)'),
        {'names': list(ALL_CONSTRAINTS)},
    ).scalar_one()


def _index_count(connection) -> int:
    return connection.execute(
        text('SELECT COUNT(*) FROM pg_indexes WHERE indexname = ANY(:names)'),
        {'names': list(ALL_INDEXES)},
    ).scalar_one()


def _normalize_default(column: tuple) -> tuple:
    """Treat `DEFAULT '1'` and `DEFAULT 1` alike: both store `1`."""
    *head, default = column
    if default is not None:
        default = re.sub(r"^'(.*)'::[a-z ]+$", r'\1', default)
    return (*head, default)


def _schema_signature(connection) -> dict[str, object]:
    """Describe every object migration 020 adds, as PostgreSQL sees it."""
    columns = connection.execute(
        text(
            'SELECT table_name, column_name, data_type, '
            'character_maximum_length, is_nullable, column_default '
            'FROM information_schema.columns '
            'WHERE (table_name = :tournaments '
            'AND column_name = ANY(:tournament_columns)) '
            'OR (table_name = :matches AND column_name = ANY(:match_columns)) '
            'OR table_name = :decisions '
            'ORDER BY table_name, column_name'
        ),
        {
            'tournaments': 'lan_tournaments',
            'tournament_columns': list(TOURNAMENT_COLUMNS),
            'matches': 'lan_tournament_matches',
            'match_columns': list(MATCH_COLUMNS),
            'decisions': DECISIONS_TABLE,
        },
    ).all()
    constraints = connection.execute(
        text(
            'SELECT conname, contype, pg_get_constraintdef(oid) '
            'FROM pg_constraint WHERE conname = ANY(:names) ORDER BY conname'
        ),
        {'names': list(ALL_CONSTRAINTS)},
    ).all()
    indexes = connection.execute(
        text(
            'SELECT indexname, indexdef FROM pg_indexes '
            'WHERE indexname = ANY(:names) ORDER BY indexname'
        ),
        {'names': list(ALL_INDEXES)},
    ).all()
    return {
        'columns': [_normalize_default(tuple(row)) for row in columns],
        'constraints': [tuple(row) for row in constraints],
        'indexes': [tuple(row) for row in indexes],
    }


def _set_config(connection, tournament_id, config) -> str | None:
    """Return the violated constraint name, or `None` if the row took it."""
    assignments = ', '.join(f'{column} = :{column}' for column in config)
    savepoint = connection.begin_nested()
    try:
        connection.execute(
            text(f'UPDATE lan_tournaments SET {assignments} WHERE id = :id'),  # noqa: S608
            {**config, 'id': tournament_id},
        )
    except IntegrityError as exc:
        savepoint.rollback()
        return exc.orig.diag.constraint_name
    savepoint.commit()
    return None


@pytest.mark.parametrize(
    'sql_path', [FORWARD_SQL_PATH, ROLLBACK_SQL_PATH], ids=['forward', 'rollback']
)
def test_migration_020_sets_a_lock_timeout_first(sql_path):
    lines = sql_path.read_text().splitlines()
    begin_index = lines.index('BEGIN;')

    assert lines[begin_index + 1] == "SET LOCAL lock_timeout = '5s';"


def test_migration_020_adds_columns_and_table():
    """The SQL adds exactly what the dbmodels create with `create_all`."""
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())
    # 025 widens the playoff CHECK 020 creates; the dbmodels carry that state.
    widen_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / '025_allow_single_group_playoffs.sql').read_text()
    )

    # Release the session's locks, or the DDL below blocks.
    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            from_dbmodels = _schema_signature(connection)
            assert {c[0] for c in from_dbmodels['constraints']} == set(
                ALL_CONSTRAINTS
            )

            # Back to the pre-020 schema, then apply the migration.
            connection.exec_driver_sql(rollback_sql)
            assert _schema_signature(connection)['columns'] == []
            assert not _table_exists(connection, DECISIONS_TABLE)

            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(widen_sql)

            assert _table_exists(connection, DECISIONS_TABLE)
            assert _column_names(
                connection, 'lan_tournaments', TOURNAMENT_COLUMNS
            ) == set(TOURNAMENT_COLUMNS)
            assert _column_names(
                connection, 'lan_tournament_matches', MATCH_COLUMNS
            ) == set(MATCH_COLUMNS)
            assert _schema_signature(connection) == from_dbmodels
        finally:
            transaction.rollback()


def test_migration_020_is_idempotent():
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())
    # 025 widens the playoff CHECK 020 creates; the dbmodels carry that state.
    widen_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / '025_allow_single_group_playoffs.sql').read_text()
    )

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Re-running on the finished schema changes nothing.
            expected = _schema_signature(connection)
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(widen_sql)
            assert _schema_signature(connection) == expected

            connection.exec_driver_sql(rollback_sql)
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(widen_sql)
            assert _schema_signature(connection) == expected
            assert _constraint_count(connection) == len(ALL_CONSTRAINTS)
        finally:
            transaction.rollback()


def test_migration_020_playoff_config_check(tournament_id):
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())
    check = 'ck_lan_tournaments_playoff_config'

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Test the constraint the SQL creates, not the dbmodel's.
            connection.exec_driver_sql(rollback_sql)
            connection.exec_driver_sql(forward_sql)

            def apply(config):
                return _set_config(connection, tournament_id, config)

            assert apply({**NO_PLAYOFF, 'game_format': 'ONE_V_ONE'}) is None
            assert apply(ROUND_ROBIN_PLAYOFF) is None
            assert apply(HIGHSCORE_PLAYOFF) is None
            assert apply({**NO_PLAYOFF, 'game_format': None}) is None

            rejected = {
                'highscore half-set (null elimination mode)': {
                    **NO_PLAYOFF,
                    'game_format': 'HIGHSCORE',
                    'playoff_game_format': 'FREE_FOR_ALL',
                },
                'round robin half-set (only playoff format)': {
                    **NO_PLAYOFF,
                    'game_format': 'ONE_V_ONE',
                    'elimination_mode': 'ROUND_ROBIN',
                    'playoff_game_format': 'ONE_V_ONE',
                },
                'round robin half-set (null group count)': {
                    **ROUND_ROBIN_PLAYOFF,
                    'playoff_group_count': None,
                },
                'highscore half-set (null qualifier count)': {
                    **HIGHSCORE_PLAYOFF,
                    'playoff_qualifier_count': None,
                },
                'no release mode': {
                    **ROUND_ROBIN_PLAYOFF,
                    'playoff_release_mode': None,
                },
                'one group': {**ROUND_ROBIN_PLAYOFF, 'playoff_group_count': 1},
                'no qualifiers per group': {
                    **ROUND_ROBIN_PLAYOFF,
                    'playoff_qualifiers_per_group': 0,
                },
                'one qualifier': {
                    **HIGHSCORE_PLAYOFF,
                    'playoff_qualifier_count': 1,
                },
                'groups on highscore': {
                    **HIGHSCORE_PLAYOFF,
                    'playoff_group_count': 4,
                },
                'ffa playoff on round robin': {
                    **ROUND_ROBIN_PLAYOFF,
                    'playoff_game_format': 'FREE_FOR_ALL',
                },
                'round robin as playoff mode': {
                    **ROUND_ROBIN_PLAYOFF,
                    'playoff_elimination_mode': 'ROUND_ROBIN',
                },
                'playoffs on plain bracket': {
                    **ROUND_ROBIN_PLAYOFF,
                    'elimination_mode': 'SINGLE_ELIMINATION',
                },
            }
            for label, config in rejected.items():
                assert apply(config) == check, label
        finally:
            transaction.rollback()


def test_migration_020_qualification_reason_check(tournament_id, orga):
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())
    check = 'ck_lan_tournament_qualification_decisions_reason'

    db.session.close()
    _assert_isolated_test_database()

    def insert(connection, scope, reason) -> str | None:
        savepoint = connection.begin_nested()
        try:
            connection.execute(
                text(
                    f'INSERT INTO {DECISIONS_TABLE} (id, tournament_id, '  # noqa: S608
                    'scope, ordered_contestant_ids, reason, decided_by, '
                    'decided_at) VALUES (:id, :tournament_id, :scope, '
                    "'[]', :reason, :decided_by, now())"
                ),
                {
                    'id': generate_uuid7(),
                    'tournament_id': tournament_id,
                    'scope': scope,
                    'reason': reason,
                    'decided_by': orga.id,
                },
            )
        except IntegrityError as exc:
            savepoint.rollback()
            return exc.orig.diag.constraint_name
        savepoint.commit()
        return None

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(rollback_sql)
            connection.exec_driver_sql(forward_sql)

            assert insert(connection, 'winner', 'Orga decision') is None
            assert insert(connection, 'group:1', '') == check
            assert insert(connection, 'group:2', '   ') == check
            assert insert(connection, 'group:3', '\n\t') == check
            assert insert(connection, 'group:4', ' \r\n \t ') == check
            assert insert(connection, 'group:5', '\n x \n') is None
            assert (
                insert(connection, 'winner', 'Second row for one scope')
                == 'uq_lan_tournament_qualification_decisions_scope'
            )
        finally:
            transaction.rollback()


def test_rollback_020_removes_all_objects():
    forward_sql = _strip_transaction_control(FORWARD_SQL_PATH.read_text())
    rollback_sql = _strip_transaction_control(ROLLBACK_SQL_PATH.read_text())

    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)
            connection.exec_driver_sql(rollback_sql)

            assert not _table_exists(connection, DECISIONS_TABLE)
            assert (
                _column_names(connection, 'lan_tournaments', TOURNAMENT_COLUMNS)
                == set()
            )
            assert (
                _column_names(
                    connection, 'lan_tournament_matches', MATCH_COLUMNS
                )
                == set()
            )
            assert _constraint_count(connection) == 0
            assert _index_count(connection) == 0

            # Idempotent: a second rollback is a no-op.
            connection.exec_driver_sql(rollback_sql)
        finally:
            transaction.rollback()
