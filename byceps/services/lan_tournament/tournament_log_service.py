"""
byceps.services.lan_tournament.tournament_log_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from byceps.database import db
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament import tournament_repository
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_log_entry import (
    TournamentLogEntry,
    TournamentLogEntryID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7


def create_log_entry(
    event_type: str,
    tournament_id: TournamentID,
    initiator_id: UserID | None,
    *,
    data: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    """Create and persist a log entry.

    With ``commit=False``, only flushes so callers inside
    flush-disciplined flows piggyback on their own transaction.
    """
    entry = TournamentLogEntry(
        id=TournamentLogEntryID(generate_uuid7()),
        occurred_at=datetime.now(UTC),
        event_type=event_type,
        tournament_id=tournament_id,
        initiator_id=initiator_id,
        data=data if data is not None else {},
    )

    persist_log_entry(entry, commit=commit)


def persist_log_entry(
    entry: TournamentLogEntry, *, commit: bool = True
) -> None:
    """Store a log entry."""
    db_entry = _to_db_entry(entry)

    db.session.add(db_entry)

    if commit:
        db.session.commit()
    else:
        db.session.flush()


def get_entries_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentLogEntry]:
    """Return the log entries for that tournament, oldest first."""
    db_entries = db.session.scalars(
        select(DbTournamentLogEntry)
        .filter_by(tournament_id=tournament_id)
        .order_by(DbTournamentLogEntry.occurred_at)
    ).all()

    return [_db_entity_to_entry(db_entry) for db_entry in db_entries]


def purge_entries_older_than(occurred_before: datetime) -> Result[int, str]:
    """Hard-delete log entries older than the cutoff; return deleted count.

    Commits internally — intentional exception to the flush-only
    discipline because the caller is a standalone CLI command. Never
    call this from request paths, jobs, or lifecycle hooks.
    """
    try:
        num_deleted = tournament_repository.delete_log_entries_older_than(
            occurred_before
        )
    except Exception as exc:
        return Err(f'failed to purge log entries: {exc}')
    return Ok(num_deleted)


def _to_db_entry(entry: TournamentLogEntry) -> DbTournamentLogEntry:
    return DbTournamentLogEntry(
        entry.id,
        entry.occurred_at,
        entry.event_type,
        entry.tournament_id,
        entry.initiator_id,
        entry.data,
    )


def _db_entity_to_entry(
    db_entry: DbTournamentLogEntry,
) -> TournamentLogEntry:
    return TournamentLogEntry(
        id=db_entry.id,
        occurred_at=db_entry.occurred_at,
        event_type=db_entry.event_type,
        tournament_id=db_entry.tournament_id,
        initiator_id=db_entry.initiator_id,
        data=db_entry.data.copy(),
    )
