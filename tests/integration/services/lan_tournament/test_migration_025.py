import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import tournament_service
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


pytestmark = pytest.mark.usefixtures('admin_app')

MIGRATIONS = (
    Path(__file__).resolve().parents[4]
    / 'byceps/services/lan_tournament/migrations'
)
PARTY_ID = PartyID('lan-party-2024-migration-025')
CHECK = 'ck_lan_tournaments_playoff_config'

ONE_GROUP = {
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'ROUND_ROBIN',
    'playoff_game_format': 'ONE_V_ONE',
    'playoff_elimination_mode': 'SINGLE_ELIMINATION',
    'playoff_group_count': 1,
    'playoff_qualifiers_per_group': 2,
    'playoff_qualifier_count': None,
    'playoff_release_mode': 'MANUAL',
}
CLEAR_ONE_GROUP_PLAYOFFS = text(
    'UPDATE lan_tournaments SET playoff_game_format = NULL,'
    ' playoff_elimination_mode = NULL, playoff_group_count = NULL,'
    ' playoff_qualifiers_per_group = NULL, playoff_qualifier_count = NULL,'
    ' playoff_release_mode = NULL WHERE playoff_group_count = 1'
)


def _sql(filename):
    return '\n'.join(
        line
        for line in (MIGRATIONS / filename).read_text().splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('migration025brand', 'Migration 025 Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Migration 025')


@pytest.fixture
def tournament_id(party):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        f'Migration 025 Tournament {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
    ).unwrap()
    yield tournament.id
    db.session.rollback()
    db.session.execute(
        text('DELETE FROM lan_tournaments WHERE id = :id'),
        {'id': tournament.id},
    )
    db.session.commit()


def _constraint_def(connection):
    return connection.execute(
        text(
            'SELECT pg_get_constraintdef(oid) FROM pg_constraint '
            "WHERE conrelid = 'lan_tournaments'::regclass AND conname = :name"
        ),
        {'name': CHECK},
    ).scalar_one()


def _set_config(connection, tournament_id, config):
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
        # psycopg keeps `constraint_name` on `.diag`, not on `orig`.
        return exc.orig.diag.constraint_name
    savepoint.commit()
    return None


def _assert_isolated_test_database():
    database = os.environ.get('POSTGRES_DB', 'byceps_test')
    assert db.engine.url.database == database
    assert database.startswith('byceps_test')


def test_025_relaxes_the_group_minimum_and_matches_the_orm(tournament_id):
    db.session.close()
    _assert_isolated_test_database()

    # DDL is transactional: the rollback restores the dbmodel schema.
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            orm_def = _constraint_def(connection)
            assert 'playoff_group_count >= 1' in orm_def

            # Rebuild the 020 constraint to prove 025 widens that one.
            connection.exec_driver_sql(
                f'ALTER TABLE lan_tournaments DROP CONSTRAINT {CHECK}'
            )
            connection.exec_driver_sql(_sql('020_add_playoff_phase.sql'))
            old_def = _constraint_def(connection)
            assert 'playoff_group_count >= 2' in old_def
            assert _set_config(connection, tournament_id, ONE_GROUP) == CHECK

            connection.exec_driver_sql(
                _sql('025_allow_single_group_playoffs.sql')
            )
            connection.exec_driver_sql(
                _sql('025_allow_single_group_playoffs.sql')
            )

            new_def = _constraint_def(connection)
            assert 'playoff_group_count >= 1' in new_def
            assert new_def == orm_def
            assert new_def == old_def.replace(
                'playoff_group_count >= 2', 'playoff_group_count >= 1'
            )
            assert _set_config(connection, tournament_id, ONE_GROUP) is None
            assert (
                _set_config(
                    connection,
                    tournament_id,
                    {**ONE_GROUP, 'playoff_group_count': 0},
                )
                == CHECK
            )
            assert (
                _set_config(
                    connection,
                    tournament_id,
                    {**ONE_GROUP, 'playoff_qualifiers_per_group': 0},
                )
                == CHECK
            )
        finally:
            transaction.rollback()


def test_rollback_025_refuses_while_a_one_group_tournament_exists(
    tournament_id,
):
    db.session.close()
    _assert_isolated_test_database()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            orm_def = _constraint_def(connection)
            assert 'playoff_group_count >= 1' in orm_def

            # Foreign one-group rows are not under test: take them out first.
            connection.execute(CLEAR_ONE_GROUP_PLAYOFFS)
            assert _set_config(connection, tournament_id, ONE_GROUP) is None

            savepoint = connection.begin_nested()
            with pytest.raises(DBAPIError) as exc_info:
                connection.exec_driver_sql(_sql('rollback_025.sql'))
            savepoint.rollback()
            assert 'rollback_025: 1 tournament(s)' in str(exc_info.value.orig)
            assert _constraint_def(connection) == orm_def

            connection.execute(
                text('DELETE FROM lan_tournaments WHERE id = :id'),
                {'id': tournament_id},
            )
            connection.exec_driver_sql(_sql('rollback_025.sql'))
            assert 'playoff_group_count >= 2' in _constraint_def(connection)

            # End in the dbmodel state, like the rest of the schema.
            connection.exec_driver_sql(
                _sql('025_allow_single_group_playoffs.sql')
            )
            assert _constraint_def(connection) == orm_def
        finally:
            transaction.rollback()
