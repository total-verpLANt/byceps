"""
byceps.services.lan_tournament.dashboard_view_helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The view adapter of the orga dashboard.

It parses and builds the list queries, formats times and durations, names
the trusted routes and turns one `DashboardPage` into the localized,
page-safe context that the admin, the site and the theme wrappers render
alike. It reads no database and takes no scope from a request: the page it
adapts is already authorized, and every label comes from a row's own
facts, never from the other rows of the page.

The context holds plain data only (`str`, `int`, `bool`, `None`, `dict`,
`list`), so a renderer cannot reach a hidden object through it.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, UTC
import re
from typing import Any, cast, Literal
from urllib.parse import parse_qsl, urlencode
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from babel import Locale
from babel.dates import format_skeleton, get_timezone_name
from flask import current_app, url_for
from flask_babel import get_locale, gettext, ngettext

from byceps.services.party.models import PartyID

from .blueprints.dashboard_csrf import CSRF_INVALID_ERROR, CSRF_INVALID_NOTICE
from .blueprints.dashboard_forms import MAX_COMMENT_LENGTH, MAX_RETURN_LENGTH
from .models.bracket import Bracket
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.operational_timing import TrafficTier
from .models.tournament import TournamentID
from .models.tournament_dashboard import (
    AckUnavailableReason,
    DASHBOARD_SCOPES,
    DASHBOARD_SORTS,
    DASHBOARD_STATES,
    DASHBOARD_VIEWS,
    DashboardAcknowledgementSummary,
    DashboardConflictRef,
    DashboardMatchLocation,
    DashboardPage,
    DashboardQuery,
    DashboardRow,
    DashboardRowState,
    DashboardScopeKind,
    DashboardSettings,
    DashboardSort,
    DashboardState,
    DashboardStatusNote,
    DashboardView,
)
from .tournament_dashboard_coordination_service import (
    DASHBOARD_ACK_BELOW_THRESHOLD_ERROR,
    DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR,
    DASHBOARD_ACK_COMMENT_INVALID_ERROR,
    DASHBOARD_ACK_CONFLICT_ERROR,
    DASHBOARD_ACK_NOT_DUE_ERROR,
    DASHBOARD_ACK_PAUSED_ERROR,
    DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR,
    DASHBOARD_ACK_TERMINAL_ERROR,
    DASHBOARD_MATCH_NOT_FOUND_ERROR,
    DASHBOARD_MATCH_TERMINAL_ERROR,
    DASHBOARD_PIN_CONFLICT_ERROR,
)
from .tournament_dashboard_repository import MAX_PAGE_NUMBER
from .tournament_dashboard_service import (
    DASHBOARD_FORBIDDEN_ERROR,
    DASHBOARD_QUERY_INVALID_ERROR,
    DASHBOARD_UNAUTHENTICATED_ERROR,
    STATUS_NOTE_BYE_ADVANCE,
    STATUS_NOTE_CORRECTED_REOPENED,
    STATUS_NOTE_EARLIER_ROUND_OPEN,
    STATUS_NOTE_LOBBY_WAITING,
    STATUS_NOTE_RESULT_CONFIRMED,
)
from .tournament_operational_domain_service import normalize_utc


Surface = Literal['admin', 'site']

ROW_ANCHOR_PREFIX = 'lt-row-'
RETURN_PARAMETER = 'return'

# The query parameters of a list URL, in the order they are written.
QUERY_PARAMETERS = ('scope', 'view', 'state', 'sort', 'tournament', 'page')

# The routes the context links to, by name. The admin routes of the
# dashboard itself are bound to the party in the URL; the site routes to
# the current site. The routes of Issues 24 and 25 must carry these names.
DASHBOARD_ENDPOINTS: Mapping[str, Mapping[str, str]] = {
    'admin': {
        'list': 'lan_tournament_admin.dashboard_for_party',
        'poll': 'lan_tournament_admin.dashboard_poll_for_party',
        'pin': 'lan_tournament_admin.dashboard_pin',
        'ack': 'lan_tournament_admin.dashboard_ack',
        'match': 'lan_tournament_admin.view_match',
        'tournament': 'lan_tournament_admin.view',
    },
    'site': {
        'list': 'lan_tournament.orga_dashboard',
        'poll': 'lan_tournament.orga_dashboard_poll',
        'pin': 'lan_tournament.orga_dashboard_pin',
        'ack': 'lan_tournament.orga_dashboard_ack',
        'match': 'lan_tournament.view_match',
        'tournament': 'lan_tournament.view',
    },
}
_PARTY_BOUND_ROUTES = frozenset({'list', 'poll', 'pin', 'ack'})

# The stable `error` code and status of every refusal a route can answer.
TRANSPORT_ERROR_SESSION_EXPIRED = 'session_expired'
TRANSPORT_ERROR_ACCESS_REVOKED = 'access_revoked'
TRANSPORT_ERROR_CSRF_INVALID = 'csrf_invalid'
TRANSPORT_ERROR_UNAVAILABLE = 'unavailable'
TRANSPORT_ERROR_STALE = 'stale'
TRANSPORT_ERROR_REFUSED = 'refused'
TRANSPORT_ERROR_INVALID = 'invalid'

TRANSPORT_ERRORS: Mapping[str, tuple[str, int]] = {
    DASHBOARD_UNAUTHENTICATED_ERROR: (TRANSPORT_ERROR_SESSION_EXPIRED, 401),
    DASHBOARD_FORBIDDEN_ERROR: (TRANSPORT_ERROR_ACCESS_REVOKED, 403),
    CSRF_INVALID_ERROR: (TRANSPORT_ERROR_CSRF_INVALID, 403),
    DASHBOARD_MATCH_NOT_FOUND_ERROR: (TRANSPORT_ERROR_UNAVAILABLE, 404),
    DASHBOARD_PIN_CONFLICT_ERROR: (TRANSPORT_ERROR_STALE, 409),
    DASHBOARD_ACK_CONFLICT_ERROR: (TRANSPORT_ERROR_STALE, 409),
    DASHBOARD_MATCH_TERMINAL_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_TERMINAL_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_PAUSED_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_NOT_DUE_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_BELOW_THRESHOLD_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR: (TRANSPORT_ERROR_REFUSED, 409),
    DASHBOARD_ACK_COMMENT_INVALID_ERROR: (TRANSPORT_ERROR_INVALID, 422),
    DASHBOARD_QUERY_INVALID_ERROR: (TRANSPORT_ERROR_INVALID, 422),
}

_UUID_PATTERN = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{12}'
)
_PAGE_PATTERN = re.compile(r'[0-9]{1,7}')
_MICROSECONDS_PER_MINUTE = 60_000_000
_MAX_ECHO_LENGTH = 40
_PAGE_WINDOW = 2

_TIER_LETTERS = {
    TrafficTier.GREEN: 'g',
    TrafficTier.YELLOW: 'y',
    TrafficTier.RED: 'r',
}
_TIER_ICONS = {'g': '○', 'y': '▲', 'r': '■'}
_TIER_COLORS = {'g': 'green', 'y': 'yellow', 'r': 'red'}

# The tier cell of a row that has no tier: kind, icon, name and caption.
_STATE_CELLS = {
    DashboardRowState.PAUSED: ('paused', '❚❚', 'state_paused', 'caption_paused'),
    DashboardRowState.UPCOMING: (
        'upcoming', '›', 'state_upcoming', 'caption_upcoming',
    ),
    DashboardRowState.PARTIAL: (
        'partial', '◌', 'state_partial', 'caption_partial',
    ),
    DashboardRowState.BYE: ('bye', '»', 'state_bye', 'caption_bye'),
    DashboardRowState.AWAITING_LOBBY: (
        'wait', '…', 'state_wait', 'caption_wait',
    ),
    DashboardRowState.DONE: ('done', '—', 'state_done', 'caption_done'),
    DashboardRowState.UNKNOWN: (
        'unknown', '?', 'state_unknown', 'caption_unknown',
    ),
}  # fmt: skip

_TERMINAL_STATES = frozenset({DashboardRowState.DONE, DashboardRowState.BYE})


@dataclass(frozen=True, kw_only=True)
class _Env:
    """What every part of one context needs, so it is not passed around."""

    surface: Surface
    party_id: PartyID | None
    query: DashboardQuery
    settings: DashboardSettings
    snapshot: datetime
    labels: Mapping[str, str]
    csrf_token: str
    return_value: str


# -------------------------------------------------------------------- labels


def dashboard_labels() -> dict[str, str]:
    """Return the localized vocabulary of the dashboard.

    A key that ends in `_template` is a raw message with `%(name)s`
    placeholders for a client to fill. Every other value is final text.
    Strings that name a threshold are built per request from the party's
    settings, not here.
    """
    return {
        'title': gettext('Orga dashboard'),
        'scope': gettext('Dashboard scope'),
        'scope_assigned': gettext('Assigned tournaments'),
        'scope_all': gettext('All tournaments of this party'),
        'scope_apply': gettext('Apply scope'),
        'scope_fixed': gettext('Fixed for site orgas'),
        # Freshness and the states of the refresh.
        'as_of': gettext('As of'),
        'refresh_now': gettext('Refresh now'),
        'refresh_retry': gettext('Try again'),
        'fresh_ok_template': gettext(
            'Refreshes automatically every %(seconds)d s'
        ),
        'fresh_loading': gettext('Refreshing…'),
        'fresh_held': gettext(
            'Automatic refresh paused while a form is being edited'
        ),
        'fresh_hidden': gettext('Paused while the tab is in the background'),
        'fresh_failed': gettext(
            'Refresh failed – the data shown is out of date.'
        ),
        'fresh_stopped': gettext('Refresh stopped'),
        'fresh_nojs': gettext('Without JavaScript: manual refresh only'),
        'fresh_ago_template': gettext('(%(minutes)d min ago)'),
        'live_refreshed_template': gettext('Refreshed, as of %(time)s'),
        'failure_detail_template': gettext(
            'As of %(time)s. Tiers and times may have changed since.'
            ' Actions are checked by the server.'
        ),
        'refresh_confirm': gettext('Keep the draft and refresh the list?'),
        'session_expired_heading': gettext(
            'Your session has expired. Refreshing was stopped.'
        ),
        'session_expired_detail': gettext(
            'The list was hidden. After signing in you return to the same view.'
        ),
        'sign_in': gettext('Sign in'),
        'access_lost_heading': gettext(
            'No access any more. Refreshing was stopped.'
        ),
        'access_lost_detail': gettext(
            'You can no longer see the orga dashboard. Contact the'
            ' tournament team with any questions.'
        ),
        'to_tournament_overview': gettext('To the tournament overview'),
        'draft_heading': gettext('Unsent draft'),
        'draft_detail': gettext(
            'Your comment was not sent. Copy it if you need it; it is'
            ' discarded when you leave the page.'
        ),
        'draft_readonly': gettext('Draft (read only)'),
        'focus_moved_template': gettext(
            '%(match)s is no longer due (confirmed) and was removed.'
        ),
        'focus_next': gettext('Focus is on the next match.'),
        'csrf_invalid': str(CSRF_INVALID_NOTICE),
        # Tier tiles.
        'tiles_heading': gettext('Due matches by tier'),
        'tiles_scope': gettext(
            '· whole scope, independent of filters and page'
        ),
        'tiles_note': gettext(
            'Green does not mean ready to play. Readiness is shown separately.'
        ),
        'tier_red': gettext('Long delay'),
        'tier_yellow': gettext('Check delay'),
        'tier_green': gettext('Below warning threshold'),
        'tile_filter': gettext('Filter the list to this tier'),
        'tile_unfilter': gettext('Remove the filter, show all matches'),
        'tile_filtered_template': gettext('Filtered to %(tier)s: %(count)s.'),
        'tile_unfiltered_template': gettext('Filter removed. %(count)s.'),
        # Views, filters, chips.
        'view_due': gettext('Due now'),
        'view_upcoming': gettext('Upcoming matches'),
        'view_all': gettext('All matches'),
        'views_nav': gettext('View'),
        'filters_form': gettext('Filter dashboard matches'),
        'filter_tournament': gettext('Tournament'),
        'filter_state': gettext('State'),
        'filter_sort': gettext('Sort order'),
        'tournaments_assigned': gettext('All assigned tournaments'),
        'state_all': gettext('All states'),
        'state_tier_template': gettext('Tier: %(tier)s'),
        'state_ready_none': gettext('Nobody ready'),
        'state_ready_one': gettext('One side ready'),
        'state_ready_both': gettext('Both ready'),
        'state_ready_unavailable': gettext('Readiness not available'),
        'state_conflict': gettext('With conflict'),
        'state_review_open': gettext('Review open'),
        'state_pinned': gettext('Pinned'),
        'sort_urgency': gettext('Urgency'),
        'sort_wait': gettext('Total active wait'),
        'sort_tournament': gettext('Tournament'),
        'apply': gettext('Apply filters'),
        'reset': gettext('Reset'),
        'applied': gettext('Applied filters'),
        'sort_chip_template': gettext('Sort: %(sort)s'),
        'query_invalid_heading': gettext(
            'Part of the query was invalid and was not applied.'
        ),
        'field_invalid': gettext('This value is invalid and was not applied.'),
        'tournament_unavailable': gettext('This tournament is not available'),
        'pager': gettext('Pages'),
        'pager_previous': gettext('‹ Previous'),
        'pager_next': gettext('Next ›'),
        'page': gettext('Page'),
        # Row context and tier cell.
        'format_knockout': gettext('Knockout'),
        'format_round_robin': gettext('Round robin'),
        'format_free_for_all': gettext('Free for all'),
        'game_unknown': gettext('Game unknown'),
        'versus': gettext('against'),
        'opponent_open': gettext('Opponent still open'),
        'no_opponent': gettext('no opponent (bye)'),
        'orgas': gettext('Responsible'),
        'no_orga': gettext('No orga assigned'),
        'readiness': gettext('Readiness'),
        'ready_unavailable_lobby': gettext('Not available (lobby format)'),
        'ready_unavailable': gettext('Not available'),
        'ready_none': gettext('Nobody ready'),
        'ready_one': gettext('One side ready'),
        'ready_both': gettext('Both ready'),
        'side_open': gettext('Open'),
        'alert_interval': gettext('Alert interval'),
        'alert_interval_frozen': gettext('Alert interval, frozen'),
        'state_paused': gettext('Paused'),
        'caption_paused': gettext('Wait time frozen'),
        'state_upcoming': gettext('Upcoming'),
        'caption_upcoming': gettext('Not due yet · no tier'),
        'state_partial': gettext('Incomplete'),
        'caption_partial': gettext('Opponent open · not playable'),
        'state_bye': gettext('Bye'),
        'caption_bye': gettext('No match needed'),
        'state_wait': gettext('Waiting for lobby'),
        'caption_wait': gettext('Lobby not complete yet'),
        'state_done': gettext('Completed'),
        'caption_done': gettext('Confirmed · no actions'),
        'state_unknown': gettext('Time unknown'),
        'caption_unknown': gettext('No tier without a time base'),
        # The five independent times of a row.
        'time_wait': gettext('Total active wait'),
        'time_occupied': gettext('Occupied since'),
        'time_due': gettext('Due since (this episode)'),
        'time_changed': gettext('Last match change'),
        'time_created': gettext('Created'),
        'not_due_yet': gettext('Not due yet'),
        'not_occupied': gettext('Not yet occupied'),
        'time_unknown': gettext('Historical time unknown'),
        'not_available': gettext('Not available'),
        # Signals and links.
        'signal_conflict': gettext('Conflict'),
        'signal_review_open': gettext('Review open'),
        'signal_review_unavailable': gettext('Review: not available'),
        'link_match': gettext('To the match'),
        'link_tournament': gettext('To the tournament'),
        'link_back': gettext('← Back to the orga dashboard'),
        'link_back_plain': gettext('Back to the orga dashboard'),
        # The acknowledgement.
        'ack_open': gettext('Log a check…'),
        'ack_again': gettext('Check again'),
        'ack_submit': gettext('Record the check'),
        'ack_cancel': gettext('Cancel'),
        'ack_heading': gettext('Delay checked – record it'),
        'ack_heading_again': gettext('Delay checked again – record it'),
        'ack_help': gettext(
            'Records that you have checked the delay. Only resets the alert'
            ' interval, not the total wait. Visible to all orgas of this'
            ' tournament, not to players.'
        ),
        'ack_comment': gettext('Comment (optional)'),
        'ack_counter_template': gettext(
            '%(count)d / %(max)d characters · text only'
        ),
        'ack_saving': gettext('Saving…'),
        'ack_pending': gettext('Not confirmed yet.'),
        'ack_success_template': gettext(
            'Check recorded (server time %(time)s). The alert interval'
            ' restarts; total wait unchanged at %(wait)s.'
        ),
        'ack_stale': gettext('dashboard_ack_conflict'),
        'ack_stale_detail_template': gettext(
            '%(actor)s already recorded this delay at %(time)s. Your draft'
            ' is kept below and was not sent.'
        ),
        'ack_refresh_keep': gettext('Refresh now · keep draft'),
        'ack_failed': gettext(
            'Check not saved – connection failed. Try again.'
        ),
        'ack_refused_template': gettext('Not recorded: %(reason)s'),
        'ack_refused_detail': gettext(
            'The row now shows the server state. Your draft stays available'
            ' for copying.'
        ),
        'ack_banner_template': gettext('Check recorded: %(match)s.'),
        'ack_banner_detail_template': gettext(
            'As of %(time)s. The alert interval restarts.'
        ),
        'ack_announce': gettext('Check recorded.'),
        'record_template': gettext('Last checked by %(actor)s at %(time)s'),
        'record_follow': gettext('Delay again – check once more'),
        'history_show': gettext('Show recent checks'),
        'history_hide': gettext('Hide recent checks'),
        'history_count_template': gettext('(%(count)d in this episode)'),
        'history_current': gettext('current'),
        'history_no_comment': gettext('no comment'),
        # The pin.
        'pin_add': gettext('Pin for the orga team'),
        'pin_remove': gettext('Remove pin'),
        'pin_adding': gettext('Pinning…'),
        'pin_removing': gettext('Removing…'),
        'pin_ok_template': gettext('Pinned (server time %(server)s)'),
        'pin_removed': gettext('Pin removed.'),
        'pin_stale': gettext(
            'The state has changed. Please refresh and try again.'
        ),
        'pin_failed': gettext('Pin not saved – connection failed. Try again.'),
        # The missing match.
        'missing_heading': gettext('This match is not available.'),
        'missing_detail': gettext('It does not exist or you cannot see it.'),
    }


# --------------------------------------------------------------- durations


def format_active_duration(elapsed_us: int) -> str:
    """Return a duration in whole minutes, rounded down.

    The floor keeps a label from contradicting the tier: thresholds are
    whole minutes, so `14:59.999999` reads `14 min` and is still green.
    """
    minutes = max(elapsed_us, 0) // _MICROSECONDS_PER_MINUTE

    if minutes < 1:
        return gettext('under 1 min')

    if minutes < 60:
        return gettext('%(minutes)d min', minutes=minutes)

    hours, rest = divmod(minutes, 60)
    return gettext('%(hours)d h %(minutes)02d min', hours=hours, minutes=rest)


def format_wall_time(value: datetime, *, snapshot: datetime) -> str:
    """Return `HH:MM` in the party's time zone.

    A day other than the snapshot's day gets a date prefix. A naive value
    is UTC.
    """
    local = _party_time(value)
    clock = local.strftime('%H:%M')

    if local.date() == _party_time(snapshot).date():
        return clock

    return f'{format_skeleton("MMMd", local, locale=_locale())}, {clock}'


def format_freshness_time(as_of: datetime) -> str:
    """Return the `HH:MM:SS` of the snapshot in the party's time zone."""
    return _party_time(as_of).strftime('%H:%M:%S')


def format_zone_label(as_of: datetime) -> str:
    """Return the localized zone abbreviation of the party at that moment."""
    local = _party_time(as_of)
    abbreviation = get_timezone_name(
        local, width='short', uncommon=True, locale=_locale()
    )
    if not abbreviation or abbreviation[0] in '+-':
        abbreviation = local.tzname() or ''
    return abbreviation


def format_comment_counter(length: int) -> str:
    """Return the counter line of the comment field."""
    return gettext(
        '%(count)d / %(max)d characters · text only',
        count=length,
        max=MAX_COMMENT_LENGTH,
    )


def _party_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    name = current_app.config.get('TIMEZONE')
    if not isinstance(name, str):
        return value.astimezone(UTC)

    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return value.astimezone(UTC)

    return value.astimezone(zone)


def _locale() -> Locale:
    return get_locale() or Locale('en')


# ----------------------------------------------------------- query parsing


def parse_dashboard_query(
    args: Mapping[str, str],
    *,
    surface: Surface,
    per_page: int,
    scope_tournament_ids: Collection[object] | None = None,
) -> tuple[DashboardQuery, dict[str, str]]:
    """Return the validated query and the notice of each dropped field.

    An invalid value is dropped, never repaired, and the query falls back
    to that field's default, so the scope never widens. A site ignores
    `scope`; a site orga is always scoped to their own tournaments. Given
    the tournament IDs of the resolved scope, a tournament outside it is
    dropped the same way: unknown, hidden and malformed IDs all read as
    "not available".
    """
    return _parse(
        _reader(args),
        surface=surface,
        per_page=per_page,
        scope_tournament_ids=scope_tournament_ids,
    )


def parse_dashboard_return(
    raw: str | None, *, surface: Surface, per_page: int
) -> DashboardQuery | None:
    """Return the list query a `return` value stands for.

    No value gives `None`: there is no list to go back to. A value that is
    not exactly an encoded query string of the allowlisted parameters, in
    any respect, gives the default list. The value is never a URL; it is
    only ever read, never followed.
    """
    if raw is None or raw == '':
        return None

    default = DashboardQuery(per_page=per_page)

    if not isinstance(raw, str) or len(raw) > MAX_RETURN_LENGTH:
        return default

    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return default

    values: dict[str, list[str]] = {}
    for key, value in pairs:
        if key not in QUERY_PARAMETERS:
            return default
        values.setdefault(key, []).append(value)

    query, errors = _parse(
        lambda name: values.get(name, []),
        surface=surface,
        per_page=per_page,
        scope_tournament_ids=None,
    )

    return default if errors else query


def build_dashboard_return(query: DashboardQuery, *, surface: Surface) -> str:
    """Return the `return` value of a list: its whole encoded query."""
    return urlencode(_query_parameters(query, surface, complete=True))


def build_dashboard_list_url(
    surface: Surface,
    party_id: PartyID,
    query: DashboardQuery,
    *,
    anchor_match_id: object | None = None,
) -> str:
    """Return the URL of a list, rebuilt from the route and the query.

    The admin party comes from the caller (the destination's tournament),
    never from a parameter. The anchor is the row of a match the viewer
    already sees.
    """
    values: dict[str, object] = dict(_query_parameters(query, surface))

    anchor = _row_anchor(anchor_match_id)
    if anchor is not None:
        values['_anchor'] = anchor

    return _route(surface, 'list', party_id, **values)


def describe_dashboard_return(query: DashboardQuery) -> str:
    """Return the context of a back link: view, sort and page."""
    labels = dashboard_labels()
    return '({})'.format(
        ' · '.join(
            (
                _view_label(labels, query.view),
                _sort_label(labels, query.sort),
                gettext('Page %(page)d', page=query.page),
            )
        )
    )


def _reader(args: Mapping[str, str]) -> Callable[[str], list[str]]:
    getlist = getattr(args, 'getlist', None)

    def read(name: str) -> list[str]:
        if callable(getlist):
            return [str(value) for value in getlist(name)]

        value = args.get(name)
        return [] if value is None else [str(value)]

    return read


def _parse(
    read: Callable[[str], list[str]],
    *,
    surface: Surface,
    per_page: int,
    scope_tournament_ids: Collection[object] | None,
) -> tuple[DashboardQuery, dict[str, str]]:
    errors: dict[str, str] = {}

    def single(name: str) -> str | None:
        values = read(name)
        if not values:
            return None
        if len(values) > 1:
            errors[name] = _dropped_notice(name, values[0])
            return None
        return values[0]

    scope: DashboardScopeKind = 'assigned'
    if surface == 'admin':
        raw_scope = single('scope')
        if raw_scope in DASHBOARD_SCOPES:
            scope = cast(DashboardScopeKind, raw_scope)
        elif raw_scope is not None and 'scope' not in errors:
            errors['scope'] = _dropped_notice('scope', raw_scope)

    view: DashboardView = 'due'
    raw_view = single('view')
    if raw_view in DASHBOARD_VIEWS:
        view = cast(DashboardView, raw_view)
    elif raw_view is not None and 'view' not in errors:
        errors['view'] = _dropped_notice('view', raw_view)

    state: DashboardState = 'all'
    raw_state = single('state')
    if raw_state in DASHBOARD_STATES:
        state = cast(DashboardState, raw_state)
    elif raw_state is not None and 'state' not in errors:
        errors['state'] = _dropped_notice('state', raw_state)

    sort: DashboardSort = 'urgency'
    raw_sort = single('sort')
    if raw_sort in DASHBOARD_SORTS:
        sort = cast(DashboardSort, raw_sort)
    elif raw_sort is not None and 'sort' not in errors:
        errors['sort'] = _dropped_notice('sort', raw_sort)

    tournament_id: TournamentID | None = None
    raw_tournament = single('tournament')
    if raw_tournament:
        parsed = _parse_uuid(raw_tournament)
        if parsed is None or not _in_scope(parsed, scope_tournament_ids):
            errors['tournament'] = _dropped_notice('tournament', raw_tournament)
        else:
            tournament_id = TournamentID(parsed)

    page = 1
    raw_page = single('page')
    if raw_page is not None:
        if (
            _PAGE_PATTERN.fullmatch(raw_page) is not None
            and 1 <= int(raw_page) <= MAX_PAGE_NUMBER
        ):
            page = int(raw_page)
        elif 'page' not in errors:
            errors['page'] = _dropped_notice('page', raw_page)

    query = DashboardQuery(
        scope=scope,
        view=view,
        state=state,
        sort=sort,
        tournament_id=tournament_id,
        page=page,
        per_page=per_page,
    )
    return query, errors


def _parse_uuid(raw: str) -> UUID | None:
    if _UUID_PATTERN.fullmatch(raw) is None:
        return None

    return UUID(raw)


def _in_scope(value: UUID, scope_ids: Collection[object] | None) -> bool:
    if scope_ids is None:
        return True

    allowed = set()
    for item in scope_ids:
        try:
            allowed.add(UUID(str(item)))
        except ValueError:
            continue

    return value in allowed


def _dropped_notice(field: str, raw: str) -> str:
    """Return the sentence that tells which value was not applied."""
    value = _echo(raw)

    if field == 'tournament':
        return gettext(
            'Tournament “%(value)s” is not available. The list is shown'
            ' without this value.',
            value=value,
        )

    return gettext(
        '%(field)s “%(value)s” does not exist. The list is shown without'
        ' this value.',
        field=_field_noun(field),
        value=value,
    )


def _field_noun(field: str) -> str:
    if field == 'scope':
        return gettext('Dashboard scope')
    if field == 'view':
        return gettext('View')
    if field == 'state':
        return gettext('State')
    if field == 'sort':
        return gettext('Sort order')
    return gettext('Page')


def _echo(raw: str) -> str:
    """Return a short, printable copy of a rejected value."""
    printable = ''.join(char for char in raw if char.isprintable()).strip()

    if len(printable) > _MAX_ECHO_LENGTH:
        return printable[:_MAX_ECHO_LENGTH] + '…'

    return printable


# ---------------------------------------------------------------- URLs


def _query_parameters(
    query: DashboardQuery, surface: Surface, *, complete: bool = False
) -> dict[str, str]:
    """Return the parameters of a query: all, or only the non-default."""
    parameters: dict[str, str] = {}

    if surface == 'admin' and (complete or query.scope != 'assigned'):
        parameters['scope'] = query.scope
    if complete or query.view != 'due':
        parameters['view'] = query.view
    if complete or query.state != 'all':
        parameters['state'] = query.state
    if complete or query.sort != 'urgency':
        parameters['sort'] = query.sort

    tournament = _uuid_text(query.tournament_id)
    if tournament is not None:
        parameters['tournament'] = tournament

    page = max(int(query.page), 1)
    if complete or page != 1:
        parameters['page'] = str(page)

    return parameters


def _uuid_text(value: object | None) -> str | None:
    if value is None:
        return None

    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


def _row_anchor(match_id: object | None) -> str | None:
    text = _uuid_text(match_id)
    return None if text is None else f'{ROW_ANCHOR_PREFIX}{text}'


def _route(
    surface: Surface, kind: str, party_id: PartyID | None, **values: Any
) -> str:
    endpoint = DASHBOARD_ENDPOINTS[surface][kind]

    if surface == 'admin' and kind in _PARTY_BOUND_ROUTES:
        if not party_id:
            raise ValueError('The admin dashboard routes need the party.')
        values['party_id'] = party_id

    return url_for(endpoint, **values)


def _list_url(env: _Env, query: DashboardQuery, **values: Any) -> str:
    return _route(
        env.surface,
        'list',
        env.party_id,
        **_query_parameters(query, env.surface),
        **values,
    )


# ------------------------------------------------------------ transport


def serialize_dashboard_fragment(
    html: str, *, as_of: datetime, poll_seconds: int
) -> dict[str, object]:
    """Return the body of a poll answer: rendered panel, snapshot, rhythm."""
    return {
        'html': html,
        'as_of': _iso_utc(as_of),
        'poll_seconds': int(poll_seconds),
    }


def serialize_dashboard_success(
    *, committed_at: datetime, fragment: Mapping[str, object]
) -> dict[str, object]:
    """Return the body of an accepted pin or acknowledgement.

    `committed_at` is the server time of the write. The fragment is the
    panel, re-read after the commit.
    """
    return {'committed_at': _iso_utc(committed_at), 'fragment': dict(fragment)}


def serialize_dashboard_error(
    error: str,
    *,
    fragment: Mapping[str, object] | None = None,
    draft_target: bool | None = None,
) -> tuple[dict[str, object], int]:
    """Return the JSON body and the status of a refused request.

    `error` is the service or CSRF error code. The `error` of the body is
    the stable transport code a client dispatches on, never the status
    alone. A refusal that leaves the list stale carries the whole re-read
    panel and whether the target row can still take the action.
    """
    try:
        code, status = TRANSPORT_ERRORS[error]
    except KeyError:
        raise ValueError(f'Unmapped dashboard error: {error}') from None

    message = (
        str(CSRF_INVALID_NOTICE)
        if error == CSRF_INVALID_ERROR
        else gettext(error)
    )
    body: dict[str, object] = {'error': code, 'message': message}

    if fragment is not None:
        body['fragment'] = dict(fragment)
    if draft_target is not None:
        body['draft_target'] = bool(draft_target)

    return body, status


def _iso_utc(value: datetime) -> str:
    return normalize_utc(value).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


# ----------------------------------------------------------- the context


def build_dashboard_context(
    page: DashboardPage,
    query: DashboardQuery,
    settings: DashboardSettings,
    *,
    surface: Surface,
    csrf_token: str,
    party_id: PartyID | None = None,
    query_errors: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return everything a wrapper needs to render one dashboard page.

    The admin routes are bound to the party, so `party_id` is required for
    `surface='admin'`. `query_errors` are the notices of
    `parse_dashboard_query`. The result holds plain data only.
    """
    if surface == 'admin' and not party_id:
        raise ValueError('The admin dashboard needs the party.')

    env = _Env(
        surface=surface,
        party_id=party_id,
        query=query,
        settings=settings,
        snapshot=normalize_utc(page.as_of),
        labels=dashboard_labels(),
        csrf_token=csrf_token,
        return_value=build_dashboard_return(query, surface=surface),
    )
    errors = dict(query_errors or {})

    tournament_names = {
        _uuid_text(ref.tournament_id): ref.name
        for ref in page.tournament_choices
    }
    selected_name = tournament_names.get(_uuid_text(query.tournament_id))
    if query.tournament_id is not None and selected_name is None:
        # The scope does not hold this tournament: say so, show no filter.
        errors.setdefault(
            'tournament',
            _dropped_notice('tournament', str(query.tournament_id)),
        )

    beyond_last = page.total_count > 0 and not page.rows

    return {
        'surface': surface,
        'labels': env.labels,
        'csrf_token': csrf_token,
        'return_value': env.return_value,
        'query': _query_summary(query, selected_name),
        'is_default_query': _is_default(query),
        'poll': {
            'seconds': settings.poll_seconds,
            'url': _route(
                surface,
                'poll',
                party_id,
                **_query_parameters(query, surface),
            ),
        },
        'freshness': _freshness(env),
        'scope': _scope_control(env),
        'tiles': _tiles(env, page),
        'views': _views(env),
        'filters': _filters(env, page, errors, selected_name),
        'banner': _query_banner(env, errors),
        'count': _count(env, page, selected_name, beyond_last),
        'empty': _empty(env, page, selected_name, beyond_last),
        'rows': [_row(row, env) for row in page.rows],
        'cards': _cards(env, page),
        'pager': _pager(env, page),
        'beyond_last_page': beyond_last,
    }


def _is_default(query: DashboardQuery) -> bool:
    """Tell if no filter, sort or view differs from the default list."""
    return (
        query.view == 'due'
        and query.state == 'all'
        and query.sort == 'urgency'
        and query.tournament_id is None
    )


def _query_summary(
    query: DashboardQuery, tournament_name: str | None
) -> dict[str, object]:
    return {
        'scope': query.scope,
        'view': query.view,
        'state': query.state,
        'sort': query.sort,
        'tournament': _uuid_text(query.tournament_id)
        if tournament_name is not None
        else None,
        'page': query.page,
        'per_page': query.per_page,
    }


def _freshness(env: _Env) -> dict[str, object]:
    return {
        'time': format_freshness_time(env.snapshot),
        'zone': format_zone_label(env.snapshot),
        'as_of': _iso_utc(env.snapshot),
        'status_text': gettext(
            'Refreshes automatically every %(seconds)d s',
            seconds=env.settings.poll_seconds,
        ),
    }


def _scope_control(env: _Env) -> dict[str, object]:
    labels = env.labels
    is_admin = env.surface == 'admin'

    hidden = _query_parameters(replace(env.query, page=1), env.surface)
    hidden.pop('scope', None)

    return {
        'kind': env.query.scope,
        'is_fixed': not is_admin,
        'label': labels['scope_all']
        if env.query.scope == 'all'
        else labels['scope_assigned'],
        'options': [
            {
                'value': 'assigned',
                'label': labels['scope_assigned'],
                'checked': env.query.scope != 'all',
            },
            {
                'value': 'all',
                'label': labels['scope_all'],
                'checked': env.query.scope == 'all',
            },
        ]
        if is_admin
        else [],
        'hidden': [[name, value] for name, value in hidden.items()],
    }


def _tier_ranges(settings: DashboardSettings) -> dict[str, str]:
    """Return the minute range text of each tier from the party's settings."""
    yellow, red = settings.yellow_minutes, settings.red_minutes
    last_yellow = red - 1

    if yellow == last_yellow:
        yellow_text = gettext('%(minutes)d min', minutes=yellow)
    else:
        yellow_text = gettext(
            '%(low)d–%(high)d min', low=yellow, high=last_yellow
        )

    return {
        'red': gettext('from %(minutes)d min', minutes=red),
        'yellow': yellow_text,
        'green': gettext('under %(minutes)d min', minutes=yellow),
    }


def _tiles(env: _Env, page: DashboardPage) -> dict[str, object]:
    labels = env.labels
    ranges = _tier_ranges(env.settings)
    counts = {
        'red': page.tier_counts.red,
        'yellow': page.tier_counts.yellow,
        'green': page.tier_counts.green,
    }

    items = []
    for color in ('red', 'yellow', 'green'):
        count = counts[color]
        name = labels[f'tier_{color}']
        active = env.query.state == f'tier-{color}'
        url = None
        aria_label = None
        if count > 0:
            view = env.query.view
            if not active and view == 'upcoming':
                # Only due rows carry a tier, so upcoming shows none.
                view = 'due'
            target = replace(
                env.query,
                view=view,
                state=cast(
                    DashboardState, 'all' if active else f'tier-{color}'
                ),
                page=1,
            )
            url = _list_url(env, target)
            aria_label = (
                ngettext(
                    '%(count)d match: %(tier)s (%(range)s).',
                    '%(count)d matches: %(tier)s (%(range)s).',
                    count,
                    count=count,
                    tier=name,
                    range=ranges[color],
                )
                + ' '
                + (labels['tile_unfilter'] if active else labels['tile_filter'])
            )
        items.append(
            {
                'tier': color,
                'letter': {'red': 'r', 'yellow': 'y', 'green': 'g'}[color],
                'icon': {'red': '■', 'yellow': '▲', 'green': '○'}[color],
                'name': name,
                'range': ranges[color],
                'count': count,
                'is_zero': count == 0,
                'is_active': active,
                'url': url,
                'aria_label': aria_label,
            }
        )

    return {
        'heading': labels['tiles_heading'],
        'scope_note': labels['tiles_scope'],
        'note': labels['tiles_note'],
        'items': items,
    }


def _view_label(labels: Mapping[str, str], view: str) -> str:
    return {
        'due': labels['view_due'],
        'upcoming': labels['view_upcoming'],
        'all': labels['view_all'],
    }[view]


def _state_label(labels: Mapping[str, str], state: str) -> str:
    if state.startswith('tier-'):
        return gettext(
            'Tier: %(tier)s', tier=labels[f'tier_{state.removeprefix("tier-")}']
        )

    return {
        'all': labels['state_all'],
        'ready-none': labels['state_ready_none'],
        'ready-one': labels['state_ready_one'],
        'ready-both': labels['state_ready_both'],
        'ready-unavailable': labels['state_ready_unavailable'],
        'conflict': labels['state_conflict'],
        'review-open': labels['state_review_open'],
        'pinned': labels['state_pinned'],
    }[state]


def _sort_label(labels: Mapping[str, str], sort: str) -> str:
    return {
        'urgency': labels['sort_urgency'],
        'wait': labels['sort_wait'],
        'tournament': labels['sort_tournament'],
    }[sort]


def _views(env: _Env) -> list[dict[str, object]]:
    return [
        {
            'key': view,
            'label': _view_label(env.labels, view),
            'url': _list_url(env, replace(env.query, view=view, page=1)),  # type: ignore[arg-type]
            'current': env.query.view == view,
        }
        for view in DASHBOARD_VIEWS
    ]


def _filters(
    env: _Env,
    page: DashboardPage,
    errors: Mapping[str, str],
    selected_name: str | None,
) -> dict[str, object]:
    labels = env.labels
    query = env.query

    all_label = (
        labels['scope_all']
        if query.scope == 'all'
        else labels['tournaments_assigned']
    )
    tournament_options = [
        {
            'value': '',
            'label': all_label,
            'selected': selected_name is None,
        }
    ] + [
        {
            'value': _uuid_text(ref.tournament_id) or '',
            'label': ref.name,
            'selected': _uuid_text(ref.tournament_id)
            == _uuid_text(query.tournament_id),
        }
        for ref in page.tournament_choices
    ]
    state_options = [
        {
            'value': state,
            'label': _state_label(labels, state),
            'selected': query.state == state,
        }
        for state in DASHBOARD_STATES
    ]
    sort_options = [
        {
            'value': sort,
            'label': _sort_label(labels, sort),
            'selected': query.sort == sort,
        }
        for sort in DASHBOARD_SORTS
    ]

    chips = []
    if query.view != 'due':
        chips.append(_view_label(labels, query.view))
    if selected_name is not None:
        chips.append(selected_name)
    if query.state != 'all':
        chips.append(_state_label(labels, query.state))
    if query.sort != 'urgency':
        chips.append(
            gettext('Sort: %(sort)s', sort=_sort_label(labels, query.sort))
        )

    hidden = [['view', query.view]]
    if env.surface == 'admin':
        hidden.insert(0, ['scope', query.scope])

    def field(name: str, label: str, options: list[dict[str, object]]):
        error = errors.get(name)
        return {
            'name': name,
            'id': f'lt-filter-{name}',
            'label': label,
            'options': options,
            'is_invalid': error is not None,
            'error': (
                labels['tournament_unavailable']
                if name == 'tournament'
                else labels['field_invalid']
            )
            if error is not None
            else None,
            'error_id': f'lt-filter-{name}-error',
        }

    return {
        'aria_label': labels['filters_form'],
        'hidden': hidden,
        'tournament': field(
            'tournament', labels['filter_tournament'], tournament_options
        ),
        'state': field('state', labels['filter_state'], state_options),
        'sort': field('sort', labels['filter_sort'], sort_options),
        'applied': chips,
        'has_applied': bool(chips),
        'reset_url': _list_url(
            env,
            DashboardQuery(scope=query.scope, per_page=query.per_page),
        )
        if chips
        else None,
    }


def _query_banner(
    env: _Env, errors: Mapping[str, str]
) -> dict[str, object] | None:
    if not errors:
        return None

    ordered = [errors[name] for name in QUERY_PARAMETERS if name in errors]
    return {
        'kind': 'warn',
        'heading': env.labels['query_invalid_heading'],
        'details': ordered,
    }


def _count(
    env: _Env,
    page: DashboardPage,
    selected_name: str | None,
    beyond_last: bool,
) -> dict[str, object]:
    labels = env.labels
    query = env.query

    parts = [_view_label(labels, query.view)]
    if query.state != 'all':
        parts.append(_state_label(labels, query.state))
    if selected_name is not None:
        parts.append(selected_name)
    parts.append(
        labels['scope_all']
        if query.scope == 'all'
        else labels['scope_assigned']
    )
    if beyond_last:
        parts.append(gettext('Page %(page)d no longer exists', page=page.page))
    elif page.total_pages > 1:
        parts.append(
            gettext(
                'Page %(page)d of %(pages)d',
                page=page.page,
                pages=page.total_pages,
            )
        )

    return {
        'heading': ngettext(
            '%(count)d match',
            '%(count)d matches',
            page.total_count,
            count=page.total_count,
        ),
        'context': ' · '.join(parts),
        'total': page.total_count,
    }


def _empty(
    env: _Env,
    page: DashboardPage,
    selected_name: str | None,
    beyond_last: bool,
) -> dict[str, object] | None:
    labels = env.labels
    query = env.query

    def card(
        kind: str,
        heading: str,
        body: str | None,
        actions: Sequence[tuple[str, str, bool]],
        *,
        context: str | None = None,
        bare: bool = False,
    ) -> dict[str, object]:
        return {
            'kind': kind,
            'heading': heading,
            'body': body,
            'context': context,
            'bare': bare,
            'actions': [
                {'label': label, 'url': url, 'primary': primary}
                for label, url, primary in actions
            ],
        }

    if beyond_last:
        return card(
            'page_empty',
            gettext('Page %(page)d is empty now.', page=query.page),
            gettext('Matches have been confirmed since your last refresh.')
            + ' '
            + ngettext(
                'There is only %(count)d page left.',
                'There are only %(count)d pages left.',
                page.total_pages,
                count=page.total_pages,
            ),
            [
                (
                    gettext('To page 1'),
                    _list_url(env, replace(query, page=1)),
                    True,
                )
            ],
        )

    reason = page.empty_reason
    if reason is None:
        return None

    reset = DashboardQuery(scope=query.scope, per_page=query.per_page)

    if reason == 'no_assignment':
        if env.surface == 'admin':
            return card(
                reason,
                gettext('You have no tournaments assigned at this party.'),
                gettext(
                    'As an administrator you can view all tournaments of this'
                    ' party.'
                ),
                [
                    (
                        gettext('Show all tournaments of this party'),
                        _list_url(env, replace(reset, scope='all')),
                        True,
                    )
                ],
                bare=True,
            )
        return card(
            reason,
            gettext('You have no tournaments assigned at this party.'),
            gettext(
                'To be assigned a tournament, the tournament team has to'
                ' enter you as orga.'
            ),
            [
                (
                    labels['to_tournament_overview'],
                    url_for('lan_tournament.index'),
                    True,
                )
            ],
            bare=True,
        )

    if reason == 'no_filter_matches':
        context = ' · '.join(
            part
            for part in (
                selected_name,
                _state_label(labels, query.state)
                if query.state != 'all'
                else None,
                _view_label(labels, query.view),
            )
            if part
        )
        return card(
            reason,
            gettext('No match fits these filters.'),
            None,
            [(gettext('Reset filters'), _list_url(env, reset), True)],
            context=context,
        )

    if reason == 'no_actionable_fixtures':
        counts = page.non_actionable_counts
        total = counts.paused + counts.pre_start + counts.partial
        return card(
            reason,
            gettext('No match is due at the moment.'),
            ngettext(
                '%(count)d match in your tournaments has no current demand:'
                ' %(paused)d paused, %(pre_start)d before the tournament'
                ' start, %(partial)d incomplete.',
                '%(count)d matches in your tournaments have no current'
                ' demand: %(paused)d paused, %(pre_start)d before the'
                ' tournament start, %(partial)d incomplete.',
                total,
                count=total,
                paused=counts.paused,
                pre_start=counts.pre_start,
                partial=counts.partial,
            ),
            [
                (
                    gettext('Show all matches'),
                    _list_url(env, replace(query, view='all', page=1)),
                    True,
                )
            ],
        )

    return card(
        reason,
        gettext('No match is due right now.'),
        gettext(
            'No playable match is waiting in your tournaments at the moment.'
        ),
        [
            (
                gettext('Show upcoming matches'),
                _list_url(env, replace(query, view='upcoming', page=1)),
                True,
            ),
            (
                labels['view_all'],
                _list_url(env, replace(query, view='all', page=1)),
                False,
            ),
        ],
    )


def _cards(env: _Env, page: DashboardPage) -> list[dict[str, object]]:
    """Return the note cards of leaderboard-only tournaments (view `all`)."""
    cards = []
    for ref in page.leaderboard_only_tournaments:
        title = (
            gettext(
                '%(tournament)s · %(game)s', tournament=ref.name, game=ref.game
            )
            if ref.game
            else ref.name
        )
        cards.append(
            {
                'title': title,
                'text': gettext(
                    'Highscore tournament with a plain leaderboard – there'
                    ' are no matches and no readiness. Playoffs would appear'
                    ' in their actual format.'
                ),
                'link_label': gettext('Open leaderboard'),
                'url': _route(
                    env.surface,
                    'tournament',
                    env.party_id,
                    tournament_id=_uuid_text(ref.tournament_id),
                    **{RETURN_PARAMETER: env.return_value},
                ),
            }
        )
    return cards


def _pager(env: _Env, page: DashboardPage) -> dict[str, object] | None:
    if page.total_pages <= 1:
        return None

    current = page.page
    total = page.total_pages
    labels = env.labels

    def link(number: int) -> str:
        return _list_url(env, replace(env.query, page=number))

    shown = sorted(
        {1, total}
        | set(
            range(
                max(1, min(current, total) - _PAGE_WINDOW),
                min(total, min(current, total) + _PAGE_WINDOW) + 1,
            )
        )
    )

    items: list[dict[str, object]] = []
    previous_number = 0
    for number in shown:
        if number - previous_number > 1:
            items.append({'gap': True})
        items.append(
            {
                'gap': False,
                'number': number,
                'url': link(number),
                'current': number == current,
            }
        )
        previous_number = number

    return {
        'aria_label': labels['pager'],
        'previous': {
            'label': labels['pager_previous'],
            'url': link(min(current, total + 1) - 1) if current > 1 else None,
        },
        'next': {
            'label': labels['pager_next'],
            'url': link(current + 1) if current < total else None,
        },
        'items': items,
    }


# ------------------------------------------------------------------ rows


def build_match_location_label(
    location: DashboardMatchLocation,
    *,
    game_format: GameFormat | None = None,
) -> str:
    """Return the position of a match, from the match's own facts only.

    Phase, bracket or group, round and match follow the stored position
    (rounds and matches are stored zero-based). Nothing here depends on
    the other matches of a page or of a phase, so a page break can never
    rename a round: the same match reads the same on every page.
    """
    is_lobby = game_format is GameFormat.FREE_FOR_ALL
    bracket = location.bracket

    parts: list[str] = []
    if location.phase >= 2:
        parts.append(gettext('Playoffs'))

    if bracket is Bracket.WINNERS:
        parts.append(
            gettext('Winners pool') if is_lobby else gettext('Winners bracket')
        )
    elif bracket is Bracket.LOSERS:
        parts.append(
            gettext('Losers pool') if is_lobby else gettext('Losers bracket')
        )
    elif bracket is Bracket.GRAND_FINAL:
        parts.append(gettext('Grand final'))
    elif bracket is Bracket.THIRD_PLACE:
        parts.append(gettext('Third place'))
    elif location.group_order is not None and not is_lobby:
        parts.append(
            gettext(
                'Group %(letter)s', letter=_group_letter(location.group_order)
            )
        )

    if location.round is not None and bracket not in (
        Bracket.GRAND_FINAL,
        Bracket.THIRD_PLACE,
    ):
        parts.append(gettext('Round %(num)s', num=location.round + 1))

    number = (location.match_order or 0) + 1
    parts.append(
        gettext('Lobby %(n)s', n=number)
        if is_lobby
        else gettext('Game %(num)d', num=number)
    )

    return ' · '.join(parts)


def _group_letter(group_order: int) -> str:
    if 0 <= group_order < 26:
        return chr(ord('A') + group_order)

    return str(group_order + 1)


def _format_label(row: DashboardRow, labels: Mapping[str, str]) -> str:
    if row.game_format is GameFormat.FREE_FOR_ALL:
        return labels['format_free_for_all']

    if row.elimination_mode is EliminationMode.ROUND_ROBIN:
        return labels['format_round_robin']

    return labels['format_knockout']


def _row(row: DashboardRow, env: _Env) -> dict[str, object]:
    match_id = _uuid_text(row.match_id)
    tournament_id = _uuid_text(row.tournament_id)
    if match_id is None or tournament_id is None:
        raise ValueError('A dashboard row needs real match and tournament IDs.')
    dom_id = f'{ROW_ANCHOR_PREFIX}{match_id}'
    labels = env.labels

    return_values = {RETURN_PARAMETER: env.return_value}

    return {
        'id': dom_id,
        'heading_id': f'{dom_id}-heading',
        'match_id': match_id,
        'tournament': {
            'id': tournament_id,
            'name': row.tournament_name,
            'url': _route(
                env.surface,
                'tournament',
                env.party_id,
                tournament_id=tournament_id,
                **return_values,
            ),
        },
        'game': row.game or None,
        'game_label': row.game or labels['game_unknown'],
        'format_label': _format_label(row, labels),
        'match_url': _route(
            env.surface,
            'match',
            env.party_id,
            match_id=match_id,
            **return_values,
        ),
        'matchup': _matchup(row, labels),
        'location': build_match_location_label(
            row.location, game_format=row.game_format
        ),
        'orgas': {
            'names': list(row.orga_names),
            'text': ', '.join(row.orga_names) if row.orga_names else None,
        },
        'tier': _tier_cell(row, labels),
        'ready': _ready(row, env),
        'signals': _signals(row, env),
        'conflicts': _conflict_blocks(row, env),
        'note': _note(row, env),
        'times': _times(row, env),
        'ack': _ack(row, env, dom_id),
        'pin': _pin(row, env),
    }


def _matchup(row: DashboardRow, labels: Mapping[str, str]) -> dict[str, object]:
    names = row.contestant_names

    if row.game_format is GameFormat.FREE_FOR_ALL:
        lobby = gettext('Lobby %(n)s', n=(row.location.match_order or 0) + 1)
        count = len(names)
        return {
            'kind': 'lobby',
            'title': f'{lobby} · '
            + ngettext(
                '%(count)d participant',
                '%(count)d participants',
                count,
                count=count,
            ),
            'lobby': lobby,
            'participants': list(names),
            'participants_label': gettext(
                'Participants in %(lobby)s', lobby=lobby
            ),
            'side_a': None,
            'side_b': None,
            'versus': None,
        }

    name_a = names[0] if len(names) > 0 else None
    name_b = names[1] if len(names) > 1 else None
    text_a = name_a or labels['opponent_open']
    text_b = name_b or (
        labels['no_opponent']
        if row.state is DashboardRowState.BYE
        else labels['opponent_open']
    )
    return {
        'kind': 'sides',
        'title': f'{text_a} {labels["versus"]} {text_b}',
        'lobby': None,
        'participants': [],
        'participants_label': None,
        'side_a': {'name': name_a, 'text': text_a, 'is_open': name_a is None},
        'side_b': {'name': name_b, 'text': text_b, 'is_open': name_b is None},
        'versus': labels['versus'],
    }


def _tier_cell(
    row: DashboardRow, labels: Mapping[str, str]
) -> dict[str, object]:
    """Return the tier cell: urgency only, never readiness."""
    if (
        row.state is DashboardRowState.DUE
        and row.tier is not None
        and row.alert_interval_us is not None
    ):
        letter = _TIER_LETTERS[row.tier]
        color = _TIER_COLORS[letter]
        return {
            'kind': letter,
            'urgency': color,
            'icon': _TIER_ICONS[letter],
            'name': labels[f'tier_{color}'],
            'value': format_active_duration(row.alert_interval_us),
            'caption': labels['alert_interval'],
        }

    kind, icon, name_key, caption_key = _STATE_CELLS.get(
        row.state, _STATE_CELLS[DashboardRowState.UNKNOWN]
    )
    value = None
    caption = labels[caption_key]
    if (
        row.state is DashboardRowState.PAUSED
        and row.alert_interval_us is not None
    ):
        value = format_active_duration(row.alert_interval_us)
        caption = labels['alert_interval_frozen']

    return {
        'kind': kind,
        'urgency': None,
        'icon': icon,
        'name': labels[name_key],
        'value': value,
        'caption': caption,
    }


def _ready(row: DashboardRow, env: _Env) -> dict[str, object] | None:
    """Return the Ready line, separate from the tier and never False by gap."""
    labels = env.labels

    def unavailable(text: str) -> dict[str, object]:
        return {
            'available': False,
            'unavailable_text': text,
            'summary': None,
            'level': None,
            'sides': [],
        }

    if row.game_format is GameFormat.FREE_FOR_ALL:
        return unavailable(labels['ready_unavailable_lobby'])

    if row.state in (
        DashboardRowState.PARTIAL,
        DashboardRowState.BYE,
        DashboardRowState.DONE,
    ):
        return None

    if not row.readiness_available:
        return unavailable(labels['ready_unavailable'])

    level = int(row.ready_at_a is not None) + int(row.ready_at_b is not None)
    summary = (labels['ready_none'], labels['ready_one'], labels['ready_both'])

    sides = []
    for index, ready_at in enumerate((row.ready_at_a, row.ready_at_b)):
        name = (
            row.contestant_names[index]
            if index < len(row.contestant_names)
            else None
        )
        side = name or labels['side_open']
        if ready_at is not None:
            text = gettext(
                '%(side)s ready since %(time)s',
                side=side,
                time=format_wall_time(ready_at, snapshot=env.snapshot),
            )
        else:
            text = gettext('%(side)s not ready', side=side)
        sides.append(
            {'name': name, 'ready': ready_at is not None, 'text': text}
        )

    return {
        'available': True,
        'unavailable_text': None,
        'summary': summary[level],
        'level': level,
        'sides': sides,
    }


def _signals(row: DashboardRow, env: _Env) -> list[dict[str, object]]:
    labels = env.labels
    signals: list[dict[str, object]] = []

    if row.pinned_at is not None:
        signals.append(
            {
                'kind': 'pin',
                'icon': '◆',
                'text': gettext(
                    'Pinned by %(actor)s at %(time)s',
                    actor=row.pinned_by_name or gettext('Deleted orga'),
                    time=format_wall_time(row.pinned_at, snapshot=env.snapshot),
                ),
            }
        )

    if row.conflicts:
        signals.append(
            {
                'kind': 'conflict',
                'icon': '⇄',
                'text': labels['signal_conflict'],
            }
        )

    if not row.review_available:
        signals.append(
            {
                'kind': 'review_unavailable',
                'icon': '',
                'text': labels['signal_review_unavailable'],
            }
        )
    elif row.review_open:
        signals.append(
            {
                'kind': 'review',
                'icon': '⚑',
                'text': labels['signal_review_open'],
            }
        )

    if row.has_prior_episode and row.episode_opened_at is not None:
        signals.append(
            {
                'kind': 'episode',
                'icon': '↻',
                'text': gettext(
                    'New episode since %(time)s',
                    time=format_wall_time(
                        row.episode_opened_at, snapshot=env.snapshot
                    ),
                ),
            }
        )

    return signals


def _conflict_blocks(row: DashboardRow, env: _Env) -> list[dict[str, object]]:
    """Return the conflict blocks of a row, built from sanitized DTOs.

    An authorized block names the counterparts the viewer may see. The
    external block is the person's name and one fixed sentence: it never
    reads a team, a reference or a count, so it is identical whatever
    the hidden overlap.
    """
    blocks: list[dict[str, object]] = []

    for conflict in row.conflicts:
        name = conflict.user_display_name

        if conflict.visible_refs:
            role = (
                gettext('(member of %(team)s)', team=conflict.via_team_name)
                if conflict.via_team_name
                else None
            )
            tail = gettext('is needed in another match at the same time:')
            blocks.append(
                {
                    'kind': 'authorized',
                    'name': name,
                    'role': role,
                    'tail': tail,
                    'text': ' '.join(
                        part for part in (name, role, tail) if part
                    ),
                    'refs': [
                        _conflict_ref(ref, env) for ref in conflict.visible_refs
                    ],
                }
            )

        if conflict.has_external_conflict:
            tail = gettext(
                'is needed in a match outside your tournaments at the same'
                ' time.'
            )
            blocks.append(
                {
                    'kind': 'external',
                    'name': name,
                    'role': None,
                    'tail': tail,
                    'text': f'{name} {tail}',
                    'refs': [],
                }
            )

    return blocks


def _conflict_ref(ref: DashboardConflictRef, env: _Env) -> dict[str, object]:
    labels = env.labels
    names = ref.contestant_names
    is_lobby = ref.game_format is GameFormat.FREE_FOR_ALL
    matchup = (
        f' {labels["versus"]} '.join(names)
        if len(names) == 2 and not is_lobby
        else ', '.join(names)
    )
    location = build_match_location_label(
        ref.location, game_format=ref.game_format
    )
    place = f'{ref.tournament_name} · {location}'
    text = f'{place}: {matchup}' if matchup else place
    via = (
        gettext('as member of %(team)s', team=ref.via_team_name)
        if ref.via_team_name
        else None
    )
    if via:
        text = f'{text} – {via}'

    list_page = ref.list_page
    return {
        'text': text,
        'tournament_name': ref.tournament_name,
        'location': location,
        'matchup': matchup,
        'via': via,
        'match_url': _route(
            env.surface,
            'match',
            env.party_id,
            match_id=_uuid_text(ref.match_id),
            **{RETURN_PARAMETER: env.return_value},
        ),
        'list_page': list_page,
        'list_page_label': gettext(
            'In this list: page %(page)d', page=list_page
        )
        if list_page is not None
        else None,
        'list_page_url': build_dashboard_list_url(
            env.surface,
            env.party_id,  # type: ignore[arg-type]
            replace(env.query, page=list_page),
            anchor_match_id=ref.match_id,
        )
        if list_page is not None
        else None,
    }


def _int_param(params: Mapping[str, str | int], key: str) -> int | None:
    value = params.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value

    return None


def _note(row: DashboardRow, env: _Env) -> str | None:
    """Return the explanatory note of a row, in words (design R31).

    The `round` and `after_round` parameters are stored (zero-based)
    rounds; the text counts from one.
    """
    note: DashboardStatusNote | None = row.status_note
    if note is None:
        return None

    params = dict(note.params)
    code = note.code

    if code == STATUS_NOTE_EARLIER_ROUND_OPEN:
        stored = _int_param(params, 'round')
        if stored is None:
            return None
        if row.location.group_order is not None:
            return gettext(
                'Round %(round)d of %(group)s is still open. Future match,'
                ' so no conflict.',
                round=stored + 1,
                group=gettext(
                    'Group %(letter)s',
                    letter=_group_letter(row.location.group_order),
                ),
            )
        return gettext(
            'Round %(round)d is still open. Future match, so no conflict.',
            round=stored + 1,
        )

    if code == STATUS_NOTE_BYE_ADVANCE:
        if not row.contestant_names:
            return None
        return gettext(
            '%(name)s advances without a match.', name=row.contestant_names[0]
        )

    if code == STATUS_NOTE_LOBBY_WAITING:
        filled = _int_param(params, 'filled')
        if filled is None:
            return None
        size = _int_param(params, 'size')
        after = _int_param(params, 'after_round')
        sentences = [
            gettext(
                '%(filled)d of %(size)d places filled.',
                filled=filled,
                size=size,
            )
            if size is not None
            else ngettext(
                '%(filled)d place filled.',
                '%(filled)d places filled.',
                filled,
                filled=filled,
            )
        ]
        if after is not None:
            sentences.append(
                gettext(
                    'The lobby is only created completely after round'
                    ' %(round)d.',
                    round=after + 1,
                )
            )
        return ' '.join(sentences)

    if code == STATUS_NOTE_RESULT_CONFIRMED:
        score_a = _int_param(params, 'score_a')
        score_b = _int_param(params, 'score_b')
        if score_a is None or score_b is None:
            return None
        score = f'{score_a}:{score_b}'
        if row.last_changed_at is None:
            return gettext('Result %(score)s confirmed.', score=score)
        return gettext(
            'Result %(score)s confirmed at %(time)s.',
            score=score,
            time=format_wall_time(row.last_changed_at, snapshot=env.snapshot),
        )

    if code == STATUS_NOTE_CORRECTED_REOPENED:
        if row.episode_opened_at is None:
            return gettext(
                'The result was corrected and reopened. Earlier checks'
                ' belong to the previous episode and do not apply here.'
            )
        return gettext(
            'The result was corrected and reopened at %(time)s. Earlier'
            ' checks belong to the previous episode and do not apply here.',
            time=format_wall_time(row.episode_opened_at, snapshot=env.snapshot),
        )

    return None


def _microseconds_between(start: datetime, end: datetime) -> int:
    return max((end - start) // timedelta(microseconds=1), 0)


def _times(row: DashboardRow, env: _Env) -> list[dict[str, object]]:
    """Return the five times of a row, each under its own label.

    Occupancy, the total active wait, the due episode, the last match
    change and the age of the match are separate facts. The alert interval
    is not among them: it is the tier cell's own value.
    """
    labels = env.labels
    snapshot = env.snapshot

    def wall(value: datetime) -> str:
        return format_wall_time(value, snapshot=snapshot)

    state = row.state
    total_us = row.total_active_wait_us
    wait_unknown = False
    if state is DashboardRowState.PAUSED and total_us is not None:
        wait = gettext(
            '%(duration)s, frozen', duration=format_active_duration(total_us)
        )
    elif state is DashboardRowState.DONE:
        wait = (
            gettext(
                '%(duration)s (until confirmation)',
                duration=format_active_duration(row.closed_episode_wait_us),
            )
            if row.closed_episode_wait_us is not None
            else '—'
        )
    elif total_us is not None:
        wait = format_active_duration(total_us)
    elif state in (
        DashboardRowState.UPCOMING,
        DashboardRowState.PARTIAL,
        DashboardRowState.BYE,
        DashboardRowState.AWAITING_LOBBY,
    ):
        wait = labels['not_due_yet']
    else:
        wait = labels['not_available']
        wait_unknown = True

    occupied_unknown = False
    if row.occupied_since is not None:
        occupied = wall(row.occupied_since)
    elif state in (DashboardRowState.PARTIAL, DashboardRowState.AWAITING_LOBBY):
        occupied = labels['not_occupied']
    else:
        occupied = labels['time_unknown']
        occupied_unknown = True

    times: list[dict[str, object]] = [
        {
            'key': 'wait',
            'label': labels['time_wait'],
            'value': wait,
            'is_key': True,
            'is_unknown': wait_unknown,
        },
        {
            'key': 'occupied',
            'label': labels['time_occupied'],
            'value': occupied,
            'is_key': False,
            'is_unknown': occupied_unknown,
        },
    ]

    if row.episode_opened_at is not None or state in (
        DashboardRowState.DUE,
        DashboardRowState.PAUSED,
    ):
        times.append(
            {
                'key': 'due',
                'label': labels['time_due'],
                'value': wall(row.episode_opened_at)
                if row.episode_opened_at is not None
                else labels['time_unknown'],
                'is_key': False,
                'is_unknown': row.episode_opened_at is None,
            }
        )

    times.append(
        {
            'key': 'changed',
            'label': labels['time_changed'],
            'value': wall(row.last_changed_at)
            if row.last_changed_at is not None
            else labels['time_unknown'],
            'is_key': False,
            'is_unknown': row.last_changed_at is None,
        }
    )
    times.append(
        {
            'key': 'created',
            'label': labels['time_created'],
            'value': gettext(
                '%(time)s · age %(age)s',
                time=wall(row.created_at),
                age=format_active_duration(
                    _microseconds_between(
                        normalize_utc(row.created_at), snapshot
                    )
                ),
            ),
            'is_key': False,
            'is_unknown': False,
        }
    )

    return times


def ack_unavailable_text(
    reason: AckUnavailableReason, settings: DashboardSettings
) -> str:
    """Return why a row offers no check, with the party's threshold."""
    minutes = settings.yellow_minutes

    if reason is AckUnavailableReason.BELOW_THRESHOLD:
        return gettext(
            'Checks start at %(minutes)d min of alert interval.',
            minutes=minutes,
        )
    if reason is AckUnavailableReason.RECENTLY_ACKNOWLEDGED:
        return gettext(
            'Just checked. Again from %(minutes)d min of alert interval.',
            minutes=minutes,
        )
    if reason is AckUnavailableReason.PAUSED:
        return gettext('Paused – checking not possible.')
    if reason is AckUnavailableReason.NOT_DUE:
        return gettext('Not due – no checking.')
    if reason is AckUnavailableReason.CLOCK_UNKNOWN:
        return gettext('No time base, no checking.')

    return gettext('Completed – no actions.')


def _ack(row: DashboardRow, env: _Env, dom_id: str) -> dict[str, object]:
    labels = env.labels
    snapshot = env.snapshot

    reason = row.ack_unavailable_reason
    if reason is None and row.episode_id is None:
        reason = AckUnavailableReason.CLOCK_UNKNOWN
    offered = reason is None

    latest = row.latest_acknowledgement
    recent = row.recent_acknowledgements

    form = None
    if offered:
        form = {
            'id': f'{dom_id}-form',
            'heading_id': f'{dom_id}-form-heading',
            'help_id': f'{dom_id}-form-help',
            'comment_id': f'{dom_id}-form-comment',
            'counter_id': f'{dom_id}-form-counter',
            'error_id': f'{dom_id}-form-error',
            'action_url': _route(
                env.surface,
                'ack',
                env.party_id,
                match_id=_uuid_text(row.match_id),
            ),
            'episode': _uuid_text(row.episode_id),
            'revision': row.ack_revision,
            'return_value': env.return_value,
            'csrf_token': env.csrf_token,
            'heading': labels['ack_heading_again']
            if latest is not None
            else labels['ack_heading'],
            'counter_text': format_comment_counter(0),
            'max_length': MAX_COMMENT_LENGTH,
        }

    record = None
    if latest is not None:
        follows = row.tier in (TrafficTier.YELLOW, TrafficTier.RED)
        record = {
            'id': f'{dom_id}-record',
            'actor': latest.actor_display_name,
            'time': format_wall_time(latest.occurred_at, snapshot=snapshot),
            'comment': latest.comment,
            'text': gettext(
                'Last checked by %(actor)s at %(time)s',
                actor=latest.actor_display_name,
                time=format_wall_time(latest.occurred_at, snapshot=snapshot),
            ),
            'follow': follows,
            'follow_text': labels['record_follow'] if follows else None,
            'follow_tier': ('red' if row.tier is TrafficTier.RED else 'yellow')
            if follows
            else None,
        }

    history = None
    if len(recent) > 1:
        history = {
            'id': f'{dom_id}-history',
            'show_label': labels['history_show'],
            'hide_label': labels['history_hide'],
            'count_text': gettext(
                '(%(count)d in this episode)',
                count=max(row.acknowledgement_count, len(recent)),
            ),
            'items': [
                _history_item(ack, index == 0, env)
                for index, ack in enumerate(recent)
            ],
        }

    return {
        'offered': offered,
        'unavailable_reason': reason.value if reason is not None else None,
        'unavailable_text': None
        if reason is None
        else ack_unavailable_text(reason, env.settings),
        'opener_label': (
            labels['ack_again'] if latest is not None else labels['ack_open']
        )
        if offered
        else None,
        'form': form,
        'record': record,
        'history': history,
    }


def _history_item(
    ack: DashboardAcknowledgementSummary, is_current: bool, env: _Env
) -> dict[str, object]:
    return {
        'actor': ack.actor_display_name,
        'time': format_wall_time(ack.occurred_at, snapshot=env.snapshot),
        'is_current': is_current,
        'current_label': env.labels['history_current'] if is_current else None,
        'comment': ack.comment,
        'no_comment_label': env.labels['history_no_comment'],
    }


def _pin(row: DashboardRow, env: _Env) -> dict[str, object]:
    labels = env.labels
    pinned = row.pinned_at is not None
    offered = row.state not in _TERMINAL_STATES

    return {
        'is_pinned': pinned,
        'offered': offered,
        'read_only': not offered,
        'revision': row.pin_revision,
        'action_url': _route(
            env.surface,
            'pin',
            env.party_id,
            match_id=_uuid_text(row.match_id),
        )
        if offered
        else None,
        'target': 'false' if pinned else 'true',
        'label': labels['pin_remove'] if pinned else labels['pin_add'],
        'pending_label': labels['pin_removing']
        if pinned
        else labels['pin_adding'],
        'return_value': env.return_value,
        'csrf_token': env.csrf_token,
    }
