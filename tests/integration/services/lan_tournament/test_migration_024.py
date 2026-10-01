import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import tournament_service


pytestmark = pytest.mark.usefixtures('admin_app')
MIGRATIONS = (
    Path(__file__).resolve().parents[4]
    / 'byceps/services/lan_tournament/migrations'
)


def _sql(filename):
    return '\n'.join(
        line
        for line in (MIGRATIONS / filename).read_text().splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


def _snapshot(connection, tournament_id):
    return connection.execute(
        text(
            "SELECT to_jsonb(t) - 'category' FROM lan_tournaments t WHERE id = :id"
        ),
        {'id': tournament_id},
    ).scalar_one()


def _category_schema(connection):
    column = connection.execute(
        text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'lan_tournaments' AND column_name = 'category'"
        )
    ).one()
    constraints = connection.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'lan_tournaments'::regclass AND conname = 'ck_lan_tournaments_category'"
        )
    ).all()
    return tuple(column), constraints


def test_first_application_backfill_rerun_rollback_and_orm_parity(party):
    regular, _ = tournament_service.create_tournament(
        party.id, 'Existing main', position=7
    ).unwrap()
    requested, _ = tournament_service.create_tournament(
        party.id,
        'Existing request',
        position=11,
        creation_token=uuid4(),
        image_alt_text='Existing cover',
    ).unwrap()
    db.session.close()
    database = os.environ.get('POSTGRES_DB', 'byceps_test')
    assert db.engine.url.database == database and database.startswith(
        'byceps_test'
    )

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            orm_schema = _category_schema(connection)
            assert orm_schema[0] == ('text', 'NO', "'MAIN'::text")
            request_id = uuid4()
            connection.execute(
                text(
                    'UPDATE lan_tournaments SET created_from_request_id = :request WHERE id = :id'
                ),
                {'request': request_id, 'id': requested.id},
            )
            snapshots = {
                t.id: _snapshot(connection, t.id) for t in (regular, requested)
            }
            connection.exec_driver_sql(_sql('rollback_024.sql'))
            # A same-named constraint on another table must not suppress ours.
            connection.exec_driver_sql(
                'CREATE TEMP TABLE category_constraint_decoy (category TEXT CONSTRAINT ck_lan_tournaments_category CHECK (category IS NOT NULL))'
            )
            connection.exec_driver_sql(_sql('024_add_tournament_category.sql'))
            assert _category_schema(connection) == orm_schema
            default_id = uuid4()
            connection.execute(
                text(
                    'INSERT INTO lan_tournaments (id, party_id, name, created_at) VALUES (:id, :party, :name, CURRENT_TIMESTAMP)'
                ),
                {'id': default_id, 'party': party.id, 'name': 'Server default'},
            )
            assert (
                connection.execute(
                    text('SELECT category FROM lan_tournaments WHERE id = :id'),
                    {'id': default_id},
                ).scalar_one()
                == 'MAIN'
            )
            assert (
                connection.execute(
                    text('SELECT category FROM lan_tournaments WHERE id = :id'),
                    {'id': regular.id},
                ).scalar_one()
                == 'MAIN'
            )
            assert (
                connection.execute(
                    text('SELECT category FROM lan_tournaments WHERE id = :id'),
                    {'id': requested.id},
                ).scalar_one()
                == 'USER_ORGANIZED'
            )
            connection.execute(
                text(
                    "UPDATE lan_tournaments SET category = 'MAIN' WHERE id = :id"
                ),
                {'id': requested.id},
            )
            connection.exec_driver_sql(_sql('024_add_tournament_category.sql'))
            assert (
                connection.execute(
                    text('SELECT category FROM lan_tournaments WHERE id = :id'),
                    {'id': requested.id},
                ).scalar_one()
                == 'MAIN'
            )
            for category in ('MAIN', 'FUN', 'STAGE', 'USER_ORGANIZED'):
                connection.execute(
                    text(
                        'UPDATE lan_tournaments SET category = :category WHERE id = :id'
                    ),
                    {'category': category, 'id': requested.id},
                )
            for invalid in ('OTHER', None):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        text(
                            'UPDATE lan_tournaments SET category = :category WHERE id = :id'
                        ),
                        {'category': invalid, 'id': requested.id},
                    )
            connection.exec_driver_sql(_sql('rollback_024.sql'))
            connection.exec_driver_sql(_sql('rollback_024.sql'))
            assert not connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid = 'lan_tournaments'::regclass AND attname = 'category' AND NOT attisdropped)"
                )
            ).scalar_one()
            for tournament_id, snapshot in snapshots.items():
                assert _snapshot(connection, tournament_id) == snapshot
        finally:
            transaction.rollback()
