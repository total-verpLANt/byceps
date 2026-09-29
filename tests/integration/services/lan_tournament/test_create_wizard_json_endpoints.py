"""
tests.integration.services.lan_tournament.test_create_wizard_json_endpoints
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the image upload, delete and list endpoints of the admin create
wizard through a real admin app.
"""

from datetime import datetime, UTC
from io import BytesIO
import os
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from PIL import Image
import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_image_repository,
    tournament_image_service,
    tournament_repository,
)
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-json-endpoints')
SIBLING_PARTY_ID = PartyID('lan-party-json-endpoints-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-json-endpoints-foreign')

_S = 'byceps.services.lan_tournament.tournament_image_service'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'JSON Endpoints Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, brand):
    return make_party(brand, SIBLING_PARTY_ID, 'JSON Endpoints Sibling')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand('json-endpoints-other-brand', 'JSON Endpoints Co')
    return make_party(other_brand, FOREIGN_PARTY_ID, 'JSON Endpoints Foreign')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.create'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def other_admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.create'})
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
def other_client(make_client, admin_app, other_admin):
    return make_client(admin_app, user_id=other_admin.id)


@pytest.fixture(scope='module')
def viewer_client(make_client, admin_app, viewer):
    return make_client(admin_app, user_id=viewer.id)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party, sibling_party, foreign_party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


@pytest.fixture
def make_image():
    def _wrapper(
        *, party_id: PartyID = PARTY_ID, creator_id, filename='seed.png'
    ) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=creator_id,
            created_at=datetime.now(UTC),
            filename=filename,
            image_type=ImageType.png,
            width=1920,
            height=1080,
            byte_size=10,
        )
        tournament_image_repository.create_image(image)
        db.session.commit()
        return image

    return _wrapper


def _png(size=(1000, 600)) -> bytes:
    buf = BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buf, format='PNG')
    return buf.getvalue()


def _noise_png(size) -> bytes:
    buf = BytesIO()
    Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3)).save(
        buf, format='PNG'
    )
    return buf.getvalue()


def _upload_url(party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/create/image'


def _delete_url(image_id, party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/create/image/{image_id}'


def _list_url(party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/images'


def _post_file(client, data: bytes, filename='pic.png', party_id=PARTY_ID):
    return client.post(
        _upload_url(party_id),
        data={'image': (BytesIO(data), filename)},
        content_type='multipart/form-data',
    )


def test_upload_png_returns_201_json(client, party, admin, data_dir):
    response = _post_file(client, _png((3840, 2160)), 'Banner.png')

    assert response.status_code == 201
    body = response.get_json()
    assert set(body) == {
        'image_id',
        'url',
        'filename',
        'width',
        'height',
        'byte_size',
    }
    assert body['filename'] == 'Banner.png'
    assert (body['width'], body['height']) == (1920, 1080)
    assert body['url'].startswith(f'/data/parties/{PARTY_ID}/')
    assert body['url'].endswith(f'{body["image_id"]}.png')

    image = tournament_image_repository.find_image(
        TournamentImageID(UUID(body['image_id']))
    )
    assert image is not None
    assert image.creator_id == admin.id
    assert image.byte_size == body['byte_size']


def test_upload_body_above_core_limit_is_accepted(client, party):
    data = _noise_png((1200, 1200))  # ~4.3 MB, core cap is 4,000,000
    assert 4_000_000 < len(data) < tournament_image_service.MAX_UPLOAD_BYTES

    response = _post_file(client, data)

    assert response.status_code == 201, response.get_data(as_text=True)


def test_upload_above_5_mib_returns_json_413(client, party):
    size = tournament_image_service.MAX_REQUEST_BYTES + 4096

    response = _post_file(client, b'\x00' * size)

    assert response.status_code == 413
    assert response.is_json
    assert response.get_json()['error'].endswith('The maximum is 5 MB.')


def test_upload_file_just_above_5_mib_returns_json_413_with_size(client, party):
    data = b'\x00' * (tournament_image_service.MAX_UPLOAD_BYTES + 1024)

    response = _post_file(client, data)

    assert response.status_code == 413
    error = response.get_json()['error']
    assert re.fullmatch(r'The file is 5[.,]0 MB\. The maximum is 5 MB\.', error)


def test_upload_svg_returns_415(client, party):
    response = _post_file(client, b'<svg xmlns=""/>', 'a.png')

    assert response.status_code == 415
    assert response.is_json
    assert 'error' in response.get_json()


def test_upload_without_file_returns_400(client, party):
    response = client.post(_upload_url(), data={})

    assert response.status_code == 400
    assert response.get_json() == {'error': 'No file selected.'}


def test_upload_to_unknown_party_returns_404(client, party):
    response = _post_file(client, _png(), party_id='no-such-party')

    assert response.status_code == 404


def test_upload_requires_create_permission(viewer_client, party):
    response = _post_file(viewer_client, _png())

    assert response.status_code == 403


def test_delete_own_unreferenced_image_204(client, admin, make_image):
    image = make_image(creator_id=admin.id)

    response = client.delete(_delete_url(image.id))

    assert response.status_code == 204
    assert response.get_data() == b''
    assert tournament_image_repository.find_image(image.id) is None


def test_delete_foreign_creator_image_403(other_client, admin, make_image):
    image = make_image(creator_id=admin.id)

    response = other_client.delete(_delete_url(image.id))

    assert response.status_code == 403
    assert response.is_json
    assert tournament_image_repository.find_image(image.id) is not None


def test_delete_referenced_image_409(client, admin, make_image):
    image = make_image(creator_id=admin.id)
    tournament, _event = tournament_domain_service.create_tournament(
        PARTY_ID, 'JSON Delete Guard Cup', image_id=image.id
    )
    tournament_repository.create_tournament(tournament)
    db.session.commit()

    response = client.delete(_delete_url(image.id))

    assert response.status_code == 409
    assert response.is_json
    assert tournament_image_repository.find_image(image.id) is not None


def test_delete_image_of_other_party_returns_404(
    client, admin, make_image, sibling_party
):
    image = make_image(party_id=SIBLING_PARTY_ID, creator_id=admin.id)

    response = client.delete(_delete_url(image.id))

    assert response.status_code == 404
    assert tournament_image_repository.find_image(image.id) is not None


def test_delete_with_malformed_image_id_returns_json_404(client, party):
    response = client.delete(_delete_url('not-a-uuid'))

    assert response.status_code == 404
    assert response.is_json


def test_delete_requires_create_permission(viewer_client, admin, make_image):
    image = make_image(creator_id=admin.id)

    response = viewer_client.delete(_delete_url(image.id))

    assert response.status_code == 403
    assert tournament_image_repository.find_image(image.id) is not None


def test_list_images_brand_scope_excludes_other_brand(
    client, admin, make_image, sibling_party, foreign_party
):
    own = make_image(creator_id=admin.id, filename='list-own.png')
    sibling = make_image(
        party_id=SIBLING_PARTY_ID, creator_id=admin.id, filename='list-sib.png'
    )
    foreign = make_image(
        party_id=FOREIGN_PARTY_ID,
        creator_id=admin.id,
        filename='list-foreign.png',
    )

    party_scope = client.get(_list_url(), query_string={'q': 'list-'})
    brand_scope = client.get(
        _list_url(), query_string={'scope': 'brand', 'q': 'list-'}
    )

    assert party_scope.status_code == 200
    party_ids = {i['image_id'] for i in party_scope.get_json()['items']}
    assert party_ids == {str(own.id)}

    assert brand_scope.status_code == 200
    body = brand_scope.get_json()
    assert {i['image_id'] for i in body['items']} == {
        str(own.id),
        str(sibling.id),
    }
    assert str(foreign.id) not in {i['image_id'] for i in body['items']}
    assert body['page'] == 1
    assert body['has_next'] is False

    item = next(i for i in body['items'] if i['image_id'] == str(sibling.id))
    assert set(item) == {
        'image_id',
        'url',
        'filename',
        'width',
        'height',
        'byte_size',
        'party_title',
        'used_by',
        'created_at',
    }
    assert item['party_title'] == 'JSON Endpoints Sibling'
    assert item['used_by'] == []


def test_list_images_rejects_unknown_scope_400(client, party):
    response = client.get(_list_url(), query_string={'scope': 'all'})

    assert response.status_code == 400
    assert response.is_json


@pytest.mark.parametrize('page', ['0', '-1', 'abc', '10001', '9' * 40])
def test_list_images_rejects_bad_page_400(client, party, page):
    response = client.get(_list_url(), query_string={'page': page})

    assert response.status_code == 400
    assert response.is_json


def test_list_images_requires_create_permission(viewer_client, party):
    response = viewer_client.get(_list_url())

    assert response.status_code == 403
