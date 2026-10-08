"""
tests.unit.services.lan_tournament.test_dashboard_conflicts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta
import re
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    tournament_dashboard_repository as repository,
    tournament_dashboard_service as service,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardConflict,
    DashboardConflictRef,
    DashboardMatchLocation,
    DashboardNonActionableCounts,
    DashboardQuery,
    DashboardRowState,
    DashboardScope,
    DashboardSettings,
    DashboardTierCounts,
)
from byceps.services.lan_tournament.tournament_dashboard_repository import (
    DashboardConflictRecord,
    DashboardConflictRefRecord,
    DashboardContestantRecord,
    DashboardMatchRecord,
    DashboardPageData,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import User, UserID

from tests.helpers import generate_uuid


PARTY = PartyID('gv-36-conflicts')
NOW = datetime(2026, 10, 7, 12, 0, 0)

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

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


def _query(**fields) -> DashboardQuery:
    fields.setdefault('per_page', 50)
    return DashboardQuery(**fields)


def _scope(*tournament_ids) -> DashboardScope:
    return DashboardScope(
        user_id=UserID(generate_uuid()),
        party_id=PARTY,
        kind='assigned',
        tournament_ids=tuple(tournament_ids),
        is_global_admin=False,
    )


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
        occupied_since=None,
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
        has_conflict=True,
    )
    values.update(fields)
    return DashboardMatchRecord(**values)


def _ref(**fields) -> DashboardConflictRefRecord:
    values: dict[str, Any] = dict(
        match_id=generate_uuid(),
        tournament_id=T2,
        tournament_name='Neon-Duell',
        location=DashboardMatchLocation(
            phase=1, bracket=Bracket.WINNERS, round=2, match_order=3
        ),
        side_a_identity=None,
        via_team_name=None,
        list_page=None,
    )
    values.update(fields)
    return DashboardConflictRefRecord(**values)


def _conflict(match_id, user_id, **fields) -> DashboardConflictRecord:
    values: dict[str, Any] = dict(
        match_id=match_id,
        user_id=user_id,
        via_team_name=None,
        refs=(),
        has_external_conflict=False,
    )
    values.update(fields)
    return DashboardConflictRecord(**values)


def _contestant(match_id, user_id=None, *, team_name=None, score=None):
    participant_id = None if team_name else generate_uuid()
    team_id = generate_uuid() if team_name else None
    return DashboardContestantRecord(
        match_id=match_id,
        identity=(
            f'participant:{participant_id}'
            if participant_id
            else f'team:{team_id}'
        ),
        participant_id=participant_id,
        team_id=team_id,
        user_id=user_id,
        team_name=team_name,
        score=score,
    )


def _data(records, **fields) -> DashboardPageData:
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


class FakeRepository:
    """Stands in for the dashboard repository."""

    def __init__(self, data: DashboardPageData) -> None:
        self.data = data

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        yield

    def get_dashboard_tournament_ids(self, party_id, user_id, *, include_all):
        return (T1,)

    def query_dashboard_matches(self, scope, query, *, now, settings):
        return self.data


@pytest.fixture
def build_page(monkeypatch):
    """Provide a function that assembles the page of the given data."""

    def _build(data: DashboardPageData, users: list[User], **query_fields):
        by_id = {user.id: user for user in users}
        monkeypatch.setattr(
            service, 'dashboard_repository', FakeRepository(data)
        )
        monkeypatch.setattr(
            service, 'get_permissions_for_user', lambda user_id: frozenset()
        )
        monkeypatch.setattr(
            service.user_service,
            'get_users_indexed_by_id',
            lambda user_ids: {u: by_id[u] for u in user_ids if u in by_id},
        )
        monkeypatch.setattr(
            service.tournament_repository, 'get_operation_time', lambda: NOW
        )
        monkeypatch.setattr(service, 'gettext', lambda text: f'T[{text}]')

        viewer = CurrentUser.create_authenticated(
            _user('Viewer'), None, frozenset()
        )
        return service.get_dashboard_page(
            viewer, PARTY, _query(**query_fields), settings=SETTINGS, now=NOW
        ).unwrap()

    return _build


def _sql(statement) -> str:
    return ' '.join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={'literal_binds': True},
            )
        ).split()
    )


def _relation(scope=None):
    scope = scope or _scope(T1, T2)
    demand = repository._party_demand(PARTY, NOW)
    users = repository._demand_users(demand)
    return users, repository._relation(scope, SETTINGS, demand, users)


def _demand_users_sql() -> str:
    demand = repository._party_demand(PARTY, NOW)
    return _sql(repository.select(repository._demand_users(demand)))


def _conflicts_sql(query=None, match_ids=None) -> str:
    users, relation = _relation()
    return _sql(
        repository._conflicts_statement(
            relation,
            users,
            query or _query(),
            match_ids or [generate_uuid()],
        )
    )


def _cte_body(sql: str, name: str) -> str:
    """Return the text of the common table expression `name`."""
    marker = f'{name} AS MATERIALIZED ('
    if marker not in sql:
        marker = f'{name} AS ('
    start = sql.index(marker) + len(marker)
    depth = 1
    for position in range(start, len(sql)):
        if sql[position] == '(':
            depth += 1
        elif sql[position] == ')':
            depth -= 1
            if depth == 0:
                return sql[start:position]
    raise AssertionError(f'unterminated CTE {name}')


def _db_row(match_id, user_id, **fields) -> SimpleNamespace:
    """Return a row of the conflicts statement; by default a hidden match."""
    values: dict[str, Any] = dict(
        kind='conflict',
        match_id=match_id,
        user_id=user_id,
        via_team_name=None,
        other_match_id=None,
        other_tournament_id=None,
        other_tournament_name=None,
        other_tournament_sort_name=None,
        other_phase=None,
        other_bracket=None,
        other_bracket_rank=None,
        other_round=None,
        other_group_order=None,
        other_match_order=None,
        other_format=None,
        other_side_a=None,
        other_via_team_name=None,
        position=None,
    )
    values.update(fields)
    return SimpleNamespace(**values)


def _db_counterpart(match_id, user_id, **fields) -> SimpleNamespace:
    """Return a row whose counterpart is a match of the scope."""
    values: dict[str, Any] = dict(
        other_match_id=generate_uuid(),
        other_tournament_id=T2,
        other_tournament_name='Neon-Duell',
        other_tournament_sort_name='neon-duell',
        other_phase=1,
        other_bracket='WB',
        other_bracket_rank=1,
        other_round=2,
        other_match_order=3,
        other_format='ONE_V_ONE',
    )
    values.update(fields)
    return _db_row(match_id, user_id, **values)


def _read_conflicts(rows, query=None):
    """Return what the reader makes of the given rows of the statement."""
    return repository._fold_conflicts(rows, (query or _query()).per_page)


# -- demand beyond the page and the scope --


def test_off_page_same_party_demand_is_detected(build_page):
    scope = _scope(T1, T2)
    sql = _demand_users_sql()

    # The demand is judged over the party, not over the scope or a page.
    assert f"lan_tournaments.party_id = '{PARTY}'" in sql
    assert f"lan_tournaments_1.party_id = '{PARTY}'" in sql
    assert str(T1) not in sql and str(T2) not in sql and str(T3) not in sql
    body = _cte_body(sql, 'demand_users')
    assert ' LIMIT ' not in body and ' OFFSET ' not in body
    assert 'party_facts' in sql and 'party_demand' in sql

    # The relation of the list decides "in conflict" by that demand, in
    # the statement of the counts and of the rows alike.
    users, relation = _relation(scope)
    query = _query(state='conflict', page=2, per_page=3)
    for statement in (
        repository._rows_statement(relation, query),
        repository._counts_statement(relation, query),
    ):
        text = _sql(statement)
        assert 'conflicted_matches' in text and 'demand_users' in text
        assert str(T3) not in text

    # The partners of a listed match are looked up among all demand: the
    # page restricts the listed side only.
    conflicts = _conflicts_sql(match_ids=[generate_uuid(), generate_uuid()])
    assert 'FROM demand_users AS mine JOIN demand_users AS other' in conflicts
    assert conflicts.count(' IN (') >= 1
    where = conflicts.rsplit(' WHERE ', 1)[1].split(' ORDER BY ')[0]
    assert where.startswith('mine.match_id IN (')
    assert 'other.' not in where

    # A counterpart that is on no listed page still becomes a reference.
    user = _user('Mara')
    row = _record()
    far = _ref(tournament_name='Neon-Duell', list_page=3)
    data = _data(
        [row],
        conflicts={
            row.match_id: (_conflict(row.match_id, user.id, refs=(far,)),)
        },
        contestants={far.match_id: (_contestant(far.match_id, user.id),)},
    )

    page = build_page(data, [user])

    (conflict,) = page.rows[0].conflicts
    (ref,) = conflict.visible_refs
    assert ref.match_id == far.match_id
    assert ref.match_id not in {r.match_id for r in page.rows}
    assert ref.list_page == 3
    assert not conflict.has_external_conflict


# -- who is the same person --


def test_solo_and_cross_team_users_share_identity(build_page):
    sql = _demand_users_sql()
    body = _cte_body(sql, 'demand_users')

    # A solo contestant and a member of a team contestant both yield the
    # user of the participant, in one relation.
    branches = body.split(' UNION ALL ')
    assert len(branches) == 2
    solo, member = branches
    assert (
        'lan_tournament_participants.id = lan_tournament_match_contestants.participant_id'
        in solo
    )
    assert (
        'lan_tournament_participants.team_id = lan_tournament_teams.id'
        in member
    )
    for branch in branches:
        assert (
            branch.count('lan_tournament_participants.user_id AS user_id') == 1
        )
    assert 'CAST(NULL AS UUID) AS team_id' in solo
    assert 'lan_tournament_teams.id AS team_id' in member

    # The conflict is by user: not by team and not by participant.
    users, relation = _relation()
    text = _sql(repository.select(repository._conflicted_matches(users)))
    assert 'GROUP BY demand_users.user_id' in text
    assert 'HAVING count(DISTINCT demand_users.match_id) > 1' in text
    assert 'team_id' not in text.split('conflicted_matches AS ')[1]
    assert 'participant_id' not in text.split('conflicted_matches AS ')[1]

    # The same person is a solo player in one row and a member of a team
    # in the others, each with the team that demands them there.
    mara = _user('Mara')
    solo_row = _record(tournament_name='Solo-Cup')
    team_row = _record(tournament_name='Team-Cup')
    team_ref = _ref(tournament_name='Team-Cup', via_team_name='Rote Teufel')
    solo_ref = _ref(tournament_name='Solo-Cup', via_team_name=None)
    data = _data(
        [solo_row, team_row],
        conflicts={
            solo_row.match_id: (
                _conflict(solo_row.match_id, mara.id, refs=(team_ref,)),
            ),
            team_row.match_id: (
                _conflict(
                    team_row.match_id,
                    mara.id,
                    via_team_name='Rote Teufel',
                    refs=(solo_ref,),
                ),
            ),
        },
        contestants={
            team_ref.match_id: (
                _contestant(team_ref.match_id, team_name='Rote Teufel'),
            ),
            solo_ref.match_id: (_contestant(solo_ref.match_id, mara.id),),
        },
    )

    page = build_page(data, [mara])

    solo_conflict = page.rows[0].conflicts[0]
    team_conflict = page.rows[1].conflicts[0]
    assert solo_conflict.user_id == team_conflict.user_id == mara.id
    assert solo_conflict.user_display_name == 'Mara'
    assert solo_conflict.via_team_name is None
    assert solo_conflict.visible_refs[0].via_team_name == 'Rote Teufel'
    assert solo_conflict.visible_refs[0].contestant_names == ('Rote Teufel',)
    assert team_conflict.via_team_name == 'Rote Teufel'
    assert team_conflict.visible_refs[0].via_team_name is None
    assert team_conflict.visible_refs[0].contestant_names == ('Mara',)


# -- what is no demand --


def test_future_paused_removed_and_registration_only_overlap_is_not_conflict():
    sql = _demand_users_sql()
    body = _cte_body(sql, 'demand_users')

    # Only a due match of a RUNNING tournament demands anybody: not a
    # paused one, not one that has not started, not a coming round.
    for branch in body.split(' UNION ALL '):
        assert 'WHERE party_demand.due AND party_demand.t_status = ' in branch
        assert "party_demand.t_status = 'ONGOING'" in branch
        assert 'PAUSED' not in branch
        # Nobody removed is demanded: neither a participant nor a team.
        assert 'lan_tournament_participants.removed_at IS NULL' in branch
    team_branch = body.split(' UNION ALL ')[1]
    assert 'lan_tournament_teams.removed_at IS NULL' in team_branch

    # Demand comes from matches alone, so a registration is no demand.
    assert 'lan_tournament_match_contestants' in body
    assert 'FROM party_demand' in body

    # "Due" is the very predicate of the lists, not a second opinion: the
    # lists and the demand read one oracle, which is defined once.
    scope = _scope(T1, T2)
    _users, listed = _relation(scope)
    text = _sql(repository.select(listed))
    assert text.count('party_facts AS (') == 1
    assert text.count('party_demand AS MATERIALIZED (') == 1
    assert 'FROM party_demand' in _cte_body(text, 'dashboard_state')
    assert 'FROM party_demand' in _cte_body(text, 'demand_users')

    # The conflict needs two different matches, never the same one twice.
    users, _relation_ = _relation(scope)
    text = _sql(repository.select(repository._conflicted_matches(users)))
    assert 'count(DISTINCT demand_users.match_id) > 1' in text
    conflicts = _conflicts_sql()
    assert 'other.match_id != mine.match_id' in conflicts

    # A row of a match that is not in a demand state has no conflict.
    relation_sql = _sql(repository.select(_relation()[1]))
    assert (
        "dashboard_state.state IN ('due', 'unknown') AND"
        ' dashboard_state.match_id IN (SELECT conflicted_matches.match_id'
    ) in relation_sql


# -- what leaves the database --


def test_external_projection_exposes_boolean_only(build_page):
    # The records and DTOs have room for exactly this and nothing more.
    assert {f.name for f in fields(DashboardConflictRecord)} == {
        'match_id',
        'user_id',
        'via_team_name',
        'refs',
        'has_external_conflict',
    }
    assert {f.name for f in fields(DashboardConflict)} == {
        'user_id',
        'user_display_name',
        'via_team_name',
        'visible_refs',
        'has_external_conflict',
    }
    assert {f.name for f in fields(DashboardConflictRef)} == {
        'match_id',
        'tournament_id',
        'tournament_name',
        'location',
        'contestant_names',
        'via_team_name',
        'list_page',
        'game_format',
    }

    # A match outside of the scope joins to nothing, and its rows fall
    # into one: neither an ID nor a number of them leaves the database.
    sql = _conflicts_sql()
    selected = sql.split(' SELECT DISTINCT ')[1].split(' FROM ')[0]
    assert 'other.match_id' not in selected
    assert 'other.team_id' not in selected
    assert 'other.user_id' not in selected
    assert 'count(' not in sql.split(' SELECT DISTINCT ')[1]
    assert 'array_agg' not in sql
    assert (
        'LEFT OUTER JOIN counterparts AS counterpart ON counterpart.match_id'
        ' = other.match_id'
    ) in sql
    # The counterparts are described by the scope's relation alone.
    body = _cte_body(sql, 'counterparts')
    assert 'FROM dashboard' in body
    assert 'demand_users.match_id' in body.split(' WHERE ')[1]
    assert 'party_demand' not in body

    # An external demand is the one boolean, with nothing to tell a few
    # such matches from many: the same record, the same projection.
    mara = _user('Mara')
    row = _record()
    other = _record()
    data = _data(
        [row, other],
        conflicts={
            row.match_id: (
                _conflict(
                    row.match_id,
                    mara.id,
                    via_team_name='Rote Teufel',
                    has_external_conflict=True,
                ),
            ),
            other.match_id: (
                _conflict(
                    other.match_id,
                    mara.id,
                    via_team_name='Rote Teufel',
                    has_external_conflict=True,
                ),
            ),
        },
    )

    page = build_page(data, [mara])

    first, second = (r.conflicts[0] for r in page.rows)
    assert first == second
    assert first.visible_refs == ()
    assert first.has_external_conflict is True
    assert repr(first) == repr(second)
    # The team named is the one of THIS authorized row, never a hidden one.
    assert first.via_team_name == 'Rote Teufel'

    # However many rows of hidden matches the statement yields, a person
    # has one flag, and a hidden match adds no reference.
    first_match, second_match = generate_uuid(), generate_uuid()
    user_id, other_user_id = generate_uuid(), generate_uuid()
    rows = [
        _db_counterpart(first_match, user_id),
        _db_row(first_match, user_id),
        _db_row(first_match, user_id),
        _db_row(first_match, other_user_id),
        _db_counterpart(second_match, user_id),
    ]

    records = _read_conflicts(rows)

    by_user = {c.user_id: c for c in records[first_match]}
    assert len(by_user[user_id].refs) == 1
    assert by_user[user_id].has_external_conflict is True
    assert by_user[other_user_id].refs == ()
    assert by_user[other_user_id].has_external_conflict is True
    (only_visible,) = records[second_match]
    assert len(only_visible.refs) == 1
    assert only_visible.has_external_conflict is False


# -- the position in the list --


# fmt: off
@pytest.mark.parametrize(('position', 'per_page', 'expected'), [
    (None, 50, None), (None, 1, None),
    (1, 50, 1), (50, 50, 1), (51, 50, 2), (100, 50, 2), (101, 50, 3),
    (1, 1, 1), (2, 1, 2), (7, 1, 7),
    (3, 3, 1), (4, 3, 2), (6, 3, 2), (7, 3, 3),
])
# fmt: on
def test_the_page_of_a_position(position, per_page, expected):
    assert repository._list_page(position, per_page) == expected


def _listed_order_and_where(sql: str) -> tuple[str, str]:
    listed = _cte_body(sql, 'listed_matches')
    order = listed.split(' OVER (ORDER BY ')[1].split(') AS position')[0]
    where = listed.split(' FROM dashboard WHERE ')[1]
    return order, where


# fmt: off
@pytest.mark.parametrize('changes', [
    {},
    {'sort': 'wait'},
    {'sort': 'tournament'},
    {'state': 'conflict'},
    {'state': 'tier-red', 'sort': 'wait'},
    {'view': 'all', 'state': 'pinned'},
    {'view': 'upcoming'},
    {'tournament_id': generate_uuid()},
    {'page': 4, 'per_page': 2},
])
# fmt: on
def test_counterpart_list_page_follows_filtered_sorted_result(changes):
    query = _query(**changes)
    users, relation = _relation()
    rows = _sql(repository._rows_statement(relation, query))
    conflicts = _sql(
        repository._conflicts_statement(
            relation, users, query, [generate_uuid()]
        )
    )

    # The position is counted in the very result the page is cut from:
    # the same filter, the same order, before any cut.
    order, where = _listed_order_and_where(conflicts)
    rows_where = rows.rsplit(' FROM dashboard WHERE ', 1)[1]
    rows_order = rows_where.split(' ORDER BY ')[1].split(' LIMIT ')[0]
    rows_where = rows_where.split(' ORDER BY ')[0]
    assert order == rows_order
    assert where == rows_where
    assert ' LIMIT ' not in _cte_body(conflicts, 'listed_matches')
    assert ' OFFSET ' not in _cte_body(conflicts, 'listed_matches')

    # A counterpart that the filter drops has no position, so no page.
    assert (
        'LEFT OUTER JOIN listed_matches ON listed_matches.match_id'
        ' = dashboard.match_id'
    ) in _cte_body(conflicts, 'counterparts')
    assert 'listed_matches.position' in _cte_body(conflicts, 'counterparts')
    assert 'counterpart.position AS position' in conflicts

    # The page of a counterpart is read from its position by the page size
    # of the same query, and a counterpart outside of the list has none.
    match_id = generate_uuid()
    user_id = generate_uuid()
    rows = [
        _db_counterpart(match_id, user_id, position=1),
        _db_counterpart(match_id, user_id, position=query.per_page),
        _db_counterpart(match_id, user_id, position=query.per_page + 1),
        _db_counterpart(match_id, user_id, position=None),
    ]

    (record,) = _read_conflicts(rows, query=query)[match_id]

    assert sorted(
        (ref.list_page for ref in record.refs), key=lambda p: p or 0
    ) == [None, 1, 1, 2]


def test_the_list_page_reaches_the_reference_unchanged(build_page):
    mara = _user('Mara')
    row = _record()
    refs = tuple(
        _ref(tournament_name=f'Cup {page}', list_page=page)
        for page in (1, 4, None)
    )
    data = _data(
        [row],
        conflicts={row.match_id: (_conflict(row.match_id, mara.id, refs=refs),)},
    )

    page = build_page(data, [mara])

    (conflict,) = page.rows[0].conflicts
    assert [ref.list_page for ref in conflict.visible_refs] == [1, 4, None]
    # The order of the references is the one the repository decided.
    assert [ref.tournament_name for ref in conflict.visible_refs] == [
        'Cup 1',
        'Cup 4',
        'Cup None',
    ]


def test_a_counterpart_carries_its_game_format(build_page):
    # The reference is told apart as a lobby or a pairing by the effective
    # format of the counterpart's own phase, selected beside its position.
    sql = _conflicts_sql()
    assert 'counterpart.format AS other_format' in sql
    selected = sql.split(' SELECT DISTINCT ')[1].split(' FROM ')[0]
    assert 'other.' not in selected.replace('other_', '')

    mine, person = generate_uuid(), generate_uuid()
    rows = [
        _db_counterpart(mine, person, other_format='FREE_FOR_ALL'),
        _db_counterpart(mine, person, other_format='ONE_V_ONE'),
        _db_row(mine, person),  # a hidden match: nothing, not even a format
    ]

    result = _read_conflicts(rows)

    (record,) = result[mine]
    assert sorted(ref.game_format.name for ref in record.refs) == [
        'FREE_FOR_ALL',
        'ONE_V_ONE',
    ]
    assert record.has_external_conflict is True

    # The service hands it on to the DTO the views read.
    mara = _user('Mara')
    row = _record()
    refs = (
        _ref(tournament_name='Orbit', game_format=GameFormat.FREE_FOR_ALL),
        _ref(tournament_name='Neon', game_format=GameFormat.ONE_V_ONE),
        _ref(tournament_name='Old'),
    )
    data = _data(
        [row],
        conflicts={row.match_id: (_conflict(row.match_id, mara.id, refs=refs),)},
    )

    page = build_page(data, [mara])

    (conflict,) = page.rows[0].conflicts
    assert [ref.game_format for ref in conflict.visible_refs] == [
        GameFormat.FREE_FOR_ALL,
        GameFormat.ONE_V_ONE,
        None,
    ]


# -- naming the team --


def test_conflict_role_names_only_authorized_team(build_page):
    # The team of a counterpart is joined only when the counterpart is
    # one of the scope's matches. A hidden match has no team name.
    sql = _conflicts_sql()
    assert (
        'LEFT OUTER JOIN lan_tournament_teams AS lan_tournament_teams_2 ON'
        ' lan_tournament_teams_2.id = other.team_id AND'
        ' counterpart.match_id IS NOT NULL'
    ) in sql
    assert 'lan_tournament_teams_2.name AS other_via_team_name' in sql
    # The team of the listed side is that of an authorized match.
    assert (
        'LEFT OUTER JOIN lan_tournament_teams AS lan_tournament_teams_1 ON'
        ' lan_tournament_teams_1.id = mine.team_id'
    ) in sql

    mara = _user('Mara')
    jonas = _user('Jonas')
    row = _record()
    named = _ref(tournament_name='Neon-Duell', via_team_name='Rote Teufel')
    solo = _ref(tournament_name='Orbit', via_team_name=None)
    data = _data(
        [row],
        conflicts={
            row.match_id: (
                _conflict(
                    row.match_id,
                    mara.id,
                    via_team_name='Blaue Pelikane',
                    refs=(named, solo),
                ),
                # Hidden demand only: no team, no reference, one boolean.
                _conflict(
                    row.match_id,
                    jonas.id,
                    via_team_name=None,
                    has_external_conflict=True,
                ),
            )
        },
    )

    page = build_page(data, [mara, jonas])

    jonas_conflict, mara_conflict = page.rows[0].conflicts
    assert mara_conflict.user_display_name == 'Mara'
    assert mara_conflict.via_team_name == 'Blaue Pelikane'
    assert [r.via_team_name for r in mara_conflict.visible_refs] == [
        'Rote Teufel',
        None,
    ]
    assert jonas_conflict.visible_refs == ()
    assert jonas_conflict.has_external_conflict is True
    assert jonas_conflict.via_team_name is None
    assert 'Rote Teufel' not in repr(jonas_conflict)

    # The reader takes the team of the listed side from its own row and the
    # team of a counterpart from the counterpart's, whatever hides elsewhere.
    match_id, user_id = generate_uuid(), generate_uuid()
    rows = [
        _db_counterpart(
            match_id,
            user_id,
            via_team_name='Blaue Pelikane',
            other_via_team_name='Rote Teufel',
        ),
        _db_counterpart(
            match_id, user_id, via_team_name='Blaue Pelikane'
        ),
        _db_row(match_id, user_id, via_team_name='Blaue Pelikane'),
    ]

    (record,) = _read_conflicts(rows)[match_id]

    assert record.via_team_name == 'Blaue Pelikane'
    assert [ref.via_team_name for ref in record.refs] == ['Rote Teufel', None]
    assert record.has_external_conflict is True


def test_people_are_listed_by_name_and_a_deleted_one_is_named_as_such(
    build_page,
):
    zoe = _user('zoe')
    anna = _user('Anna')
    gone = _user(None, deleted=True)
    row = _record()
    data = _data(
        [row],
        conflicts={
            row.match_id: tuple(
                _conflict(row.match_id, user.id, has_external_conflict=True)
                for user in (zoe, gone, anna)
            )
        },
    )

    page = build_page(data, [zoe, anna, gone])

    assert [c.user_display_name for c in page.rows[0].conflicts] == [
        'Anna',
        'T[Deleted user]',
        'zoe',
    ]


# -- the filter and the weight --


def test_conflict_filter_and_urgency_weight_precede_pagination():
    users, relation = _relation()

    # "In conflict" is a column of the relation the page is cut from.
    relation_sql = _sql(repository.select(relation))
    assert re.search(
        r"dashboard_state\.state IN \('due', 'unknown'\) AND"
        r' dashboard_state\.match_id IN \(SELECT conflicted_matches\.match_id'
        r' FROM conflicted_matches\) AS has_conflict',
        relation_sql,
    )

    for page, per_page in ((1, 5), (3, 2)):
        query = _query(state='conflict', page=page, per_page=per_page)
        rows = _sql(repository._rows_statement(relation, query))
        counts = _sql(repository._counts_statement(relation, query))

        # The filter comes first, then the order, then the cut.
        where = rows.rsplit(' WHERE ', 1)[1].split(' ORDER BY ')[0]
        assert 'dashboard.has_conflict' in where
        assert rows.rindex(' WHERE ') < rows.rindex(' ORDER BY ')
        assert rows.rindex(' ORDER BY ') < rows.rindex(' LIMIT ')
        # The total is counted by the same predicate, with no cut.
        assert 'filter (where' in counts.lower().replace('  ', ' ')
        assert 'dashboard.has_conflict' in counts
        statement = counts.rsplit('SELECT count(*)', 1)[1]
        assert ' LIMIT ' not in statement and ' OFFSET ' not in statement

    # The urgency order weighs it right after the tier, before the
    # intervals: red before yellow before green, then conflict first.
    urgency = _sql(repository._rows_statement(relation, _query())).rsplit(
        'ORDER BY ', 1
    )[1]
    assert urgency.startswith(
        'dashboard.urgency_rank ASC, dashboard.has_conflict DESC,'
        ' dashboard.alert_us DESC NULLS LAST'
    )
    # The other orders know no conflict.
    for sort in ('wait', 'tournament'):
        other = _sql(
            repository._rows_statement(relation, _query(sort=sort))
        ).rsplit('ORDER BY ', 1)[1]
        assert 'has_conflict' not in other

    # The decision of the repository rides on the record, not on a count.
    assert repository.DashboardMatchRecord.__dataclass_fields__[
        'has_conflict'
    ].default is False


def test_a_page_without_a_conflict_has_no_conflict_rows():
    # The conflicts ride in the page statement, which yields none for a
    # page of rows that are not in conflict.
    rows = [
        SimpleNamespace(kind=repository._COUNTS_KIND),
        SimpleNamespace(kind=repository._ROW_KIND),
    ]

    assert repository._fold_conflicts(rows, 50) == {}
    assert repository._fold_conflicts([], 50) == {}
