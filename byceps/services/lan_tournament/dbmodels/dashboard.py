"""
byceps.services.lan_tournament.dbmodels.dashboard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisodeID,
    MatchEscalationAcknowledgementID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7


class DbMatchDueEpisode(db.Model):
    """One uninterrupted period of due demand of a match.

    The tournament and match IDs are snapshots without a foreign key, so
    the history outlives matches that are regenerated or deleted.
    """

    __tablename__ = 'lan_tournament_match_due_episodes'
    # Named as in migration 023, because code dispatches on the names.
    __table_args__ = (
        db.CheckConstraint(
            'opened_clock_us >= 0',
            name='ck_lan_tournament_due_episodes_opened_clock_us',
        ),
        db.CheckConstraint(
            'closed_clock_us >= 0',
            name='ck_lan_tournament_due_episodes_closed_clock_us',
        ),
        db.CheckConstraint(
            'ack_revision >= 0',
            name='ck_lan_tournament_due_episodes_ack_revision',
        ),
        db.CheckConstraint(
            '(closed_at IS NULL AND closed_clock_us IS NULL)'
            ' OR (closed_at IS NOT NULL AND closed_clock_us IS NOT NULL)',
            name='ck_lan_tournament_due_episodes_close_pair',
        ),
        db.Index(
            'uq_lan_tournament_due_episodes_open_match',
            'match_id',
            unique=True,
            postgresql_where=text('closed_at IS NULL'),
        ),
        db.Index(
            'ix_lan_tournament_due_episodes_open_tournament',
            'tournament_id',
            postgresql_where=text('closed_at IS NULL'),
        ),
        db.Index(
            'ix_lan_tournament_due_episodes_closed_match',
            'match_id',
            postgresql_where=text('closed_at IS NOT NULL'),
        ),
    )

    id: Mapped[MatchDueEpisodeID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(db.Uuid)
    match_id: Mapped[TournamentMatchID] = mapped_column(db.Uuid)
    pairing_key: Mapped[str] = mapped_column(db.UnicodeText)
    opened_at: Mapped[datetime]
    opened_clock_us: Mapped[int] = mapped_column(db.BigInteger)
    closed_at: Mapped[datetime | None]
    closed_clock_us: Mapped[int | None] = mapped_column(db.BigInteger)
    ack_revision: Mapped[int] = mapped_column(
        db.Integer, nullable=False, default=0, server_default='0'
    )

    def __init__(
        self,
        episode_id: MatchDueEpisodeID,
        tournament_id: TournamentID,
        match_id: TournamentMatchID,
        pairing_key: str,
        opened_at: datetime,
        opened_clock_us: int,
        *,
        closed_at: datetime | None = None,
        closed_clock_us: int | None = None,
        ack_revision: int = 0,
    ) -> None:
        self.id = episode_id
        self.tournament_id = tournament_id
        self.match_id = match_id
        self.pairing_key = pairing_key
        self.opened_at = opened_at
        self.opened_clock_us = opened_clock_us
        self.closed_at = closed_at
        self.closed_clock_us = closed_clock_us
        self.ack_revision = ack_revision


class DbMatchEscalationAck(db.Model):
    """A checked delay of a due match, one per episode revision.

    Only the episode is referenced by a foreign key. The tournament,
    match and actor IDs are snapshots.
    """

    __tablename__ = 'lan_tournament_match_escalation_acks'
    # Named as in migration 023, because code dispatches on the names.
    __table_args__ = (
        db.UniqueConstraint(
            'episode_id',
            'revision',
            name='uq_lan_tournament_escalation_ack_episode_revision',
        ),
        db.CheckConstraint(
            'revision >= 1',
            name='ck_lan_tournament_escalation_ack_revision',
        ),
        db.CheckConstraint(
            'clock_us >= 0',
            name='ck_lan_tournament_escalation_ack_clock_us',
        ),
    )

    id: Mapped[MatchEscalationAcknowledgementID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    episode_id: Mapped[MatchDueEpisodeID] = mapped_column(
        db.Uuid,
        db.ForeignKey(
            'lan_tournament_match_due_episodes.id',
            name='fk_lan_tournament_escalation_acks_episode_id',
        ),
    )
    tournament_id: Mapped[TournamentID] = mapped_column(db.Uuid)
    match_id: Mapped[TournamentMatchID] = mapped_column(db.Uuid)
    actor_id: Mapped[UserID] = mapped_column(db.Uuid)
    revision: Mapped[int] = mapped_column(db.Integer)
    occurred_at: Mapped[datetime]
    clock_us: Mapped[int] = mapped_column(db.BigInteger)
    comment: Mapped[str | None] = mapped_column(db.String(500))

    def __init__(
        self,
        ack_id: MatchEscalationAcknowledgementID,
        episode_id: MatchDueEpisodeID,
        tournament_id: TournamentID,
        match_id: TournamentMatchID,
        actor_id: UserID,
        revision: int,
        occurred_at: datetime,
        clock_us: int,
        *,
        comment: str | None = None,
    ) -> None:
        self.id = ack_id
        self.episode_id = episode_id
        self.tournament_id = tournament_id
        self.match_id = match_id
        self.actor_id = actor_id
        self.revision = revision
        self.occurred_at = occurred_at
        self.clock_us = clock_us
        self.comment = comment


class DbMatchDashboardAnnotation(db.Model):
    """The shared pin of a live match.

    No foreign keys: the row is removed explicitly with its match.
    """

    __tablename__ = 'lan_tournament_match_dashboard_annotations'
    # Named as in migration 023, because code dispatches on the names.
    __table_args__ = (
        db.CheckConstraint(
            'revision >= 0',
            name='ck_lan_tournament_match_dashboard_annotations_revision',
        ),
        db.CheckConstraint(
            '(pinned_at IS NULL AND pinned_by IS NULL)'
            ' OR (pinned_at IS NOT NULL AND pinned_by IS NOT NULL)',
            name='ck_lan_tournament_match_dashboard_annotations_pin_pair',
        ),
    )

    match_id: Mapped[TournamentMatchID] = mapped_column(
        db.Uuid, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(db.Uuid)
    revision: Mapped[int] = mapped_column(
        db.Integer, nullable=False, default=0, server_default='0'
    )
    pinned_at: Mapped[datetime | None]
    pinned_by: Mapped[UserID | None] = mapped_column(db.Uuid)
    updated_at: Mapped[datetime]
    updated_by: Mapped[UserID] = mapped_column(db.Uuid)

    def __init__(
        self,
        match_id: TournamentMatchID,
        tournament_id: TournamentID,
        updated_at: datetime,
        updated_by: UserID,
        *,
        revision: int = 0,
        pinned_at: datetime | None = None,
        pinned_by: UserID | None = None,
    ) -> None:
        self.match_id = match_id
        self.tournament_id = tournament_id
        self.updated_at = updated_at
        self.updated_by = updated_by
        self.revision = revision
        self.pinned_at = pinned_at
        self.pinned_by = pinned_by


class DbDashboardPartyThresholds(db.Model):
    """The Wartung override of the traffic thresholds of one party.

    The party ID is a snapshot without a foreign key. A missing row means
    the deployment default applies.
    """

    __tablename__ = 'lan_tournament_dashboard_party_thresholds'
    # Named as in migration 023, because code dispatches on the names.
    __table_args__ = (
        db.CheckConstraint(
            'yellow_minutes >= 1',
            name='ck_lan_tournament_dashboard_party_thresholds_yellow_min',
        ),
        db.CheckConstraint(
            'yellow_minutes < red_minutes',
            name='ck_lan_tournament_dashboard_party_thresholds_order',
        ),
        db.CheckConstraint(
            'red_minutes <= 1440',
            name='ck_lan_tournament_dashboard_party_thresholds_red_max',
        ),
        db.CheckConstraint(
            'revision >= 1',
            name='ck_lan_tournament_dashboard_party_thresholds_revision',
        ),
    )

    party_id: Mapped[PartyID] = mapped_column(db.UnicodeText, primary_key=True)
    yellow_minutes: Mapped[int] = mapped_column(db.Integer)
    red_minutes: Mapped[int] = mapped_column(db.Integer)
    revision: Mapped[int] = mapped_column(
        db.Integer, nullable=False, default=1, server_default='1'
    )
    updated_at: Mapped[datetime]
    updated_by: Mapped[UserID] = mapped_column(db.Uuid)

    def __init__(
        self,
        party_id: PartyID,
        yellow_minutes: int,
        red_minutes: int,
        updated_at: datetime,
        updated_by: UserID,
        *,
        revision: int = 1,
    ) -> None:
        self.party_id = party_id
        self.yellow_minutes = yellow_minutes
        self.red_minutes = red_minutes
        self.revision = revision
        self.updated_at = updated_at
        self.updated_by = updated_by
