"""
tests.integration.services.lan_tournament.test_tournament_maintenance_orphans
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, UTC
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
import pytest

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


PARTY_ID = PartyID('lan-party-orphans')

SIBLING_PARTY_ID = PartyID('lan-party-orphans-sibling')

_S = 'byceps.services.lan_tournament.tournament_image_service'


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('orphans-brand', 'Orphans Brand')
    return make_party(brand, PARTY_ID, 'Orphans Party')


@pytest.fixture(scope='module')
def sibling_party(make_party, make_brand):
    brand = make_brand('orphans-sibling-brand', 'Orphans Sibling Brand')
    return make_party(brand, SIBLING_PARTY_ID, 'Orphans Sibling Party')


@pytest.fixture(scope='module')
def actor(make_user):
    return make_user('OrphansActor')


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


@pytest.fixture(autouse=True)
def clean_images(party):
    yield
    db.session.rollback()
    db.session.execute(
        DbTournament.__table__.delete().where(
            DbTournament.party_id.in_([PARTY_ID, SIBLING_PARTY_ID])
        )
    )
    db.session.execute(
        DbTournamentImage.__table__.delete().where(
            DbTournamentImage.party_id == PARTY_ID
        )
    )
    db.session.commit()


@pytest.fixture
def image_dir(data_dir):
    path = tournament_image_service.get_party_image_dir(PARTY_ID)
    path.mkdir(parents=True)
    return path


@pytest.fixture
def make_orphan(image_dir):
    def _wrapper(
        *,
        age: timedelta = timedelta(days=2),
        size: tuple[int, int] | None = (64, 32),
        extension: str = 'png',
        name: str | None = None,
    ) -> Path:
        path = image_dir / (name or f'{uuid4()}.{extension}')
        if size is not None:
            buffer = BytesIO()
            Image.new('RGB', size).save(buffer, format='PNG')
            path.write_bytes(buffer.getvalue())
        else:
            path.write_bytes(b'not an image')
        _set_age(path, age)
        return path

    return _wrapper


def _set_age(path: Path, age: timedelta) -> None:
    mtime = (_now() - age).timestamp()
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def _now() -> datetime:
    return datetime.now(UTC)


def test_get_party_image_dir_is_the_parent_of_get_image_file_path(data_dir):
    image = TournamentImage(
        id=TournamentImageID(generate_uuid7()),
        party_id=PARTY_ID,
        creator_id=None,
        created_at=_now(),
        filename='seed.png',
        image_type=ImageType.webp,
        width=1920,
        height=1080,
        byte_size=1,
    )

    directory = tournament_image_service.get_party_image_dir(PARTY_ID)

    assert (
        directory
        == data_dir / 'parties' / PARTY_ID / 'lan_tournament' / 'images'
    )
    assert (
        tournament_image_service.get_image_file_path(image).parent == directory
    )
    assert tournament_image_service.get_image_url_path(
        image
    ) == tournament_image_service.get_party_image_url_path(
        PARTY_ID, f'{image.id}.webp'
    )


def test_orphaned_files_lists_files_without_row_older_than_grace(
    make_orphan,
):
    oldest = make_orphan(age=timedelta(days=5), size=(64, 32))
    older = make_orphan(age=timedelta(days=3), extension='webp', size=None)
    recent = make_orphan(age=timedelta(hours=1))

    eligible, kept = tournament_maintenance_service._scan_orphaned_files(
        PARTY_ID, _now()
    )
    preview = tournament_maintenance_service.preview_orphaned_files(
        PARTY_ID, _now()
    )
    summary = tournament_maintenance_service.summarize_orphaned_files(
        PARTY_ID, _now()
    )

    assert [i.key for i in eligible] == [oldest.name, older.name]
    assert [i.key for i in kept] == [recent.name]
    assert [i.key for i in preview.items] == [oldest.name, older.name]
    assert [i.key for i in preview.kept] == [recent.name]
    assert preview.more_count == 0
    assert (summary.count, summary.kept_count) == (2, 1)
    assert summary.byte_size == oldest.stat().st_size + older.stat().st_size
    item = eligible[0]
    assert item.file_name == oldest.name
    assert item.byte_size == oldest.stat().st_size
    assert item.creator_id is None
    assert (item.width, item.height) == (64, 32)
    assert item.type_name == 'PNG'
    assert item.thumbnail_url == (
        f'/data/parties/{PARTY_ID}/lan_tournament/images/{oldest.name}'
    )
    assert abs(item.created_at - (_now() - timedelta(days=5))) < timedelta(
        minutes=1
    )
    assert (eligible[1].width, eligible[1].height) == (None, None)


def test_orphaned_preview_caps_at_limit(make_orphan, monkeypatch):
    monkeypatch.setattr(tournament_maintenance_service, 'PREVIEW_LIMIT', 1)
    oldest = make_orphan(age=timedelta(days=5))
    make_orphan(age=timedelta(days=4))
    make_orphan(age=timedelta(hours=1))
    make_orphan(age=timedelta(hours=2))

    preview = tournament_maintenance_service.preview_orphaned_files(
        PARTY_ID, _now()
    )
    summary = tournament_maintenance_service.summarize_orphaned_files(
        PARTY_ID, _now()
    )

    assert [i.key for i in preview.items] == [oldest.name]
    assert preview.more_count == 1
    assert len(preview.kept) == 1
    assert (summary.count, summary.kept_count) == (2, 2)


def test_orphaned_files_ignore_files_with_row_recent_foreign_names_dirs_and_symlinks(
    make_orphan, image_dir, actor
):
    orphan = make_orphan()
    with_row = make_orphan()
    tournament_image_repository.create_image(
        TournamentImage(
            id=TournamentImageID(with_row.stem),
            party_id=PARTY_ID,
            creator_id=actor.id,
            created_at=_now() - timedelta(days=2),
            filename='seed.png',
            image_type=ImageType.png,
            width=64,
            height=32,
            byte_size=1,
        )
    )
    db.session.commit()
    make_orphan(name='notes.txt')
    make_orphan(name=f'{uuid4()}.gif')
    make_orphan(name=f'{str(uuid4()).upper()}.png')
    make_orphan(name=f'{uuid4()}.png.txt')
    (image_dir / f'{uuid4()}.png').mkdir()
    target = make_orphan(name='target.dat')
    link = image_dir / f'{uuid4()}.png'
    link.symlink_to(target)

    eligible, kept = tournament_maintenance_service._scan_orphaned_files(
        PARTY_ID, _now()
    )

    assert [i.key for i in eligible] == [orphan.name]
    assert kept == []


def test_delete_orphaned_files_deletes_only_posted_eligible_names(
    make_orphan, actor, monkeypatch
):
    posted = make_orphan()
    also_posted = make_orphan()
    not_posted = make_orphan()
    recent = make_orphan(age=timedelta(hours=1))
    expected_size = posted.stat().st_size + also_posted.stat().st_size
    records = []
    monkeypatch.setattr(
        tournament_maintenance_service.log,
        'info',
        lambda event, **kwargs: records.append((event, kwargs)),
    )

    report = tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID,
        [posted.name, posted.name, also_posted.name, recent.name],
        actor.id,
        _now(),
    )

    assert report.deleted_count == 2
    assert report.byte_size == expected_size
    assert report.skipped_count == 1
    assert report.failed_file_count == 0
    assert not posted.exists()
    assert not also_posted.exists()
    assert not_posted.exists()
    assert recent.exists()
    assert len(records) == 1
    event, fields = records[0]
    assert event == 'Orphaned tournament image files deleted'
    assert fields['party_id'] == PARTY_ID
    assert fields['actor_id'] == actor.id
    assert fields['action'] == 'orphaned-files'
    assert fields['count'] == 2
    assert fields['keys'] == [posted.name, also_posted.name]


def test_delete_orphaned_files_rejects_traversal_keys(
    make_orphan, image_dir, data_dir, actor
):
    sentinel = data_dir / 'x.png'
    sentinel.write_bytes(b'sentinel')
    (image_dir.parent / 'x.png').write_bytes(b'sibling sentinel')
    keys = ['../x.png', '/etc/passwd', 'a/b.png', '%2e%2e/x.png']

    report = tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID, keys, actor.id, _now()
    )

    assert report.deleted_count == 0
    assert report.skipped_count == 4
    assert report.failed_file_count == 0
    assert sentinel.read_bytes() == b'sentinel'
    assert (image_dir.parent / 'x.png').read_bytes() == b'sibling sentinel'
    assert Path('/etc/passwd').exists()


def test_delete_orphaned_files_skips_file_that_gained_a_row(make_orphan, actor):
    gained = make_orphan()
    plain = make_orphan()
    tournament_image_repository.create_image(
        TournamentImage(
            id=TournamentImageID(gained.stem),
            party_id=PARTY_ID,
            creator_id=actor.id,
            created_at=_now() - timedelta(days=2),
            filename='seed.png',
            image_type=ImageType.png,
            width=64,
            height=32,
            byte_size=1,
        )
    )
    db.session.commit()

    report = tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID, [gained.name, plain.name], actor.id, _now()
    )

    assert report.deleted_count == 1
    assert report.skipped_count == 1
    assert gained.exists()
    assert not plain.exists()


def test_delete_orphaned_files_counts_unlink_failures(make_orphan, actor):
    broken = make_orphan()
    fine = make_orphan()
    real_unlink = Path.unlink

    def _unlink(self, *args, **kwargs):
        if self == broken:
            raise PermissionError('denied')
        return real_unlink(self, *args, **kwargs)

    with patch.object(Path, 'unlink', _unlink):
        report = tournament_maintenance_service.delete_orphaned_files(
            PARTY_ID, [broken.name, fine.name], actor.id, _now()
        )

    assert report.deleted_count == 1
    assert report.failed_file_count == 1
    assert report.skipped_count == 0
    assert broken.exists()
    assert not fine.exists()


def test_orphan_scan_treats_a_missing_directory_as_empty(actor):
    assert tournament_maintenance_service._scan_orphaned_files(
        PARTY_ID, _now()
    ) == ([], [])
    assert tournament_maintenance_service.summarize_orphaned_files(
        PARTY_ID, _now()
    ) == tournament_maintenance_service.MaintenanceSummary(
        count=0, byte_size=0, kept_count=0
    )
    report = tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID, ['x.png'], actor.id, _now()
    )
    assert report.deleted_count == 0


def test_orphaned_files_action_is_registered_second():
    actions = tournament_maintenance_service.get_actions()

    assert [a.id for a in actions] == ['unused-images', 'orphaned-files']
    assert actions[1].permission == 'lan_tournament.maintain'


def test_delete_orphaned_files_confines_deletion_to_the_party_directory(
    image_dir, data_dir, actor, monkeypatch
):
    sentinel = data_dir / 'x.png'
    sentinel.write_bytes(b'sentinel')
    hostile = tournament_maintenance_service.MaintenanceItem(
        key='../../../../x.png',
        file_name='x.png',
        byte_size=1,
        created_at=_now() - timedelta(days=2),
        creator_id=None,
        width=None,
        height=None,
        type_name=None,
        thumbnail_url=None,
    )
    monkeypatch.setattr(
        tournament_maintenance_service,
        '_scan_orphaned_files',
        lambda party_id, now: ([hostile], []),
    )

    report = tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID, [hostile.key], actor.id, _now()
    )

    assert report.deleted_count == 0
    assert report.skipped_count == 1
    assert sentinel.exists()


def test_orphan_scan_treats_an_unreadable_directory_as_empty(
    image_dir, actor, monkeypatch
):
    records = []
    monkeypatch.setattr(
        tournament_maintenance_service.log,
        'warning',
        lambda event, **kwargs: records.append((event, kwargs)),
    )

    def _deny(path):
        raise PermissionError(13, 'denied', str(path))

    monkeypatch.setattr(tournament_maintenance_service.os, 'scandir', _deny)

    assert tournament_maintenance_service._scan_orphaned_files(
        PARTY_ID, _now()
    ) == ([], [])
    assert records == [
        (
            'Scanning tournament image directory failed',
            {'party_id': PARTY_ID, 'error': 'PermissionError'},
        )
    ]


def test_delete_orphaned_files_caps_keys_per_post(
    make_orphan, actor, monkeypatch
):
    monkeypatch.setattr(tournament_maintenance_service, 'PREVIEW_LIMIT', 2)
    paths = [make_orphan(age=timedelta(days=3 + i)) for i in range(3)]

    report = tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID, [path.name for path in paths], actor.id, _now()
    )

    assert report.deleted_count == 2
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()


def test_delete_orphaned_files_writes_a_log_record_for_no_keys(
    image_dir, actor, monkeypatch
):
    records = []
    monkeypatch.setattr(
        tournament_maintenance_service.log,
        'info',
        lambda event, **kwargs: records.append((event, kwargs)),
    )

    tournament_maintenance_service.delete_orphaned_files(
        PARTY_ID, [], actor.id, _now()
    )

    assert records == [
        (
            'Orphaned tournament image files deleted',
            {
                'party_id': PARTY_ID,
                'actor_id': actor.id,
                'action': 'orphaned-files',
                'count': 0,
                'byte_size': 0,
                'keys': [],
            },
        )
    ]


def test_orphaned_files_label_webp_as_webp(make_orphan):
    make_orphan(extension='webp')
    make_orphan(extension='jpeg')

    eligible, _ = tournament_maintenance_service._scan_orphaned_files(
        PARTY_ID, _now()
    )

    assert sorted(item.type_name for item in eligible) == ['JPEG', 'WebP']


def _make_tournament(party_id: PartyID, name: str, image_url: str) -> None:
    tournament, _event = tournament_domain_service.create_tournament(
        party_id, name, image_url=image_url
    )
    tournament_repository.create_tournament(tournament)
    db.session.commit()


def test_orphaned_files_skip_files_a_tournament_shows_through_image_url(
    make_orphan, sibling_party
):
    shown_absolute = make_orphan()
    shown_relative = make_orphan()
    shown_elsewhere = make_orphan()
    plain = make_orphan()
    served = tournament_image_service.get_party_image_url_path
    _make_tournament(
        PARTY_ID,
        'Orphan Absolute Cup',
        'https://lan.example' + served(PARTY_ID, shown_absolute.name),
    )
    _make_tournament(
        PARTY_ID, 'Orphan Relative Cup', served(PARTY_ID, shown_relative.name)
    )
    _make_tournament(
        SIBLING_PARTY_ID,
        'Orphan Sibling Cup',
        'https://lan.example' + served(PARTY_ID, shown_elsewhere.name),
    )

    preview = tournament_maintenance_service.preview_orphaned_files(
        PARTY_ID, _now()
    )
    summary = tournament_maintenance_service.summarize_orphaned_files(
        PARTY_ID, _now()
    )

    assert [item.key for item in preview.items] == [plain.name]
    assert preview.kept == []
    assert summary.count == 1
