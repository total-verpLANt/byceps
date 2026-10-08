"""
HTTP contract of the site's orga dashboard routes.

The template names are asserted but the templates are isolated: `render_template`
of the site views is replaced by a spy, so these tests prove the routes (the
checks, the status codes, the transport bodies and the context they hand to
the templates), not the rendered pages. The real template pipeline is proved
by the template, render and surface-parity tests.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import inspect
import json
from urllib.parse import urlsplit
from uuid import uuid4

from flask import g, template_rendered
from flask_babel import gettext
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    dashboard_view_helpers as helpers,
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_orga_service as orgas,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.blueprints.dashboard_csrf import (
    CSRF_INVALID_NOTICE,
    CSRF_SESSION_KEY,
)
from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'
DASHBOARD_URL = f'{BASE_URL}/orga-dashboard'
POLL_URL = f'{DASHBOARD_URL}/poll'
PAGE_TEMPLATE = 'site/lan_tournament/dashboard.html'
PANEL_TEMPLATE = 'common/lan_tournament/_dashboard_panel.html'

MINUTE_US = 60_000_000
RUNNING_FOR = timedelta(hours=3)
# Far before any server clock, so no stamp could hide behind `GREATEST`.
LAST_CHANGED = datetime(2026, 1, 15, 8, 0, 0)

JSON = {'Accept': 'application/json'}
NO_STORE = 'private, no-store'

RETURN_DUE = helpers.build_dashboard_return(
    DashboardQuery(per_page=50), surface='site'
)
RETURN_ALL = helpers.build_dashboard_return(
    DashboardQuery(view='all', per_page=50), surface='site'
)
# Only the red tier: the yellow matches of the world are not in this list.
RETURN_RED = helpers.build_dashboard_return(
    DashboardQuery(state='tier-red', per_page=50), surface='site'
)

STATE_TABLES = (
    'lan_tournaments',
    'lan_tournament_matches',
    'lan_tournament_match_due_episodes',
    'lan_tournament_match_escalation_acks',
    'lan_tournament_match_dashboard_annotations',
    'lan_tournament_log_entries',
)


# -------------------------------------------------------------------- #
# the world


class _RenderSpy:
    """Stand in for `render_template` of the site views."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, template_name, **context):
        self.calls.append((template_name, context))
        return f'<rendered {template_name}>'

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def spy(monkeypatch):
    render_spy = _RenderSpy()
    monkeypatch.setattr(site_views, 'render_template', render_spy)
    return render_spy


@pytest.fixture(scope='module')
def confirmer(make_user):
    return make_user(f'f03sconf{uuid4().hex[:8]}')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'lan_tournament.administrate'}, screen_name=f'f03sadm{uuid4().hex[:8]}'
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin(
        {'lan_tournament.view'}, screen_name=f'f03sview{uuid4().hex[:8]}'
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def other_party(make_party, brand):
    party_id = PartyID(f'f03s-{uuid4().hex[:12]}')
    return make_party(brand, party_id, f'F03 site routes {party_id}')


@dataclass
class World:
    """Tournaments of two parties, with the people who may see which."""

    party_id: PartyID
    orga: object
    other_orga: object
    power: object
    outsider: object
    abroad: object
    viewer: object
    admin: object
    a: TournamentID
    b: TournamentID
    c: TournamentID
    a1: TournamentMatchID
    a2: TournamentMatchID
    a3: TournamentMatchID
    b1: TournamentMatchID
    c1: TournamentMatchID
    names: dict[str, str]


class _Builder:
    def __init__(self, confirmer) -> None:
        self.now = repo.get_operation_time()
        db.session.rollback()
        self.confirmer = confirmer
        self._orders = iter(range(1, 1000))
        self._joined: dict[tuple, TournamentParticipantID] = {}

    def tournament(self, party_id, name) -> TournamentID:
        """Create a tournament that has run for three hours."""
        tournament_id = TournamentID(uuid7())
        started = self.now - RUNNING_FOR
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=party_id,
                name=name,
                game=None,
                description=None,
                image_url=None,
                ruleset=None,
                start_time=None,
                created_at=started,
                min_players=None,
                max_players=None,
                min_teams=None,
                max_teams=None,
                min_players_in_team=None,
                max_players_in_team=None,
                contestant_type=None,
                tournament_status=TournamentStatus.ONGOING,
                game_format=GameFormat.ONE_V_ONE,
                elimination_mode=EliminationMode.SINGLE_ELIMINATION,
                group_size_max=None,
                operational_clock_elapsed_us=0,
                operational_clock_running_since=started,
                operational_clock_activated_at=started,
            )
        )
        db.session.commit()
        return tournament_id

    def duel(
        self, tournament_id, player_a, player_b, *, wait_minutes=20, done=False
    ) -> TournamentMatchID:
        """Create a match of two people; with `wait_minutes` it is due."""
        match_id = TournamentMatchID(uuid7())
        db.session.add(
            DbTournamentMatch(
                match_id,
                tournament_id,
                self.now - timedelta(hours=1),
                match_order=next(self._orders),
                round=1,
                confirmed_by=self.confirmer.id if done else None,
                phase=1,
                occupied_since=self.now - timedelta(minutes=30),
                last_changed_at=LAST_CHANGED,
            )
        )
        db.session.flush()
        for user in (player_a, player_b):
            db.session.add(
                DbTournamentMatchToContestant(
                    TournamentMatchToContestantID(uuid7()),
                    match_id,
                    self.now - timedelta(hours=1),
                    participant_id=self._join(tournament_id, user),
                )
            )
        db.session.flush()
        if wait_minutes is not None and not done:
            repo.open_due_episode_flush(
                MatchDueEpisode(
                    id=MatchDueEpisodeID(uuid7()),
                    tournament_id=tournament_id,
                    match_id=match_id,
                    pairing_key='key',
                    opened_at=self.now - timedelta(minutes=wait_minutes),
                    opened_clock_us=(
                        int(RUNNING_FOR.total_seconds() // 60 - wait_minutes)
                        * MINUTE_US
                    ),
                )
            )
        db.session.commit()
        return match_id

    def _join(self, tournament_id, user):
        key = (tournament_id, user.id)
        if key not in self._joined:
            participant = DbTournamentParticipant(
                TournamentParticipantID(uuid7()),
                user.id,
                tournament_id,
                self.now,
            )
            db.session.add(participant)
            db.session.commit()
            self._joined[key] = participant.id
        return self._joined[key]


@pytest.fixture
def world(party, other_party, make_user, make_admin, confirmer, admin, viewer):
    """Build two parties of tournaments and the people around them.

    `orga` is assigned to tournament A of the site's party and to tournament
    C of another party. `other_orga` is assigned to B of the site's party.
    A, B and C share their four players, so demand overlaps across them.
    """
    suffix = uuid4().hex[:8]
    people = [make_user(f'f03s{label}{suffix}') for label in 'oxzqpkmn']
    orga, other_orga, outsider, abroad, *players = people
    power = make_admin(
        {'lan_tournament.administrate'}, screen_name=f'f03spower{suffix}'
    )
    for user in (orga, other_orga, outsider, abroad, power):
        log_in_user(user.id)

    builder = _Builder(confirmer)
    names = {
        'a': f'Site Dash A {suffix}',
        'b': f'Site Dash B {suffix}',
        'c': f'Site Dash C {suffix}',
    }
    a = builder.tournament(party.id, names['a'])
    b = builder.tournament(party.id, names['b'])
    c = builder.tournament(other_party.id, names['c'])

    a1 = builder.duel(a, players[0], players[1])
    a2 = builder.duel(a, players[2], players[3], wait_minutes=25)
    a3 = builder.duel(a, players[0], players[2], done=True)
    b1 = builder.duel(b, players[0], players[1])
    c1 = builder.duel(c, players[0], players[1])

    for tournament_id, user in ((a, orga), (b, other_orga), (c, orga)):
        orgas.assign_orga(tournament_id, user.id, user.id).unwrap()
    # `abroad` is assigned in the other party only.
    orgas.assign_orga(c, abroad.id, abroad.id).unwrap()
    orgas.assign_orga(a, power.id, power.id).unwrap()
    db.session.commit()

    return World(
        party_id=party.id,
        orga=orga,
        other_orga=other_orga,
        power=power,
        outsider=outsider,
        abroad=abroad,
        viewer=viewer,
        admin=admin,
        a=a,
        b=b,
        c=c,
        a1=a1,
        a2=a2,
        a3=a3,
        b1=b1,
        c1=c1,
        names=names,
    )


@pytest.fixture(autouse=True)
def _leave_no_transaction():
    yield
    db.session.rollback()


# -------------------------------------------------------------------- #
# helpers


def _client(site_app, user=None):
    if user is None:
        return site_app.test_client()

    with http_client(site_app, user_id=user.id) as client:
        return client


def _token(client) -> str:
    """Open the dashboard once, the way a browser does, and read the token."""
    response = client.get(DASHBOARD_URL)
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session[CSRF_SESSION_KEY]['token']


def _url(kind, match_id) -> str:
    return f'{DASHBOARD_URL}/matches/{match_id}/{kind}'


def _open_episode(match_id):
    db.session.rollback()
    row = db.session.execute(
        text(
            'SELECT id, ack_revision FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :m AND closed_at IS NULL'
        ),
        {'m': match_id},
    ).one()
    db.session.rollback()
    return row.id, row.ack_revision


def _ack_data(token, match_id, **overrides):
    episode_id, revision = _open_episode(match_id)
    data = {
        'csrf_token': token,
        'episode': str(episode_id),
        'revision': str(revision),
        'comment': 'Both teams were asked to come to the desk.',
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _pin_data(token, **overrides):
    data = {'csrf_token': token, 'revision': '0', 'pinned': 'true'}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _data(kind, token, match_id, **overrides):
    if kind == 'ack':
        return _ack_data(token, match_id, **overrides)
    return _pin_data(token, **overrides)


def _post(client, kind, match_id, data, *, json_mode=True):
    return client.post(
        _url(kind, match_id),
        data=data,
        headers=JSON if json_mode else None,
    )


def _snapshot():
    db.session.rollback()
    state = {
        table: [
            tuple(row)
            for row in db.session.execute(
                text(f'SELECT * FROM {table} ORDER BY 1')  # noqa: S608
            )
        ]
        for table in STATE_TABLES
    }
    db.session.rollback()
    return state


def _acks(match_id):
    db.session.rollback()
    rows = db.session.execute(
        text(
            'SELECT revision, comment FROM lan_tournament_match_escalation_acks'
            ' WHERE match_id = :m ORDER BY revision'
        ),
        {'m': match_id},
    ).all()
    db.session.rollback()
    return [tuple(row) for row in rows]


def _pin(match_id):
    db.session.rollback()
    row = db.session.execute(
        text(
            'SELECT revision, pinned_at IS NOT NULL AS pinned'
            ' FROM lan_tournament_match_dashboard_annotations'
            ' WHERE match_id = :m'
        ),
        {'m': match_id},
    ).first()
    db.session.rollback()
    return None if row is None else tuple(row)


def _stored_time(kind, match_id) -> str:
    """Return the stored time of the last write, as the transport spells it."""
    table, column = {
        'ack': ('lan_tournament_match_escalation_acks', 'occurred_at'),
        'pin': ('lan_tournament_match_dashboard_annotations', 'updated_at'),
    }[kind]
    db.session.rollback()
    value = db.session.execute(
        text(f'SELECT {column} FROM {table} WHERE match_id = :m'),  # noqa: S608
        {'m': match_id},
    ).scalar_one()
    db.session.rollback()
    return value.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _pause(tournament_id) -> None:
    repo.set_tournament_status_flush(
        tournament_id, TournamentStatus.PAUSED
    ).unwrap()
    db.session.commit()


def _localized(app, message) -> str:
    """Return the text as the site's own request would render it."""
    with app.test_request_context(BASE_URL):
        g.user = CurrentUser.create_anonymous(None)
        g.locales = []
        return gettext(message) if isinstance(message, str) else str(message)


def _row(context, match_id):
    (row,) = [
        row
        for row in context['dashboard']['rows']
        if row['match_id'] == str(match_id)
    ]
    return row


def _row_ids(context) -> set[str]:
    return {row['match_id'] for row in context['dashboard']['rows']}


def _assert_plain(value, path='context') -> None:
    """Fail on anything but plain data: a renderer could reach it."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), path
            _assert_plain(item, f'{path}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_plain(item, f'{path}[{index}]')
    else:
        assert value is None or isinstance(value, (str, int, float, bool)), (
            f'{path}: {type(value).__name__}'
        )


class _Statements:
    """Record the statements the engine runs."""

    def __enter__(self):
        self.sql: list[str] = []
        event.listen(Engine, 'before_cursor_execute', self._record)
        return self

    def __exit__(self, *exc_info):
        event.remove(Engine, 'before_cursor_execute', self._record)

    def _record(self, conn, cursor, statement, *args):
        self.sql.append(statement)

    def writes(self) -> list[str]:
        return [
            statement
            for statement in self.sql
            if statement.lstrip()
            .upper()
            .startswith(('INSERT', 'UPDATE', 'DELETE'))
            or 'FOR UPDATE' in statement.upper()
        ]


# -------------------------------------------------------------------- #
# the collection: at least one assignment at the current party


def test_site_collection_requires_current_party_assignment(
    site_app, world, spy
):
    page = _client(site_app, world.orga).get(DASHBOARD_URL)

    assert page.status_code == 200
    assert page.headers['Cache-Control'] == NO_STORE
    ((name, context),) = spy.calls
    assert name == PAGE_TEMPLATE
    assert set(context) == {'dashboard', 'party', 'action', 'unavailable'}
    assert context['action'] is None and context['unavailable'] is None
    assert context['party'].id == world.party_id
    assert context['dashboard']['surface'] == 'site'
    # Only the assigned tournament of this party: not B, not the other party.
    assert _row_ids(context) == {str(world.a1), str(world.a2)}


@pytest.mark.parametrize(
    'actor',
    ['outsider', 'viewer', 'admin', 'abroad'],
    ids=[
        'plain user',
        'view permission only',
        'global administrate, no assignment',
        'assigned in another party only',
    ],
)
def test_a_viewer_without_assignment_is_refused_not_shown_an_empty_page(
    site_app, world, spy, actor
):
    client = _client(site_app, getattr(world, actor))

    page = client.get(DASHBOARD_URL)
    poll = client.get(POLL_URL)
    action = _post(client, 'pin', world.a1, _pin_data('x' * 43))

    assert page.status_code == 403
    assert spy.calls == []
    for response in (poll, action):
        assert response.status_code == 403
        assert response.get_json()['error'] == 'access_revoked'
        assert response.headers['Cache-Control'] == NO_STORE


def test_the_login_redirect_stays_for_the_native_page(site_app, world, spy):
    anonymous = _client(site_app)

    page = anonymous.get(DASHBOARD_URL)

    assert page.status_code == 302
    assert '/authentication/log_in' in page.headers['Location']
    assert spy.calls == []


def test_revocation_and_cross_party_ids_cannot_mutate(site_app, world, spy):
    client = _client(site_app, world.orga)
    token = _token(client)
    before = _snapshot()

    # Another party's match and another orga's match, both with a correct
    # episode and revision: the viewer's assignments decide, not the ID.
    for match_id in (world.c1, world.b1):
        for kind in ('pin', 'ack'):
            response = _post(
                client, kind, match_id, _data(kind, token, match_id)
            )
            assert response.status_code == 404, (kind, match_id)
            assert response.get_json()['error'] == 'unavailable'

    assert _snapshot() == before

    # The assignment is revoked while the page is open.
    orgas.revoke_orga(world.a, world.orga.id, world.other_orga.id).unwrap()
    db.session.commit()
    after_revoke = _snapshot()

    for kind in ('pin', 'ack'):
        response = _post(client, kind, world.a1, _data(kind, token, world.a1))
        assert response.status_code == 403
        assert response.get_json()['error'] == 'access_revoked'
        native = _post(
            client,
            kind,
            world.a1,
            _data(kind, token, world.a1),
            json_mode=False,
        )
        assert native.status_code == 403

    assert _snapshot() == after_revoke
    assert _acks(world.a1) == []
    assert _pin(world.a1) is None


def test_a_global_admin_acts_on_the_site_only_within_the_assignments(
    site_app, world, spy
):
    # The administrate permission opens the whole party in the backend, not
    # on the site: here the assignments decide, for the page and the action.
    client = _client(site_app, world.power)
    token = _token(client)
    assert _row_ids(spy.last[1]) == {str(world.a1), str(world.a2)}
    before = _snapshot()

    hidden = _post(client, 'pin', world.b1, _pin_data(token))
    missing = _post(client, 'pin', uuid4(), _pin_data(token))

    assert hidden.status_code == 404
    assert hidden.get_data() == missing.get_data()
    assert _snapshot() == before
    assert _pin(world.b1) is None
    assert _post(client, 'pin', world.a1, _pin_data(token)).status_code == 200


# -------------------------------------------------------------------- #
# CSRF


@pytest.mark.parametrize('kind', ['pin', 'ack'])
@pytest.mark.parametrize(
    'variant', ['missing', 'empty', 'malformed', 'wrong', 'foreign', 'long']
)
def test_site_pin_ack_require_csrf(site_app, world, spy, kind, variant):
    client = _client(site_app, world.orga)
    token = _token(client)
    foreign = _token(_client(site_app, world.other_orga))
    forged = {
        'missing': None,
        'empty': '',
        'malformed': 'not a token!',
        'wrong': 'A' * 43,
        'foreign': foreign,
        'long': 'A' * 5000,
    }[variant]
    before = _snapshot()
    spy.calls.clear()

    response = _post(
        client,
        kind,
        world.a1,
        _data(kind, token, world.a1, csrf_token=forged),
    )

    assert response.status_code == 403
    body = response.get_json()
    # A refused check is not lost authority: the client keeps its data.
    assert body['error'] == 'csrf_invalid'
    assert body['message'] == _localized(site_app, CSRF_INVALID_NOTICE)
    assert 'fragment' not in body
    assert _snapshot() == before
    assert spy.calls == []

    positive = _post(client, kind, world.a1, _data(kind, token, world.a1))
    assert positive.status_code == 200


def test_a_native_csrf_failure_rerenders_the_list_with_the_draft(
    site_app, world, spy
):
    client = _client(site_app, world.orga)
    token = _token(client)
    spy.calls.clear()
    before = _snapshot()

    response = _post(
        client,
        'ack',
        world.a1,
        _ack_data(token, world.a1, csrf_token='A' * 43),
        json_mode=False,
    )

    assert response.status_code == 403
    assert response.headers['Cache-Control'] == NO_STORE
    ((name, context),) = spy.calls
    assert name == PAGE_TEMPLATE
    action = context['action']
    assert action['kind'] == 'ack'
    assert action['match_id'] == str(world.a1)
    assert action['error'] == 'csrf_invalid'
    assert action['draft'] == 'Both teams were asked to come to the desk.'
    assert action['draft_target'] is True
    form = _row(context, world.a1)['ack']['form']
    assert form['open'] is True
    assert form['draft'] == action['draft']
    assert _snapshot() == before


# -------------------------------------------------------------------- #
# the poll


def test_site_poll_is_private_read_only_and_sanitized(site_app, world, spy):
    client = _client(site_app, world.orga)
    _token(client)
    spy.calls.clear()
    before = _snapshot()

    with _Statements() as statements:
        response = client.get(f'{POLL_URL}?view=all')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == NO_STORE
    assert response.mimetype == 'application/json'
    body = response.get_json()
    assert set(body) == {'html', 'as_of', 'poll_seconds'}
    assert body['html'] == f'<rendered {PANEL_TEMPLATE}>'
    assert body['as_of'].endswith('Z') and len(body['as_of']) == 27
    assert body['poll_seconds'] == 30

    # No write of any kind, and no state moved: no clock, no annotation.
    assert statements.writes() == []
    assert _snapshot() == before

    ((name, context),) = spy.calls
    assert name == PANEL_TEMPLATE
    assert set(context) == {'dashboard', 'action'}
    assert context['action'] is None
    _assert_plain(context['dashboard'])
    # `view=all` reaches the assigned tournament only. The hidden tournaments
    # share people with it, yet nothing about them is in the transport.
    assert _row_ids(context) == {str(world.a1), str(world.a2), str(world.a3)}
    transport = json.dumps(context['dashboard']) + response.get_data(
        as_text=True
    )
    for hidden in (world.b1, world.c1, world.b, world.c):
        assert str(hidden) not in transport
    assert world.names['b'] not in transport
    assert world.names['c'] not in transport


def test_the_poll_refreshes_the_scope_every_time(site_app, world, spy):
    client = _client(site_app, world.orga)

    # Having no assignment is not remembered either.
    newcomer = _client(site_app, world.outsider)
    assert newcomer.get(POLL_URL).status_code == 403
    orgas.assign_orga(world.a, world.outsider.id, world.orga.id).unwrap()
    db.session.commit()
    assert newcomer.get(POLL_URL).status_code == 200
    assert _row_ids(spy.last[1]) == {str(world.a1), str(world.a2)}

    assert client.get(POLL_URL).status_code == 200
    assert _row_ids(spy.last[1]) == {str(world.a1), str(world.a2)}

    # A new assignment is visible on the next poll ...
    orgas.assign_orga(world.b, world.orga.id, world.other_orga.id).unwrap()
    db.session.commit()
    assert client.get(POLL_URL).status_code == 200
    assert str(world.b1) in _row_ids(spy.last[1])

    # ... and the loss of every assignment ends it.
    for tournament_id in (world.a, world.b):
        orgas.revoke_orga(tournament_id, world.orga.id, world.other_orga.id)
    db.session.commit()
    revoked = client.get(POLL_URL)

    assert revoked.status_code == 403
    assert revoked.get_json()['error'] == 'access_revoked'


def test_site_poll_answers_401_and_403_json_not_redirect(site_app, world, spy):
    anonymous = _client(site_app)

    for headers in (None, {'Accept': 'text/html'}, JSON):
        response = anonymous.get(POLL_URL, headers=headers)
        assert response.status_code == 401
        assert response.get_json()['error'] == 'session_expired'
        assert 'Location' not in response.headers
        assert response.headers['Cache-Control'] == NO_STORE

    for kind in ('pin', 'ack'):
        response = _post(anonymous, kind, world.a1, {'csrf_token': 'x' * 43})
        assert response.status_code == 401
        assert response.get_json()['error'] == 'session_expired'
        assert 'Location' not in response.headers

    outsider = _client(site_app, world.outsider).get(
        POLL_URL, headers={'Accept': 'text/html'}
    )
    assert outsider.status_code == 403
    assert outsider.get_json()['error'] == 'access_revoked'
    assert 'Location' not in outsider.headers

    # A browser form still gets the login redirect of the core.
    native = _post(
        anonymous, 'pin', world.a1, {'csrf_token': 'x' * 43}, json_mode=False
    )
    assert native.status_code == 302
    assert '/authentication/log_in' in native.headers['Location']
    assert spy.calls == []


# -------------------------------------------------------------------- #
# native errors


def test_site_native_error_rerenders_open_form_with_draft(site_app, world, spy):
    client = _client(site_app, world.orga)
    token = _token(client)
    before = _snapshot()
    comment = 'x' * 501

    spy.calls.clear()
    response = _post(
        client,
        'ack',
        world.a1,
        _ack_data(token, world.a1, comment=comment, **{'return': RETURN_ALL}),
        json_mode=False,
    )

    assert response.status_code == 422
    assert response.headers['Cache-Control'] == NO_STORE
    ((name, context),) = spy.calls
    assert name == PAGE_TEMPLATE
    assert set(context) == {'dashboard', 'party', 'action', 'unavailable'}
    action = context['action']
    assert set(action) == {
        'kind',
        'match_id',
        'error',
        'message',
        'detail',
        'draft',
        'draft_target',
        'field_error',
    }
    assert (action['kind'], action['match_id']) == ('ack', str(world.a1))
    assert action['error'] == 'invalid'
    field_error = action['field_error']
    assert field_error['field'] == 'comment'
    assert '501' in field_error['text']
    assert action['message'] == field_error['text']
    assert action['draft'] == comment
    assert action['draft_target'] is True
    # The list is the one the form came from (`return`), on the server's
    # state, and the row's form is open with the draft and the error.
    assert str(world.a3) in _row_ids(context)
    form = _row(context, world.a1)['ack']['form']
    assert form['open'] is True
    assert form['draft'] == comment
    assert form['is_over'] is True
    assert '501' in form['counter_text']
    assert form['error_text'] == field_error['text']
    # No other row's form is touched.
    assert 'open' not in _row(context, world.a2)['ack']['form']
    assert _snapshot() == before
    _assert_plain(context['dashboard'])


def test_a_stale_native_ack_keeps_the_form_open_with_the_draft(
    site_app, world, spy
):
    client = _client(site_app, world.orga)
    token = _token(client)
    before = _snapshot()

    spy.calls.clear()
    response = _post(
        client,
        'ack',
        world.a1,
        _ack_data(token, world.a1, revision='7'),
        json_mode=False,
    )

    assert response.status_code == 409
    action = spy.last[1]['action']
    assert action['error'] == 'stale'
    assert action['message'] == _localized(site_app, 'dashboard_ack_conflict')
    assert action['draft_target'] is True
    assert action['field_error'] is None
    form = _row(spy.last[1], world.a1)['ack']['form']
    assert form['open'] is True
    assert form['draft'] == 'Both teams were asked to come to the desk.'
    assert form['is_over'] is False
    assert form['error_text'] is None
    assert _snapshot() == before


def test_a_stale_native_pin_names_the_row_without_a_draft(site_app, world, spy):
    client = _client(site_app, world.orga)
    token = _token(client)

    spy.calls.clear()
    response = _post(
        client,
        'pin',
        world.a1,
        _pin_data(token, revision='3'),
        json_mode=False,
    )

    assert response.status_code == 409
    action = spy.last[1]['action']
    assert (action['kind'], action['error']) == ('pin', 'stale')
    assert action['match_id'] == str(world.a1)
    assert action['draft'] is None
    assert action['draft_target'] is True
    assert _pin(world.a1) is None


def test_a_refused_native_ack_keeps_the_draft_when_the_row_has_left(
    site_app, world, spy
):
    client = _client(site_app, world.orga)
    token = _token(client)
    data = _ack_data(token, world.a1, **{'return': RETURN_DUE})
    # Meanwhile the tournament is paused: the match leaves `view=due`.
    _pause(world.a)

    spy.calls.clear()
    response = _post(client, 'ack', world.a1, data, json_mode=False)

    assert response.status_code == 409
    context = spy.last[1]
    action = context['action']
    assert action['error'] == 'refused'
    assert action['draft'] == 'Both teams were asked to come to the desk.'
    assert action['draft_target'] is False
    with site_app.test_request_context(BASE_URL):
        g.user = CurrentUser.create_anonymous(None)
        g.locales = []
        labels = helpers.dashboard_labels()
        reason = gettext('dashboard_ack_paused')
    assert action['message'] == labels['ack_refused_template'] % {
        'reason': reason
    }
    assert action['detail'] == labels['ack_refused_detail']
    # The row is gone from the list, so the note does not name it.
    assert action['match_id'] is None
    assert str(world.a1) not in _row_ids(context)
    assert _acks(world.a1) == []


def test_a_native_success_redirects_to_the_validated_list(
    site_app, world, spy, monkeypatch
):
    flashed = []
    monkeypatch.setattr(site_views, 'flash_success', flashed.append)
    client = _client(site_app, world.orga)
    token = _token(client)
    spy.calls.clear()

    response = _post(
        client,
        'ack',
        world.a1,
        _ack_data(token, world.a1, **{'return': RETURN_ALL}),
        json_mode=False,
    )

    assert response.status_code == 303
    with site_app.test_request_context(BASE_URL):
        expected = helpers.build_dashboard_list_url(
            'site',
            world.party_id,
            DashboardQuery(view='all', per_page=50),
            anchor_match_id=world.a1,
        )
    location = urlsplit(response.headers['Location'])
    assert location.path == '/lan-tournaments/orga-dashboard'
    assert location.fragment == f'lt-row-{world.a1}'
    assert 'view=all' in location.query
    rebuilt = urlsplit(expected)
    assert (location.path, location.query, location.fragment) == (
        rebuilt.path,
        rebuilt.query,
        rebuilt.fragment,
    )
    assert _acks(world.a1) == [
        (1, 'Both teams were asked to come to the desk.')
    ]
    # The outcome is flashed for the next page, naming the match.
    assert len(flashed) == 1
    assert world.names['a'] in flashed[0]
    assert spy.calls == []


@pytest.mark.parametrize(
    'hostile',
    [
        'https://evil.example/steal',
        '//evil.example/',
        '/lan-tournaments/orga-dashboard?view=due',
        'javascript:alert(1)',
        'view=bogus',
        'view=all&evil=1',
        'view=all&view=due',
        'x' * 5000,
    ],
)
def test_the_return_value_never_becomes_the_redirect_target(
    site_app, world, spy, hostile
):
    client = _client(site_app, world.orga)
    token = _token(client)

    response = _post(
        client,
        'pin',
        world.a1,
        _pin_data(token, **{'return': hostile}),
        json_mode=False,
    )

    assert response.status_code == 303
    location = urlsplit(response.headers['Location'])
    assert location.netloc == ''
    assert location.path == '/lan-tournaments/orga-dashboard'
    assert location.fragment == f'lt-row-{world.a1}'
    assert 'evil' not in response.headers['Location']
    assert 'view=all' not in location.query
    assert _pin(world.a1) == (1, True)


# -------------------------------------------------------------------- #
# JSON success and refusal


@pytest.mark.parametrize('kind', ['pin', 'ack'])
def test_a_json_success_returns_the_commit_time_and_a_fresh_panel(
    site_app, world, spy, kind
):
    client = _client(site_app, world.orga)
    token = _token(client)
    spy.calls.clear()

    response = _post(
        client,
        kind,
        world.a1,
        _data(kind, token, world.a1, **{'return': RETURN_ALL}),
    )

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == NO_STORE
    body = response.get_json()
    assert set(body) == {'committed_at', 'fragment'}
    assert body['committed_at'] == _stored_time(kind, world.a1)
    assert set(body['fragment']) == {'html', 'as_of', 'poll_seconds'}
    ((name, context),) = spy.calls
    assert name == PANEL_TEMPLATE
    # The panel was read after the commit, with the list the form came from.
    row = _row(context, world.a1)
    assert str(world.a3) in _row_ids(context)
    if kind == 'ack':
        assert row['ack']['record']['actor']
        assert _acks(world.a1)[0][0] == 1
    else:
        assert row['pin']['is_pinned'] is True
        assert _pin(world.a1) == (1, True)


@pytest.mark.parametrize(
    'overrides',
    [
        {'revision': None},
        {'revision': 'abc'},
        {'revision': '9' * 40},
        {'episode': 'not-a-uuid'},
        {'episode': None},
        {'comment': 'a' * 100_000},
        {'comment': 'bad \x00 text'},
    ],
    ids=[
        'no revision',
        'revision text',
        'giant revision',
        'malformed episode',
        'no episode',
        'giant comment',
        'control character',
    ],
)
def test_malformed_ack_input_is_a_422_with_the_whole_panel(
    site_app, world, spy, overrides
):
    client = _client(site_app, world.orga)
    token = _token(client)
    before = _snapshot()
    spy.calls.clear()

    response = _post(
        client, 'ack', world.a1, _ack_data(token, world.a1, **overrides)
    )

    assert response.status_code == 422
    body = response.get_json()
    assert body['error'] == 'invalid'
    assert body['draft_target'] is True
    assert set(body['fragment']) == {'html', 'as_of', 'poll_seconds'}
    assert body['message']
    assert _snapshot() == before


def test_site_refusal_returns_full_panel_and_draft_target(site_app, world, spy):
    client = _client(site_app, world.orga)
    token = _token(client)
    tiles_before = [tile['count'] for tile in _tiles(client, spy)]
    assert tiles_before == [0, 2, 0]

    # The row stays, the action is still offered: the draft goes back in.
    stale = _post(
        client, 'ack', world.a1, _ack_data(token, world.a1, revision='5')
    )
    assert stale.status_code == 409
    body = stale.get_json()
    assert body['error'] == 'stale'
    assert body['draft_target'] is True
    assert set(body['fragment']) == {'html', 'as_of', 'poll_seconds'}
    assert body['fragment']['html'] == f'<rendered {PANEL_TEMPLATE}>'
    assert body['message'] == _localized(site_app, 'dashboard_ack_conflict')
    assert spy.last[0] == PANEL_TEMPLATE
    assert spy.last[1]['action'] is None

    # The tournament is paused meanwhile: the row leaves `view=due`, and so
    # do the counts. One panel carries all of it, from one snapshot.
    data = _ack_data(token, world.a1, **{'return': RETURN_DUE})
    _pause(world.a)
    refused = _post(client, 'ack', world.a1, data)

    assert refused.status_code == 409
    body = refused.get_json()
    assert body['error'] == 'refused'
    assert body['draft_target'] is False
    context = spy.last[1]
    assert str(world.a1) not in _row_ids(context)
    assert [t['count'] for t in context['dashboard']['tiles']['items']] == [
        0,
        0,
        0,
    ]
    assert _acks(world.a1) == []

    # The same row in `view=all` is still there, but the action is not
    # offered any more: not a draft target either.
    data = _ack_data(token, world.a1, **{'return': RETURN_ALL})
    refused = _post(client, 'ack', world.a1, data)
    assert refused.status_code == 409
    assert refused.get_json()['draft_target'] is False
    assert str(world.a1) in _row_ids(spy.last[1])
    assert _row(spy.last[1], world.a1)['ack']['offered'] is False

    # A confirmed match takes no pin: refused, shown read only.
    pin = _post(
        client,
        'pin',
        world.a3,
        _pin_data(token, **{'return': RETURN_ALL}),
    )
    assert pin.status_code == 409
    assert pin.get_json()['error'] == 'refused'
    assert pin.get_json()['draft_target'] is False
    assert _pin(world.a3) is None


def test_site_json_stale_ack_names_the_other_orga_like_the_page(
    site_app, world, spy
):
    client = _client(site_app, world.orga)
    token = _token(client)
    first = _post(
        client,
        'ack',
        world.a1,
        _ack_data(token, world.a1, **{'return': RETURN_ALL}),
    )
    assert first.status_code == 200
    stale_form = _ack_data(
        token, world.a1, revision='0', **{'return': RETURN_ALL}
    )

    # The row is in the re-read: the script gets the sentence of the page,
    # from the same record.
    response = _post(client, 'ack', world.a1, stale_form)
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert body['draft_target'] is False  # just checked: not offered again
    assert world.orga.screen_name in body['detail']
    native = _post(client, 'ack', world.a1, stale_form, json_mode=False)
    assert native.status_code == 409
    assert spy.last[1]['action']['detail'] == body['detail']
    expected = _localized(
        site_app,
        '%(actor)s already recorded this delay at %(time)s.'
        ' Your draft is kept below and was not sent.',
    )
    assert body['detail'].startswith(
        expected.split('%(time)s')[0].replace(
            '%(actor)s', world.orga.screen_name
        )
    )

    # Not in the re-read (the list the form came from does not hold it):
    # nothing is named.
    hidden = {**stale_form, 'return': RETURN_RED}
    response = _post(client, 'ack', world.a1, hidden)
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert 'detail' not in body
    assert str(world.a1) not in _row_ids(spy.last[1])

    # A match the viewer cannot reach says nothing at all.
    response = _post(client, 'ack', world.b1, _ack_data(token, world.b1))
    assert response.status_code == 404
    assert 'detail' not in response.get_json()

    # Other codes carry no detail: the script words them itself.
    refused = _post(
        client,
        'ack',
        world.a1,
        _ack_data(token, world.a1, **{'return': RETURN_ALL}),
    )
    body = refused.get_json()
    assert (refused.status_code, body['error']) == (409, 'refused')
    assert 'detail' not in body
    invalid = _post(
        client,
        'ack',
        world.a2,
        _ack_data(token, world.a2, comment='y' * 501),
    )
    body = invalid.get_json()
    assert (invalid.status_code, body['error']) == (422, 'invalid')
    assert 'detail' not in body
    pin = _post(client, 'pin', world.a2, _pin_data(token, revision='9'))
    body = pin.get_json()
    assert (pin.status_code, body['error']) == (409, 'stale')
    assert 'detail' not in body
    forged = _post(
        client,
        'ack',
        world.a2,
        _ack_data('x' * 43, world.a2),
    )
    assert forged.get_json()['error'] == 'csrf_invalid'
    assert 'detail' not in forged.get_json()


def _tiles(client, spy):
    response = client.get(POLL_URL)
    assert response.status_code == 200
    return spy.last[1]['dashboard']['tiles']['items']


# -------------------------------------------------------------------- #
# the uniform lookup


def _unavailable_cases(world):
    return {
        'missing': uuid4(),
        'malformed': 'not-a-uuid',
        'numeric': '12345',
        'other party': world.c1,
        'other tournament': world.b1,
    }


@pytest.mark.parametrize('kind', ['pin', 'ack'])
def test_site_missing_and_hidden_match_responses_are_identical(
    site_app, world, spy, kind
):
    client = _client(site_app, world.orga)
    token = _token(client)
    # Whatever the ID, the form is the same: a hidden match must not be
    # told from a missing one by its answer to a well-formed request.
    data = _pin_data(token) if kind == 'pin' else _ack_data(token, world.a1)
    before = _snapshot()

    for json_mode in (True, False):
        spy.calls.clear()
        answers = {}
        contexts = {}
        for label, match_id in _unavailable_cases(world).items():
            response = _post(client, kind, match_id, data, json_mode=json_mode)
            answers[label] = (
                response.status_code,
                response.get_data(),
                response.headers['Content-Length'],
                sorted(response.headers.items()),
            )
            contexts[label] = spy.calls[-1] if spy.calls else None

        assert len({repr(answer) for answer in answers.values()}) == 1, answers
        status, body, *_ = answers['missing']
        assert status == 404
        if json_mode:
            assert json.loads(body)['error'] == 'unavailable'
        else:
            # The page gets one fixed context, whatever was asked for.
            name, context = contexts['missing']
            assert name == PAGE_TEMPLATE
            assert context['dashboard'] is None
            assert context['action'] is None
            assert set(context['unavailable']) == {
                'heading',
                'detail',
                'back_label',
                'back_url',
            }
            assert context['unavailable']['back_url'] == (
                '/lan-tournaments/orga-dashboard'
            )
            assert len({repr(c) for c in contexts.values()}) == 1
    assert _snapshot() == before


def test_site_post_lookup_is_uniform_404_after_collection_check(
    site_app, world, spy
):
    # No assignment: the collection answer comes first, for every ID.
    outsider = _client(site_app, world.outsider)
    for match_id in (world.a1, world.b1, uuid4(), 'not-a-uuid'):
        response = _post(outsider, 'ack', match_id, {'csrf_token': 'x' * 43})
        assert response.status_code == 403
        assert response.get_json()['error'] == 'access_revoked'

    # With an assignment, an existing match outside it reads like a missing
    # one: not 403, which would tell that it exists.
    client = _client(site_app, world.orga)
    token = _token(client)
    answers = {
        label: _post(client, 'ack', match_id, _ack_data(token, world.a1))
        for label, match_id in _unavailable_cases(world).items()
    }
    assert {r.status_code for r in answers.values()} == {404}
    assert len({r.get_data() for r in answers.values()}) == 1

    # The other orga sees B1 but not A1: the same rule, from the other side.
    other = _client(site_app, world.other_orga)
    other_token = _token(other)
    assert (
        _post(other, 'pin', world.b1, _pin_data(other_token)).status_code == 200
    )
    hidden = _post(other, 'pin', world.a1, _pin_data(other_token))
    assert hidden.status_code == 404
    assert hidden.get_data() == answers['missing'].get_data()

    # The dashboard routes do not use the decorator and the helper that
    # answer 404 for a missing match but 403 for one the viewer may not see.
    for function in (
        site_views.orga_dashboard,
        site_views.orga_dashboard_poll,
        site_views.orga_dashboard_pin,
        site_views.orga_dashboard_ack,
        site_views._handle_dashboard_action,
        site_views._is_dashboard_match,
    ):
        source = inspect.getsource(function)
        assert 'scoped_orga_required' not in source
        assert '_get_orga_match_and_tournament_or_404' not in source


# -------------------------------------------------------------------- #
# discoverability: the link authority is the server's


def test_site_index_link_authority_comes_from_the_server(site_app, world):
    seen = []

    def record(sender, template, context, **extra):
        if 'may_view_orga_dashboard' in context and 'tournaments' in context:
            seen.append(bool(context['may_view_orga_dashboard']))

    template_rendered.connect(record, site_app)
    try:
        answers = {}
        for label, user in (
            ('anonymous', None),
            ('outsider', world.outsider),
            ('abroad', world.abroad),
            ('admin', world.admin),
            ('orga', world.orga),
        ):
            seen.clear()
            response = _client(site_app, user).get(f'{BASE_URL}/')
            assert response.status_code == 200, label
            answers[label] = list(seen)
    finally:
        template_rendered.disconnect(record, site_app)

    assert answers == {
        'anonymous': [False],
        'outsider': [False],
        'abroad': [False],
        'admin': [False],
        'orga': [True],
    }


def test_the_link_authority_costs_nothing_until_it_is_asked_for(
    site_app, world, party
):
    with site_app.test_request_context(BASE_URL):
        g.user = CurrentUser.create_authenticated(world.orga, None, frozenset())
        g.party = party

        with _Statements() as statements:
            authority = site_views._provide_orga_dashboard_authority()[
                'may_view_orga_dashboard'
            ]
        assert statements.sql == []

        with _Statements() as statements:
            assert bool(authority) is True
            assert bool(authority) is True
        assert 0 < len(statements.sql) <= 3

        # The answer is kept for the rest of the request.
        with _Statements() as statements:
            assert bool(authority) is True
        assert statements.sql == []


# -------------------------------------------------------------------- #
# the routes themselves


def test_the_four_dashboard_endpoints_are_the_ones_the_context_links_to(
    site_app,
):
    registered = {
        rule.endpoint: (rule.rule, sorted(rule.methods - {'HEAD', 'OPTIONS'}))
        for rule in site_app.url_map.iter_rules()
        if rule.endpoint.startswith('lan_tournament.orga_dashboard')
    }

    assert registered == {
        'lan_tournament.orga_dashboard': (
            '/lan-tournaments/orga-dashboard',
            ['GET'],
        ),
        'lan_tournament.orga_dashboard_poll': (
            '/lan-tournaments/orga-dashboard/poll',
            ['GET'],
        ),
        'lan_tournament.orga_dashboard_pin': (
            '/lan-tournaments/orga-dashboard/matches/<match_id>/pin',
            ['POST'],
        ),
        'lan_tournament.orga_dashboard_ack': (
            '/lan-tournaments/orga-dashboard/matches/<match_id>/ack',
            ['POST'],
        ),
    }
    for key in ('list', 'poll', 'pin', 'ack'):
        assert helpers.DASHBOARD_ENDPOINTS['site'][key] in registered
