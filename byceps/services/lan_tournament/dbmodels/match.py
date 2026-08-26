from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column, relationship

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchPairingID,
    TournamentMatchID,
)
from byceps.services.user.dbmodels import DbUser
from byceps.services.user.models import UserID
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7

from .tournament import DbTournament


class DbTournamentMatch(db.Model):
    """A match in a LAN tournament."""

    __tablename__ = 'lan_tournament_matches'
    __table_args__ = (
        db.CheckConstraint(
            'pairing_generation >= 0',
            name='ck_lan_tournament_matches_pairing_generation',
        ),
        db.CheckConstraint(
            'readiness_revision >= 0',
            name='ck_lan_tournament_matches_readiness_revision',
        ),
        db.CheckConstraint(
            'phase IN (1, 2)', name='ck_lan_tournament_matches_phase'
        ),
        db.Index(
            'ix_lan_tournament_matches_tournament_phase',
            'tournament_id',
            'phase',
        ),
        db.Index(
            'ix_lan_tournament_matches_tournament_seeding_target',
            'tournament_id',
            'seeding_target',
        ),
    )

    id: Mapped[TournamentMatchID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournaments.id'),
        index=True,
    )
    tournament: Mapped[DbTournament] = relationship(DbTournament)
    group_order: Mapped[int | None]
    match_order: Mapped[int | None]
    round: Mapped[int | None]
    next_match_id: Mapped[TournamentMatchID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_matches.id'),
        index=True,
    )
    bracket: Mapped[str | None] = mapped_column(
        db.String(8),
    )
    loser_next_match_id: Mapped[TournamentMatchID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_matches.id'),
        index=True,
    )
    confirmed_by: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
    )
    confirmed_by_user: Mapped[DbUser | None] = relationship(
        DbUser, foreign_keys=[confirmed_by]
    )
    occupied_since: Mapped[datetime | None]
    ready_at_a: Mapped[datetime | None]
    ready_at_b: Mapped[datetime | None]
    ready_by_a: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
    )
    ready_by_b: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
    )
    both_ready_notified_at: Mapped[datetime | None]
    pairing_generation: Mapped[int] = mapped_column(
        db.BigInteger, default=0, server_default='0'
    )
    readiness_revision: Mapped[int] = mapped_column(
        db.BigInteger, default=0, server_default='0'
    )
    pairing_id: Mapped[MatchPairingID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey(
            'lan_tournament_match_pairings.id',
            name='fk_lan_tournament_matches_pairing_id',
        ),
    )
    invitation_hold_a: Mapped[bool] = mapped_column(
        default=False, server_default=db.false()
    )
    invitation_hold_b: Mapped[bool] = mapped_column(
        default=False, server_default=db.false()
    )
    created_at: Mapped[datetime]
    phase: Mapped[int] = mapped_column(
        db.SmallInteger, nullable=False, default=1, server_default='1'
    )
    seeding_target: Mapped[str | None] = mapped_column(db.String(40))

    def __init__(
        self,
        match_id: TournamentMatchID,
        tournament_id: TournamentID,
        created_at: datetime,
        *,
        group_order: int | None = None,
        match_order: int | None = None,
        round: int | None = None,
        next_match_id: TournamentMatchID | None = None,
        bracket: str | None = None,
        loser_next_match_id: TournamentMatchID | None = None,
        confirmed_by: UserID | None = None,
        phase: int = 1,
        seeding_target: str | None = None,
        occupied_since: datetime | None = None,
        ready_at_a: datetime | None = None,
        ready_at_b: datetime | None = None,
        ready_by_a: UserID | None = None,
        ready_by_b: UserID | None = None,
        both_ready_notified_at: datetime | None = None,
        pairing_generation: int = 0,
        readiness_revision: int = 0,
        pairing_id: MatchPairingID | None = None,
        invitation_hold_a: bool = False,
        invitation_hold_b: bool = False,
    ) -> None:
        self.id = match_id
        self.tournament_id = tournament_id
        self.created_at = created_at
        self.group_order = group_order
        self.match_order = match_order
        self.round = round
        self.next_match_id = next_match_id
        self.bracket = bracket
        self.loser_next_match_id = loser_next_match_id
        self.confirmed_by = confirmed_by
        self.phase = phase
        self.seeding_target = seeding_target
        self.occupied_since = occupied_since
        self.ready_at_a = ready_at_a
        self.ready_at_b = ready_at_b
        self.ready_by_a = ready_by_a
        self.ready_by_b = ready_by_b
        self.both_ready_notified_at = both_ready_notified_at
        self.pairing_generation = pairing_generation
        self.readiness_revision = readiness_revision
        self.pairing_id = pairing_id
        self.invitation_hold_a = invitation_hold_a
        self.invitation_hold_b = invitation_hold_b

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('tournament_id')
            .add_with_lookup('round')
            .add_with_lookup('match_order')
            .build()
        )
