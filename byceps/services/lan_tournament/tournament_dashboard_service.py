"""
byceps.services.lan_tournament.tournament_dashboard_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The read side of the orga dashboard: who may see what, and one page of it.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from math import ceil
from uuid import UUID

from flask_babel import gettext

from byceps.services.authn.session.models import CurrentUser
from byceps.services.party.models import PartyID
from byceps.services.user import user_service
from byceps.services.user.models import User, UserID
from byceps.util.authz import get_permissions_for_user
from byceps.util.result import Err, Ok, Result

from . import (
    tournament_dashboard_repository as dashboard_repository,
    tournament_repository,
)
from .dashboard_config import MAX_PAGE_SIZE
from .models.game_format import GameFormat
from .models.operational_timing import TrafficTier
from .models.tournament_dashboard import (
    AckUnavailableReason,
    DASHBOARD_SCOPES,
    DASHBOARD_SORTS,
    DASHBOARD_STATES,
    DASHBOARD_VIEWS,
    DashboardAcknowledgementSummary,
    DashboardConflict,
    DashboardConflictRef,
    DashboardEmptyReason,
    DashboardPage,
    DashboardQuery,
    DashboardRow,
    DashboardRowState,
    DashboardScope,
    DashboardSettings,
    DashboardStatusNote,
)
from .tournament_dashboard_repository import (
    DashboardAcknowledgementRecord,
    DashboardConflictRefRecord,
    DashboardContestantRecord,
    DashboardMatchRecord,
    DashboardPageData,
    MAX_PAGE_NUMBER,
)
from .tournament_operational_domain_service import (
    derive_traffic_tier,
    normalize_utc,
)


ADMINISTRATE_PERMISSION = 'lan_tournament.administrate'

DASHBOARD_UNAUTHENTICATED_ERROR = 'dashboard_unauthenticated'
DASHBOARD_FORBIDDEN_ERROR = 'dashboard_forbidden'
DASHBOARD_QUERY_INVALID_ERROR = 'dashboard_query_invalid'

# The vocabulary of `DashboardStatusNote.code`, with its parameters.
STATUS_NOTE_EARLIER_ROUND_OPEN = 'earlier_round_open'  # round
STATUS_NOTE_BYE_ADVANCE = 'bye_advance'
# `after_round` is the stored (zero-based) round the lobby waits for.
STATUS_NOTE_LOBBY_WAITING = 'lobby_waiting'  # filled, [size], [after_round]
STATUS_NOTE_RESULT_CONFIRMED = 'result_confirmed'  # score_a, score_b
STATUS_NOTE_CORRECTED_REOPENED = 'corrected_reopened'

_DEMAND_STATES = (
    DashboardRowState.DUE,
    DashboardRowState.PAUSED,
    DashboardRowState.UNKNOWN,
)


def resolve_dashboard_scope(
    viewer: CurrentUser, party_id: PartyID, requested_scope: str
) -> Result[DashboardScope, str]:
    """Return what the viewer may see of the party's dashboard, right now.

    The decision reads the viewer's permissions and assignments afresh
    and never trusts what the request or the session carried. Only a
    global administrator may widen the scope to every tournament of the
    party. A scoped orga always gets their own tournaments, whatever
    they ask for. Anybody else, including an identity that may only
    view the tournaments in the backend, gets no dashboard at all.
    """
    if not viewer.authenticated:
        return Err(DASHBOARD_UNAUTHENTICATED_ERROR)

    is_global_admin = ADMINISTRATE_PERMISSION in get_permissions_for_user(
        viewer.id
    )
    widen = is_global_admin and requested_scope == 'all'

    tournament_ids = dashboard_repository.get_dashboard_tournament_ids(
        party_id, viewer.id, include_all=widen
    )
    if not is_global_admin and not tournament_ids:
        return Err(DASHBOARD_FORBIDDEN_ERROR)

    return Ok(
        DashboardScope(
            user_id=viewer.id,
            party_id=party_id,
            kind='all' if widen else 'assigned',
            tournament_ids=tournament_ids,
            is_global_admin=is_global_admin,
        )
    )


def get_dashboard_page(
    viewer: CurrentUser,
    party_id: PartyID,
    query: DashboardQuery,
    *,
    settings: DashboardSettings,
    now: datetime | None = None,
) -> Result[DashboardPage, str]:
    """Return one consistent page of the dashboard for the viewer.

    The scope, the counts, the rows and everything that names them are
    read in one read-only `REPEATABLE READ` transaction, so they show a
    single moment of the database. Nothing is written. The session's
    transaction is rolled back before and after, so call it outside of a
    write transaction.

    Leave `now` out. The service then reads the server time right at
    the start of the snapshot, which no commit visible in it can
    outlast. A given `now` is for tests and replays.
    """
    invalid = _check_query(query)
    if invalid is not None:
        return Err(invalid)

    with dashboard_repository.read_snapshot():
        as_of = (
            tournament_repository.get_operation_time()
            if now is None
            else normalize_utc(now)
        )

        scope_result = resolve_dashboard_scope(viewer, party_id, query.scope)
        if scope_result.is_err():
            return Err(scope_result.unwrap_err())
        scope = scope_result.unwrap()

        data = dashboard_repository.query_dashboard_matches(
            scope, query, now=as_of, settings=settings
        )
        users = user_service.get_users_indexed_by_id(_user_ids_of(data))

    return Ok(_build_page(scope, query, data, users, as_of, settings))


def _check_query(query: DashboardQuery) -> str | None:
    """Return the error if the query was not validated by its parser."""
    if (
        query.scope not in DASHBOARD_SCOPES
        or query.view not in DASHBOARD_VIEWS
        or query.state not in DASHBOARD_STATES
        or query.sort not in DASHBOARD_SORTS
    ):
        return DASHBOARD_QUERY_INVALID_ERROR

    if not _is_int(query.page) or not 1 <= query.page <= MAX_PAGE_NUMBER:
        return DASHBOARD_QUERY_INVALID_ERROR

    if not _is_int(query.per_page) or not 1 <= query.per_page <= MAX_PAGE_SIZE:
        return DASHBOARD_QUERY_INVALID_ERROR

    if query.tournament_id is not None:
        try:
            UUID(str(query.tournament_id))
        except ValueError:
            return DASHBOARD_QUERY_INVALID_ERROR

    return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _user_ids_of(data: DashboardPageData) -> set[UserID]:
    user_ids: set[UserID] = set()
    for contestants in data.contestants.values():
        user_ids.update(c.user_id for c in contestants if c.user_id is not None)
    for orga_ids in data.orga_user_ids.values():
        user_ids.update(orga_ids)
    for acknowledgements in data.acknowledgements.values():
        user_ids.update(a.actor_id for a in acknowledgements)
    user_ids.update(
        r.pinned_by for r in data.records if r.pinned_by is not None
    )
    for conflicts in data.conflicts.values():
        user_ids.update(conflict.user_id for conflict in conflicts)
    return user_ids


def _build_page(
    scope: DashboardScope,
    query: DashboardQuery,
    data: DashboardPageData,
    users: Mapping[UserID, User],
    as_of: datetime,
    settings: DashboardSettings,
) -> DashboardPage:
    rows = tuple(
        _build_row(record, data, users, settings) for record in data.records
    )

    return DashboardPage(
        rows=rows,
        as_of=as_of,
        total_count=data.total_count,
        page=query.page,
        per_page=query.per_page,
        total_pages=ceil(data.total_count / query.per_page),
        tier_counts=data.tier_counts,
        non_actionable_counts=data.non_actionable_counts,
        leaderboard_only_tournaments=data.leaderboard_only_tournaments,
        tournament_choices=data.tournament_choices,
        empty_reason=_empty_reason(scope, query, data),
    )


def _empty_reason(
    scope: DashboardScope, query: DashboardQuery, data: DashboardPageData
) -> DashboardEmptyReason | None:
    """Tell why the result is empty. A page beyond the last is not empty."""
    if data.total_count > 0:
        return None

    if not scope.tournament_ids:
        return 'no_assignment'

    filtered = query.tournament_id is not None or query.state != 'all'
    if filtered and data.view_total_count > 0:
        return 'no_filter_matches'

    counts = data.non_actionable_counts
    if (
        query.view == 'due'
        and counts.paused + counts.pre_start + counts.partial
    ):
        return 'no_actionable_fixtures'

    return 'no_current_demand'


def _build_row(
    record: DashboardMatchRecord,
    data: DashboardPageData,
    users: Mapping[UserID, User],
    settings: DashboardSettings,
) -> DashboardRow:
    contestants = _in_side_order(
        data.contestants.get(record.match_id, ()), record.side_a_identity
    )
    acknowledgements = [
        DashboardAcknowledgementSummary(
            id=ack.id,
            revision=ack.revision,
            actor_display_name=_orga_name(users.get(ack.actor_id)),
            occurred_at=ack.occurred_at,
            comment=ack.comment,
        )
        for ack in _acknowledgements_of(record, data)
    ]
    tier = (
        derive_traffic_tier(record.alert_interval_us, settings)
        if record.state is DashboardRowState.DUE
        and record.alert_interval_us is not None
        else None
    )

    return DashboardRow(
        match_id=record.match_id,
        tournament_id=record.tournament_id,
        tournament_name=record.tournament_name,
        game=record.game,
        game_format=record.game_format,
        elimination_mode=record.elimination_mode,
        location=record.location,
        contestant_names=tuple(_contestant_name(c, users) for c in contestants),
        orga_names=_orga_names(
            data.orga_user_ids.get(record.tournament_id, ()), users
        ),
        state=record.state,
        tier=tier,
        status_note=_status_note(record, contestants),
        created_at=record.created_at,
        occupied_since=record.occupied_since,
        episode_opened_at=record.episode_opened_at,
        has_prior_episode=record.has_prior_episode
        and record.episode_id is not None,
        total_active_wait_us=record.total_active_wait_us,
        alert_interval_us=record.alert_interval_us,
        closed_episode_wait_us=record.closed_episode_wait_us,
        last_changed_at=record.last_changed_at,
        readiness_available=record.readiness_available,
        ready_at_a=record.ready_at_a,
        ready_at_b=record.ready_at_b,
        pin_revision=record.pin_revision,
        pinned_at=record.pinned_at,
        pinned_by_name=(
            _orga_name(users.get(record.pinned_by))
            if record.pinned_by is not None
            else None
        ),
        episode_id=record.episode_id,
        ack_revision=record.ack_revision,
        ack_unavailable_reason=_ack_unavailable_reason(
            record, tier, bool(acknowledgements)
        ),
        latest_acknowledgement=acknowledgements[0]
        if acknowledgements
        else None,
        recent_acknowledgements=tuple(acknowledgements),
        acknowledgement_count=_acknowledgement_count(record, data),
        conflicts=_build_conflicts(record, data, users),
    )


def _build_conflicts(
    record: DashboardMatchRecord,
    data: DashboardPageData,
    users: Mapping[UserID, User],
) -> tuple[DashboardConflict, ...]:
    """Return the people of the match who are demanded elsewhere too.

    Only what the repository judged visible reaches a reference. Demand
    elsewhere is the boolean of the person, with no further field.
    """
    conflicts = [
        DashboardConflict(
            user_id=conflict.user_id,
            user_display_name=_user_name(users.get(conflict.user_id)),
            via_team_name=conflict.via_team_name,
            visible_refs=tuple(
                _build_conflict_ref(ref, data, users) for ref in conflict.refs
            ),
            has_external_conflict=conflict.has_external_conflict,
        )
        for conflict in data.conflicts.get(record.match_id, ())
    ]
    conflicts.sort(key=lambda c: (c.user_display_name.casefold(), c.user_id))
    return tuple(conflicts)


def _build_conflict_ref(
    ref: DashboardConflictRefRecord,
    data: DashboardPageData,
    users: Mapping[UserID, User],
) -> DashboardConflictRef:
    contestants = _in_side_order(
        data.contestants.get(ref.match_id, ()), ref.side_a_identity
    )

    return DashboardConflictRef(
        match_id=ref.match_id,
        tournament_id=ref.tournament_id,
        tournament_name=ref.tournament_name,
        location=ref.location,
        game_format=ref.game_format,
        contestant_names=tuple(_contestant_name(c, users) for c in contestants),
        via_team_name=ref.via_team_name,
        list_page=ref.list_page,
    )


def _acknowledgements_of(
    record: DashboardMatchRecord, data: DashboardPageData
) -> Sequence[DashboardAcknowledgementRecord]:
    if record.episode_id is None:
        return ()

    return data.acknowledgements.get(record.episode_id, ())


def _acknowledgement_count(
    record: DashboardMatchRecord, data: DashboardPageData
) -> int:
    acknowledgements = _acknowledgements_of(record, data)
    return acknowledgements[0].episode_count if acknowledgements else 0


def _ack_unavailable_reason(
    record: DashboardMatchRecord, tier: TrafficTier | None, has_ack: bool
) -> AckUnavailableReason | None:
    """Return why a row offers no acknowledgement, or `None` if it does.

    It is offered for a running, due match with a known clock whose
    alert interval has reached the yellow threshold.
    """
    state = record.state
    if state in (DashboardRowState.DONE, DashboardRowState.BYE):
        return AckUnavailableReason.TERMINAL
    if state is DashboardRowState.PAUSED:
        return AckUnavailableReason.PAUSED
    if state is DashboardRowState.UNKNOWN:
        return AckUnavailableReason.CLOCK_UNKNOWN
    if state is not DashboardRowState.DUE:
        return AckUnavailableReason.NOT_DUE

    if tier is TrafficTier.GREEN:
        return (
            AckUnavailableReason.RECENTLY_ACKNOWLEDGED
            if has_ack
            else AckUnavailableReason.BELOW_THRESHOLD
        )

    return None


def _in_side_order(
    contestants: Sequence[DashboardContestantRecord],
    side_a_identity: str | None,
) -> list[DashboardContestantRecord]:
    """Put the contestant of side A first, if the pairing says which it is."""
    if side_a_identity is None:
        return list(contestants)

    return sorted(contestants, key=lambda c: c.identity != side_a_identity)


def _contestant_name(
    contestant: DashboardContestantRecord, users: Mapping[UserID, User]
) -> str:
    if contestant.team_id is not None:
        return contestant.team_name or ''

    return _user_name(
        users.get(contestant.user_id) if contestant.user_id else None
    )


def _user_name(user: User | None) -> str:
    if user is None or user.deleted or not user.screen_name:
        return gettext('Deleted user')

    return user.screen_name


def _orga_name(user: User | None) -> str:
    if user is None or user.deleted or not user.screen_name:
        return gettext('Deleted orga')

    return user.screen_name


def _orga_names(
    user_ids: Sequence[UserID], users: Mapping[UserID, User]
) -> tuple[str, ...]:
    names = []
    for user_id in user_ids:
        user = users.get(user_id)
        if user is not None and not user.deleted and user.screen_name:
            names.append(user.screen_name)
    return tuple(names)


def _status_note(
    record: DashboardMatchRecord,
    contestants: Sequence[DashboardContestantRecord],
) -> DashboardStatusNote | None:
    state = record.state

    if state is DashboardRowState.UPCOMING:
        if record.earlier_open_round is not None:
            return DashboardStatusNote(
                code=STATUS_NOTE_EARLIER_ROUND_OPEN,
                params=(('round', record.earlier_open_round),),
            )
        return None

    if state is DashboardRowState.BYE:
        return DashboardStatusNote(code=STATUS_NOTE_BYE_ADVANCE)

    if state is DashboardRowState.AWAITING_LOBBY:
        params: list[tuple[str, str | int]] = [
            ('filled', record.contestant_count)
        ]
        if record.lobby_size is not None:
            params.append(('size', record.lobby_size))
        round_ = record.location.round
        if round_ is not None and round_ >= 1:
            params.append(('after_round', round_ - 1))
        return DashboardStatusNote(
            code=STATUS_NOTE_LOBBY_WAITING, params=tuple(params)
        )

    if state is DashboardRowState.DONE:
        if (
            record.confirmed
            and record.game_format is GameFormat.ONE_V_ONE
            and len(contestants) == 2
        ):
            score_a, score_b = contestants[0].score, contestants[1].score
            if score_a is not None and score_b is not None:
                return DashboardStatusNote(
                    code=STATUS_NOTE_RESULT_CONFIRMED,
                    params=(('score_a', score_a), ('score_b', score_b)),
                )
        return None

    if state in _DEMAND_STATES and record.reopened_same_pairing:
        return DashboardStatusNote(code=STATUS_NOTE_CORRECTED_REOPENED)

    return None
