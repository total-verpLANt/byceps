"""
byceps.services.lan_tournament.models.tournament_dashboard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import get_args, Literal

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .bracket import Bracket
from .elimination_mode import EliminationMode
from .game_format import GameFormat
from .operational_timing import (
    MatchDueEpisodeID,
    MatchEscalationAcknowledgementID,
    TrafficTier,
)
from .tournament import TournamentID
from .tournament_match import TournamentMatchID


DashboardScopeKind = Literal['assigned', 'all']
DashboardView = Literal['due', 'upcoming', 'all']
DashboardState = Literal[
    'all',
    'tier-red',
    'tier-yellow',
    'tier-green',
    'ready-none',
    'ready-one',
    'ready-both',
    'ready-unavailable',
    'conflict',
    'review-open',
    'pinned',
]
DashboardSort = Literal['urgency', 'wait', 'tournament']
DashboardEmptyReason = Literal[
    'no_assignment',
    'no_current_demand',
    'no_actionable_fixtures',
    'no_filter_matches',
]
ThresholdSource = Literal['deployment', 'party']

DASHBOARD_SCOPES: tuple[str, ...] = get_args(DashboardScopeKind)
DASHBOARD_VIEWS: tuple[str, ...] = get_args(DashboardView)
DASHBOARD_STATES: tuple[str, ...] = get_args(DashboardState)
DASHBOARD_SORTS: tuple[str, ...] = get_args(DashboardSort)


class DashboardRowState(Enum):
    """What a match row shows in its tier cell, besides the tier colour."""

    DUE = 'due'
    PAUSED = 'paused'
    UPCOMING = 'upcoming'
    PARTIAL = 'partial'
    BYE = 'bye'
    AWAITING_LOBBY = 'awaiting_lobby'
    DONE = 'done'
    UNKNOWN = 'unknown'


class AckUnavailableReason(Enum):
    """Why a row offers no acknowledgement."""

    BELOW_THRESHOLD = 'below_threshold'
    RECENTLY_ACKNOWLEDGED = 'recently_acknowledged'
    PAUSED = 'paused'
    NOT_DUE = 'not_due'
    CLOCK_UNKNOWN = 'clock_unknown'
    TERMINAL = 'terminal'


@dataclass(frozen=True, kw_only=True)
class DashboardSettings:
    """The effective settings of one party, thresholds in whole minutes."""

    yellow_minutes: int
    red_minutes: int
    poll_seconds: int
    page_size: int
    threshold_source: ThresholdSource


@dataclass(frozen=True, kw_only=True)
class PartyDashboardThresholds:
    """The thresholds an orga set for one party."""

    party_id: PartyID
    yellow_minutes: int
    red_minutes: int
    revision: int
    updated_at: datetime
    updated_by: UserID


@dataclass(frozen=True, kw_only=True)
class DashboardQuery:
    """A validated list request. `per_page` comes from the settings."""

    scope: DashboardScopeKind = 'assigned'
    view: DashboardView = 'due'
    state: DashboardState = 'all'
    sort: DashboardSort = 'urgency'
    tournament_id: TournamentID | None = None
    page: int = 1
    per_page: int


@dataclass(frozen=True, kw_only=True)
class DashboardScope:
    """What a viewer may see, resolved on the server for every request.

    It is never built from submitted form data.
    """

    user_id: UserID
    party_id: PartyID
    kind: DashboardScopeKind
    tournament_ids: tuple[TournamentID, ...]
    is_global_admin: bool


@dataclass(frozen=True, kw_only=True)
class DashboardTournamentRef:
    """An authorized tournament, as a filter choice or a note card."""

    tournament_id: TournamentID
    name: str
    game: str | None = None


@dataclass(frozen=True, kw_only=True)
class DashboardMatchLocation:
    """The stored position of a match, labelled by the view helpers."""

    phase: int
    bracket: Bracket | None = None
    round: int | None = None
    group_order: int | None = None
    match_order: int | None = None


@dataclass(frozen=True, kw_only=True)
class DashboardConflictRef:
    """An authorized counterpart match of a conflict.

    `list_page` is the page of the same filtered, sorted result that
    holds the match, or `None` if the result does not contain it.
    `via_team_name` names the authorized team that demands the person.
    `game_format` is the match's effective format, which tells a lobby
    from a pairing; `None` if it is not known.
    """

    match_id: TournamentMatchID
    tournament_id: TournamentID
    tournament_name: str
    location: DashboardMatchLocation
    contestant_names: tuple[str, ...] = ()
    via_team_name: str | None = None
    list_page: int | None = None
    game_format: GameFormat | None = None


@dataclass(frozen=True, kw_only=True)
class DashboardConflict:
    """A person who is demanded in this row and in other matches.

    Only authorized counterparts appear as references. Demand outside
    the viewer's tournaments is the one boolean, whatever its size.
    `via_team_name` is the team through which this row demands them.
    """

    user_id: UserID
    user_display_name: str
    via_team_name: str | None = None
    visible_refs: tuple[DashboardConflictRef, ...] = ()
    has_external_conflict: bool = False


@dataclass(frozen=True, kw_only=True)
class DashboardAcknowledgementSummary:
    """A page-safe view of one acknowledgement."""

    id: MatchEscalationAcknowledgementID
    revision: int
    actor_display_name: str
    occurred_at: datetime
    comment: str | None = None


@dataclass(frozen=True, kw_only=True)
class DashboardStatusNote:
    """An explanatory row note as a code and its parameters."""

    code: str
    params: tuple[tuple[str, str | int], ...] = ()


@dataclass(frozen=True, kw_only=True)
class DashboardRow:
    """One match of the dashboard.

    Match age, occupancy, total wait, alert interval, last change,
    Ready, review, pin, acknowledgement and conflicts stay separate
    facts. A duration or time that is unknown is `None`, never zero.
    `tier` is set only for a due match with a known clock. The
    acknowledgement is offered iff `ack_unavailable_reason` is `None`.
    """

    match_id: TournamentMatchID
    tournament_id: TournamentID
    tournament_name: str
    game: str | None = None
    game_format: GameFormat
    elimination_mode: EliminationMode | None = None
    location: DashboardMatchLocation
    contestant_names: tuple[str, ...] = ()
    orga_names: tuple[str, ...] = ()
    state: DashboardRowState
    tier: TrafficTier | None = None
    status_note: DashboardStatusNote | None = None

    created_at: datetime
    occupied_since: datetime | None = None
    episode_opened_at: datetime | None = None
    has_prior_episode: bool = False
    total_active_wait_us: int | None = None
    alert_interval_us: int | None = None
    closed_episode_wait_us: int | None = None
    last_changed_at: datetime | None = None

    readiness_available: bool = False
    ready_at_a: datetime | None = None
    ready_at_b: datetime | None = None
    review_available: bool = False
    review_open: bool = False

    pin_revision: int = 0
    pinned_at: datetime | None = None
    pinned_by_name: str | None = None

    episode_id: MatchDueEpisodeID | None = None
    ack_revision: int = 0
    ack_unavailable_reason: AckUnavailableReason | None = None
    latest_acknowledgement: DashboardAcknowledgementSummary | None = None
    recent_acknowledgements: tuple[DashboardAcknowledgementSummary, ...] = ()
    acknowledgement_count: int = 0

    conflicts: tuple[DashboardConflict, ...] = ()


@dataclass(frozen=True, kw_only=True)
class DashboardTierCounts:
    """The due matches with a known clock per tier, over the whole scope."""

    red: int = 0
    yellow: int = 0
    green: int = 0


@dataclass(frozen=True, kw_only=True)
class DashboardNonActionableCounts:
    """The matches in scope that have no current demand, by reason."""

    paused: int = 0
    pre_start: int = 0
    partial: int = 0


@dataclass(frozen=True, kw_only=True)
class DashboardPage:
    """A consistent snapshot of one page of the dashboard.

    `page` is the requested page and is never clamped to `total_pages`.
    `query_metrics` is filled by tests only.
    """

    rows: tuple[DashboardRow, ...]
    as_of: datetime
    total_count: int
    page: int
    per_page: int
    total_pages: int
    tier_counts: DashboardTierCounts
    non_actionable_counts: DashboardNonActionableCounts
    leaderboard_only_tournaments: tuple[DashboardTournamentRef, ...] = ()
    tournament_choices: tuple[DashboardTournamentRef, ...] = ()
    empty_reason: DashboardEmptyReason | None = None
    query_metrics: tuple[tuple[str, int], ...] = ()
