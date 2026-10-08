"""
byceps.services.lan_tournament.tournament_dashboard_repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read-only queries of the orga dashboard. Nothing here writes, flushes,
commits or locks.
"""

from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    and_,
    BigInteger,
    case,
    cast,
    ColumnElement,
    CompoundSelect,
    CTE,
    DateTime,
    distinct,
    exists,
    extract,
    false,
    func,
    literal,
    not_,
    null,
    or_,
    Select,
    select,
    Text,
    true,
    union_all,
)
from sqlalchemy.engine import Row
from sqlalchemy.orm import aliased, InstrumentedAttribute
from sqlalchemy.types import TypeEngine

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from .dbmodels.dashboard import (
    DbMatchDashboardAnnotation,
    DbMatchDueEpisode,
    DbMatchEscalationAck,
)
from .dbmodels.match import DbTournamentMatch
from .dbmodels.match_contestant import DbTournamentMatchToContestant
from .dbmodels.match_readiness import DbMatchPairing
from .dbmodels.participant import DbTournamentParticipant
from .dbmodels.team import DbTournamentTeam
from .dbmodels.tournament import DbTournament
from .dbmodels.tournament_orga import DbTournamentOrga
from .models.bracket import Bracket
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.operational_timing import (
    MatchDueEpisodeID,
    MatchEscalationAcknowledgementID,
)
from .models.tournament import TournamentID
from .models.tournament_dashboard import (
    DashboardMatchLocation,
    DashboardNonActionableCounts,
    DashboardQuery,
    DashboardRowState,
    DashboardScope,
    DashboardSettings,
    DashboardState,
    DashboardTierCounts,
    DashboardTournamentRef,
    DashboardView,
)
from .models.tournament_match import TournamentMatchID
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeamID
from .tournament_operational_domain_service import normalize_utc


_MICROSECONDS_PER_MINUTE = 60_000_000

# The names the pure policy of `tournament_operational_domain_service`
# builds its due set from.
_DEMAND_STATUSES = (TournamentStatus.ONGOING.name, TournamentStatus.PAUSED.name)
_RUNNING_STATUS = TournamentStatus.ONGOING.name
_TERMINAL_STATUSES = (
    TournamentStatus.COMPLETED.name,
    TournamentStatus.CANCELLED.name,
)
_PRE_START_STATUSES = (
    TournamentStatus.DRAFT.name,
    TournamentStatus.REGISTRATION_OPEN.name,
    TournamentStatus.REGISTRATION_CLOSED.name,
)
_ELIMINATION_MODES = (
    EliminationMode.SINGLE_ELIMINATION.name,
    EliminationMode.DOUBLE_ELIMINATION.name,
)
_ROUND_ROBIN = EliminationMode.ROUND_ROBIN.name
_ONE_V_ONE = GameFormat.ONE_V_ONE.name
_FREE_FOR_ALL = GameFormat.FREE_FOR_ALL.name
_HIGHSCORE = GameFormat.HIGHSCORE.name
_BRACKETS = tuple(bracket.value for bracket in Bracket)
_WINNERS = Bracket.WINNERS.value
_LOSERS = Bracket.LOSERS.value
_GRAND_FINAL = Bracket.GRAND_FINAL.value
# The seeding target of a winners round is `ffa:WB:<round>`.
_WINNERS_TARGET_PREFIX = 'ffa:WB:'

_DUE = DashboardRowState.DUE.value
_PAUSED = DashboardRowState.PAUSED.value
_UPCOMING = DashboardRowState.UPCOMING.value
_PARTIAL = DashboardRowState.PARTIAL.value
_BYE = DashboardRowState.BYE.value
_AWAITING_LOBBY = DashboardRowState.AWAITING_LOBBY.value
_DONE = DashboardRowState.DONE.value
_UNKNOWN = DashboardRowState.UNKNOWN.value

_TIER_RED = 'red'
_TIER_YELLOW = 'yellow'
_TIER_GREEN = 'green'

_NOT_DUE_STATES = (_UPCOMING, _PARTIAL, _AWAITING_LOBBY)
_VIEW_STATES: dict[DashboardView, tuple[str, ...]] = {
    'due': (_DUE, _UNKNOWN),
    'upcoming': _NOT_DUE_STATES,
    'all': tuple(state.value for state in DashboardRowState),
}

# A page of an unusual size is a client error: refuse it before the
# database sees it.
MAX_PAGE_NUMBER = 1_000_000

_ACK_HISTORY_LENGTH = 3

# What a row of the page statement is: the counts, a listed match or one
# person of a listed match who is demanded elsewhere too.
_COUNTS_KIND = 'counts'
_ROW_KIND = 'row'
_CONFLICT_KIND = 'conflict'


@dataclass(frozen=True, kw_only=True)
class DashboardContestantRecord:
    """A real contestant of a listed match, with what names it."""

    match_id: TournamentMatchID
    identity: str
    participant_id: TournamentParticipantID | None
    team_id: TournamentTeamID | None
    user_id: UserID | None
    team_name: str | None
    score: int | None


@dataclass(frozen=True, kw_only=True)
class DashboardMatchRecord:
    """The facts of one listed match, as the canonical query yields them."""

    match_id: TournamentMatchID
    tournament_id: TournamentID
    tournament_name: str
    game: str | None
    game_format: GameFormat
    elimination_mode: EliminationMode | None
    location: DashboardMatchLocation
    state: DashboardRowState
    confirmed: bool
    contestant_count: int
    created_at: datetime
    occupied_since: datetime | None
    last_changed_at: datetime | None
    episode_id: MatchDueEpisodeID | None
    episode_opened_at: datetime | None
    ack_revision: int
    has_prior_episode: bool
    reopened_same_pairing: bool
    total_active_wait_us: int | None
    alert_interval_us: int | None
    closed_episode_wait_us: int | None
    readiness_available: bool
    ready_at_a: datetime | None
    ready_at_b: datetime | None
    side_a_identity: str | None
    pin_revision: int
    pinned_at: datetime | None
    pinned_by: UserID | None
    earlier_open_round: int | None
    lobby_size: int | None
    has_conflict: bool = False


@dataclass(frozen=True, kw_only=True)
class DashboardAcknowledgementRecord:
    """One acknowledgement of a current episode.

    `episode_count` is the number of acknowledgements of its episode.
    """

    id: MatchEscalationAcknowledgementID
    episode_id: MatchDueEpisodeID
    revision: int
    actor_id: UserID
    occurred_at: datetime
    comment: str | None
    episode_count: int


@dataclass(frozen=True, kw_only=True)
class DashboardConflictRefRecord:
    """An authorized match in which a conflicted person is demanded too.

    `list_page` is the page of the requested list that holds the match,
    or `None` if the list does not contain it.
    """

    match_id: TournamentMatchID
    tournament_id: TournamentID
    tournament_name: str
    location: DashboardMatchLocation
    side_a_identity: str | None
    via_team_name: str | None
    list_page: int | None
    game_format: GameFormat | None = None


@dataclass(frozen=True, kw_only=True)
class DashboardConflictRecord:
    """One person of a listed match who is demanded in other matches too.

    Matches of tournaments the viewer may not see are not in `refs`.
    Their existence is the single `has_external_conflict`, whatever
    their number.
    """

    match_id: TournamentMatchID
    user_id: UserID
    via_team_name: str | None
    refs: tuple[DashboardConflictRefRecord, ...]
    has_external_conflict: bool


@dataclass(frozen=True, kw_only=True)
class DashboardPageData:
    """What the canonical query returns for one page of one scope.

    The counts cover the whole authorized scope. Only `total_count` and
    `view_total_count` follow the query. `contestants` also names the
    authorized counterparts of the conflicts.
    """

    total_count: int
    view_total_count: int
    tier_counts: DashboardTierCounts
    non_actionable_counts: DashboardNonActionableCounts
    records: tuple[DashboardMatchRecord, ...]
    contestants: Mapping[
        TournamentMatchID, tuple[DashboardContestantRecord, ...]
    ]
    orga_user_ids: Mapping[TournamentID, tuple[UserID, ...]]
    acknowledgements: Mapping[
        MatchDueEpisodeID, tuple[DashboardAcknowledgementRecord, ...]
    ]
    tournament_choices: tuple[DashboardTournamentRef, ...]
    leaderboard_only_tournaments: tuple[DashboardTournamentRef, ...]
    conflicts: Mapping[
        TournamentMatchID, tuple[DashboardConflictRecord, ...]
    ] = field(default_factory=dict)


def _empty_page_data() -> DashboardPageData:
    return DashboardPageData(
        total_count=0,
        view_total_count=0,
        tier_counts=DashboardTierCounts(),
        non_actionable_counts=DashboardNonActionableCounts(),
        records=(),
        contestants={},
        orga_user_ids={},
        acknowledgements={},
        tournament_choices=(),
        leaderboard_only_tournaments=(),
    )


@contextmanager
def read_snapshot() -> Iterator[None]:
    """Run the enclosed reads in one read-only `REPEATABLE READ` transaction.

    A single moment of the database is visible to all of them. The
    isolation is transaction-scoped, so nothing is left on the pooled
    connection. Call it outside of a write transaction: it rolls back
    what the session has begun, and it refuses to drop unflushed changes.
    It cannot be nested.
    """
    session = db.session
    if session.new or session.dirty or session.deleted:
        raise RuntimeError(
            'A dashboard snapshot must not drop pending changes.'
        )

    session.rollback()
    session.connection(
        execution_options={
            'isolation_level': 'REPEATABLE READ',
            'postgresql_readonly': True,
        }
    )
    try:
        yield
    finally:
        session.rollback()


def get_dashboard_tournament_ids(
    party_id: PartyID, user_id: UserID, *, include_all: bool
) -> tuple[TournamentID, ...]:
    """Return the tournaments of the party the user may see.

    That is every tournament of the party with `include_all`, else the
    ones the user is assigned to as orga.
    """
    statement = select(DbTournament.id).where(DbTournament.party_id == party_id)
    if not include_all:
        statement = statement.where(
            DbTournament.id.in_(
                select(DbTournamentOrga.tournament_id).where(
                    DbTournamentOrga.user_id == user_id
                )
            )
        )

    return tuple(db.session.scalars(statement.order_by(DbTournament.id)).all())


def query_dashboard_matches(
    scope: DashboardScope,
    query: DashboardQuery,
    *,
    now: datetime,
    settings: DashboardSettings,
) -> DashboardPageData:
    """Return one page of the matches the scope may see.

    The scope bounds the lists before counting, filtering, sorting and
    paging. All of it is one canonical relation of the scope's matches,
    so a count, a filter and a row cannot disagree. Whether a match is
    in conflict is part of that relation. It is judged over the whole
    party, so demand outside the scope counts, but only as the boolean
    `has_external_conflict`.

    The due oracle runs once, over the party. The counts, the rows and
    the conflicts are read from it in one statement, so neither the
    work nor a hidden match leaves the database twice.
    """
    if not scope.tournament_ids:
        return _empty_page_data()

    at = normalize_utc(now)
    demand = _party_demand(scope.party_id, at)
    users = _demand_users(demand)
    relation = _relation(scope, settings, demand, users)

    rows = db.session.execute(_page_statement(relation, users, query)).all()
    counts = _read_counts(rows)
    records = tuple(
        _row_to_record(row._mapping) for row in rows if row.kind == _ROW_KIND
    )
    match_ids = [record.match_id for record in records]
    conflicts = _fold_conflicts(rows, query.per_page)
    counterpart_ids = sorted(
        {
            ref.match_id
            for people in conflicts.values()
            for person in people
            for ref in person.refs
        }
        - set(match_ids),
        key=str,
    )
    tournament_ids = sorted(
        {record.tournament_id for record in records}, key=str
    )
    episode_ids = [r.episode_id for r in records if r.episode_id is not None]
    choices, leaderboard_only = _query_tournaments(scope, query)

    return DashboardPageData(
        total_count=counts.total,
        view_total_count=counts.view_total,
        tier_counts=DashboardTierCounts(
            red=counts.red, yellow=counts.yellow, green=counts.green
        ),
        non_actionable_counts=DashboardNonActionableCounts(
            paused=counts.paused,
            pre_start=counts.pre_start,
            partial=counts.partial,
        ),
        records=records,
        contestants=_query_contestants([*match_ids, *counterpart_ids]),
        orga_user_ids=_query_orga_user_ids(tournament_ids),
        acknowledgements=_query_acknowledgements(episode_ids),
        tournament_choices=choices,
        leaderboard_only_tournaments=leaderboard_only,
        conflicts=conflicts,
    )


# -- the canonical relation --


def _identity(
    participant_id: InstrumentedAttribute[Any],
    team_id: InstrumentedAttribute[Any],
) -> ColumnElement[str]:
    """Return the text a contestant is told apart by, and matched on."""
    return case(
        (
            participant_id.is_not(None),
            literal('participant:', Text) + cast(participant_id, Text),
        ),
        else_=literal('team:', Text) + cast(team_id, Text),
    )


def _truthy(condition: ColumnElement[bool]) -> ColumnElement[bool]:
    """Turn an unknown (`NULL`) outcome into false, so `NOT` stays sound."""
    return func.coalesce(condition, false())


def _facts(party_id: PartyID) -> CTE:
    """Select every match of the party with the facts it is judged by.

    Matches whose phase runs no match format are no fixtures and stay
    out. The window columns look at all matches of a tournament, so
    nothing may be filtered away before them except whole tournaments.
    The facts cover every tournament of the party, whoever may see it:
    demand is judged over the party, and a list narrows to its scope
    only after the judgement.
    """
    m = DbTournamentMatch
    t = DbTournament
    c = DbTournamentMatchToContestant
    p = DbMatchPairing
    pin = DbMatchDashboardAnnotation
    ep = DbMatchDueEpisode
    ack = DbMatchEscalationAck
    losers = aliased(DbTournamentMatch)
    prior = aliased(DbMatchDueEpisode)

    party_tournament = aliased(DbTournament)

    contestants = (
        select(
            c.tournament_match_id.label('match_id'),
            func.count().label('n_real'),
            func.count(c.participant_id).label('n_participants'),
            func.count(c.team_id).label('n_teams'),
            func.min(_identity(c.participant_id, c.team_id)).label('id_min'),
            func.max(_identity(c.participant_id, c.team_id)).label('id_max'),
        )
        .join(m, m.id == c.tournament_match_id)
        .where(
            m.tournament_id.in_(
                select(party_tournament.id).where(
                    party_tournament.party_id == party_id
                )
            ),
            or_(c.participant_id.is_not(None), c.team_id.is_not(None)),
        )
        .group_by(c.tournament_match_id)
        .subquery('contestants')
    )

    latest_ack = (
        select(ack.clock_us.label('clock_us'))
        .where(ack.episode_id == ep.id)
        .order_by(ack.revision.desc())
        .limit(1)
        .lateral('latest_ack')
    )

    format_ = case(
        (m.phase == 1, t.game_format), (m.phase == 2, t.playoff_game_format)
    )
    mode = case(
        (m.phase == 1, t.elimination_mode),
        (m.phase == 2, t.playoff_elimination_mode),
    )
    bracket = case((m.bracket.in_(_BRACKETS), m.bracket))

    open_unconfirmed_round = case(
        (and_(m.confirmed_by.is_(None), m.round.is_not(None)), m.round)
    )
    merged_into_losers = exists().where(
        losers.tournament_id == m.tournament_id,
        losers.phase == m.phase,
        losers.bracket == _LOSERS,
        losers.seeding_target
        == literal(_WINNERS_TARGET_PREFIX, Text) + cast(m.round + 1, Text),
    )
    closed_episodes = and_(prior.match_id == m.id, prior.closed_at.is_not(None))

    return (
        select(
            m.id.label('match_id'),
            m.tournament_id.label('tournament_id'),
            t.name.label('tournament_name'),
            t.game.label('game'),
            t.tournament_status.label('t_status'),
            t.group_size_max.label('group_size_max'),
            t.operational_clock_elapsed_us.label('clock_elapsed_us'),
            t.operational_clock_running_since.label('clock_running_since'),
            t.operational_clock_activated_at.label('clock_activated_at'),
            m.phase.label('phase'),
            m.round.label('round'),
            m.group_order.label('group_order'),
            m.match_order.label('match_order'),
            m.confirmed_by.label('confirmed_by'),
            m.occupied_since.label('occupied_since'),
            m.created_at.label('created_at'),
            m.last_changed_at.label('last_changed_at'),
            m.ready_at_a.label('ready_at_a'),
            m.ready_at_b.label('ready_at_b'),
            m.pairing_generation.label('pairing_generation'),
            format_.label('format'),
            mode.label('mode'),
            bracket.label('bracket'),
            func.coalesce(contestants.c.n_real, 0).label('n_real'),
            func.coalesce(contestants.c.n_participants, 0).label(
                'n_participants'
            ),
            func.coalesce(contestants.c.n_teams, 0).label('n_teams'),
            contestants.c.id_min.label('id_min'),
            contestants.c.id_max.label('id_max'),
            p.id.label('pairing_row_id'),
            p.match_id.label('pairing_match_id'),
            p.tournament_id.label('pairing_tournament_id'),
            p.generation.label('pairing_row_generation'),
            p.ended_at.label('pairing_ended_at'),
            (p.side_a_kind + ':' + cast(p.side_a_id, Text)).label('side_a'),
            (p.side_b_kind + ':' + cast(p.side_b_id, Text)).label('side_b'),
            func.coalesce(pin.revision, 0).label('pin_revision'),
            pin.pinned_at.label('pinned_at'),
            pin.pinned_by.label('pinned_by'),
            ep.id.label('episode_id'),
            ep.opened_at.label('episode_opened_at'),
            ep.opened_clock_us.label('episode_opened_clock_us'),
            ep.ack_revision.label('episode_ack_revision'),
            latest_ack.c.clock_us.label('latest_ack_clock_us'),
            func.min(open_unconfirmed_round)
            .over(partition_by=[m.tournament_id, m.phase, m.group_order])
            .label('round_robin_frontier'),
            func.max(m.round)
            .over(partition_by=[m.tournament_id, m.phase, bracket])
            .label('pool_latest_round'),
            func.bool_or(case((bracket == _GRAND_FINAL, true()), else_=false()))
            .over(partition_by=[m.tournament_id, m.phase])
            .label('grand_final_exists'),
            merged_into_losers.label('merged_into_losers'),
            exists().where(closed_episodes).label('has_prior_episode'),
            exists()
            .where(closed_episodes, prior.pairing_key == ep.pairing_key)
            .label('reopened_same_pairing'),
            select(
                func.greatest(
                    prior.closed_clock_us - prior.opened_clock_us,
                    0,
                    type_=BigInteger,
                )
            )
            .where(closed_episodes)
            .order_by(prior.closed_at.desc(), prior.id.desc())
            .limit(1)
            .scalar_subquery()
            .label('closed_episode_wait_us'),
        )
        .select_from(m)
        .join(t, t.id == m.tournament_id)
        .outerjoin(contestants, contestants.c.match_id == m.id)
        .outerjoin(p, p.id == m.pairing_id)
        .outerjoin(pin, pin.match_id == m.id)
        .outerjoin(ep, and_(ep.match_id == m.id, ep.closed_at.is_(None)))
        .outerjoin(latest_ack, true())
        .where(
            t.party_id == party_id,
            format_.in_((_ONE_V_ONE, _FREE_FOR_ALL)),
        )
        .cte('party_facts')
    )


def _demand(facts: CTE, at: datetime) -> CTE:
    """Judge the facts: is a match due, has it a valid pair, what is the clock.

    This is the one due oracle of a snapshot. Every reader of demand
    (the lists, the conflicts) reads this very relation, which the
    database evaluates once.
    """
    f = facts.c

    knockout_due = and_(
        f.format == _ONE_V_ONE,
        f.mode.in_(_ELIMINATION_MODES),
        f.confirmed_by.is_(None),
        f.n_real == 2,
    )
    round_robin_due = and_(
        f.format == _ONE_V_ONE,
        f.mode == _ROUND_ROBIN,
        f.confirmed_by.is_(None),
        f.round.is_not(None),
        f.round == f.round_robin_frontier,
        f.n_real == 2,
    )
    free_for_all_due = and_(
        f.format == _FREE_FOR_ALL,
        f.mode.in_(_ELIMINATION_MODES),
        f.confirmed_by.is_(None),
        f.occupied_since.is_not(None),
        f.n_real >= 2,
        or_(
            f.bracket == _GRAND_FINAL,
            and_(
                f.round.is_not(None),
                f.round == f.pool_latest_round,
                not_(and_(f.bracket.is_not(None), f.grand_final_exists)),
                not_(
                    and_(_truthy(f.bracket == _WINNERS), f.merged_into_losers)
                ),
            ),
        ),
    )
    due = _truthy(
        and_(
            f.t_status.in_(_DEMAND_STATUSES),
            or_(knockout_due, round_robin_due, free_for_all_due),
        )
    )

    pairing_valid = _truthy(
        and_(
            f.format == _ONE_V_ONE,
            f.n_real == 2,
            or_(f.n_participants == 2, f.n_teams == 2),
            f.id_min != f.id_max,
            f.pairing_row_id.is_not(None),
            f.pairing_match_id == f.match_id,
            f.pairing_tournament_id == f.tournament_id,
            f.pairing_row_generation == f.pairing_generation,
            f.pairing_ended_at.is_(None),
            f.side_a != f.side_b,
            f.id_min == func.least(f.side_a, f.side_b),
            f.id_max == func.greatest(f.side_a, f.side_b),
        )
    )

    since = f.clock_running_since
    running_us = func.greatest(
        (extract('epoch', literal(at, DateTime()) - since) * 1_000_000).cast(
            BigInteger
        ),
        0,
    )
    clock_us = f.clock_elapsed_us + case((since.is_(None), 0), else_=running_us)

    return (
        select(
            *facts.c,
            due.label('due'),
            pairing_valid.label('pairing_valid'),
            clock_us.label('clock_us'),
            and_(
                f.clock_activated_at.is_not(None), f.episode_id.is_not(None)
            ).label('timing_known'),
        )
        .cte('party_demand')
        .prefix_with('MATERIALIZED')
    )


def _dash(demand: CTE, tournament_ids: tuple[TournamentID, ...]) -> CTE:
    """Classify the matches of the tournaments: state, waits, tier, readiness.

    This is where the party narrows to the scope, after the oracle has
    judged every fixture.
    """
    d = demand.c

    state = case(
        (d.confirmed_by.is_not(None), case((d.n_real < 2, _BYE), else_=_DONE)),
        (d.t_status.in_(_TERMINAL_STATUSES), _DONE),
        (
            and_(d.due, d.t_status == _RUNNING_STATUS),
            case((d.timing_known, _DUE), else_=_UNKNOWN),
        ),
        (d.due, _PAUSED),
        (
            d.format == _FREE_FOR_ALL,
            case(
                (
                    or_(d.occupied_since.is_(None), d.n_real < 2),
                    _AWAITING_LOBBY,
                ),
                else_=_UPCOMING,
            ),
        ),
        (d.n_real == 2, _UPCOMING),
        else_=_PARTIAL,
    )

    return (
        select(
            *demand.c,
            state.label('state'),
            case((d.pairing_valid, d.ready_at_a)).label('claim_at_a'),
            case((d.pairing_valid, d.ready_at_b)).label('claim_at_b'),
        )
        .where(d.tournament_id.in_(list(tournament_ids)))
        .cte('dashboard_state')
    )


def _party_demand(party_id: PartyID, at: datetime) -> CTE:
    """Judge every fixture of the party: the due oracle of one snapshot."""
    return _demand(_facts(party_id), at)


def _demand_users(demand: CTE) -> CTE:
    """Select who is demanded where, over every tournament of the party.

    A match demands people while it is due in a running tournament: not
    when it is merely coming, paused, confirmed or of a tournament that
    has not started. It demands the participant of a solo contestant
    and each active member of a team contestant. Removed participants
    and removed teams are nobody. Overlap is by user, not by team or by
    per-tournament participant, so a solo player and a team member, or
    members of different teams, are the same person.
    """
    d = demand.c
    c = DbTournamentMatchToContestant
    p = DbTournamentParticipant
    team = DbTournamentTeam
    running = and_(d.due, d.t_status == _RUNNING_STATUS)

    solo = (
        select(
            d.match_id.label('match_id'),
            p.user_id.label('user_id'),
            cast(null(), db.Uuid).label('team_id'),
        )
        .select_from(demand)
        .join(c, c.tournament_match_id == d.match_id)
        .join(
            p,
            and_(
                p.id == c.participant_id,
                p.tournament_id == d.tournament_id,
                p.removed_at.is_(None),
            ),
        )
        .where(running)
    )
    member = (
        select(
            d.match_id.label('match_id'),
            p.user_id.label('user_id'),
            team.id.label('team_id'),
        )
        .select_from(demand)
        .join(c, c.tournament_match_id == d.match_id)
        .join(
            team,
            and_(
                team.id == c.team_id,
                team.tournament_id == d.tournament_id,
                team.removed_at.is_(None),
            ),
        )
        .join(
            p,
            and_(
                p.team_id == team.id,
                p.tournament_id == team.tournament_id,
                p.removed_at.is_(None),
            ),
        )
        .where(running)
    )

    return union_all(solo, member).cte('demand_users')


def _conflicted_matches(demand_users: CTE) -> CTE:
    """Select the matches that demand a person who is demanded elsewhere."""
    u = demand_users.c
    shared = (
        select(u.user_id)
        .group_by(u.user_id)
        .having(func.count(distinct(u.match_id)) > 1)
    )
    return (
        select(u.match_id)
        .where(u.user_id.in_(shared))
        .distinct()
        .cte('conflicted_matches')
    )


def _timed(
    dash_state: CTE, settings: DashboardSettings, conflicted: CTE
) -> CTE:
    s = dash_state.c
    yellow_us = settings.yellow_minutes * _MICROSECONDS_PER_MINUTE
    red_us = settings.red_minutes * _MICROSECONDS_PER_MINUTE

    timed = and_(s.state.in_((_DUE, _PAUSED)), s.timing_known)
    total_wait = case(
        (
            timed,
            func.greatest(
                s.clock_us - s.episode_opened_clock_us, 0, type_=BigInteger
            ),
        )
    )
    alert = case(
        (
            timed,
            func.greatest(
                s.clock_us
                - func.coalesce(
                    s.latest_ack_clock_us, s.episode_opened_clock_us
                ),
                0,
                type_=BigInteger,
            ),
        )
    )
    tier = case(
        (
            s.state == _DUE,
            case(
                (alert >= red_us, _TIER_RED),
                (alert >= yellow_us, _TIER_YELLOW),
                else_=_TIER_GREEN,
            ),
        )
    )
    ready_count = case((s.claim_at_a.is_not(None), 1), else_=0) + case(
        (s.claim_at_b.is_not(None), 1), else_=0
    )
    rank = case(
        (tier == _TIER_RED, 0),
        (tier == _TIER_YELLOW, 1),
        (tier == _TIER_GREEN, 2),
        (s.state.in_((_PAUSED, _UNKNOWN)), 3),
        else_=4,
    )
    bracket_rank = case(
        (s.bracket == _WINNERS, 1),
        (s.bracket == _LOSERS, 2),
        (s.bracket == _GRAND_FINAL, 3),
        (s.bracket.is_not(None), 4),
        else_=0,
    )
    # A person of a due match is demanded in another due match of the
    # party, whoever may see that other match.
    has_conflict = and_(
        s.state.in_((_DUE, _UNKNOWN)),
        s.match_id.in_(select(conflicted.c.match_id)),
    )

    return select(
        *s,
        total_wait.label('total_wait_us'),
        alert.label('alert_us'),
        tier.label('tier'),
        ready_count.label('ready_count'),
        rank.label('urgency_rank'),
        bracket_rank.label('bracket_rank'),
        has_conflict.label('has_conflict'),
        func.lower(s.tournament_name, type_=Text).label('tournament_sort_name'),
    ).cte('dashboard')


def _relation(
    scope: DashboardScope,
    settings: DashboardSettings,
    demand: CTE,
    demand_users: CTE,
) -> CTE:
    """Return the canonical relation of the scope's matches.

    Pass the oracle `demand` and the `demand_users` made from it: the
    lists and the conflicts must read one judgement of demand.
    """
    return _timed(
        _dash(demand, scope.tournament_ids),
        settings,
        _conflicted_matches(demand_users),
    )


# -- filters and order --


def _view_predicate(relation: CTE, view: DashboardView) -> ColumnElement[bool]:
    return relation.c.state.in_(_VIEW_STATES[view])


def _tournament_predicate(
    relation: CTE, tournament_id: UUID | None
) -> ColumnElement[bool]:
    if tournament_id is None:
        return true()

    return relation.c.tournament_id == tournament_id


_STATE_PREDICATES: dict[
    DashboardState, Callable[[CTE], ColumnElement[bool]]
] = {
    'all': lambda r: true(),
    'tier-red': lambda r: r.c.tier == _TIER_RED,
    'tier-yellow': lambda r: r.c.tier == _TIER_YELLOW,
    'tier-green': lambda r: r.c.tier == _TIER_GREEN,
    'ready-none': lambda r: and_(r.c.pairing_valid, r.c.ready_count == 0),
    'ready-one': lambda r: and_(r.c.pairing_valid, r.c.ready_count == 1),
    'ready-both': lambda r: and_(r.c.pairing_valid, r.c.ready_count == 2),
    'ready-unavailable': lambda r: not_(r.c.pairing_valid),
    'conflict': lambda r: r.c.has_conflict,
    # No review feature is installed, so no match has an open review.
    'review-open': lambda r: false(),
    'pinned': lambda r: r.c.pinned_at.is_not(None),
}


def _selection(relation: CTE, query: DashboardQuery) -> ColumnElement[bool]:
    """Return what the requested list consists of: view, tournament, state.

    The total and the rows are cut from this one predicate.
    """
    return and_(
        _view_predicate(relation, query.view),
        _tournament_predicate(relation, _as_uuid(query.tournament_id)),
        _STATE_PREDICATES[query.state](relation),
    )


def _match_label_order(r: CTE) -> list[ColumnElement]:
    """Order matches of one tournament by what their label reads."""
    return [
        r.c.phase.asc(),
        r.c.group_order.asc().nulls_first(),
        r.c.bracket_rank.asc(),
        r.c.round.asc().nulls_last(),
        r.c.match_order.asc().nulls_last(),
    ]


def _sort_order(r: CTE, sort: str) -> list[ColumnElement]:
    by_tournament = [
        r.c.tournament_sort_name.asc(),
        r.c.tournament_id.asc(),
        *_match_label_order(r),
    ]
    if sort == 'urgency':
        keys = [
            r.c.urgency_rank.asc(),
            r.c.has_conflict.desc(),
            r.c.alert_us.desc().nulls_last(),
            r.c.total_wait_us.desc().nulls_last(),
            *by_tournament,
        ]
    elif sort == 'wait':
        keys = [r.c.total_wait_us.desc().nulls_last(), *by_tournament]
    elif sort == 'tournament':
        keys = by_tournament
    else:
        raise ValueError(f'Unknown dashboard sort "{sort}"')

    return [*keys, r.c.match_id.asc()]


# -- the statements --


@dataclass(frozen=True, kw_only=True)
class _Counts:
    total: int
    view_total: int
    red: int
    yellow: int
    green: int
    paused: int
    pre_start: int
    partial: int


def _counts_statement(relation: CTE, query: DashboardQuery) -> Select:
    r = relation
    in_view = _view_predicate(r, query.view)
    selected = _selection(r, query)
    waiting = r.c.state.in_(_NOT_DUE_STATES)
    pre_start = _truthy(r.c.t_status.in_(_PRE_START_STATUSES))

    return select(
        func.count().filter(selected).label('total'),
        func.count().filter(in_view).label('view_total'),
        func.count().filter(r.c.tier == _TIER_RED).label('red'),
        func.count().filter(r.c.tier == _TIER_YELLOW).label('yellow'),
        func.count().filter(r.c.tier == _TIER_GREEN).label('green'),
        func.count().filter(r.c.state == _PAUSED).label('paused'),
        func.count().filter(and_(waiting, pre_start)).label('pre_start'),
        func.count().filter(and_(waiting, not_(pre_start))).label('partial'),
    ).select_from(r)


def _read_counts(rows: Sequence[Row]) -> _Counts:
    (row,) = (row for row in rows if row.kind == _COUNTS_KIND)
    return _Counts(
        total=row.total,
        view_total=row.view_total,
        red=row.red,
        yellow=row.yellow,
        green=row.green,
        paused=row.paused,
        pre_start=row.pre_start,
        partial=row.partial,
    )


_RECORD_COLUMNS = (
    'match_id',
    'tournament_id',
    'tournament_name',
    'game',
    'format',
    'mode',
    'phase',
    'bracket',
    'round',
    'group_order',
    'match_order',
    'state',
    'confirmed_by',
    'n_real',
    'created_at',
    'occupied_since',
    'last_changed_at',
    'episode_id',
    'episode_opened_at',
    'episode_ack_revision',
    'has_prior_episode',
    'reopened_same_pairing',
    'total_wait_us',
    'alert_us',
    'closed_episode_wait_us',
    'pairing_valid',
    'claim_at_a',
    'claim_at_b',
    'side_a',
    'pin_revision',
    'pinned_at',
    'pinned_by',
    'round_robin_frontier',
    'group_size_max',
    'has_conflict',
)


def _rows_statement(relation: CTE, query: DashboardQuery) -> Select:
    r = relation
    order = _sort_order(r, query.sort)
    return (
        select(
            *(r.c[name] for name in _RECORD_COLUMNS),
            # What the order of the page reads back as.
            func.row_number(type_=BigInteger)
            .over(order_by=order)
            .label('row_position'),
        )
        .where(_selection(r, query))
        .order_by(*order)
        .offset((query.page - 1) * query.per_page)
        .limit(query.per_page)
    )


def _row_to_record(row: Mapping) -> DashboardMatchRecord:
    state = DashboardRowState(row['state'])
    bracket = row['bracket']
    format_ = GameFormat[row['format']]
    has_episode = row['episode_id'] is not None
    demand = state in (
        DashboardRowState.DUE,
        DashboardRowState.PAUSED,
        DashboardRowState.UNKNOWN,
    )
    frontier = row['round_robin_frontier']
    earlier_open_round = (
        frontier
        if (
            state is DashboardRowState.UPCOMING
            and row['mode'] == _ROUND_ROBIN
            and frontier is not None
            and row['round'] is not None
            and row['round'] > frontier
        )
        else None
    )
    closed = state in (DashboardRowState.DONE, DashboardRowState.BYE)

    return DashboardMatchRecord(
        match_id=row['match_id'],
        tournament_id=row['tournament_id'],
        tournament_name=row['tournament_name'],
        game=row['game'],
        game_format=format_,
        elimination_mode=(
            EliminationMode[row['mode']] if row['mode'] is not None else None
        ),
        location=DashboardMatchLocation(
            phase=row['phase'],
            bracket=Bracket(bracket) if bracket is not None else None,
            round=row['round'],
            group_order=row['group_order'],
            match_order=row['match_order'],
        ),
        state=state,
        confirmed=row['confirmed_by'] is not None,
        contestant_count=row['n_real'],
        created_at=row['created_at'],
        occupied_since=row['occupied_since'],
        last_changed_at=row['last_changed_at'],
        episode_id=row['episode_id'] if demand else None,
        episode_opened_at=row['episode_opened_at'] if demand else None,
        ack_revision=(row['episode_ack_revision'] or 0) if demand else 0,
        has_prior_episode=bool(row['has_prior_episode']),
        reopened_same_pairing=bool(
            demand and has_episode and row['reopened_same_pairing']
        ),
        total_active_wait_us=row['total_wait_us'],
        alert_interval_us=row['alert_us'],
        closed_episode_wait_us=row['closed_episode_wait_us']
        if closed
        else None,
        readiness_available=bool(row['pairing_valid']),
        ready_at_a=row['claim_at_a'],
        ready_at_b=row['claim_at_b'],
        side_a_identity=row['side_a'] if row['pairing_valid'] else None,
        pin_revision=row['pin_revision'],
        pinned_at=row['pinned_at'],
        pinned_by=row['pinned_by'],
        earlier_open_round=earlier_open_round,
        lobby_size=(
            row['group_size_max']
            if state is DashboardRowState.AWAITING_LOBBY
            else None
        ),
        has_conflict=bool(row['has_conflict']),
    )


def _conflicts_statement(
    relation: CTE,
    demand_users: CTE,
    query: DashboardQuery,
    match_ids: Sequence[TournamentMatchID] | Select,
) -> Select:
    """Select who is demanded elsewhere too, for the given listed matches.

    A row is one person of a listed match and one other match that
    demands them. The other match is described by `relation`, which
    holds only the scope's matches, so a match outside the scope joins
    to nothing. Such rows are all `NULL` except for the listed side and
    fall into one by `DISTINCT`: neither identity nor number of the
    hidden matches leaves the database.
    """
    mine = demand_users.alias('mine')
    other = demand_users.alias('other')
    mine_team = aliased(DbTournamentTeam)
    other_team = aliased(DbTournamentTeam)
    # The position in the very list the page is cut from.
    listed = (
        select(
            relation.c.match_id,
            func.row_number(type_=BigInteger)
            .over(order_by=_sort_order(relation, query.sort))
            .label('position'),
        )
        .where(_selection(relation, query))
        .cte('listed_matches')
        .prefix_with('MATERIALIZED')
    )
    # Only a match that demands people can be a counterpart, so the
    # relation is narrowed to those once, not once per person.
    counterparts = (
        select(
            relation.c.match_id,
            relation.c.tournament_id,
            relation.c.tournament_name,
            relation.c.tournament_sort_name,
            relation.c.phase,
            relation.c.bracket,
            relation.c.bracket_rank,
            relation.c.round,
            relation.c.group_order,
            relation.c.match_order,
            relation.c.format,
            case((relation.c.pairing_valid, relation.c.side_a)).label('side_a'),
            listed.c.position,
        )
        .select_from(relation)
        .outerjoin(listed, listed.c.match_id == relation.c.match_id)
        .where(relation.c.match_id.in_(select(demand_users.c.match_id)))
        .cte('counterparts')
        .prefix_with('MATERIALIZED')
    )
    r = counterparts.alias('counterpart')

    return (
        select(
            mine.c.match_id.label('match_id'),
            mine.c.user_id.label('user_id'),
            mine_team.name.label('via_team_name'),
            r.c.match_id.label('other_match_id'),
            r.c.tournament_id.label('other_tournament_id'),
            r.c.tournament_name.label('other_tournament_name'),
            r.c.tournament_sort_name.label('other_tournament_sort_name'),
            r.c.phase.label('other_phase'),
            r.c.bracket.label('other_bracket'),
            r.c.bracket_rank.label('other_bracket_rank'),
            r.c.round.label('other_round'),
            r.c.group_order.label('other_group_order'),
            r.c.match_order.label('other_match_order'),
            r.c.format.label('other_format'),
            r.c.side_a.label('other_side_a'),
            other_team.name.label('other_via_team_name'),
            r.c.position.label('position'),
        )
        .select_from(mine)
        .join(
            other,
            and_(
                other.c.user_id == mine.c.user_id,
                other.c.match_id != mine.c.match_id,
            ),
        )
        .outerjoin(r, r.c.match_id == other.c.match_id)
        .outerjoin(mine_team, mine_team.id == mine.c.team_id)
        .outerjoin(
            other_team,
            and_(
                other_team.id == other.c.team_id,
                r.c.match_id.is_not(None),
            ),
        )
        .where(mine.c.match_id.in_(match_ids))
        .distinct()
        .order_by(
            mine.c.match_id,
            mine.c.user_id,
            r.c.tournament_sort_name.asc().nulls_last(),
            r.c.tournament_id.asc().nulls_last(),
            *_match_label_order(r),
            r.c.match_id.asc().nulls_last(),
        )
    )


def _list_page(position: int | None, per_page: int) -> int | None:
    """Return the page of a 1-based position in the list, if it is in it."""
    if position is None:
        return None

    return (position - 1) // per_page + 1


def _stacked(parts: Mapping[str, Select]) -> CompoundSelect:
    """Stack selects of different shapes into one result.

    Every row names the `kind` of its part. A column that its part does
    not have is `NULL`, so a column name means one thing in all parts.
    """
    subqueries = {kind: part.subquery() for kind, part in parts.items()}
    layout: dict[str, TypeEngine] = {}
    for subquery in subqueries.values():
        for column in subquery.c:
            known = layout.setdefault(column.name, column.type)
            if type(known) is not type(column.type):
                raise ValueError(
                    f'The parts disagree on the type of "{column.name}".'
                )

    return union_all(
        *(
            select(
                literal(kind, Text).label('kind'),
                *(
                    subquery.c[name]
                    if name in subquery.c
                    else cast(null(), type_).label(name)
                    for name, type_ in layout.items()
                ),
            )
            for kind, subquery in subqueries.items()
        )
    )


def _page_statement(
    relation: CTE, demand_users: CTE, query: DashboardQuery
) -> CompoundSelect:
    """Select the counts, the rows and the conflicts of one page.

    One statement reads all three from the same oracle, so the party is
    judged once however many listed matches are in conflict, and the
    matches that only the oracle knows never leave the database. The
    conflicts are those of the listed rows that are in conflict.
    """
    page = _rows_statement(relation, query).cte('page_rows')
    conflicted = select(page.c.match_id).where(page.c.has_conflict)
    stacked = _stacked(
        {
            _COUNTS_KIND: _counts_statement(relation, query),
            _ROW_KIND: select(page),
            _CONFLICT_KIND: _conflicts_statement(
                relation, demand_users, query, conflicted
            ),
        }
    )
    c = stacked.selected_columns
    return stacked.order_by(
        c.kind,
        c.row_position,
        c.match_id,
        c.user_id,
        c.other_tournament_sort_name.asc().nulls_last(),
        c.other_tournament_id.asc().nulls_last(),
        c.other_phase.asc(),
        c.other_group_order.asc().nulls_first(),
        c.other_bracket_rank.asc(),
        c.other_round.asc().nulls_last(),
        c.other_match_order.asc().nulls_last(),
        c.other_match_id.asc().nulls_last(),
    )


def _fold_conflicts(
    rows: Sequence[Row], per_page: int
) -> dict[TournamentMatchID, tuple[DashboardConflictRecord, ...]]:
    """Return the people of the listed matches who are demanded elsewhere."""
    team_names: dict[tuple, str | None] = {}
    refs: dict[tuple, list[DashboardConflictRefRecord]] = {}
    external: set[tuple] = set()
    for row in rows:
        if row.kind != _CONFLICT_KIND:
            continue

        key = (row.match_id, row.user_id)
        team_names[key] = row.via_team_name
        key_refs = refs.setdefault(key, [])
        if row.other_match_id is None:
            external.add(key)
            continue

        key_refs.append(
            DashboardConflictRefRecord(
                match_id=row.other_match_id,
                tournament_id=row.other_tournament_id,
                tournament_name=row.other_tournament_name,
                location=DashboardMatchLocation(
                    phase=row.other_phase,
                    bracket=(
                        Bracket(row.other_bracket)
                        if row.other_bracket is not None
                        else None
                    ),
                    round=row.other_round,
                    group_order=row.other_group_order,
                    match_order=row.other_match_order,
                ),
                side_a_identity=row.other_side_a,
                via_team_name=row.other_via_team_name,
                list_page=_list_page(row.position, per_page),
                game_format=(
                    GameFormat[row.other_format]
                    if row.other_format is not None
                    else None
                ),
            )
        )

    grouped: dict[TournamentMatchID, list[DashboardConflictRecord]] = (
        defaultdict(list)
    )
    for key, key_refs in refs.items():
        match_id, user_id = key
        grouped[match_id].append(
            DashboardConflictRecord(
                match_id=match_id,
                user_id=user_id,
                via_team_name=team_names[key],
                refs=tuple(key_refs),
                has_external_conflict=key in external,
            )
        )

    return {key: tuple(values) for key, values in grouped.items()}


def _query_contestants(
    match_ids: list[TournamentMatchID],
) -> dict[TournamentMatchID, tuple[DashboardContestantRecord, ...]]:
    if not match_ids:
        return {}

    c = DbTournamentMatchToContestant
    participant = DbTournamentParticipant
    team = DbTournamentTeam
    rows = db.session.execute(
        select(
            c.tournament_match_id,
            c.participant_id,
            c.team_id,
            c.score,
            participant.user_id,
            team.name,
        )
        .outerjoin(participant, participant.id == c.participant_id)
        .outerjoin(team, team.id == c.team_id)
        .where(
            c.tournament_match_id.in_(match_ids),
            or_(c.participant_id.is_not(None), c.team_id.is_not(None)),
        )
        .order_by(c.tournament_match_id, c.created_at, c.id)
    ).all()

    grouped: dict[TournamentMatchID, list[DashboardContestantRecord]] = (
        defaultdict(list)
    )
    for match_id, participant_id, team_id, score, user_id, team_name in rows:
        identity = (
            f'participant:{participant_id}'
            if participant_id is not None
            else f'team:{team_id}'
        )
        grouped[match_id].append(
            DashboardContestantRecord(
                match_id=match_id,
                identity=identity,
                participant_id=participant_id,
                team_id=team_id,
                user_id=user_id,
                team_name=team_name,
                score=score,
            )
        )

    return {match_id: tuple(records) for match_id, records in grouped.items()}


def _query_orga_user_ids(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, tuple[UserID, ...]]:
    if not tournament_ids:
        return {}

    rows = db.session.execute(
        select(DbTournamentOrga.tournament_id, DbTournamentOrga.user_id)
        .where(DbTournamentOrga.tournament_id.in_(tournament_ids))
        .order_by(
            DbTournamentOrga.tournament_id,
            DbTournamentOrga.assigned_at,
            DbTournamentOrga.id,
        )
    ).all()

    grouped: dict[TournamentID, list[UserID]] = defaultdict(list)
    for tournament_id, user_id in rows:
        grouped[tournament_id].append(user_id)

    return {key: tuple(user_ids) for key, user_ids in grouped.items()}


def _query_acknowledgements(
    episode_ids: list[MatchDueEpisodeID],
) -> dict[MatchDueEpisodeID, tuple[DashboardAcknowledgementRecord, ...]]:
    """Return the newest acknowledgements of each episode, newest first."""
    if not episode_ids:
        return {}

    ack = DbMatchEscalationAck
    ranked = (
        select(
            ack.id,
            ack.episode_id,
            ack.revision,
            ack.actor_id,
            ack.occurred_at,
            ack.comment,
            func.row_number()
            .over(partition_by=ack.episode_id, order_by=ack.revision.desc())
            .label('position'),
            func.count()
            .over(partition_by=ack.episode_id)
            .label('episode_count'),
        )
        .where(ack.episode_id.in_(episode_ids))
        .subquery('ranked')
    )
    rows = db.session.execute(
        select(ranked)
        .where(ranked.c.position <= _ACK_HISTORY_LENGTH)
        .order_by(ranked.c.episode_id, ranked.c.position)
    ).all()

    grouped: dict[MatchDueEpisodeID, list[DashboardAcknowledgementRecord]] = (
        defaultdict(list)
    )
    for row in rows:
        grouped[row.episode_id].append(
            DashboardAcknowledgementRecord(
                id=row.id,
                episode_id=row.episode_id,
                revision=row.revision,
                actor_id=row.actor_id,
                occurred_at=row.occurred_at,
                comment=row.comment,
                episode_count=row.episode_count,
            )
        )

    return {key: tuple(records) for key, records in grouped.items()}


def _query_tournaments(
    scope: DashboardScope, query: DashboardQuery
) -> tuple[
    tuple[DashboardTournamentRef, ...], tuple[DashboardTournamentRef, ...]
]:
    """Return the filter choices and the leaderboard-only tournaments.

    A leaderboard-only tournament has no matches. Its note card belongs
    to the unfiltered list of every match.
    """
    t = DbTournament
    rows = db.session.execute(
        select(t.id, t.name, t.game, t.game_format)
        .where(
            t.party_id == scope.party_id,
            t.id.in_(list(scope.tournament_ids)),
        )
        .order_by(func.lower(t.name), t.id)
    ).all()

    choices = tuple(
        DashboardTournamentRef(
            tournament_id=row.id, name=row.name, game=row.game
        )
        for row in rows
    )
    show_cards = query.view == 'all' and query.state == 'all'
    filter_id = _as_uuid(query.tournament_id)
    leaderboard_only = tuple(
        DashboardTournamentRef(
            tournament_id=row.id, name=row.name, game=row.game
        )
        for row in rows
        if show_cards
        and row.game_format == _HIGHSCORE
        and (filter_id is None or row.id == filter_id)
    )
    return choices, leaderboard_only


def _as_uuid(value: object) -> UUID | None:
    """Coerce an ID that may be a plain string at runtime."""
    if value is None:
        return None

    return value if isinstance(value, UUID) else UUID(str(value))
