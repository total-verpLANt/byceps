"""
tests.integration.services.lan_tournament.test_maintenance_views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, UTC
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_image_repository,
    tournament_image_service,
    tournament_repository,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_image import (
    DbTournamentImage,
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

PARTY_ID = PartyID('lan-party-maintenance-views')
FOREIGN_PARTY_ID = PartyID('lan-party-maintenance-views-foreign')

_S = 'byceps.services.lan_tournament.tournament_image_service'

_ENGLISH = {'Accept-Language': 'en'}


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('maintenance-views-brand', 'Maintenance Views Brand')
    return make_party(brand, PARTY_ID, 'Maintenance Views Party')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    brand = make_brand(
        'maintenance-views-foreign-brand', 'Maintenance Views Foreign Brand'
    )
    return make_party(brand, FOREIGN_PARTY_ID, 'Maintenance Views Foreign')


@pytest.fixture(scope='module')
def maintainer(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.maintain'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def administrator(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.administrate'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, maintainer):
    return make_client(admin_app, user_id=maintainer.id)


@pytest.fixture(scope='module')
def administrator_client(make_client, admin_app, administrator):
    return make_client(admin_app, user_id=administrator.id)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


@pytest.fixture(autouse=True)
def clean_images(party, foreign_party):
    yield
    db.session.rollback()
    db.session.execute(
        DbTournament.__table__.delete().where(
            DbTournament.party_id.in_([PARTY_ID, FOREIGN_PARTY_ID])
        )
    )
    db.session.execute(
        DbTournamentImage.__table__.delete().where(
            DbTournamentImage.party_id.in_([PARTY_ID, FOREIGN_PARTY_ID])
        )
    )
    db.session.commit()


@pytest.fixture
def make_unused_image(maintainer):
    def _wrapper(
        *, age: timedelta = timedelta(days=2), party_id: PartyID = PARTY_ID
    ) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=maintainer.id,
            created_at=datetime.now(UTC) - age,
            filename='seed.png',
            image_type=ImageType.png,
            width=1920,
            height=1080,
            byte_size=2048,
        )
        tournament_image_repository.create_image(image)
        db.session.commit()
        return image

    return _wrapper


@pytest.fixture
def make_orphan_file():
    def _wrapper(*, age: timedelta = timedelta(days=2)):
        directory = tournament_image_service.get_party_image_dir(PARTY_ID)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{uuid4()}.png'
        path.write_bytes(b'x' * 1024)
        mtime = (datetime.now(UTC) - age).timestamp()
        os.utime(path, (mtime, mtime))
        return path

    return _wrapper


def _path(url: str) -> str:
    return url.removeprefix('http://admin.acmecon.test')


def _maintenance_url() -> str:
    return f'{BASE_URL}/for_party/{PARTY_ID}/maintenance'


def test_maintenance_page_requires_the_permission(
    client, administrator_client, party
):
    assert client.get(_maintenance_url()).status_code == 200
    assert administrator_client.get(_maintenance_url()).status_code == 403


def test_maintenance_page_shows_both_actions_with_counts(
    client, party, make_unused_image, make_orphan_file
):
    make_unused_image()
    make_unused_image()
    make_unused_image(age=timedelta(hours=1))
    make_orphan_file()

    response = client.get(_maintenance_url(), headers=_ENGLISH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Unused tournament images' in html
    assert 'Orphaned image files' in html
    assert '<strong>2 images</strong>, together 4 KB.' in html
    assert '1 more is younger than 24 hours and stays.' in html
    assert '<strong>1 file</strong>, 1 KB.' in html
    assert html.count('Show preview') == 2
    assert '/maintenance/unused-images' in html
    assert '/maintenance/orphaned-files' in html


def test_maintenance_page_shows_clean_state_without_button(client, party):
    response = client.get(_maintenance_url(), headers=_ENGLISH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('Nothing to clean up.') == 2
    assert 'An unused image appears here 24 hours after its upload.' in html
    assert 'Show preview' not in html


def _preview_url(action_id: str) -> str:
    return f'{_maintenance_url()}/{action_id}'


def test_preview_lists_eligible_items_checked_and_kept_items_without_checkbox(
    client, party, make_unused_image
):
    old = make_unused_image(age=timedelta(days=3))
    recent = make_unused_image(age=timedelta(hours=2))

    response = client.get(_preview_url('unused-images'), headers=_ENGLISH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('name="key"') == 1
    assert f'value="{old.id}" checked data-bytes="2048"' in html
    assert f'value="{recent.id}"' not in html
    assert 'Stay' in html
    assert 'Younger than 24 hours.' in html
    assert 'data-label-one="Delete %(count)s image (%(size)s)"' in html
    assert 'data-label-many="Delete %(count)s images (%(size)s)"' in html
    assert 'data-label-none="Nothing selected"' in html
    assert '/maintenance/unused-images"' in html
    assert 'Maintenance Views Party' in html
    assert 'after deleting' not in html.lower()


def test_preview_of_orphaned_files_lists_names_without_uploader(
    client, party, make_orphan_file
):
    path = make_orphan_file()

    response = client.get(_preview_url('orphaned-files'), headers=_ENGLISH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'value="{path.name}" checked data-bytes="1024"' in html
    assert 'Uploaded by' not in html
    assert 'Last modified' in html


def test_preview_without_items_shows_the_empty_state(client, party):
    response = client.get(_preview_url('unused-images'), headers=_ENGLISH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'lt-maint-empty' in html
    assert 'name="key"' not in html
    assert '<form' not in html


def test_preview_unknown_action_is_404(client, party):
    response = client.get(_preview_url('no-such-action'))

    assert response.status_code == 404


def test_preview_requires_the_permission(administrator_client, party):
    response = administrator_client.get(_preview_url('unused-images'))

    assert response.status_code == 403


def test_run_action_stub_requires_the_permission(administrator_client, party):
    response = administrator_client.post(_preview_url('unused-images'))

    assert response.status_code == 403


@pytest.fixture
def make_tournament(party):
    def _wrapper(name: str, **kwargs):
        tournament, _event = tournament_domain_service.create_tournament(
            PARTY_ID, name, **kwargs
        )
        tournament_repository.create_tournament(tournament)
        db.session.commit()
        return tournament

    return _wrapper


def _write_image_file(image: TournamentImage):
    path = tournament_image_service.get_image_file_path(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * image.byte_size)
    return path


def _row_exists_in_fresh_session(image_id) -> bool:
    with SqlaSession(bind=db.engine) as session:
        return (
            session.scalar(
                select(DbTournamentImage.id).where(
                    DbTournamentImage.id == image_id
                )
            )
            is not None
        )


def test_post_deletes_posted_eligible_images_and_redirects_with_flash(
    client, party, make_unused_image
):
    first = make_unused_image()
    second = make_unused_image()
    kept = make_unused_image()
    first_path = _write_image_file(first)
    second_path = _write_image_file(second)

    response = client.post(
        _preview_url('unused-images'),
        data={'key': [str(first.id), str(second.id)]},
        headers=_ENGLISH,
    )

    assert response.status_code == 302
    assert response.location == _path(_maintenance_url())
    assert not _row_exists_in_fresh_session(first.id)
    assert not _row_exists_in_fresh_session(second.id)
    assert _row_exists_in_fresh_session(kept.id)
    assert not first_path.exists()
    assert not second_path.exists()

    page = client.get(_maintenance_url(), headers=_ENGLISH)
    assert '2 images deleted, 4 KB freed.' in page.get_data(as_text=True)


def test_post_reports_skipped_image_that_became_referenced(
    client, party, make_unused_image, make_tournament
):
    free = make_unused_image()
    referenced = make_unused_image()
    make_tournament('Views Cup', image_id=referenced.id)

    response = client.post(
        _preview_url('unused-images'),
        data={'key': [str(free.id), str(referenced.id)]},
        headers=_ENGLISH,
    )

    assert response.status_code == 302
    assert not _row_exists_in_fresh_session(free.id)
    assert _row_exists_in_fresh_session(referenced.id)

    page = client.get(_maintenance_url(), headers=_ENGLISH)
    html = page.get_data(as_text=True)
    assert '1 image deleted, 2 KB freed.' in html
    assert '1 image was skipped because a tournament uses it now.' in html


def test_post_without_keys_flashes_nothing_selected(
    client, party, make_unused_image
):
    image = make_unused_image()

    response = client.post(_preview_url('unused-images'), headers=_ENGLISH)

    assert response.status_code == 302
    assert response.location == _path(_preview_url('unused-images'))
    assert _row_exists_in_fresh_session(image.id)

    page = client.get(_preview_url('unused-images'), headers=_ENGLISH)
    assert 'Nothing selected.' in page.get_data(as_text=True)


def test_post_with_foreign_party_image_key_deletes_nothing(
    client, party, foreign_party, make_unused_image
):
    foreign = make_unused_image(party_id=FOREIGN_PARTY_ID)
    path = _write_image_file(foreign)

    response = client.post(
        _preview_url('unused-images'),
        data={'key': [str(foreign.id)]},
        headers=_ENGLISH,
    )

    assert response.status_code == 302
    assert _row_exists_in_fresh_session(foreign.id)
    assert path.exists()


def test_post_deletes_orphaned_files_with_the_file_flash(
    client, party, make_orphan_file
):
    path = make_orphan_file()

    response = client.post(
        _preview_url('orphaned-files'),
        data={'key': [path.name, '../escape.png']},
        headers=_ENGLISH,
    )

    assert response.status_code == 302
    assert not path.exists()

    page = client.get(_maintenance_url(), headers=_ENGLISH)
    html = page.get_data(as_text=True)
    assert '1 file deleted, 1 KB freed.' in html
    assert '1 file was skipped because it no longer qualifies.' in html


def test_post_reports_failed_file_deletion_per_action(
    client, party, make_unused_image, make_orphan_file
):
    image = make_unused_image()
    _write_image_file(image)
    orphan = make_orphan_file()
    unlink = (
        'byceps.services.lan_tournament.tournament_maintenance_service._unlink'
    )

    with patch(unlink, return_value=False):
        client.post(
            _preview_url('unused-images'),
            data={'key': [str(image.id)]},
            headers=_ENGLISH,
        )
        html = client.get(_maintenance_url(), headers=_ENGLISH).get_data(
            as_text=True
        )
        assert (
            '1 file could not be deleted from disk. It now appears under'
            ' &#34;Orphaned image files&#34;.'
        ) in html

        client.post(
            _preview_url('orphaned-files'),
            data={'key': [orphan.name]},
            headers=_ENGLISH,
        )
        html = client.get(_maintenance_url(), headers=_ENGLISH).get_data(
            as_text=True
        )
        assert '1 file could not be deleted from disk.' in html
        assert 'It now appears' not in html


def test_post_unknown_action_is_404(client, party):
    response = client.post(_preview_url('no-such-action'), data={'key': ['x']})

    assert response.status_code == 404


def test_get_does_not_delete(client, party, make_unused_image):
    image = make_unused_image()
    path = _write_image_file(image)

    response = client.get(
        f'{_preview_url("unused-images")}?key={image.id}', headers=_ENGLISH
    )
    assert response.status_code == 200
    response = client.open(
        _preview_url('unused-images'), method='PUT', data={'key': str(image.id)}
    )
    assert response.status_code == 405

    assert _row_exists_in_fresh_session(image.id)
    assert path.exists()


def test_post_of_an_already_deleted_key_flashes_nothing_deleted(client, party):
    response = client.post(
        _preview_url('unused-images'),
        data={'key': [str(uuid4())]},
        headers=_ENGLISH,
    )

    assert response.status_code == 302
    html = client.get(_maintenance_url(), headers=_ENGLISH).get_data(
        as_text=True
    )
    assert 'Nothing was deleted.' in html
    assert '1 image was skipped because it no longer qualifies.' in html
    assert 'freed' not in html
    assert 'a tournament uses' not in html


def test_post_with_only_an_in_use_image_flashes_nothing_deleted_and_in_use(
    client, party, make_unused_image, make_tournament
):
    referenced = make_unused_image()
    make_tournament('Views In Use Cup', image_id=referenced.id)

    client.post(
        _preview_url('unused-images'),
        data={'key': [str(referenced.id)]},
        headers=_ENGLISH,
    )

    html = client.get(_maintenance_url(), headers=_ENGLISH).get_data(
        as_text=True
    )
    assert 'Nothing was deleted.' in html
    assert '1 image was skipped because a tournament uses it now.' in html
    assert 'no longer qualifies' not in html
    assert 'freed' not in html


def test_post_of_a_vanished_orphan_key_flashes_nothing_deleted(client, party):
    client.post(
        _preview_url('orphaned-files'),
        data={'key': [f'{uuid4()}.png']},
        headers=_ENGLISH,
    )

    html = client.get(_maintenance_url(), headers=_ENGLISH).get_data(
        as_text=True
    )
    assert 'Nothing was deleted.' in html
    assert '1 file was skipped because it no longer qualifies.' in html
    assert 'freed' not in html
