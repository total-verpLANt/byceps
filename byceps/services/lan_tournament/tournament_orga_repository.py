"""
byceps.services.lan_tournament.tournament_orga_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from sqlalchemy import delete, select

from byceps.database import db
from byceps.services.user.models import UserID

from .dbmodels.tournament_orga import DbTournamentOrga
from .models.tournament import TournamentID
from .models.tournament_orga import TournamentOrga, TournamentOrgaID


def create_orga(orga: TournamentOrga) -> None:
    """Create the orga assignment without committing."""
    db_orga = DbTournamentOrga(
        orga.id,
        orga.tournament_id,
        orga.user_id,
        orga.assigned_at,
        assigned_by_id=orga.assigned_by_id,
        duties=orga.duties,
    )

    db.session.add(db_orga)
    db.session.flush()


def find_orga(orga_id: TournamentOrgaID) -> TournamentOrga | None:
    """Return the orga assignment, or `None` if not found."""
    db_orga = db.session.get(DbTournamentOrga, orga_id)
    if db_orga is None:
        return None
    return _db_entity_to_orga(db_orga)


def find_orga_for_tournament_and_user(
    tournament_id: TournamentID, user_id: UserID
) -> TournamentOrga | None:
    """Return the orga assignment for that tournament and user,
    or `None` if not found.
    """
    db_orga = db.session.execute(
        select(DbTournamentOrga).filter_by(
            tournament_id=tournament_id, user_id=user_id
        )
    ).scalar_one_or_none()
    if db_orga is None:
        return None
    return _db_entity_to_orga(db_orga)


def get_orgas_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentOrga]:
    """Return all orga assignments for that tournament."""
    db_orgas = (
        db.session.execute(
            select(DbTournamentOrga).filter_by(tournament_id=tournament_id)
        )
        .scalars()
        .all()
    )
    return [_db_entity_to_orga(db_orga) for db_orga in db_orgas]


def get_tournament_ids_for_orga(user_id: UserID) -> set[TournamentID]:
    """Return the IDs of tournaments the user is an orga of."""
    tournament_ids = (
        db.session.execute(
            select(DbTournamentOrga.tournament_id).filter_by(user_id=user_id)
        )
        .scalars()
        .all()
    )
    return set(tournament_ids)


def delete_orga(orga_id: TournamentOrgaID) -> None:
    """Delete the orga assignment without committing."""
    db.session.execute(delete(DbTournamentOrga).filter_by(id=orga_id))
    db.session.flush()


def delete_orgas_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all orga assignments for that tournament."""
    db.session.execute(
        delete(DbTournamentOrga).filter_by(tournament_id=tournament_id)
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()


def exists_orga_for_tournament_and_user(
    tournament_id: TournamentID, user_id: UserID
) -> bool:
    """Return `True` if the user is an orga of that tournament."""
    return (
        db.session.scalar(
            select(
                select(DbTournamentOrga)
                .filter_by(tournament_id=tournament_id, user_id=user_id)
                .exists()
            )
        )
        or False
    )


def _db_entity_to_orga(db_orga: DbTournamentOrga) -> TournamentOrga:
    return TournamentOrga(
        id=db_orga.id,
        tournament_id=db_orga.tournament_id,
        user_id=db_orga.user_id,
        assigned_at=db_orga.assigned_at,
        assigned_by_id=db_orga.assigned_by_id,
        duties=db_orga.duties,
    )
