"""
byceps.services.lan_tournament.models.tournament_image
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NewType
from uuid import UUID

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.image.image_type import ImageType


TournamentImageID = NewType('TournamentImageID', UUID)


@dataclass(frozen=True, kw_only=True)
class TournamentImage:
    id: TournamentImageID
    party_id: PartyID
    creator_id: UserID
    created_at: datetime
    filename: str
    image_type: ImageType
    width: int
    height: int
    byte_size: int


@dataclass(frozen=True, kw_only=True)
class TournamentImageListItem:
    image: TournamentImage
    used_by: tuple[str, ...]
