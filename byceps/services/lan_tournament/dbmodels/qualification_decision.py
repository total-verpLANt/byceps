"""
byceps.services.lan_tournament.dbmodels.qualification_decision
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Sequence
from datetime import datetime
import json
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecisionID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.user.models import UserID


class DbQualificationDecision(db.Model):
    """An orga's ordering of a tie, one per tournament and scope."""

    __tablename__ = 'lan_tournament_qualification_decisions'
    # Named as in migration 020, because code dispatches on the names.
    __table_args__ = (
        db.UniqueConstraint(
            'tournament_id',
            'scope',
            name='uq_lan_tournament_qualification_decisions_scope',
        ),
        db.CheckConstraint(
            "length(btrim(reason, E' \\t\\r\\n')) > 0",
            name='ck_lan_tournament_qualification_decisions_reason',
        ),
    )

    id: Mapped[QualificationDecisionID] = mapped_column(
        db.Uuid, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey(
            'lan_tournaments.id',
            name='fk_lan_tournament_qualification_decisions_tournament_id',
        ),
    )
    scope: Mapped[str] = mapped_column(db.String(60))
    ordered_contestant_ids: Mapped[str] = mapped_column(db.UnicodeText)
    reason: Mapped[str] = mapped_column(db.UnicodeText)
    decided_by: Mapped[UserID] = mapped_column(
        db.Uuid,
        db.ForeignKey(
            'users.id',
            name='fk_lan_tournament_qualification_decisions_decided_by',
        ),
    )
    decided_at: Mapped[datetime]

    def __init__(
        self,
        decision_id: QualificationDecisionID,
        tournament_id: TournamentID,
        scope: str,
        blocks: Sequence[DecisionBlock],
        reason: str,
        decided_by: UserID,
        decided_at: datetime,
    ) -> None:
        self.id = decision_id
        self.tournament_id = tournament_id
        self.scope = scope
        self.ordered_contestant_ids = blocks_to_json(blocks)
        self.reason = reason
        self.decided_by = decided_by
        self.decided_at = decided_at


def blocks_to_json(blocks: Sequence[DecisionBlock]) -> str:
    return json.dumps(
        [
            {
                'ids': list(b.contestant_ids),
                'reason': b.reason,
                'decided_by': str(b.decided_by),
                'decided_at': b.decided_at.isoformat(),
            }
            for b in blocks
        ]
    )


def blocks_from_json(
    raw: str,
    *,
    reason: str,
    decided_by: UserID,
    decided_at: datetime,
) -> tuple[DecisionBlock, ...]:
    """Read the stored blocks.

    A legacy flat list of IDs reads as one block, a list of lists as one
    block per list; both take the row's meta. Raises `ValueError` on
    anything else.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError('Malformed qualification decision blocks.') from exc

    if not isinstance(data, list):
        raise ValueError('Malformed qualification decision blocks.')

    if not data:
        return ()

    if all(isinstance(item, str) for item in data):
        return (
            DecisionBlock(
                contestant_ids=tuple(data),
                reason=reason,
                decided_by=decided_by,
                decided_at=decided_at,
            ),
        )

    return tuple(
        _block_from_json(item, reason, decided_by, decided_at) for item in data
    )


def _block_from_json(
    item: object, reason: str, decided_by: UserID, decided_at: datetime
) -> DecisionBlock:
    meta: dict[str, Any] = {}
    if isinstance(item, list):
        ids: object = item
    elif isinstance(item, dict):
        ids = item.get('ids')
        meta = item
    else:
        raise ValueError('Malformed qualification decision blocks.')

    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError('Malformed qualification decision blocks.')

    try:
        block_decided_by = (
            UserID(UUID(meta['decided_by']))
            if meta.get('decided_by')
            else decided_by
        )
        block_decided_at = (
            datetime.fromisoformat(meta['decided_at'])
            if meta.get('decided_at')
            else decided_at
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError('Malformed qualification decision blocks.') from exc

    block_reason = meta.get('reason')
    return DecisionBlock(
        contestant_ids=tuple(ids),
        reason=block_reason if isinstance(block_reason, str) else reason,
        decided_by=block_decided_by,
        decided_at=block_decided_at,
    )
