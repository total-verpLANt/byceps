"""
byceps.services.lan_tournament.tournament_maintenance_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import os
from pathlib import Path
import re
from uuid import UUID

from PIL import Image
import structlog

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from . import (
    tournament_image_repository,
    tournament_image_service,
    tournament_repository,
)
from .models.tournament_image import TournamentImage, TournamentImageID


log = structlog.get_logger()


GRACE_PERIOD = timedelta(hours=24)
PREVIEW_LIMIT = 100

_TYPE_LABELS = {'jpeg': 'JPEG', 'png': 'PNG', 'webp': 'WebP'}

_ORPHAN_NAME = re.compile(
    r'^(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    r'\.(?:jpeg|png|webp)$'
)


@dataclass(frozen=True, kw_only=True)
class MaintenanceSummary:
    count: int
    byte_size: int
    kept_count: int


@dataclass(frozen=True, kw_only=True)
class MaintenanceItem:
    key: str
    file_name: str
    byte_size: int
    created_at: datetime
    creator_id: UserID | None
    width: int | None
    height: int | None
    type_name: str | None
    thumbnail_url: str | None


@dataclass(frozen=True, kw_only=True)
class MaintenancePreview:
    items: list[MaintenanceItem]
    kept: list[MaintenanceItem]
    more_count: int


@dataclass(frozen=True, kw_only=True)
class CleanupReport:
    deleted_count: int
    byte_size: int
    skipped_count: int
    failed_file_count: int
    in_use_count: int = 0


@dataclass(frozen=True, kw_only=True)
class MaintenanceAction:
    id: str
    permission: str
    summarize: Callable[[PartyID, datetime], MaintenanceSummary]
    preview: Callable[[PartyID, datetime], MaintenancePreview]
    execute: Callable[[PartyID, Sequence[str], UserID, datetime], CleanupReport]


def get_actions() -> tuple[MaintenanceAction, ...]:
    """Return the maintenance actions in display order."""
    return _ACTIONS


def find_action(action_id: str) -> MaintenanceAction | None:
    """Return the action, or `None` if there is none with that ID."""
    for action in _ACTIONS:
        if action.id == action_id:
            return action

    return None


# -- unused images --


def summarize_unused_images(
    party_id: PartyID, now: datetime
) -> MaintenanceSummary:
    """Summarize the party's unused images."""
    count, byte_size = tournament_image_repository.summarize_unused_images(
        party_id, created_before=now - GRACE_PERIOD
    )
    kept_count = tournament_image_repository.count_recent_unused_images(
        party_id, created_since=now - GRACE_PERIOD
    )

    return MaintenanceSummary(
        count=count, byte_size=byte_size, kept_count=kept_count
    )


def preview_unused_images(
    party_id: PartyID, now: datetime
) -> MaintenancePreview:
    """List the unused images that a run deletes and those it keeps."""
    cutoff = now - GRACE_PERIOD
    count, _ = tournament_image_repository.summarize_unused_images(
        party_id, created_before=cutoff
    )
    images = tournament_image_repository.find_unused_images(
        party_id, created_before=cutoff, limit=PREVIEW_LIMIT
    )
    kept = tournament_image_repository.find_recent_unused_images(
        party_id, created_since=cutoff, limit=PREVIEW_LIMIT
    )

    return MaintenancePreview(
        items=[_to_item(image) for image in images],
        kept=[_to_item(image) for image in kept],
        more_count=max(count - len(images), 0),
    )


def delete_unused_images(
    party_id: PartyID, keys: Sequence[str], actor_id: UserID, now: datetime
) -> CleanupReport:
    """Delete the posted images that are still eligible."""
    image_ids = _parse_keys(keys)
    if not image_ids:
        _log_unused_images_deleted(party_id, actor_id, [])
        return CleanupReport(
            deleted_count=0, byte_size=0, skipped_count=0, failed_file_count=0
        )

    cutoff = now - GRACE_PERIOD
    try:
        locked = tournament_image_repository.lock_images_for_update(image_ids)
        deletable = []
        in_use_count = 0
        for image in locked:
            if image.party_id != party_id or image.created_at >= cutoff:
                continue
            if tournament_image_repository.is_image_referenced(image.id):
                in_use_count += 1
            else:
                deletable.append(image)
        for image in deletable:
            tournament_image_repository.delete_image(image.id)
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    failed_file_count = 0
    for image in deletable:
        path = tournament_image_service.get_image_file_path(image)
        if not _unlink(path):
            failed_file_count += 1

    byte_size = _log_unused_images_deleted(party_id, actor_id, deletable)

    return CleanupReport(
        deleted_count=len(deletable),
        byte_size=byte_size,
        skipped_count=len(image_ids) - len(deletable),
        failed_file_count=failed_file_count,
        in_use_count=in_use_count,
    )


def _log_unused_images_deleted(
    party_id: PartyID, actor_id: UserID, deleted: list[TournamentImage]
) -> int:
    """Log the run, and return the freed byte size."""
    byte_size = sum(image.byte_size for image in deleted)
    log.info(
        'Unused tournament images deleted',
        party_id=party_id,
        actor_id=actor_id,
        action=_UNUSED_IMAGES_ACTION_ID,
        count=len(deleted),
        byte_size=byte_size,
        keys=[str(image.id) for image in deleted],
    )

    return byte_size


# -- orphaned files --


def _scan_orphaned_files(
    party_id: PartyID, now: datetime
) -> tuple[list[MaintenanceItem], list[MaintenanceItem]]:
    """Return the eligible and the kept orphaned files, oldest first."""
    image_dir = tournament_image_service.get_party_image_dir(party_id)
    candidates: list[tuple[str, TournamentImageID, os.stat_result]] = []
    try:
        with os.scandir(image_dir) as entries:
            for entry in entries:
                match = _ORPHAN_NAME.match(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    candidates.append(
                        (
                            entry.name,
                            TournamentImageID(UUID(match['id'])),
                            entry.stat(follow_symlinks=False),
                        )
                    )
    except FileNotFoundError:
        return [], []
    except OSError as e:
        log.warning(
            'Scanning tournament image directory failed',
            party_id=party_id,
            error=type(e).__name__,
        )
        return [], []

    existing_ids = tournament_image_repository.find_existing_image_ids(
        party_id, [image_id for _, image_id, _ in candidates]
    )

    shown_names = tournament_image_repository.find_image_file_names_in_urls()

    cutoff = now - GRACE_PERIOD
    eligible: list[MaintenanceItem] = []
    kept: list[MaintenanceItem] = []
    for name, image_id, stat in sorted(
        candidates, key=lambda c: (c[2].st_mtime, c[0])
    ):
        if image_id in existing_ids or name in shown_names:
            continue

        item = _to_orphan_item(party_id, image_dir / name, name, stat)
        if item.created_at < cutoff:
            eligible.append(item)
        else:
            kept.append(item)

    kept.reverse()

    return eligible, kept


def _to_orphan_item(
    party_id: PartyID, path: Path, name: str, stat: os.stat_result
) -> MaintenanceItem:
    width, height = _read_dimensions(path)

    return MaintenanceItem(
        key=name,
        file_name=name,
        byte_size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        creator_id=None,
        width=width,
        height=height,
        type_name=_TYPE_LABELS.get(path.suffix[1:], path.suffix[1:].upper()),
        thumbnail_url=tournament_image_service.get_party_image_url_path(
            party_id, name
        ),
    )


def _read_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Return the dimensions from the image header, or `None` values."""
    try:
        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        return None, None

    return width, height


def summarize_orphaned_files(
    party_id: PartyID, now: datetime
) -> MaintenanceSummary:
    """Summarize the party's orphaned image files."""
    eligible, kept = _scan_orphaned_files(party_id, now)

    return MaintenanceSummary(
        count=len(eligible),
        byte_size=sum(item.byte_size for item in eligible),
        kept_count=len(kept),
    )


def preview_orphaned_files(
    party_id: PartyID, now: datetime
) -> MaintenancePreview:
    """List the orphaned files that a run deletes and those it keeps."""
    eligible, kept = _scan_orphaned_files(party_id, now)

    return MaintenancePreview(
        items=eligible[:PREVIEW_LIMIT],
        kept=kept[:PREVIEW_LIMIT],
        more_count=max(len(eligible) - PREVIEW_LIMIT, 0),
    )


def delete_orphaned_files(
    party_id: PartyID, keys: Sequence[str], actor_id: UserID, now: datetime
) -> CleanupReport:
    """Delete the posted files that are still orphaned and old enough."""
    unique_keys = list(
        dict.fromkeys(key for key in keys if isinstance(key, str))
    )
    unique_keys = unique_keys[:PREVIEW_LIMIT]
    if not unique_keys:
        _log_orphaned_files_deleted(party_id, actor_id, [])
        return CleanupReport(
            deleted_count=0, byte_size=0, skipped_count=0, failed_file_count=0
        )

    eligible, _ = _scan_orphaned_files(party_id, now)
    eligible_by_key = {item.key: item for item in eligible}
    image_dir = tournament_image_service.get_party_image_dir(party_id)
    resolved_dir = image_dir.resolve()

    deleted: list[MaintenanceItem] = []
    failed_file_count = 0
    for key in unique_keys:
        item = eligible_by_key.get(key)
        if item is None:
            continue

        path = image_dir / key
        if path.resolve().parent != resolved_dir:
            continue

        if _unlink(path):
            deleted.append(item)
        else:
            failed_file_count += 1

    byte_size = _log_orphaned_files_deleted(party_id, actor_id, deleted)

    return CleanupReport(
        deleted_count=len(deleted),
        byte_size=byte_size,
        skipped_count=len(unique_keys) - len(deleted) - failed_file_count,
        failed_file_count=failed_file_count,
    )


def _log_orphaned_files_deleted(
    party_id: PartyID, actor_id: UserID, deleted: list[MaintenanceItem]
) -> int:
    """Log the run, and return the freed byte size."""
    byte_size = sum(item.byte_size for item in deleted)
    log.info(
        'Orphaned tournament image files deleted',
        party_id=party_id,
        actor_id=actor_id,
        action=_ORPHANED_FILES_ACTION_ID,
        count=len(deleted),
        byte_size=byte_size,
        keys=[item.key for item in deleted],
    )

    return byte_size


def _parse_keys(keys: Sequence[str]) -> list[TournamentImageID]:
    """Return the unique valid keys as IDs, capped at `PREVIEW_LIMIT`."""
    image_ids: dict[UUID, None] = {}
    for key in keys:
        try:
            image_ids[UUID(key)] = None
        except (ValueError, TypeError, AttributeError):
            continue

    return [TournamentImageID(i) for i in image_ids][:PREVIEW_LIMIT]


def _to_item(image: TournamentImage) -> MaintenanceItem:
    return MaintenanceItem(
        key=str(image.id),
        file_name=image.filename,
        byte_size=image.byte_size,
        created_at=image.created_at,
        creator_id=image.creator_id,
        width=image.width,
        height=image.height,
        type_name=_TYPE_LABELS.get(
            image.image_type.name.lower(), image.image_type.name.upper()
        ),
        thumbnail_url=tournament_image_service.get_image_url_path(image),
    )


def _unlink(path: Path) -> bool:
    """Delete the file, and return `True` if it is gone."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning(
            'Deleting tournament image file failed',
            path=str(path),
            exc_info=True,
        )
        return False

    return True


_UNUSED_IMAGES_ACTION_ID = 'unused-images'
_ORPHANED_FILES_ACTION_ID = 'orphaned-files'

_ACTIONS = (
    MaintenanceAction(
        id=_UNUSED_IMAGES_ACTION_ID,
        permission='lan_tournament.maintain',
        summarize=summarize_unused_images,
        preview=preview_unused_images,
        execute=delete_unused_images,
    ),
    MaintenanceAction(
        id=_ORPHANED_FILES_ACTION_ID,
        permission='lan_tournament.maintain',
        summarize=summarize_orphaned_files,
        preview=preview_orphaned_files,
        execute=delete_orphaned_files,
    ),
)
