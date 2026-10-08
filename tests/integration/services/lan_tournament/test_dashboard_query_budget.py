from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, UTC
import json
import re
from statistics import median
import time
from typing import Any, Self
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_dashboard_service as dashboard_service,
    tournament_dashboard_settings_service as settings_service,
    tournament_match_service as matches,
    tournament_repository as repo,
    tournament_service,
)
from byceps.services.lan_tournament.dashboard_config import (
    MAX_PAGE_SIZE,
    PAGE_SIZE_KEY,
)
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardPage,
    DashboardQuery,
    DashboardScopeKind,
)
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.site import site_service
from byceps.services.site.models import Site
from byceps.services.user.models import User
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


ROUND_ROBIN_TOURNAMENTS = 30
ROUND_ROBIN_PLAYERS = 10
# Ten players meet once each: 45 pairings in nine rounds of five.
ROUND_ROBIN_FIXTURES = 45
KNOCKOUT_TOURNAMENTS = 2
KNOCKOUT_PLAYERS = 8
FREE_FOR_ALL_PLAYERS = 8
POOL_SIZE = 60
POOL_STEP = 3

# Whole minutes the clock of a group of tournaments is moved back, so that
# the due matches of the fleet wait long enough to show every tier.
RED_BACKDATE = 50
YELLOW_BACKDATE = 20

ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments/for_party'
SITE_URL = 'http://www.acmecon.test/lan-tournaments'

STATEMENT_BUDGET = 16
PAGE_SIZES = (10, 50, 100)
TIMING_RUNS = 5

SELECT_ONLY = re.compile(r'^\s*(SELECT|WITH)\b', re.IGNORECASE)
WRITES = re.compile(
    r'\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|COPY|LOCK|NEXTVAL|SETVAL)\b'
    r'|\bFOR\s+(NO\s+KEY\s+)?(UPDATE|SHARE)\b|\bFOR\s+KEY\s+SHARE\b',
    re.IGNORECASE,
)
EXECUTION_TIME = re.compile(r'Execution Time: ([0-9.]+) ms')
PLANNING_TIME = re.compile(r'Planning Time: ([0-9.]+) ms')
SHARED_HIT = re.compile(r'shared hit=(\d+)')
SHARED_READ = re.compile(r'shared read=(\d+)')


# -- counting the real statements --


@dataclass
class Statement:
    sql: str
    parameters: Any
    milliseconds: float = 0.0


class Probe:
    """Record what the real engine executes, and how long it takes.

    The listeners sit on the engine class, so nothing that touches the
    database goes unseen. A counter that is faked or filtered would not
    be one.
    """

    def __init__(self) -> None:
        self.statements: list[Statement] = []
        self.commits = 0
        self.rollbacks = 0
        self._started: list[float] = []
        self._listeners: dict[str, Callable] = {
            'before_cursor_execute': self._before,
            'after_cursor_execute': self._after,
            'commit': self._commit,
            'rollback': self._rollback,
        }

    def __enter__(self) -> Self:
        for name, listener in self._listeners.items():
            event.listen(Engine, name, listener)
        return self

    def __exit__(self, *exception) -> None:
        for name, listener in self._listeners.items():
            event.remove(Engine, name, listener)

    def _before(
        self, connection, cursor, statement, parameters, context, many
    ) -> None:
        self.statements.append(Statement(statement, parameters))
        self._started.append(time.perf_counter())

    def _after(
        self, connection, cursor, statement, parameters, context, many
    ) -> None:
        started = self._started.pop()
        self.statements[-1].milliseconds = (
            time.perf_counter() - started
        ) * 1000

    def _commit(self, connection) -> None:
        self.commits += 1

    def _rollback(self, connection) -> None:
        self.rollbacks += 1

    @property
    def count(self) -> int:
        return len(self.statements)


def assert_read_only(probe: Probe) -> None:
    """Every statement reads, locks nothing, and nothing is committed."""
    assert probe.statements
    for statement in probe.statements:
        assert SELECT_ONLY.match(statement.sql), statement.sql[:200]
        assert not WRITES.search(statement.sql), statement.sql[:200]
    assert probe.commits == 0


# -- the fleet: a party of 30 round robins, some knockouts and a lobby --


@dataclass
class Fleet:
    party_id: PartyID
    admin: User
    orga_all: User
    orga_some: User
    orga_one: User
    round_robin_ids: list[TournamentID]
    knockout_ids: list[TournamentID]
    free_for_all_id: TournamentID

    @property
    def all_ids(self) -> list[TournamentID]:
        return [
            *self.round_robin_ids,
            *self.knockout_ids,
            self.free_for_all_id,
        ]


def _register(tournament_id, users) -> None:
    for user in users:
        repo.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament_id,
                substitute_player=False,
                team_id=None,
                created_at=datetime.now(UTC),
            )
        )
    db.session.commit()


def _create(party_id, name, **settings) -> TournamentID:
    result = tournament_service.create_tournament(
        party_id,
        name,
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        **settings,
    )
    tournament, _ = result.unwrap()
    return tournament.id


def _start(tournament_id, admin) -> None:
    tournament_service.change_status(
        tournament_id, TournamentStatus.ONGOING, admin.id
    ).unwrap()


def _backdate(tournament_ids, minutes: int) -> None:
    """Move the active clock back, so the due matches have waited longer."""
    if not minutes:
        return

    db.session.execute(
        text(
            'UPDATE lan_tournaments SET'
            ' operational_clock_running_since ='
            '   operational_clock_running_since - make_interval(mins => :m),'
            ' operational_clock_activated_at ='
            '   operational_clock_activated_at - make_interval(mins => :m)'
            ' WHERE id = ANY(:ids)'
        ),
        {'m': minutes, 'ids': list(tournament_ids)},
    )
    db.session.commit()


@pytest.fixture(scope='module')
def fleet(make_party, brand, make_user, make_admin, admin_app) -> Fleet:
    """Build the representative fixture through the real services.

    Thirty round robins of ten players each give 1,350 generated fixtures.
    Players take part in about three tournaments each, so demand overlaps
    across tournaments. Two knockouts and one free-for-all lobby round
    come on top. Every tournament is started, so the due matches are the
    ones of the current rounds and nothing else.
    """
    suffix = uuid4().hex[:6]
    party_id = PartyID(f'f03q-{uuid4().hex[:12]}')
    make_party(brand, party_id, f'F03 query budget {party_id}')

    admin = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'},
        screen_name=f'f03qadmin{suffix}',
    )
    orga_all, orga_some, orga_one = (
        make_user(f'f03qorga{label}{suffix}')
        for label in ('all', 'some', 'one')
    )
    for user in (admin, orga_all, orga_some, orga_one):
        log_in_user(user.id)
    pool = [make_user(f'f03qplayer{i:02d}{suffix}') for i in range(POOL_SIZE)]

    def roster(index: int, size: int):
        return [pool[(index * POOL_STEP + k) % POOL_SIZE] for k in range(size)]

    round_robin_ids = []
    for index in range(ROUND_ROBIN_TOURNAMENTS):
        tournament_id = _create(
            party_id,
            f'Budget RR {index:02d} {suffix}',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
        )
        _register(tournament_id, roster(index, ROUND_ROBIN_PLAYERS))
        matches.generate_round_robin_bracket(tournament_id).unwrap()
        _start(tournament_id, admin)
        round_robin_ids.append(tournament_id)

    knockout_ids = []
    for index in range(KNOCKOUT_TOURNAMENTS):
        tournament_id = _create(
            party_id,
            f'Budget KO {index} {suffix}',
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        )
        _register(tournament_id, roster(index + 5, KNOCKOUT_PLAYERS))
        matches.generate_single_elimination_bracket(tournament_id).unwrap()
        _start(tournament_id, admin)
        knockout_ids.append(tournament_id)

    free_for_all_id = _create(
        party_id,
        f'Budget FFA {suffix}',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        max_players=16,
        point_table=[5, 3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
    )
    _register(free_for_all_id, roster(11, FREE_FOR_ALL_PLAYERS))
    matches.generate_ffa_round(
        free_for_all_id, bracket=None, initiator_id=admin.id
    ).unwrap()
    _start(free_for_all_id, admin)

    # A third of the tournaments each: long, medium and short waits.
    _backdate(round_robin_ids[:10], RED_BACKDATE)
    _backdate(round_robin_ids[10:20], YELLOW_BACKDATE)

    assigned = {
        orga_all: [*round_robin_ids, *knockout_ids, free_for_all_id],
        orga_some: round_robin_ids[:20],
        orga_one: round_robin_ids[:1],
    }
    for user, tournament_ids in assigned.items():
        for tournament_id in tournament_ids:
            db.session.add(
                DbTournamentOrga(
                    TournamentOrgaID(generate_uuid7()),
                    tournament_id,
                    user.id,
                    datetime.now(UTC),
                )
            )
    db.session.commit()

    return Fleet(
        party_id=party_id,
        admin=admin,
        orga_all=orga_all,
        orga_some=orga_some,
        orga_one=orga_one,
        round_robin_ids=round_robin_ids,
        knockout_ids=knockout_ids,
        free_for_all_id=free_for_all_id,
    )


def _bound_to(site: Site, party_id: PartyID | None) -> Site:
    """Bind the site to that party, keeping every other field."""
    return site_service.update_site(
        site.id,
        site.title,
        site.server_name,
        party_id,
        site.enabled,
        site.user_account_creation_enabled,
        site.login_enabled,
        site.board_id,
        site.storefront_id,
        site.is_intranet,
        site.check_in_on_login,
        site.archived,
    )


@pytest.fixture(scope='module')
def site_of_the_fleet(site, fleet) -> Iterator[None]:
    """Let the site serve the fleet's party, and give it back afterwards.

    A site reads its party from its row on every request, so the app
    needs no rebuild. The fleet lives in a party of its own, because
    every tournament of the shared party counts for a global orga.
    """
    _bound_to(site, fleet.party_id)
    yield
    _bound_to(site, site.party_id)


@pytest.fixture(autouse=True)
def _leave_no_transaction(admin_app):
    yield
    db.session.rollback()


# -- reading the service --


def _viewer(user, *permissions: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(permissions))


def _who(fleet: Fleet, name: str) -> CurrentUser:
    if name == 'admin':
        return _viewer(fleet.admin, 'lan_tournament.administrate')

    return _viewer(getattr(fleet, name))


def _query(name: str, per_page: int = 50, **values) -> DashboardQuery:
    scope: DashboardScopeKind = 'all' if name == 'admin' else 'assigned'
    return DashboardQuery(scope=scope, per_page=per_page, **values)


def read_page(
    fleet: Fleet, name: str, query: DashboardQuery
) -> tuple[DashboardPage, Probe]:
    """Read one page through the real service and count its statements.

    The settings are read first: they belong to the request around the
    snapshot, not to the snapshot.
    """
    settings = settings_service.get_effective_dashboard_settings(
        fleet.party_id
    ).unwrap()
    viewer = _who(fleet, name)
    with Probe() as probe:
        result = dashboard_service.get_dashboard_page(
            viewer, fleet.party_id, query, settings=settings
        )
    return result.unwrap(), probe


SNAPSHOT_CASES = [
    ('admin-all-due', 'admin', {}),
    ('admin-everything', 'admin', {'view': 'all', 'per_page': 100}),
    ('admin-upcoming', 'admin', {'view': 'upcoming'}),
    ('admin-conflicts', 'admin', {'state': 'conflict'}),
    ('admin-red-tier', 'admin', {'state': 'tier-red'}),
    ('admin-by-wait-page-two', 'admin', {'sort': 'wait', 'page': 2}),
    ('admin-by-tournament', 'admin', {'sort': 'tournament', 'per_page': 10}),
    ('admin-one-tournament', 'admin', {'tournament': 0}),
    ('admin-beyond-last-page', 'admin', {'page': 90, 'per_page': 10}),
    ('orga-all-due', 'orga_all', {}),
    ('orga-some-due', 'orga_some', {'per_page': 100}),
    ('orga-some-conflicts', 'orga_some', {'state': 'conflict'}),
    ('orga-one-due', 'orga_one', {}),
    ('orga-one-everything', 'orga_one', {'view': 'all'}),
]


def _case_query(fleet: Fleet, who: str, values: dict) -> DashboardQuery:
    values = dict(values)
    index = values.pop('tournament', None)
    if index is not None:
        values['tournament_id'] = fleet.round_robin_ids[index]
    return _query(who, **values)


@pytest.mark.parametrize(
    'who,values',
    [case[1:] for case in SNAPSHOT_CASES],
    ids=[case[0] for case in SNAPSHOT_CASES],
)
def test_snapshot_query_count_at_most_16(fleet, who, values):
    assert MAX_PAGE_SIZE >= max(PAGE_SIZES)

    page, probe = read_page(fleet, who, _case_query(fleet, who, values))

    # The listener sees the whole snapshot: a handful of statements at
    # least, or it would be counting something else.
    assert 5 <= probe.count <= STATEMENT_BUDGET, [
        statement.sql[:80] for statement in probe.statements
    ]
    assert any('lan_tournament_matches' in s.sql for s in probe.statements)
    assert_read_only(probe)
    # Reading ended its transaction, and no read committed.
    assert probe.rollbacks >= 1
    print(
        f'SNAPSHOT {who} {values}: {probe.count} statements,'
        f' {len(page.rows)} of {page.total_count} rows'
    )


# -- the due oracle runs once --


def _plan_of(statement: Statement) -> str:
    """Return how the database plans one statement of a snapshot, unrun."""
    with db.engine.connect() as connection:
        lines = (
            connection.exec_driver_sql(
                f'EXPLAIN (COSTS OFF) {statement.sql}', statement.parameters
            )
            .scalars()
            .all()
        )
        connection.rollback()
    return '\n'.join(lines)


MATCHES_TABLE = re.compile(r'\blan_tournament_matches\b')


@pytest.mark.parametrize(
    'who,values',
    [case[1:] for case in SNAPSHOT_CASES],
    ids=[case[0] for case in SNAPSHOT_CASES],
)
def test_the_due_oracle_runs_once_per_snapshot(fleet, who, values):
    page, probe = read_page(fleet, who, _case_query(fleet, who, values))

    # One statement of the snapshot judges the party; no other statement
    # reads the matches again to decide what is due or who is demanded.
    (oracle,) = [s for s in probe.statements if 'party_facts' in s.sql]
    others = [s for s in probe.statements if s is not oracle]
    assert not any(MATCHES_TABLE.search(s.sql) for s in others)
    assert oracle.sql.count('party_facts AS') == 1
    # The counts, the rows and the conflicts are that statement's parts.
    assert oracle.sql.count('AS kind') == 3

    # The database evaluates the chain once, however many readers it has
    # (the counts, the rows, the demand, the conflicts).
    plan = _plan_of(oracle)
    assert plan.count('CTE party_demand') == 1, plan
    assert plan.count('Subquery Scan on party_facts') == 1, plan
    assert plan.count('CTE Scan on party_demand') >= 2, plan
    # A page is a read of what the oracle judged, never a second judgement.
    assert 'Subquery Scan on facts' not in plan
    assert page.total_count >= len(page.rows)


# -- growth from 10 to 100 rows --


def _row_markup_count(html: str) -> int:
    return html.count('<li class="ltd-row"')


def _measure_over_http(client, url: str) -> tuple[int, str]:
    client.get(url)
    with Probe() as probe:
        response = client.get(url)
    assert response.status_code == 200
    assert_read_only(probe)
    return probe.count, response.get_data(as_text=True)


@pytest.mark.parametrize('who', ['admin', 'orga_all', 'orga_some'])
@pytest.mark.parametrize('view', ['due', 'all'])
def test_page_growth_10_to_100_adds_at_most_one_query(fleet, who, view):
    counts = {}
    for per_page in PAGE_SIZES:
        page, probe = read_page(
            fleet, who, _query(who, per_page=per_page, view=view)
        )
        # Every page is full, and the conflicts statement runs on each:
        # growth is measured between equal kinds of page.
        assert len(page.rows) == per_page
        assert any(row.conflicts for row in page.rows)
        counts[per_page] = probe.count

    print(f'GROWTH {who} {view}: {counts}')
    assert counts[10] <= STATEMENT_BUDGET
    assert counts[100] <= STATEMENT_BUDGET
    assert counts[50] - counts[10] <= 1
    assert counts[100] - counts[10] <= 1


@pytest.mark.parametrize(
    'surface,who', [('admin', 'admin'), ('site', 'orga_some')]
)
def test_page_growth_over_the_whole_request_adds_at_most_one_query(
    fleet,
    site_of_the_fleet,
    surface,
    who,
    admin_app,
    site_app,
    make_client,
    monkeypatch,
):
    """The route, the real templates and the layout add nothing per row."""
    app = admin_app if surface == 'admin' else site_app
    client = make_client(app, user_id=getattr(fleet, who).id)
    url = (
        f'{ADMIN_URL}/{fleet.party_id}/dashboard?scope=all'
        if surface == 'admin'
        else f'{SITE_URL}/orga-dashboard'
    )

    counts = {}
    for per_page in PAGE_SIZES:
        monkeypatch.setitem(app.config, PAGE_SIZE_KEY, per_page)
        counts[per_page], html = _measure_over_http(client, url)
        # The real markup holds every row of the page.
        assert _row_markup_count(html) == per_page

    print(f'REQUEST GROWTH {surface}: {counts}')
    assert counts[50] - counts[10] <= 1
    assert counts[100] - counts[10] <= 1


# -- representative metrics --


def _plans(probe: Probe) -> list[dict]:
    """Explain the statements of a read, as the database plans them.

    Only reads are replayed, in a connection of their own and never
    committed.
    """
    plans = []
    with db.engine.connect() as connection:
        for number, statement in enumerate(probe.statements, start=1):
            if not SELECT_ONLY.match(statement.sql) or WRITES.search(
                statement.sql
            ):
                continue

            lines = (
                connection.exec_driver_sql(
                    f'EXPLAIN (ANALYZE, BUFFERS) {statement.sql}',
                    statement.parameters,
                )
                .scalars()
                .all()
            )
            plan = '\n'.join(lines)
            planning = PLANNING_TIME.search(plan)
            execution = EXECUTION_TIME.search(plan)
            plans.append(
                {
                    'statement': number,
                    'head': ' '.join(statement.sql.split())[:90],
                    'planning_ms': float(planning[1]) if planning else None,
                    'execution_ms': float(execution[1]) if execution else None,
                    'shared_hit': sum(
                        int(n) for n in SHARED_HIT.findall(plan)[:1]
                    ),
                    'shared_read': sum(
                        int(n) for n in SHARED_READ.findall(plan)[:1]
                    ),
                    'top': lines[0].strip(),
                    'seq_scans': sorted(
                        set(re.findall(r'Seq Scan on (\w+)', plan))
                    ),
                    'lines': len(lines),
                }
            )
        connection.rollback()
    return plans


def _timed_requests(client, url: str) -> dict:
    client.get(url)
    milliseconds = []
    for _ in range(TIMING_RUNS):
        started = time.perf_counter()
        response = client.get(url)
        milliseconds.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
    with Probe() as probe:
        client.get(url)
    return {
        'statements': probe.count,
        'ms_min': round(min(milliseconds), 1),
        'ms_median': round(median(milliseconds), 1),
        'ms_max': round(max(milliseconds), 1),
    }


def _scalar(sql: str, **parameters):
    return db.session.execute(text(sql), parameters).scalar_one()


def test_many_tournament_metrics_are_recorded(
    fleet,
    site_of_the_fleet,
    admin_app,
    site_app,
    make_client,
    record_property,
):
    ids = [str(tournament_id) for tournament_id in fleet.all_ids]
    round_robin = [str(t) for t in fleet.round_robin_ids]
    knockout = [str(t) for t in fleet.knockout_ids]

    # -- the fixture is what the plan describes --
    fixtures = _scalar(
        'SELECT count(*) FROM lan_tournament_matches'
        ' WHERE tournament_id = ANY(CAST(:ids AS uuid[]))',
        ids=round_robin,
    )
    participants = _scalar(
        'SELECT count(*) FROM lan_tournament_participants'
        ' WHERE tournament_id = ANY(CAST(:ids AS uuid[]))',
        ids=ids,
    )
    first_round = _scalar(
        'SELECT count(*) FROM lan_tournament_matches'
        ' WHERE tournament_id = ANY(CAST(:ids AS uuid[])) AND round = 0',
        ids=[*round_robin, *knockout],
    )
    db.session.rollback()
    assert len(fleet.round_robin_ids) == ROUND_ROBIN_TOURNAMENTS
    assert fixtures == ROUND_ROBIN_TOURNAMENTS * ROUND_ROBIN_FIXTURES == 1350
    assert participants == (
        ROUND_ROBIN_TOURNAMENTS * ROUND_ROBIN_PLAYERS
        + KNOCKOUT_TOURNAMENTS * KNOCKOUT_PLAYERS
        + FREE_FOR_ALL_PLAYERS
    )

    # -- demand is the current rounds only --
    page, probe = read_page(
        fleet, 'admin', _query('admin', per_page=100, view='due')
    )
    everything, _ = read_page(
        fleet, 'admin', _query('admin', per_page=100, view='all')
    )
    expected_due = first_round + 2  # the free-for-all lobbies of round one
    assert page.total_count == expected_due
    assert page.total_count < fixtures / 4
    assert {row.location.round for row in page.rows} == {0}
    assert everything.total_count >= fixtures
    tiers = page.tier_counts
    assert tiers.red > 0 and tiers.yellow > 0 and tiers.green > 0
    assert tiers.red + tiers.yellow + tiers.green == page.total_count
    assert sum(1 for row in page.rows if row.conflicts) > 0

    # -- the snapshot's statement count does not follow the party --
    by_scope = {}
    for who in ('orga_one', 'orga_some', 'orga_all', 'admin'):
        scoped, scoped_probe = read_page(fleet, who, _query(who))
        assert any(row.conflicts for row in scoped.rows)
        by_scope[who] = {
            'tournaments': {
                'orga_one': 1,
                'orga_some': 20,
                'orga_all': len(ids),
                'admin': len(ids),
            }[who],
            'statements': scoped_probe.count,
            'rows': len(scoped.rows),
            'due_total': scoped.total_count,
        }
    counts = [entry['statements'] for entry in by_scope.values()]
    assert max(counts) - min(counts) <= 1
    assert max(counts) <= STATEMENT_BUDGET

    # -- the report: statements, plans, requests --
    per_statement = [
        {
            'statement': number,
            'head': ' '.join(statement.sql.split())[:90],
            'ms': round(statement.milliseconds, 2),
        }
        for number, statement in enumerate(probe.statements, start=1)
    ]
    plans = _plans(probe)

    admin_client = make_client(admin_app, user_id=fleet.admin.id)
    site_client = make_client(site_app, user_id=fleet.orga_some.id)
    admin_url = f'{ADMIN_URL}/{fleet.party_id}/dashboard'
    requests = {
        'admin page': _timed_requests(admin_client, f'{admin_url}?scope=all'),
        'admin poll': _timed_requests(
            admin_client, f'{admin_url}/poll?scope=all'
        ),
        'site page': _timed_requests(site_client, f'{SITE_URL}/orga-dashboard'),
        'site poll': _timed_requests(
            site_client, f'{SITE_URL}/orga-dashboard/poll'
        ),
    }
    for name, measured in requests.items():
        # The route, the session and the layout come on top of the snapshot.
        measured['beyond_the_snapshot'] = measured['statements'] - probe.count
        assert measured['statements'] > probe.count, name

    report = {
        'fixture': {
            'tournaments': len(ids),
            'round_robin_fixtures': fixtures,
            'participants': participants,
            'due_matches': page.total_count,
            'all_view_rows': everything.total_count,
            'tiers': {
                'red': tiers.red,
                'yellow': tiers.yellow,
                'green': tiers.green,
            },
        },
        'snapshot_by_scope': by_scope,
        'snapshot_statements_admin_all_due_100': per_statement,
        'plans': plans,
        'requests': requests,
    }
    rendered = json.dumps(report, indent=2, default=str)
    print(f'METRICS {rendered}')
    record_property('dashboard_query_metrics', rendered)

    assert len(per_statement) == probe.count
    assert plans and all(plan['execution_ms'] is not None for plan in plans)
    assert all(
        row['statements'] <= STATEMENT_BUDGET for row in by_scope.values()
    )
