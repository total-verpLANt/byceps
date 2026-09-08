"""
byceps.services.lan_tournament.models.tournament_log_entry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, NewType
from uuid import UUID

from byceps.services.user.models import UserID

from .tournament import TournamentID


TournamentLogEntryID = NewType('TournamentLogEntryID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentLogEntry:
    id: TournamentLogEntryID
    occurred_at: datetime
    event_type: str
    tournament_id: TournamentID
    initiator_id: UserID | None
    data: dict[str, Any]
