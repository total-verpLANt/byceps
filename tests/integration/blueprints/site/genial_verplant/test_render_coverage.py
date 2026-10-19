"""
tests.integration.blueprints.site.genial_verplant.test_render_coverage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Hit every argument-free site GET route through the real `genial-verplant`
overrides, so that no core template is left broken by the new site.
"""

from collections.abc import Iterator
from datetime import datetime
import re

from flask import Flask
import pytest

from byceps.blueprints.site import register_site_blueprints
from byceps.byceps_app import BycepsApp
from byceps.services.board import board_service
from byceps.services.board.models import Board, BoardID
from byceps.services.brand.models import Brand, BrandID
from byceps.services.party.models import Party, PartyID
from byceps.services.seating import seating_area_service
from byceps.services.shop.storefront.models import Storefront
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID
from byceps.services.timetable import timetable_service

from tests.helpers import create_site, generate_token, log_in_user


SITE_ID = SiteID('genial-verplant')

MIN_ROUTE_COUNT = 40

EXCLUDED_ENDPOINTS = frozenset({'static', 'site_file', 'authn_login.log_out'})
EXCLUDED_ENDPOINT_PREFIXES = ('healthcheck.', 'metrics.', 'locale.')

# Pages that do not extend the site layout and so link only the tokens.
STANDALONE_ENDPOINTS = frozenset({'pizza_delivery.beamershow'})


def _get_routes(app: Flask) -> dict[str, str]:
    """Return the argument-free GET rules of the app, mapped to endpoints."""
    routes: dict[str, str] = {}

    for rule in app.url_map.iter_rules():
        endpoint = rule.endpoint

        if 'GET' not in rule.methods or rule.arguments:
            continue
        if endpoint in EXCLUDED_ENDPOINTS:
            continue
        if endpoint.startswith(EXCLUDED_ENDPOINT_PREFIXES):
            continue

        routes.setdefault(rule.rule, endpoint)

    return dict(sorted(routes.items()))


def _collect_site_routes() -> dict[str, str]:
    """Enumerate the routes at collection time, without an app or database."""
    app = Flask('byceps')
    register_site_blueprints(app)
    return _get_routes(app)


ROUTES = _collect_site_routes()
RULES = [pytest.param(rule, id=rule) for rule in ROUTES]


@pytest.fixture(scope='module')
def gv_brand(make_brand) -> Brand:
    return make_brand(BrandID(f'gv-{generate_token()}'), 'gv render coverage')


@pytest.fixture(scope='module')
def gv_party(make_party, gv_brand: Brand) -> Party:
    return make_party(
        gv_brand,
        PartyID(f'gv-{generate_token()}'),
        'GV Render Coverage #37',
        max_ticket_quantity=240,
    )


@pytest.fixture(scope='module')
def gv_board(gv_brand: Brand) -> Board:
    return board_service.create_board(gv_brand, BoardID(generate_token()))


@pytest.fixture(scope='module')
def gv_storefront(
    gv_brand: Brand,
    make_shop,
    make_order_number_sequence,
    make_storefront,
    make_product,
) -> Storefront:
    """A storefront with one orderable product, so the order form renders."""
    shop = make_shop(gv_brand)
    sequence = make_order_number_sequence(shop.id)
    make_product(shop.id, total_quantity=5)
    return make_storefront(shop.id, sequence.id)


@pytest.fixture(scope='module', autouse=True)
def gv_party_content(gv_party: Party) -> None:
    """Give the party a timetable and two seating areas.

    Without them the routes answer 404 and their templates stay unrendered.
    """
    timetable = timetable_service.create_timetable(gv_party, hidden=False)
    timetable_service.create_item(
        timetable.id,
        datetime(2014, 10, 24, 18, 0),
        'Einlass',
        None,
        None,
        None,
        hidden=False,
    )

    for _ in range(2):
        seating_area_service.create_area(
            gv_party.id,
            generate_token(),
            generate_token(),
            image_filename='hall.png',
            image_width=600,
            image_height=400,
        )


def _bound_to(site: Site, party_id: PartyID | None) -> Site:
    return site_service.update_site(
        site.id,
        site.title,
        site.server_name,
        party_id,
        site.enabled,
        site.user_account_creation_enabled,
        site.login_enabled,
        site.board_id,
        site.storefront_id,
        site.is_intranet,
        site.check_in_on_login,
        site.archived,
    )


@pytest.fixture(scope='module')
def gv_site(
    gv_party: Party, gv_board: Board, gv_storefront: Storefront
) -> Iterator[Site]:
    """The real on-disk `template_overrides` id, bound to our party."""
    previous = site_service.find_site(SITE_ID)
    if previous is None:
        site = create_site(
            SITE_ID,
            gv_party.brand_id,
            server_name='gv-render-coverage.test',
            party_id=gv_party.id,
            board_id=gv_board.id,
            storefront_id=gv_storefront.id,
        )
    else:
        site = _bound_to(previous, gv_party.id)

    yield site

    if previous is None:
        site_service.delete_site(SITE_ID)
    else:
        _bound_to(previous, previous.party_id)


@pytest.fixture(scope='module')
def gv_app(database, make_site_app, gv_site: Site) -> BycepsApp:
    app = make_site_app(gv_site.server_name, gv_site.id)
    with app.app_context():
        return app


@pytest.fixture
def anonymous(make_client, gv_app):
    return make_client(gv_app)


@pytest.fixture
def logged_in(make_user, make_client, gv_app):
    user = make_user()
    log_in_user(user.id)
    return make_client(gv_app, user_id=user.id)


def get_text(client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_route_enumeration_matches_the_site_app(gv_app) -> None:
    assert ROUTES == _get_routes(gv_app)
    assert len(ROUTES) >= MIN_ROUTE_COUNT
    for endpoint in (
        'homepage.index',
        'dashboard.index',
        'orga_team.index',
        'ticketing.index_mine',
        'pizza_delivery.board',
        'pizza_delivery.beamershow',
        'lan_tournament.index',
        'lan_tournament.my_requests',
        'lan_tournament.propose_form',
    ):
        assert endpoint in ROUTES.values()


@pytest.mark.parametrize(
    'rule', ['/board/', '/seating/', '/shop/order', '/timetable']
)
def test_configured_service_pages_render_instead_of_404(
    anonymous, rule: str
) -> None:
    assert rule in ROUTES

    html = get_text(anonymous, rule)

    assert 'KEIN SIGNAL' not in html


@pytest.mark.parametrize('rule', RULES)
def test_route_renders_without_server_error_anonymous(
    anonymous, rule: str
) -> None:
    response = anonymous.get(rule)

    assert response.status_code < 500


@pytest.mark.parametrize('rule', RULES)
def test_route_renders_without_server_error_logged_in(
    logged_in, rule: str
) -> None:
    response = logged_in.get(rule)

    assert response.status_code < 500


def test_html_pages_link_design_css(anonymous, logged_in) -> None:
    checked = 0
    offenders = []

    for who, client in (('anonymous', anonymous), ('logged in', logged_in)):
        for rule, endpoint in ROUTES.items():
            response = client.get(rule)
            if response.status_code != 200 or response.mimetype != 'text/html':
                continue

            html = response.get_data(as_text=True)
            stylesheet = (
                'style/gv-tokens.css'
                if endpoint in STANDALONE_ENDPOINTS
                else 'style/gv-base.css'
            )
            missing = []
            if stylesheet not in html:
                missing.append(stylesheet)
            if not re.search(r'<html[^>]* data-era="[1-3]"', html):
                missing.append('data-era= on <html>')
            if missing:
                offenders.append(f'{who} {rule}: {", ".join(missing)}')
            checked += 1

    assert not offenders, '\n'.join(offenders)
    # A pass must not come from skipping everything.
    assert checked >= MIN_ROUTE_COUNT


def test_homepage_renders_bands(anonymous) -> None:
    html = get_text(anonymous, '/')

    assert '<section class="hero"' in html
    assert '<header class="site-head">' in html
    # The party has a ticket limit, so the meter band renders.
    assert 'id="tickets"' in html
    assert 'meter-card' in html
    # The meter band is the only ticket meter on the page.
    assert 'class="tbar' not in html


def test_lan_tournament_page_loads_bridge_css(anonymous) -> None:
    html = get_text(anonymous, '/lan-tournaments/')

    assert html.count('style/gv-lan-tournament.css') == 1
    assert 'lt-dark' in html
    assert re.search(r'<body class="[^"]*\blt-dark\b', html)


def test_unknown_url_renders_site_404(anonymous) -> None:
    response = anonymous.get(f'/no-such-page-{generate_token()}')

    assert response.status_code == 404
    assert 'KEIN SIGNAL' in response.get_data(as_text=True)
