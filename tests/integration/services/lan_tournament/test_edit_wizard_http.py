"""
tests.integration.services.lan_tournament.test_edit_wizard_http
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin edit form, the update POST and the pre-check through a
real admin app, the way a browser without JavaScript would.
"""

from datetime import datetime, UTC
from io import BytesIO
import json
import os
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from PIL import Image
import pytest
from sqlalchemy import update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_image_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    CREATE_WIZARD_STEP_FIELDS,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.party.models import PartyID

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-edit-wizard-http')
SIBLING_PARTY_ID = PartyID('lan-party-edit-wizard-http-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-edit-wizard-http-foreign')

_S = 'byceps.services.lan_tournament.tournament_image_service'

FLOOR_MESSAGE = 'Whole numbers from 1 only.'
RELEASED_MESSAGE = 'The playoffs are released, so their settings are locked.'
UNAVAILABLE_MESSAGE = (
    'This image is no longer available. Please choose another.'
)

# What the wizard reads from `config.storedValues`.
_STORED_KEYS = {
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
    'group_size_min',
    'group_size_max',
    'advancement_count',
    'point_table',
    'start_time',
    'playoff_enabled',
}


@pytest.fixture(scope='module')
def edit_brand(make_brand):
    return make_brand('editwizardhttpbrand', 'Edit Wizard HTTP Brand')


@pytest.fixture(scope='module')
def party(make_party, edit_brand):
    return make_party(edit_brand, PARTY_ID, 'Edit Wizard HTTP Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, edit_brand):
    return make_party(edit_brand, SIBLING_PARTY_ID, 'Edit Wizard HTTP Sibling')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand(
        'editwizardhttpotherbrand', 'Edit Wizard HTTP Other Brand'
    )
    return make_party(other_brand, FOREIGN_PARTY_ID, 'Edit Wizard HTTP Foreign')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.update'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture(scope='module')
def viewer_client(make_client, admin_app, viewer):
    return make_client(admin_app, user_id=viewer.id)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party, sibling_party, foreign_party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


def _name(label: str) -> str:
    return f'{label} {uuid4().hex[:8]}'


def _set_row(tournament_id, **values) -> None:
    db.session.execute(
        update(DbTournament)
        .where(DbTournament.id == tournament_id)
        .values(**values)
    )
    db.session.commit()


def _stored(tournament_id) -> DbTournament:
    db.session.expire_all()
    return db.session.get(DbTournament, tournament_id)


def _solo(party, name, *, status='DRAFT', **row):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        name,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        min_players=4,
        max_players=16,
    ).unwrap()
    _set_row(tournament.id, tournament_status=status, **row)
    return tournament


def _ffa(party, name, *, status='DRAFT'):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        name,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=[3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
    ).unwrap()
    _set_row(tournament.id, tournament_status=status)
    return tournament


def _grouped(party, name, *, status='DRAFT', released=False):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        name,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        min_players=8,
        max_players=16,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    ).unwrap()
    row = {'tournament_status': status}
    if released:
        row['playoff_released_at'] = datetime.now(UTC)
    _set_row(tournament.id, **row)
    return tournament


def _form(name, **extra) -> dict:
    return {
        'category': 'MAIN',
        'name': name,
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'min_players': '4',
        'max_players': '16',
        'image_id': '',
        **extra,
    }


def _grouped_form(name, **extra) -> dict:
    grouped = {
        'elimination_mode': 'ROUND_ROBIN',
        'min_players': '8',
        'playoff_enabled': 'y',
        'playoff_group_count': '2',
        'playoff_qualifiers_per_group': '2',
        'playoff_elimination_mode': 'SINGLE_ELIMINATION',
        'playoff_release_mode': 'MANUAL',
    }
    return _form(name, **{**grouped, **extra})


def _form_url(tournament_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}/update'


def _update_url(tournament_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}'


def _validate_url(tournament_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}/update/validate'


def _config(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="lt-create-wizard-config">'
        r'(.*?)</script>',
        html,
        re.S,
    )
    assert match is not None
    return json.loads(match.group(1))


def _first_error_step(html: str) -> int | None:
    match = re.search(r'data-first-error-step="(\d+)"', html)
    return int(match.group(1)) if match else None


def _step_of(field: str) -> int:
    return next(
        index
        for index, names in enumerate(CREATE_WIZARD_STEP_FIELDS)
        if field in names
    )


def _flashes(client) -> str:
    with client.session_transaction() as session:
        flashes = session.pop('_flashes', None) or []
    return ' | '.join(str(message) for _, message in flashes)


def _png(size=(1920, 1080)) -> bytes:
    buf = BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buf, format='PNG')
    return buf.getvalue()


def _noise_png(size) -> bytes:
    buf = BytesIO()
    Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3)).save(
        buf, format='PNG'
    )
    return buf.getvalue()


def _store_image(party, creator_id, filename='stored.png'):
    return tournament_image_service.store_uploaded_image(
        party.id, creator_id, BytesIO(_png()), filename
    ).unwrap()


def _attach(tournament, image) -> None:
    tournament_service.update_tournament(
        tournament.id,
        name=tournament.name,
        contestant_type=tournament.contestant_type,
        game_format=tournament.game_format,
        elimination_mode=tournament.elimination_mode,
        min_players=tournament.min_players,
        max_players=tournament.max_players,
        image_id=image.id,
        image_alt_text='Stored alt',
    ).unwrap()


def _hidden_value(html: str, name: str) -> str:
    tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', html).group(0)
    value = re.search(r'value="([^"]*)"', tag)
    return value.group(1) if value else ''


# --------------------------------------------------------------------- #
# the form
# --------------------------------------------------------------------- #


def test_edit_page_renders_the_wizard(client, party):
    name = _name('Edit page')
    tournament = _solo(party, name)

    response = client.get(_form_url(tournament.id))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-lt-wizard' in html
    assert 'data-wiz-mode="edit"' in html
    assert f'action="/lan-tournaments/tournaments/{tournament.id}"' in html
    assert f'value="{name}"' in html
    assert 'name="from_request_id"' not in html
    assert 'name="submission_token"' not in html
    assert 'data-wiz-lock-banner' not in html
    assert _hidden_value(html, 'image_id') == ''

    config = _config(html)
    assert config['mode'] == 'edit'
    assert config['sessionKey'] is None
    assert config['requireStructure'] is False
    assert config['stagedImage'] is None
    assert config['lockedFields'] == []
    base = f'/lan-tournaments/tournaments/{tournament.id}'
    assert config['urls']['create'] == base
    assert config['urls']['validate'] == f'{base}/update/validate'
    assert config['urls']['upload'] == f'{base}/image'
    assert config['urls']['images'] == f'{base}/images'
    assert config['urls']['cancel'] == base
    assert set(config['storedValues']) == _STORED_KEYS
    assert config['storedValues']['min_players'] == '4'
    assert config['storedValues']['min_teams'] == ''
    assert config['storedValues']['playoff_enabled'] == ''


def test_edit_page_of_an_ongoing_tournament_shows_the_locks(client, party):
    tournament = _solo(party, _name('Edit ongoing'), status='ONGOING')

    response = client.get(_form_url(tournament.id))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-wiz-lock-banner' in html
    name_input = re.search(r'<input[^>]*name="name"[^>]*>', html).group(0)
    assert ' disabled ' in name_input
    locked = set(_config(html)['lockedFields'])
    assert {'name', 'max_players', 'game_format', 'playoff_enabled'} <= locked
    assert 'description' not in locked
    assert 'image_url' not in locked


def test_edit_page_shows_the_attached_image(client, party, admin):
    tournament = _solo(party, _name('Edit image'))
    image = _store_image(party, admin.id)
    _attach(tournament, image)

    response = client.get(_form_url(tournament.id))

    html = response.get_data(as_text=True)
    assert _hidden_value(html, 'image_id') == str(image.id)
    staged = _config(html)['stagedImage']
    assert staged['imageId'] == str(image.id)
    assert staged['filename'] == 'stored.png'
    assert 'Stored alt' in html


def test_edit_page_renders_a_legacy_row(client, party):
    tournament = _solo(
        party,
        _name('Edit legacy'),
        contestant_type=None,
        game_format=None,
        elimination_mode=None,
        min_players=0,
        max_players=5000,
    )

    response = client.get(_form_url(tournament.id))

    assert response.status_code == 200
    stored = _config(response.get_data(as_text=True))['storedValues']
    assert stored['min_players'] == '0'
    assert stored['max_players'] == '5000'


def test_edit_page_stored_values_read_like_the_inputs(client, party):
    tournament = _grouped(party, _name('Edit stored values'))
    _set_row(
        tournament.id,
        start_time=datetime(2026, 10, 10, 18, 30),
        point_table=json.dumps([10, 7, 5]),
    )

    response = client.get(_form_url(tournament.id))

    config = _config(response.get_data(as_text=True))
    expected_start = (
        datetime(2026, 10, 10, 18, 30, tzinfo=UTC)
        .astimezone(ZoneInfo(config['timezoneName']))
        .strftime('%Y-%m-%dT%H:%M')
    )
    stored = config['storedValues']
    assert stored['start_time'] == expected_start
    assert stored['point_table'] == '10, 7, 5'
    assert stored['playoff_enabled'] == 'y'
    assert stored['min_players'] == '8'
    assert stored['max_players'] == '16'


def test_edit_page_of_an_unknown_tournament_is_404(client, party):
    assert client.get(_form_url(uuid4())).status_code == 404


# --------------------------------------------------------------------- #
# the update POST
# --------------------------------------------------------------------- #


def test_edit_post_renames_a_draft(client, party):
    tournament = _solo(party, _name('Edit draft'))
    new_name = _name('Renamed draft')
    _flashes(client)

    response = client.post(
        _update_url(tournament.id),
        data=_form(new_name, description='Fresh text'),
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith(
        f'/tournaments/{tournament.id}'
    )
    assert f'Tournament "{new_name}" has been updated.' in _flashes(client)
    stored = _stored(tournament.id)
    assert stored.name == new_name
    assert stored.description == 'Fresh text'


def test_edit_post_field_error_rerenders_the_wizard_on_the_step(client, party):
    name = _name('Edit error')
    tournament = _solo(party, name)
    _flashes(client)

    response = client.post(
        _update_url(tournament.id), data=_form(name, min_players='0')
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-wiz-mode="edit"' in html
    assert FLOOR_MESSAGE in html
    assert _first_error_step(html) == _step_of('min_players')
    assert _config(html)['serverErrors'] == {'min_players': [FLOOR_MESSAGE]}
    assert _flashes(client) == ''
    assert _stored(tournament.id).min_players == 4


@pytest.mark.parametrize(
    ('data', 'field', 'message'),
    [
        (
            {'elimination_mode': 'NONE'},
            'elimination_mode',
            'Invalid combination of game format and elimination mode.',
        ),
        (
            {
                'game_format': 'FREE_FOR_ALL',
                'point_table': '5, x',
                'group_size_max': '4',
                'advancement_count': '1',
            },
            'point_table',
            'Point table must be comma-separated integers.',
        ),
    ],
)
def test_edit_post_domain_error_lands_on_its_field_not_on_a_flash(
    client, party, data, field, message
):
    name = _name('Edit domain error')
    tournament = _solo(party, name)
    _flashes(client)

    response = client.post(_update_url(tournament.id), data=_form(name, **data))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _config(html)['serverErrors'] == {field: [message]}
    assert _first_error_step(html) == _step_of(field)
    assert _flashes(client) == ''
    assert _stored(tournament.id).elimination_mode == 'SINGLE_ELIMINATION'


def test_edit_post_ongoing_injects_locked_values(client, party):
    name = _name('Edit locked')
    tournament = _solo(party, name, status='ONGOING', game='CS2')
    forged = _form(
        'Forged name',
        game='Forged game',
        category='SIDE',
        max_players='999',
        game_format='FREE_FOR_ALL',
        elimination_mode='DOUBLE_ELIMINATION',
        point_table='5',
        description='Allowed change',
    )

    response = client.post(_update_url(tournament.id), data=forged)

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.name == name
    assert stored.game == 'CS2'
    assert stored.category == 'MAIN'
    assert stored.max_players == 16
    assert stored.game_format == 'ONE_V_ONE'
    assert stored.elimination_mode == 'SINGLE_ELIMINATION'
    assert stored.point_table is None
    assert stored.description == 'Allowed change'


def test_edit_post_ongoing_ffa_fields_are_injected(client, party):
    tournament = _ffa(party, _name('Edit locked ffa'), status='ONGOING')

    response = client.post(
        _update_url(tournament.id),
        data={
            'description': 'Allowed',
            'point_table': '1',
            'group_size_min': '3',
            'group_size_max': '9',
            'advancement_count': '2',
        },
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert json.loads(stored.point_table) == [3, 2, 1]
    assert (stored.group_size_min, stored.group_size_max) == (2, 4)
    assert stored.advancement_count == 1
    assert stored.description == 'Allowed'


def test_edit_post_ongoing_without_the_disabled_fields_saves(client, party):
    tournament = _solo(party, _name('Edit browser'), status='ONGOING')

    response = client.post(
        _update_url(tournament.id),
        data={'description': 'Only this', 'ruleset': 'And this'},
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert (stored.description, stored.ruleset) == ('Only this', 'And this')
    assert stored.min_players == 4


def test_edit_post_released_playoff_change_is_refused_on_the_field(
    client, party
):
    name = _name('Edit released')
    tournament = _grouped(party, name, status='COMPLETED', released=True)
    _flashes(client)

    response = client.post(
        _update_url(tournament.id),
        data=_form(name, elimination_mode='SINGLE_ELIMINATION'),
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _config(html)['serverErrors'] == {
        'playoff_enabled': [RELEASED_MESSAGE]
    }
    assert _first_error_step(html) == _step_of('playoff_enabled')
    assert _flashes(client) == ''
    stored = _stored(tournament.id)
    assert stored.elimination_mode == 'ROUND_ROBIN'
    assert stored.playoff_group_count == 2


def test_edit_post_forged_playoff_fields_do_not_change_a_released_one(
    client, party
):
    name = _name('Edit forged playoff')
    tournament = _grouped(party, name, status='ONGOING', released=True)
    forged = _grouped_form(
        name,
        playoff_group_count='4',
        playoff_qualifiers_per_group='1',
        playoff_elimination_mode='DOUBLE_ELIMINATION',
        playoff_release_mode='AUTOMATIC',
        description='Allowed change',
    )

    response = client.post(_update_url(tournament.id), data=forged)

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.description == 'Allowed change'
    assert stored.playoff_group_count == 2
    assert stored.playoff_qualifiers_per_group == 2
    assert stored.playoff_elimination_mode == 'SINGLE_ELIMINATION'
    assert stored.playoff_release_mode == 'MANUAL'


def test_edit_post_playoff_started_switch_is_locked_before_the_release(
    client, party
):
    name = _name('Edit started playoff')
    tournament = _grouped(party, name, status='ONGOING')

    response = client.post(
        _update_url(tournament.id),
        data=_grouped_form(
            name, playoff_enabled='', playoff_group_count='5', description='X'
        ),
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.playoff_game_format == 'ONE_V_ONE'
    assert stored.playoff_group_count == 2
    assert stored.description == 'X'


def test_edit_post_legacy_values_still_save(client, party):
    name = _name('Edit legacy save')
    ongoing = _solo(
        party, name, status='ONGOING', min_players=0, max_players=5000
    )
    draft = _solo(
        party,
        _name('Edit legacy draft'),
        min_players=0,
        max_players=5000,
        min_teams=0,
    )

    locked = client.post(
        _update_url(ongoing.id), data={'description': 'Locked new'}
    )
    unchanged = client.post(
        _update_url(draft.id),
        data=_form(
            draft.name,
            min_players='0',
            max_players='5000',
            min_teams='0',
            description='Draft new',
        ),
    )
    changed = client.post(
        _update_url(draft.id),
        data=_form(draft.name, min_players='0', max_players='5001'),
    )

    assert locked.status_code == 302
    assert _stored(ongoing.id).description == 'Locked new'
    assert _stored(ongoing.id).min_players == 0
    assert unchanged.status_code == 302, unchanged.get_data(as_text=True)
    stored = _stored(draft.id)
    assert (stored.description, stored.min_players) == ('Draft new', 0)
    assert stored.max_players == 5000
    assert changed.status_code == 200
    assert 'At most' in changed.get_data(as_text=True)
    assert _stored(draft.id).max_players == 5000


def test_edit_post_attaches_an_uploaded_image_without_js(
    client, party, data_dir
):
    name = _name('Edit upload')
    tournament = _solo(party, name)

    response = client.post(
        _update_url(tournament.id),
        data=_form(
            name,
            image_alt_text='A blue field',
            image=(BytesIO(_png()), 'cover.png'),
        ),
        content_type='multipart/form-data',
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.image_id is not None
    assert stored.image_alt_text == 'A blue field'
    assert stored.image_url == (
        f'/data/parties/{PARTY_ID}/lan_tournament/images/{stored.image_id}.png'
    )
    assert list(data_dir.rglob(f'{stored.image_id}.png'))


def test_edit_post_accepts_a_body_above_the_core_limit(client, party):
    name = _name('Edit big upload')
    tournament = _solo(party, name)
    data = _noise_png((1200, 1200))  # ~4.3 MB, the core cap is 4,000,000
    assert 4_000_000 < len(data) < tournament_image_service.MAX_UPLOAD_BYTES

    response = client.post(
        _update_url(tournament.id),
        data=_form(name, image=(BytesIO(data), 'big.png')),
        content_type='multipart/form-data',
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    assert _stored(tournament.id).image_id is not None


def test_edit_post_upload_wins_over_a_stale_image_id(client, party):
    name = _name('Edit upload wins')
    tournament = _solo(party, name)

    response = client.post(
        _update_url(tournament.id),
        data=_form(
            name,
            image_id=str(uuid4()),
            image=(BytesIO(_png()), 'fresh.png'),
        ),
        content_type='multipart/form-data',
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    assert _stored(tournament.id).image_id is not None


def test_edit_post_rejects_an_upload_that_is_not_an_image(client, party):
    name = _name('Edit bad upload')
    tournament = _solo(party, name)

    response = client.post(
        _update_url(tournament.id),
        data=_form(
            name,
            description='Not saved',
            image=(BytesIO(b'<svg xmlns=""/>'), 'x.png'),
        ),
        content_type='multipart/form-data',
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'image' in _config(html)['serverErrors']
    assert _first_error_step(html) == _step_of('image')
    assert _stored(tournament.id).description is None


def test_edit_post_keeps_the_stored_image_when_its_id_comes_back(
    client, party, admin
):
    name = _name('Edit keep image')
    tournament = _solo(party, name)
    image = _store_image(party, admin.id)
    _attach(tournament, image)

    response = client.post(
        _update_url(tournament.id),
        data=_form(
            name,
            image_id=str(image.id),
            image_alt_text='Stored alt',
            description='Kept the image',
        ),
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.image_id == image.id
    assert stored.image_alt_text == 'Stored alt'
    assert stored.description == 'Kept the image'


def test_edit_post_with_an_empty_image_id_removes_the_image(
    client, party, admin
):
    name = _name('Edit remove image')
    tournament = _solo(party, name)
    _attach(tournament, _store_image(party, admin.id))

    response = client.post(
        _update_url(tournament.id), data=_form(name, image_id='')
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.image_id is None
    assert stored.image_url is None
    assert stored.image_alt_text is None


def test_edit_post_attaches_an_image_picked_from_another_party(
    client, party, sibling_party, admin
):
    name = _name('Edit pick')
    tournament = _solo(party, name)
    image = _store_image(sibling_party, admin.id, 'sibling.png')

    response = client.post(
        _update_url(tournament.id), data=_form(name, image_id=str(image.id))
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    assert _stored(tournament.id).image_id == image.id


def test_edit_post_refuses_an_image_of_another_brand(
    client, party, foreign_party, admin
):
    name = _name('Edit foreign image')
    tournament = _solo(party, name)
    foreign = _store_image(foreign_party, admin.id, 'foreign.png')

    response = client.post(
        _update_url(tournament.id),
        data=_form(name, image_id=str(foreign.id), description='Not saved'),
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _config(html)['serverErrors'] == {'image_id': [UNAVAILABLE_MESSAGE]}
    assert _first_error_step(html) == _step_of('image_id')
    assert _hidden_value(html, 'image_id') == ''
    stored = _stored(tournament.id)
    assert stored.image_id is None
    assert stored.description is None


def test_edit_post_keeps_an_attached_image_the_party_may_no_longer_pick(
    client, party, admin
):
    name = _name('Edit stale image')
    tournament = _solo(party, name)
    image = _store_image(party, admin.id, 'stale.png')
    _attach(tournament, image)

    with patch.object(
        tournament_image_service, 'find_attachable_image', return_value=None
    ):
        response = client.post(
            _update_url(tournament.id),
            data=_form(name, image_id=str(image.id), description='Saved'),
        )

    assert response.status_code == 302, response.get_data(as_text=True)
    stored = _stored(tournament.id)
    assert stored.image_id == image.id
    assert stored.description == 'Saved'


def test_edit_post_refuses_an_unknown_or_malformed_image_id(client, party):
    name = _name('Edit unknown image')
    tournament = _solo(party, name)

    unknown = client.post(
        _update_url(tournament.id),
        data=_form(name, image_id=str(uuid4())),
    )
    malformed = client.post(
        _update_url(tournament.id), data=_form(name, image_id='not-a-uuid')
    )

    for response in (unknown, malformed):
        assert response.status_code == 200
        assert _config(response.get_data(as_text=True))['serverErrors'] == {
            'image_id': [UNAVAILABLE_MESSAGE]
        }
    assert _stored(tournament.id).image_id is None


def test_edit_post_body_over_limit_returns_413_with_the_form(client, party):
    name = _name('Edit too big')
    tournament = _solo(party, name)
    body = b'\x89PNG\r\n\x1a\n' + b'\x00' * (
        tournament_image_service.MAX_REQUEST_BYTES + 1024
    )

    response = client.post(
        _update_url(tournament.id),
        data=_form(name, description='Lost', image=(BytesIO(body), 'x.png')),
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    html = response.get_data(as_text=True)
    assert 'data-wiz-mode="edit"' in html
    assert 'The maximum is 5 MB' in html
    assert 'Request Entity Too Large' not in html
    assert _stored(tournament.id).description is None


def test_edit_post_requires_the_update_permission(viewer_client, party):
    name = _name('Edit forbidden')
    tournament = _solo(party, name)

    get = viewer_client.get(_form_url(tournament.id))
    post = viewer_client.post(
        _update_url(tournament.id), data=_form('Hijacked')
    )

    assert get.status_code == 403
    assert post.status_code == 403
    assert _stored(tournament.id).name == name


# --------------------------------------------------------------------- #
# the pre-check
# --------------------------------------------------------------------- #


def test_validate_update_returns_json_and_writes_nothing(client, party):
    name = _name('Edit validate')
    tournament = _solo(party, name)
    before = _stored(tournament.id)
    snapshot = (before.name, before.min_players, before.updated_at)

    invalid = client.post(
        _validate_url(tournament.id), data=_form(name, min_players='0')
    )
    valid = client.post(
        _validate_url(tournament.id),
        data=_form('Would be renamed', description='Would be saved'),
    )

    assert invalid.status_code == 200
    body = invalid.get_json()
    assert body['ok'] is False
    assert body['errors'] == {'min_players': [FLOOR_MESSAGE]}
    assert body['first_error_step'] == _step_of('min_players')
    assert body['refusal'] is None
    assert re.fullmatch(r'\d\d:\d\d', body['checked_at'])
    assert valid.status_code == 200
    assert valid.get_json()['ok'] is True
    assert valid.get_json()['first_error_step'] is None
    after = _stored(tournament.id)
    assert (after.name, after.min_players, after.updated_at) == snapshot
    assert after.description is None


def test_validate_update_reports_the_errors_the_update_would(client, party):
    name = _name('Edit validate domain')
    tournament = _solo(party, name)

    response = client.post(
        _validate_url(tournament.id),
        data=_form(name, elimination_mode='NONE'),
    )

    body = response.get_json()
    assert body['ok'] is False
    assert body['errors'] == {
        'elimination_mode': [
            'Invalid combination of game format and elimination mode.'
        ]
    }
    assert body['first_error_step'] == _step_of('elimination_mode')


def test_validate_update_injects_the_locked_fields(client, party):
    tournament = _solo(party, _name('Edit validate ongoing'), status='ONGOING')

    response = client.post(
        _validate_url(tournament.id), data={'description': 'Only this'}
    )

    assert response.get_json()['ok'] is True


def test_validate_update_refuses_an_image_of_another_brand(
    client, party, foreign_party, admin
):
    name = _name('Edit validate foreign')
    tournament = _solo(party, name)
    foreign = _store_image(foreign_party, admin.id, 'foreign-v.png')

    response = client.post(
        _validate_url(tournament.id),
        data=_form(name, image_id=str(foreign.id)),
    )

    body = response.get_json()
    assert body['ok'] is False
    assert body['errors'] == {'image_id': [UNAVAILABLE_MESSAGE]}
    assert _stored(tournament.id).image_id is None


def test_validate_update_over_body_limit_is_json_413(client, party):
    tournament = _solo(party, _name('Edit validate big'))
    body = b'\x00' * (tournament_image_service.MAX_REQUEST_BYTES + 1024)

    response = client.post(
        _validate_url(tournament.id),
        data=_form('Too big', image=(BytesIO(body), 'x.png')),
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    assert 'The maximum is 5 MB' in response.get_json()['error']


def test_validate_update_requires_update_permission(viewer_client, party):
    name = _name('Edit validate forbidden')
    tournament = _solo(party, name)

    response = viewer_client.post(
        _validate_url(tournament.id), data=_form(name)
    )

    assert response.status_code == 403
    assert response.get_json() is None


def test_validate_update_of_an_unknown_tournament_is_404(client, party):
    response = client.post(_validate_url(uuid4()), data=_form('x'))

    assert response.status_code == 404
