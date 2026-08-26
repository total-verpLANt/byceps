"""Retained pairing snapshots and per-recipient invitation work."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    MatchPairingID,
    TournamentMatchID,
)
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7


class DbMatchPairing(db.Model):
    """A historical opponent identity snapshot, independent of live rows."""

    __tablename__ = 'lan_tournament_match_pairings'
    __table_args__ = (
        db.UniqueConstraint(
            'match_id', 'generation',
            name='uq_lan_tournament_match_pairings_match_generation',
        ),
        db.CheckConstraint(
            'generation >= 0',
            name='ck_lan_tournament_match_pairings_generation',
        ),
        db.CheckConstraint(
            "side_a_kind IN ('participant', 'team')",
            name='ck_lan_tournament_match_pairings_side_a_kind',
        ),
        db.CheckConstraint(
            "side_b_kind IN ('participant', 'team')",
            name='ck_lan_tournament_match_pairings_side_b_kind',
        ),
        db.CheckConstraint(
            'side_a_kind <> side_b_kind OR side_a_id <> side_b_id',
            name='ck_lan_tournament_match_pairings_distinct_sides',
        ),
        db.CheckConstraint(
            'ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at',
            name='ck_lan_tournament_match_pairings_time_order',
        ),
    )

    id: Mapped[MatchPairingID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    match_id: Mapped[TournamentMatchID] = mapped_column(db.Uuid)
    tournament_id: Mapped[TournamentID] = mapped_column(db.Uuid)
    generation: Mapped[int] = mapped_column(db.BigInteger)
    side_a_kind: Mapped[str] = mapped_column(db.String(11))
    side_b_kind: Mapped[str] = mapped_column(db.String(11))
    side_a_id: Mapped[UUID] = mapped_column(db.Uuid)
    side_b_id: Mapped[UUID] = mapped_column(db.Uuid)
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]

    def __init__(
        self,
        pairing_id: MatchPairingID,
        match_id: TournamentMatchID,
        tournament_id: TournamentID,
        generation: int,
        side_a_kind: str,
        side_a_id: UUID,
        side_b_kind: str,
        side_b_id: UUID,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        self.id = pairing_id
        self.match_id = match_id
        self.tournament_id = tournament_id
        self.generation = generation
        self.side_a_kind = side_a_kind
        self.side_a_id = side_a_id
        self.side_b_kind = side_b_kind
        self.side_b_id = side_b_id
        self.started_at = started_at
        self.ended_at = ended_at


class DbMatchInvitation(db.Model):
    """Durable delivery work with no foreign key to deletable live matches."""

    __tablename__ = 'lan_tournament_match_invitations'
    __table_args__ = (
        db.UniqueConstraint(
            'match_id', 'pairing_generation', 'recipient_id',
            name='uq_lan_tournament_match_invitations_match_generation_recipient',
        ),
        db.CheckConstraint(
            'pairing_generation >= 0',
            name='ck_lan_tournament_match_invitations_pairing_generation',
        ),
        db.CheckConstraint(
            'expected_readiness_revision >= 0',
            name='ck_lan_tournament_match_invitations_readiness_revision',
        ),
        db.CheckConstraint(
            'attempts >= 0', name='ck_lan_tournament_match_invitations_attempts'
        ),
        db.CheckConstraint(
            "status IN ('pending', 'dispatching', 'queued', 'sending',"
            " 'accepted', 'failed', 'suppressed', 'delivery_unknown')",
            name='ck_lan_tournament_match_invitations_status',
        ),
        db.CheckConstraint(
            "(status IN ('dispatching', 'queued', 'sending')"
            ' AND dispatch_token IS NOT NULL AND lease_until IS NOT NULL)'
            " OR (status NOT IN ('dispatching', 'queued', 'sending')"
            ' AND lease_until IS NULL)',
            name='ck_lan_tournament_match_invitations_dispatch_stage',
        ),
        db.CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL)"
            " OR (status <> 'accepted' AND accepted_at IS NULL)",
            name='ck_lan_tournament_match_invitations_acceptance_stage',
        ),
        db.Index(
            'ix_lan_tournament_match_invitations_status_next_attempt',
            'status', 'next_attempt_at',
        ),
    )

    id: Mapped[MatchInvitationID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    match_id: Mapped[TournamentMatchID] = mapped_column(db.Uuid)
    tournament_id: Mapped[TournamentID] = mapped_column(db.Uuid)
    pairing_generation: Mapped[int] = mapped_column(db.BigInteger)
    recipient_id: Mapped[UserID] = mapped_column(
        db.Uuid,
        db.ForeignKey(
            'users.id', name='fk_lan_tournament_match_invitations_recipient_id'
        ),
    )
    status: Mapped[str] = mapped_column(db.String(16))
    attempts: Mapped[int] = mapped_column(default=0, server_default='0')
    dispatch_token: Mapped[UUID | None] = mapped_column(db.Uuid)
    expected_readiness_revision: Mapped[int] = mapped_column(db.BigInteger)
    next_attempt_at: Mapped[datetime | None]
    lease_until: Mapped[datetime | None]
    accepted_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(db.String(500))

    def __init__(
        self,
        invitation_id: MatchInvitationID,
        match_id: TournamentMatchID,
        tournament_id: TournamentID,
        pairing_generation: int,
        recipient_id: UserID,
        status: str,
        expected_readiness_revision: int,
        *,
        attempts: int = 0,
        dispatch_token: UUID | None = None,
        next_attempt_at: datetime | None = None,
        lease_until: datetime | None = None,
        accepted_at: datetime | None = None,
        last_error: str | None = None,
    ) -> None:
        self.id = invitation_id
        self.match_id = match_id
        self.tournament_id = tournament_id
        self.pairing_generation = pairing_generation
        self.recipient_id = recipient_id
        self.status = status
        self.expected_readiness_revision = expected_readiness_revision
        self.attempts = attempts
        self.dispatch_token = dispatch_token
        self.next_attempt_at = next_attempt_at
        self.lease_until = lease_until
        self.accepted_at = accepted_at
        self.last_error = last_error
