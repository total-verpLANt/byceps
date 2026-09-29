"""
tests.integration.services.lan_tournament.test_service_point_ceiling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The tournament service bounds the FFA point table itself, so a caller
that skips the admin views cannot store a place value the group
confirmation could not write to its 32-bit column.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-service-point-ceiling')

CEILING = tournament_domain_service.MAX_POINTS_PER_PLACE
MAX_PLACES = tournament_domain_service.MAX_POINT_TABLE_PLACES

TOO_HIGH = f'Points may be at most {CEILING}.'
TOO_LOW = f'Points may be at least {-CEILING}.'

# fmt: off
OUT_OF_RANGE_TABLES = [
    pytest.param([10, 10**12],          TOO_HIGH, id='10**12'),
    pytest.param([10, -(10**12)],       TOO_LOW,  id='-10**12'),
    pytest.param([CEILING + 1, 6, 3],   TOO_HIGH, id='ceiling+1'),
    pytest.param([10, -(CEILING + 1)],  TOO_LOW,  id='-(ceiling+1)'),
    pytest.param([2**31, 1],            TOO_HIGH, id='2**31'),
]
# fmt: on


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Service Point Ceiling Party')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Service Point Ceiling Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'SvcCeiling{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def orga(make_user):
    return make_user('SvcCeilingOrga')


def _create(name: str, point_table: list[int], **kwargs):
    return tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
        point_table=point_table,
        **kwargs,
    )


def _make_tournament(name: str, point_table: list[int]):
    result = _create(name, point_table)
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    return tournament


def _update(tournament, point_table: list[int], name: str | None = None):
    return tournament_service.update_tournament(
        tournament.id,
        name=name or tournament.name,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
        point_table=point_table,
    )


@pytest.mark.parametrize(('point_table', 'expected'), OUT_OF_RANGE_TABLES)
def test_create_rejects_a_place_value_out_of_range(
    party, point_table, expected
):
    token = uuid4()
    before = len(tournament_service.get_tournaments_for_party(PARTY_ID))

    result = _create('Too high on create', point_table, creation_token=token)

    assert result.is_err()
    assert result.unwrap_err() == expected
    assert tournament_service.find_tournament_by_creation_token(token) is None
    assert len(tournament_service.get_tournaments_for_party(PARTY_ID)) == before


@pytest.mark.parametrize(('point_table', 'expected'), OUT_OF_RANGE_TABLES)
def test_update_rejects_a_place_value_out_of_range(
    party, point_table, expected
):
    tournament = _make_tournament('Too high on update', [10, 6, 3, 1])
    before = tournament_service.get_tournament(tournament.id)

    result = _update(tournament, point_table, name='Renamed by a bad update')

    assert result.is_err()
    assert result.unwrap_err() == expected
    assert tournament_service.get_tournament(tournament.id) == before
    assert before.point_table == [10, 6, 3, 1]
    assert before.name == 'Too high on update'


def test_create_accepts_the_ceiling_in_both_signs(party):
    token = uuid4()

    result = _create(
        'Ceiling on create', [CEILING, 0, -CEILING], creation_token=token
    )

    assert result.is_ok(), result
    stored = tournament_service.find_tournament_by_creation_token(token)
    assert stored is not None
    assert stored.point_table == [CEILING, 0, -CEILING]


def test_update_accepts_the_ceiling_in_both_signs(party):
    tournament = _make_tournament('Ceiling on update', [10, 6, 3, 1])

    result = _update(tournament, [CEILING, 6, 3, -CEILING])

    assert result.is_ok(), result
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.point_table == [CEILING, 6, 3, -CEILING]


@pytest.mark.parametrize('places', [MAX_PLACES + 1, 1000])
def test_create_rejects_a_point_table_with_too_many_places(party, places):
    token = uuid4()
    before = len(tournament_service.get_tournaments_for_party(PARTY_ID))

    result = _create('Too long on create', [1] * places, creation_token=token)

    assert result.is_err()
    assert result.unwrap_err() == f'At most {MAX_PLACES} places.'
    assert tournament_service.find_tournament_by_creation_token(token) is None
    assert len(tournament_service.get_tournaments_for_party(PARTY_ID)) == before


@pytest.mark.parametrize('places', [MAX_PLACES + 1, 1000])
def test_update_rejects_a_point_table_with_too_many_places(party, places):
    tournament = _make_tournament('Too long on update', [10, 6, 3, 1])
    before = tournament_service.get_tournament(tournament.id)

    result = _update(tournament, [1] * places, name='Renamed by a bad update')

    assert result.is_err()
    assert result.unwrap_err() == f'At most {MAX_PLACES} places.'
    assert tournament_service.get_tournament(tournament.id) == before
    assert before.point_table == [10, 6, 3, 1]
    assert before.name == 'Too long on update'


def test_create_accepts_a_point_table_with_the_most_places(party):
    token = uuid4()
    point_table = list(range(MAX_PLACES, 0, -1))

    result = _create('Full table on create', point_table, creation_token=token)

    assert result.is_ok(), result
    stored = tournament_service.find_tournament_by_creation_token(token)
    assert stored is not None
    assert stored.point_table == point_table


def test_update_accepts_a_point_table_with_the_most_places(party):
    tournament = _make_tournament('Full table on update', [10, 6, 3, 1])
    point_table = list(range(MAX_PLACES, 0, -1))

    result = _update(tournament, point_table)

    assert result.is_ok(), result
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.point_table == point_table


def test_confirming_a_group_stores_the_ceiling_points(party, ticketed, orga):
    tournament = _make_tournament(
        'Confirm ceiling points', [CEILING, 500_000_000, 1, -CEILING]
    )
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed:
        assert tournament_participant_service.join_tournament(
            tournament.id, user.id
        ).is_ok()
    for status in (
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    ):
        assert tournament_service.change_status(tournament.id, status).is_ok()
    assert tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=orga.id
    ).is_ok()
    (group,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    contestants = tournament_match_service.get_contestants_for_match(group.id)

    placed = tournament_match_service.set_ffa_placements(
        group.id,
        {str(c.participant_id): i + 1 for i, c in enumerate(contestants)},
    )
    confirmed = tournament_match_service.confirm_ffa_match(group.id, orga.id)

    assert placed.is_ok(), placed
    assert confirmed.is_ok(), confirmed
    stored = tournament_match_service.get_contestants_for_match(group.id)
    assert sorted(c.points for c in stored) == [
        -CEILING,
        1,
        500_000_000,
        CEILING,
    ]


def _make_legacy_tournament(name: str, point_table: list[int]):
    tournament = _make_tournament(name, [10, 6, 3, 1])
    db.session.execute(
        update(DbTournament)
        .where(DbTournament.id == tournament.id)
        .values(point_table=json.dumps(point_table))
    )
    db.session.commit()
    return tournament_service.get_tournament(tournament.id)


def test_update_keeps_an_unchanged_legacy_table_above_the_place_limit(party):
    legacy = list(range(MAX_PLACES + 1, 0, -1))
    tournament = _make_legacy_tournament('Legacy table kept', legacy)

    result = _update(tournament, legacy, name='Renamed with legacy table')

    assert result.is_ok(), result
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.name == 'Renamed with legacy table'
    assert stored.point_table == legacy


def test_update_rejects_a_changed_legacy_table_above_the_place_limit(party):
    legacy = list(range(MAX_PLACES + 1, 0, -1))
    tournament = _make_legacy_tournament('Legacy table changed', legacy)

    result = _update(tournament, [*legacy[:-1], 99])

    assert result.is_err()
    assert result.unwrap_err() == f'At most {MAX_PLACES} places.'
    assert tournament_service.get_tournament(tournament.id).point_table == (
        legacy
    )
