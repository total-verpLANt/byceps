"""
tests.integration.services.lan_tournament.test_ffa_point_ceiling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A place of the FFA point table is worth at most the score ceiling, so
confirming a group can always store it.
"""

import re
from uuid import UUID, uuid4

import pytest

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-ffa-point-ceiling')

CEILING = tournament_domain_service.MAX_POINTS_PER_PLACE
TOO_HIGH = 10**12


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'FFA Point Ceiling Party')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'FFA Point Ceiling Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'FfaCeiling{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.create', 'lan_tournament.update'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


def _shows_ceiling_error(html: str) -> bool:
    # The number follows the locale of the request.
    return (
        re.search(r'Points may be at most 999[.,]999[.,]999\.', html)
        is not None
    )


def _create_data(token: str, name: str, point_table: str) -> dict:
    return {
        'submission_token': token,
        'name': name,
        'contestant_type': 'SOLO',
        'game_format': 'FREE_FOR_ALL',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'max_players': '8',
        'point_table': point_table,
        'group_size_min': '2',
        'group_size_max': '4',
        'advancement_count': '1',
    }


def _make_ffa_tournament(name: str, point_table: list[int]):
    result = tournament_service.create_tournament(
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
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    return tournament


def test_create_post_with_a_huge_place_value_is_a_field_error(client, party):
    token = str(uuid4())
    before = len(tournament_service.get_tournaments_for_party(PARTY_ID))

    response = client.post(
        f'{BASE_URL}/for_party/{PARTY_ID}',
        data=_create_data(token, 'Huge points', f'10, {TOO_HIGH}'),
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _shows_ceiling_error(html)
    assert (
        tournament_service.find_tournament_by_creation_token(UUID(token))
        is None
    )
    assert len(tournament_service.get_tournaments_for_party(PARTY_ID)) == before


def test_create_post_with_a_place_value_below_the_floor_is_a_field_error(
    client, party
):
    token = str(uuid4())

    response = client.post(
        f'{BASE_URL}/for_party/{PARTY_ID}',
        data=_create_data(token, 'Deep negative points', f'10, -{TOO_HIGH}'),
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert re.search(r'Points may be at least [-−]999[.,]999[.,]999\.', html)
    assert not _shows_ceiling_error(html)
    assert (
        tournament_service.find_tournament_by_creation_token(UUID(token))
        is None
    )


def test_create_post_with_the_ceiling_is_stored(client, party):
    token = str(uuid4())

    response = client.post(
        f'{BASE_URL}/for_party/{PARTY_ID}',
        data=_create_data(token, 'Ceiling points', f'{CEILING}, 0, -{CEILING}'),
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    tournament = tournament_service.find_tournament_by_creation_token(
        UUID(token)
    )
    assert tournament is not None
    assert tournament.point_table == [CEILING, 0, -CEILING]


def test_update_post_with_a_huge_place_value_changes_nothing(client, party):
    tournament = _make_ffa_tournament('Update huge points', [10, 6, 3, 1])

    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        data={
            'name': tournament.name,
            'contestant_type': 'SOLO',
            'game_format': 'FREE_FOR_ALL',
            'elimination_mode': 'SINGLE_ELIMINATION',
            'max_players': '8',
            'point_table': f'10, {TOO_HIGH}',
            'group_size_min': '2',
            'group_size_max': '4',
            'advancement_count': '1',
        },
    )

    assert response.status_code == 200
    assert _shows_ceiling_error(response.get_data(as_text=True))
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.point_table == [10, 6, 3, 1]


def test_update_post_with_the_ceiling_is_stored(client, party):
    tournament = _make_ffa_tournament('Update ceiling points', [10, 6, 3, 1])

    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        data={
            'name': tournament.name,
            'contestant_type': 'SOLO',
            'game_format': 'FREE_FOR_ALL',
            'elimination_mode': 'SINGLE_ELIMINATION',
            'max_players': '8',
            'point_table': f'{CEILING}, 6, 3, 1',
            'group_size_min': '2',
            'group_size_max': '4',
            'advancement_count': '1',
        },
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.point_table == [CEILING, 6, 3, 1]


def test_confirming_a_group_stores_the_highest_allowed_points(
    party, ticketed, admin
):
    tournament = _make_ffa_tournament(
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
        tournament.id, initiator_id=admin.id
    ).is_ok()
    (group,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    contestants = tournament_match_service.get_contestants_for_match(group.id)

    placed = tournament_match_service.set_ffa_placements(
        group.id,
        {str(c.participant_id): i + 1 for i, c in enumerate(contestants)},
    )
    confirmed = tournament_match_service.confirm_ffa_match(group.id, admin.id)

    assert placed.is_ok(), placed
    assert confirmed.is_ok(), confirmed
    stored = tournament_match_service.get_contestants_for_match(group.id)
    assert sorted(c.points for c in stored) == [
        -CEILING,
        1,
        500_000_000,
        CEILING,
    ]
