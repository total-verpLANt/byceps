"""
byceps.services.lan_tournament.tournament_image_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections import defaultdict
from collections.abc import Collection
from datetime import datetime
import re

from sqlalchemy import cast, delete, exists, func, literal, or_, select, Text

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.util.image.image_type import ImageType

from .dbmodels.tournament import DbTournament
from .dbmodels.tournament_image import DbTournamentImage
from .models.tournament_image import (
    TournamentImage,
    TournamentImageID,
    TournamentImageListItem,
)

_IMAGE_URL_MARKER = '/lan_tournament/images/'
_URL_FILE_NAME = re.compile(
    re.escape(_IMAGE_URL_MARKER)
    + r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.\w+)'
)


def create_image(image: TournamentImage) -> None:
    """Persist an image (flush only -- caller commits)."""
    db_image = DbTournamentImage(
        image.id,
        image.party_id,
        image.creator_id,
        image.created_at,
        image.filename,
        image.image_type.name,
        image.width,
        image.height,
        image.byte_size,
    )

    db.session.add(db_image)
    db.session.flush()


def find_image(image_id: TournamentImageID) -> TournamentImage | None:
    """Return the image, or `None` if it does not exist."""
    db_image = db.session.get(DbTournamentImage, image_id)
    if db_image is None:
        return None

    return _db_image_to_image(db_image)


def delete_image(image_id: TournamentImageID) -> None:
    """Delete an image row (flush only -- caller commits)."""
    db.session.execute(
        delete(DbTournamentImage).where(DbTournamentImage.id == image_id)
    )
    db.session.flush()


def is_image_referenced(image_id: TournamentImageID) -> bool:
    """Return `True` if any tournament shows the image.

    A tournament shows an image through `image_id`, or through an
    `image_url` that holds the image's served path.
    """
    id_param = literal(image_id, DbTournament.image_id.type)

    return bool(
        db.session.scalar(select(exists().where(_references(id_param))))
    )


def _references(image_id):
    """Match the tournaments that show the image, by ID or by URL."""
    return or_(
        DbTournament.image_id == image_id,
        DbTournament.image_url.like(
            '%' + _IMAGE_URL_MARKER + cast(image_id, Text) + '.%'
        ),
    )


def _is_unused():
    return ~exists().where(_references(DbTournamentImage.id))


def find_image_file_names_in_urls() -> set[str]:
    """Return the image file names that any tournament's `image_url` holds."""
    image_urls = db.session.scalars(
        select(DbTournament.image_url).where(
            DbTournament.image_url.like('%' + _IMAGE_URL_MARKER + '%')
        )
    ).all()

    return {
        match[1]
        for image_url in image_urls
        if image_url
        for match in _URL_FILE_NAME.finditer(image_url)
    }


def summarize_unused_images(
    party_id: PartyID, *, created_before: datetime
) -> tuple[int, int]:
    """Return count and total byte size of the party's old unused images."""
    count, byte_size = db.session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(DbTournamentImage.byte_size), 0),
        )
        .where(DbTournamentImage.party_id == party_id)
        .where(DbTournamentImage.created_at < created_before)
        .where(_is_unused())
    ).one()

    return count, int(byte_size)


def find_unused_images(
    party_id: PartyID, *, created_before: datetime, limit: int
) -> list[TournamentImage]:
    """Return the party's old unused images, oldest first."""
    db_images = db.session.scalars(
        select(DbTournamentImage)
        .where(DbTournamentImage.party_id == party_id)
        .where(DbTournamentImage.created_at < created_before)
        .where(_is_unused())
        .order_by(DbTournamentImage.created_at, DbTournamentImage.id)
        .limit(limit)
    ).all()

    return [_db_image_to_image(db_image) for db_image in db_images]


def find_recent_unused_images(
    party_id: PartyID, *, created_since: datetime, limit: int
) -> list[TournamentImage]:
    """Return the party's unused images created since the cutoff, newest first."""
    db_images = db.session.scalars(
        select(DbTournamentImage)
        .where(DbTournamentImage.party_id == party_id)
        .where(DbTournamentImage.created_at >= created_since)
        .where(_is_unused())
        .order_by(
            DbTournamentImage.created_at.desc(), DbTournamentImage.id.desc()
        )
        .limit(limit)
    ).all()

    return [_db_image_to_image(db_image) for db_image in db_images]


def count_recent_unused_images(
    party_id: PartyID, *, created_since: datetime
) -> int:
    """Return the number of the party's unused images created since the cutoff."""
    return (
        db.session.scalar(
            select(func.count())
            .select_from(DbTournamentImage)
            .where(DbTournamentImage.party_id == party_id)
            .where(DbTournamentImage.created_at >= created_since)
            .where(_is_unused())
        )
        or 0
    )


def find_existing_image_ids(
    party_id: PartyID, image_ids: Collection[TournamentImageID]
) -> set[TournamentImageID]:
    """Return the IDs among `image_ids` that have a row of the party."""
    if not image_ids:
        return set()

    return set(
        db.session.scalars(
            select(DbTournamentImage.id)
            .where(DbTournamentImage.party_id == party_id)
            .where(DbTournamentImage.id.in_(image_ids))
        ).all()
    )


def lock_images_for_update(
    image_ids: Collection[TournamentImageID],
) -> list[TournamentImage]:
    """Lock the existing image rows in ID order and return them."""
    db_images = db.session.scalars(
        select(DbTournamentImage)
        .where(DbTournamentImage.id.in_(image_ids))
        .order_by(DbTournamentImage.id)
        .with_for_update()
    ).all()

    return [_db_image_to_image(db_image) for db_image in db_images]


def list_images(
    party_ids: Collection[PartyID],
    *,
    filename_query: str | None,
    limit: int,
    offset: int,
) -> list[TournamentImageListItem]:
    """Return the parties' images, newest first, with their users."""
    stmt = select(DbTournamentImage).where(
        DbTournamentImage.party_id.in_(party_ids)
    )
    stmt = _filter_by_filename(stmt, filename_query)
    db_images = db.session.scalars(
        stmt.order_by(
            DbTournamentImage.created_at.desc(), DbTournamentImage.id.desc()
        )
        .limit(limit)
        .offset(offset)
    ).all()

    used_by = _get_tournament_names_by_image_id(
        [db_image.id for db_image in db_images]
    )

    return [
        TournamentImageListItem(
            image=_db_image_to_image(db_image),
            used_by=tuple(used_by.get(db_image.id, ())),
        )
        for db_image in db_images
    ]


def count_images(
    party_ids: Collection[PartyID], *, filename_query: str | None
) -> int:
    """Return the number of the parties' images."""
    stmt = (
        select(func.count())
        .select_from(DbTournamentImage)
        .where(DbTournamentImage.party_id.in_(party_ids))
    )
    stmt = _filter_by_filename(stmt, filename_query)

    return db.session.scalar(stmt)


def _filter_by_filename(stmt, filename_query: str | None):
    if not filename_query:
        return stmt

    escaped = (
        filename_query.replace('\\', '\\\\')
        .replace('%', '\\%')
        .replace('_', '\\_')
    )
    return stmt.where(
        DbTournamentImage.filename.ilike(f'%{escaped}%', escape='\\')
    )


def _get_tournament_names_by_image_id(
    image_ids: Collection[TournamentImageID],
) -> dict[TournamentImageID, list[str]]:
    if not image_ids:
        return {}

    rows = db.session.execute(
        select(DbTournament.image_id, DbTournament.name)
        .where(DbTournament.image_id.in_(image_ids))
        .order_by(DbTournament.name, DbTournament.id)
    ).all()

    names_by_image_id: dict[TournamentImageID, list[str]] = defaultdict(list)
    for image_id, name in rows:
        names_by_image_id[image_id].append(name)

    return names_by_image_id


def _db_image_to_image(db_image: DbTournamentImage) -> TournamentImage:
    return TournamentImage(
        id=db_image.id,
        party_id=db_image.party_id,
        creator_id=db_image.creator_id,
        created_at=db_image.created_at,
        filename=db_image.filename,
        image_type=ImageType[db_image.image_type],
        width=db_image.width,
        height=db_image.height,
        byte_size=db_image.byte_size,
    )
