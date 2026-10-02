"""
byceps.services.lan_tournament.models.qualification_decision
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models import UserID

from .tournament import TournamentID


QualificationDecisionID = NewType('QualificationDecisionID', UUID)


@dataclass(frozen=True, kw_only=True)
class DecisionBlock:
    """An orga's order of one tie, with the reason for it."""

    contestant_ids: tuple[str, ...]
    reason: str
    decided_by: UserID
    decided_at: datetime


@dataclass(frozen=True, kw_only=True)
class QualificationDecision:
    """An orga's ordering of tied contestants, one block per tie.

    The scope-level `reason`, `decided_by` and `decided_at` are those of
    the latest block.
    """

    id: QualificationDecisionID
    tournament_id: TournamentID
    scope: str
    blocks: tuple[DecisionBlock, ...]
    reason: str
    decided_by: UserID
    decided_at: datetime

    @property
    def orders(self) -> tuple[tuple[str, ...], ...]:
        return tuple(b.contestant_ids for b in self.blocks)
