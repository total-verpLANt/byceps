"""
tests.integration.services.lan_tournament.test_match_phase_persistence
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
from itertools import count

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-match-phase')

NOW = datetime(2024, 1, 1, 12, 0, 0)

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('matchphasebrand', 'Match Phase Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Match Phase')


@pytest.fixture
def tournament(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Match Phase Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    yield tournament
    db.session.rollback()


def _create_match(tournament, **kwargs) -> TournamentMatchID:
    match_id = TournamentMatchID(generate_uuid7())
    tournament_repository.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=tournament.id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=NOW,
            **kwargs,
        )
    )
    return match_id


def _find(match_id) -> TournamentMatch:
    db.session.expire_all()
    match = tournament_repository.find_match(match_id)
    assert match is not None
    return match


def test_match_phase_defaults_to_one(tournament):
    match_id = _create_match(tournament)
    assert _find(match_id).phase == 1

    # The column default also applies to rows written without the ORM.
    raw_id = generate_uuid7()
    db.session.execute(
        text(
            'INSERT INTO lan_tournament_matches (id, tournament_id, created_at)'
            ' VALUES (:id, :tid, :created_at)'
        ),
        {'id': raw_id, 'tid': tournament.id, 'created_at': NOW},
    )
    assert _find(TournamentMatchID(raw_id)).phase == 1


def test_match_phase_two_round_trip(tournament):
    match_id = _create_match(tournament, phase=2)

    assert _find(match_id).phase == 2
    matches = tournament_repository.get_matches_for_round(tournament.id, 1)
    assert [m.phase for m in matches] == [2]


def test_match_phase_check_rejects_other_values(tournament):
    with pytest.raises(IntegrityError) as exc_info:
        _create_match(tournament, phase=3)
    db.session.rollback()

    assert (
        exc_info.value.orig.diag.constraint_name
        == 'ck_lan_tournament_matches_phase'
    )


def test_match_seeding_target_defaults_to_none(tournament):
    match_id = _create_match(tournament)
    assert _find(match_id).seeding_target is None


def test_match_seeding_target_round_trips_and_is_queryable(tournament):
    tagged = _create_match(tournament, seeding_target='ffa:WB:1')
    _create_match(tournament, seeding_target='ffa:LB:1')
    _create_match(tournament)

    assert _find(tagged).seeding_target == 'ffa:WB:1'
    found = tournament_repository.get_matches_for_seeding_target(
        tournament.id, 'ffa:WB:1'
    )
    assert [m.id for m in found] == [tagged]
