"""
byceps.services.lan_tournament.dbmodels.tournament_request
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    PrimaryKeyConstraint,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequestID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID


class DbTournamentRequest(db.Model):
    """A user-submitted proposal for a LAN tournament."""

    __tablename__ = 'lan_tournament_requests'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='pk_lan_tournament_requests'),
        UniqueConstraint(
            'party_id',
            'number',
            name='uq_lan_tournament_requests_party_number',
        ),
        CheckConstraint(
            'preferred_end_time >= preferred_start_time',
            name='ck_lan_tournament_requests_period',
        ),
        CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL",
            name='ck_lan_tournament_requests_rejection_reason',
        ),
        CheckConstraint('number > 0', name='ck_lan_tournament_requests_number'),
        CheckConstraint(
            'team_size >= 1', name='ck_lan_tournament_requests_team_size'
        ),
        CheckConstraint(
            'participant_limit >= 2', name='ck_lan_tournament_requests_limit'
        ),
        db.Index(
            'ix_lan_tournament_requests_created_tournament_id',
            'created_tournament_id',
            postgresql_where=text('created_tournament_id IS NOT NULL'),
        ),
    )

    id: Mapped[TournamentRequestID] = mapped_column(db.Uuid)
    party_id: Mapped[PartyID] = mapped_column(
        db.UnicodeText,
        ForeignKey('parties.id', name='fk_lan_tournament_requests_party_id'),
        index=True,
    )
    number: Mapped[int]
    proposer_id: Mapped[UserID] = mapped_column(
        db.Uuid,
        ForeignKey('users.id', name='fk_lan_tournament_requests_proposer_id'),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    status: Mapped[str] = mapped_column(db.UnicodeText, index=True)
    name: Mapped[str] = mapped_column(db.UnicodeText)
    game: Mapped[str] = mapped_column(db.UnicodeText)
    game_format: Mapped[str] = mapped_column(db.UnicodeText)
    elimination_mode: Mapped[str] = mapped_column(db.UnicodeText)
    team_size: Mapped[int]
    participant_limit: Mapped[int]
    preferred_start_time: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True)
    )
    preferred_end_time: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True)
    )
    description: Mapped[str] = mapped_column(db.UnicodeText)
    special_rules: Mapped[str | None] = mapped_column(db.UnicodeText)
    notes: Mapped[str | None] = mapped_column(db.UnicodeText)
    desired_template: Mapped[str | None] = mapped_column(db.UnicodeText)
    decided_at: Mapped[datetime | None] = mapped_column(
        db.DateTime(timezone=True)
    )
    decided_by_id: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        ForeignKey('users.id', name='fk_lan_tournament_requests_decided_by_id'),
    )
    rejection_reason: Mapped[str | None] = mapped_column(db.UnicodeText)
    created_tournament_id: Mapped[TournamentID | None] = mapped_column(
        db.Uuid,
        ForeignKey(
            'lan_tournaments.id',
            name='fk_lan_tournament_requests_created_tournament_id',
        ),
    )

    def __init__(
        self,
        request_id: TournamentRequestID,
        party_id: PartyID,
        number: int,
        proposer_id: UserID,
        created_at: datetime,
        status: str,
        name: str,
        game: str,
        game_format: str,
        elimination_mode: str,
        team_size: int,
        participant_limit: int,
        preferred_start_time: datetime,
        preferred_end_time: datetime,
        description: str,
        *,
        updated_at: datetime | None = None,
        special_rules: str | None = None,
        notes: str | None = None,
        desired_template: str | None = None,
        decided_at: datetime | None = None,
        decided_by_id: UserID | None = None,
        rejection_reason: str | None = None,
        created_tournament_id: TournamentID | None = None,
    ) -> None:
        self.id = request_id
        self.party_id = party_id
        self.number = number
        self.proposer_id = proposer_id
        self.created_at = created_at
        self.updated_at = updated_at
        self.status = status
        self.name = name
        self.game = game
        self.game_format = game_format
        self.elimination_mode = elimination_mode
        self.team_size = team_size
        self.participant_limit = participant_limit
        self.preferred_start_time = preferred_start_time
        self.preferred_end_time = preferred_end_time
        self.description = description
        self.special_rules = special_rules
        self.notes = notes
        self.desired_template = desired_template
        self.decided_at = decided_at
        self.decided_by_id = decided_by_id
        self.rejection_reason = rejection_reason
        self.created_tournament_id = created_tournament_id
