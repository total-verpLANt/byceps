"""
byceps.services.lan_tournament.tournament_seeding_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select, update

from byceps.database import db
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result

from .dbmodels.seeding import (
    DbTournamentSeeding,
    snapshot_from_json,
    snapshot_to_json,
)
from .models.tournament import TournamentID
from .models.tournament_seeding import (
    RosterEntry,
    TournamentSeeding,
    TournamentSeedingID,
)


def create_seeding(seeding: TournamentSeeding) -> None:
    """Create the seeding draft without committing."""
    db_seeding = DbTournamentSeeding(
        seeding.id,
        seeding.tournament_id,
        seeding.target,
        seeding.seed_code,
        seeding.created_at,
        seeding.updated_at,
        version=seeding.version,
        generated_seed_code=seeding.generated_seed_code,
        generated_at=seeding.generated_at,
        updated_by=seeding.updated_by,
        roster_snapshot=seeding.roster_snapshot,
    )

    db.session.add(db_seeding)
    db.session.flush()


def find_seeding(
    tournament_id: TournamentID, target: str
) -> TournamentSeeding | None:
    """Return the seeding draft for that target, or `None` if not found."""
    db_seeding = db.session.execute(
        select(DbTournamentSeeding).filter_by(
            tournament_id=tournament_id, target=target
        )
    ).scalar_one_or_none()
    if db_seeding is None:
        return None
    return _db_entity_to_seeding(db_seeding)


def find_seeding_for_update(
    tournament_id: TournamentID, target: str
) -> TournamentSeeding | None:
    """Return the seeding draft with its row locked, or `None` if not found."""
    db_seeding = db.session.execute(
        select(DbTournamentSeeding)
        .filter_by(tournament_id=tournament_id, target=target)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_seeding is None:
        return None
    return _db_entity_to_seeding(db_seeding)


def update_seeding_code(
    seeding_id: TournamentSeedingID,
    *,
    seed_code: str,
    expected_version: int,
    roster_snapshot: Sequence[RosterEntry],
    updated_by: UserID | None,
    now: datetime,
) -> Result[TournamentSeeding, str]:
    """Replace the seed code and roster snapshot if the stored version is
    `expected_version`. `updated_by` is `None` for the system.

    Flushes only; the caller commits.
    """
    db_seeding = db.session.execute(
        update(DbTournamentSeeding)
        .where(
            DbTournamentSeeding.id == seeding_id,
            DbTournamentSeeding.version == expected_version,
        )
        .values(
            seed_code=seed_code,
            roster_snapshot=snapshot_to_json(roster_snapshot),
            version=DbTournamentSeeding.version + 1,
            updated_by=updated_by,
            updated_at=now,
        )
        .returning(DbTournamentSeeding)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_seeding is None:
        return Err('The seeding was changed by another orga. Reload the page.')

    db.session.flush()
    return Ok(_db_entity_to_seeding(db_seeding))


def set_generated(
    seeding_id: TournamentSeedingID,
    *,
    expected_version: int,
    generated_seed_code: str,
    roster_snapshot: Sequence[RosterEntry],
    now: datetime,
) -> Result[TournamentSeeding, str]:
    """Record the seed code the bracket was generated from, refresh the
    roster snapshot and consume the draft version.

    Refuses if the stored version is not `expected_version`. Flushes only;
    the caller commits.
    """
    db_seeding = db.session.execute(
        update(DbTournamentSeeding)
        .where(
            DbTournamentSeeding.id == seeding_id,
            DbTournamentSeeding.version == expected_version,
        )
        .values(
            generated_seed_code=generated_seed_code,
            roster_snapshot=snapshot_to_json(roster_snapshot),
            generated_at=now,
            version=DbTournamentSeeding.version + 1,
        )
        .returning(DbTournamentSeeding)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_seeding is None:
        return Err('The seeding was changed by another orga. Reload the page.')

    db.session.flush()
    return Ok(_db_entity_to_seeding(db_seeding))


def get_seedings_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentSeeding]:
    """Return all seeding drafts for that tournament."""
    db_seedings = (
        db.session.execute(
            select(DbTournamentSeeding)
            .filter_by(tournament_id=tournament_id)
            .order_by(DbTournamentSeeding.target)
        )
        .scalars()
        .all()
    )
    return [_db_entity_to_seeding(db_seeding) for db_seeding in db_seedings]


def delete_seedings_for_tournament(tournament_id: TournamentID) -> None:
    """Delete all seeding drafts for that tournament without committing."""
    db.session.execute(
        delete(DbTournamentSeeding).filter_by(tournament_id=tournament_id)
    )
    db.session.flush()


def _db_entity_to_seeding(
    db_seeding: DbTournamentSeeding,
) -> TournamentSeeding:
    return TournamentSeeding(
        id=db_seeding.id,
        tournament_id=db_seeding.tournament_id,
        target=db_seeding.target,
        seed_code=db_seeding.seed_code,
        version=db_seeding.version,
        generated_seed_code=db_seeding.generated_seed_code,
        generated_at=db_seeding.generated_at,
        updated_by=db_seeding.updated_by,
        created_at=db_seeding.created_at,
        updated_at=db_seeding.updated_at,
        roster_snapshot=snapshot_from_json(db_seeding.roster_snapshot),
    )
