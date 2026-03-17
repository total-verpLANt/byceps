from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column, relationship

from byceps.database import db
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchCommentID,
)
from byceps.services.user.dbmodels.user import DbUser
from byceps.services.user.models.user import UserID
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7

from .match import DbTournamentMatch


class DbTournamentMatchComment(db.Model):
    """A comment on a LAN tournament match."""

    __tablename__ = 'lan_tournament_match_comments'

    id: Mapped[TournamentMatchCommentID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_match_id: Mapped[TournamentMatchID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_matches.id'),
        index=True,
    )
    match: Mapped[DbTournamentMatch] = relationship(DbTournamentMatch)
    created_by: Mapped[UserID] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
    )
    created_by_user: Mapped[DbUser] = relationship(DbUser)
    comment: Mapped[str] = mapped_column(db.UnicodeText)
    created_at: Mapped[datetime]

    def __init__(
        self,
        comment_id: TournamentMatchCommentID,
        tournament_match_id: TournamentMatchID,
        created_by: UserID,
        comment: str,
        created_at: datetime,
    ) -> None:
        self.id = comment_id
        self.tournament_match_id = tournament_match_id
        self.created_by = created_by
        self.comment = comment
        self.created_at = created_at

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('tournament_match_id')
            .add_with_lookup('created_by')
            .build()
        )
