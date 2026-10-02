"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from byceps.database import db


RESET = Path('byceps/services/chair_optout/scripts/reset_test_chair_data.sql')


@pytest.fixture
def connection(admin_app):
    schema = f'chair_reset_{uuid4().hex}'
    with db.engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA {schema}')
        try:
            connection.exec_driver_sql(f'SET search_path TO {schema}')
            connection.exec_driver_sql("""
                CREATE TABLE tickets (
                    id integer PRIMARY KEY, chair_source text,
                    used_by_id integer, occupied_seat_id integer
                );
                INSERT INTO tickets VALUES
                    (1, 'user', 11, 21), (2, 'venue', 12, 22),
                    (3, 'rental', 13, NULL), (4, NULL, NULL, NULL),
                    (5, 'unknown', 14, 23);
                CREATE TABLE tournaments (id integer PRIMARY KEY);
                INSERT INTO tournaments VALUES (42);
            """)
            yield connection
        finally:
            connection.exec_driver_sql('ROLLBACK')
            connection.exec_driver_sql('SET search_path TO public')
            connection.exec_driver_sql(f'DROP SCHEMA {schema} CASCADE')


def _create_legacy_table(connection):
    connection.exec_driver_sql("""
        CREATE TABLE party_ticket_chair_optouts (
            id integer PRIMARY KEY, party_id text, ticket_id integer,
            user_id integer, brings_own_chair boolean, updated_at timestamp
        );
        INSERT INTO party_ticket_chair_optouts VALUES
            (1, 'party', 1, 11, true, now());
    """)


def _reset(connection):
    connection.exec_driver_sql(
        RESET.read_text(), execution_options={'no_parameters': True}
    )


@pytest.mark.parametrize('legacy_table', [False, True])
def test_reset_clears_only_chair_sources(connection, legacy_table):
    if legacy_table:
        _create_legacy_table(connection)
    before = connection.exec_driver_sql(
        'SELECT id, used_by_id, occupied_seat_id FROM tickets ORDER BY id'
    ).all()
    _reset(connection)
    assert connection.exec_driver_sql(
        'SELECT DISTINCT chair_source FROM tickets'
    ).all() == [('unknown',)]
    assert (
        connection.exec_driver_sql(
            'SELECT id, used_by_id, occupied_seat_id FROM tickets ORDER BY id'
        ).all()
        == before
    )
    assert connection.exec_driver_sql('SELECT id FROM tournaments').all() == [
        (42,)
    ]
    assert (
        connection.exec_driver_sql(
            "SELECT to_regclass('party_ticket_chair_optouts')"
        ).scalar_one()
        is None
    )
    _reset(connection)


@pytest.mark.parametrize('obstacle', ['unexpected_column', 'dependency'])
def test_unexpected_legacy_table_rolls_back_complete_reset(
    connection, obstacle
):
    _create_legacy_table(connection)
    if obstacle == 'unexpected_column':
        connection.exec_driver_sql(
            'ALTER TABLE party_ticket_chair_optouts ADD COLUMN extra text'
        )
    else:
        connection.exec_driver_sql(
            'CREATE VIEW dependent AS SELECT * FROM party_ticket_chair_optouts'
        )
    before = connection.exec_driver_sql(
        'SELECT * FROM tickets ORDER BY id'
    ).all()
    with pytest.raises(DBAPIError):
        _reset(connection)
    connection.exec_driver_sql('ROLLBACK')
    assert (
        connection.exec_driver_sql('SELECT * FROM tickets ORDER BY id').all()
        == before
    )
    assert (
        connection.exec_driver_sql(
            'SELECT count(*) FROM party_ticket_chair_optouts'
        ).scalar_one()
        == 1
    )
