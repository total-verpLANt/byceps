"""
byceps.services.lan_tournament.tournament_qualification_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from byceps.database import db

from .dbmodels.qualification_decision import (
    DbQualificationDecision,
    blocks_from_json,
    blocks_to_json,
)
from .models.qualification_decision import QualificationDecision
from .models.tournament import TournamentID


def find_decision(
    tournament_id: TournamentID, scope: str
) -> QualificationDecision | None:
    """Return the decision for that scope, or `None` if not found."""
    db_decision = db.session.execute(
        select(DbQualificationDecision)
        .filter_by(tournament_id=tournament_id, scope=scope)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_decision is None:
        return None
    return _db_entity_to_decision(db_decision)


def get_decisions_for_tournament(
    tournament_id: TournamentID,
) -> dict[str, QualificationDecision]:
    """Return the decisions of the tournament, keyed by scope."""
    db_decisions = db.session.scalars(
        select(DbQualificationDecision)
        .filter_by(tournament_id=tournament_id)
        .order_by(DbQualificationDecision.scope)
        .execution_options(populate_existing=True)
    ).all()
    return {d.scope: _db_entity_to_decision(d) for d in db_decisions}


def upsert_decision(decision: QualificationDecision) -> None:
    """Insert the decision, or replace the one stored for its scope.

    The stored row keeps its ID. Raises `ValueError` on a blank reason.
    Flushes only; the caller commits.
    """
    if not decision.reason.strip():
        raise ValueError('A qualification decision needs a reason.')

    values = {
        'ordered_contestant_ids': blocks_to_json(decision.blocks),
        'reason': decision.reason,
        'decided_by': decision.decided_by,
        'decided_at': decision.decided_at,
    }
    db.session.execute(
        insert(DbQualificationDecision)
        .values(
            id=decision.id,
            tournament_id=decision.tournament_id,
            scope=decision.scope,
            **values,
        )
        .on_conflict_do_update(
            constraint='uq_lan_tournament_qualification_decisions_scope',
            set_=values,
        )
    )
    db.session.flush()


def delete_decision(tournament_id: TournamentID, scope: str) -> None:
    """Delete the decision for that scope without committing."""
    db.session.execute(
        delete(DbQualificationDecision).filter_by(
            tournament_id=tournament_id, scope=scope
        )
    )
    db.session.flush()


def delete_decisions_for_tournament(tournament_id: TournamentID) -> None:
    """Delete every decision of the tournament without committing."""
    db.session.execute(
        delete(DbQualificationDecision).filter_by(tournament_id=tournament_id)
    )
    db.session.flush()


def _db_entity_to_decision(
    db_decision: DbQualificationDecision,
) -> QualificationDecision:
    return QualificationDecision(
        id=db_decision.id,
        tournament_id=db_decision.tournament_id,
        scope=db_decision.scope,
        blocks=blocks_from_json(
            db_decision.ordered_contestant_ids,
            reason=db_decision.reason,
            decided_by=db_decision.decided_by,
            decided_at=db_decision.decided_at,
        ),
        reason=db_decision.reason,
        decided_by=db_decision.decided_by,
        decided_at=db_decision.decided_at,
    )
