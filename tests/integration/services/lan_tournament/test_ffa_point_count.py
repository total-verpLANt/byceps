"""
tests.integration.services.lan_tournament.test_ffa_point_count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The admin update route rejects a point table with too many places in
the admin's language, before the service is reached.
"""

from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from flask_babel import get_babel
import pytest

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
)
from byceps.services.party.models import PartyID

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-ffa-point-count')

TRANSLATIONS_DIR = Path(__file__).parents[4] / 'byceps' / 'translations'

MAX_PLACES = tournament_domain_service.MAX_POINT_TABLE_PLACES

GERMAN_MESSAGE = f'Höchstens {MAX_PLACES} Plätze.'
ENGLISH_MESSAGE = f'At most {MAX_PLACES} places.'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'FFA Point Count Party')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.update'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def german_app(make_admin_app, tmp_path_factory):
    """An admin app that reads `messages.po` instead of the stale `.mo`."""
    directory = tmp_path_factory.mktemp('lt_point_count_translations')
    po_path = TRANSLATIONS_DIR / 'de' / 'LC_MESSAGES' / 'messages.po'
    with po_path.open('rb') as f:
        catalog = read_po(f, locale='de')
    mo_path = directory / 'de' / 'LC_MESSAGES' / 'messages.mo'
    mo_path.parent.mkdir(parents=True)
    with mo_path.open('wb') as f:
        write_mo(f, catalog)

    app = make_admin_app('admin.acmecon.test')
    babel = get_babel(app)
    babel.default_directories = [str(directory)]
    babel.translation_directories = [str(directory)]
    app.babel_instance.domain_instance.cache.clear()
    return app


@pytest.fixture(scope='module')
def client(german_app, admin):
    with http_client(german_app, user_id=admin.id) as client:
        yield client


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


def _update(client, tournament, point_table: str, *, name: str | None = None):
    return client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        headers={'Accept-Language': 'de-DE,de;q=0.9'},
        data={
            'name': name or tournament.name,
            'contestant_type': 'SOLO',
            'game_format': 'FREE_FOR_ALL',
            'elimination_mode': 'SINGLE_ELIMINATION',
            'max_players': '8',
            'point_table': point_table,
            'group_size_min': '2',
            'group_size_max': '4',
            'advancement_count': '1',
        },
    )


def _table(places: int, *, last: int = 1) -> str:
    return ', '.join(['1'] * (places - 1) + [str(last)])


def test_update_with_too_many_places_says_so_in_german(client, party):
    tournament = _make_ffa_tournament('Too many places', [10, 6, 3, 1])

    response = _update(client, tournament, _table(MAX_PLACES + 1))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count(GERMAN_MESSAGE) == 1
    assert ENGLISH_MESSAGE not in html


def test_update_with_too_many_places_changes_nothing(client, party):
    tournament = _make_ffa_tournament('Too many, untouched', [10, 6, 3, 1])
    before = tournament_service.get_tournament(tournament.id)

    _update(client, tournament, _table(MAX_PLACES + 1), name='Renamed')

    assert tournament_service.get_tournament(tournament.id) == before
    assert before.point_table == [10, 6, 3, 1]


def test_update_reports_the_place_count_before_the_values(client, party):
    tournament = _make_ffa_tournament('Count before values', [10, 6, 3, 1])

    response = _update(client, tournament, _table(MAX_PLACES + 1, last=10**12))

    html = response.get_data(as_text=True)
    assert GERMAN_MESSAGE in html
    assert 'Punkte pro Platz' not in html


def test_update_with_the_highest_allowed_place_count_is_stored(client, party):
    tournament = _make_ffa_tournament('Just enough places', [10, 6, 3, 1])

    response = _update(client, tournament, _table(MAX_PLACES))

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.point_table == [1] * MAX_PLACES
