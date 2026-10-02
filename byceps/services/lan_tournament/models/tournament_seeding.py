"""
byceps.services.lan_tournament.models.tournament_seeding
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models import UserID

from .tournament import TournamentID


TournamentSeedingID = NewType('TournamentSeedingID', UUID)


@dataclass(frozen=True)
class RosterEntry:
    """One entrant of the roster a seed code was made against.

    `joined_late` marks an entrant a re-seed appended; it lasts until the
    next generation. `prefill_index` is the entrant's 0-based place in the
    qualification order a playoff draft was last prefilled from, and
    `origin` its scope there (`group:0`); both are `None` for any other
    draft.
    """

    id: str
    label: str
    joined_late: bool = False
    prefill_index: int | None = None
    origin: str | None = None


@dataclass(frozen=True, kw_only=True)
class TournamentSeeding:
    id: TournamentSeedingID
    tournament_id: TournamentID
    target: str
    seed_code: str
    version: int
    generated_seed_code: str | None
    generated_at: datetime | None
    updated_by: UserID | None
    created_at: datetime
    updated_at: datetime
    roster_snapshot: tuple[RosterEntry, ...] = ()
