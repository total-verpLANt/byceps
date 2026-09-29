"""
tests.integration.services.lan_tournament.test_tournament_overview_header
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin overview header: the status tag, the tiles and the
"Open registration" button that posts the existing status transition.
"""

from datetime import datetime
import re

import pytest

from byceps.services.lan_tournament import tournament_service
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-overview-header')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Overview Header Party')


@pytest.fixture(scope='module')
def administrator(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.view', 'lan_tournament.administrate'},
        screen_name='OverviewAdministrator',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.view'},
        screen_name='OverviewViewer',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def administrator_client(make_client, admin_app, administrator):
    return make_client(admin_app, user_id=administrator.id)


@pytest.fixture(scope='module')
def viewer_client(make_client, admin_app, viewer):
    return make_client(admin_app, user_id=viewer.id)


def _draft(party, name, **extra):
    result = tournament_service.create_tournament(
        party.id,
        name,
        game='Chess.com',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.DRAFT,
        max_players=16,
        start_time=datetime(2026, 10, 4, 8, 0),
        **extra,
    )
    assert result.is_ok(), result
    tournament, _event = result.unwrap()
    return tournament


def _status(tournament) -> TournamentStatus:
    return tournament_service.get_tournament(tournament.id).tournament_status


def _view_url(tournament) -> str:
    return f'{BASE_URL}/tournaments/{tournament.id}'


def _header(html: str) -> str:
    start = html.index('lt-tv-head"')
    return html[start : html.index('data-wiz-overview>')]


def test_overview_shows_the_draft_tag_and_the_tiles(
    administrator_client, party
):
    tournament = _draft(party, 'Overview tiles')

    response = administrator_client.get(_view_url(tournament))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert re.search(
        r'<h1 class="title">Overview tiles\s*'
        r'<span class="tag color-disabled">[^<]+</span></h1>',
        html,
    )
    overview = html[html.index('data-wiz-overview>') :]
    overview = overview[: overview.index('button-row subnav')]
    assert 'Chess.com' in overview
    assert re.search(r'Solo · 1v1 · \S', overview)
    # An unset minimum reads as a dash, not as "– – 16".
    assert re.search(r'–\s+\w+\s+16\s+\w+', overview)
    assert '– –' not in overview
    assert re.search(
        r'10:00\s*<span class="dimmed">\(Europe/Berlin, ', overview
    )


def test_header_posts_the_status_transition_with_the_permission(
    administrator_client, party
):
    tournament = _draft(party, 'Overview opens')

    html = administrator_client.get(_view_url(tournament)).get_data(
        as_text=True
    )
    form = re.search(
        r'<form action="([^"]+)" method="post" data-overview-open-registration>'
        r'\s*<button type="submit" class="button color-primary">[^<]+</button>',
        _header(html),
    )
    assert form is not None
    assert form.group(1).endswith(
        f'/tournaments/{tournament.id}/open_registration'
    )

    response = administrator_client.post(form.group(1))

    assert response.status_code in (302, 303)
    assert _status(tournament) is TournamentStatus.REGISTRATION_OPEN
    html = administrator_client.get(_view_url(tournament)).get_data(
        as_text=True
    )
    assert 'data-overview-open-registration' not in html


def test_header_offers_no_transition_without_the_permission(
    viewer_client, party
):
    tournament = _draft(party, 'Overview closed')

    response = viewer_client.get(_view_url(tournament))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-overview-open-registration' not in html
    assert 'open_registration' not in html


def test_the_transition_itself_is_refused_without_the_permission(
    viewer_client, party
):
    tournament = _draft(party, 'Overview forged')

    response = viewer_client.post(f'{_view_url(tournament)}/open_registration')

    assert response.status_code == 403
    assert _status(tournament) is TournamentStatus.DRAFT
