"""
tests.unit.services.lan_tournament.test_dashboard_scope
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import call, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    tournament_dashboard_repository as repository,
    tournament_dashboard_service as service,
    tournament_operational_domain_service as policy,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisodeID,
    MatchEscalationAcknowledgementID,
    TrafficTier,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    AckUnavailableReason,
    DASHBOARD_SCOPES,
    DASHBOARD_SORTS,
    DASHBOARD_STATES,
    DASHBOARD_VIEWS,
    DashboardMatchLocation,
    DashboardNonActionableCounts,
    DashboardQuery,
    DashboardRowState,
    DashboardScope,
    DashboardSettings,
    DashboardTierCounts,
    DashboardTournamentRef,
)
from byceps.services.lan_tournament.tournament_dashboard_repository import (
    DashboardAcknowledgementRecord,
    DashboardContestantRecord,
    DashboardMatchRecord,
    DashboardPageData,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import User, UserID

from tests.helpers import generate_uuid


PARTY = PartyID('gv-36-scope')
NOW = datetime(2026, 10, 7, 12, 0, 0)
MINUTE_US = 60_000_000

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

ADMIN_PERMISSION = 'lan_tournament.administrate'

T1 = generate_uuid()
T2 = generate_uuid()
T3 = generate_uuid()


def _user(name: str | None = 'Nori', *, deleted: bool = False) -> User:
    return User(
        id=UserID(generate_uuid()),
        screen_name=name,
        initialized=True,
        suspended=False,
        deleted=deleted,
        avatar_url='',
    )


def _viewer(user: User, *claimed: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(claimed))


def _query(**fields) -> DashboardQuery:
    fields.setdefault('per_page', 50)
    return DashboardQuery(**fields)


def _record(**fields) -> DashboardMatchRecord:
    values: dict[str, Any] = dict(
        match_id=generate_uuid(),
        tournament_id=T1,
        tournament_name='Kupfer-Cup',
        game='Arena Five',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        location=DashboardMatchLocation(phase=1, round=1, match_order=1),
        state=DashboardRowState.DUE,
        confirmed=False,
        contestant_count=2,
        created_at=NOW - timedelta(hours=1),
        occupied_since=NOW - timedelta(minutes=30),
        last_changed_at=None,
        episode_id=None,
        episode_opened_at=None,
        ack_revision=0,
        has_prior_episode=False,
        reopened_same_pairing=False,
        total_active_wait_us=None,
        alert_interval_us=None,
        closed_episode_wait_us=None,
        readiness_available=False,
        ready_at_a=None,
        ready_at_b=None,
        side_a_identity=None,
        pin_revision=0,
        pinned_at=None,
        pinned_by=None,
        earlier_open_round=None,
        lobby_size=None,
    )
    values.update(fields)
    return DashboardMatchRecord(**values)


def _data(records=(), **fields) -> DashboardPageData:
    values: dict[str, Any] = dict(
        total_count=len(records),
        view_total_count=len(records),
        tier_counts=DashboardTierCounts(),
        non_actionable_counts=DashboardNonActionableCounts(),
        records=tuple(records),
        contestants={},
        orga_user_ids={},
        acknowledgements={},
        tournament_choices=(),
        leaderboard_only_tournaments=(),
    )
    values.update(fields)
    return DashboardPageData(**values)


def _scope(*tournament_ids, kind='assigned', admin=False) -> DashboardScope:
    return DashboardScope(
        user_id=UserID(generate_uuid()),
        party_id=PARTY,
        kind=kind,
        tournament_ids=tuple(tournament_ids),
        is_global_admin=admin,
    )


class FakeRepository:
    """Stands in for the dashboard repository, and records the order."""

    def __init__(self, *, assigned=(), everything=(), data=None):
        self.assigned = tuple(assigned)
        self.everything = tuple(everything)
        self.data = data if data is not None else _data()
        self.calls: list = []
        self.seen: dict = {}
        self.fail_with: Exception | None = None

    @contextmanager
    def read_snapshot(self):
        self.calls.append('enter')
        try:
            yield
        finally:
            self.calls.append('exit')

    def get_dashboard_tournament_ids(self, party_id, user_id, *, include_all):
        self.calls.append(('ids', party_id, user_id, include_all))
        return self.everything if include_all else self.assigned

    def query_dashboard_matches(self, scope, query, *, now, settings):
        self.calls.append('query')
        self.seen = {
            'scope': scope,
            'query': query,
            'now': now,
            'settings': settings,
        }
        if self.fail_with is not None:
            raise self.fail_with
        return self.data


class Environment:
    def __init__(self, monkeypatch, fake, permissions):
        self.fake = fake
        self.permissions: dict[UserID, frozenset[str]] = permissions
        self.permission_reads: list[UserID] = []
        self.users: dict = {}
        self.user_reads: list = []
        self.clock = [NOW]

        def get_permissions(user_id):
            fake.calls.append('permissions')
            self.permission_reads.append(user_id)
            return self.permissions.get(user_id, frozenset())

        def get_users(user_ids):
            fake.calls.append('users')
            self.user_reads.append(set(user_ids))
            return {u: self.users[u] for u in user_ids if u in self.users}

        def operation_time():
            fake.calls.append('now')
            return self.clock[0]

        monkeypatch.setattr(service, 'dashboard_repository', fake)
        monkeypatch.setattr(
            service, 'get_permissions_for_user', get_permissions
        )
        monkeypatch.setattr(
            service.user_service, 'get_users_indexed_by_id', get_users
        )
        monkeypatch.setattr(
            service.tournament_repository, 'get_operation_time', operation_time
        )
        monkeypatch.setattr(service, 'gettext', lambda text: f'T[{text}]')


@pytest.fixture
def make_environment(monkeypatch):
    def _make(*, assigned=(), everything=(), data=None, permissions=None):
        fake = FakeRepository(
            assigned=assigned, everything=everything, data=data
        )
        return Environment(monkeypatch, fake, permissions or {})

    return _make


def _page(env, viewer, **fields):
    return service.get_dashboard_page(
        viewer,
        PARTY,
        _query(**fields),
        settings=SETTINGS,
        now=NOW,
    )


# -- scope --


def test_scoped_orga_cannot_widen_party_or_assignments(make_environment):
    orga = _user('Mara')
    admin = _user('Alex')
    stranger = _user('Jonas')
    viewer_only = _user('Tess')
    env = make_environment(
        assigned=[T1],
        everything=[T1, T2, T3],
        permissions={
            admin.id: frozenset({ADMIN_PERMISSION}),
            viewer_only.id: frozenset({'lan_tournament.view'}),
        },
    )
    calls = env.fake.calls

    # A scoped orga gets their assignments, whatever scope they name, and
    # a claim carried by the viewer object is not their permission.
    for claimed in ((), (ADMIN_PERMISSION,)):
        for requested in ('assigned', 'all', 'bogus', ''):
            calls.clear()
            scope = service.resolve_dashboard_scope(
                _viewer(orga, *claimed), PARTY, requested
            ).unwrap()
            assert scope.kind == 'assigned'
            assert scope.tournament_ids == (T1,)
            assert scope.party_id == PARTY
            assert scope.user_id == orga.id
            assert not scope.is_global_admin
            assert ('ids', PARTY, orga.id, False) in calls
            assert ('ids', PARTY, orga.id, True) not in calls

    # Only a global administrator widens it, and only if they ask.
    wide = service.resolve_dashboard_scope(
        _viewer(admin), PARTY, 'all'
    ).unwrap()
    assert (wide.kind, wide.tournament_ids) == ('all', (T1, T2, T3))
    assert wide.is_global_admin
    for requested in ('assigned', 'bogus'):
        narrow = service.resolve_dashboard_scope(
            _viewer(admin), PARTY, requested
        ).unwrap()
        assert (narrow.kind, narrow.tournament_ids) == ('assigned', (T1,))
        assert narrow.is_global_admin

    # The decision reads the permissions afresh on every call.
    env.permission_reads.clear()
    for _ in range(3):
        service.resolve_dashboard_scope(_viewer(admin), PARTY, 'all')
    assert env.permission_reads == [admin.id] * 3
    env.permissions[admin.id] = frozenset()
    revoked = service.resolve_dashboard_scope(
        _viewer(admin, ADMIN_PERMISSION), PARTY, 'all'
    ).unwrap()
    assert (revoked.kind, revoked.tournament_ids) == ('assigned', (T1,))
    assert not revoked.is_global_admin

    # Nobody without authority gets a scope, and they ask for none.
    env.fake.assigned = ()
    for user in (stranger, viewer_only):
        calls.clear()
        result = service.resolve_dashboard_scope(
            _viewer(user, ADMIN_PERMISSION), PARTY, 'all'
        )
        assert result.unwrap_err() == service.DASHBOARD_FORBIDDEN_ERROR
        assert all(c[3] is False for c in calls if isinstance(c, tuple)), (
            'a user without authority must not read the whole party'
        )

    calls.clear()
    anonymous = CurrentUser.create_anonymous(None)
    result = service.resolve_dashboard_scope(anonymous, PARTY, 'all')
    assert result.unwrap_err() == service.DASHBOARD_UNAUTHENTICATED_ERROR
    assert calls == []

    # The page asks the same, inside its snapshot, and stops at a refusal.
    result = _page(env, _viewer(stranger))
    assert result.unwrap_err() == service.DASHBOARD_FORBIDDEN_ERROR
    assert 'query' not in calls
    assert calls[0] == 'enter'
    assert calls[-1] == 'exit'


# -- the SQL is built from the policy --


def test_sql_due_ids_equal_pure_policy():
    # Names that the pure policy decides by, as the SQL spells them.
    assert set(repository._DEMAND_STATUSES) == {
        status.name for status in policy._DEMAND_STATUSES
    }
    assert set(repository._ELIMINATION_MODES) == {
        mode.name for mode in policy._ELIMINATION_MODES
    }
    assert repository._ROUND_ROBIN == EliminationMode.ROUND_ROBIN.name
    assert repository._ONE_V_ONE == GameFormat.ONE_V_ONE.name
    assert repository._FREE_FOR_ALL == GameFormat.FREE_FOR_ALL.name
    assert repository._GRAND_FINAL == Bracket.GRAND_FINAL.value
    assert repository._WINNERS == Bracket.WINNERS.value
    assert repository._LOSERS == Bracket.LOSERS.value
    assert set(repository._BRACKETS) == {b.value for b in Bracket}
    for round_number in (1, 2, 17):
        assert (
            f'{repository._WINNERS_TARGET_PREFIX}{round_number}'
            == policy._winners_target(round_number)
        )

    sql = _facts_sql()
    # Round robin: per tournament, phase and group, over open matches.
    assert (
        _normalized(sql).count(
            'min(CASE WHEN (lan_tournament_matches.confirmed_by IS NULL'
            ' AND lan_tournament_matches.round IS NOT NULL)'
            ' THEN lan_tournament_matches.round END) OVER (PARTITION BY'
            ' lan_tournament_matches.tournament_id,'
            ' lan_tournament_matches.phase,'
            ' lan_tournament_matches.group_order)'
        )
        == 1
    )
    # Free-for-all: the latest round of a pool is read over all its matches.
    assert 'max(lan_tournament_matches.round) OVER (PARTITION BY' in sql
    assert 'bool_or(' in sql
    # Only matches of a phase that runs a match format are fixtures.
    assert "IN ('ONE_V_ONE', 'FREE_FOR_ALL')" in sql
    # The pure policy never depends on a conflict or a clock fact in
    # the due set, so neither may the facts it is judged by.
    assert 'lan_tournament_match_escalation_acks' in sql
    due_sql = _normalized(_demand_sql())
    for status in policy._DEMAND_STATUSES:
        assert f"'{status.name}'" in due_sql


def test_the_vocabulary_of_views_and_states_is_complete():
    assert set(repository._VIEW_STATES) == set(DASHBOARD_VIEWS)
    every_state = {state.value for state in DashboardRowState}
    assert set(repository._VIEW_STATES['all']) == every_state
    due = set(repository._VIEW_STATES['due'])
    upcoming = set(repository._VIEW_STATES['upcoming'])
    assert due == {'due', 'unknown'}
    assert due.isdisjoint(upcoming)
    # A paused match is in no list of its own, only in the whole one.
    assert 'paused' not in due | upcoming
    assert set(repository._STATE_PREDICATES) == set(DASHBOARD_STATES)


# -- order, counts and paging, as statements --


def _relation(at=NOW):
    demand = repository._party_demand(PARTY, at)
    users = repository._demand_users(demand)
    return repository._relation(_scope(T1, T2), SETTINGS, demand, users)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={'literal_binds': True},
        )
    )


def _normalized(sql: str) -> str:
    return ' '.join(sql.split())


def _facts_sql() -> str:
    return _sql(repository.select(repository._facts(PARTY)))


def _demand_sql() -> str:
    facts = repository._facts(PARTY)
    return _sql(repository.select(repository._demand(facts, NOW)))


def test_sort_and_counts_precede_stable_pagination():
    relation = _relation()
    for sort in DASHBOARD_SORTS:
        query = _query(sort=sort, page=3, per_page=7)
        statement = repository._rows_statement(relation, query)
        sql = _normalized(_sql(statement))

        order_by = sql.rsplit('ORDER BY ', 1)[1]
        terms = [t.strip() for t in order_by.split(' LIMIT ')[0].split(',')]
        # The last term of every order is the unique match ID.
        assert terms[-1] == 'dashboard.match_id ASC', sort
        # Nulls and the label of a match have a defined place.
        assert 'NULLS LAST' in order_by
        assert 'dashboard.tournament_id ASC' in order_by or sort == 'wait'
        assert sql.endswith('LIMIT 7 OFFSET 14'), sort

        # The counts read the whole result: no cut, and no order.
        counts = _normalized(
            _sql(repository._counts_statement(relation, query))
        )
        assert counts.endswith('FROM dashboard')
        assert 'OFFSET' not in counts

    # The order is a pure function of the query: no hidden state.
    query = _query(sort='urgency')
    assert _sql(repository._rows_statement(relation, query)) == _sql(
        repository._rows_statement(_relation(), query)
    )

    with pytest.raises(ValueError):
        repository._sort_order(relation, 'newest')

    # Urgency: tier, conflict, alert interval, total wait, then the names.
    urgency = _normalized(
        _sql(repository._rows_statement(relation, _query(sort='urgency')))
    ).rsplit('ORDER BY ', 1)[1]
    columns = [
        'dashboard.urgency_rank ASC',
        'dashboard.has_conflict DESC',
        'dashboard.alert_us DESC NULLS LAST',
        'dashboard.total_wait_us DESC NULLS LAST',
        'dashboard.tournament_sort_name ASC',
    ]
    positions = [urgency.index(column) for column in columns]
    assert positions == sorted(positions)

    wait = _normalized(
        _sql(repository._rows_statement(relation, _query(sort='wait')))
    ).rsplit('ORDER BY ', 1)[1]
    assert wait.startswith('dashboard.total_wait_us DESC NULLS LAST')
    assert 'alert_us' not in wait


def test_a_page_is_cut_from_the_ordered_filtered_result(make_environment):
    records = [_record() for _ in range(3)]
    env = make_environment(
        everything=[T1],
        assigned=[T1],
        data=_data(records, total_count=7, view_total_count=9),
        permissions={},
    )
    orga = _user()

    page = _page(env, _viewer(orga), page=2, per_page=3).unwrap()

    # The query reaches the repository as given: the service adds no
    # filter, order or clamp of its own.
    assert env.fake.seen['query'] == _query(page=2, per_page=3)
    assert env.fake.seen['settings'] == SETTINGS
    assert page.total_count == 7
    assert page.total_pages == 3
    assert (page.page, page.per_page) == (2, 3)
    assert [row.match_id for row in page.rows] == [
        record.match_id for record in records
    ]


# -- no fake tier --


def test_unknown_history_has_no_fake_green_tier(make_environment):
    unknown = _record(
        state=DashboardRowState.UNKNOWN,
        total_active_wait_us=None,
        alert_interval_us=None,
    )
    # Even a stray duration on such a record is no tier.
    stray = _record(
        state=DashboardRowState.UNKNOWN,
        total_active_wait_us=0,
        alert_interval_us=0,
    )
    paused = _record(
        state=DashboardRowState.PAUSED,
        total_active_wait_us=20 * MINUTE_US,
        alert_interval_us=50 * MINUTE_US,
    )
    due_without_interval = _record(
        state=DashboardRowState.DUE, alert_interval_us=None
    )
    env = make_environment(
        assigned=[T1],
        data=_data([unknown, stray, paused, due_without_interval]),
    )

    page = _page(env, _viewer(_user())).unwrap()

    rows = {row.match_id: row for row in page.rows}
    for record in (unknown, stray):
        row = rows[record.match_id]
        assert row.tier is None
        assert row.ack_unavailable_reason is AckUnavailableReason.CLOCK_UNKNOWN
    assert rows[unknown.match_id].alert_interval_us is None
    assert rows[unknown.match_id].total_active_wait_us is None
    # A frozen row shows its waits but no tier, however long it waited.
    assert rows[paused.match_id].tier is None
    assert rows[paused.match_id].alert_interval_us == 50 * MINUTE_US
    assert rows[due_without_interval.match_id].tier is None


# fmt: off
@pytest.mark.parametrize('alert_minutes, expected', [
    (0, TrafficTier.GREEN),
    (14.999999, TrafficTier.GREEN),
    (15, TrafficTier.YELLOW),
    (44.999999, TrafficTier.YELLOW),
    (45, TrafficTier.RED),
    (600, TrafficTier.RED),
])
# fmt: on
def test_the_displayed_tier_is_the_pure_policy_of_the_settings(
    make_environment, alert_minutes, expected
):
    record = _record(
        state=DashboardRowState.DUE,
        alert_interval_us=int(alert_minutes * MINUTE_US),
    )
    env = make_environment(assigned=[T1], data=_data([record]))

    row = _page(env, _viewer(_user())).unwrap().rows[0]

    assert row.tier is expected
    assert row.tier is policy.derive_traffic_tier(
        record.alert_interval_us, SETTINGS
    )


# -- counts that ignore the query --


def test_tier_counts_ignore_filters_and_page():
    relation = _relation()
    queries = [
        _query(),
        _query(view='all'),
        _query(view='upcoming'),
        _query(state='tier-red'),
        _query(state='pinned', tournament_id=uuid4()),
        _query(sort='wait', page=9, per_page=3),
    ]
    columns = ('red', 'yellow', 'green', 'paused', 'pre_start', 'partial')

    def rendered(query):
        statement = repository._counts_statement(relation, query)
        return {
            name: _sql(statement.selected_columns[name].element)
            for name in columns
        }

    reference = rendered(queries[0])
    for query in queries[1:]:
        assert rendered(query) == reference

    # The totals do follow the query.
    totals = {
        _sql(
            repository._counts_statement(relation, q).selected_columns[
                'total'
            ].element
        )
        for q in queries
    }
    assert len(totals) >= 4


def test_the_counts_come_from_the_scope_only():
    sql = _sql(repository._counts_statement(_relation(), _query()))
    assert str(T1) in sql and str(T2) in sql
    assert str(T3) not in sql
    assert f"'{PARTY}'" in sql


# -- state filters --


def test_state_filters_and_urgency_sort_precede_pagination():
    relation = _relation()
    for state in DASHBOARD_STATES:
        query = _query(state=state, per_page=5, page=2)
        sql = _normalized(_sql(repository._rows_statement(relation, query)))

        where = sql.rsplit(' WHERE ', 1)[1].split(' ORDER BY ')[0]
        assert where, state
        # filter, then sort, then the cut
        assert sql.index(' WHERE ') < sql.index(' ORDER BY ')
        assert sql.index(' ORDER BY ') < sql.index(' LIMIT ')
        if state in ('tier-red', 'tier-yellow', 'tier-green'):
            assert "dashboard.tier = '" in where
        if state.startswith('ready-'):
            assert 'dashboard.pairing_valid' in where
        if state == 'pinned':
            assert 'dashboard.pinned_at IS NOT NULL' in where
        if state == 'review-open':
            assert 'false' in where.lower()

        counts = _sql(repository._counts_statement(relation, query))
        # The total counts the very same predicate.
        assert where.split(' AND ')[-1] in counts or state == 'all'

    # An unknown state is no filter at all.
    with pytest.raises(KeyError):
        repository._rows_statement(relation, _query(state='everything'))

    # Every allowlisted parameter is valid, nothing else is.
    for field, values in (
        ('scope', DASHBOARD_SCOPES),
        ('view', DASHBOARD_VIEWS),
        ('state', DASHBOARD_STATES),
        ('sort', DASHBOARD_SORTS),
    ):
        for value in values:
            assert service._check_query(_query(**{field: value})) is None
        assert (
            service._check_query(_query(**{field: 'nonsense'}))
            == service.DASHBOARD_QUERY_INVALID_ERROR
        )


# fmt: off
@pytest.mark.parametrize('fields', [
    {'page': 0}, {'page': -3}, {'page': True}, {'page': 1.0},
    {'page': repository.MAX_PAGE_NUMBER + 1},
    {'per_page': 0}, {'per_page': -1}, {'per_page': 101},
    {'per_page': False}, {'per_page': '5'},
    {'tournament_id': 'not-a-uuid'}, {'tournament_id': ''},
])
# fmt: on
def test_a_query_that_its_parser_would_refuse_is_refused(
    make_environment, fields
):
    env = make_environment(assigned=[T1])

    result = _page(env, _viewer(_user()), **fields)

    assert result.unwrap_err() == service.DASHBOARD_QUERY_INVALID_ERROR
    assert env.fake.calls == []


def test_a_tournament_filter_may_arrive_as_a_string(make_environment):
    env = make_environment(assigned=[T1], data=_data())

    result = _page(env, _viewer(_user()), tournament_id=str(T2))

    assert result.is_ok()
    assert env.fake.seen['query'].tournament_id == str(T2)


# -- empty and out-of-range pages --

_NONE = DashboardNonActionableCounts()
_SOME = DashboardNonActionableCounts(paused=1, pre_start=0, partial=2)


# fmt: off
@pytest.mark.parametrize(
    'scope_ids, query, total, view_total, counts, expected', [
        # Anything listed is not an empty page, whatever the page number.
        ((T1,), {}, 3, 3, _SOME, None),
        ((T1,), {'page': 9}, 3, 3, _SOME, None),
        ((), {}, 1, 1, _NONE, None),
        # No tournament in scope.
        ((), {}, 0, 0, _NONE, 'no_assignment'),
        ((), {'state': 'pinned', 'view': 'all'}, 0, 0, _SOME,
         'no_assignment'),
        # A filter emptied a view that is not empty.
        ((T1,), {'state': 'tier-red'}, 0, 4, _NONE, 'no_filter_matches'),
        ((T1,), {'tournament_id': T2}, 0, 4, _SOME, 'no_filter_matches'),
        ((T1,), {'state': 'pinned', 'view': 'all'}, 0, 1, _NONE,
         'no_filter_matches'),
        # A filter on a view that is empty anyway is no reason of its own.
        ((T1,), {'state': 'pinned'}, 0, 0, _SOME, 'no_actionable_fixtures'),
        ((T1,), {'state': 'pinned'}, 0, 0, _NONE, 'no_current_demand'),
        # Fixtures exist, but none is due.
        ((T1,), {}, 0, 0, _SOME, 'no_actionable_fixtures'),
        ((T1,), {}, 0, 0, DashboardNonActionableCounts(paused=1),
         'no_actionable_fixtures'),
        ((T1,), {}, 0, 0, DashboardNonActionableCounts(pre_start=1),
         'no_actionable_fixtures'),
        ((T1,), {}, 0, 0, DashboardNonActionableCounts(partial=1),
         'no_actionable_fixtures'),
        # Only the list of what is due is explained by them.
        ((T1,), {'view': 'upcoming'}, 0, 0, _SOME, 'no_current_demand'),
        ((T1,), {'view': 'all'}, 0, 0, _SOME, 'no_current_demand'),
        # Nothing at all is due or waiting.
        ((T1,), {}, 0, 0, _NONE, 'no_current_demand'),
    ],
)
# fmt: on
def test_page_beyond_last_is_not_clamped_and_empty_reason_is_distinct(
    make_environment, scope_ids, query, total, view_total, counts, expected
):
    permissions = {}
    orga = _user()
    permissions[orga.id] = frozenset({ADMIN_PERMISSION})
    env = make_environment(
        assigned=scope_ids,
        everything=scope_ids,
        data=_data(
            [_record() for _ in range(min(total, 2))],
            total_count=total,
            view_total_count=view_total,
            non_actionable_counts=counts,
        ),
        permissions=permissions,
    )

    page = _page(env, _viewer(orga), **query).unwrap()

    assert page.empty_reason == expected
    # The requested page is reported as it was, and never clamped.
    assert page.page == query.get('page', 1)
    assert page.total_count == total
    assert page.total_pages == -(-total // 50)


# fmt: off
@pytest.mark.parametrize('total, per_page, pages', [
    (0, 50, 0), (1, 50, 1), (50, 50, 1), (51, 50, 2), (101, 25, 5),
    (7, 1, 7), (100, 100, 1),
])
# fmt: on
def test_the_page_count_is_the_ceiling(
    make_environment, total, per_page, pages
):
    env = make_environment(
        assigned=[T1], data=_data([], total_count=total, view_total_count=total)
    )

    page = _page(env, _viewer(_user()), per_page=per_page, page=999).unwrap()

    assert page.total_pages == pages
    assert page.page == 999


# -- non-actionable counts and note cards --


def test_non_actionable_counts_and_leaderboard_cards_are_scoped(
    make_environment,
):
    card = DashboardTournamentRef(tournament_id=T2, name='Comet Run', game='X')
    choice = DashboardTournamentRef(tournament_id=T1, name='Kupfer-Cup')
    counts = DashboardNonActionableCounts(paused=2, pre_start=1, partial=4)
    tiers = DashboardTierCounts(red=1, yellow=2, green=3)
    env = make_environment(
        assigned=[T1],
        data=_data(
            [_record()],
            non_actionable_counts=counts,
            tier_counts=tiers,
            tournament_choices=(choice,),
            leaderboard_only_tournaments=(card,),
        ),
    )

    page = _page(env, _viewer(_user()), view='all').unwrap()

    # The service hands on what the scoped query returned, nothing else.
    assert page.non_actionable_counts == counts
    assert page.tier_counts == tiers
    assert page.tournament_choices == (choice,)
    assert page.leaderboard_only_tournaments == (card,)
    assert env.fake.seen['scope'].tournament_ids == (T1,)


def test_a_scope_without_tournaments_reads_nothing():
    session = MagicMock()
    with patch.object(repository.db, 'session', session):
        data = repository.query_dashboard_matches(
            _scope(), _query(), now=NOW, settings=SETTINGS
        )

    assert session.mock_calls == []
    assert data.records == ()
    assert data.total_count == 0
    assert data.tournament_choices == ()
    assert data.leaderboard_only_tournaments == ()


def test_the_statements_stay_inside_the_party_and_the_scope():
    scope = _scope(T1, T2)
    sql = _normalized(_sql(repository._rows_statement(_relation(), _query())))
    # The party is judged whole, then the list narrows to the scope.
    assert f"lan_tournaments.party_id = '{PARTY}'" in sql
    assert f"party_demand.tournament_id IN ('{T1}', '{T2}')" in sql
    assert str(T3) not in sql
    assert scope.tournament_ids == (T1, T2)


def test_the_due_oracle_is_evaluated_once_per_snapshot():
    demand = repository._party_demand(PARTY, NOW)
    users = repository._demand_users(demand)
    relation = repository._relation(_scope(T1, T2), SETTINGS, demand, users)
    queries = [
        _query(),
        _query(view='all', state='conflict', sort='wait'),
        _query(sort='tournament', page=3, per_page=7, tournament_id=T1),
    ]
    for query in queries:
        statement = repository._page_statement(relation, users, query)
        sql = _normalized(_sql(statement))

        # The counts, the rows and the conflicts are one statement ...
        for kind in ('counts', 'row', 'conflict'):
            assert sql.count(f"'{kind}' AS kind") == 1, kind
        # ... that judges the party once: one chain of facts, one oracle,
        # read by the lists and by the demand alike.
        assert sql.count('WITH party_facts AS (') == 1
        assert sql.count(', party_facts AS (') == 0
        assert sql.count('party_demand AS MATERIALIZED (') == 1
        assert sql.count(' FROM party_facts') == 1
        assert sql.count('bool_or(') == 1
        assert sql.count('max(lan_tournament_matches.round) OVER') == 1
        assert sql.count('FROM party_demand') >= 3


def test_stacked_parts_name_their_kind_and_agree_on_their_columns():
    ones = repository.select(repository.literal(1).label('x'))
    texts = repository.select(repository.literal('t').label('y'))

    sql = _normalized(_sql(repository._stacked({'ones': ones, 'texts': texts})))

    # A column that a part lacks is a typed `NULL`, whichever part leads.
    assert "'ones' AS kind" in sql and "'texts' AS kind" in sql
    assert 'CAST(NULL AS VARCHAR) AS y' in sql
    assert 'CAST(NULL AS INTEGER) AS x' in sql
    # A name that means two things is refused, not guessed.
    clash = repository.select(repository.literal('t').label('x'))
    with pytest.raises(ValueError):
        repository._stacked({'ones': ones, 'clash': clash})


# -- one snapshot --


def test_snapshot_reads_are_consistent_across_statements(make_environment):
    orga = _user()
    env = make_environment(assigned=[T1], data=_data([_record()]))
    calls = env.fake.calls

    # Without a time, the snapshot reads the server clock first.
    env.clock[0] = NOW + timedelta(seconds=7)
    page = service.get_dashboard_page(
        _viewer(orga), PARTY, _query(), settings=SETTINGS
    ).unwrap()
    assert page.as_of == NOW + timedelta(seconds=7)
    assert env.fake.seen['now'] == page.as_of
    assert calls[:3] == ['enter', 'now', 'permissions']
    assert calls[-1] == 'exit'
    for name in ('permissions', 'query', 'users'):
        assert calls.index('enter') < calls.index(name) < calls.index('exit')
    assert calls.count('enter') == calls.count('exit') == 1

    # A given time is used as it is, aware times as naive UTC.
    calls.clear()
    aware = datetime(2026, 10, 7, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    page = service.get_dashboard_page(
        _viewer(orga), PARTY, _query(), settings=SETTINGS, now=aware
    ).unwrap()
    assert page.as_of == datetime(2026, 10, 7, 12, 0, 0)
    assert page.as_of.tzinfo is None
    assert 'now' not in calls

    # A refusal and a failure both leave the snapshot.
    calls.clear()
    env.fake.fail_with = RuntimeError('boom')
    with pytest.raises(RuntimeError):
        service.get_dashboard_page(
            _viewer(orga), PARTY, _query(), settings=SETTINGS, now=NOW
        )
    assert calls[0] == 'enter'
    assert calls[-1] == 'exit'
    env.fake.fail_with = None
    calls.clear()
    stranger = _user('Stranger')
    env.fake.assigned = ()
    result = service.get_dashboard_page(
        _viewer(stranger), PARTY, _query(), settings=SETTINGS, now=NOW
    )
    assert result.is_err()
    assert calls[0] == 'enter'
    assert calls[-1] == 'exit'

    # A query that is refused never opens one.
    calls.clear()
    result = service.get_dashboard_page(
        _viewer(orga), PARTY, _query(page=0), settings=SETTINGS, now=NOW
    )
    assert result.is_err()
    assert calls == []


def test_the_snapshot_is_read_only_repeatable_read_and_transaction_scoped():
    session = MagicMock()
    session.new = session.dirty = session.deleted = ()
    with patch.object(repository.db, 'session', session):
        with repository.read_snapshot():
            pass

    options = {
        'isolation_level': 'REPEATABLE READ',
        'postgresql_readonly': True,
    }
    # The transaction of the request ends, the next one carries the
    # options, and it ends again. Nothing sets a session variable.
    assert session.mock_calls == [
        call.rollback(),
        call.connection(execution_options=options),
        call.rollback(),
    ]
    assert not any(
        name in repr(session.mock_calls)
        for name in ('SET ', 'execute', 'commit')
    )

    # An error inside still ends the transaction.
    session.reset_mock()
    with patch.object(repository.db, 'session', session):
        with pytest.raises(KeyError):
            with repository.read_snapshot():
                raise KeyError('inside')
    assert session.mock_calls[-1] == call.rollback()

    # Pending changes are never dropped silently.
    session.reset_mock()
    session.new = [object()]
    with patch.object(repository.db, 'session', session):
        with pytest.raises(RuntimeError):
            with repository.read_snapshot():
                pytest.fail('the snapshot must not start')
    assert session.mock_calls == []


# -- the facts of a row --


def test_a_row_is_assembled_from_its_record_and_batches(make_environment):
    side_a = _user('Side A')
    side_b = _user('Side B')
    gone = _user(None, deleted=True)
    orga = _user('Mara')
    pinner = _user('Alex')
    episode_id = MatchDueEpisodeID(generate_uuid())
    record = _record(
        state=DashboardRowState.DUE,
        episode_id=episode_id,
        episode_opened_at=NOW - timedelta(minutes=40),
        ack_revision=4,
        total_active_wait_us=40 * MINUTE_US,
        alert_interval_us=16 * MINUTE_US,
        readiness_available=True,
        ready_at_a=NOW - timedelta(minutes=3),
        side_a_identity='participant:b',
        pinned_at=NOW - timedelta(minutes=5),
        pinned_by=pinner.id,
        pin_revision=2,
        has_prior_episode=True,
        reopened_same_pairing=True,
        closed_episode_wait_us=None,
    )

    def contestant(identity, user, name=None):
        return DashboardContestantRecord(
            match_id=record.match_id,
            identity=identity,
            participant_id=generate_uuid() if user else None,
            team_id=None if user else generate_uuid(),
            user_id=user.id if user else None,
            team_name=name,
            score=None,
        )

    def ack(revision, actor, comment, count):
        return DashboardAcknowledgementRecord(
            id=MatchEscalationAcknowledgementID(generate_uuid()),
            episode_id=episode_id,
            revision=revision,
            actor_id=actor,
            occurred_at=NOW - timedelta(minutes=20 - revision),
            comment=comment,
            episode_count=count,
        )

    acks = (
        ack(4, gone.id, 'c4', 4),
        ack(3, orga.id, None, 4),
        ack(2, UserID(generate_uuid()), 'c2', 4),
    )
    env = make_environment(
        assigned=[T1],
        data=_data(
            [record],
            contestants={
                record.match_id: (
                    contestant('participant:a', side_a),
                    contestant('participant:b', side_b),
                )
            },
            orga_user_ids={T1: (orga.id, gone.id, pinner.id)},
            acknowledgements={episode_id: acks},
        ),
    )
    for user in (side_a, side_b, gone, orga, pinner):
        env.users[user.id] = user

    row = _page(env, _viewer(orga)).unwrap().rows[0]

    # Side A first, as the pairing says.
    assert row.contestant_names == ('Side B', 'Side A')
    assert row.orga_names == ('Mara', 'Alex')
    assert row.pinned_by_name == 'Alex'
    assert (row.pin_revision, row.pinned_at) == (2, NOW - timedelta(minutes=5))
    assert row.tier is TrafficTier.YELLOW
    assert row.ack_unavailable_reason is None
    assert row.acknowledgement_count == 4
    assert [a.revision for a in row.recent_acknowledgements] == [4, 3, 2]
    assert row.latest_acknowledgement == row.recent_acknowledgements[0]
    assert [a.actor_display_name for a in row.recent_acknowledgements] == [
        'T[Deleted orga]',
        'Mara',
        'T[Deleted orga]',
    ]
    assert [a.comment for a in row.recent_acknowledgements] == [
        'c4',
        None,
        'c2',
    ]
    assert row.readiness_available
    assert row.ready_at_a == NOW - timedelta(minutes=3)
    assert row.status_note.code == service.STATUS_NOTE_CORRECTED_REOPENED
    assert row.has_prior_episode
    assert row.conflicts == ()
    assert (row.review_available, row.review_open) == (False, False)
    # One batch of users for the whole page, nobody twice.
    assert env.user_reads == [
        {side_a.id, side_b.id, orga.id, gone.id, pinner.id, acks[2].actor_id}
    ]


def test_a_team_and_a_missing_participant_are_named(make_environment):
    record = _record()
    ghost = DashboardContestantRecord(
        match_id=record.match_id,
        identity='participant:x',
        participant_id=generate_uuid(),
        team_id=None,
        user_id=UserID(generate_uuid()),
        team_name=None,
        score=None,
    )
    team = DashboardContestantRecord(
        match_id=record.match_id,
        identity='team:y',
        participant_id=None,
        team_id=generate_uuid(),
        user_id=None,
        team_name='Kupferfüchse',
        score=None,
    )
    env = make_environment(
        assigned=[T1],
        data=_data([record], contestants={record.match_id: (ghost, team)}),
    )

    row = _page(env, _viewer(_user())).unwrap().rows[0]

    assert row.contestant_names == ('T[Deleted user]', 'Kupferfüchse')


# fmt: off
@pytest.mark.parametrize('state, tier_minutes, acked, expected', [
    (DashboardRowState.DONE, None, False, AckUnavailableReason.TERMINAL),
    (DashboardRowState.BYE, None, False, AckUnavailableReason.TERMINAL),
    (DashboardRowState.PAUSED, 30, False, AckUnavailableReason.PAUSED),
    (DashboardRowState.UNKNOWN, None, False,
     AckUnavailableReason.CLOCK_UNKNOWN),
    (DashboardRowState.UPCOMING, None, False, AckUnavailableReason.NOT_DUE),
    (DashboardRowState.PARTIAL, None, False, AckUnavailableReason.NOT_DUE),
    (DashboardRowState.AWAITING_LOBBY, None, False,
     AckUnavailableReason.NOT_DUE),
    (DashboardRowState.DUE, 0, False, AckUnavailableReason.BELOW_THRESHOLD),
    (DashboardRowState.DUE, 14, False, AckUnavailableReason.BELOW_THRESHOLD),
    (DashboardRowState.DUE, 3, True,
     AckUnavailableReason.RECENTLY_ACKNOWLEDGED),
    (DashboardRowState.DUE, 15, False, None),
    (DashboardRowState.DUE, 15, True, None),
    (DashboardRowState.DUE, 45, True, None),
    (DashboardRowState.DUE, 90, False, None),
])
# fmt: on
def test_the_reason_an_acknowledgement_is_unavailable(
    make_environment, state, tier_minutes, acked, expected
):
    episode_id = MatchDueEpisodeID(generate_uuid())
    record = _record(
        state=state,
        episode_id=episode_id if acked or tier_minutes is not None else None,
        alert_interval_us=(
            None if tier_minutes is None else tier_minutes * MINUTE_US
        ),
    )
    acknowledgements = {}
    if acked:
        acknowledgements[episode_id] = (
            DashboardAcknowledgementRecord(
                id=MatchEscalationAcknowledgementID(generate_uuid()),
                episode_id=episode_id,
                revision=1,
                actor_id=UserID(generate_uuid()),
                occurred_at=NOW,
                comment=None,
                episode_count=1,
            ),
        )
    env = make_environment(
        assigned=[T1],
        data=_data([record], acknowledgements=acknowledgements),
    )

    row = _page(env, _viewer(_user())).unwrap().rows[0]

    assert row.ack_unavailable_reason is expected


# fmt: off
@pytest.mark.parametrize('fields, code, params', [
    ({'state': DashboardRowState.UPCOMING, 'earlier_open_round': 2},
     'earlier_round_open', {'round': 2}),
    ({'state': DashboardRowState.UPCOMING}, None, None),
    ({'state': DashboardRowState.PARTIAL}, None, None),
    ({'state': DashboardRowState.BYE, 'confirmed': True},
     'bye_advance', {}),
    ({'state': DashboardRowState.AWAITING_LOBBY, 'contestant_count': 2,
      'lobby_size': 4,
      'location': DashboardMatchLocation(phase=1, round=3)},
     'lobby_waiting', {'filled': 2, 'size': 4, 'after_round': 2}),
    # FFA rounds are zero-based: a lobby of round 1 waits for round 0.
    ({'state': DashboardRowState.AWAITING_LOBBY, 'contestant_count': 1,
      'location': DashboardMatchLocation(phase=1, round=1)},
     'lobby_waiting', {'filled': 1, 'after_round': 0}),
    ({'state': DashboardRowState.AWAITING_LOBBY, 'contestant_count': 3,
      'lobby_size': 4,
      'location': DashboardMatchLocation(phase=1, round=1)},
     'lobby_waiting', {'filled': 3, 'size': 4, 'after_round': 0}),
    ({'state': DashboardRowState.AWAITING_LOBBY, 'contestant_count': 1,
      'location': DashboardMatchLocation(phase=1, round=0)},
     'lobby_waiting', {'filled': 1}),
    ({'state': DashboardRowState.AWAITING_LOBBY, 'contestant_count': 1,
      'location': DashboardMatchLocation(phase=1)},
     'lobby_waiting', {'filled': 1}),
    ({'state': DashboardRowState.DUE, 'reopened_same_pairing': True},
     'corrected_reopened', {}),
    ({'state': DashboardRowState.PAUSED, 'reopened_same_pairing': True},
     'corrected_reopened', {}),
    ({'state': DashboardRowState.DONE, 'confirmed': True,
      'reopened_same_pairing': True}, None, None),
])
# fmt: on
def test_status_notes_carry_their_parameters(
    make_environment, fields, code, params
):
    env = make_environment(assigned=[T1], data=_data([_record(**fields)]))

    row = _page(env, _viewer(_user())).unwrap().rows[0]

    if code is None:
        assert row.status_note is None
    else:
        assert row.status_note.code == code
        assert dict(row.status_note.params) == params


def test_a_confirmed_result_names_the_scores_in_side_order(make_environment):
    record = _record(
        state=DashboardRowState.DONE,
        confirmed=True,
        side_a_identity='participant:b',
    )

    def contestant(identity, score):
        return DashboardContestantRecord(
            match_id=record.match_id,
            identity=identity,
            participant_id=None,
            team_id=generate_uuid(),
            user_id=None,
            team_name=identity,
            score=score,
        )

    contestants = {
        record.match_id: (contestant('team:a', 1), contestant('participant:b', 3))
    }
    env = make_environment(
        assigned=[T1], data=_data([record], contestants=contestants)
    )

    row = _page(env, _viewer(_user())).unwrap().rows[0]

    assert row.contestant_names == ('participant:b', 'team:a')
    assert dict(row.status_note.params) == {'score_a': 3, 'score_b': 1}
    # An undecided or missing score names no result.
    contestants = {
        record.match_id: (contestant('team:a', 1), contestant('participant:b', None))
    }
    env = make_environment(
        assigned=[T1], data=_data([record], contestants=contestants)
    )
    assert _page(env, _viewer(_user())).unwrap().rows[0].status_note is None


def test_the_error_codes_are_constants_in_the_catalogue_style():
    codes = {
        service.DASHBOARD_UNAUTHENTICATED_ERROR,
        service.DASHBOARD_FORBIDDEN_ERROR,
        service.DASHBOARD_QUERY_INVALID_ERROR,
    }
    assert len(codes) == 3
    for code in codes:
        assert code.startswith('dashboard_')
        assert code == code.lower()
        assert ' ' not in code
