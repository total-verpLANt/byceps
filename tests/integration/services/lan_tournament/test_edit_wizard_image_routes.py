"""
tests.integration.services.lan_tournament.test_edit_wizard_image_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the image upload, delete and list endpoints of the admin edit
wizard (`lan_tournament.update`) and the refactored create endpoints
through a real admin app.
"""

from datetime import datetime, UTC
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from PIL import Image
import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_image_repository,
    tournament_repository,
)
from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-edit-image-routes')
SIBLING_PARTY_ID = PartyID('lan-party-edit-image-routes-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-edit-image-routes-foreign')

_S = 'byceps.services.lan_tournament.tournament_image_service'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Edit Image Routes Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, brand):
    return make_party(brand, SIBLING_PARTY_ID, 'Edit Image Routes Sibling')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand(
        'edit-image-routes-other-brand', 'Edit Image Routes Foreign Co'
    )
    return make_party(
        other_brand, FOREIGN_PARTY_ID, 'Edit Image Routes Foreign'
    )


@pytest.fixture(scope='module')
def editor(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.update'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def other_editor(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.update'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def creator(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.create'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, editor):
    return make_client(admin_app, user_id=editor.id)


@pytest.fixture(scope='module')
def other_client(make_client, admin_app, other_editor):
    return make_client(admin_app, user_id=other_editor.id)


@pytest.fixture(scope='module')
def creator_client(make_client, admin_app, creator):
    return make_client(admin_app, user_id=creator.id)


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


@pytest.fixture
def make_tournament():
    def _wrapper(party_id: PartyID = PARTY_ID, *, image_id=None):
        tournament, _event = tournament_domain_service.create_tournament(
            party_id, f'Edit Image Cup {uuid4().hex[:8]}', image_id=image_id
        )
        tournament_repository.create_tournament(tournament)
        db.session.commit()
        return tournament

    return _wrapper


def _png(size=(1000, 600)) -> bytes:
    buf = BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buf, format='PNG')
    return buf.getvalue()


# fmt: off
_UPLOAD_REJECTIONS = [
    (b'<svg xmlns=""/>',                       'a.png', 415),
    (b'\x00' * (5 * 1024 * 1024 + 1024),       'a.png', 413),
    (b'\x00' * (5 * 1024 * 1024 + 300 * 1024), 'a.png', 413),
    (_png((100, 100)),                         'a.png', 400),
    (None,                                     None,    400),
]
# fmt: on


def _edit_upload_url(tournament_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}/image'


def _edit_delete_url(tournament_id, image_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}/image/{image_id}'


def _edit_list_url(tournament_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}/images'


def _create_upload_url(party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/create/image'


def _create_delete_url(image_id, party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/create/image/{image_id}'


def _create_list_url(party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/images'


def _post_file(client, url: str, data: bytes, filename='pic.png'):
    return client.post(
        url,
        data={'image': (BytesIO(data), filename)},
        content_type='multipart/form-data',
    )


def test_upload_edit_image_requires_update_permission(
    client, creator_client, make_tournament
):
    tournament = make_tournament()
    url = _edit_upload_url(tournament.id)

    forbidden = _post_file(creator_client, url, _png())
    allowed = _post_file(client, url, _png())

    assert forbidden.status_code == 403
    assert allowed.status_code == 201
    assert set(allowed.get_json()) == {
        'image_id',
        'url',
        'filename',
        'width',
        'height',
        'byte_size',
    }


def test_upload_edit_image_uses_the_tournament_party(
    client, editor, make_tournament, sibling_party
):
    tournament = make_tournament(SIBLING_PARTY_ID)

    response = _post_file(
        client, _edit_upload_url(tournament.id), _png(), 'Edit.png'
    )

    assert response.status_code == 201
    body = response.get_json()
    image = tournament_image_repository.find_image(
        TournamentImageID(UUID(body['image_id']))
    )
    assert image is not None
    assert image.party_id == SIBLING_PARTY_ID
    assert image.creator_id == editor.id
    assert body['url'].startswith(f'/data/parties/{SIBLING_PARTY_ID}/')


@pytest.mark.parametrize('tournament_id', [uuid4(), 'not-a-uuid'])
def test_edit_image_routes_return_404_for_an_unknown_tournament(
    client, editor, make_image, tournament_id
):
    image = make_image(creator_id=editor.id)

    upload = _post_file(client, _edit_upload_url(tournament_id), _png())
    delete = client.delete(_edit_delete_url(tournament_id, image.id))
    listing = client.get(_edit_list_url(tournament_id))

    assert upload.status_code == 404
    assert delete.status_code == 404
    assert listing.status_code == 404
    assert tournament_image_repository.find_image(image.id) is not None


@pytest.mark.parametrize(
    ('payload', 'filename', 'expected_status'),
    _UPLOAD_REJECTIONS,
    ids=[
        'not-an-image',
        'above-5-mib',
        'above-body-cap',
        'too-small',
        'no-file',
    ],
)
def test_upload_edit_image_status_mapping(
    client, make_tournament, payload, filename, expected_status
):
    url = _edit_upload_url(make_tournament().id)

    if payload is None:
        response = client.post(url, data={})
    else:
        response = _post_file(client, url, payload, filename)

    assert response.status_code == expected_status
    assert response.is_json
    assert 'error' in response.get_json()


def test_delete_edit_image_removes_own_unreferenced_image(
    client, editor, make_image, make_tournament
):
    tournament = make_tournament()
    image = make_image(creator_id=editor.id)

    response = client.delete(_edit_delete_url(tournament.id, image.id))

    assert response.status_code == 204
    assert response.get_data() == b''
    assert tournament_image_repository.find_image(image.id) is None


def test_delete_edit_image_refuses_a_referenced_image(
    client, editor, make_image, make_tournament
):
    image = make_image(creator_id=editor.id)
    tournament = make_tournament(image_id=image.id)

    response = client.delete(_edit_delete_url(tournament.id, image.id))

    assert response.status_code == 409
    assert response.is_json
    assert tournament_image_repository.find_image(image.id) is not None


def test_delete_edit_image_refuses_an_image_of_another_uploader(
    other_client, editor, make_image, make_tournament
):
    tournament = make_tournament()
    image = make_image(creator_id=editor.id)

    response = other_client.delete(_edit_delete_url(tournament.id, image.id))

    assert response.status_code == 403
    assert response.is_json
    assert tournament_image_repository.find_image(image.id) is not None


def test_delete_edit_image_scopes_to_the_tournament_party(
    client, editor, make_image, make_tournament, sibling_party
):
    tournament = make_tournament(PARTY_ID)
    image = make_image(party_id=SIBLING_PARTY_ID, creator_id=editor.id)

    response = client.delete(_edit_delete_url(tournament.id, image.id))

    assert response.status_code == 404
    assert tournament_image_repository.find_image(image.id) is not None


def test_delete_edit_image_with_malformed_image_id_returns_json_404(
    client, make_tournament
):
    tournament = make_tournament()

    response = client.delete(_edit_delete_url(tournament.id, 'not-a-uuid'))

    assert response.status_code == 404
    assert response.is_json


def test_delete_edit_image_requires_update_permission(
    creator_client, creator, make_image, make_tournament
):
    tournament = make_tournament()
    image = make_image(creator_id=creator.id)

    response = creator_client.delete(_edit_delete_url(tournament.id, image.id))

    assert response.status_code == 403
    assert tournament_image_repository.find_image(image.id) is not None


def test_list_edit_images_scopes_to_the_tournament_brand(
    client, editor, make_image, make_tournament, sibling_party, foreign_party
):
    own = make_image(creator_id=editor.id, filename='edit-list-own.png')
    sibling = make_image(
        party_id=SIBLING_PARTY_ID,
        creator_id=editor.id,
        filename='edit-list-sib.png',
    )
    foreign = make_image(
        party_id=FOREIGN_PARTY_ID,
        creator_id=editor.id,
        filename='edit-list-foreign.png',
    )
    url = _edit_list_url(make_tournament(PARTY_ID).id)

    party_scope = client.get(url, query_string={'q': 'edit-list-'})
    brand_scope = client.get(
        url, query_string={'scope': 'brand', 'q': 'edit-list-'}
    )

    assert party_scope.status_code == 200
    assert {i['image_id'] for i in party_scope.get_json()['items']} == {
        str(own.id)
    }
    assert brand_scope.status_code == 200
    brand_ids = {i['image_id'] for i in brand_scope.get_json()['items']}
    assert brand_ids == {str(own.id), str(sibling.id)}
    assert str(foreign.id) not in brand_ids


def test_list_edit_images_ignores_a_party_from_the_client(
    client, editor, make_image, make_tournament, sibling_party, foreign_party
):
    foreign = make_image(
        party_id=FOREIGN_PARTY_ID,
        creator_id=editor.id,
        filename='edit-ignore-foreign.png',
    )
    url = _edit_list_url(make_tournament(PARTY_ID).id)

    response = client.get(
        url,
        query_string={
            'scope': 'brand',
            'q': 'edit-ignore-',
            'party_id': FOREIGN_PARTY_ID,
        },
    )

    assert response.status_code == 200
    assert str(foreign.id) not in {
        i['image_id'] for i in response.get_json()['items']
    }


@pytest.mark.parametrize(
    'query_string', [{'scope': 'all'}, {'page': '0'}, {'page': 'abc'}]
)
def test_list_edit_images_rejects_bad_query_400(
    client, make_tournament, query_string
):
    response = client.get(
        _edit_list_url(make_tournament().id), query_string=query_string
    )

    assert response.status_code == 400
    assert response.is_json


def test_list_edit_images_requires_update_permission(
    creator_client, make_tournament
):
    response = creator_client.get(_edit_list_url(make_tournament().id))

    assert response.status_code == 403


@pytest.mark.parametrize(
    ('payload', 'filename', 'expected_status'),
    _UPLOAD_REJECTIONS,
    ids=[
        'not-an-image',
        'above-5-mib',
        'above-body-cap',
        'too-small',
        'no-file',
    ],
)
def test_create_image_routes_keep_their_status_mapping(
    creator_client, payload, filename, expected_status
):
    url = _create_upload_url()

    if payload is None:
        response = creator_client.post(url, data={})
    else:
        response = _post_file(creator_client, url, payload, filename)

    assert response.status_code == expected_status
    assert response.is_json
    assert 'error' in response.get_json()


def test_create_delete_route_keeps_its_status_mapping(
    creator_client, creator, make_image, make_tournament, sibling_party
):
    own = make_image(creator_id=creator.id)
    referenced = make_image(creator_id=creator.id)
    sibling_image = make_image(party_id=SIBLING_PARTY_ID, creator_id=creator.id)
    make_tournament(image_id=referenced.id)

    statuses = {
        'malformed': creator_client.delete(_create_delete_url('not-a-uuid')),
        'other_party': creator_client.delete(
            _create_delete_url(sibling_image.id)
        ),
        'referenced': creator_client.delete(_create_delete_url(referenced.id)),
        'own': creator_client.delete(_create_delete_url(own.id)),
    }

    assert {k: r.status_code for k, r in statuses.items()} == {
        'malformed': 404,
        'other_party': 404,
        'referenced': 409,
        'own': 204,
    }


def test_create_list_route_keeps_its_status_mapping(creator_client):
    bad_scope = creator_client.get(
        _create_list_url(), query_string={'scope': 'all'}
    )
    ok = creator_client.get(_create_list_url())

    assert bad_scope.status_code == 400
    assert ok.status_code == 200


def test_edit_wizard_urls_point_at_the_edit_routes(admin_app, make_tournament):
    tournament = make_tournament()
    path = f'/lan-tournaments/tournaments/{tournament.id}'

    with admin_app.test_request_context(
        path, base_url='http://admin.acmecon.test'
    ):
        urls = views._edit_wizard_urls(tournament)

    assert {k: v for k, v in urls.items() if k != 'validate'} == {
        'create': path,
        'upload': f'{path}/image',
        'delete_template': f'{path}/image/__ID__',
        'images': f'{path}/images',
        'cancel': path,
    }
