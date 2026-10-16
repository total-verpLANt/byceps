"""Render the Kantine pages through the real `totalverplant-36` overrides."""

from collections.abc import Iterator
from datetime import datetime, timedelta, UTC
from pathlib import Path
import re

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from flask_babel import get_babel
import pytest

from byceps.byceps_app import BycepsApp
from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.pizza_delivery import pizza_delivery_service
from byceps.services.pizza_delivery.dbmodels import DbPizzaDeliveryEntry
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID

from tests.helpers import create_site, log_in_user


TRANSLATIONS_DIR = Path(__file__).resolve().parents[5] / 'byceps/translations'
SITE_ID = SiteID('totalverplant-36')


@pytest.fixture(scope='module')
def tv36_party(make_party, brand):
    return make_party(brand, PartyID('pizza-tv36-party'), 'Pizza tv36 Party')


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
def tv36_site(tv36_party) -> Iterator[Site]:
    """The real on-disk `template_overrides` id, bound to our party."""
    previous = site_service.find_site(SITE_ID)
    if previous is None:
        site = create_site(
            SITE_ID,
            tv36_party.brand_id,
            server_name='pizza-tv36.test',
            party_id=tv36_party.id,
        )
    else:
        site = _bound_to(previous, tv36_party.id)

    yield site

    if previous is None:
        site_service.delete_site(SITE_ID)
    else:
        _bound_to(previous, previous.party_id)


@pytest.fixture(scope='module')
def german_translations(tmp_path_factory) -> Path:
    """Compile `messages.po` into a temporary catalogue (no `.mo` in the repo)."""
    target = tmp_path_factory.mktemp('pizza_tv36_translations')
    for po_path in sorted(TRANSLATIONS_DIR.glob('*/LC_MESSAGES/messages.po')):
        locale = po_path.parent.parent.name
        with po_path.open('rb') as f:
            catalog = read_po(f, locale=locale)
        mo_path = target / locale / 'LC_MESSAGES' / 'messages.mo'
        mo_path.parent.mkdir(parents=True)
        with mo_path.open('wb') as f:
            write_mo(f, catalog)
    return target


@pytest.fixture(scope='module')
def tv36_app(
    database, make_site_app, tv36_site: Site, german_translations: Path
) -> BycepsApp:
    app = make_site_app(tv36_site.server_name, tv36_site.id)
    babel = get_babel(app)
    babel.default_directories = [str(german_translations)]
    babel.translation_directories = [str(german_translations)]
    app.babel_instance.domain_instance.cache.clear()
    with app.app_context():
        return app


def _german(client):
    client.environ_base['HTTP_ACCEPT_LANGUAGE'] = 'de'
    return client


@pytest.fixture
def anonymous(make_client, tv36_app):
    return _german(make_client(tv36_app))


@pytest.fixture
def reader(make_user, make_client, tv36_app):
    user = make_user()
    log_in_user(user.id)
    return user, _german(make_client(tv36_app, user_id=user.id))


@pytest.fixture
def entries(tv36_party, make_user):
    """Create entries; delete them again after the test."""
    creator = make_user()
    created = []

    def _make(number, *, user_id=None, called_minutes_ago=None):
        entry = pizza_delivery_service.create_entry(
            tv36_party.id, number, user_id, creator.id
        ).unwrap()
        created.append(entry)

        if called_minutes_ago is not None:
            pizza_delivery_service.deliver_entry(entry.id).unwrap()
            db_entry = db.session.get(DbPizzaDeliveryEntry, entry.id)
            db_entry.delivered_at = datetime.now(UTC) - timedelta(
                minutes=called_minutes_ago
            )
            db.session.commit()

        return entry

    yield _make

    for entry in created:
        pizza_delivery_service.delete_entry(entry.id)


def get_text(client, url) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def lead_number(html: str) -> str:
    return re.search(r'class="pz-lead-no">([^<]*)<', html).group(1)


def test_board_lead_is_the_newest_call_and_pending_stays_off(
    anonymous, entries
) -> None:
    entries('OLD-1', called_minutes_ago=50)
    entries('NEW-1', called_minutes_ago=1)
    entries('MID-1', called_minutes_ago=20)
    entries('WAITING-1')

    html = get_text(anonymous, '/pizza/')

    assert 'Die <em>Pizzatafel</em>' in html
    assert 'Abholbereite Pizzen · neueste zuerst' in html
    assert lead_number(html) == 'NEW-1'
    assert html.index('MID-1') < html.index('OLD-1')
    assert 'WAITING-1' not in html
    assert '3 Nummern aufgerufen' in html
    assert re.search(r'Stand \d\d:\d\d Uhr', html)


def test_board_marks_fresh_numbers_without_relying_on_colour(
    anonymous, entries
) -> None:
    entries('FRESH-1', called_minutes_ago=2)
    entries('STALE-1', called_minutes_ago=40)

    html = get_text(anonymous, '/pizza/')

    assert 'Frisch aus dem Ofen' in html
    assert '✶ = frisch, unter 10 Minuten' in html
    assert '<li class="pz-no pz-no--fresh' not in html  # the lead has the tag
    assert 'class="pz-tag pz-tag--fresh"' in html


def test_board_refresh_can_be_paused(anonymous) -> None:
    running = get_text(anonymous, '/pizza/')
    paused = get_text(anonymous, '/pizza/?refresh=0')

    assert '<meta http-equiv="refresh" content="30">' in running
    assert 'Aktualisierung anhalten' in running
    assert 'http-equiv="refresh"' not in paused
    assert 'Aktualisierung fortsetzen' in paused


def test_board_logged_out_offers_login_not_a_name(anonymous) -> None:
    html = get_text(anonymous, '/pizza/')

    assert 'Du wartest auf eine Pizza?' in html
    assert 'Anmelden' in html
    assert 'Mein Abholschein' not in html
    assert 'id="pz-claim"' not in html


def test_board_empty_state(anonymous) -> None:
    html = get_text(anonymous, '/pizza/')

    assert 'Es wurden noch keine Lieferungen aufgerufen.' in html
    assert 'class="pz-empty"' in html
    assert 'class="pz-lead' not in html


def test_board_single_number_has_no_register(anonymous, entries) -> None:
    entries('ONLY-1', called_minutes_ago=5)

    html = get_text(anonymous, '/pizza/')

    assert 'Bisher nur diese eine.' in html
    assert 'Vorher aufgerufen' not in html


def test_board_flash_for_called_number(reader, entries) -> None:
    user, client = reader
    entries('LEAD-1', called_minutes_ago=1)
    entries('MINE-1', user_id=user.id, called_minutes_ago=30)

    html = get_text(client, '/pizza/')

    assert '<section class="pz-flash" role="status"' in html
    assert '<span>Eilmeldung</span>' in html
    assert 'Deine Nummer wurde aufgerufen!' in html
    assert 'Aufgerufen: MINE-1' in html  # tab title
    assert re.search(r'<li class="pz-no[^"]*pz-no--mine"><b>MINE-1', html)
    assert 'Deine Pizza liegt an der Essensausgabe. Bitte abholen.' in html


def test_board_flash_names_every_called_number(reader, entries) -> None:
    user, client = reader
    entries('MINE-A', user_id=user.id, called_minutes_ago=3)
    entries('MINE-B', user_id=user.id, called_minutes_ago=4)

    html = get_text(client, '/pizza/')

    assert 'Deine Nummern wurden aufgerufen!' in html
    assert 'Aufgerufen: MINE-A, MINE-B' in html


def test_board_reader_strip_for_waiting_numbers(reader, entries) -> None:
    user, client = reader
    entries('WAIT-1', user_id=user.id)

    html = get_text(client, '/pizza/')

    assert 'WAIT-1</b> ist noch nicht aufgerufen.' in html
    assert 'Du bekommst zusätzlich eine E-Mail.' in html
    assert '>Noch unterwegs</span>' in html
    assert 'class="pz-flash"' not in html
    assert '1 Nummer verfolgt' in html


def test_board_reader_strip_lists_several_waiting_numbers(
    reader, entries
) -> None:
    user, client = reader
    for number in ('W-1', 'W-2', 'W-3'):
        entries(number, user_id=user.id)

    html = get_text(client, '/pizza/')

    assert re.search(
        r'Noch nicht aufgerufen: <b class="pz-num">W-\d</b>, '
        r'<b class="pz-num">W-\d</b> und <b class="pz-num">W-\d</b>\.',
        html,
    )
    assert 'Du bekommst jeweils eine E-Mail.' in html
    assert '3 Nummern verfolgt' in html


def test_board_reader_without_numbers_gets_the_claim_form(reader) -> None:
    _, client = reader

    html = get_text(client, '/pizza/')

    assert 'Noch keine Lieferung mit deinem Konto verknüpft.' in html
    assert 'Noch keine Nummer verfolgt' in html
    assert 'name="next" value="board"' in html
    assert 'id="pz-claim"' in html


def test_board_caps_at_200_and_says_so(anonymous, entries) -> None:
    for i in range(201):
        entries(f'CAP-{i:03}', called_minutes_ago=300 - i // 10)

    html = get_text(anonymous, '/pizza/')

    assert html.count('<li class="pz-no') == 199
    assert 'Abgedruckt sind die <b>200</b>' not in html
    assert 'Abgedruckt sind die 200 jüngsten Nummern.' in html


def test_numbers_are_escaped_everywhere(anonymous, entries) -> None:
    entries('X<B>1', called_minutes_ago=2)
    entries('Y<I>2', called_minutes_ago=9)

    for url in ('/pizza/', '/pizza/beamershow'):
        html = get_text(anonymous, url)

        assert 'X&lt;B&gt;1' in html
        assert '<B>' not in html and '<I>' not in html


def test_my_status_without_numbers(reader) -> None:
    _, client = reader

    html = get_text(client, '/pizza/my-status')

    assert 'Mein <em>Abholschein</em>' in html
    assert 'pz-status--empty' in html
    assert 'Noch keine Lieferung mit deinem Konto verknüpft' in html
    assert 'Zurück zur Übersicht' in html


def test_my_status_puts_called_slips_first(reader, entries) -> None:
    user, client = reader
    entries('SLIP-WAIT', user_id=user.id)
    entries('SLIP-READY', user_id=user.id, called_minutes_ago=4)

    html = get_text(client, '/pizza/my-status')

    assert html.index('SLIP-READY') < html.index('SLIP-WAIT')
    assert 'coupon pz-slip pz-slip--ready' in html
    assert 'coupon pz-slip pz-slip--wait' in html
    assert '>Abholbereit</span>' in html and '>Noch unterwegs</span>' in html
    assert 'Noch nicht aufgerufen. Wir melden uns.' in html
    assert re.search(r'Aufgerufen um \d\d:\d\d Uhr\.', html)
    # Only a waiting slip has a "since" line: a called entry has no claim time.
    assert html.count('Verfolgt seit') == 1
    assert '2 Nummern verfolgt' in html


def test_claim_error_keeps_input_and_names_nobody(
    reader, make_user, entries
) -> None:
    _, client = reader
    other = make_user()
    entries('TAKEN-1', user_id=other.id)

    response = client.post('/pizza/claim', data={'number': 'taken-1'})
    html = response.get_data(as_text=True)

    assert response.status_code == 422
    assert 'Diese Nummer verfolgt schon jemand.' in html
    assert 'aria-invalid="true"' in html
    assert 'value="TAKEN-1"' in html
    assert other.screen_name not in html


def test_claim_unknown_number_is_a_field_error(reader) -> None:
    _, client = reader

    response = client.post('/pizza/claim', data={'number': 'nope-9'})
    html = response.get_data(as_text=True)

    assert response.status_code == 422
    assert 'Pizzanummer nicht gefunden.' in html
    assert 'value="NOPE-9"' in html


def test_claim_success_flashes_in_german(reader, entries) -> None:
    _, client = reader
    entries('FOLLOW-1')

    response = client.post('/pizza/claim', data={'number': 'follow-1'})
    assert response.status_code == 302

    html = get_text(client, response.location)
    assert 'Du verfolgst jetzt FOLLOW-1.' in html
    assert 'color-success' in html


def test_claim_called_number_flashes_a_warning(reader, entries) -> None:
    _, client = reader
    entries('LATE-1', called_minutes_ago=6)

    response = client.post(
        '/pizza/claim', data={'number': 'late-1', 'next': 'board'}
    )
    assert response.status_code == 302

    html = get_text(client, response.location)
    assert (
        'LATE-1 wurde schon aufgerufen. Geh sofort zur Essensausgabe.' in html
    )
    assert 'color-warning' in html


def test_beamershow_is_a_standalone_dark_headband(anonymous, entries) -> None:
    entries('OLDER-1', called_minutes_ago=30)
    entries('NEWER-1', called_minutes_ago=2)

    html = get_text(anonymous, '/pizza/beamershow')

    assert 'data-theme="dark"' in html
    assert '<meta http-equiv="refresh" content="10">' in html
    assert 'Abholbereit · neueste zuerst' in html
    assert html.index('NEWER-1') < html.index('OLDER-1')
    assert '<li class="hb-fresh"><i>&#10038;</i><b>NEWER-1</b></li>' in html
    assert '<li><b>OLDER-1</b></li>' in html
    assert '<div class="hb-more" data-more="-10" hidden>' in html
    assert 'class="page-header-fixed"' not in html
    assert 'localStorage' not in html


def test_beamershow_counts_the_numbers_beyond_the_band(
    anonymous, entries
) -> None:
    for i in range(205):
        entries(f'BM-{i:03}', called_minutes_ago=300 - i // 10)

    html = get_text(anonymous, '/pizza/beamershow')

    # All numbers are the same size: twelve in the band, the rest as a count.
    assert html.count('<b>BM-') == 12
    assert '<div class="hb-more" data-more="193">' in html
    assert '<b>+193</b>' in html
    assert '<span>weitere</span><span>/pizza</span>' in html


def test_beamershow_empty_state(anonymous) -> None:
    html = get_text(anonymous, '/pizza/beamershow')

    assert 'Es wurden noch keine Lieferungen aufgerufen.' in html
    assert 'Die erste Pizza ist unterwegs.' in html
    assert 'class="hb-row"' not in html
