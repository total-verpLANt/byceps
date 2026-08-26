"""Actual PostgreSQL SQL instrumentation of the read-only projection path."""

from contextlib import contextmanager
from datetime import datetime

from flask import current_app
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_match_readiness_projections, count_match_projections,
    filter_match_projections,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import DbTournamentMatchToContestant
from byceps.services.lan_tournament.dbmodels.match_readiness import DbMatchPairing
from byceps.services.lan_tournament.dbmodels.participant import DbTournamentParticipant
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.match_readiness import ReadinessDisplayStatus
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5, 12)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PartyID('issue5-readiness-projection'), 'Issue 5 projection')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Issue5Projection{i}') for i in range(2)]


@pytest.fixture
def projection_rows(party, users):
    assert db.engine.dialect.name == 'postgresql'
    tournament = DbTournament(
        uuid7(), party.id, f'Projection {uuid7()}', NOW,
        game_format='ONE_V_ONE', tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [DbTournamentParticipant(uuid7(), u.id, tournament.id, NOW) for u in users]
    db.session.add_all(participants)
    db.session.flush()
    matches = [DbTournamentMatch(uuid7(), tournament.id, NOW) for _ in range(103)]
    db.session.add_all(matches)
    db.session.flush()
    for index, match in enumerate(matches):
        # 100 current pairs, one fully assigned legacy row with no pair, zero/
        # one assigned Waiting rows. No GET may initialize the legacy row.
        count = 2 if index <= 100 else index - 101
        for participant in participants[:count]:
            db.session.add(DbTournamentMatchToContestant(
                uuid7(), match.id, NOW, participant_id=participant.id,
            ))
    db.session.flush()
    tournament_id = tournament.id
    match_ids = [m.id for m in matches]
    repo.get_tournament_for_update(tournament_id)
    for match_id in sorted(match_ids[:100]):
        repo.get_match_for_update(match_id)
        result = repo.refresh_match_pairing_flush(match_id, occurred_at=NOW)
        assert result.is_ok() and result.unwrap()
        repo.set_side_ready_flush(match_id, MatchSide.A, NOW, users[0].id)
        repo.set_side_ready_flush(match_id, MatchSide.B, NOW, users[1].id)
    db.session.commit()
    # Materialize IDs/DTO before instrumentation, excluding fixture refreshes.
    tournament_dto = repo.get_tournament(tournament_id)
    db.session.expunge_all()
    yield tournament_dto, match_ids
    db.session.rollback()


@contextmanager
def _statements():
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)


def _read(tournament, match_ids):
    matches = repo.get_matches_by_ids(set(match_ids))
    contestants = repo.get_contestants_for_matches(set(match_ids))
    return build_match_readiness_projections(tournament, matches, contestants)


def test_projection_batch_query_growth_postgresql(projection_rows):
    tournament, match_ids = projection_rows
    counts = []
    for quantity in (10, 100):
        db.session.expunge_all()
        with _statements() as statements:
            projections = _read(tournament, match_ids[:quantity])
            assert len(projections) == quantity
            assert all(p.status is ReadinessDisplayStatus.BOTH_READY for p in projections.values())
            # Force consumption of scalar actor/time/identity data inside capture.
            assert all(p.ready_by_a and p.ready_by_b and p.pairing_started_at == NOW for p in projections.values())
            assert count_match_projections(list(projections.values()))['both_ready'] == quantity
        counts.append(len(statements))
        assert all(s.lstrip().upper().startswith('SELECT') for s in statements)
        assert sum('lan_tournament_match_pairings' in s for s in statements) == 1
        assert not any('FROM users' in s for s in statements)
    assert counts == [3, 3]  # existing match + contestant batch, one pair batch
    assert counts[1] - counts[0] <= 1
    print(f'POSTGRESQL SQL statements: 10 rows={counts[0]}, 100 rows={counts[1]}, growth={counts[1] - counts[0]}')


def _snapshot(match_ids):
    # Independent session verifies persisted state, not an ORM identity cache.
    with Session(db.engine) as session:
        match_table = DbTournamentMatch.__table__
        matches = session.execute(select(match_table).where(match_table.c.id.in_(match_ids)).order_by(match_table.c.id)).all()
        pairs = session.execute(select(DbMatchPairing.__table__).where(DbMatchPairing.match_id.in_(match_ids)).order_by(DbMatchPairing.id)).all()
        return matches, pairs


def test_projection_get_performs_no_writes(projection_rows):
    tournament, match_ids = projection_rows
    before = _snapshot(match_ids)
    flushes, commits = [], []

    def flushed(session, context):
        flushes.append(True)

    def committed(session):
        commits.append(True)

    session = db.session()
    event.listen(session, 'after_flush', flushed)
    event.listen(session, 'after_commit', committed)
    try:
        with current_app.test_request_context('/lan-tournaments/projection', method='GET'):
            with _statements() as statements:
                first = _read(tournament, match_ids)
                second = _read(tournament, match_ids)
                assert first == second
                scoped = list(first.values())
                count_match_projections(scoped)
                filter_match_projections(scoped, only='both_ready')
    finally:
        event.remove(session, 'after_flush', flushed)
        event.remove(session, 'after_commit', committed)
    assert len(statements) == 6
    assert all(s.lstrip().upper().startswith('SELECT') for s in statements)
    assert flushes == commits == []
    assert _snapshot(match_ids) == before
    legacy = first[match_ids[100]]
    assert legacy.assignment_complete and legacy.pairing_id is None
    assert legacy.ready_sides == () and not legacy.mutation_available
    assert legacy.original_occupied_since is legacy.pairing_started_at is None
    assert first[match_ids[101]].assigned_contestant_count == 0
    assert first[match_ids[102]].assigned_contestant_count == 1
    assert first[match_ids[101]].status is first[match_ids[102]].status is ReadinessDisplayStatus.NOT_YET_OCCUPIED
    print('GET projection: 6 SELECT statements, 0 writes, 0 flushes, 0 commits; independent persisted snapshot unchanged')
