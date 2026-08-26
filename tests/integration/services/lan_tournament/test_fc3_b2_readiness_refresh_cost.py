"""Statement cost of the engine contestant insert, and what it must not change."""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as engine,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.match_readiness import (
    InvitationStatus,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 5)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand, PartyID('fc3-b2-refresh-cost'), 'FC3 B2 refresh cost'
    )


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Fc3B2Refresh{i}') for i in range(3)]


@pytest.fixture
def facts_reads(monkeypatch):
    real = readiness._locked_facts
    calls = []

    def counting(match_id):
        calls.append(match_id)
        return real(match_id)

    monkeypatch.setattr(readiness, '_locked_facts', counting)
    return calls


@pytest.fixture
def setup(party, users):
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Refresh cost {uuid7()}',
        NOW,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, NOW)
        for user in users
    ]
    db.session.add_all(participants)
    match_id = uuid7()
    repo.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=tournament.id,
            created_at=NOW,
            group_order=None,
            match_order=0,
            round=0,
            next_match_id=None,
            confirmed_by=None,
        )
    )
    db.session.commit()
    yield SimpleNamespace(
        tournament_id=tournament.id,
        match_id=match_id,
        participant_ids=[p.id for p in participants],
        user_ids=[u.id for u in users],
    )
    db.session.rollback()


@contextmanager
def statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(' '.join(statement.lower().split()))

    engine_ = db.engine
    event.listen(engine_, 'before_cursor_execute', record)
    try:
        yield seen
    finally:
        event.remove(engine_, 'before_cursor_execute', record)


def locked_tournament_reads(seen):
    return [
        s
        for s in seen
        if s.startswith('select')
        and 'from lan_tournaments' in s
        and 'for update' in s
    ]


def invitation_statements(seen):
    return [s for s in seen if 'lan_tournament_match_invitations' in s]


def insert(setup, index):
    engine._create_match_contestant_flush(
        TournamentMatchToContestant(
            id=uuid7(),
            tournament_match_id=setup.match_id,
            created_at=NOW,
            team_id=None,
            participant_id=setup.participant_ids[index],
            score=None,
        )
    )


def invitation_rows(match_id):
    db.session.expire_all()
    return db.session.scalars(
        select(DbMatchInvitation).where(DbMatchInvitation.match_id == match_id)
    ).all()


def test_first_contestant_in_an_empty_match_locks_once_and_skips_the_reconcile(
    setup, facts_reads
):
    with statements() as seen:
        insert(setup, 0)
    assert len(facts_reads) == 1
    assert len(locked_tournament_reads(seen)) == 1
    assert invitation_statements(seen) == []
    # Measured: 25 statements with the second facts read and the reconcile, 10 without.
    assert len(seen) <= 12
    assert invitation_rows(setup.match_id) == []
    match = db.session.get(DbTournamentMatch, setup.match_id)
    assert match.pairing_id is None and match.pairing_generation == 0


def test_second_contestant_still_creates_the_pairing_and_the_recipient_work(
    setup, facts_reads
):
    insert(setup, 0)
    facts_reads.clear()
    with statements() as seen:
        engine._create_match_contestant_flush(
            TournamentMatchToContestant(
                id=uuid7(),
                tournament_match_id=setup.match_id,
                created_at=NOW,
                team_id=None,
                participant_id=setup.participant_ids[1],
                score=None,
            )
        )
    assert invitation_statements(seen)
    rows = invitation_rows(setup.match_id)
    assert {r.recipient_id for r in rows} == set(setup.user_ids[:2])
    assert {r.status for r in rows} == {InvitationStatus.PENDING.value}
    match = db.session.get(DbTournamentMatch, setup.match_id)
    assert match.pairing_id is not None and match.pairing_generation == 1
    # A changed pairing needs fresh facts for the audit and the result.
    assert len(facts_reads) == 2


def test_refresh_result_is_unchanged_for_a_new_pairing(setup):
    insert(setup, 0)
    insert(setup, 1)
    ids = sorted(r.id for r in invitation_rows(setup.match_id))
    repo.get_tournament_for_update(setup.tournament_id)
    repo.get_match_for_update(setup.match_id)
    result = readiness.refresh_pairing_and_invitations_flush(
        setup.match_id, occurred_at=NOW
    )
    change = result.unwrap()
    assert sorted(change.pending_invitation_ids) == ids
    assert change.match.pairing_generation == 1
    assert change.readiness.pairing_valid


def test_unchanged_pairing_reads_the_facts_once_but_still_reconciles(
    setup, facts_reads
):
    insert(setup, 0)
    insert(setup, 1)
    ids = sorted(r.id for r in invitation_rows(setup.match_id))
    facts_reads.clear()
    with statements() as seen:
        change = readiness.refresh_pairing_and_invitations_flush(
            setup.match_id,
            occurred_at=NOW,
        ).unwrap()
    assert len(facts_reads) == 1
    assert invitation_statements(seen)
    assert sorted(change.pending_invitation_ids) == ids
    assert change.match.pairing_generation == 1


def test_reset_without_claims_or_pairing_skips_second_facts_and_reconcile(
    setup, facts_reads
):
    insert(setup, 0)
    facts_reads.clear()
    with statements() as seen:
        change = readiness.reset_readiness_flush(
            setup.match_id, occurred_at=NOW
        ).unwrap()
    assert len(facts_reads) == 1
    assert invitation_statements(seen) == []
    assert change.pending_invitation_ids == ()
    assert change.match.readiness_revision == 0


def test_reset_with_claims_reads_fresh_facts_and_bumps_the_revision(
    setup, facts_reads
):
    insert(setup, 0)
    insert(setup, 1)
    repo.get_tournament_for_update(setup.tournament_id)
    repo.get_match_for_update(setup.match_id)
    repo.set_side_invitation_hold_flush(setup.match_id, MatchSide.A, True)
    revision = db.session.get(
        DbTournamentMatch, setup.match_id
    ).readiness_revision
    facts_reads.clear()
    change = readiness.reset_readiness_flush(
        setup.match_id, occurred_at=NOW
    ).unwrap()
    assert len(facts_reads) == 2
    assert change.match.readiness_revision == revision + 1
    assert not change.match.invitation_hold_a
