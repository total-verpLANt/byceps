"""
byceps.services.lan_tournament.models.tournament_orga
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.user.models import User, UserID

from .tournament import TournamentID


TournamentOrgaID = NewType('TournamentOrgaID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentOrga:
    id: TournamentOrgaID
    tournament_id: TournamentID
    user_id: UserID
    assigned_at: datetime
    assigned_by_id: UserID | None
    duties: str | None


@dataclass(frozen=True, kw_only=True)
class PublicTournamentOrga:
    user: User
    duties: str | None
