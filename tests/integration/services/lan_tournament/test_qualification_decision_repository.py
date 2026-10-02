"""
tests.integration.services.lan_tournament.test_qualification_decision_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_qualification_repository as repo,
    tournament_service,
)
from byceps.services.lan_tournament.db_error_helpers import (
    extract_constraint_name,
)
from byceps.services.lan_tournament.dbmodels.qualification_decision import (
    DbQualificationDecision,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
    QualificationDecisionID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-qualification-decision-repo')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('qualdecisionrepobrand', 'Qualification Decision Repo')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Qualification Repo')


@pytest.fixture(scope='module')
def orga(make_user):
    return make_user('QualDecisionRepoOrga')


@pytest.fixture
def tournament(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Qualification Decision Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    yield tournament
    db.session.rollback()
    repo.delete_decisions_for_tournament(tournament.id)
    db.session.commit()


def _make_decision(
    tournament, orga, scope='group:1', ids=('c', 'a', 'b'), **kwargs
):
    fields = {
        'id': QualificationDecisionID(generate_uuid7()),
        'tournament_id': tournament.id,
        'scope': scope,
        'reason': 'Coin was not allowed, so the orga chose.',
        'decided_by': orga.id,
        'decided_at': datetime.now(UTC).replace(tzinfo=None),
    }
    fields.update(kwargs)
    fields.setdefault(
        'blocks',
        (
            DecisionBlock(
                contestant_ids=ids,
                reason=fields['reason'],
                decided_by=fields['decided_by'],
                decided_at=fields['decided_at'],
            ),
        ),
    )
    return QualificationDecision(**fields)


def test_upsert_and_find_decision(tournament, orga):
    decision = _make_decision(tournament, orga)

    repo.upsert_decision(decision)

    assert repo.find_decision(tournament.id, 'group:1') == decision
    assert repo.find_decision(tournament.id, 'group:2') is None
    assert repo.get_decisions_for_tournament(tournament.id) == {
        'group:1': decision
    }

    now = datetime.now(UTC).replace(tzinfo=None)
    replacement = replace(
        decision,
        id=QualificationDecisionID(generate_uuid7()),
        blocks=(
            DecisionBlock(
                contestant_ids=('b', 'c', 'a'),
                reason='Reconsidered.',
                decided_by=orga.id,
                decided_at=now,
            ),
        ),
        reason='Reconsidered.',
        decided_at=now,
    )
    repo.upsert_decision(replacement)

    found = repo.find_decision(tournament.id, 'group:1')
    assert found == replace(replacement, id=decision.id)
    assert found.orders == (('b', 'c', 'a'),)
    assert len(repo.get_decisions_for_tournament(tournament.id)) == 1

    other = _make_decision(tournament, orga, scope='leaderboard')
    repo.upsert_decision(other)

    assert set(repo.get_decisions_for_tournament(tournament.id)) == {
        'group:1',
        'leaderboard',
    }
    assert repo.find_decision(tournament.id, 'leaderboard') == other


@pytest.mark.parametrize('reason', ['', '   ', '\n\t '])
def test_reason_required(tournament, orga, reason):
    with pytest.raises(ValueError):
        repo.upsert_decision(_make_decision(tournament, orga, reason=reason))

    assert repo.find_decision(tournament.id, 'group:1') is None


@pytest.mark.parametrize('reason', ['', '   '])
def test_reason_required_by_the_table_itself(tournament, orga, reason):
    db.session.add(
        DbQualificationDecision(
            QualificationDecisionID(generate_uuid7()),
            tournament.id,
            'group:1',
            (),
            reason,
            orga.id,
            datetime.now(UTC).replace(tzinfo=None),
        )
    )
    with pytest.raises(IntegrityError) as excinfo:
        db.session.flush()
    db.session.rollback()

    assert (
        extract_constraint_name(excinfo.value)
        == 'ck_lan_tournament_qualification_decisions_reason'
    )


def test_blocks_keep_their_own_meta_through_the_database(
    tournament, orga, make_user
):
    other = make_user('QualDecisionRepoOtherOrga')
    first_at = datetime(2026, 10, 1, 9, 15, 30, 123456)
    second_at = datetime(2026, 10, 2, 11, 0, 0)
    decision = _make_decision(
        tournament,
        orga,
        blocks=(
            DecisionBlock(
                contestant_ids=('a', 'c'),
                reason='First tie.',
                decided_by=orga.id,
                decided_at=first_at,
            ),
            DecisionBlock(
                contestant_ids=('b', 'd'),
                reason='Second tie.',
                decided_by=other.id,
                decided_at=second_at,
            ),
        ),
        reason='Second tie.',
        decided_by=other.id,
        decided_at=second_at,
    )

    repo.upsert_decision(decision)
    db.session.commit()

    assert repo.find_decision(tournament.id, 'group:1') == decision


def test_legacy_flat_row_reads_as_one_block(tournament, orga):
    decision = _make_decision(tournament, orga)
    db.session.add(
        DbQualificationDecision(
            decision.id,
            tournament.id,
            'group:1',
            (),
            decision.reason,
            orga.id,
            decision.decided_at,
        )
    )
    db.session.flush()
    db.session.execute(
        update(DbQualificationDecision)
        .filter_by(id=decision.id)
        .values(ordered_contestant_ids='["c","a","b"]')
    )

    found = repo.find_decision(tournament.id, 'group:1')

    assert found == decision
    assert found.orders == (('c', 'a', 'b'),)
    assert found.blocks[0].reason == decision.reason


def test_reason_required_on_replacement_keeps_stored_decision(tournament, orga):
    decision = _make_decision(tournament, orga)
    repo.upsert_decision(decision)
    db.session.commit()

    with pytest.raises(ValueError):
        repo.upsert_decision(replace(decision, reason=' '))

    assert repo.find_decision(tournament.id, 'group:1') == decision


def test_delete_decision_only_removes_that_scope(tournament, orga):
    repo.upsert_decision(_make_decision(tournament, orga, scope='group:1'))
    repo.upsert_decision(_make_decision(tournament, orga, scope='group:2'))

    repo.delete_decision(tournament.id, 'group:1')
    repo.delete_decision(tournament.id, 'group:9')

    assert set(repo.get_decisions_for_tournament(tournament.id)) == {'group:2'}


def test_delete_decisions_for_tournament_spares_other_tournaments(
    tournament, orga
):
    other_result = tournament_service.create_tournament(
        PARTY_ID,
        f'Qualification Decision Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
    )
    other, _ = other_result.unwrap()
    repo.upsert_decision(_make_decision(tournament, orga))
    repo.upsert_decision(_make_decision(other, orga))

    repo.delete_decisions_for_tournament(tournament.id)

    assert repo.get_decisions_for_tournament(tournament.id) == {}
    assert set(repo.get_decisions_for_tournament(other.id)) == {'group:1'}
    repo.delete_decisions_for_tournament(other.id)
    db.session.commit()
