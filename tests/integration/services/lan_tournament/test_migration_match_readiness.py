"""Execute 022 against private PostgreSQL, not SQL-text or SQLite substitutes."""

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.schema import CreateTable

from byceps.cli.commands.create_database_tables import _load_dbmodels
from byceps.config.converter import assemble_database_uri
from byceps.database import db
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)


MIGRATIONS = Path('byceps/services/lan_tournament/migrations')
NEW_COLUMNS = {
    'pairing_generation',
    'readiness_revision',
    'pairing_id',
    'invitation_hold_a',
    'invitation_hold_b',
}
DROPPED_REVOCATION_COLUMNS = {
    'ready_revoked_at',
    'ready_revoked_by',
    'ready_revoked_reason',
    'ready_revoked_side',
    'ready_revoked_role',
}
TABLES = [
    DbMatchPairing.__table__,
    DbMatchInvitation.__table__,
    DbTournamentMatch.__table__,
]
NOW = datetime(2026, 10, 5, 10)


def execute_script(connection, filename):
    # AUTOCOMMIT connection: the SQL script owns its BEGIN/COMMIT transaction.
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute((MIGRATIONS / filename).read_text())


def apply(connection):
    execute_script(connection, '022_add_match_readiness_integrity.sql')


def rollback(connection):
    execute_script(connection, 'rollback_022.sql')


def snapshot(connection, schema, table):
    inspector = inspect(connection)
    return {
        'columns': {
            c['name']: (str(c['type']), c['nullable'], c['default'])
            for c in inspector.get_columns(table, schema=schema)
        },
        'checks': {
            c['name']: c['sqltext']
            for c in inspector.get_check_constraints(table, schema=schema)
        },
        'unique': {
            c['name']: c['column_names']
            for c in inspector.get_unique_constraints(table, schema=schema)
        },
        'indexes': {
            c['name']: (c['column_names'], c['unique'])
            for c in inspector.get_indexes(table, schema=schema)
        },
        'fks': {
            c['name']: (
                c['constrained_columns'],
                c['referred_table'],
                c['referred_columns'],
                c['options'],
            )
            for c in inspector.get_foreign_keys(table, schema=schema)
        },
        'pk': inspector.get_pk_constraint(table, schema=schema)[
            'constrained_columns'
        ],
    }


def assert_sibling_untouched(connection, schema):
    for name, value in (
        ('lan_tournament_match_pairings', 7),
        ('lan_tournament_match_invitations', 8),
    ):
        table = Table(name, MetaData(), schema=schema, autoload_with=connection)
        assert connection.scalar(select(table.c.sentinel)) == value


@pytest.fixture
def migration_db(database_config):
    # Deliberately does NOT request the destructive inherited database fixture.
    # Fail closed unless the approved private preflight identity is selected.
    assert database_config.host == '127.0.0.1'
    assert database_config.port == 55484
    assert database_config.database == 'byceps_test_prd_fixes'
    engine = create_engine(assemble_database_uri(database_config))
    prefix = f'f04_issue3_{uuid4().hex}'
    schemas = [prefix, f'{prefix}_orm', f'{prefix}_unrelated']
    _load_dbmodels()
    with engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as conn:
        assert (
            conn.scalar(text('SELECT current_database()'))
            == database_config.database
        )
        assert conn.scalar(text('SELECT inet_server_port()')) == 55484
        try:
            for schema in schemas:
                conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            for schema in schemas[:2]:
                conn.exec_driver_sql(f'SET search_path TO "{schema}"')
                # Reduced prerequisite fixture: only columns used by 022. Match,
                # pair, work and contestant DDL come from actual ORM metadata.
                conn.exec_driver_sql("""
                    CREATE TABLE users (id UUID PRIMARY KEY);
                    CREATE TABLE lan_tournaments (
                        id UUID PRIMARY KEY, game_format TEXT,
                        playoff_game_format TEXT, tournament_status TEXT);
                    CREATE TABLE lan_tournament_teams (
                        id UUID PRIMARY KEY,
                        tournament_id UUID REFERENCES lan_tournaments(id),
                        removed_at TIMESTAMP);
                    CREATE TABLE lan_tournament_participants (
                        id UUID PRIMARY KEY,
                        tournament_id UUID REFERENCES lan_tournaments(id),
                        user_id UUID REFERENCES users(id),
                        team_id UUID REFERENCES lan_tournament_teams(id),
                        removed_at TIMESTAMP);
                """)
                unrelated_tables = [
                    db.metadata.tables[name]
                    for name in (
                        'lan_tournament_orgas',
                        'lan_tournament_log_entries',
                        'lan_tournament_seedings',
                        'lan_tournament_qualification_decisions',
                    )
                ]
                for table in (
                    TABLES
                    + [DbTournamentMatchToContestant.__table__]
                    + unrelated_tables
                ):
                    conn.execute(CreateTable(table))
                    for index in table.indexes:
                        index.create(conn)
            conn.exec_driver_sql(f'SET search_path TO "{schemas[2]}"')
            # Same names in a sibling schema must neither satisfy guards nor be
            # touched by rollback. Include a same-named constraint as well.
            conn.exec_driver_sql("""
                CREATE TABLE lan_tournament_match_pairings (
                    sentinel INTEGER CONSTRAINT ck_lan_tournament_match_pairings_generation
                    CHECK (sentinel > 0));
                INSERT INTO lan_tournament_match_pairings VALUES (7);
                CREATE TABLE lan_tournament_match_invitations (sentinel INTEGER);
                INSERT INTO lan_tournament_match_invitations VALUES (8);
            """)
            conn.exec_driver_sql(f'SET search_path TO "{prefix}"')
            rollback(conn)
            yield conn, schemas
        finally:
            conn.exec_driver_sql('ROLLBACK')
            # Only schemas created by THIS fixture. No CASCADE, reflection-wide
            # drop_all or cleanup of public/sibling application data.
            for schema in schemas:
                conn.exec_driver_sql(f'SET search_path TO "{schema}"')
                for table in (
                    'lan_tournament_match_contestants',
                    'lan_tournament_matches',
                    'lan_tournament_match_invitations',
                    'lan_tournament_match_pairings',
                    'lan_tournament_orgas',
                    'lan_tournament_log_entries',
                    'lan_tournament_seedings',
                    'lan_tournament_qualification_decisions',
                    'lan_tournament_participants',
                    'lan_tournament_teams',
                    'lan_tournaments',
                    'users',
                ):
                    conn.exec_driver_sql(
                        f'DROP TABLE IF EXISTS "{schema}"."{table}"'
                    )
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}"')
    engine.dispose()


def test_schema_matches_orm(migration_db):
    conn, (schema, reference, _) = migration_db
    apply(conn)
    for table in TABLES:
        actual = snapshot(conn, schema, table.name)
        expected = snapshot(conn, reference, table.name)
        for name, facts in expected['columns'].items():
            assert actual['columns'][name] == facts, name
        assert actual == expected
        assert set(actual['columns']) == set(table.columns.keys())
        for column in table.columns:
            reflected_type, nullable, _ = actual['columns'][column.name]
            expected_type = str(column.type.compile(dialect=conn.dialect))
            assert reflected_type == expected_type.replace(
                ' WITHOUT TIME ZONE', ''
            )
            assert nullable == column.nullable
        assert set(actual['checks']) == {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == 'CheckConstraint'
        }
        # UUID defaults are application-side callables, intentionally absent in SQL.
        assert actual['columns']['id'][2] is None
        assert callable(table.c.id.default.arg)
        print(
            f'{table.name}: columns={len(actual["columns"])}, '
            f'checks={len(actual["checks"])}, unique={len(actual["unique"])}, '
            f'indexes={len(actual["indexes"])}, FKs={len(actual["fks"])}'
        )
    print(
        'PostgreSQL parity: all match/pair/work columns, checks, defaults, '
        'unique constraints, indexes, primary keys and FKs match ORM DDL'
    )


def test_apply_and_reapply(migration_db):
    conn, (schema, _, sibling) = migration_db
    before = snapshot(conn, schema, 'lan_tournament_matches')
    apply(conn)
    first = {t.name: snapshot(conn, schema, t.name) for t in TABLES}
    apply(conn)
    assert first == {t.name: snapshot(conn, schema, t.name) for t in TABLES}
    for name, facts in before['columns'].items():
        assert first['lan_tournament_matches']['columns'][name] == facts
    assert_sibling_untouched(conn, sibling)


def test_rollback_and_repeat_are_scoped(migration_db):
    conn, (schema, _, sibling) = migration_db
    baseline = {
        name: snapshot(conn, schema, name)
        for name in (
            'lan_tournament_matches',
            'lan_tournament_orgas',
            'lan_tournament_log_entries',
            'lan_tournament_seedings',
            'lan_tournament_qualification_decisions',
        )
    }
    apply(conn)
    rollback(conn)
    rollback(conn)
    for name, facts in baseline.items():
        assert snapshot(conn, schema, name) == facts
    assert not NEW_COLUMNS.intersection(
        baseline['lan_tournament_matches']['columns']
    )
    assert 'ready_at_a' in baseline['lan_tournament_matches']['columns']
    assert not DROPPED_REVOCATION_COLUMNS.intersection(
        baseline['lan_tournament_matches']['columns']
    )
    assert not inspect(conn).has_table(
        'lan_tournament_match_pairings', schema=schema
    )
    assert not inspect(conn).has_table(
        'lan_tournament_match_invitations', schema=schema
    )
    assert_sibling_untouched(conn, sibling)
    apply(conn)
    assert inspect(conn).has_table(
        'lan_tournament_match_pairings', schema=schema
    )
    print(
        'Apply/reapply/rollback/repeat/reapply executed; all pre-022 match '
        'columns/checks/indexes/FKs and real orga/audit/seeding/qualification '
        'DDL preserved; sibling same-named tables/constraint preserved'
    )


def test_readme_documents_approved_commands():
    readme = (MIGRATIONS / 'README.md').read_text()
    section = readme.split('### 022_add_match_readiness_integrity.sql', 1)[1]
    section = section.split('## Pre-Application Checklist', 1)[0]
    for required in (
        'Human approval',
        'activation or rollback',
        'BEGIN',
        'COMMIT',
        "lock_timeout = '5s'",
        'highest migration number',
        'never deployed',
        'actual start',
        'delivery_unknown',
        'Rollback is data-losing',
        'rollback_014.sql',
        'docker compose stop web worker',
        'docker compose start web worker',
        'psql -v ON_ERROR_STOP=1',
        'systemctl stop byceps-web byceps-worker',
        'systemctl start byceps-web byceps-worker',
        'migrations/022_add_match_readiness_integrity.sql',
        'migrations/rollback_022.sql',
        'integration.lock',
    ):
        assert required in section, required


def seed_match(
    conn,
    *,
    game_format='ONE_V_ONE',
    playoff_format=None,
    phase=1,
    status='ONGOING',
    occupied=None,
    placeholder=False,
    team=False,
    removed=False,
    confirmed=False,
    duplicate=False,
):
    tournament_id, match_id = uuid4(), uuid4()
    conn.execute(
        text(
            """INSERT INTO lan_tournaments VALUES (:id, :format, :playoff, :status)"""
        ),
        {
            'id': tournament_id,
            'format': game_format,
            'playoff': playoff_format,
            'status': status,
        },
    )
    user_ids = [uuid4() for _ in range(4 if team else 2)]
    for user_id in user_ids:
        conn.execute(text('INSERT INTO users VALUES (:id)'), {'id': user_id})
    conn.execute(
        text("""INSERT INTO lan_tournament_matches
        (id, tournament_id, created_at, phase, occupied_since, confirmed_by)
        VALUES (:id, :tournament, :created, :phase, :occupied, :confirmed)"""),
        {
            'id': match_id,
            'tournament': tournament_id,
            'created': NOW,
            'phase': phase,
            'occupied': occupied,
            'confirmed': user_ids[0] if confirmed else None,
        },
    )
    identities = [UUID(int=2), UUID(int=1)] if duplicate else [uuid4(), uuid4()]
    if duplicate:
        identities[1] = identities[0]
    for index, identity in enumerate(dict.fromkeys(identities)):
        if team:
            conn.execute(
                text(
                    'INSERT INTO lan_tournament_teams VALUES (:id, :tournament, NULL)'
                ),
                {'id': identity, 'tournament': tournament_id},
            )
        for user_id in (
            user_ids[index * 2 : index * 2 + 2] if team else [user_ids[index]]
        ):
            conn.execute(
                text("""INSERT INTO lan_tournament_participants
                VALUES (:id, :tournament, :user, :team, :removed)"""),
                {
                    'id': uuid4() if team else identity,
                    'tournament': tournament_id,
                    'user': user_id,
                    'team': identity if team else None,
                    'removed': NOW if removed else None,
                },
            )
    # Same creation times deliberately force the UUID tie-breaker; insert B first.
    for index in (1, 0):
        conn.execute(
            text("""INSERT INTO lan_tournament_match_contestants
            (id, tournament_match_id, participant_id, team_id, created_at)
            VALUES (:id, :match, :participant, :team, :created)"""),
            {
                'id': UUID(int=index + 1),
                'match': match_id,
                'participant': identities[index] if not team else None,
                'team': identities[index] if team else None,
                'created': NOW,
            },
        )
    if placeholder:
        # A legacy DEFWIN/placeholder slot references neither real identity.
        # Old databases can contain these; loosen only this owned fixture schema.
        conn.exec_driver_sql(
            'ALTER TABLE lan_tournament_match_contestants '
            'DROP CONSTRAINT IF EXISTS ck_exactly_one_contestant'
        )
        conn.execute(
            text("""UPDATE lan_tournament_match_contestants
            SET participant_id = NULL, team_id = NULL
            WHERE tournament_match_id = :id AND id = :side"""),
            {'id': match_id, 'side': UUID(int=2)},
        )
    # Avoid side-row PK collisions across matches without changing relative order.
    rows = (
        conn.execute(
            text("""SELECT id FROM lan_tournament_match_contestants
        WHERE tournament_match_id = :id ORDER BY id"""),
            {'id': match_id},
        )
        .scalars()
        .all()
    )
    offset = uuid4().int >> 32
    for index, old in enumerate(rows):
        conn.execute(
            text(
                'UPDATE lan_tournament_match_contestants SET id = :new WHERE id = :old'
            ),
            {'new': UUID(int=offset + index), 'old': old},
        )
    return match_id, tournament_id, identities, user_ids


def test_backfill_uses_effective_format_and_unknown_history(migration_db):
    conn, _ = migration_db
    unknown, _, sides, audience = seed_match(conn)
    known, _, _, _ = seed_match(conn, occupied=datetime(2026, 10, 5, 9))
    playoff, _, _, _ = seed_match(
        conn, game_format='FREE_FOR_ALL', playoff_format='ONE_V_ONE', phase=2
    )
    prestart, _, _, _ = seed_match(conn, status='SCHEDULED')
    team, _, _, team_audience = seed_match(conn, team=True)
    completed, _, _, _ = seed_match(conn, confirmed=True)
    excluded = [
        seed_match(conn, **kwargs)[0]
        for kwargs in (
            {'placeholder': True},
            {'game_format': 'FREE_FOR_ALL'},
            {'playoff_format': 'FREE_FOR_ALL', 'phase': 2},
            {'phase': 2},
            {'removed': True},
            {'duplicate': True},
        )
    ]
    apply(conn)
    pairs = {
        row.match_id: row
        for row in conn.execute(
            text('SELECT * FROM lan_tournament_match_pairings')
        )
    }
    assert set(pairs) == {unknown, known, playoff, prestart, team, completed}
    assert not set(excluded).intersection(pairs)
    assert pairs[unknown].started_at is None
    assert pairs[unknown].side_a_id == sides[0]
    assert pairs[unknown].side_b_id == sides[1]
    assert (
        pairs[known].started_at == NOW
    )  # replacement side timing, not old occupancy
    assert pairs[known].ended_at is None
    work = conn.execute(
        text('SELECT * FROM lan_tournament_match_invitations')
    ).all()
    assert {r.recipient_id for r in work if r.match_id == unknown} == set(
        audience
    )
    assert {r.recipient_id for r in work if r.match_id == team} == set(
        team_audience
    )
    assert not any(r.match_id in (prestart, completed) for r in work)
    assert all(
        r.status == 'delivery_unknown'
        and r.attempts == 0
        and r.accepted_at is None
        and r.lease_until is None
        and r.dispatch_token is None
        and r.next_attempt_at is None
        for r in work
    )
    assert (
        conn.scalar(
            text("""SELECT count(*) FROM lan_tournament_matches
        WHERE ready_at_a IS NOT NULL OR ready_at_b IS NOT NULL
        OR ready_by_a IS NOT NULL OR ready_by_b IS NOT NULL
        OR both_ready_notified_at IS NOT NULL""")
        )
        == 0
    )
    assert conn.scalar(
        text(
            'SELECT occupied_since FROM lan_tournament_matches WHERE id = :id'
        ),
        {'id': known},
    ) == datetime(2026, 10, 5, 9)
    # Reapply must preserve evidence entered by repaired code, not turn accepted
    # recipients back into historical unknown or rewrite a pair snapshot.
    conn.execute(
        text("""UPDATE lan_tournament_match_invitations
        SET status = 'accepted', accepted_at = :now, attempts = 2 WHERE id = :id"""),
        {'now': NOW, 'id': work[0].id},
    )
    pair_ids = {m: p.id for m, p in pairs.items()}
    apply(conn)
    assert {
        r.match_id: r.id
        for r in conn.execute(
            text('SELECT * FROM lan_tournament_match_pairings')
        )
    } == pair_ids
    accepted = conn.execute(
        text('SELECT * FROM lan_tournament_match_invitations WHERE id = :id'),
        {'id': work[0].id},
    ).one()
    assert (accepted.status, accepted.accepted_at, accepted.attempts) == (
        'accepted',
        NOW,
        2,
    )


def test_retained_history_has_no_live_row_fks(migration_db):
    conn, (schema, _, _) = migration_db
    match_id, tournament_id, _, _ = seed_match(conn, team=True)
    apply(conn)
    before_pairs = conn.execute(
        text('SELECT * FROM lan_tournament_match_pairings')
    ).all()
    before_work = conn.execute(
        text('SELECT * FROM lan_tournament_match_invitations')
    ).all()
    assert (
        inspect(conn).get_foreign_keys(
            'lan_tournament_match_pairings', schema=schema
        )
        == []
    )
    work_fks = inspect(conn).get_foreign_keys(
        'lan_tournament_match_invitations', schema=schema
    )
    assert [
        (fk['constrained_columns'], fk['referred_table']) for fk in work_fks
    ] == [(['recipient_id'], 'users')]
    for table, column, value in (
        ('lan_tournament_match_contestants', 'tournament_match_id', match_id),
        ('lan_tournament_matches', 'id', match_id),
        ('lan_tournament_participants', 'tournament_id', tournament_id),
        ('lan_tournament_teams', 'tournament_id', tournament_id),
        ('lan_tournaments', 'id', tournament_id),
    ):
        live_table = Table(table, MetaData(), schema=schema, autoload_with=conn)
        conn.execute(live_table.delete().where(live_table.c[column] == value))
    assert (
        conn.execute(text('SELECT * FROM lan_tournament_match_pairings')).all()
        == before_pairs
    )
    assert (
        conn.execute(
            text('SELECT * FROM lan_tournament_match_invitations')
        ).all()
        == before_work
    )
    replacement, _, _, _ = seed_match(conn)
    apply(conn)
    assert (
        conn.scalar(text('SELECT count(*) FROM lan_tournament_match_pairings'))
        == 2
    )
    assert (
        conn.scalar(
            text(
                'SELECT pairing_id FROM lan_tournament_matches WHERE id = :id'
            ),
            {'id': replacement},
        )
        != before_pairs[0].id
    )
    print(
        'Deletion/regeneration executed: pair history retained, four team '
        'recipient work rows retained; pair FKs=0, work FKs=recipient users only'
    )
