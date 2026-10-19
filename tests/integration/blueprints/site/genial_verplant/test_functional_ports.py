"""
tests.integration.blueprints.site.genial_verplant.test_functional_ports
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Check that every override carrying a function core lacks (chair coupons and
reminders, orga departments, pizza claim forms, tournament-request pages,
homepage tournament cards) still carries it on the `genial-verplant` site.
"""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, UTC
import re

import pytest

from byceps.byceps_app import BycepsApp
from byceps.database import db
from byceps.services.brand.models import Brand, BrandID
from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.orga_team import orga_team_service
from byceps.services.party.dbmodels import DbParty
from byceps.services.party.models import Party, PartyID
from byceps.services.pizza_delivery import pizza_delivery_service
from byceps.services.seating import seating_area_service
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
)
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.services.user.models import User

from tests.helpers import create_site, generate_token, log_in_user


SITE_ID = SiteID('genial-verplant')


@pytest.fixture(scope='module')
def gv_brand(make_brand) -> Brand:
    return make_brand(BrandID(f'gv-{generate_token()}'), 'gv functional ports')


@pytest.fixture(scope='module')
def gv_party(make_party, gv_brand: Brand) -> Iterator[Party]:
    party = make_party(
        gv_brand,
        PartyID(f'gv-{generate_token()}'),
        'GV Functional Ports #37',
        max_ticket_quantity=240,
    )
    db.session.get(DbParty, party.id).ticket_management_enabled = True
    db.session.commit()

    yield party


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
def gv_site(gv_party: Party) -> Iterator[Site]:
    """The real on-disk `template_overrides` id, bound to our party."""
    previous = site_service.find_site(SITE_ID)
    if previous is None:
        site = create_site(
            SITE_ID,
            gv_party.brand_id,
            server_name='gv-functional-ports.test',
            party_id=gv_party.id,
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
def reader(make_user, make_client, gv_app):
    user = make_user()
    log_in_user(user.id)
    return user, make_client(gv_app, user_id=user.id)


@pytest.fixture
def make_participant(
    make_user, make_client, gv_app, gv_party, make_ticket_category
):
    """Provide a logged-in user who uses a ticket with no chair answer."""

    def _make():
        user = make_user()
        category = make_ticket_category(gv_party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, user, user=user
        )
        log_in_user(user.id)
        return user, ticket, make_client(gv_app, user_id=user.id)

    return _make


@pytest.fixture
def make_tournament(gv_party) -> Iterator[Callable[..., Tournament]]:
    """Create tournaments; delete them again after the test."""
    created: list[Tournament] = []

    def _make(name: str, **kwargs) -> Tournament:
        tournament, _ = tournament_service.create_tournament(
            gv_party.id,
            name,
            **{
                'tournament_status': TournamentStatus.REGISTRATION_OPEN,
                'game_format': GameFormat.ONE_V_ONE,
                'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                **kwargs,
            },
        ).unwrap()
        created.append(tournament)
        return tournament

    yield _make

    for tournament in created:
        tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def make_pizza_entry(gv_party, make_user):
    """Create pizza entries; delete them again after the test."""
    creator = make_user()
    created = []

    def _make(number: str, *, user_id=None, called: bool = False):
        entry = pizza_delivery_service.create_entry(
            gv_party.id, number, user_id, creator.id
        ).unwrap()
        created.append(entry)
        if called:
            pizza_delivery_service.deliver_entry(entry.id).unwrap()
        return entry

    yield _make

    for entry in created:
        pizza_delivery_service.delete_entry(entry.id)


def get_text(client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    return response.get_data(as_text=True)


def answer_chair(ticket: DbTicket, user: User) -> None:
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.venue, user
    ).unwrap()


def test_dashboard_shows_ticket_coupon(reader) -> None:
    _, client = reader

    html = get_text(client, '/dashboard')

    assert '<aside class="tv-coupon ds-coupon"' in html
    assert re.search(
        r'<a class="tv-coupon__cta" href="/shop/order">Tickets sichern</a>',
        html,
    )
    assert '<span class="tv-coupon__num">240</span>' in html
    assert 'dashboard-chair-coupon' not in html


def test_dashboard_shows_chair_coupon_when_pending(make_participant) -> None:
    user, ticket, client = make_participant()

    pending = get_text(client, '/dashboard')
    answer_chair(ticket, user)
    answered = get_text(client, '/dashboard')

    assert '<aside class="tv-coupon dashboard-chair-coupon"' in pending
    assert re.search(
        rf'<a class="tv-coupon__cta" href="/tickets/mine#ticket-{ticket.id}">',
        pending,
    )
    assert 'ds-coupon' not in pending  # the ticket coupon is for ticketless
    assert 'tv-coupon' not in answered
    assert 'ds-coupon' not in answered  # the user has a ticket now


def test_seating_area_shows_chair_reminder(make_participant, gv_party) -> None:
    user, ticket, client = make_participant()
    area = seating_area_service.create_area(
        gv_party.id,
        generate_token(),
        generate_token(),
        image_filename='hall.png',
        image_width=600,
        image_height=400,
    )

    pending = get_text(client, f'/seating/areas/{area.slug}')
    answer_chair(ticket, user)
    answered = get_text(client, f'/seating/areas/{area.slug}')

    assert '<div class="tv-reminder chair-prompt">' in pending
    assert re.search(
        r'<a class="button chair-information-link" '
        rf'href="/tickets/mine#ticket-{ticket.id}">',
        pending,
    )
    assert 'seatplan-frame' in pending
    assert 'chair-prompt' not in answered
    assert 'seatplan-frame' in answered


def test_ticketing_mine_shows_chair_information(make_participant) -> None:
    user, ticket, client = make_participant()

    html = get_text(client, '/tickets/mine')

    assert html.count(f'id="ticket-{ticket.id}"') == 1
    assert 'class="field chair-information tv-chair-info"' in html
    assert 'data-chair-source="unknown"' in html
    # Rental is off for the party: only the two real answers are offered.
    assert html.count(' data-action="set-chair-source"') == 2
    assert 'data-chair-source="user"' in html
    assert 'data-chair-source="venue"' in html
    assert 'data-chair-source="rental"' not in html
    assert 'behavior/chair-information.js' in html
    assert '<h1 class="title">Meine Eintrittskarten</h1>' in html


def test_user_profile_shows_chair_information(
    make_participant, reader, gv_party
) -> None:
    user, ticket, client = make_participant()
    _, other_client = reader

    own = get_text(client, f'/users/{user.id}')
    foreign = get_text(other_client, f'/users/{user.id}')

    assert '<div class="tv-chair-info">' in own
    assert f'Ticket {ticket.code}</span>' in own
    assert 'tv-chair-info__state is-pending' in own
    assert re.search(
        r'<a class="tv-chair-info__action" '
        rf'href="/tickets/mine#ticket-{ticket.id}">',
        own,
    )
    assert 'tv-chair-info' not in foreign  # only the owner sees it
    assert ticket.code not in foreign


def test_orga_team_groups_by_department(anonymous, gv_party, make_user) -> None:
    tech, catering, other = (
        orga_team_service.create_team(gv_party.id, title)
        for title in ('Technik', 'Catering', 'Zebra')
    )
    solo, both, outsider = make_user(), make_user(), make_user()
    orga_team_service.create_membership(catering.id, solo.id, 'Netzwerk')
    orga_team_service.create_membership(tech.id, both.id, 'Kueche')
    orga_team_service.create_membership(catering.id, both.id, 'Rack')
    orga_team_service.create_membership(other.id, outsider.id, None)

    html = get_text(anonymous, '/orgas/')

    sections = html.split('<section class="tv-dept">')[1:]
    departments = [re.search(r'<h2>([^<]*)</h2>', s).group(1) for s in sections]
    # Priority departments first, in their fixed order, the rest after.
    assert departments == ['Catering', 'Technik', 'Zebra']
    names = [re.findall(r'class="tv-dept__name">', s) for s in sections]
    assert [len(n) for n in names] == [2, 1, 1]
    assert 'class="tv-dept__count">2 Mitglieder</span>' in sections[0]
    assert 'class="tv-dept__count">1 Mitglied</span>' in sections[1]
    # The person in two departments is filed under both, with their duty
    # for each one.
    assert both.screen_name in sections[0] and both.screen_name in sections[1]
    assert 'title="Funktion">Kueche</span>' in sections[1]
    assert 'title="Funktion">Rack</span>' in sections[0]
    # No duty, no role chip.
    assert 'tv-dept__role' not in sections[2]
    assert 'tv-dept__empty' not in html


def test_pizza_board_renders_claim_form(reader, anonymous) -> None:
    _, client = reader

    logged_in = get_text(client, '/pizza/')
    logged_out = get_text(anonymous, '/pizza/')

    assert (
        '<form class="tv-pizza-claim" id="pz-claim" method="post"' in logged_in
    )
    assert 'action="/pizza/claim"' in logged_in
    assert '<input type="hidden" name="next" value="board">' in logged_in
    assert 'id="pz-number" name="number"' in logged_in
    assert re.search(
        r'<button class="button color-primary" type="submit">', logged_in
    )
    # A visitor gets a link to log in instead of a form.
    assert 'id="pz-claim"' not in logged_out
    assert 'class="button color-primary pz-side-btn"' in logged_out


def test_pizza_my_status_renders_claim_form(reader, make_pizza_entry) -> None:
    user, client = reader

    empty = get_text(client, '/pizza/my-status')
    make_pizza_entry(f'MY-{generate_token()}', user_id=user.id)
    followed = get_text(client, '/pizza/my-status')
    rejected = client.post('/pizza/claim', data={'number': 'nope-9'})

    assert 'pz-status--empty' in empty
    assert 'id="pz-claim"' in empty
    assert 'pz-slip pz-slip--wait' in followed
    assert 'pz-status--empty' not in followed
    assert 'id="pz-claim"' in followed
    # A wrong number re-renders the form with the error and the input.
    html = rejected.get_data(as_text=True)
    assert rejected.status_code == 422
    assert 'pz-fld pz-fld--err' in html
    assert 'class="pz-err" id="pz-number-err"' in html
    assert 'aria-invalid="true"' in html
    assert 'value="NOPE-9"' in html


def test_pizza_beamershow_renders(anonymous, make_pizza_entry) -> None:
    empty = get_text(anonymous, '/pizza/beamershow')
    number = f'BM-{generate_token()}'.upper()
    make_pizza_entry(number, called=True)
    called = get_text(anonymous, '/pizza/beamershow')

    for html in (empty, called):
        assert '<html' in html and 'data-era="1" data-theme="dark"' in html
        assert '<body class="tv-beamer">' in html
        assert 'style/gv-tokens.css' in html
        assert 'style/gv-services-event.css' in html
        # A standalone page: no site layout around it.
        assert 'site-head' not in html
        assert 'style/gv-base.css' not in html
    assert 'class="hb-empty"' in empty
    assert f'<b>{number}</b>' in called
    assert 'class="hb-empty"' not in called


def test_lan_tournament_my_requests_renders(reader, gv_party) -> None:
    user, client = reader
    now = datetime.now(UTC)
    name = f'Request Cup {generate_token()}'
    tournament_request_service.submit_request(
        gv_party.id,
        user.id,
        party_capacity=None,
        name=name,
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=now + timedelta(days=1),
        preferred_end_time=now + timedelta(days=1, hours=4),
        description='A friendly cup.',
    ).unwrap()

    html = get_text(client, '/lan-tournaments/requests')

    assert '<div class="request-page">' in html
    assert re.search(r'<body class="[^"]*\blt-dark\b', html)
    assert f'<div class="nm">{name}</div>' in html
    assert '<article class="req card-open">' in html
    assert '<span class="st st-submitted">' in html
    assert 'href="/lan-tournaments/requests/propose"' in html
    # The page CSS comes last, after the module CSS and the bridge.
    assert html.index('style/gv-lan-tournament.css') < html.index(
        'style/gv-lt-requests.css'
    )


def test_lan_tournament_propose_form_renders(reader, make_participant) -> None:
    _, no_ticket_client = reader
    _, _, client = make_participant()

    html = get_text(client, '/lan-tournaments/requests/propose')
    no_ticket = get_text(no_ticket_client, '/lan-tournaments/requests/propose')

    assert '<div class="request-page">' in html
    assert re.search(r'<body class="[^"]*\blt-dark\b', html)
    assert (
        '<form method="post" action="/lan-tournaments/requests/propose"' in html
    )
    assert '<div class="seg" id="game_format">' in html
    assert '<label class="seg-option' in html
    assert 'class="opts" id="elimination_mode"' in html
    assert 'name="game_format"' in html and 'name="elimination_mode"' in html
    assert 'data-capacity="240"' in html
    assert 'class="warn"' not in html
    # Without a ticket the form warns, as the TV36 page did.
    assert '<div class="warn-h">Ticket required</div>' in no_ticket
    assert html.index('style/gv-lan-tournament.css') < html.index(
        'style/gv-lt-requests.css'
    )


def test_lan_tournament_my_tournaments_renders(reader, make_tournament) -> None:
    user, client = reader
    name = f'Personal Cup {generate_token()}'
    tournament = make_tournament(name)
    tournament_participant_service.admin_add_participant(
        tournament.id, user.id
    ).unwrap()

    html = get_text(client, '/lan-tournaments/mine')

    assert '<div class="personal-tournaments">' in html
    assert '<header class="gv-pagehead"><h1>' in html
    assert 'class="gv-folio-path"' in html
    assert re.search(r'<body class="[^"]*\blt-dark\b', html)
    assert name in html
    assert 'style/lan_tournament_personal.css' in html
    assert html.index('style/gv-lan-tournament.css') < html.index(
        'style/gv-lt-requests.css'
    )


def test_homepage_lists_tournament_cards(anonymous, make_tournament) -> None:
    token = generate_token()
    live = make_tournament(
        f'Live {token}', tournament_status=TournamentStatus.ONGOING
    )
    open_ = make_tournament(f'Open {token}', max_players=16)
    draft = make_tournament(
        f'Draft {token}', tournament_status=TournamentStatus.DRAFT
    )

    html = get_text(anonymous, '/')

    assert '<section class="band" id="turniere"' in html
    assert '<div class="tgrid">' in html
    assert html.count('<a class="tcard"') == 2
    assert f'href="/lan-tournaments/{live.id}"' in html
    assert f'href="/lan-tournaments/{open_.id}"' in html
    assert 'class="chip live"' in html
    assert 'class="chip open"' in html
    # The live tournament leads, a draft never reaches a visitor.
    assert html.index(live.name) < html.index(open_.name)
    assert draft.name not in html
    # The sidebar table and the webring link follow the cards.
    assert 'Turniere und Belegung' in html
    assert 'Turniere »' in html
