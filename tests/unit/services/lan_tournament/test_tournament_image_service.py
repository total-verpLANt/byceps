"""
tests.unit.services.lan_tournament.test_tournament_image_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from io import BytesIO
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from PIL import Image, ImageCms, ImageFile
import pytest

from byceps.services.lan_tournament import tournament_image_service as svc
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
    TournamentImageID,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.image.image_type import ImageType


_S = 'byceps.services.lan_tournament.tournament_image_service'

PARTY_ID = PartyID('party-1')
CREATOR_ID = UserID(UUID('00000000-0000-4000-8000-000000000001'))


def _image_bytes(
    fmt: str, size=(1000, 600), *, exif: bool = False, noise: bool = False
) -> bytes:
    if noise:
        image = Image.frombytes(
            'RGB', size, bytes(range(256)) * (size[0] * size[1] * 3 // 256 + 1)
        )
        image = Image.frombytes(
            'RGB', size, image.tobytes()[: size[0] * size[1] * 3]
        )
    else:
        image = Image.new('RGB', size, (200, 30, 30))

    kwargs = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = 'SecretCameraMaker'
        kwargs['exif'] = data.tobytes()

    buf = BytesIO()
    image.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path):
    """Patch repository, session and data path; capture stored bytes."""
    stored: dict[str, bytes] = {}

    def fake_store(source, target_path, *, create_parent_path_if_nonexistent):
        stored['data'] = source.read()
        stored['path'] = target_path

    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with (
        patch(f'{_S}.get_current_byceps_app', return_value=app),
        patch(f'{_S}.tournament_image_repository') as repo,
        patch(f'{_S}.tournament_repository') as base_repo,
        patch(f'{_S}.upload') as up,
    ):
        up.store.side_effect = fake_store
        yield SimpleNamespace(
            repo=repo, base_repo=base_repo, upload=up, stored=stored
        )


def _store(data: bytes, filename: str = 'pic.png'):
    return svc.store_uploaded_image(
        PARTY_ID, CREATOR_ID, BytesIO(data), filename
    )


def _err(result) -> ValidationMessage:
    assert result.is_err()
    return result.unwrap_err()


@pytest.mark.parametrize(
    'payload',
    [
        b'<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="1000"/>',
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
        b'GIF89a' + b'\x00' * 64,
        b'not an image at all',
        b'',
    ],
)
def test_rejects_svg_bytes_regardless_of_filename(env, payload):
    message = _err(_store(payload, 'photo.png'))

    assert message == ValidationMessage(svc.IMAGE_TYPE_ERROR)
    env.repo.create_image.assert_not_called()
    env.upload.store.assert_not_called()


def test_type_comes_from_bytes_not_extension(env):
    result = _store(_image_bytes('PNG'), 'x.jpg')

    image = result.unwrap()
    assert image.image_type is ImageType.png
    assert str(env.stored['path']).endswith(f'{image.id}.png')
    assert Image.open(BytesIO(env.stored['data'])).format == 'PNG'
    assert image.filename == 'x.jpg'


def test_rejects_more_than_5_mib_without_decoding(env):
    class RecordingStream(BytesIO):
        requested: list[int] = []

        def read(self, size=-1):
            self.requested.append(size)
            return super().read(size)

    stream = RecordingStream(b'\x89PNG\r\n\x1a\n' + b'\x00' * (6 * 1024 * 1024))

    with (
        patch(f'{_S}.determine_dimensions') as dims,
        patch(f'{_S}._reencode') as thumb,
    ):
        result = svc.store_uploaded_image(PARTY_ID, CREATOR_ID, stream, 'a.png')

    message = _err(result)
    assert message.msgid == svc.IMAGE_SIZE_ERROR
    assert stream.requested == [svc.MAX_UPLOAD_BYTES + 1]
    dims.assert_not_called()
    thumb.assert_not_called()
    env.repo.create_image.assert_not_called()


@pytest.mark.parametrize('size', [(959, 600), (1000, 539), (100, 100)])
def test_rejects_below_960x540(env, size):
    message = _err(_store(_image_bytes('PNG', size)))

    assert message == ValidationMessage(
        svc.IMAGE_TOO_SMALL_ERROR, (('w', size[0]), ('h', size[1]))
    )
    env.repo.create_image.assert_not_called()


@pytest.mark.parametrize('size', [(8001, 600), (1000, 8001)])
def test_rejects_above_8000x8000_from_header(env, size):
    data = _image_bytes('PNG', size)

    with patch(f'{_S}._reencode') as thumb:
        message = _err(_store(data))

    assert message == ValidationMessage(
        svc.IMAGE_TOO_LARGE_ERROR, (('w', size[0]), ('h', size[1]))
    )
    thumb.assert_not_called()
    env.repo.create_image.assert_not_called()


def test_rejects_above_25_megapixels_from_header(env):
    data = _image_bytes('PNG', (6000, 4200))  # 25.2 MP, each edge <= 8000

    with patch(f'{_S}._reencode') as reencode:
        message = _err(_store(data))

    assert message == ValidationMessage(
        svc.IMAGE_TOO_MANY_PIXELS_ERROR, (('mp', '25.2'),)
    )
    reencode.assert_not_called()
    env.repo.create_image.assert_not_called()


def test_accepts_exactly_25_megapixels(env):
    data = _image_bytes('PNG', (5000, 5000))

    assert _store(data).is_ok()


def _oriented_jpeg(size, orientation: int) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = BytesIO()
    Image.new('RGB', size, (200, 30, 30)).save(
        buf, format='JPEG', exif=exif.tobytes()
    )
    return buf.getvalue()


@pytest.mark.parametrize('orientation', [5, 6, 7, 8])
def test_transposing_orientation_is_stored_upright(env, orientation):
    image = _store(_oriented_jpeg((1200, 1000), orientation), 'c.jpg').unwrap()

    stored = Image.open(BytesIO(env.stored['data']))
    assert stored.size == (900, 1080)
    assert (image.width, image.height) == (900, 1080)
    assert not stored.getexif()


@pytest.mark.parametrize('orientation', [1, 2, 3, 4])
def test_non_transposing_orientation_keeps_size(env, orientation):
    _store(_oriented_jpeg((1200, 1000), orientation), 'c.jpg').unwrap()

    assert Image.open(BytesIO(env.stored['data'])).size == (1200, 1000)


def test_minimum_dimensions_apply_to_the_upright_size(env):
    # Raw 540x960 is too narrow, but upright it is 960x540.
    assert _store(_oriented_jpeg((540, 960), 6), 'a.jpg').is_ok()

    message = _err(_store(_oriented_jpeg((1000, 539), 6), 'b.jpg'))

    assert message == ValidationMessage(
        svc.IMAGE_TOO_SMALL_ERROR, (('w', 539), ('h', 1000))
    )


@pytest.mark.parametrize('fmt', ['PNG', 'JPEG'])
def test_rejects_truncated_image(env, fmt):
    data = _image_bytes(fmt, (1000, 600), noise=True)

    message = _err(_store(data[: len(data) * 6 // 10]))

    assert message == ValidationMessage(svc.IMAGE_CORRUPT_ERROR)
    env.repo.create_image.assert_not_called()
    env.upload.store.assert_not_called()


def test_rejects_png_that_only_verify_finds_broken(env):
    data = _image_bytes('PNG')
    body = b'tEXtk\x00v'
    bad_chunk = struct.pack('>I', len(body) - 4) + body + b'\x00\x00\x00\x00'
    broken = data[:-12] + bad_chunk + data[-12:]

    message = _err(_store(broken))

    assert message == ValidationMessage(svc.IMAGE_CORRUPT_ERROR)
    env.repo.create_image.assert_not_called()


def test_rejects_header_only_image(env):
    data = _image_bytes('PNG')

    message = _err(_store(data[:33]))

    assert message == ValidationMessage(svc.IMAGE_CORRUPT_ERROR)


def test_reencode_strips_exif(env):
    source = _image_bytes('JPEG', exif=True)
    assert Image.open(BytesIO(source)).getexif()

    _store(source, 'cam.jpg').unwrap()

    assert not Image.open(BytesIO(env.stored['data'])).getexif()
    assert b'SecretCameraMaker' not in env.stored['data']


def _png_with(size, *, orientation=None, icc=None, mode='RGB') -> bytes:
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs['exif'] = exif.tobytes()
    if icc is not None:
        kwargs['icc_profile'] = icc
    buf = BytesIO()
    Image.new(mode, size).save(buf, format='PNG', **kwargs)
    return buf.getvalue()


@pytest.mark.parametrize('size', [(12000, 12000), (8000, 8000)])
def test_png_caps_reject_without_decoding_pixels(env, size):
    data = _png_with(size)

    with (
        patch.object(ImageFile.ImageFile, 'load') as load,
        patch(f'{_S}._reencode') as reencode,
    ):
        message = _err(_store(data))

    assert message.msgid in (
        svc.IMAGE_TOO_LARGE_ERROR,
        svc.IMAGE_TOO_MANY_PIXELS_ERROR,
    )
    load.assert_not_called()
    reencode.assert_not_called()


def test_rotated_png_reports_its_upright_size(env):
    data = _png_with((1000, 539), orientation=6)

    with patch.object(ImageFile.ImageFile, 'load') as load:
        message = _err(_store(data))

    assert message == ValidationMessage(
        svc.IMAGE_TOO_SMALL_ERROR, (('w', 539), ('h', 1000))
    )
    load.assert_not_called()
    assert _store(_png_with((540, 960), orientation=6)).is_ok()


def test_reencode_drops_jpeg_comment(env):
    payload = b'<html><script>alert(1)</script></html>'
    buf = BytesIO()
    Image.new('RGB', (1000, 600)).save(buf, format='JPEG', comment=payload)
    assert payload in buf.getvalue()

    _store(buf.getvalue(), 'c.jpg').unwrap()

    assert payload not in env.stored['data']
    assert 'comment' not in Image.open(BytesIO(env.stored['data'])).info


def test_reencode_drops_png_icc_profile(env):
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    source = _png_with((1000, 600), icc=icc)
    assert b'iCCP' in source

    _store(source).unwrap()

    assert b'iCCP' not in env.stored['data']


def test_reencode_keeps_png_palette_transparency(env):
    image = Image.new('P', (1000, 600))
    image.putpalette([0, 0, 0, 255, 0, 0] * 128)
    buf = BytesIO()
    image.save(buf, format='PNG', transparency=0)

    _store(buf.getvalue()).unwrap()

    assert b'tRNS' in env.stored['data']


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ((3840, 2160), (1920, 1080)),
        ((4000, 1000), (1920, 480)),
        ((1000, 4000), (270, 1080)),
        ((1200, 700), (1200, 700)),
    ],
)
def test_downscales_to_fit_1920x1080_keeping_aspect(env, source, expected):
    image = _store(_image_bytes('PNG', source)).unwrap()

    stored_size = Image.open(BytesIO(env.stored['data'])).size
    assert (image.width, image.height) == stored_size
    assert stored_size[0] <= 1920 and stored_size[1] <= 1080
    assert stored_size == pytest.approx(expected, abs=1)
    assert image.byte_size == len(env.stored['data'])


def test_store_commits_after_file_write_and_returns_metadata(env):
    image = _store(_image_bytes('PNG'), 'a.png').unwrap()

    env.repo.create_image.assert_called_once_with(image)
    env.base_repo.commit_session.assert_called_once()
    assert image.party_id == PARTY_ID
    assert image.creator_id == CREATOR_ID
    assert image.created_at.tzinfo is not None


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('C:\\Users\\bob\\pic.png', 'pic.png'),
        ('../../etc/passwd', 'passwd'),
        ('/abs/dir/a.jpg', 'a.jpg'),
        ('a\x00b\x1f\x7fc.png', 'abc.png'),
        ('line\nbreak\r.png', 'linebreak.png'),
        ('ev\u202eil\u2066x\u200f.png', 'evilx.png'),
        ('u\u2028v\u2029w.png', 'uvw.png'),
        ('zwj\u200d\U0001f468.png', 'zwj\u200d\U0001f468.png'),
        ('  padded.png  ', 'padded.png'),
        ('', 'image'),
        ('..', 'image'),
        ('.', 'image'),
        ('dir/', 'image'),
        ('\x00\x01', 'image'),
        ('e\u0301.png', '\u00e9.png'),
    ],
)
def test_normalize_filename_strips_path_and_control_chars(raw, expected):
    assert svc._normalize_filename(raw) == expected


def test_normalize_filename_truncates_to_200_chars():
    result = svc._normalize_filename('a' * 500 + '.png')

    assert len(result) == svc.FILENAME_MAX_LENGTH


def test_url_and_file_path_layout(tmp_path):
    image = TournamentImage(
        id=TournamentImageID(UUID('01900000-0000-7000-8000-000000000000')),
        party_id=PartyID('lan-36'),
        creator_id=CREATOR_ID,
        created_at=datetime.now(UTC),
        filename='x.png',
        image_type=ImageType.webp,
        width=1000,
        height=600,
        byte_size=10,
    )
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))

    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        path = svc.get_image_file_path(image)

    name = '01900000-0000-7000-8000-000000000000.webp'
    assert svc.get_image_url_path(image) == (
        f'/data/parties/lan-36/lan_tournament/images/{name}'
    )
    assert (
        path
        == Path(tmp_path)
        / 'parties'
        / 'lan-36'
        / ('lan_tournament')
        / 'images'
        / name
    )


def test_allowed_types_are_jpeg_png_webp_only():
    assert svc.ALLOWED_IMAGE_TYPES == {
        ImageType.jpeg,
        ImageType.png,
        ImageType.webp,
    }
