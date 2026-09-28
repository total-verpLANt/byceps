"""
byceps.services.lan_tournament.models.tournament_request
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .elimination_mode import EliminationMode
from .game_format import GameFormat
from .tournament import TournamentID


TournamentRequestID = NewType('TournamentRequestID', UUID)


class TournamentRequestStatus(Enum):
    submitted = 'submitted'
    accepted = 'accepted'
    tournament_created = 'tournament_created'
    rejected = 'rejected'
    withdrawn = 'withdrawn'


_TERMINAL_STATUSES = frozenset(
    {
        TournamentRequestStatus.tournament_created,
        TournamentRequestStatus.rejected,
        TournamentRequestStatus.withdrawn,
    }
)


@dataclass(frozen=True, kw_only=True)
class TournamentRequest:
    id: TournamentRequestID
    party_id: PartyID
    number: int
    proposer_id: UserID
    created_at: datetime
    updated_at: datetime | None = None
    status: TournamentRequestStatus
    name: str
    game: str
    game_format: GameFormat
    elimination_mode: EliminationMode
    team_size: int
    participant_limit: int
    preferred_start_time: datetime
    preferred_end_time: datetime
    description: str
    special_rules: str | None = None
    notes: str | None = None
    desired_template: str | None = None
    decided_at: datetime | None = None
    decided_by_id: UserID | None = None
    rejection_reason: str | None = None
    created_tournament_id: TournamentID | None = None

    @property
    def is_editable(self) -> bool:
        return self.status is TournamentRequestStatus.submitted

    @property
    def is_editable_by_admin(self) -> bool:
        return self.status in (
            TournamentRequestStatus.submitted,
            TournamentRequestStatus.accepted,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def tournament_deleted(self) -> bool:
        """Return whether the tournament this request produced was deleted.

        True only once a `tournament_created` request's link has been
        cleared by `delete_tournament`'s cascade -- the request stays
        `tournament_created` as history, but no longer points at a
        live tournament.
        """
        return (
            self.status is TournamentRequestStatus.tournament_created
            and self.created_tournament_id is None
        )


@dataclass(frozen=True, kw_only=True)
class TournamentFieldGap:
    supplied: list[str]
    admin_fills: list[str]
    blocking: list[str]
    info_only: list[str] = field(default_factory=list)
