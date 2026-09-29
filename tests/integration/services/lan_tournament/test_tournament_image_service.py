"""
tests.integration.services.lan_tournament.test_tournament_image_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, UTC
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
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
from byceps.services.lan_tournament.dbmodels.tournament_image import (
    DbTournamentImage,
)
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType
from byceps.util.uuid import generate_uuid4, generate_uuid7


PARTY_ID = PartyID('lan-party-image-service')
SIBLING_PARTY_ID = PartyID('lan-party-image-service-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-image-service-foreign')

_S = 'byceps.services.lan_tournament.tournament_image_service'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Image Service Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, brand):
    return make_party(brand, SIBLING_PARTY_ID, 'Image Service Sibling')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand('image-service-other-brand', 'Other Brand')
    return make_party(other_brand, FOREIGN_PARTY_ID, 'Image Service Foreign')


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('ImageServiceCreator')


@pytest.fixture(scope='module')
def other_user(make_user):
    return make_user('ImageServiceOtherUser')


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party, sibling_party, foreign_party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


@pytest.fixture
def make_image(creator):
    def _wrapper(
        *,
        party_id: PartyID = PARTY_ID,
        creator_id=None,
        created_at: datetime | None = None,
        with_file: bool = True,
    ) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=creator_id if creator_id is not None else creator.id,
            created_at=created_at if created_at is not None else _now(),
            filename='seed.png',
            image_type=ImageType.png,
            width=1920,
            height=1080,
            byte_size=10,
        )
        tournament_image_repository.create_image(image)
        db.session.commit()
        if with_file:
            path = tournament_image_service.get_image_file_path(image)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'seed')
        return image

    return _wrapper


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


def _now() -> datetime:
    return datetime.now(UTC)


def _png(size=(1000, 600)) -> BytesIO:
    buf = BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buf, format='PNG')
    buf.seek(0)
    return buf


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


def test_store_uploaded_image_writes_file_and_row(party, creator):
    result = tournament_image_service.store_uploaded_image(
        PARTY_ID, creator.id, _png((3840, 2160)), 'C:\\pics\\Banner.png'
    )

    image = result.unwrap()
    path = tournament_image_service.get_image_file_path(image)
    assert path.is_file()
    assert path.parent.name == 'images'
    assert path.suffix == '.png'
    with Image.open(path) as stored:
        assert stored.format == 'PNG'
        assert stored.size == (1920, 1080)
    assert (image.width, image.height) == (1920, 1080)
    assert image.byte_size == path.stat().st_size
    assert image.filename == 'Banner.png'
    assert image.creator_id == creator.id
    assert image.created_at.tzinfo is not None
    assert _row_exists_in_fresh_session(image.id)
    assert tournament_image_repository.find_image(image.id) == image


def test_store_rejects_invalid_upload_without_row_or_file(
    party, creator, data_dir
):
    result = tournament_image_service.store_uploaded_image(
        PARTY_ID, creator.id, BytesIO(b'<svg xmlns=""/>'), 'a.png'
    )

    assert (
        result.unwrap_err().msgid == tournament_image_service.IMAGE_TYPE_ERROR
    )
    assert not list(data_dir.rglob('*.png'))


def test_store_rolls_back_row_when_file_store_fails(party, creator, data_dir):
    before = tournament_image_repository.count_images(
        [PARTY_ID], filename_query='rollback-probe.png'
    )

    with patch(f'{_S}.upload.store', side_effect=OSError('disk full')):
        with pytest.raises(OSError, match='disk full'):
            tournament_image_service.store_uploaded_image(
                PARTY_ID, creator.id, _png(), 'rollback-probe.png'
            )

    with SqlaSession(bind=db.engine) as session:
        rows = session.scalars(
            select(DbTournamentImage.id).where(
                DbTournamentImage.filename == 'rollback-probe.png'
            )
        ).all()
    assert rows == []
    assert (
        tournament_image_repository.count_images(
            [PARTY_ID], filename_query='rollback-probe.png'
        )
        == before
    )
    assert not list(data_dir.rglob('*.png'))


def test_store_removes_file_when_commit_fails(party, creator, data_dir):
    with patch(
        f'{_S}.tournament_repository.commit_session',
        side_effect=RuntimeError('commit failed'),
    ):
        with pytest.raises(RuntimeError, match='commit failed'):
            tournament_image_service.store_uploaded_image(
                PARTY_ID, creator.id, _png(), 'commit-probe.png'
            )

    assert not list(data_dir.rglob('*.png'))
    assert (
        tournament_image_repository.count_images(
            [PARTY_ID], filename_query='commit-probe.png'
        )
        == 0
    )


def test_upload_does_not_delete_old_unused_images(party, creator, make_image):
    old = make_image(created_at=_now() - timedelta(hours=48))

    result = tournament_image_service.store_uploaded_image(
        PARTY_ID, creator.id, _png(), 'trigger.png'
    )

    assert result.is_ok()
    assert tournament_image_repository.find_image(old.id) is not None
    assert tournament_image_service.get_image_file_path(old).exists()


def test_delete_staged_image_requires_creator_and_unreferenced(
    party, creator, other_user, make_image, make_tournament
):
    svc = tournament_image_service
    image = make_image()
    path = svc.get_image_file_path(image)

    not_creator = svc.delete_staged_image(
        image.id, party_id=PARTY_ID, requester_id=other_user.id
    )
    assert not_creator.unwrap_err().msgid == svc.IMAGE_NOT_CREATOR_ERROR

    wrong_party = svc.delete_staged_image(
        image.id, party_id=SIBLING_PARTY_ID, requester_id=creator.id
    )
    assert wrong_party.unwrap_err().msgid == svc.IMAGE_UNAVAILABLE_ERROR

    unknown = svc.delete_staged_image(
        TournamentImageID(generate_uuid4()),
        party_id=PARTY_ID,
        requester_id=creator.id,
    )
    assert unknown.unwrap_err().msgid == svc.IMAGE_UNAVAILABLE_ERROR
    assert path.exists()
    assert tournament_image_repository.find_image(image.id) is not None

    make_tournament('Delete Guard Cup', image_id=image.id)
    in_use = svc.delete_staged_image(
        image.id, party_id=PARTY_ID, requester_id=creator.id
    )
    assert in_use.unwrap_err().msgid == svc.IMAGE_IN_USE_ERROR
    assert path.exists()

    by_url = make_image()
    make_tournament(
        'Delete Guard Url Cup',
        image_url='https://lan.example' + svc.get_image_url_path(by_url),
    )
    in_use_by_url = svc.delete_staged_image(
        by_url.id, party_id=PARTY_ID, requester_id=creator.id
    )
    assert in_use_by_url.unwrap_err().msgid == svc.IMAGE_IN_USE_ERROR
    assert svc.get_image_file_path(by_url).exists()
    assert tournament_image_repository.find_image(by_url.id) is not None

    free = make_image()
    free_path = svc.get_image_file_path(free)
    result = svc.delete_staged_image(
        free.id, party_id=PARTY_ID, requester_id=creator.id
    )
    assert result.is_ok()
    assert tournament_image_repository.find_image(free.id) is None
    assert not free_path.exists()


def test_find_attachable_image_same_party_and_same_brand_only(
    party, sibling_party, foreign_party, make_image
):
    own = make_image()
    sibling = make_image(party_id=SIBLING_PARTY_ID)
    foreign = make_image(party_id=FOREIGN_PARTY_ID)
    find = tournament_image_service.find_attachable_image

    assert find(own.id, party) == own
    assert find(sibling.id, party) == sibling
    assert find(foreign.id, party) is None
    assert find(TournamentImageID(generate_uuid4()), party) is None
    assert find(foreign.id, foreign_party) == foreign
    assert find(own.id, foreign_party) is None


def test_list_picker_images_party_and_brand_scope(
    party, sibling_party, foreign_party, make_image, creator
):
    own = make_image(created_at=_now() - timedelta(seconds=3))
    sibling = make_image(
        party_id=SIBLING_PARTY_ID, created_at=_now() - timedelta(seconds=2)
    )
    foreign = make_image(party_id=FOREIGN_PARTY_ID)
    list_images = tournament_image_service.list_picker_images

    party_page = list_images(party, scope='party', filename_query=None, page=1)
    brand_page = list_images(party, scope='brand', filename_query=None, page=1)

    party_ids = {item.image.id for item in party_page.items}
    brand_ids = {item.image.id for item in brand_page.items}
    assert own.id in party_ids
    assert sibling.id not in party_ids
    assert {own.id, sibling.id} <= brand_ids
    assert foreign.id not in brand_ids
    assert foreign.id not in party_ids
    assert set(brand_page.party_titles) >= {PARTY_ID, SIBLING_PARTY_ID}
    assert FOREIGN_PARTY_ID not in brand_page.party_titles
    assert party_page.party_titles == {PARTY_ID: 'Image Service Party'}


def test_list_picker_images_paginates_and_filters(party, make_image):
    for _ in range(tournament_image_service.PICKER_PAGE_SIZE + 1):
        make_image()
    per_page = tournament_image_service.PICKER_PAGE_SIZE

    first = tournament_image_service.list_picker_images(
        party, scope='party', filename_query='seed', page=1
    )
    total = tournament_image_repository.count_images(
        [PARTY_ID], filename_query='seed'
    )
    last_page = -(-total // per_page)
    last = tournament_image_service.list_picker_images(
        party, scope='party', filename_query='seed', page=last_page
    )
    none = tournament_image_service.list_picker_images(
        party, scope='party', filename_query='no-such-name', page=1
    )

    assert len(first.items) == per_page
    assert first.has_next
    assert not last.has_next
    assert none.items == [] and not none.has_next
