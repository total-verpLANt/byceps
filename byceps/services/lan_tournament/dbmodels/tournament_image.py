"""
byceps.services.lan_tournament.dbmodels.tournament_image
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImageID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID


class DbTournamentImage(db.Model):
    """An uploaded cover image for LAN tournaments."""

    __tablename__ = 'lan_tournament_images'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='pk_lan_tournament_images'),
        CheckConstraint(
            'char_length(filename) BETWEEN 1 AND 200',
            name='ck_lan_tournament_images_filename_length',
        ),
        CheckConstraint(
            "image_type IN ('jpeg', 'png', 'webp')",
            name='ck_lan_tournament_images_image_type',
        ),
        CheckConstraint(
            'width > 0 AND height > 0',
            name='ck_lan_tournament_images_dimensions',
        ),
        CheckConstraint(
            'byte_size > 0', name='ck_lan_tournament_images_byte_size'
        ),
        db.Index(
            'ix_lan_tournament_images_party_id_created_at',
            'party_id',
            'created_at',
        ),
    )

    id: Mapped[TournamentImageID] = mapped_column(db.Uuid)
    party_id: Mapped[PartyID] = mapped_column(
        db.UnicodeText,
        ForeignKey('parties.id', name='fk_lan_tournament_images_party_id'),
    )
    creator_id: Mapped[UserID] = mapped_column(
        db.Uuid,
        ForeignKey('users.id', name='fk_lan_tournament_images_creator_id'),
    )
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True))
    filename: Mapped[str] = mapped_column(db.UnicodeText)
    image_type: Mapped[str] = mapped_column(db.UnicodeText)
    width: Mapped[int]
    height: Mapped[int]
    byte_size: Mapped[int]

    def __init__(
        self,
        image_id: TournamentImageID,
        party_id: PartyID,
        creator_id: UserID,
        created_at: datetime,
        filename: str,
        image_type: str,
        width: int,
        height: int,
        byte_size: int,
    ) -> None:
        self.id = image_id
        self.party_id = party_id
        self.creator_id = creator_id
        self.created_at = created_at
        self.filename = filename
        self.image_type = image_type
        self.width = width
        self.height = height
        self.byte_size = byte_size
