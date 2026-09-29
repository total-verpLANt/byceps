"""
tests.integration.services.lan_tournament.test_tournament_maintenance_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, UTC
from pathlib import Path
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
    tournament_maintenance_service,
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


PARTY_ID = PartyID('lan-party-maintenance')
SIBLING_PARTY_ID = PartyID('lan-party-maintenance-sibling')
FOREIGN_PARTY_ID = PartyID('lan-party-maintenance-foreign')

_S = 'byceps.services.lan_tournament.tournament_image_service'
_M = 'byceps.services.lan_tournament.tournament_maintenance_service'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Maintenance Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, brand):
    return make_party(brand, SIBLING_PARTY_ID, 'Maintenance Sibling')


@pytest.fixture(scope='module')
def foreign_party(make_party, make_brand):
    other_brand = make_brand(
        'maintenance-other-brand', 'Maintenance Other Brand'
    )
    return make_party(other_brand, FOREIGN_PARTY_ID, 'Maintenance Foreign')


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('MaintenanceCreator')


@pytest.fixture(scope='module')
def actor(make_user):
    return make_user('MaintenanceActor')


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party, sibling_party, foreign_party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


@pytest.fixture(autouse=True)
def clean_images(party, sibling_party, foreign_party):
    yield
    db.session.rollback()
    db.session.execute(
        DbTournament.__table__.delete().where(
            DbTournament.party_id.in_(
                [PARTY_ID, SIBLING_PARTY_ID, FOREIGN_PARTY_ID]
            )
        )
    )
    db.session.execute(
        DbTournamentImage.__table__.delete().where(
            DbTournamentImage.party_id.in_(
                [PARTY_ID, SIBLING_PARTY_ID, FOREIGN_PARTY_ID]
            )
        )
    )
    db.session.commit()


@pytest.fixture
def make_image(creator):
    def _wrapper(
        *,
        party_id: PartyID = PARTY_ID,
        age: timedelta = timedelta(days=2),
        byte_size: int = 10,
        with_file: bool = True,
        image_type: ImageType = ImageType.png,
    ) -> TournamentImage:
        image = TournamentImage(
            id=TournamentImageID(generate_uuid7()),
            party_id=party_id,
            creator_id=creator.id,
            created_at=_now() - age,
            filename='seed.png',
            image_type=image_type,
            width=1920,
            height=1080,
            byte_size=byte_size,
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
    def _wrapper(name: str, party_id: PartyID = PARTY_ID, **kwargs):
        tournament, _event = tournament_domain_service.create_tournament(
            party_id, name, **kwargs
        )
        tournament_repository.create_tournament(tournament)
        db.session.commit()
        return tournament

    return _wrapper


def _now() -> datetime:
    return datetime.now(UTC)


def _file_path(image: TournamentImage) -> Path:
    return tournament_image_service.get_image_file_path(image)


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


def test_get_actions_lists_unused_images_first():
    actions = tournament_maintenance_service.get_actions()

    assert actions[0].id == 'unused-images'
    assert actions[0].permission == 'lan_tournament.maintain'
    assert (
        tournament_maintenance_service.find_action('unused-images')
        is actions[0]
    )
    assert tournament_maintenance_service.find_action('nope') is None


def test_summarize_unused_images_counts_old_unused_of_this_party_only(
    make_image, make_tournament, actor
):
    make_image(byte_size=100)
    make_image(byte_size=50)
    referenced = make_image(byte_size=7)
    make_tournament('Summary Cup', image_id=referenced.id)
    make_image(party_id=SIBLING_PARTY_ID, byte_size=1000)
    make_image(party_id=FOREIGN_PARTY_ID, byte_size=1000)

    summary = tournament_maintenance_service.summarize_unused_images(
        PARTY_ID, _now()
    )

    assert summary.count == 2
    assert summary.byte_size == 150
    assert summary.kept_count == 0


def test_summarize_unused_images_reports_recent_unused_as_kept(make_image):
    make_image(age=timedelta(hours=23))
    make_image(age=timedelta(hours=25))

    summary = tournament_maintenance_service.summarize_unused_images(
        PARTY_ID, _now()
    )

    assert summary.count == 1
    assert summary.kept_count == 1


def test_summarize_unused_images_counts_all_recent_as_kept(
    make_image, monkeypatch
):
    monkeypatch.setattr(tournament_maintenance_service, 'PREVIEW_LIMIT', 1)
    make_image(age=timedelta(hours=1))
    make_image(age=timedelta(hours=2))

    summary = tournament_maintenance_service.summarize_unused_images(
        PARTY_ID, _now()
    )

    assert summary.kept_count == 2


def test_preview_unused_images_lists_oldest_first_and_caps_at_limit(
    make_image, monkeypatch
):
    monkeypatch.setattr(tournament_maintenance_service, 'PREVIEW_LIMIT', 2)
    oldest = make_image(age=timedelta(days=5))
    middle = make_image(age=timedelta(days=4))
    make_image(age=timedelta(days=3))
    recent = make_image(age=timedelta(hours=1))

    preview = tournament_maintenance_service.preview_unused_images(
        PARTY_ID, _now()
    )

    assert [i.key for i in preview.items] == [str(oldest.id), str(middle.id)]
    assert preview.more_count == 1
    assert [i.key for i in preview.kept] == [str(recent.id)]
    item = preview.items[0]
    assert item.file_name == 'seed.png'
    assert item.type_name == 'PNG'
    assert item.thumbnail_url == tournament_image_service.get_image_url_path(
        oldest
    )


def test_preview_unused_images_excludes_images_used_by_another_partys_tournament(
    make_image, make_tournament, sibling_party
):
    used = make_image()
    free = make_image()
    make_tournament('Sibling Cup', party_id=SIBLING_PARTY_ID, image_id=used.id)

    preview = tournament_maintenance_service.preview_unused_images(
        PARTY_ID, _now()
    )

    assert [i.key for i in preview.items] == [str(free.id)]
    assert preview.more_count == 0


def test_delete_unused_images_deletes_rows_and_files(make_image, actor):
    first = make_image(byte_size=100)
    second = make_image(byte_size=50)

    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID, [str(first.id), str(second.id)], actor.id, _now()
    )

    assert report.deleted_count == 2
    assert report.byte_size == 150
    assert report.skipped_count == 0
    assert report.failed_file_count == 0
    for image in (first, second):
        assert not _row_exists_in_fresh_session(image.id)
        assert not _file_path(image).exists()


def test_delete_unused_images_skips_foreign_recent_and_referenced_keys(
    make_image, make_tournament, actor
):
    deletable = make_image()
    foreign = make_image(party_id=SIBLING_PARTY_ID)
    recent = make_image(age=timedelta(hours=1))
    referenced = make_image()
    make_tournament('Skip Cup', image_id=referenced.id)
    keep = [foreign, recent, referenced]

    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID,
        [str(i.id) for i in [deletable, *keep]],
        actor.id,
        _now(),
    )

    assert report.deleted_count == 1
    assert report.skipped_count == 3
    assert not _row_exists_in_fresh_session(deletable.id)
    for image in keep:
        assert _row_exists_in_fresh_session(image.id)
        assert _file_path(image).is_file()


def test_delete_unused_images_ignores_malformed_and_unknown_keys(
    make_image, actor
):
    image = make_image()
    key = str(image.id)

    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID,
        [key, key, 'not-a-uuid', '', '../../etc/passwd', str(uuid4())],
        actor.id,
        _now(),
    )

    assert report.deleted_count == 1
    assert report.skipped_count == 1
    assert not _row_exists_in_fresh_session(image.id)


def test_delete_unused_images_counts_unlink_failures(make_image, actor):
    broken = make_image()
    fine = make_image()
    broken_path = _file_path(broken)
    real_unlink = Path.unlink

    def _unlink(self, *args, **kwargs):
        if self == broken_path:
            raise PermissionError('denied')
        return real_unlink(self, *args, **kwargs)

    with patch.object(Path, 'unlink', _unlink):
        report = tournament_maintenance_service.delete_unused_images(
            PARTY_ID, [str(broken.id), str(fine.id)], actor.id, _now()
        )

    assert report.deleted_count == 2
    assert report.failed_file_count == 1
    assert not _row_exists_in_fresh_session(broken.id)
    assert broken_path.exists()
    assert not _file_path(fine).exists()


def test_delete_unused_images_writes_one_log_record(
    make_image, actor, monkeypatch
):
    image = make_image(byte_size=42)
    records = []
    monkeypatch.setattr(
        tournament_maintenance_service.log,
        'info',
        lambda event, **kwargs: records.append((event, kwargs)),
    )

    tournament_maintenance_service.delete_unused_images(
        PARTY_ID, [str(image.id)], actor.id, _now()
    )

    assert records == [
        (
            'Unused tournament images deleted',
            {
                'party_id': PARTY_ID,
                'actor_id': actor.id,
                'action': 'unused-images',
                'count': 1,
                'byte_size': 42,
                'keys': [str(image.id)],
            },
        )
    ]


def test_delete_unused_images_counts_in_use_skips_separately(
    make_image, make_tournament, actor
):
    free = make_image()
    referenced = make_image()
    recent = make_image(age=timedelta(hours=1))
    make_tournament('In Use Cup', image_id=referenced.id)

    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID,
        [str(free.id), str(referenced.id), str(recent.id), str(uuid4())],
        actor.id,
        _now(),
    )

    assert report.deleted_count == 1
    assert report.skipped_count == 3
    assert report.in_use_count == 1


def test_delete_unused_images_caps_keys_per_post(
    make_image, actor, monkeypatch
):
    monkeypatch.setattr(tournament_maintenance_service, 'PREVIEW_LIMIT', 2)
    images = [make_image() for _ in range(3)]

    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID, [str(image.id) for image in images], actor.id, _now()
    )

    assert report.deleted_count == 2
    assert not _row_exists_in_fresh_session(images[0].id)
    assert not _row_exists_in_fresh_session(images[1].id)
    assert _row_exists_in_fresh_session(images[2].id)
    assert _file_path(images[2]).exists()


def test_delete_unused_images_writes_a_log_record_for_malformed_keys_only(
    actor, monkeypatch
):
    records = []
    monkeypatch.setattr(
        tournament_maintenance_service.log,
        'info',
        lambda event, **kwargs: records.append((event, kwargs)),
    )

    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID, ['nope', ''], actor.id, _now()
    )

    assert report.deleted_count == 0
    assert records == [
        (
            'Unused tournament images deleted',
            {
                'party_id': PARTY_ID,
                'actor_id': actor.id,
                'action': 'unused-images',
                'count': 0,
                'byte_size': 0,
                'keys': [],
            },
        )
    ]


@pytest.mark.parametrize(
    ('image_type', 'expected'),
    [
        (ImageType.jpeg, 'JPEG'),
        (ImageType.png, 'PNG'),
        (ImageType.webp, 'WebP'),
    ],
)
def test_preview_unused_images_labels_the_image_type(
    make_image, image_type, expected
):
    make_image(image_type=image_type)

    preview = tournament_maintenance_service.preview_unused_images(
        PARTY_ID, _now()
    )

    assert [item.type_name for item in preview.items] == [expected]


def test_url_referenced_image_is_kept_and_counted_as_in_use(
    make_image, make_tournament, actor
):
    by_absolute_url = make_image()
    by_relative_url = make_image()
    by_foreign_party_url = make_image()
    unused = make_image()
    make_tournament(
        'Absolute Url Cup',
        image_url='https://lan.example'
        + tournament_image_service.get_image_url_path(by_absolute_url),
    )
    make_tournament(
        'Relative Url Cup',
        image_url=tournament_image_service.get_image_url_path(by_relative_url),
    )
    make_tournament(
        'Sibling Url Cup',
        party_id=SIBLING_PARTY_ID,
        image_url='https://lan.example'
        + tournament_image_service.get_image_url_path(by_foreign_party_url),
    )

    summary = tournament_maintenance_service.summarize_unused_images(
        PARTY_ID, _now()
    )
    preview = tournament_maintenance_service.preview_unused_images(
        PARTY_ID, _now()
    )
    report = tournament_maintenance_service.delete_unused_images(
        PARTY_ID,
        [
            str(image.id)
            for image in (
                by_absolute_url,
                by_relative_url,
                by_foreign_party_url,
                unused,
            )
        ],
        actor.id,
        _now(),
    )

    assert summary.count == 1
    assert [item.key for item in preview.items] == [str(unused.id)]
    assert report.deleted_count == 1
    assert report.in_use_count == 3
    for image in (by_absolute_url, by_relative_url, by_foreign_party_url):
        assert _file_path(image).exists()
        assert _row_exists_in_fresh_session(image.id)
    assert not _file_path(unused).exists()
