"""
byceps.services.lan_tournament.models.operational_timing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import NewType
from uuid import UUID

from byceps.services.user.models import UserID

from .tournament import TournamentID
from .tournament_match import TournamentMatchID


MatchDueEpisodeID = NewType('MatchDueEpisodeID', UUID)
MatchEscalationAcknowledgementID = NewType(
    'MatchEscalationAcknowledgementID', UUID
)


class TrafficTier(Enum):
    """The alert tier of a due match, derived and never persisted."""

    GREEN = 'green'
    YELLOW = 'yellow'
    RED = 'red'


@dataclass(frozen=True, kw_only=True)
class OperationalClock:
    """The cumulative active time of a tournament.

    Time counts only while the tournament runs. `running_since` is set
    while it runs, `activated_at` is set once it started under this
    feature. A clock without `activated_at` has no known history.
    """

    elapsed_us: int = 0
    running_since: datetime | None = None
    activated_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class MatchDueEpisode:
    """One uninterrupted period of due demand of a match.

    The tournament and match IDs are snapshots, not live references.
    """

    id: MatchDueEpisodeID
    tournament_id: TournamentID
    match_id: TournamentMatchID
    pairing_key: str
    opened_at: datetime
    opened_clock_us: int
    closed_at: datetime | None = None
    closed_clock_us: int | None = None
    ack_revision: int = 0


@dataclass(frozen=True, kw_only=True)
class MatchEscalationAcknowledgement:
    """An immutable record that an orga checked a delay.

    The IDs and the actor are snapshots, not live references.
    """

    id: MatchEscalationAcknowledgementID
    episode_id: MatchDueEpisodeID
    tournament_id: TournamentID
    match_id: TournamentMatchID
    revision: int
    occurred_at: datetime
    clock_us: int
    actor_id: UserID
    comment: str | None = None


@dataclass(frozen=True, kw_only=True)
class MatchPinState:
    """The shared pin of a match, with its compare-and-set revision.

    A match that was never annotated has no state. Unpinning a match
    that is not pinned changes neither revision nor timestamps.
    """

    match_id: TournamentMatchID
    tournament_id: TournamentID
    revision: int
    pinned_at: datetime | None
    pinned_by: UserID | None
    updated_at: datetime
    updated_by: UserID
