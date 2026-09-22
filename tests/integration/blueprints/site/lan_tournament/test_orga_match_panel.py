"""
tests.integration.blueprints.site.lan_tournament.test_orga_match_panel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.ticketing import ticket_creation_service

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'


@pytest.fixture(scope='module')
def players(make_user, make_ticket_category, party):
    category = make_ticket_category(party.id, 'Orga Panel Entry')
    users = [make_user(f'OrgaPanelPlayer{i}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(category, user, user=user)
    return users


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('OrgaPanelOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def bracket(party, players, orga):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Orga panel',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).unwrap()
    for player in players:
        tournament_participant_service.join_tournament(
            tournament.id, player.id
        ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).unwrap()
    tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    played, open_ = (
        m
        for m in matches
        if m.bracket in (None, Bracket.WINNERS) and m.round == 0
    )
    contestants = tournament_match_service.get_contestants_for_match(played.id)
    tournament_match_service.admin_set_and_confirm_match(
        played.id,
        orga.id,
        {contestants[0].participant_id: 3, contestants[1].participant_id: 1},
    ).unwrap()

    return tournament, played, open_


def _get(app, path, user=None):
    with http_client(app, user_id=user.id if user else None) as client:
        return client.get(f'{BASE_URL}{path}')


def test_orga_gets_correction_but_no_unconfirm_on_bracket_match(
    site_app, bracket, orga
):
    _, played, _ = bracket

    response = _get(site_app, f'/matches/{played.id}', orga)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'orga_correction_reason' in html
    assert 'orga_unconfirm_reason' not in html


def test_orga_gets_confirm_form_on_open_match(site_app, bracket, orga):
    _, _, open_ = bracket

    response = _get(site_app, f'/matches/{open_.id}', orga)

    assert response.status_code == 200
    assert 'orga_score_' in response.get_data(as_text=True)


def test_anonymous_visitor_gets_no_orga_panel(site_app, bracket):
    _, played, _ = bracket

    response = _get(site_app, f'/matches/{played.id}')

    assert response.status_code == 200
    assert 'orga_correction_reason' not in response.get_data(as_text=True)


def test_paused_tournament_locks_the_result_forms(site_app, bracket, orga):
    tournament, played, _ = bracket
    tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    ).unwrap()
    try:
        response = _get(site_app, f'/matches/{played.id}', orga)
    finally:
        tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING
        ).unwrap()

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'orga_correction_reason' not in html
    assert 'orga_comment' in html


def test_orga_unconfirm_of_bracket_match_changes_nothing(
    site_app, bracket, orga
):
    _, played, _ = bracket

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            f'{BASE_URL}/orga/matches/{played.id}/unconfirm',
            data={'reason': 'posted directly'},
        )

    assert response.status_code == 302
    match = tournament_match_service.get_match(played.id)
    assert match.confirmed_by is not None


@pytest.fixture(scope='module')
def ffa(party, players, orga):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Orga panel FFA',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=[3, 1],
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).unwrap()
    for player in players:
        tournament_participant_service.join_tournament(
            tournament.id, player.id
        ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()
    tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=orga.id
    ).unwrap()
    tournament_orga_service.assign_orga(
        tournament.id, orga.id, orga.id
    ).unwrap()

    played, open_ = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    first, second = tournament_match_service.get_contestants_for_match(
        played.id
    )
    tournament_match_service.set_ffa_placements(
        played.id,
        {str(first.participant_id): 1, str(second.participant_id): 2},
    ).unwrap()
    tournament_match_service.confirm_ffa_match(played.id, orga.id).unwrap()

    return played, open_, first.participant_id, second.participant_id


def test_orga_gets_placement_form_on_open_ffa_match(site_app, ffa, orga):
    _, open_, _, _ = ffa

    response = _get(site_app, f'/matches/{open_.id}', orga)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'/orga/matches/{open_.id}/set_ffa_placements' in html
    assert 'orga_placement_' in html


def test_orga_can_reenter_and_confirm_an_unconfirmed_ffa_result(
    site_app, ffa, orga
):
    played, _, winner_id, loser_id = ffa

    with http_client(site_app, user_id=orga.id) as client:
        client.post(
            f'{BASE_URL}/orga/matches/{played.id}/unconfirm',
            data={'reason': 'placements swapped'},
        )
        client.post(
            f'{BASE_URL}/orga/matches/{played.id}/set_ffa_placements',
            data={
                f'placement_{winner_id}': '2',
                f'placement_{loser_id}': '1',
            },
        )
        response = client.post(
            f'{BASE_URL}/orga/matches/{played.id}/confirm_ffa'
        )

    assert response.status_code == 302
    match = tournament_match_service.get_match(played.id)
    assert match.confirmed_by == orga.id
    placements = {
        c.participant_id: c.placement
        for c in tournament_match_service.get_contestants_for_match(played.id)
    }
    assert placements == {winner_id: 2, loser_id: 1}
