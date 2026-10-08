from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from uuid import uuid4

import pytest
from sqlalchemy import (
    CheckConstraint,
    column,
    create_engine,
    func,
    select,
    table as sql_table,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from byceps.cli.commands.create_database_tables import _load_dbmodels
from byceps.config.converter import assemble_database_uri
from byceps.database import db
from byceps.services.lan_tournament import tournament_repository
from byceps.services.lan_tournament.dbmodels.dashboard import DbMatchDueEpisode


MIGRATIONS = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)
APPLY = '023_add_dashboard_operational_timing.sql'
ROLLBACK = 'rollback_023.sql'

EPISODES = 'lan_tournament_match_due_episodes'
ACKS = 'lan_tournament_match_escalation_acks'
ANNOTATIONS = 'lan_tournament_match_dashboard_annotations'
THRESHOLDS = 'lan_tournament_dashboard_party_thresholds'
HISTORY_TABLES = (ACKS, EPISODES, ANNOTATIONS, THRESHOLDS)

CLOCK_COLUMNS = {
    ('lan_tournaments', 'operational_clock_elapsed_us'),
    (EPISODES, 'opened_clock_us'),
    (EPISODES, 'closed_clock_us'),
    (ACKS, 'clock_us'),
}
ADDED_COLUMNS = {
    ('lan_tournaments', 'operational_clock_elapsed_us'),
    ('lan_tournaments', 'operational_clock_running_since'),
    ('lan_tournaments', 'operational_clock_activated_at'),
    ('lan_tournament_matches', 'last_changed_at'),
}
ELAPSED_CHECK = 'ck_lan_tournaments_operational_clock_elapsed_us'
OPEN_MATCH_INDEX = 'uq_lan_tournament_due_episodes_open_match'
OPEN_TOURNAMENT_INDEX = 'ix_lan_tournament_due_episodes_open_tournament'
CLOSED_MATCH_INDEX = 'ix_lan_tournament_due_episodes_closed_match'

NOW = datetime(2026, 10, 7, 12)
# Three days in microseconds, far above the ceiling of an INTEGER.
THREE_DAYS_US = 259_200_000_000


class Schemas(NamedTuple):
    target: str
    reference: str
    sibling: str


def lan_tables():
    _load_dbmodels()
    return [
        table
        for name, table in db.metadata.tables.items()
        if name.startswith('lan_tournament')
    ]


def use(connection, schema):
    connection.exec_driver_sql(f'SET search_path TO "{schema}"')


def run_script(connection, filename):
    # AUTOCOMMIT connection: the SQL script owns its BEGIN/COMMIT transaction.
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute((MIGRATIONS / filename).read_text())


def apply(connection):
    run_script(connection, APPLY)


def rollback(connection):
    run_script(connection, ROLLBACK)


def create_prerequisites(connection, schema, tables):
    use(connection, schema)
    connection.exec_driver_sql("""
        CREATE TABLE users (id UUID PRIMARY KEY);
        CREATE TABLE parties (id TEXT PRIMARY KEY);
    """)
    db.metadata.create_all(connection, tables=tables, checkfirst=False)


def create_sibling(connection, schema):
    # Same table, constraint and index names in another schema: relation-scoped
    # guards must not see them and the scripts must not touch them.
    use(connection, schema)
    connection.exec_driver_sql("""
        CREATE TABLE lan_tournaments (
            id UUID PRIMARY KEY,
            operational_clock_elapsed_us BIGINT NOT NULL DEFAULT 0,
            CONSTRAINT ck_lan_tournaments_operational_clock_elapsed_us
                CHECK (operational_clock_elapsed_us >= 0));
        CREATE TABLE lan_tournament_matches (
            id UUID PRIMARY KEY, last_changed_at TIMESTAMP);
        CREATE TABLE lan_tournament_match_due_episodes (
            sentinel INTEGER
                CONSTRAINT ck_lan_tournament_due_episodes_ack_revision
                CHECK (sentinel > 0));
        CREATE UNIQUE INDEX uq_lan_tournament_due_episodes_open_match
            ON lan_tournament_match_due_episodes (sentinel);
        CREATE INDEX ix_lan_tournament_due_episodes_open_tournament
            ON lan_tournament_match_due_episodes (sentinel);
        CREATE INDEX ix_lan_tournament_due_episodes_closed_match
            ON lan_tournament_match_due_episodes (sentinel);
        CREATE TABLE lan_tournament_match_escalation_acks (sentinel INTEGER);
        CREATE TABLE lan_tournament_match_dashboard_annotations (
            sentinel INTEGER);
        CREATE TABLE lan_tournament_dashboard_party_thresholds (
            sentinel INTEGER);
        INSERT INTO lan_tournament_match_due_episodes VALUES (7);
        INSERT INTO lan_tournament_match_escalation_acks VALUES (7);
        INSERT INTO lan_tournament_match_dashboard_annotations VALUES (7);
        INSERT INTO lan_tournament_dashboard_party_thresholds VALUES (7);
    """)


def drop_schema(connection, schema):
    assert schema.startswith('f03_issue4_')
    rows = connection.execute(
        text("""
            SELECT c.relname, con.conname
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND con.contype = 'f'
        """),
        {'schema': schema},
    ).all()
    for table, constraint in rows:
        connection.exec_driver_sql(
            f'ALTER TABLE "{schema}"."{table}" DROP CONSTRAINT "{constraint}"'
        )
    tables = (
        connection.execute(
            text('SELECT tablename FROM pg_tables WHERE schemaname = :schema'),
            {'schema': schema},
        )
        .scalars()
        .all()
    )
    for table in tables:
        connection.exec_driver_sql(f'DROP TABLE "{schema}"."{table}"')
    connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}"')


@pytest.fixture
def migration_db(database_config):
    # Fail closed: the fixture creates and drops schemas on this database.
    assert database_config.database.startswith('byceps_test')
    tables = lan_tables()
    engine = create_engine(assemble_database_uri(database_config))
    prefix = f'f03_issue4_{uuid4().hex}'
    schemas = Schemas(prefix, f'{prefix}_orm', f'{prefix}_sibling')
    new_tables = [t for t in tables if t.name in HISTORY_TABLES]
    old_tables = [t for t in tables if t.name not in HISTORY_TABLES]
    with engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as conn:
        assert (
            conn.scalar(text('SELECT current_database()'))
            == database_config.database
        )
        try:
            for schema in schemas:
                conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            # The reference is what `create_all` builds from the dbmodels.
            create_prerequisites(
                conn, schemas.reference, old_tables + new_tables
            )
            # The target is the same schema as it was before 023. The columns
            # and the check are removed here, not by the rollback script.
            create_prerequisites(conn, schemas.target, old_tables)
            conn.exec_driver_sql(f"""
                ALTER TABLE lan_tournaments
                    DROP CONSTRAINT {ELAPSED_CHECK},
                    DROP COLUMN operational_clock_activated_at,
                    DROP COLUMN operational_clock_running_since,
                    DROP COLUMN operational_clock_elapsed_us;
                ALTER TABLE lan_tournament_matches
                    DROP COLUMN last_changed_at;
            """)
            create_sibling(conn, schemas.sibling)
            use(conn, schemas.target)
            yield conn, schemas
        finally:
            # A script that failed halfway leaves its BEGIN open.
            conn.exec_driver_sql('ROLLBACK')
            for schema in schemas:
                drop_schema(conn, schema)
            conn.exec_driver_sql('RESET search_path')
    engine.dispose()


def snapshot(connection, schema):
    params = {'schema': schema}

    def unqualified(definition):
        return definition.replace(f'{schema}.', '')

    columns = {
        (r.table_name, r.column_name): (
            r.data_type,
            r.character_maximum_length,
            r.is_nullable,
            r.column_default,
        )
        for r in connection.execute(
            text("""
                SELECT table_name, column_name, data_type,
                    character_maximum_length, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = :schema
            """),
            params,
        )
    }
    constraints = {
        (r.table_name, r.name): (r.kind, unqualified(r.definition))
        for r in connection.execute(
            text("""
                SELECT c.relname AS table_name, con.conname AS name,
                    con.contype AS kind,
                    pg_get_constraintdef(con.oid) AS definition
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema
            """),
            params,
        )
    }
    indexes = {
        (r.tablename, r.indexname): unqualified(r.indexdef)
        for r in connection.execute(
            text("""
                SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE schemaname = :schema
            """),
            params,
        )
    }
    return {'columns': columns, 'constraints': constraints, 'indexes': indexes}


def assert_same_schema(actual, expected):
    for kind in ('columns', 'constraints', 'indexes'):
        assert actual[kind] == expected[kind], kind


def table_names(snap):
    return {table for table, _ in snap['columns']}


def constraint_names(snap, table, kind):
    return {
        name
        for (owner, name), (contype, _) in snap['constraints'].items()
        if owner == table and contype == kind
    }


def assert_rejected(connection, statement, params, constraint):
    with pytest.raises(IntegrityError) as excinfo:
        connection.execute(text(statement), params)
    assert excinfo.value.orig.diag.constraint_name == constraint


def seed_legacy(connection):
    connection.execute(
        text("INSERT INTO parties (id) VALUES ('party') ON CONFLICT DO NOTHING")
    )
    tournament_id, match_id = uuid4(), uuid4()
    connection.execute(
        text("""
            INSERT INTO lan_tournaments (id, party_id, name, created_at)
            VALUES (:id, 'party', 'Legacy', :now)
        """),
        {'id': tournament_id, 'now': NOW},
    )
    connection.execute(
        text("""
            INSERT INTO lan_tournament_matches (id, tournament_id, created_at)
            VALUES (:id, :tournament, :now)
        """),
        {'id': match_id, 'tournament': tournament_id, 'now': NOW},
    )
    return tournament_id, match_id


def insert_episode(
    connection,
    tournament_id,
    match_id,
    *,
    opened_clock_us=0,
    closed_clock_us=None,
):
    episode_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO lan_tournament_match_due_episodes (id, tournament_id,
                match_id, pairing_key,
                opened_at, opened_clock_us, closed_at, closed_clock_us)
            VALUES (:id, :tournament, :match, 'key', :now, :opened,
                :closed_at, :closed)
        """),
        {
            'id': episode_id,
            'tournament': tournament_id,
            'match': match_id,
            'now': NOW,
            'opened': opened_clock_us,
            'closed_at': None if closed_clock_us is None else NOW,
            'closed': closed_clock_us,
        },
    )
    return episode_id


def insert_ack(connection, episode_id, tournament_id, match_id, *, clock_us=0):
    connection.execute(
        text("""
            INSERT INTO lan_tournament_match_escalation_acks (id, episode_id,
                tournament_id, match_id, actor_id, revision, occurred_at,
                clock_us)
            VALUES (:id, :episode, :tournament, :match, :actor, 1, :now,
                :clock)
        """),
        {
            'id': uuid4(),
            'episode': episode_id,
            'tournament': tournament_id,
            'match': match_id,
            'actor': uuid4(),
            'now': NOW,
            'clock': clock_us,
        },
    )


def insert_annotation(connection, tournament_id, match_id):
    connection.execute(
        text("""
            INSERT INTO lan_tournament_match_dashboard_annotations
                (match_id, tournament_id, updated_at, updated_by)
            VALUES (:match, :tournament, :now, :actor)
        """),
        {
            'match': match_id,
            'tournament': tournament_id,
            'now': NOW,
            'actor': uuid4(),
        },
    )


def insert_threshold(connection, party_id, yellow, red, revision=1):
    connection.execute(
        text("""
            INSERT INTO lan_tournament_dashboard_party_thresholds (party_id,
                yellow_minutes, red_minutes, revision, updated_at, updated_by)
            VALUES (:party, :yellow, :red, :revision, :now, :actor)
        """),
        {
            'party': party_id,
            'yellow': yellow,
            'red': red,
            'revision': revision,
            'now': NOW,
            'actor': uuid4(),
        },
    )


def seed_history(connection, tournament_id, match_id):
    episode_id = insert_episode(connection, tournament_id, match_id)
    insert_ack(connection, episode_id, tournament_id, match_id)
    insert_annotation(connection, tournament_id, match_id)
    insert_threshold(connection, 'party', 10, 20)


def seed_episode_volume(connection, tournament_id, match_id):
    # The wanted tournament and match among many others, with fresh statistics.
    for _ in range(20):
        other_tournament, other_match = uuid4(), uuid4()
        for _ in range(4):
            insert_episode(connection, other_tournament, uuid4())
            insert_episode(connection, uuid4(), other_match, closed_clock_us=1)
    for _ in range(4):
        insert_episode(connection, tournament_id, uuid4())
        insert_episode(connection, uuid4(), match_id, closed_clock_us=1)
    connection.exec_driver_sql(f'ANALYZE {EPISODES}')


def explain(connection, statement):
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}
        )
    )
    # SET LOCAL needs a transaction block, and this connection autocommits.
    connection.exec_driver_sql('BEGIN')
    try:
        connection.exec_driver_sql('SET LOCAL enable_seqscan = off')
        plan = connection.exec_driver_sql(f'EXPLAIN {sql}').scalars().all()
    finally:
        connection.exec_driver_sql('ROLLBACK')
    return '\n'.join(plan)


def count_rows(connection, name):
    return connection.scalar(select(func.count()).select_from(sql_table(name)))


def dump_rows(connection, name):
    result = connection.execute(
        select(text('*')).select_from(sql_table(name)).order_by(text('id'))
    )
    return [dict(row) for row in result.mappings()]


def test_schema_matches_orm(migration_db):
    conn, schemas = migration_db
    before = snapshot(conn, schemas.target)
    assert not ADDED_COLUMNS & set(before['columns'])
    assert not set(HISTORY_TABLES) & table_names(before)

    apply(conn)

    migrated = snapshot(conn, schemas.target)
    reference = snapshot(conn, schemas.reference)
    assert set(HISTORY_TABLES) <= table_names(migrated)
    assert ADDED_COLUMNS <= set(migrated['columns'])
    assert_same_schema(migrated, reference)

    # Do not rely on the comparison alone: the ORM metadata names the checks.
    for table in (t for t in lan_tables() if t.name in HISTORY_TABLES):
        expected = {
            c.name for c in table.constraints if isinstance(c, CheckConstraint)
        }
        assert expected
        assert constraint_names(migrated, table.name, 'c') == expected
    assert (
        migrated['indexes'][(EPISODES, OPEN_MATCH_INDEX)]
        == f'CREATE UNIQUE INDEX {OPEN_MATCH_INDEX} '
        f'ON {EPISODES} USING btree (match_id) WHERE (closed_at IS NULL)'
    )
    assert (
        migrated['indexes'][(EPISODES, OPEN_TOURNAMENT_INDEX)]
        == f'CREATE INDEX {OPEN_TOURNAMENT_INDEX} '
        f'ON {EPISODES} USING btree (tournament_id) WHERE (closed_at IS NULL)'
    )
    assert (
        migrated['indexes'][(EPISODES, CLOSED_MATCH_INDEX)]
        == f'CREATE INDEX {CLOSED_MATCH_INDEX} '
        f'ON {EPISODES} USING btree (match_id) WHERE (closed_at IS NOT NULL)'
    )
    assert (
        'lan_tournaments',
        ELAPSED_CHECK,
    ) in migrated['constraints']
    assert migrated['columns'][
        ('lan_tournaments', 'operational_clock_elapsed_us')
    ][2:] == ('NO', "'0'::bigint")
    assert migrated['columns'][('lan_tournament_matches', 'last_changed_at')][
        2:
    ] == ('YES', None)


def test_due_episode_lookups_use_the_partial_indexes(migration_db, monkeypatch):
    conn, schemas = migration_db
    apply(conn)
    tournament_id, match_id = uuid4(), uuid4()

    # The statement the repository runs inside every result write.
    statements = []

    def scalars(statement):
        statements.append(statement)
        return SimpleNamespace(all=list)

    with monkeypatch.context() as patched:
        patched.setattr(
            tournament_repository,
            'db',
            SimpleNamespace(session=SimpleNamespace(scalars=scalars)),
        )
        tournament_repository.list_open_due_episodes(tournament_id)
    [open_of_tournament] = statements
    # The prior-episode facts of every dashboard fixture.
    closed_of_match = select(DbMatchDueEpisode.id).where(
        DbMatchDueEpisode.match_id == match_id,
        DbMatchDueEpisode.closed_at.is_not(None),
    )

    # Both the migrated schema and the one `create_all` builds from the
    # dbmodels must serve the lookups.
    for schema in (schemas.target, schemas.reference):
        use(conn, schema)
        seed_episode_volume(conn, tournament_id, match_id)

        open_plan = explain(conn, open_of_tournament)
        closed_plan = explain(conn, closed_of_match)

        assert OPEN_TOURNAMENT_INDEX in open_plan, (schema, open_plan)
        assert CLOSED_MATCH_INDEX in closed_plan, (schema, closed_plan)


def test_apply_and_reapply_are_idempotent(migration_db):
    conn, schemas = migration_db
    tournament_id, match_id = seed_legacy(conn)

    apply(conn)
    first = snapshot(conn, schemas.target)

    # Unknown history stays unknown: no clock is invented, nothing backfilled.
    tournament = conn.execute(
        text("""
            SELECT operational_clock_elapsed_us AS elapsed,
                operational_clock_running_since AS running_since,
                operational_clock_activated_at AS activated_at
            FROM lan_tournaments WHERE id = :id
        """),
        {'id': tournament_id},
    ).one()
    assert tuple(tournament) == (0, None, None)
    assert (
        conn.scalar(
            text('SELECT last_changed_at FROM lan_tournament_matches'),
        )
        is None
    )

    # Facts written by later code survive a second apply.
    conn.execute(
        text("""
            UPDATE lan_tournaments SET operational_clock_elapsed_us = :us,
                operational_clock_running_since = :now,
                operational_clock_activated_at = :now
        """),
        {'us': THREE_DAYS_US, 'now': NOW},
    )
    conn.execute(
        text('UPDATE lan_tournament_matches SET last_changed_at = :now'),
        {'now': NOW},
    )
    seed_history(conn, tournament_id, match_id)

    apply(conn)

    assert_same_schema(snapshot(conn, schemas.target), first)
    assert_same_schema(
        snapshot(conn, schemas.target), snapshot(conn, schemas.reference)
    )
    assert conn.execute(
        text("""
            SELECT operational_clock_elapsed_us, operational_clock_running_since,
                operational_clock_activated_at FROM lan_tournaments
        """)
    ).one() == (THREE_DAYS_US, NOW, NOW)
    assert (
        conn.scalar(text('SELECT last_changed_at FROM lan_tournament_matches'))
        == NOW
    )
    for table in HISTORY_TABLES:
        assert count_rows(conn, table) == 1, table


def test_rollback_and_repeat_rollback_are_scoped(migration_db):
    conn, schemas = migration_db
    tournament_id, match_id = seed_legacy(conn)
    before = snapshot(conn, schemas.target)
    sibling_before = snapshot(conn, schemas.sibling)
    legacy_before = {
        table: dump_rows(conn, table)
        for table in ('lan_tournaments', 'lan_tournament_matches')
    }
    unrelated = set(before['columns']) - ADDED_COLUMNS
    assert len(table_names(before)) > 10

    apply(conn)
    # Acknowledgements reference their episode: the drop order must cope.
    seed_history(conn, tournament_id, match_id)
    rollback(conn)
    rollback(conn)

    # Every other table, column, check, foreign key and index is as before.
    after = snapshot(conn, schemas.target)
    assert_same_schema(after, before)
    assert not set(HISTORY_TABLES) & table_names(after)
    assert not ADDED_COLUMNS & set(after['columns'])
    assert unrelated == set(after['columns'])
    assert {
        table: dump_rows(conn, table)
        for table in ('lan_tournaments', 'lan_tournament_matches')
    } == legacy_before

    # The same-named objects of the sibling schema were never touched.
    assert_same_schema(snapshot(conn, schemas.sibling), sibling_before)
    for table in HISTORY_TABLES:
        sentinel = select(column('sentinel')).select_from(
            sql_table(table, schema=schemas.sibling)
        )
        assert conn.scalar(sentinel) == 7, table
    assert (
        'lan_tournaments',
        ELAPSED_CHECK,
    ) in snapshot(conn, schemas.sibling)['constraints']

    # A rolled back database can take the migration again.
    apply(conn)
    assert_same_schema(
        snapshot(conn, schemas.target), snapshot(conn, schemas.reference)
    )


def test_history_has_no_live_match_or_tournament_fk(migration_db):
    conn, schemas = migration_db
    apply(conn)

    foreign_keys = conn.execute(
        text("""
            SELECT c.relname AS source, rc.relname AS target,
                con.confdeltype AS on_delete, con.confupdtype AS on_update
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_class rc ON rc.oid = con.confrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND con.contype = 'f'
        """),
        {'schema': schemas.target},
    ).all()
    # Only the acknowledgement points at a retained episode, without cascade.
    assert {
        tuple(fk) for fk in foreign_keys if fk.source in HISTORY_TABLES
    } == {(ACKS, EPISODES, 'a', 'a')}
    assert {
        fk.source for fk in foreign_keys if fk.target in HISTORY_TABLES
    } == {ACKS}

    # Facts about matches, tournaments and actors that do not exist are valid.
    seed_history(conn, uuid4(), uuid4())

    # Deleting or regenerating live rows is not blocked and keeps the history.
    tournament_id, match_id = seed_legacy(conn)
    episode_id = insert_episode(conn, tournament_id, match_id)
    insert_ack(conn, episode_id, tournament_id, match_id)
    insert_annotation(conn, tournament_id, match_id)
    conn.execute(
        text('DELETE FROM lan_tournament_matches WHERE id = :id'),
        {'id': match_id},
    )
    conn.execute(
        text('DELETE FROM lan_tournaments WHERE id = :id'),
        {'id': tournament_id},
    )
    for table in (EPISODES, ACKS, ANNOTATIONS):
        assert count_rows(conn, table) == 2, table


def test_clock_columns_are_bigint(migration_db):
    conn, schemas = migration_db
    apply(conn)

    for schema in (schemas.target, schemas.reference):
        columns = conn.execute(
            text("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = :schema AND column_name::text ~ '_us$'
            """),
            {'schema': schema},
        ).all()
        assert {(c.table_name, c.column_name) for c in columns} == CLOCK_COLUMNS
        assert {c.data_type for c in columns} == {'bigint'}, schema

    # Values above the 32-bit ceiling are stored, and the checks hold.
    tournament_id, match_id = seed_legacy(conn)
    conn.execute(
        text("""
            UPDATE lan_tournaments SET operational_clock_elapsed_us = :us
            WHERE id = :id
        """),
        {'us': THREE_DAYS_US, 'id': tournament_id},
    )
    episode_id = insert_episode(
        conn,
        tournament_id,
        match_id,
        opened_clock_us=THREE_DAYS_US,
        closed_clock_us=THREE_DAYS_US + 1,
    )
    insert_ack(
        conn, episode_id, tournament_id, match_id, clock_us=THREE_DAYS_US + 2
    )
    assert (
        conn.scalar(
            text('SELECT operational_clock_elapsed_us FROM lan_tournaments')
        )
        == THREE_DAYS_US
    )
    assert conn.execute(
        text(
            'SELECT opened_clock_us, closed_clock_us'
            ' FROM lan_tournament_match_due_episodes'
        )
    ).one() == (THREE_DAYS_US, THREE_DAYS_US + 1)
    assert conn.scalar(
        text('SELECT clock_us FROM lan_tournament_match_escalation_acks')
    ) == (THREE_DAYS_US + 2)
    assert_rejected(
        conn,
        'UPDATE lan_tournaments SET operational_clock_elapsed_us = -1',
        {},
        ELAPSED_CHECK,
    )
    assert_rejected(
        conn,
        'UPDATE lan_tournament_match_due_episodes SET opened_clock_us = -1',
        {},
        'ck_lan_tournament_due_episodes_opened_clock_us',
    )
    assert_rejected(
        conn,
        'UPDATE lan_tournament_match_due_episodes SET closed_clock_us = -1',
        {},
        'ck_lan_tournament_due_episodes_closed_clock_us',
    )
    assert_rejected(
        conn,
        'UPDATE lan_tournament_match_escalation_acks SET clock_us = -1',
        {},
        'ck_lan_tournament_escalation_ack_clock_us',
    )

    # The threshold checks are present, named and enforced.
    migrated = snapshot(conn, schemas.target)
    prefix = 'ck_lan_tournament_dashboard_party_thresholds_'
    assert constraint_names(migrated, THRESHOLDS, 'c') == {
        prefix + 'yellow_min',
        prefix + 'order',
        prefix + 'red_max',
        prefix + 'revision',
    }
    insert_threshold(conn, 'lowest', 1, 2)
    insert_threshold(conn, 'highest', 1439, 1440)
    for yellow, red, revision, constraint in (
        (0, 5, 1, 'yellow_min'),
        (10, 10, 1, 'order'),
        (10, 5, 1, 'order'),
        (5, 1441, 1, 'red_max'),
        (5, 10, 0, 'revision'),
    ):
        with pytest.raises(IntegrityError) as excinfo:
            insert_threshold(conn, 'rejected', yellow, red, revision)
        assert excinfo.value.orig.diag.constraint_name == prefix + constraint
    assert count_rows(conn, THRESHOLDS) == 2


@pytest.mark.parametrize('filename', [APPLY, ROLLBACK])
def test_scripts_are_atomic_and_never_cascade(filename):
    lines = (MIGRATIONS / filename).read_text().splitlines()
    code = '\n'.join(line for line in lines if not line.startswith('--'))
    assert code.strip().startswith('BEGIN;')
    assert code.strip().endswith('COMMIT;')
    assert "SET LOCAL lock_timeout = '5s';" in code
    assert 'CASCADE' not in code.upper()
    assert 'SET NULL' not in code.upper()


def test_readme_documents_023():
    readme = (MIGRATIONS / 'README.md').read_text()
    section = readme.split('### 023_add_dashboard_operational_timing.sql', 1)[1]
    section = section.split('## Pre-Application Checklist', 1)[0]
    for required in (
        'Human approval',
        'BIGINT',
        'last_changed_at',
        'unknown',
        "lock_timeout = '5s'",
        'Rollback is data-losing',
        'psql -v ON_ERROR_STOP=1',
        'migrations/023_add_dashboard_operational_timing.sql',
        'migrations/rollback_023.sql',
        'test_migration_dashboard_timing.py',
    ):
        assert required in section, required
