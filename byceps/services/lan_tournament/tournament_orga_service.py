"""
byceps.services.lan_tournament.tournament_orga_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC

from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.user import user_service
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import signals, tournament_log_service, tournament_orga_repository
from .db_error_helpers import extract_constraint_name
from .events import TournamentOrgaAssignedEvent, TournamentOrgaRevokedEvent
from .models.tournament import TournamentID
from .models.tournament_orga import (
    PublicTournamentOrga,
    TournamentOrga,
    TournamentOrgaID,
)


# The named unique constraint, and PostgreSQL's default name for it.
_DUPLICATE_ORGA_CONSTRAINT_NAMES = frozenset(
    {
        'uq_lan_tournament_orgas_tournament_user',
        'lan_tournament_orgas_tournament_id_user_id_key',
    }
)


def assign_orga(
    tournament_id: TournamentID,
    user_id: UserID,
    initiator_id: UserID,
    *,
    duties: str | None = None,
) -> Result[tuple[TournamentOrga, TournamentOrgaAssignedEvent], str]:
    """Assign the user as an orga of the tournament."""
    if tournament_orga_repository.exists_orga_for_tournament_and_user(
        tournament_id, user_id
    ):
        return Err('User is already an orga of this tournament.')

    now = datetime.now(UTC)
    orga = TournamentOrga(
        id=TournamentOrgaID(generate_uuid7()),
        tournament_id=tournament_id,
        user_id=user_id,
        assigned_at=now,
        assigned_by_id=initiator_id,
        duties=duties,
    )

    try:
        tournament_orga_repository.create_orga(orga)

        tournament_log_service.create_log_entry(
            'tournament-orga-assigned',
            tournament_id,
            initiator_id,
            data={'user_id': str(user_id)},
            commit=False,
        )

        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()

        # A concurrent request assigned the same user first.
        constraint = extract_constraint_name(e)
        if constraint in _DUPLICATE_ORGA_CONSTRAINT_NAMES:
            return Err('User is already an orga of this tournament.')
        raise
    except Exception:
        db.session.rollback()
        raise

    event = TournamentOrgaAssignedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
        user_id=user_id,
        duties=duties,
    )

    signals.tournament_orga_assigned.send(None, event=event)

    return Ok((orga, event))


def revoke_orga(
    tournament_id: TournamentID,
    user_id: UserID,
    initiator_id: UserID,
) -> Result[None, str]:
    """Revoke the user's orga assignment for the tournament."""
    orga = tournament_orga_repository.find_orga_for_tournament_and_user(
        tournament_id, user_id
    )
    if orga is None:
        return Err('User is not an orga of this tournament.')

    try:
        tournament_orga_repository.delete_orga(orga.id)

        tournament_log_service.create_log_entry(
            'tournament-orga-revoked',
            tournament_id,
            initiator_id,
            data={'user_id': str(user_id)},
            commit=False,
        )

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    event = TournamentOrgaRevokedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
        user_id=user_id,
    )

    signals.tournament_orga_revoked.send(None, event=event)

    return Ok(None)


def is_orga_for_tournament(
    user_id: UserID, tournament_id: TournamentID
) -> bool:
    """Return `True` if the user is an orga of that tournament."""
    return tournament_orga_repository.exists_orga_for_tournament_and_user(
        tournament_id, user_id
    )


def get_orgas_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentOrga]:
    """Return all orga assignments for that tournament."""
    return tournament_orga_repository.get_orgas_for_tournament(tournament_id)


def get_public_orgas_for_tournament(
    tournament_id: TournamentID,
) -> list[PublicTournamentOrga]:
    """Return the orgas of that tournament, without real names."""
    orgas = tournament_orga_repository.get_orgas_for_tournament(tournament_id)
    if not orgas:
        return []

    user_ids = {orga.user_id for orga in orgas}
    users_by_id = user_service.get_users_indexed_by_id(
        user_ids, include_avatars=True
    )

    public_orgas = []
    for orga in orgas:
        user = users_by_id.get(orga.user_id)
        if user is None or user.deleted:
            continue

        public_orgas.append(
            PublicTournamentOrga(
                user=user,
                duties=orga.duties,
            )
        )

    return public_orgas
