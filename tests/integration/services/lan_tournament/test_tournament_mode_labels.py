"""
tests.integration.services.lan_tournament.test_tournament_mode_labels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The admin list, the admin view and the site view name the elimination
mode by its translated label, not by the raw enum name.
"""

from datetime import datetime
import re

import pytest

from byceps.byceps_app import BycepsApp
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
from byceps.services.site.models import Site, SiteID

from tests.helpers import create_site, http_client, log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-mode-labels')

SITE_ID = SiteID('lt-test-mode-labels')

# fmt: off
MODES = [
    (EliminationMode.SINGLE_ELIMINATION, 'Single knockout'),
    (EliminationMode.DOUBLE_ELIMINATION, 'Double knockout'),
    (EliminationMode.ROUND_ROBIN,        'Everyone plays everyone'),
]
# fmt: on


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Mode Labels Party')


@pytest.fixture(scope='module')
def admin_client(make_client, admin_app, make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.view'},
        screen_name='ModeLabelsViewer',
    )
    log_in_user(user.id)
    return make_client(admin_app, user_id=user.id)


@pytest.fixture(scope='module')
def site(party) -> Site:
    """A site without `template_overrides`: the blueprint's own view."""
    return create_site(
        SITE_ID,
        party.brand_id,
        server_name='lt-test-mode-labels.test',
        party_id=party.id,
    )


@pytest.fixture(scope='module')
def site_app(database, make_site_app, site: Site) -> BycepsApp:
    app = make_site_app(site.server_name, site.id)
    with app.app_context():
        return app


def _create(party, mode: EliminationMode):
    result = tournament_service.create_tournament(
        party.id,
        f'Mode labels {mode.name}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=mode,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        max_players=16,
        start_time=datetime(2026, 10, 4, 8, 0),
    )
    assert result.is_ok(), result
    tournament, _event = result.unwrap()
    return tournament


def _squash(html: str) -> str:
    return re.sub(r'\s+', ' ', html).strip()


@pytest.mark.parametrize(('mode', 'label'), MODES, ids=str)
def test_admin_view_tile_names_the_mode_by_its_label(
    admin_client, party, mode, label
):
    tournament = _create(party, mode)

    response = admin_client.get(f'{ADMIN_URL}/tournaments/{tournament.id}')

    assert response.status_code == 200
    tile = re.search(
        r'>Tournament mode</div>\s*<div class="data-value">(.*?)</div>',
        response.get_data(as_text=True),
        re.S,
    )
    assert tile is not None
    assert _squash(tile.group(1)) == f'1v1 / {label}'


@pytest.mark.parametrize(('mode', 'label'), MODES, ids=str)
def test_admin_list_names_the_mode_by_its_label(
    admin_client, party, mode, label
):
    tournament = _create(party, mode)

    response = admin_client.get(f'{ADMIN_URL}/for_party/{party.id}')

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    row = html[html.index(f'<strong>{tournament.name}</strong>') :]
    cell = re.search(r'</td>\s*<td>[^<]*</td>\s*<td>(.*?)</td>', row, re.S)
    assert cell is not None
    assert _squash(cell.group(1)) == f'1v1 / {label}'


@pytest.mark.parametrize(('mode', 'label'), MODES, ids=str)
def test_site_view_names_the_mode_by_its_label(site_app, party, mode, label):
    tournament = _create(party, mode)

    # English: the msgids, independent of the compiled catalogue.
    with http_client(site_app) as client:
        response = client.get(
            f'/lan-tournaments/{tournament.id}',
            headers={'Accept-Language': 'en'},
        )

    assert response.status_code == 200
    value = re.search(
        r'>Mode</div>\s*<div class="data-value">(.*?)</div>',
        response.get_data(as_text=True),
        re.S,
    )
    assert value is not None
    assert _squash(value.group(1)) == f'1v1 / {label}'
