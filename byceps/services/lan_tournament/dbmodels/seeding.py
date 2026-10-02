"""
byceps.services.lan_tournament.dbmodels.seeding
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_seeding import (
    RosterEntry,
    TournamentSeedingID,
)
from byceps.services.user.models import UserID


class DbTournamentSeeding(db.Model):
    """The server-held seeding draft for one target of a LAN tournament."""

    __tablename__ = 'lan_tournament_seedings'
    # Named as in migration 019, because code dispatches on the names.
    __table_args__ = (
        db.UniqueConstraint(
            'tournament_id',
            'target',
            name='uq_lan_tournament_seedings_tournament_target',
        ),
        db.CheckConstraint(
            'version >= 1', name='ck_lan_tournament_seedings_version'
        ),
        db.CheckConstraint(
            "target IN ('initial', 'playoff')"
            " OR target ~ '^ffa:(SE|WB|LB):[0-9]{1,4}$'",
            name='ck_lan_tournament_seedings_target',
        ),
    )

    id: Mapped[TournamentSeedingID] = mapped_column(db.Uuid, primary_key=True)
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey(
            'lan_tournaments.id',
            name='fk_lan_tournament_seedings_tournament_id',
        ),
    )
    target: Mapped[str] = mapped_column(db.String(40))
    seed_code: Mapped[str] = mapped_column(db.UnicodeText)
    version: Mapped[int] = mapped_column(default=1, server_default='1')
    generated_seed_code: Mapped[str | None] = mapped_column(db.UnicodeText)
    generated_at: Mapped[datetime | None]
    updated_by: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id', name='fk_lan_tournament_seedings_updated_by'),
    )
    roster_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=db.text("'[]'::jsonb")
    )
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    def __init__(
        self,
        seeding_id: TournamentSeedingID,
        tournament_id: TournamentID,
        target: str,
        seed_code: str,
        created_at: datetime,
        updated_at: datetime,
        *,
        version: int = 1,
        generated_seed_code: str | None = None,
        generated_at: datetime | None = None,
        updated_by: UserID | None = None,
        roster_snapshot: Sequence[RosterEntry] = (),
    ) -> None:
        self.id = seeding_id
        self.tournament_id = tournament_id
        self.target = target
        self.seed_code = seed_code
        self.created_at = created_at
        self.updated_at = updated_at
        self.version = version
        self.generated_seed_code = generated_seed_code
        self.generated_at = generated_at
        self.updated_by = updated_by
        self.roster_snapshot = snapshot_to_json(roster_snapshot)


def snapshot_to_json(entries: Sequence[RosterEntry]) -> list[dict[str, Any]]:
    return [
        {'id': e.id, 'label': e.label}
        | ({'joined_late': True} if e.joined_late else {})
        | (
            {'prefill_index': e.prefill_index}
            if e.prefill_index is not None
            else {}
        )
        | ({'origin': e.origin} if e.origin is not None else {})
        for e in entries
    ]


def snapshot_from_json(
    raw: Sequence[dict[str, Any]],
) -> tuple[RosterEntry, ...]:
    return tuple(
        RosterEntry(
            id=e['id'],
            label=e['label'],
            joined_late=bool(e.get('joined_late')),
            prefill_index=e.get('prefill_index'),
            origin=e.get('origin'),
        )
        for e in raw
    )
