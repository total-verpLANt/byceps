from datetime import datetime, timedelta
import inspect
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from babel.messages.extract import extract
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
import flask_babel
import pytest
from sqlalchemy import event, text

from byceps.database import db
from byceps.services.authz import authz_service
from byceps.services.authz.models import PermissionID, RoleID
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_log_service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.blueprints.admin import (
    views as admin_views,
)
from byceps.services.lan_tournament.blueprints.dashboard_csrf import (
    CSRF_SESSION_KEY,
)
from byceps.services.lan_tournament.dashboard_view_helpers import (
    build_dashboard_return,
    TRANSPORT_ERRORS,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
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
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_dashboard_settings_service import (
    get_effective_dashboard_settings,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7

from tests.helpers import create_role_with_permissions_assigned, log_in_user


BASE = 'http://admin.acmecon.test/lan-tournaments'
JSON = {'Accept': 'application/json'}
PATH = '/lan-tournaments/for_party'

MINUTE_US = 60_000_000
NOW = datetime(2026, 10, 8, 12, 0, 0)
CLOCK_START = NOW - timedelta(hours=3)
CLOCK_AT_NOW_US = 180 * MINUTE_US
# A fixed date long before any server clock: a write that stamps the match
# could not hide behind `GREATEST`.
LAST_CHANGED = datetime(2026, 1, 15, 8, 0, 0)

YELLOW_WAIT = 20
RED_WAIT = 50
GREEN_WAIT = 5

# The list a form came from: every tournament of the party, due now.
RETURN_ALL = build_dashboard_return(
    DashboardQuery(scope='all', per_page=50), surface='admin'
)

# The same list, but only the green tier: the red match is not in it.
RETURN_GREEN = build_dashboard_return(
    DashboardQuery(scope='all', state='tier-green', per_page=50),
    surface='admin',
)

ISO_UTC = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z')


# -- fixtures --


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'F03Rt{i:02d}') for i in range(12)]


@pytest.fixture(scope='module')
def confirmer(make_user):
    return make_user('F03RtConfirmer')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'},
        screen_name='F03RtAdmin',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def other_admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'},
        screen_name='F03RtOtherAdmin',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def view_only(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.view'}, screen_name='F03RtViewOnly'
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def no_backend_access(make_admin):
    """Hold the permission but not the backend: core reads them as anonymous."""
    user = make_admin(
        {'lan_tournament.administrate'}, screen_name='F03RtNoBackend'
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def admin_client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture(scope='module')
def other_admin_client(make_client, admin_app, other_admin):
    return make_client(admin_app, user_id=other_admin.id)


@pytest.fixture(scope='module')
def view_only_client(make_client, admin_app, view_only):
    return make_client(admin_app, user_id=view_only.id)


@pytest.fixture(scope='module')
def no_backend_client(make_client, admin_app, no_backend_access):
    return make_client(admin_app, user_id=no_backend_access.id)


@pytest.fixture
def anonymous_client(make_client, admin_app):
    return make_client(admin_app)


@pytest.fixture(autouse=True)
def _context(admin_app):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture(scope='module')
def german_translations():
    po_path = (
        Path(__file__).parents[4]
        / 'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        catalog = read_po(f, locale='de')
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return Translations(fp=buffer)


@pytest.fixture(autouse=True)
def german(monkeypatch, german_translations):
    """Read the catalogue as it stands, not the compiled one on disk."""
    monkeypatch.setattr(
        flask_babel.Domain,
        'get_translations',
        lambda self: german_translations,
    )


@pytest.fixture
def clock(monkeypatch):
    """Make `clock.now` the server time of every transaction."""
    state = SimpleNamespace(now=NOW)
    monkeypatch.setattr(repo, 'get_operation_time', lambda: state.now)
    return state


class Renders:
    """Stand in for the templates, which Issues 26 and 27 build.

    The answer is the render call as JSON, so a test reads what the route
    handed to the template. This proves the HTTP contract, not the markup.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, template_name, **context):
        self.calls.append((template_name, context))
        return json.dumps(
            {'template': template_name, **context}, default=str, sort_keys=True
        )


@pytest.fixture
def renders(monkeypatch):
    fake = Renders()
    monkeypatch.setattr(admin_views, 'render_template', fake)
    return fake


class Spy:
    def __init__(self, real) -> None:
        self.real = real
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.real(*args, **kwargs)


@pytest.fixture
def services(monkeypatch):
    """Record every call of the two services, and call through."""
    spies = SimpleNamespace(
        ack=Spy(admin_views.acknowledge_match),
        pin=Spy(admin_views.set_match_pin),
    )
    monkeypatch.setattr(admin_views, 'acknowledge_match', spies.ack)
    monkeypatch.setattr(admin_views, 'set_match_pin', spies.pin)
    return spies


# -- the world: two parties, due matches in one, one in the other --


class World:
    """Party A holds a yellow, a red and a green due match, party B one."""

    def __init__(self, make_party, brand, players, confirmer) -> None:
        self.confirmer = confirmer
        self.players = players
        self._joined: dict[tuple, TournamentParticipantID] = {}
        self._orders = iter(range(1, 10_000))

        self.party_a = self._party(make_party, brand, 'a')
        self.party_b = self._party(make_party, brand, 'b')
        self.tournament_a = self._tournament(self.party_a)
        self.tournament_b = self._tournament(self.party_b)
        self.yellow = self._duel(
            self.tournament_a, players[0], players[1], YELLOW_WAIT
        )
        self.red = self._duel(
            self.tournament_a, players[2], players[3], RED_WAIT
        )
        self.green = self._duel(
            self.tournament_a, players[4], players[5], GREEN_WAIT
        )
        self.foreign = self._duel(
            self.tournament_b, players[6], players[7], YELLOW_WAIT
        )

    def _party(self, make_party, brand, suffix: str) -> PartyID:
        party_id = PartyID(f'f03r-{suffix}-{uuid4().hex[:10]}')
        make_party(brand, party_id, f'F03 routes {party_id}')
        return party_id

    def _tournament(self, party_id: PartyID) -> TournamentID:
        tournament_id = TournamentID(uuid7())
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=party_id,
                name=f'Routes {tournament_id}',
                game=None,
                description=None,
                image_url=None,
                ruleset=None,
                start_time=None,
                created_at=NOW,
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
                operational_clock_running_since=CLOCK_START,
                operational_clock_activated_at=CLOCK_START,
            )
        )
        db.session.commit()
        return tournament_id

    def _duel(
        self, tournament_id, player_a, player_b, wait_minutes: int
    ) -> TournamentMatchID:
        match_id = TournamentMatchID(uuid7())
        db.session.add(
            DbTournamentMatch(
                match_id,
                tournament_id,
                NOW - timedelta(hours=1),
                match_order=next(self._orders),
                round=1,
                confirmed_by=None,
                phase=1,
                occupied_since=NOW - timedelta(minutes=30),
                last_changed_at=LAST_CHANGED,
            )
        )
        db.session.flush()
        for user in (player_a, player_b):
            db.session.add(
                DbTournamentMatchToContestant(
                    TournamentMatchToContestantID(uuid7()),
                    match_id,
                    NOW - timedelta(hours=1),
                    participant_id=self._join(tournament_id, user),
                )
            )
        db.session.flush()
        repo.open_due_episode_flush(
            MatchDueEpisode(
                id=MatchDueEpisodeID(uuid7()),
                tournament_id=tournament_id,
                match_id=match_id,
                pairing_key='key',
                opened_at=NOW - timedelta(minutes=wait_minutes),
                opened_clock_us=CLOCK_AT_NOW_US - wait_minutes * MINUTE_US,
            )
        )
        db.session.commit()
        return match_id

    def _join(self, tournament_id: TournamentID, user):
        key = (tournament_id, user.id)
        if key not in self._joined:
            participant = DbTournamentParticipant(
                TournamentParticipantID(uuid7()), user.id, tournament_id, NOW
            )
            db.session.add(participant)
            db.session.commit()
            self._joined[key] = participant.id
        return self._joined[key]

    def assign(self, tournament_id: TournamentID, user) -> None:
        """Make the user a scoped orga of the tournament."""
        db.session.add(
            DbTournamentOrga(
                TournamentOrgaID(uuid7()), tournament_id, user.id, NOW
            )
        )
        db.session.commit()

    def tournament_of(self, match_id) -> TournamentID:
        if match_id == self.foreign:
            return self.tournament_b
        return self.tournament_a

    def party_of(self, match_id) -> PartyID:
        return self.party_b if match_id == self.foreign else self.party_a

    def confirm(self, match_id) -> None:
        _execute(
            'UPDATE lan_tournament_matches SET confirmed_by = :user'
            ' WHERE id = :id',
            user=self.confirmer.id,
            id=match_id,
        )


@pytest.fixture
def world(make_party, brand, players, confirmer, clock):
    return World(make_party, brand, players, confirmer)


# -- raw reads through a connection of their own: only what is committed --


def _read(sql: str, **params) -> list[dict]:
    with db.engine.connect() as connection:
        rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


def _execute(sql: str, **params) -> None:
    with db.engine.begin() as connection:
        connection.execute(text(sql), params)


def _open_episode(match_id) -> dict:
    (episode,) = _read(
        'SELECT * FROM lan_tournament_match_due_episodes'
        ' WHERE match_id = :id AND closed_at IS NULL',
        id=match_id,
    )
    return episode


def _facts(world: World, match_id) -> dict:
    """Everything a request may or may not change, as committed."""
    return {
        'match': _read(
            'SELECT * FROM lan_tournament_matches WHERE id = :id',
            id=match_id,
        ),
        'episodes': _read(
            'SELECT * FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :id ORDER BY id',
            id=match_id,
        ),
        'acks': _read(
            'SELECT * FROM lan_tournament_match_escalation_acks'
            ' WHERE match_id = :id ORDER BY revision',
            id=match_id,
        ),
        'pins': _read(
            'SELECT * FROM lan_tournament_match_dashboard_annotations'
            ' WHERE match_id = :id',
            id=match_id,
        ),
        'audit': [
            (entry.event_type, entry.initiator_id, entry.data)
            for entry in tournament_log_service.get_entries_for_tournament(
                world.tournament_of(match_id)
            )
        ],
    }


# -- requests --


def _url(party_id, tail: str = '') -> str:
    return f'{BASE}/for_party/{party_id}/dashboard{tail}'


def _action_url(party_id, match_id, kind: str) -> str:
    return _url(party_id, f'/matches/{match_id}/{kind}')


def _login_next(response) -> str:
    """The path a login redirect brings the user back to."""
    query = parse_qs(urlsplit(response.headers['Location']).query)
    (target,) = query['next']
    return target


def _path_of(url: str) -> str:
    return url.removeprefix('http://admin.acmecon.test')


def _csrf_token(client, user) -> str:
    """Put a token of this user into the client's session and return it."""
    token = secrets.token_urlsafe(32)
    with client.session_transaction() as session:
        session[CSRF_SESSION_KEY] = {'user_id': str(user.id), 'token': token}
    return token


def _ack_data(token, match_id, **overrides) -> dict:
    """The fields of an ack form as the page rendered them."""
    episode = _open_episode(match_id)
    data = {
        'csrf_token': token,
        'episode': str(episode['id']),
        'revision': str(episode['ack_revision']),
        'return': RETURN_ALL,
        'comment': None,
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _pin_data(token, match_id, *, pinned: bool = True, **overrides) -> dict:
    pins = _read(
        'SELECT revision FROM lan_tournament_match_dashboard_annotations'
        ' WHERE match_id = :id',
        id=match_id,
    )
    data = {
        'csrf_token': token,
        'revision': str(pins[0]['revision'] if pins else 0),
        'return': RETURN_ALL,
        'pinned': 'true' if pinned else 'false',
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _post(client, party_id, match_id, kind, data, *, native=False):
    return client.post(
        _action_url(party_id, match_id, kind),
        data=data,
        headers=None if native else JSON,
    )


def _data(kind, token, match_id, **overrides) -> dict:
    if kind == 'ack':
        return _ack_data(token, match_id, **overrides)
    return _pin_data(token, match_id, **overrides)


def _page(response) -> dict:
    """The render call of a native answer."""
    return json.loads(response.get_data(as_text=True))


def _panel(body: dict) -> dict:
    """The dashboard context inside the fragment of a JSON answer."""
    return json.loads(body['fragment']['html'])['dashboard']


def _polled(body: dict) -> dict:
    """The dashboard context inside the body of a poll answer."""
    return json.loads(body['html'])['dashboard']


def _row(dashboard: dict, match_id) -> dict | None:
    for row in dashboard['rows']:
        if row['match_id'] == str(match_id):
            return row
    return None


def _row_ids(dashboard: dict) -> set[str]:
    return {row['match_id'] for row in dashboard['rows']}


def _tile_counts(dashboard: dict) -> dict[str, int]:
    return {item['tier']: item['count'] for item in dashboard['tiles']['items']}


def _snapshot(response) -> tuple:
    """Everything of a response a client could tell apart."""
    return (
        response.status_code,
        tuple(sorted(response.headers.items())),
        response.get_data(),
    )


# -- the nine named tests --


def test_admin_route_requires_administrate_not_view_only(
    world,
    renders,
    services,
    admin_client,
    admin,
    view_only_client,
    view_only,
):
    # The identity is also a scoped orga of the tournament. The services
    # would let it see its own tournaments, so only the gate keeps it out
    # of the backend dashboard.
    world.assign(world.tournament_a, view_only)
    token = _csrf_token(view_only_client, view_only)
    before = {
        match_id: _facts(world, match_id)
        for match_id in (world.yellow, world.red)
    }

    # A view-only identity is refused on every route, whatever it sends.
    list_response = view_only_client.get(_url(world.party_a))
    assert list_response.status_code == 403
    poll = view_only_client.get(_url(world.party_a, '/poll'), headers=JSON)
    assert (poll.status_code, poll.get_json()['error']) == (
        403,
        'access_revoked',
    )
    for kind in ('ack', 'pin'):
        data = _data(kind, token, world.yellow)
        json_response = _post(
            view_only_client, world.party_a, world.yellow, kind, data
        )
        assert (
            json_response.status_code,
            json_response.get_json()['error'],
        ) == (
            403,
            'access_revoked',
        )
        native = _post(
            view_only_client,
            world.party_a,
            world.yellow,
            kind,
            data,
            native=True,
        )
        assert native.status_code == 403
        assert 'Location' not in native.headers

    # The gate answers before the services are asked, and nothing is read
    # or rendered for the refused identity.
    assert services.ack.calls == []
    assert services.pin.calls == []
    assert renders.calls == []
    for match_id, facts in before.items():
        assert _facts(world, match_id) == facts

    # The same requests work for a holder of `lan_tournament.administrate`.
    assert admin_client.get(_url(world.party_a)).status_code == 200
    assert (
        admin_client.get(_url(world.party_a, '/poll'), headers=JSON).status_code
        == 200
    )
    admin_token = _csrf_token(admin_client, admin)
    response = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(admin_token, world.yellow),
    )
    assert response.status_code == 200
    assert len(_facts(world, world.yellow)['acks']) == 1


def test_admin_action_binds_selected_party(
    world, renders, services, admin_client, admin
):
    token = _csrf_token(admin_client, admin)
    foreign_before = _facts(world, world.foreign)
    yellow_before = _facts(world, world.yellow)

    # The party of the URL is the one the match must belong to. A hidden
    # field naming the match's own party changes nothing.
    for kind in ('ack', 'pin'):
        for native in (False, True):
            forged = _post(
                admin_client,
                world.party_a,
                world.foreign,
                kind,
                _data(kind, token, world.foreign, party_id=world.party_b),
                native=native,
            )
            assert forged.status_code == 404
            reverse = _post(
                admin_client,
                world.party_b,
                world.yellow,
                kind,
                _data(kind, token, world.yellow, party_id=world.party_a),
                native=native,
            )
            assert reverse.status_code == 404
    assert _facts(world, world.foreign) == foreign_before
    assert _facts(world, world.yellow) == yellow_before
    # The service saw the URL's party each time, never the form's.
    urls = {str(world.foreign): world.party_a, str(world.yellow): world.party_b}
    for spy in (services.ack, services.pin):
        assert len(spy.calls) == 4
        for (_, party_id, match_id), _kwargs in spy.calls:
            assert party_id == urls[match_id]

    # Through its own party's URL the match is reachable.
    own = _post(
        admin_client,
        world.party_b,
        world.foreign,
        'ack',
        _ack_data(token, world.foreign),
    )
    assert own.status_code == 200
    assert len(_facts(world, world.foreign)['acks']) == 1

    # The list of a party never shows another party's matches, also when a
    # tournament of the other party is asked for by ID.
    poll = admin_client.get(
        _url(world.party_b, '/poll?scope=all'), headers=JSON
    )
    assert _row_ids(_polled(poll.get_json())) == {str(world.foreign)}
    widened = admin_client.get(
        _url(world.party_b, f'/poll?scope=all&tournament={world.tournament_a}'),
        headers=JSON,
    )
    dashboard = _polled(widened.get_json())
    assert _row_ids(dashboard) == {str(world.foreign)}
    assert dashboard['banner']['kind'] == 'warn'
    assert dashboard['filters']['tournament']['is_invalid'] is True


def test_admin_pin_ack_require_csrf_and_current_revision(
    world,
    renders,
    services,
    admin_client,
    admin,
    other_admin_client,
    other_admin,
):
    token = _csrf_token(admin_client, admin)
    foreign_token = _csrf_token(other_admin_client, other_admin)
    before = _facts(world, world.yellow)

    # No token, a garbage token, a token of the other user and a token of
    # the right shape that was never issued: none of them reaches a service.
    refused = [
        None,
        '',
        'x' * 43,
        'short',
        foreign_token,
        secrets.token_urlsafe(32),
    ]
    for kind in ('ack', 'pin'):
        for bad in refused:
            response = _post(
                admin_client,
                world.party_a,
                world.yellow,
                kind,
                _data(kind, None, world.yellow, csrf_token=bad),
            )
            assert response.status_code == 403
            assert response.get_json()['error'] == 'csrf_invalid'
    assert services.ack.calls == services.pin.calls == []
    assert _facts(world, world.yellow) == before

    # A stale form cannot write: not the revision, not a copy of a request
    # that already won, not another episode.
    for revision in ('1', '2', '7', '2147483647'):
        response = _post(
            admin_client,
            world.party_a,
            world.yellow,
            'ack',
            _ack_data(token, world.yellow, revision=revision),
        )
        assert (response.status_code, response.get_json()['error']) == (
            409,
            'stale',
        )
    other_episode = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow, episode=str(uuid4())),
    )
    assert (other_episode.status_code, other_episode.get_json()['error']) == (
        409,
        'stale',
    )
    pin_stale = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'pin',
        _pin_data(token, world.yellow, revision='3'),
    )
    assert (pin_stale.status_code, pin_stale.get_json()['error']) == (
        409,
        'stale',
    )
    assert _facts(world, world.yellow) == before

    # The first request with the current values wins, its copy does not.
    form = _ack_data(token, world.yellow, comment='checked')
    first = _post(admin_client, world.party_a, world.yellow, 'ack', form)
    copy = _post(admin_client, world.party_a, world.yellow, 'ack', form)
    assert (first.status_code, copy.status_code) == (200, 409)
    assert copy.get_json()['error'] == 'stale'
    assert len(_facts(world, world.yellow)['acks']) == 1

    pin_form = _pin_data(token, world.yellow)
    first_pin = _post(
        admin_client, world.party_a, world.yellow, 'pin', pin_form
    )
    pin_copy = _post(admin_client, world.party_a, world.yellow, 'pin', pin_form)
    assert (first_pin.status_code, pin_copy.status_code) == (200, 409)
    (pin,) = _facts(world, world.yellow)['pins']
    assert pin['revision'] == 1

    # A native request without a token neither writes nor redirects: the
    # draft stays visible on the page that answers.
    native = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, csrf_token=None, comment='keep me'),
        native=True,
    )
    assert native.status_code == 403
    action = _page(native)['action']
    assert (action['error'], action['draft']) == ('csrf_invalid', 'keep me')
    assert _facts(world, world.red)['acks'] == []


def test_admin_native_and_json_responses_use_safe_urls(
    world, renders, admin_client, admin
):
    token = _csrf_token(admin_client, admin)
    anchor = f'#lt-row-{world.yellow}'
    list_path = f'{PATH}/{world.party_a}/dashboard'

    # A valid `return` is rebuilt from its parameters, with the row anchor.
    valid = 'scope=all&view=all&state=all&sort=wait&page=2'
    response = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'pin',
        _pin_data(token, world.yellow, **{'return': valid}),
        native=True,
    )
    assert response.status_code == 303
    assert response.headers['Location'] == (
        f'{list_path}?scope=all&view=all&sort=wait&page=2{anchor}'
    )

    # Nothing else of `return` ever reaches the redirect, and neither does a
    # `next` or `redirect` field. Each value holds a valid, non-default part,
    # so only a whole rejection equals the default list.
    hostile = [
        'https://evil.example/phish',
        '//evil.example/',
        'javascript:alert(1)',
        '/lan-tournaments/elsewhere',
        'view=all&evil=https://evil.example',
        'view=all&view=upcoming',
        'view=all&sort=bogus',
        'view=all&page=0',
        f'view=all&tournament={uuid4()}&x=1',
        'view=all&' + 'a' * 5000,
        '\r\nLocation: https://evil.example',
        '%0d%0aSet-Cookie:x=1',
    ]
    for index, value in enumerate(hostile):
        match_id = (world.yellow, world.red, world.green)[index % 3]
        response = _post(
            admin_client,
            world.party_a,
            match_id,
            'pin',
            _pin_data(
                token,
                match_id,
                pinned=(index < 3),
                **{
                    'return': value,
                    'next': 'https://evil.example/',
                    'redirect': 'https://evil.example/',
                },
            ),
            native=True,
        )
        assert response.status_code == 303, value
        location = response.headers['Location']
        assert location == f'{list_path}#lt-row-{match_id}', value
        assert 'evil' not in location
        assert '\n' not in location and '\r' not in location
        # The flash writes the session cookie; nothing of the value is in a header.
        assert 'x=1' not in str(response.headers)
        assert 'evil' not in str(response.headers)

    # Without `return` there is the default list, still anchored.
    plain = _post(
        admin_client,
        world.party_a,
        world.red,
        'pin',
        _pin_data(token, world.red, pinned=False, **{'return': None}),
        native=True,
    )
    assert plain.headers['Location'] == f'{list_path}#lt-row-{world.red}'

    # A JSON answer holds data only: no URL to follow, the server time, and
    # the panel that was read after the commit.
    json_response = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow, comment='ok'),
    )
    body = json_response.get_json()
    assert json_response.status_code == 200
    assert set(body) == {'committed_at', 'fragment'}
    assert body['committed_at'] == '2026-10-08T12:00:00.000000Z'
    assert ISO_UTC.fullmatch(body['fragment']['as_of'])
    assert set(body['fragment']) == {'html', 'as_of', 'poll_seconds'}
    assert 'Location' not in json_response.headers
    assert json_response.mimetype == 'application/json'

    # Hidden fields are bounded: an invented revision, a giant comment and a
    # malformed episode are field errors, never a 500 or a write.
    facts = _facts(world, world.red)
    for overrides in (
        {'revision': '99999999999'},
        {'revision': '-1'},
        {'revision': '1e3'},
        {'episode': 'not-a-uuid'},
        {'episode': 'a' * 100_000},
        {'comment': 'x' * 501},
        {'comment': 'x' * 1_000_000},
        {'comment': 'bad\x00text'},
    ):
        response = _post(
            admin_client,
            world.party_a,
            world.red,
            'ack',
            _ack_data(token, world.red, **overrides),
        )
        assert (response.status_code, response.get_json()['error']) == (
            422,
            'invalid',
        ), overrides
    assert _facts(world, world.red) == facts

    # An oversized `return` is dropped, not an error: the action goes through.
    long_return = _post(
        admin_client,
        world.party_a,
        world.red,
        'pin',
        _pin_data(token, world.red, **{'return': 'view=all&' + 'a' * 2000}),
        native=True,
    )
    assert long_return.status_code == 303
    assert long_return.headers['Location'] == f'{list_path}#lt-row-{world.red}'


def test_admin_poll_answers_401_and_403_json_not_redirect(
    world,
    renders,
    services,
    anonymous_client,
    no_backend_client,
    view_only_client,
    view_only,
):
    poll_url = _url(world.party_a, '/poll')
    token = _csrf_token(view_only_client, view_only)

    # No session: the status and the code, never the login form's 302.
    for client in (anonymous_client, no_backend_client):
        poll = client.get(poll_url, headers=JSON)
        assert poll.status_code == 401
        assert poll.get_json()['error'] == 'session_expired'
        assert 'Location' not in poll.headers
        assert poll.mimetype == 'application/json'
        assert poll.headers['Cache-Control'] == 'private, no-store'

        # Also for a client that did not ask for JSON: a poll is JSON only.
        plain = client.get(poll_url)
        assert plain.status_code == 401
        assert plain.get_json()['error'] == 'session_expired'

        for kind in ('ack', 'pin'):
            answer = _post(
                client,
                world.party_a,
                world.yellow,
                kind,
                _data(kind, token, world.yellow),
            )
            assert answer.status_code == 401
            assert answer.get_json()['error'] == 'session_expired'
            assert 'Location' not in answer.headers

    # The session is there, the authority is not.
    lost = view_only_client.get(poll_url)
    assert lost.status_code == 403
    assert lost.get_json()['error'] == 'access_revoked'
    assert lost.headers['Cache-Control'] == 'private, no-store'

    # The native page keeps the ordinary login redirect, and a native POST
    # goes to the login form too, and back to the plain list afterwards.
    page = anonymous_client.get(_url(world.party_a))
    assert page.status_code == 302
    assert '/authentication/log_in' in page.headers['Location']
    assert _login_next(page) == f'{PATH}/{world.party_a}/dashboard'
    native = _post(
        anonymous_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow),
        native=True,
    )
    assert native.status_code == 302
    location = native.headers['Location']
    assert '/authentication/log_in' in location
    assert _login_next(native) == f'{PATH}/{world.party_a}/dashboard'
    assert services.ack.calls == []
    assert renders.calls == []
    assert _facts(world, world.yellow)['acks'] == []


def test_admin_native_error_rerenders_open_form_with_draft(
    world, renders, services, admin_client, admin, clock
):
    token = _csrf_token(admin_client, admin)

    # An over-long comment: 422, the form is open with the draft and the
    # counter, the field error is linked, nothing was saved.
    draft = 'x' * 501
    before = _facts(world, world.yellow)
    response = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow, comment=draft),
        native=True,
    )
    assert response.status_code == 422
    page = _page(response)
    assert page['template'] == 'admin/lan_tournament/dashboard.html'
    assert page['unavailable'] is None
    action = page['action']
    assert (action['kind'], action['error']) == ('ack', 'invalid')
    assert action['match_id'] == str(world.yellow)
    assert action['draft'] == draft
    assert action['draft_target'] is True
    assert action['field_error']['field'] == 'comment'
    assert action['message'] == action['field_error']['text']
    assert '501' in action['message'] and '500' in action['message']
    form = _row(page['dashboard'], world.yellow)['ack']['form']
    assert form['open'] is True
    assert form['draft'] == draft
    assert form['is_over'] is True
    assert form['counter_text'].startswith('501 / 500')
    assert form['error_text'] == action['field_error']['text']
    assert (form['episode'], form['revision']) == (
        str(_open_episode(world.yellow)['id']),
        0,
    )
    # The other rows are untouched by the open form.
    assert 'open' not in _row(page['dashboard'], world.red)['ack']['form']
    assert services.ack.calls == []
    assert _facts(world, world.yellow) == before

    # Another orga recorded the check first, and the alert came back: the
    # old form is stale (409), the row offers the check again, so the draft
    # goes back into the fresh form, and the other orga is named.
    first = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow, comment='first'),
    )
    assert first.status_code == 200
    stale_form = _ack_data(token, world.yellow, revision='0', comment='mine')
    clock.now = NOW + timedelta(minutes=16)
    response = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        stale_form,
        native=True,
    )
    assert response.status_code == 409
    page = _page(response)
    action = page['action']
    assert (action['error'], action['draft']) == ('stale', 'mine')
    assert action['draft_target'] is True
    assert action['message'] == (
        'Der Stand hat sich geändert. Bitte aktualisieren und erneut prüfen.'
    )
    assert admin.screen_name in action['detail']
    assert 'bereits festgehalten' in action['detail']
    form = _row(page['dashboard'], world.yellow)['ack']['form']
    assert (form['open'], form['draft']) == (True, 'mine')
    assert form['revision'] == 1
    assert form['error_text'] is None
    assert len(_facts(world, world.yellow)['acks']) == 1

    # A refusal of the state, not of the form: the row is listed but offers
    # no check, so the draft goes to the read-only card and says why.
    clock.now = NOW
    response = _post(
        admin_client,
        world.party_a,
        world.green,
        'ack',
        _ack_data(token, world.green, comment='too early'),
        native=True,
    )
    assert response.status_code == 409
    page = _page(response)
    action = page['action']
    assert (action['error'], action['draft']) == ('refused', 'too early')
    assert action['draft_target'] is False
    assert action['message'] == (
        'Nicht festgehalten: Die Verzögerung hat die Schwelle noch nicht'
        ' erreicht.'
    )
    assert action['detail'].startswith('Die Zeile zeigt')
    green = _row(page['dashboard'], world.green)
    assert green is not None and green['ack']['form'] is None
    assert _facts(world, world.green)['acks'] == []

    # A stale pin: 409, the pin form is the row's, no ack form is opened.
    response = _post(
        admin_client,
        world.party_a,
        world.red,
        'pin',
        _pin_data(token, world.red, revision='4'),
        native=True,
    )
    assert response.status_code == 409
    action = _page(response)['action']
    assert (action['kind'], action['error']) == ('pin', 'stale')
    assert action['draft'] is None and action['detail'] is None
    assert action['draft_target'] is True
    assert action['message'].startswith('Der Pin wurde inzwischen geändert')

    # A forged token keeps the draft visible and says what to do (R44).
    response = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, csrf_token='x' * 43, comment='keep me'),
        native=True,
    )
    assert response.status_code == 403
    action = _page(response)['action']
    assert (action['error'], action['draft']) == ('csrf_invalid', 'keep me')
    assert action['message'].startswith('Die Formularprüfung ist abgelaufen')


def test_admin_missing_and_hidden_match_responses_are_identical(
    world, renders, services, admin_client, admin
):
    token = _csrf_token(admin_client, admin)
    missing = uuid4()
    candidates = [missing, 'not-a-uuid', '12345', world.foreign]

    for kind in ('ack', 'pin'):
        for native in (False, True):
            responses = []
            for match_id in candidates:
                data = _data(kind, token, world.yellow, comment='draft')
                responses.append(
                    _post(
                        admin_client,
                        world.party_a,
                        match_id,
                        kind,
                        data,
                        native=native,
                    )
                )
            snapshots = {_snapshot(response) for response in responses}
            assert len(snapshots) == 1, (kind, native)
            assert responses[0].status_code == 404
            if native:
                page = _page(responses[0])
                assert page['unavailable'] is not None
                assert page['dashboard'] is None and page['action'] is None
                assert page['unavailable']['back_url'].startswith(
                    f'{PATH}/{world.party_a}/dashboard'
                )
            else:
                # Nothing about the list, the draft or the match.
                assert set(responses[0].get_json()) == {'error', 'message'}
                assert responses[0].get_json()['error'] == 'unavailable'

    # A hidden match is hidden from the forms too: a refused form on it
    # carries no more than one on a match that does not exist.
    for native in (False, True):
        bad = {'revision': 'x'}
        refused = [
            _snapshot(
                _post(
                    admin_client,
                    world.party_a,
                    match_id,
                    'ack',
                    _ack_data(token, world.yellow, **bad),
                    native=native,
                )
            )
            for match_id in (missing, world.foreign)
        ]
        assert refused[0] == refused[1]
    assert _facts(world, world.foreign)['acks'] == []


def test_admin_refusal_returns_full_panel_and_draft_target(
    world, renders, admin_client, admin, clock
):
    token = _csrf_token(admin_client, admin)

    # Refused for the state: the answer is the whole panel from one
    # snapshot, not the target row. Every due match of the scope is in it.
    response = _post(
        admin_client,
        world.party_a,
        world.green,
        'ack',
        _ack_data(token, world.green, comment='early'),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'refused')
    assert body['message'] == (
        'Die Verzögerung hat die Schwelle noch nicht erreicht.'
    )
    assert body['draft_target'] is False
    assert ISO_UTC.fullmatch(body['fragment']['as_of'])
    assert body['fragment']['as_of'] == '2026-10-08T12:00:00.000000Z'
    dashboard = _panel(body)
    assert _row_ids(dashboard) == {
        str(world.yellow),
        str(world.red),
        str(world.green),
    }
    assert _tile_counts(dashboard) == {'red': 1, 'yellow': 1, 'green': 1}
    assert dashboard['count']['total'] == 3
    assert {'tiles', 'filters', 'count', 'rows', 'pager', 'freshness'} <= set(
        dashboard
    )

    # The target left `view=due` meanwhile: it is confirmed. The answer is
    # still the whole panel, now without it and with moved counts, and the
    # client is told that its draft has no form to go back to.
    world.confirm(world.yellow)
    response = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow, comment='late'),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'refused')
    assert body['draft_target'] is False
    dashboard = _panel(body)
    assert _row_ids(dashboard) == {str(world.red), str(world.green)}
    assert _tile_counts(dashboard) == {'red': 1, 'yellow': 0, 'green': 1}
    assert dashboard['count']['total'] == 2
    assert _facts(world, world.yellow)['acks'] == []

    # Stale, and the row still offers the check: the draft has a form.
    first = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, comment='first'),
    )
    assert first.status_code == 200
    stale_form = _ack_data(token, world.red, revision='0')
    clock.now = NOW + timedelta(minutes=16)
    response = _post(admin_client, world.party_a, world.red, 'ack', stale_form)
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert body['draft_target'] is True
    assert body['fragment']['as_of'] == '2026-10-08T12:16:00.000000Z'
    row = _row(_panel(body), world.red)
    assert row['ack']['offered'] is True
    assert row['ack']['form']['revision'] == 1
    # The refused request wrote nothing: one acknowledgement, no stamp.
    assert len(_facts(world, world.red)['acks']) == 1

    # An invalid form carries the panel too, and the form's own message.
    response = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, revision='1', comment='y' * 501),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (422, 'invalid')
    assert '501' in body['message']
    assert body['draft_target'] is True
    assert _row(_panel(body), world.red) is not None

    # A pin refused as stale: same shape.
    response = _post(
        admin_client,
        world.party_a,
        world.green,
        'pin',
        _pin_data(token, world.green, revision='9'),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert body['draft_target'] is True
    assert _row(_panel(body), world.green)['pin']['offered'] is True


def test_admin_json_stale_ack_names_the_other_orga_like_the_page(
    world, renders, admin_client, admin, clock
):
    token = _csrf_token(admin_client, admin)
    first = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, comment='first'),
    )
    assert first.status_code == 200
    stale_form = _ack_data(token, world.red, revision='0', comment='mine')

    # Other codes carry no detail: the script words them itself.
    response = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, comment='again'),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'refused')
    assert 'detail' not in body
    response = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red, comment='y' * 501),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (422, 'invalid')
    assert 'detail' not in body
    response = _post(
        admin_client,
        world.party_a,
        world.green,
        'pin',
        _pin_data(token, world.green, revision='9'),
    )
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert 'detail' not in body
    response = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data('x' * 43, world.red),
    )
    assert response.get_json()['error'] == 'csrf_invalid'
    assert 'detail' not in response.get_json()

    # The alert came back and the form is old. The row is in the re-read, so
    # the script gets the sentence of the page, from the same record.
    clock.now = NOW + timedelta(minutes=16)
    response = _post(admin_client, world.party_a, world.red, 'ack', stale_form)
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert body['draft_target'] is True
    assert admin.screen_name in body['detail']
    assert 'bereits festgehalten' in body['detail']
    native = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        stale_form,
        native=True,
    )
    assert native.status_code == 409
    assert _page(native)['action']['detail'] == body['detail']

    # Not in the re-read (the list the form came from does not hold it):
    # nothing is named, and the draft has no form to go back to.
    hidden = {**stale_form, 'return': RETURN_GREEN}
    response = _post(admin_client, world.party_a, world.red, 'ack', hidden)
    body = response.get_json()
    assert (response.status_code, body['error']) == (409, 'stale')
    assert body['draft_target'] is False
    assert 'detail' not in body
    assert _row(_panel(body), world.red) is None

    # A match the viewer cannot reach says nothing at all.
    response = _post(
        admin_client,
        world.party_a,
        world.foreign,
        'ack',
        _ack_data(token, world.foreign),
    )
    assert response.status_code == 404
    assert 'detail' not in response.get_json()


def test_admin_error_codes_distinguish_csrf_from_revoked_access(
    world,
    renders,
    admin_client,
    admin,
    view_only_client,
    anonymous_client,
    make_user,
    make_client,
    admin_app,
):
    token = _csrf_token(admin_client, admin)
    world.confirm(world.red)

    scenarios = {
        'session_expired': (
            401,
            lambda: _post(
                anonymous_client,
                world.party_a,
                world.yellow,
                'ack',
                _ack_data(token, world.yellow),
            ),
        ),
        'access_revoked': (
            403,
            lambda: _post(
                view_only_client,
                world.party_a,
                world.yellow,
                'ack',
                _ack_data(token, world.yellow),
            ),
        ),
        'csrf_invalid': (
            403,
            lambda: _post(
                admin_client,
                world.party_a,
                world.yellow,
                'ack',
                _ack_data('x' * 43, world.yellow),
            ),
        ),
        'unavailable': (
            404,
            lambda: _post(
                admin_client,
                world.party_a,
                uuid4(),
                'ack',
                _ack_data(token, world.yellow),
            ),
        ),
        'stale': (
            409,
            lambda: _post(
                admin_client,
                world.party_a,
                world.yellow,
                'ack',
                _ack_data(token, world.yellow, revision='5'),
            ),
        ),
        'refused': (
            409,
            lambda: _post(
                admin_client,
                world.party_a,
                world.red,
                'ack',
                _ack_data(token, world.red),
            ),
        ),
        'invalid': (
            422,
            lambda: _post(
                admin_client,
                world.party_a,
                world.yellow,
                'ack',
                _ack_data(token, world.yellow, comment='z' * 501),
            ),
        ),
    }
    for code, (status, request_it) in scenarios.items():
        response = request_it()
        assert (response.status_code, response.get_json()['error']) == (
            status,
            code,
        )
        assert response.headers['Cache-Control'] == 'private, no-store'

    # Two 403s are never the same answer: losing the form check is not
    # losing access, so a client may keep its draft in one and drop it in
    # the other.
    csrf = scenarios['csrf_invalid'][1]().get_json()
    revoked = scenarios['access_revoked'][1]().get_json()
    assert csrf['error'] != revoked['error']
    assert csrf['message'].startswith('Die Formularprüfung ist abgelaufen')
    assert revoked['message'].startswith('Du hast keinen Zugriff')

    # An administrator whose role is taken away meanwhile reads as revoked
    # on the very next request, with a valid session and a valid token.
    temporary = make_user('F03RtTemporary')
    log_in_user(temporary.id)
    backend_role = RoleID(f'f03rt_backend_{uuid4().hex[:8]}')
    admin_role = RoleID(f'f03rt_admin_{uuid4().hex[:8]}')
    create_role_with_permissions_assigned(
        backend_role, [PermissionID('admin.access')]
    )
    create_role_with_permissions_assigned(
        admin_role, [PermissionID('lan_tournament.administrate')]
    )
    authz_service.assign_role_to_user(backend_role, temporary)
    authz_service.assign_role_to_user(admin_role, temporary)
    client = make_client(admin_app, user_id=temporary.id)
    temporary_token = _csrf_token(client, temporary)
    assert (
        client.get(_url(world.party_a, '/poll'), headers=JSON).status_code
        == 200
    )
    authz_service.deassign_role_from_user(admin_role, temporary)
    for kind in ('ack', 'pin'):
        response = _post(
            client,
            world.party_a,
            world.yellow,
            kind,
            _data(kind, temporary_token, world.yellow),
        )
        assert (response.status_code, response.get_json()['error']) == (
            403,
            'access_revoked',
        )
    poll = client.get(_url(world.party_a, '/poll'), headers=JSON)
    assert (poll.status_code, poll.get_json()['error']) == (
        403,
        'access_revoked',
    )
    assert _facts(world, world.yellow)['acks'] == []


# -- the rest of the contract --


def test_get_and_poll_are_private_no_store_and_agree(
    world, renders, admin_client, admin
):
    query = '?scope=all&view=all&sort=wait'
    page = admin_client.get(_url(world.party_a, query))
    poll = admin_client.get(_url(world.party_a, f'/poll{query}'), headers=JSON)
    for response in (page, poll):
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'private, no-store'

    template, context = renders.calls[0]
    assert template == 'admin/lan_tournament/dashboard.html'
    assert set(context) == {'dashboard', 'party', 'action', 'unavailable'}
    assert context['party'].id == world.party_a
    assert context['action'] is None and context['unavailable'] is None

    panel_template, panel_context = renders.calls[1]
    assert panel_template == 'common/lan_tournament/_dashboard_panel.html'
    assert set(panel_context) == {'dashboard', 'action'}
    assert panel_context['action'] is None
    body = poll.get_json()
    assert set(body) == {'html', 'as_of', 'poll_seconds'}
    assert body['poll_seconds'] == (
        get_effective_dashboard_settings(world.party_a).unwrap().poll_seconds
    )

    # One scope and one query give the same rows and the same URL to poll.
    dashboard, polled = context['dashboard'], panel_context['dashboard']
    assert (
        _row_ids(dashboard)
        == _row_ids(polled)
        == {
            str(world.yellow),
            str(world.red),
            str(world.green),
        }
    )
    assert dashboard['poll']['url'] == polled['poll']['url']
    assert dashboard['poll']['url'] == (
        f'{PATH}/{world.party_a}/dashboard/poll?scope=all&view=all&sort=wait'
    )

    # Every JSON answer is private, also a successful pin and a refusal.
    token = _csrf_token(admin_client, admin)
    for response in (
        _post(
            admin_client,
            world.party_a,
            world.yellow,
            'pin',
            _pin_data(token, world.yellow),
        ),
        _post(
            admin_client,
            world.party_a,
            world.yellow,
            'pin',
            _pin_data(token, world.yellow, revision='6'),
        ),
        _post(
            admin_client,
            world.party_a,
            world.yellow,
            'pin',
            _pin_data(token, world.yellow, csrf_token=None),
        ),
    ):
        assert response.headers['Cache-Control'] == 'private, no-store'
    native = _post(
        admin_client,
        world.party_a,
        world.red,
        'pin',
        _pin_data(token, world.red),
        native=True,
    )
    assert native.status_code == 303
    assert native.headers['Cache-Control'] == 'private, no-store'


def test_get_and_poll_write_nothing_and_lock_nothing(
    world, renders, admin_client
):
    statements: list[str] = []

    def record(connection, cursor, statement, *_):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        page = admin_client.get(_url(world.party_a, '?scope=all'))
        poll = admin_client.get(_url(world.party_a, '/poll?scope=all'))
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)

    assert (page.status_code, poll.status_code) == (200, 200)
    assert statements
    writes = [
        statement
        for statement in statements
        if re.match(r'\s*(INSERT|UPDATE|DELETE)\b', statement, re.IGNORECASE)
        or re.search(
            r'\bFOR\s+(NO\s+KEY\s+)?UPDATE\b', statement, re.IGNORECASE
        )
    ]
    assert writes == []


def test_list_wires_forms_to_these_routes_and_the_token_works(
    world, renders, admin_client, admin
):
    response = admin_client.get(_url(world.party_a, '?scope=all'))
    assert response.status_code == 200
    dashboard = renders.calls[0][1]['dashboard']

    # What the page renders is what the routes accept: the token, the URLs
    # and the revision values come from the context, not from the test.
    token = dashboard['csrf_token']
    assert len(token) == 43
    row = _row(dashboard, world.yellow)
    ack = row['ack']['form']
    pin = row['pin']
    assert ack['action_url'] == _path_of(
        _action_url(world.party_a, world.yellow, 'ack')
    )
    assert pin['action_url'] == _path_of(
        _action_url(world.party_a, world.yellow, 'pin')
    )
    assert ack['csrf_token'] == pin['csrf_token'] == token
    assert (
        ack['return_value'] == pin['return_value'] == dashboard['return_value']
    )

    accepted = admin_client.post(
        ack['action_url'],
        data={
            'csrf_token': ack['csrf_token'],
            'episode': ack['episode'],
            'revision': str(ack['revision']),
            'return': ack['return_value'],
            'comment': 'from the rendered form',
        },
        headers=JSON,
    )
    assert accepted.status_code == 200
    (stored,) = _facts(world, world.yellow)['acks']
    assert stored['comment'] == 'from the rendered form'
    assert stored['actor_id'] == admin.id

    pinned = admin_client.post(
        pin['action_url'],
        data={
            'csrf_token': pin['csrf_token'],
            'revision': str(pin['revision']),
            'return': pin['return_value'],
            'pinned': pin['target'],
        },
    )
    assert pinned.status_code == 303
    (state,) = _facts(world, world.yellow)['pins']
    assert (state['revision'], state['pinned_by']) == (1, admin.id)


def test_pin_and_ack_commit_once_and_report_the_server_state(
    world, renders, admin_client, admin
):
    token = _csrf_token(admin_client, admin)
    before = _facts(world, world.yellow)

    ack = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'ack',
        _ack_data(token, world.yellow, comment='Called both captains'),
    )
    assert ack.status_code == 200
    after = _facts(world, world.yellow)
    (stored,) = after['acks']
    assert (stored['revision'], stored['actor_id'], stored['comment']) == (
        1,
        admin.id,
        'Called both captains',
    )
    assert stored['occurred_at'] == NOW
    # A check changes nothing of the match itself.
    assert after['match'] == before['match']
    assert after['match'][0]['last_changed_at'] == LAST_CHANGED
    assert after['audit'] == [
        (
            'match-acknowledged',
            admin.id,
            {
                'match_id': str(world.yellow),
                'episode_id': str(_open_episode(world.yellow)['id']),
                'revision': 1,
            },
        )
    ]
    # The panel of the answer was read after the commit: the alert
    # interval started over, the total wait did not.
    row = _row(_panel(ack.get_json()), world.yellow)
    assert row['tier']['urgency'] == 'green'
    assert row['ack']['record']['actor'] == admin.screen_name
    assert row['ack']['record']['comment'] == 'Called both captains'

    pin = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'pin',
        _pin_data(token, world.yellow),
    )
    assert pin.status_code == 200
    assert pin.get_json()['committed_at'] == '2026-10-08T12:00:00.000000Z'
    assert (
        _row(_panel(pin.get_json()), world.yellow)['pin']['is_pinned'] is True
    )
    unpin = _post(
        admin_client,
        world.party_a,
        world.yellow,
        'pin',
        _pin_data(token, world.yellow, pinned=False, revision='1'),
    )
    assert unpin.status_code == 200
    assert (
        _row(_panel(unpin.get_json()), world.yellow)['pin']['is_pinned']
        is False
    )
    (state,) = _facts(world, world.yellow)['pins']
    assert (state['revision'], state['pinned_at']) == (2, None)

    # Unpinning what nobody pinned is not an error and writes nothing.
    noop = _post(
        admin_client,
        world.party_a,
        world.red,
        'pin',
        _pin_data(token, world.red, pinned=False),
    )
    assert noop.status_code == 200
    assert noop.get_json()['committed_at'] == '2026-10-08T12:00:00.000000Z'
    assert _facts(world, world.red)['pins'] == []

    # Native success flashes the German confirmation and redirects.
    native = _post(
        admin_client,
        world.party_a,
        world.red,
        'ack',
        _ack_data(token, world.red),
        native=True,
    )
    assert native.status_code == 303
    with admin_client.session_transaction() as session:
        flashes = session.get('_flashes', [])
    texts = [json.dumps(flash, ensure_ascii=False) for flash in flashes]
    assert any('Prüfung festgehalten.' in text for text in texts)
    assert any(
        'Stand 14:00:00. Alarmintervall läuft neu.' in text for text in texts
    )


def test_query_parameters_never_widen_the_scope_and_never_break_the_page(
    world, renders, admin_client, admin
):
    # An administrator without an assignment sees nothing by default and
    # everything of the party on request.
    default = admin_client.get(_url(world.party_a))
    assert default.status_code == 200
    dashboard = renders.calls[-1][1]['dashboard']
    assert dashboard['rows'] == []
    assert dashboard['empty']['kind'] == 'no_assignment'

    everything = admin_client.get(_url(world.party_a, '?scope=all'))
    assert everything.status_code == 200
    assert _row_ids(renders.calls[-1][1]['dashboard']) == {
        str(world.yellow),
        str(world.red),
        str(world.green),
    }

    # An invalid value is dropped with a notice and never widens anything.
    junk = (
        '?scope=bogus&view=nonsense&state=%00&sort=x&page=-3'
        f'&tournament={world.tournament_b}'
    )
    for suffix in ('', '/poll'):
        response = admin_client.get(_url(world.party_a, f'{suffix}{junk}'))
        assert response.status_code == 200
        calls = renders.calls[-1][1]
        dashboard = calls['dashboard']
        assert dashboard['rows'] == []
        assert dashboard['banner']['kind'] == 'warn'
        assert len(dashboard['banner']['details']) == 6
        assert dashboard['query']['scope'] == 'assigned'

    # A page far beyond the last is not clamped, and a huge one is refused
    # as a value, not as a request.
    far = admin_client.get(_url(world.party_a, '?scope=all&page=99999999999'))
    assert far.status_code == 200
    assert renders.calls[-1][1]['dashboard']['banner']['kind'] == 'warn'
    beyond = admin_client.get(_url(world.party_a, '?scope=all&page=9'))
    assert beyond.status_code == 200
    assert renders.calls[-1][1]['dashboard']['beyond_last_page'] is True

    # Another party's ID in the URL is an empty list, not that party's list.
    stranger = admin_client.get(
        _url(world.party_b, f'?scope=all&tournament={world.tournament_a}')
    )
    assert stranger.status_code == 200
    assert _row_ids(renders.calls[-1][1]['dashboard']) == {str(world.foreign)}
    missing = admin_client.get(_url(f'nope-{uuid4().hex[:8]}'))
    assert missing.status_code == 404


def test_the_routes_are_registered_exactly_once(admin_app):
    endpoints = {
        'lan_tournament_admin.dashboard_for_party',
        'lan_tournament_admin.dashboard_poll_for_party',
        'lan_tournament_admin.dashboard_pin',
        'lan_tournament_admin.dashboard_ack',
    }
    rules = {
        (rule.rule, tuple(sorted(rule.methods - {'HEAD', 'OPTIONS'})))
        for rule in admin_app.url_map.iter_rules()
        if rule.endpoint in endpoints
    }

    prefix = '/lan-tournaments/for_party/<party_id>/dashboard'
    assert rules == {
        (prefix, ('GET',)),
        (f'{prefix}/poll', ('GET',)),
        (f'{prefix}/matches/<match_id>/pin', ('POST',)),
        (f'{prefix}/matches/<match_id>/ack', ('POST',)),
    }


def test_the_route_strings_have_german():
    po_path = (
        Path(__file__).parents[4]
        / 'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        catalog = read_po(f, locale='de')

    def german(msgid: str) -> bool:
        message = catalog.get(msgid)
        return (
            message is not None
            and bool(message.string)
            and not message.fuzzy
            and message.string != msgid
        )

    # Every literal the routes pass to `gettext`, from the marker on.
    source = inspect.getsource(admin_views)
    first_line = source.split('# orga dashboard')[0].count('\n')
    msgids = {
        message if isinstance(message, str) else message[0]
        for lineno, message, _, _ in extract(
            'python', BytesIO(source.encode('utf-8'))
        )
        if lineno > first_line
    }
    assert msgids
    assert [msgid for msgid in msgids if not german(msgid)] == []

    # Every error code a route can answer is a msgid of its own, and the
    # form-check notice has its German too.
    codes = sorted(set(TRANSPORT_ERRORS) - {'csrf_invalid'})
    assert [code for code in codes if not german(code)] == []
    assert german(
        'The form check has expired. Please reload the page;'
        ' your draft stays visible.'
    )
