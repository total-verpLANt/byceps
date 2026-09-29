"""
byceps.services.lan_tournament.tournament_image_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime, UTC
from io import BytesIO
from pathlib import Path
import re
from typing import BinaryIO, Literal
import unicodedata

from PIL import Image, ImageOps
from sqlalchemy.exc import IntegrityError

from byceps.byceps_app import get_current_byceps_app
from byceps.services.party import party_service
from byceps.services.party.models import Party, PartyID
from byceps.services.user.models import UserID
from byceps.util import upload
from byceps.util.image.dimensions import determine_dimensions, Dimensions
from byceps.util.image.image_type import determine_image_type, ImageType
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import tournament_image_repository, tournament_repository
from .models.tournament_image import (
    TournamentImage,
    TournamentImageID,
    TournamentImageListItem,
)
from .models.tournament_team import TournamentTeamID
from .models.validation_message import ValidationMessage


ALLOWED_IMAGE_TYPES = frozenset({ImageType.jpeg, ImageType.png, ImageType.webp})
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 256 * 1024
MIN_DIMENSIONS = Dimensions(960, 540)
MAX_SOURCE_DIMENSIONS = Dimensions(8000, 8000)
MAX_SOURCE_PIXELS = 25_000_000
TARGET_DIMENSIONS = Dimensions(1920, 1080)
PICKER_PAGE_SIZE = 24
FILENAME_MAX_LENGTH = 200
_EXIF_ORIENTATION = 0x0112
_TRANSPOSING_ORIENTATIONS = frozenset({5, 6, 7, 8})

IMAGE_TYPE_ERROR = (
    'The file is not a supported image. Allowed are JPEG, PNG and WebP.'
)
IMAGE_SIZE_ERROR = 'The file is %(size)s. The maximum is 5 MB.'
IMAGE_TOO_SMALL_ERROR = (
    'The image is %(w)s × %(h)s pixels. At least 960 × 540 is required.'
)
IMAGE_TOO_LARGE_ERROR = (
    'The image is %(w)s × %(h)s pixels. At most 8000 × 8000 is allowed.'
)
IMAGE_TOO_MANY_PIXELS_ERROR = (
    'The image has %(mp)s megapixels. At most 25 are allowed.'
)
IMAGE_CORRUPT_ERROR = 'The file is damaged or not a readable image.'
IMAGE_UNAVAILABLE_ERROR = (
    'This image is no longer available. Please choose another.'
)
IMAGE_IN_USE_ERROR = 'This image is used by a tournament and cannot be deleted.'
IMAGE_NOT_CREATOR_ERROR = 'Only the uploader can delete this image.'

_FALLBACK_FILENAME = 'image'

# Bidirectional formatting characters (category Cf) that can visually
# reorder a displayed filename.
_BIDI_CONTROLS = frozenset(
    '\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
)
_DROPPED_CATEGORIES = frozenset({'Cc', 'Zl', 'Zp'})
_PATH_SEPARATORS = re.compile(r'[/\\]')


@dataclass(frozen=True, kw_only=True)
class PickerPage:
    items: list[TournamentImageListItem]
    page: int
    has_next: bool
    party_titles: dict[PartyID, str]


def set_team_image(team_id: TournamentTeamID, stream: BinaryIO) -> None:
    raise NotImplementedError


def store_uploaded_image(
    party_id: PartyID,
    creator_id: UserID,
    stream: BinaryIO,
    original_filename: str,
) -> Result[TournamentImage, ValidationMessage]:
    """Validate, re-encode and store an uploaded image."""
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return Err(ValidationMessage(IMAGE_SIZE_ERROR, (('size', len(data)),)))

    buf = BytesIO(data)

    match determine_image_type(buf, ALLOWED_IMAGE_TYPES):
        case Ok(image_type):
            pass
        case Err():
            return Err(ValidationMessage(IMAGE_TYPE_ERROR))

    try:
        source_dimensions = _upright_dimensions(buf)
    except Exception:
        return Err(ValidationMessage(IMAGE_CORRUPT_ERROR))

    if (
        source_dimensions.width < MIN_DIMENSIONS.width
        or source_dimensions.height < MIN_DIMENSIONS.height
    ):
        return Err(_dimensions_error(IMAGE_TOO_SMALL_ERROR, source_dimensions))
    if (
        source_dimensions.width > MAX_SOURCE_DIMENSIONS.width
        or source_dimensions.height > MAX_SOURCE_DIMENSIONS.height
    ):
        return Err(_dimensions_error(IMAGE_TOO_LARGE_ERROR, source_dimensions))
    pixels = source_dimensions.width * source_dimensions.height
    if pixels > MAX_SOURCE_PIXELS:
        return Err(
            ValidationMessage(
                IMAGE_TOO_MANY_PIXELS_ERROR, (('mp', f'{pixels / 1e6:.1f}'),)
            )
        )

    try:
        Image.open(buf).verify()
        buf.seek(0)

        output_data = _reencode(buf, image_type)
        output_dimensions = determine_dimensions(BytesIO(output_data))
    except Exception:
        return Err(ValidationMessage(IMAGE_CORRUPT_ERROR))

    now = datetime.now(UTC)
    image = TournamentImage(
        id=TournamentImageID(generate_uuid7()),
        party_id=party_id,
        creator_id=creator_id,
        created_at=now,
        filename=_normalize_filename(original_filename),
        image_type=image_type,
        width=output_dimensions.width,
        height=output_dimensions.height,
        byte_size=len(output_data),
    )

    path = get_image_file_path(image)
    file_may_exist = False
    try:
        tournament_image_repository.create_image(image)
        file_may_exist = True
        upload.store(
            BytesIO(output_data), path, create_parent_path_if_nonexistent=True
        )
        tournament_repository.commit_session()
    except FileExistsError:
        tournament_repository.rollback_session()
        raise
    except Exception:
        tournament_repository.rollback_session()
        if file_may_exist:
            upload.delete(path)
        raise

    return Ok(image)


def delete_staged_image(
    image_id: TournamentImageID,
    *,
    party_id: PartyID,
    requester_id: UserID,
) -> Result[None, ValidationMessage]:
    """Delete an unreferenced image, if the requester uploaded it."""
    image = tournament_image_repository.find_image(image_id)
    if image is None or image.party_id != party_id:
        return Err(ValidationMessage(IMAGE_UNAVAILABLE_ERROR))

    if image.creator_id != requester_id:
        return Err(ValidationMessage(IMAGE_NOT_CREATOR_ERROR))

    if tournament_image_repository.is_image_referenced(image_id):
        return Err(ValidationMessage(IMAGE_IN_USE_ERROR))

    try:
        tournament_image_repository.delete_image(image_id)
        tournament_repository.commit_session()
    except IntegrityError:
        # A tournament referenced the image after the check.
        tournament_repository.rollback_session()
        return Err(ValidationMessage(IMAGE_IN_USE_ERROR))
    except Exception:
        tournament_repository.rollback_session()
        raise

    upload.delete(get_image_file_path(image))

    return Ok(None)


def find_attachable_image(
    image_id: TournamentImageID, party: Party
) -> TournamentImage | None:
    """Return the image if the party's brand may use it."""
    image = tournament_image_repository.find_image(image_id)
    if image is None:
        return None

    if image.party_id == party.id:
        return image

    image_party = party_service.find_party(image.party_id)
    if image_party is None or image_party.brand_id != party.brand_id:
        return None

    return image


def list_picker_images(
    party: Party,
    *,
    scope: Literal['party', 'brand'],
    filename_query: str | None,
    page: int,
) -> PickerPage:
    """Return one page of the images the party may pick from."""
    if scope == 'party':
        parties = [party]
    else:
        parties = party_service.get_parties_for_brand(party.brand_id)
    party_ids = [p.id for p in parties]

    page = max(page, 1)
    offset = (page - 1) * PICKER_PAGE_SIZE

    items = tournament_image_repository.list_images(
        party_ids,
        filename_query=filename_query,
        limit=PICKER_PAGE_SIZE,
        offset=offset,
    )
    total = tournament_image_repository.count_images(
        party_ids, filename_query=filename_query
    )

    return PickerPage(
        items=items,
        page=page,
        has_next=total > offset + PICKER_PAGE_SIZE,
        party_titles={p.id: p.title for p in parties},
    )


def get_party_image_dir(party_id: PartyID) -> Path:
    """Return the directory holding the party's tournament images."""
    data_path = get_current_byceps_app().byceps_config.data_path
    return data_path / 'parties' / party_id / 'lan_tournament' / 'images'


def get_party_image_url_path(party_id: PartyID, file_name: str) -> str:
    """Return the served path of a file in the party's image directory."""
    return f'/data/parties/{party_id}/lan_tournament/images/{file_name}'


def get_image_url_path(image: TournamentImage) -> str:
    """Return the served path of the image."""
    return get_party_image_url_path(
        image.party_id, f'{image.id}.{image.image_type.name}'
    )


def get_image_file_path(image: TournamentImage) -> Path:
    """Return the file path of the image."""
    return get_party_image_dir(image.party_id) / (
        f'{image.id}.{image.image_type.name}'
    )


def _upright_dimensions(stream: BinaryIO) -> Dimensions:
    """Return the dimensions the image has once its EXIF rotation applies.

    Reads the header only. Pillow's `getexif()` decodes a whole PNG to find
    trailing chunks, so PNG orientation comes from the `eXIf` chunk that
    precedes the pixel data.
    """
    image = Image.open(stream)
    width, height = image.size
    exif = Image.Exif()
    if image.format == 'PNG':
        raw = image.info.get('exif')
        if raw:
            exif.load(raw)
    else:
        exif = image.getexif()
    stream.seek(0)
    if exif.get(_EXIF_ORIENTATION) in _TRANSPOSING_ORIENTATIONS:
        return Dimensions(height, width)
    return Dimensions(width, height)


def _reencode(stream: BinaryIO, image_type: ImageType) -> bytes:
    """Rotate upright, scale down and re-encode, dropping all metadata."""
    image = Image.open(stream)
    ImageOps.exif_transpose(image, in_place=True)
    image.thumbnail(TARGET_DIMENSIONS, resample=Image.Resampling.LANCZOS)
    # Comments and ICC profiles would otherwise be written back out.
    image.info = {k: v for k, v in image.info.items() if k == 'transparency'}
    output = BytesIO()
    image.save(output, format=image_type.name)
    return output.getvalue()


def _dimensions_error(msgid: str, dimensions: Dimensions) -> ValidationMessage:
    return ValidationMessage(
        msgid, (('w', dimensions.width), ('h', dimensions.height))
    )


def _normalize_filename(name: str) -> str:
    """Return a display-safe filename (basename only, no control chars)."""
    name = unicodedata.normalize('NFC', name)
    name = ''.join(
        ch
        for ch in name
        if ch not in _BIDI_CONTROLS
        and unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )
    name = _PATH_SEPARATORS.split(name)[-1].strip()
    name = name[:FILENAME_MAX_LENGTH].strip()

    if name in ('', '.', '..'):
        return _FALLBACK_FILENAME

    return name
