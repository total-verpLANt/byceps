from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
import difflib
import html
from io import BytesIO
from itertools import count
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Self
from urllib.parse import quote, unquote, urlencode
from uuid import uuid4

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
from flask.testing import FlaskClient
import flask_babel
import pytest
from sqlalchemy import (
    BigInteger,
    cast as sql_cast,
    column,
    event,
    func,
    select,
    table,
    Text,
    text,
)
from sqlalchemy.engine import Engine

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_dashboard_coordination_service as coordination,
    tournament_dashboard_service as dashboard_service,
    tournament_dashboard_settings_service as settings_service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
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
    DashboardPage,
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
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID
from byceps.services.user.models import User
from byceps.util.uuid import uuid7

from tests.helpers import create_site, log_in_user


ADMIN_ORIGIN = 'http://admin.acmecon.test'
ADMIN_BASE = f'{ADMIN_ORIGIN}/lan-tournaments/for_party'
SITE_BASE = '/lan-tournaments/orga-dashboard'
BOTE_SITE_ID = SiteID('totalverplant-36')
JSON = {'Accept': 'application/json'}

NOW = datetime(2026, 10, 8, 12, 0, 0)
NOW_ISO = '2026-10-08T12:00:00.000000Z'
LATER_ISO = '2026-10-08T12:20:00.000000Z'
MINUTE_US = 60_000_000
CLOCK_START = NOW - timedelta(hours=3)
CLOCK_AT_NOW_US = 180 * MINUTE_US
# A date that no stamp of the test day could hide behind.
LAST_CHANGED = datetime(2026, 1, 15, 8, 0, 0)

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
SINGLE = EliminationMode.SINGLE_ELIMINATION
ROUND_ROBIN = EliminationMode.ROUND_ROBIN

SELECT_ONLY = re.compile(r'^\s*(SELECT|WITH)\b', re.IGNORECASE)
WRITES = re.compile(
    r'\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|COPY|LOCK|NEXTVAL|SETVAL)\b'
    r'|\bFOR\s+(NO\s+KEY\s+)?(UPDATE|SHARE)\b|\bFOR\s+KEY\s+SHARE\b'
    r'|\bpg_advisory\w*',
    re.IGNORECASE,
)
PANEL_END = 'data-live></div>\n</section>'
FINGERPRINT_HEADERS = {
    'etag',
    'last-modified',
    'content-md5',
    'digest',
    'server-timing',
}


# -- fixtures: catalogue, clock, apps --


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


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Make `clock.now` the server time of every transaction."""
    state = SimpleNamespace(now=NOW)
    monkeypatch.setattr(repo, 'get_operation_time', lambda: state.now)
    return state


@pytest.fixture(autouse=True)
def _leave_no_transaction(admin_app):
    yield
    db.session.rollback()


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
def bote_site(party) -> Iterator[Site]:
    """Bound to the real `totalverplant-36` override directory.

    The id exists once per database, so a module that found the site
    of another one binds it to its own party and gives it back.
    """
    previous = site_service.find_site(BOTE_SITE_ID)
    if previous is None:
        site = create_site(
            BOTE_SITE_ID,
            party.brand_id,
            server_name='lt-test-dashboard-parity-bote.test',
            party_id=party.id,
        )
    else:
        site = _bound_to(previous, party.id)

    yield site

    if previous is None:
        site_service.delete_site(BOTE_SITE_ID)
    else:
        _bound_to(previous, previous.party_id)


@pytest.fixture(scope='module')
def bote_app(database, make_site_app, bote_site):
    app = make_site_app(bote_site.server_name, bote_site.id)
    with app.app_context():
        return app


@pytest.fixture(scope='module')
def other_party(make_party, brand):
    party_id = PartyID(f'f03par-{uuid4().hex[:12]}')
    return make_party(brand, party_id, f'F03 parity {party_id}')


# -- the world: rows built by hand, so every fact is known --


class Scene:
    """Builds committed tournaments, with chosen people and times.

    A team without members demands nobody, so it stands in for the
    opponent when only one person is to be demanded.
    """

    def __init__(self, party_id: PartyID, confirmer) -> None:
        self.party_id = party_id
        self.confirmer = confirmer
        self._ghosts: dict[TournamentID, TournamentTeamID] = {}
        self._joined: dict[tuple, TournamentParticipantID] = {}
        self._orders = count(1)

    def tournament(
        self,
        name: str,
        *,
        status: TournamentStatus = ONGOING,
        mode: EliminationMode = SINGLE,
        clock: str = 'running',
    ) -> TournamentID:
        """`clock` is `running`, `frozen` (paused) or `unknown` (legacy)."""
        tournament_id = TournamentID(uuid7())
        elapsed, running_since, activated = {
            'running': (0, CLOCK_START, CLOCK_START),
            'frozen': (CLOCK_AT_NOW_US, None, CLOCK_START),
            'unknown': (0, None, None),
        }[clock]
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=self.party_id,
                name=name,
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
                tournament_status=status,
                game_format=GameFormat.ONE_V_ONE,
                elimination_mode=mode,
                group_size_max=None,
                operational_clock_elapsed_us=elapsed,
                operational_clock_running_since=running_since,
                operational_clock_activated_at=activated,
            )
        )
        db.session.commit()
        return tournament_id

    def assign(self, tournament_id: TournamentID, *users) -> None:
        for user in users:
            db.session.add(
                DbTournamentOrga(
                    TournamentOrgaID(uuid7()), tournament_id, user.id, NOW
                )
            )
        db.session.commit()

    def join(
        self,
        tournament_id: TournamentID,
        user,
        *,
        team_id: TournamentTeamID | None = None,
    ) -> TournamentParticipantID:
        key = (tournament_id, user.id)
        if key not in self._joined:
            participant = DbTournamentParticipant(
                TournamentParticipantID(uuid7()),
                user.id,
                tournament_id,
                NOW,
                team_id=team_id,
            )
            db.session.add(participant)
            db.session.commit()
            self._joined[key] = participant.id
        return self._joined[key]

    def team(
        self, tournament_id: TournamentID, name: str, captain, *members
    ) -> TournamentTeamID:
        team = DbTournamentTeam(
            TournamentTeamID(uuid7()), tournament_id, name, captain.id, NOW
        )
        db.session.add(team)
        db.session.commit()
        for user in (captain, *members):
            self.join(tournament_id, user, team_id=team.id)
        return team.id

    def ghost(self, tournament_id: TournamentID) -> TournamentTeamID:
        if tournament_id not in self._ghosts:
            team = DbTournamentTeam(
                TournamentTeamID(uuid7()),
                tournament_id,
                f'Ghost {uuid4().hex[:8]}',
                self.confirmer.id,
                NOW,
            )
            db.session.add(team)
            db.session.commit()
            self._ghosts[tournament_id] = team.id
        return self._ghosts[tournament_id]

    def match(
        self,
        tournament_id: TournamentID,
        *,
        participants=(),
        teams=(),
        round: int = 1,
        confirmed: bool = False,
        wait: int | None = 20,
        occupied: int | None = 30,
        created: int = 60,
        last_changed: datetime | None = LAST_CHANGED,
    ) -> TournamentMatchID:
        """Create a match; with `wait` (minutes) it has an open episode."""
        match_id = TournamentMatchID(uuid7())
        db.session.add(
            DbTournamentMatch(
                match_id,
                tournament_id,
                NOW - timedelta(minutes=created),
                match_order=next(self._orders),
                round=round,
                confirmed_by=self.confirmer.id if confirmed else None,
                phase=1,
                occupied_since=(
                    NOW - timedelta(minutes=occupied)
                    if occupied is not None
                    else None
                ),
                last_changed_at=last_changed,
            )
        )
        db.session.flush()
        entries = [{'participant_id': p} for p in participants]
        entries += [{'team_id': t} for t in teams]
        for kwargs in entries:
            db.session.add(
                DbTournamentMatchToContestant(
                    TournamentMatchToContestantID(uuid7()),
                    match_id,
                    NOW - timedelta(minutes=created),
                    **kwargs,
                )
            )
        db.session.flush()
        if wait is not None and not confirmed:
            repo.open_due_episode_flush(
                MatchDueEpisode(
                    id=MatchDueEpisodeID(uuid7()),
                    tournament_id=tournament_id,
                    match_id=match_id,
                    pairing_key='key',
                    opened_at=NOW - timedelta(minutes=wait),
                    opened_clock_us=CLOCK_AT_NOW_US - wait * MINUTE_US,
                )
            )
        db.session.commit()
        return match_id

    def duel(self, tournament_id, a, b, **fields) -> TournamentMatchID:
        """Create a match of two people."""
        return self.match(
            tournament_id,
            participants=[
                self.join(tournament_id, a),
                self.join(tournament_id, b),
            ],
            **fields,
        )

    def solo(self, tournament_id, user, **fields) -> TournamentMatchID:
        """Create a match of one person against a team without members."""
        return self.match(
            tournament_id,
            participants=[self.join(tournament_id, user)],
            teams=[self.ghost(tournament_id)],
            **fields,
        )

    def for_team(self, tournament_id, team_id, **fields) -> TournamentMatchID:
        """Create a match of one team against a team without members."""
        return self.match(
            tournament_id,
            teams=[team_id, self.ghost(tournament_id)],
            **fields,
        )


@dataclass
class Cast:
    """The people of one world. Every name is unique to it."""

    suffix: str
    players: list[User]
    hidden: list[User]
    orga_a: User
    orga_b: User
    orga_c: User
    power: User
    outsider: User
    confirmer: User

    def everyone(self) -> list[User]:
        return [
            *self.players,
            *self.hidden,
            self.orga_a,
            self.orga_b,
            self.orga_c,
            self.power,
            self.outsider,
            self.confirmer,
        ]


def build_cast(make_user, make_admin) -> Cast:
    suffix = uuid4().hex[:8]
    power = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'},
        screen_name=f'f03parpower{suffix}',
    )
    people = Cast(
        suffix=suffix,
        players=[make_user(f'f03parp{i}x{suffix}') for i in range(8)],
        hidden=[make_user(f'f03parhid{i}x{suffix}') for i in range(4)],
        orga_a=make_user(f'f03parorga{suffix}'),
        orga_b=make_user(f'f03parorgb{suffix}'),
        orga_c=make_user(f'f03parorgc{suffix}'),
        power=power,
        outsider=make_user(f'f03paroutsider{suffix}'),
        confirmer=make_user(f'f03parconf{suffix}'),
    )
    for user in people.everyone():
        log_in_user(user.id)
    return people


@pytest.fixture
def cast(make_user, make_admin) -> Cast:
    return build_cast(make_user, make_admin)


@dataclass
class World:
    """Four tournaments the orga may see, and hidden ones around them.

    `orga_a` and `power` are assigned to the same four tournaments of the
    site's party, `orga_b` to the first only. `orga_c` is assigned to a
    hidden tournament of the same party, and `orga_a` also to one of
    another party. p0 is needed twice in the visible tournaments (a real
    conflict), p2 and p3 are needed in hidden matches too (the external
    hint), p4 is needed in another party (no conflict at all).
    """

    cast: Cast
    scene: Scene
    party_id: PartyID
    names: dict[str, str]
    t1: TournamentID
    t2: TournamentID
    t_paused: TournamentID
    t_legacy: TournamentID
    t_hidden: TournamentID
    t_far: TournamentID
    red: TournamentMatchID
    yellow: TournamentMatchID
    green: TournamentMatchID
    done: TournamentMatchID
    bee: TournamentMatchID
    later: TournamentMatchID
    frozen: TournamentMatchID
    legacy: TournamentMatchID
    hy: TournamentMatchID
    hz: TournamentMatchID
    ht: TournamentMatchID
    hd: TournamentMatchID
    far: TournamentMatchID

    @property
    def visible_matches(self) -> set[str]:
        return {
            str(m)
            for m in (
                self.red,
                self.yellow,
                self.green,
                self.done,
                self.bee,
                self.later,
                self.frozen,
                self.legacy,
            )
        }

    @property
    def visible_tournaments(self) -> set[str]:
        return {
            str(t) for t in (self.t1, self.t2, self.t_paused, self.t_legacy)
        }


def build_world(party, other_party, cast) -> World:
    sx = cast.suffix
    p, h = cast.players, cast.hidden
    names = {
        't1': f'Sichtbar Eins {sx}',
        't2': f'Sichtbar Zwei {sx}',
        't_paused': f'Sichtbar Pause {sx}',
        't_legacy': f'Sichtbar Alt {sx}',
        't_hidden': f'Geheimturnier Zeta {sx}',
        't_far': f'Fernturnier Omega {sx}',
        'team': f'Geheimteam Kappa {sx}',
    }
    scene = Scene(party.id, cast.confirmer)
    far_scene = Scene(other_party.id, cast.confirmer)

    t1 = scene.tournament(names['t1'])
    t2 = scene.tournament(names['t2'], mode=ROUND_ROBIN)
    t_paused = scene.tournament(
        names['t_paused'], status=PAUSED, clock='frozen'
    )
    t_legacy = scene.tournament(names['t_legacy'], clock='unknown')
    t_hidden = scene.tournament(names['t_hidden'])
    t_far = far_scene.tournament(names['t_far'])

    red = scene.duel(t1, p[0], p[1], wait=50)
    yellow = scene.duel(t1, p[2], p[3], wait=20)
    green = scene.duel(t1, p[4], p[5], wait=5)
    done = scene.duel(t1, p[0], p[4], confirmed=True)
    bee = scene.duel(t2, p[0], p[6], wait=25)
    later = scene.duel(t2, p[2], p[7], round=2, wait=None)
    frozen = scene.duel(t_paused, p[6], p[7], wait=10)
    legacy = scene.duel(t_legacy, p[5], p[7], wait=None)

    # Hidden: p2 and p3 are needed here too (the external hint), the
    # others only meet each other. Times differ from every visible one.
    hy = scene.duel(
        t_hidden,
        p[2],
        h[0],
        wait=33,
        occupied=41,
        created=120,
        last_changed=datetime(2026, 2, 2, 2, 22, 22),
    )
    hz = scene.duel(t_hidden, h[1], h[2], wait=77, occupied=41)
    team = scene.team(t_hidden, names['team'], p[3], h[3])
    ht = scene.for_team(t_hidden, team, wait=17)
    hd = scene.duel(t_hidden, h[0], h[1], confirmed=True)
    far = far_scene.duel(t_far, p[4], h[3], wait=29)

    scene.assign(t1, cast.orga_a, cast.orga_b, cast.power)
    scene.assign(t2, cast.orga_a, cast.power)
    scene.assign(t_paused, cast.orga_a, cast.power)
    scene.assign(t_legacy, cast.orga_a, cast.power)
    scene.assign(t_hidden, cast.orga_c)
    far_scene.assign(t_far, cast.orga_a)

    return World(
        cast=cast,
        scene=scene,
        party_id=party.id,
        names=names,
        t1=t1,
        t2=t2,
        t_paused=t_paused,
        t_legacy=t_legacy,
        t_hidden=t_hidden,
        t_far=t_far,
        red=red,
        yellow=yellow,
        green=green,
        done=done,
        bee=bee,
        later=later,
        frozen=frozen,
        legacy=legacy,
        hy=hy,
        hz=hz,
        ht=ht,
        hd=hd,
        far=far,
    )


@pytest.fixture(scope='module')
def shared_world(party, other_party, make_user, make_admin) -> World:
    """One world for the tests that only read, built once."""
    return build_world(party, other_party, build_cast(make_user, make_admin))


@pytest.fixture
def world(party, other_party, cast) -> World:
    """A world of its own, for the tests that change it."""
    return build_world(party, other_party, cast)


# -- the surfaces: real routes, real templates, real sessions --


@dataclass
class Surface:
    """One app, as one logged-in person reaches it."""

    name: str
    client: FlaskClient
    party_id: PartyID
    user: User | None

    def url(self, tail: str = '', query: str = '') -> str:
        base = (
            f'{ADMIN_BASE}/{self.party_id}/dashboard'
            if self.name == 'admin'
            else SITE_BASE
        )
        return f'{base}{tail}' + (f'?{query}' if query else '')

    def page(self, query: str = ''):
        return self.client.get(self.url('', query))

    def poll(self, query: str = ''):
        return self.client.get(self.url('/poll', query))

    def absolute(self, path: str) -> str:
        return f'{ADMIN_ORIGIN}{path}' if self.name == 'admin' else path

    def post(self, path: str, data: dict, *, native: bool = False):
        return self.client.post(
            self.absolute(path), data=data, headers=None if native else JSON
        )


@pytest.fixture
def surfaces(party, admin_app, site_app, bote_app, make_client):
    """Provide a factory of `Surface`s: `surface('bote', user)`."""

    def build(name: str, user, party_id: PartyID | None = None) -> Surface:
        """Reach an app as that user, or as nobody with `user=None`."""
        app = {'admin': admin_app, 'site': site_app, 'bote': bote_app}[name]
        return Surface(
            name=name,
            client=make_client(app, user_id=user.id if user else None),
            party_id=party_id or party.id,
            user=user,
        )

    return build


# -- reading what the apps answered --


def name_of(user: User) -> str:
    assert user.screen_name is not None
    return user.screen_name


def panel_of(page_html: str) -> str:
    """Return the panel as written: its root to its live region's close."""
    assert page_html.count('<section class="lt-dashboard') == 1
    assert page_html.count(PANEL_END) == 1
    start = page_html.index('<section class="lt-dashboard')
    return page_html[start : page_html.index(PANEL_END) + len(PANEL_END)]


def row_ids(panel: str) -> list[str]:
    return re.findall(
        r'<li class="ltd-row" id="lt-row-([0-9a-f-]{36})">', panel
    )


def rows_of(panel: str) -> dict[str, str]:
    """Return the markup of each row by match ID, in page order."""
    found = re.findall(
        r'(<li class="ltd-row" id="lt-row-([0-9a-f-]{36})">.*?</article>\n</li>)',
        panel,
        flags=re.DOTALL,
    )
    rows = {match_id: markup for markup, match_id in found}
    assert list(rows) == row_ids(panel)
    return rows


def tiles_of(panel: str) -> dict[str, int]:
    found = re.findall(
        r'<li class="ltd-st t-([ryg])[^"]*">.*?<b class="ltd-stc">(\d+)</b>',
        panel,
        flags=re.DOTALL,
    )
    return {letter: int(number) for letter, number in found}


def normalise(markup: str) -> str:
    """Blank what differs by surface and by session; keep all the rest.

    The routes, the form tokens, the poll URL and the admin's scope
    control are the surface's own. Everything an orga reads stays.
    """
    markup = re.sub(r'\b(href|action)="[^"]*"', r'\1=""', markup)
    markup = re.sub(
        r'(<input type="hidden" name="(?:csrf_token|return)" value=")[^"]*',
        r'\1',
        markup,
    )
    markup = re.sub(
        r'\b(data-poll-url|data-login-url)="[^"]*"', r'\1=""', markup
    )
    markup = re.sub(
        r'<input type="hidden" name="scope" value="[^"]*">\n', '', markup
    )
    markup = re.sub(
        r'<(form|p) class="ltd-scope.*?</(form|p)>\n',
        '',
        markup,
        flags=re.DOTALL,
    )
    return markup.replace(
        'data-surface="admin"', 'data-surface="site"'
    ).replace('class="lt-dashboard is-site"', 'class="lt-dashboard"')


def capture(response) -> tuple:
    """Everything of a response a client could tell apart, but its clock.

    The session cookie is the one header that depends on the request's
    place in a session: the first answer sets it, later ones do not. Its
    content is scanned on its own (`assert_session_clean`).
    """
    return (
        response.status_code,
        tuple(
            sorted(
                (name, value)
                for name, value in response.headers.items()
                if name.lower() not in ('date', 'set-cookie')
            )
        ),
        response.get_data(),
    )


def assert_session_clean(
    label: str, surface: 'Surface', secrets: dict[str, str]
) -> None:
    """Scan what the server keeps in the client's signed session cookie."""
    with surface.client.session_transaction() as session:
        content = json.dumps(dict(session), default=str)
    for what, value in secrets.items():
        for spelling in variants(value):
            assert spelling not in content, f'{label}: {what} in the session'


def assert_same(label: str, expected: tuple, actual: tuple) -> None:
    """Fail with the first lines that differ, not with two blobs of HTML."""
    if actual == expected:
        return

    difference = [
        line
        for line in difflib.unified_diff(
            expected[2].decode().splitlines(),
            actual[2].decode().splitlines(),
            lineterm='',
            n=0,
        )
        if not line.startswith(('---', '+++', '@@'))
    ]
    pytest.fail(
        f'{label}: status {expected[0]} -> {actual[0]};'
        f' headers {sorted(set(expected[1]) ^ set(actual[1]))};'
        f' {len(expected[2])} -> {len(actual[2])} bytes;'
        f' body lines that differ:\n' + '\n'.join(difference[:12])
    )


def variants(value: str) -> set[str]:
    """Spell a value as the markup, a URL and JSON would."""
    return {
        value,
        html.escape(value),
        html.escape(value, quote=False),
        quote(value, safe=''),
        json.dumps(value)[1:-1],
        value.replace('-', ''),
    }


def assert_clean(label: str, captured: tuple, secrets: dict[str, str]) -> None:
    """Scan bytes, headers and decoded JSON for what must not be there."""
    status, headers, body = captured
    # A validator or fingerprint of the body would be a hidden-state oracle.
    assert not {name.lower() for name, _ in headers} & FINGERPRINT_HEADERS, (
        label
    )
    text_body = body.decode('utf-8')
    haystacks = {
        'body': text_body,
        'unescaped body': html.unescape(text_body),
        'decoded body': unquote(html.unescape(text_body)),
        'headers': '\n'.join(f'{name}: {value}' for name, value in headers),
    }
    try:
        parsed = json.loads(text_body)
    except ValueError:
        parsed = None
    if parsed is not None:
        strings: list[str] = []

        def collect(node) -> None:
            if isinstance(node, str):
                strings.append(node)
            elif isinstance(node, dict):
                for key, value in node.items():
                    strings.append(str(key))
                    collect(value)
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        collect(parsed)
        joined = '\n'.join(strings)
        haystacks['json strings'] = joined
        haystacks['json strings, unescaped'] = html.unescape(joined)

    for what, value in secrets.items():
        for spelling in variants(value):
            for where, haystack in haystacks.items():
                assert spelling not in haystack, (
                    f'{label}: {what} ({spelling!r}) found in the {where}'
                )


# -- watching the engine --


class Recorder:
    """Record what the real engine executes and how it ends a transaction."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self._listeners: dict[str, Callable] = {
            'before_cursor_execute': self._execute,
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

    def _execute(
        self, connection, cursor, statement, parameters, context, many
    ) -> None:
        self.statements.append(statement)

    def _commit(self, connection) -> None:
        self.commits += 1

    def _rollback(self, connection) -> None:
        self.rollbacks += 1


def fingerprint() -> dict[str, tuple[int, int]]:
    """Return rows and newest writer of every tournament table.

    An update that sets the values it found still gives a row a new
    `xmin`, so this tells a write from a read even if no value changed.
    """
    with db.engine.connect() as connection:
        tables = (
            connection.execute(
                text(
                    'SELECT tablename FROM pg_tables WHERE schemaname ='
                    " 'public' AND tablename LIKE 'lan\\_tournament%'"
                    ' ORDER BY tablename'
                )
            )
            .scalars()
            .all()
        )
        result = {}
        newest_writer = func.coalesce(
            func.max(sql_cast(sql_cast(column('xmin'), Text), BigInteger)), 0
        )
        for name in tables:
            rows, newest = connection.execute(
                select(func.count(), newest_writer).select_from(table(name))
            ).one()
            result[name] = (rows, newest)
        connection.rollback()
    return result


# -- the canonical read, to compare the pipelines with --


def _viewer(user, *permissions: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(permissions))


def canonical(party_id: PartyID, user, **values) -> DashboardPage:
    """Read the page through the service alone, at the frozen time."""
    settings = settings_service.get_effective_dashboard_settings(
        party_id
    ).unwrap()
    query = DashboardQuery(
        view=values.get('view', 'due'),
        sort=values.get('sort', 'urgency'),
        state=values.get('state', 'all'),
        tournament_id=values.get('tournament'),
        page=int(values.get('page', 1)),
        per_page=settings.page_size,
    )
    return dashboard_service.get_dashboard_page(
        _viewer(user), party_id, query, settings=settings, now=NOW
    ).unwrap()


def _tiers(page: DashboardPage) -> dict[str, int]:
    counts = page.tier_counts
    return {'r': counts.red, 'y': counts.yellow, 'g': counts.green}


def _admin_reads_assigned(surface: Surface, query: str) -> str:
    """Add what makes the admin list the viewer's own tournaments."""
    return f'scope=assigned&{query}' if surface.name == 'admin' else query


# -- the six named tests --

QUERIES = {
    'default': {},
    'every-view': {'view': 'all'},
    'upcoming': {'view': 'upcoming'},
    'by-wait': {'view': 'all', 'sort': 'wait'},
    'conflicts-only': {'state': 'conflict'},
    'one-tournament': {'tournament': 't2'},
    'beyond-the-last-page': {'page': '2'},
}


@pytest.mark.parametrize('name', list(QUERIES))
def test_real_apps_and_poll_agree_for_same_scope(shared_world, surfaces, name):
    world = shared_world
    cast = world.cast
    values = dict(QUERIES[name])
    if 'tournament' in values:
        values['tournament'] = world.t2
    query = urlencode(values)
    expected = canonical(world.party_id, cast.power, **values)
    expected_ids = [str(row.match_id) for row in expected.rows]
    if name == 'beyond-the-last-page':
        assert expected.rows == () and expected.total_count > 0
    else:
        assert expected_ids

    # The same four tournaments, reached as the same person through the
    # admin and both site apps, and as a scoped orga through the site apps.
    reached = [
        ('admin, administrator', surfaces('admin', cast.power)),
        ('site, administrator', surfaces('site', cast.power)),
        ('bote, administrator', surfaces('bote', cast.power)),
        ('site, scoped orga', surfaces('site', cast.orga_a)),
        ('bote, scoped orga', surfaces('bote', cast.orga_a)),
    ]
    panels = {}
    for label, surface in reached:
        page = surface.page(_admin_reads_assigned(surface, query))
        poll = surface.poll(_admin_reads_assigned(surface, query))
        assert (page.status_code, poll.status_code) == (200, 200), label
        assert page.headers.get('Cache-Control') == 'private, no-store', label
        assert poll.headers.get('Cache-Control') == 'private, no-store', label

        panel = panel_of(page.get_data(as_text=True))
        answer = poll.get_json()
        assert set(answer) == {'html', 'as_of', 'poll_seconds'}, label
        # The poll is the page's own panel, byte for byte, at one moment.
        assert answer['html'] == panel, label
        assert answer['as_of'] == NOW_ISO, label
        assert f'data-as-of="{NOW_ISO}"' in panel, label
        assert answer['poll_seconds'] == 30, label

        # What the pipeline shows is what the service computed.
        assert row_ids(panel) == expected_ids, label
        assert tiles_of(panel) == _tiers(expected), label
        assert f'<h2 id="ltd-count">{expected.total_count} ' in panel, label
        panels[label] = normalise(panel)

    # Everything else of the panel is identical on every surface.
    first_label, first = next(iter(panels.items()))
    for label, panel in panels.items():
        assert panel == first, f'{label} differs from {first_label}'

    if name == 'default':
        # The scene is not empty: urgent rows lead, a conflict is flagged.
        assert expected_ids[0] == str(world.red)
        assert expected.rows[0].conflicts
        assert sum(_tiers(expected).values()) >= 3


def test_get_and_poll_execute_no_dml(shared_world, surfaces):
    world = shared_world
    cast = world.cast
    reached = [
        surfaces('admin', cast.power),
        surfaces('site', cast.power),
        surfaces('bote', cast.power),
        surfaces('site', cast.orga_a),
        surfaces('bote', cast.orga_a),
    ]
    queries = ['', 'view=all&sort=wait', 'state=pinned', 'page=999']
    for surface in reached:
        # The first request of a session may open it; measure the second.
        surface.page()
        for query in queries:
            for kind, fetch in (('page', surface.page), ('poll', surface.poll)):
                before = fingerprint()
                with Recorder() as recorder:
                    response = fetch(_admin_reads_assigned(surface, query))
                after = fingerprint()
                label = f'{surface.name} {kind} {query!r}'

                assert response.status_code == 200, label
                # Real reads happened, and nothing else.
                assert any(
                    'lan_tournament_matches' in sql
                    for sql in recorder.statements
                ), label
                for sql in recorder.statements:
                    assert SELECT_ONLY.match(sql), f'{label}: {sql[:120]}'
                    assert not WRITES.search(sql), f'{label}: {sql[:120]}'
                # The snapshot ends by rolling back; nothing is committed.
                assert recorder.commits == 0, label
                assert recorder.rollbacks >= 1, label
                # No table of the module was written, not even to the same
                # values: the clock of the legacy tournament stays unknown.
                assert after == before, label

    legacy = db.session.execute(
        text(
            'SELECT operational_clock_activated_at,'
            ' operational_clock_running_since FROM lan_tournaments'
            ' WHERE id = :id'
        ),
        {'id': world.t_legacy},
    ).one()
    assert tuple(legacy) == (None, None)


# -- who may see what --


def hidden_secrets(world: World) -> dict[str, str]:
    """Return what only people outside the visible tournaments may know."""
    cast = world.cast
    rows = db.session.execute(
        text(
            'SELECT p.id AS participant, e.id AS episode'
            ' FROM lan_tournament_participants p'
            ' LEFT JOIN lan_tournament_match_due_episodes e'
            '   ON e.tournament_id = p.tournament_id'
            ' WHERE p.tournament_id = ANY(:ids)'
        ),
        {'ids': [world.t_hidden, world.t_far]},
    ).all()
    db.session.rollback()
    secrets = {
        'hidden tournament id': str(world.t_hidden),
        'hidden tournament name': world.names['t_hidden'],
        'hidden team name': world.names['team'],
        'other party tournament id': str(world.t_far),
        'other party tournament name': world.names['t_far'],
        'hidden orga name': name_of(cast.orga_c),
        'hidden orga id': str(cast.orga_c.id),
        # Times of the hidden matches: the wait and the occupancy.
        'hidden wait start': '13:27',
        'hidden occupancy': '13:19',
        'hidden last change': '03:22',
    }
    for number, user in enumerate(cast.hidden):
        secrets[f'hidden player {number} name'] = name_of(user)
        secrets[f'hidden player {number} id'] = str(user.id)
    for match_id in (world.hy, world.hz, world.ht, world.hd, world.far):
        secrets[f'hidden match {match_id}'] = str(match_id)
    for row in rows:
        secrets[f'hidden participant {row.participant}'] = str(row.participant)
        if row.episode is not None:
            secrets[f'hidden episode {row.episode}'] = str(row.episode)
    return secrets


def secrets_of_the_smaller_scope(world: World) -> dict[str, str]:
    """Return what the orga of the first tournament alone must not see."""
    cast = world.cast
    secrets = hidden_secrets(world)
    for key in ('t2', 't_paused', 't_legacy'):
        secrets[f'tournament {key} name'] = world.names[key]
    for key in ('t2', 't_paused', 't_legacy'):
        secrets[f'tournament {key} id'] = str(getattr(world, key))
    for key in ('bee', 'later', 'frozen', 'legacy'):
        secrets[f'match {key}'] = str(getattr(world, key))
    for number in (6, 7):
        secrets[f'player {number} name'] = name_of(cast.players[number])
        secrets[f'player {number} id'] = str(cast.players[number].id)
    return secrets


def hidden_changes(world: World) -> None:
    """Change the hidden tournaments in ways the orga must not notice.

    p2 and p3 stay needed in a hidden match, so the one boolean each
    has stays what it was. Everything else about the hidden side moves.
    """
    cast = world.cast
    scene = world.scene
    h = cast.hidden

    db.session.execute(
        text('UPDATE lan_tournaments SET name = :name WHERE id = :id'),
        {'name': f'Umbenannt Sigma {cast.suffix}', 'id': world.t_hidden},
    )
    db.session.execute(
        text(
            'UPDATE lan_tournament_matches SET last_changed_at = :at,'
            ' occupied_since = :at WHERE id = ANY(:ids)'
        ),
        {'at': datetime(2026, 3, 3, 3, 33, 33), 'ids': [world.hy, world.hz]},
    )
    db.session.commit()
    # More hidden demand of every tier, and one more hidden orga.
    scene.duel(world.t_hidden, h[2], h[3], wait=120)
    scene.duel(world.t_hidden, h[0], h[2], wait=44)
    scene.duel(world.t_hidden, h[1], h[3], wait=2)
    scene.assign(world.t_hidden, cast.confirmer)
    # A hidden check with a comment and a hidden pin, as the real services
    # take them.
    orga_c = _viewer(cast.orga_c)
    episode = db.session.execute(
        text(
            'SELECT id FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :m AND closed_at IS NULL'
        ),
        {'m': world.hy},
    ).scalar_one()
    db.session.rollback()
    coordination.acknowledge_match(
        orga_c,
        world.party_id,
        world.hy,
        expected_episode_id=episode,
        expected_ack_revision=0,
        comment=f'GEHEIM-Kommentar {cast.suffix}',
    ).unwrap()
    coordination.set_match_pin(
        orga_c, world.party_id, world.hz, pinned=True, expected_revision=0
    ).unwrap()
    # A hidden result settles a match that never involved p2 or p3.
    db.session.execute(
        text(
            'UPDATE lan_tournament_matches SET confirmed_by = :user'
            ' WHERE id = :id'
        ),
        {'user': cast.confirmer.id, 'id': world.hz},
    )
    # Demand in another party moves too.
    db.session.execute(
        text(
            'UPDATE lan_tournament_match_due_episodes SET opened_at ='
            " opened_at - interval '11 minutes' WHERE match_id = :m"
        ),
        {'m': world.far},
    )
    db.session.commit()


# -- forms, as the page renders them --


def hidden_inputs(markup: str) -> dict[str, str]:
    return {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', markup
        )
    }


def form_of(row: str, css_class: str) -> tuple[str, dict[str, str]]:
    """Return the action and the hidden fields of a form of a row."""
    found = re.search(
        rf'<form class="{css_class}"[^>]*?action="([^"]*)"[^>]*>(.*?)</form>',
        row,
        flags=re.DOTALL,
    )
    assert found, css_class
    return html.unescape(found[1]), hidden_inputs(found[2])


def action_path(surface: Surface, kind: str, match_id) -> str:
    base = (
        f'/lan-tournaments/for_party/{surface.party_id}/dashboard'
        if surface.name == 'admin'
        else SITE_BASE
    )
    return f'{base}/matches/{match_id}/{kind}'


# -- test: nothing unauthorized in any transport --


def _transports(world: World, surface: Surface, hidden_match) -> dict:
    """Capture every kind of answer the viewer can provoke."""
    answers = {}
    for name, wanted in (('default', ''), ('every view', 'view=all')):
        query = _admin_reads_assigned(surface, wanted)
        answers[f'page {name}'] = capture(surface.page(query))
        answers[f'poll {name}'] = capture(surface.poll(query))

    # The forms of a row, as a browser would send them back.
    page = surface.page(_admin_reads_assigned(surface, ''))
    row = rows_of(panel_of(page.get_data(as_text=True)))[str(world.yellow)]
    ack_action, fields = form_of(row, 'ltd-form')
    stale = surface.post(
        ack_action, {**fields, 'revision': '7', 'comment': 'veraltet'}
    )
    assert stale.status_code == 409
    assert stale.get_json()['error'] == 'stale'
    assert stale.get_json()['fragment']['html']
    answers['ack, stale revision'] = capture(stale)

    sent = {**fields, 'revision': '0', 'comment': ''}
    answers['ack, hidden match, json'] = capture(
        surface.post(action_path(surface, 'ack', hidden_match), sent)
    )
    answers['ack, hidden match, native'] = capture(
        surface.post(
            action_path(surface, 'ack', hidden_match), sent, native=True
        )
    )
    return answers


def _viewers(world: World, surfaces):
    """Return (label, surface, hidden match, what it must not see)."""
    cast = world.cast
    everywhere = hidden_secrets(world)
    smaller = secrets_of_the_smaller_scope(world)
    return [
        (
            'admin, assigned',
            surfaces('admin', cast.power),
            world.far,
            everywhere,
        ),
        (
            'site, orga of four',
            surfaces('site', cast.orga_a),
            world.hy,
            everywhere,
        ),
        (
            'bote, orga of four',
            surfaces('bote', cast.orga_a),
            world.hy,
            everywhere,
        ),
        (
            'site, orga of one',
            surfaces('site', cast.orga_b),
            world.bee,
            smaller,
        ),
        (
            'bote, orga of one',
            surfaces('bote', cast.orga_b),
            world.bee,
            smaller,
        ),
    ]


def test_complete_transport_has_no_unauthorized_metadata(world, surfaces):
    cast = world.cast
    viewers = _viewers(world, surfaces)

    before = {}
    for label, surface, hidden_match, secrets in viewers:
        before[label] = _transports(world, surface, hidden_match)
        for name, captured in before[label].items():
            # Bytes, attributes, ARIA, JSON and headers: all of it.
            assert captured[0] < 500, f'{label} {name}'
            assert_clean(f'{label}: {name}', captured, secrets)
        assert_session_clean(label, surface, secrets)

    # What the viewers may see is there, so the scan is not blind.
    page = viewers[1][1].page().get_data(as_text=True)
    assert name_of(cast.players[2]) in page
    assert world.names['t1'] in page
    assert str(world.yellow) in page
    assert 'ltd-conf is-ext' in page
    # The smaller scope shows a smaller list: no tournament of the others.
    small = viewers[3][1].page().get_data(as_text=True)
    assert world.names['t1'] in small
    assert world.names['t2'] not in small
    assert str(world.bee) not in small

    # Nobody without authority is told anything either.
    secrets = secrets_of_the_smaller_scope(world)
    outsider = surfaces('site', cast.outsider)
    anonymous = surfaces('site', None)
    for label, answer, status in (
        ('outsider page', outsider.page(), 403),
        ('outsider poll', outsider.poll(), 403),
        ('anonymous poll', anonymous.poll(), 401),
    ):
        assert answer.status_code == status, label
        assert_clean(label, capture(answer), secrets)
    refused = outsider.poll().get_json()
    assert set(refused) == {'error', 'message'}

    # The hidden side moves; the orga's answers do not change by a byte.
    hidden_changes(world)
    for label, surface, hidden_match, secrets in viewers:
        after = _transports(world, surface, hidden_match)
        assert after.keys() == before[label].keys()
        for name, captured in after.items():
            assert_same(f'{label}: {name}', before[label][name], captured)
            assert_clean(f'{label}: {name} after', captured, secrets)
        assert_session_clean(f'{label} after', surface, secrets)


def test_missing_hidden_and_malformed_matches_get_one_answer(
    shared_world, surfaces
):
    world = shared_world
    cast = world.cast
    for label, surface, hidden in (
        ('admin', surfaces('admin', cast.power), [world.far]),
        (
            'site',
            surfaces('site', cast.orga_a),
            [world.hy, world.hz, world.far],
        ),
        ('bote', surfaces('bote', cast.orga_a), [world.hy]),
    ):
        page = surface.page(_admin_reads_assigned(surface, ''))
        row = rows_of(panel_of(page.get_data(as_text=True)))[str(world.yellow)]
        ack_action, fields = form_of(row, 'ltd-form')
        pin_action, pin_fields = form_of(row, 'ltd-pin')
        assert pin_action and ack_action
        sent = {
            'ack': {**fields, 'revision': '0', 'comment': ''},
            'pin': {**pin_fields, 'revision': '0'},
        }
        for kind in ('ack', 'pin'):
            for native in (False, True):
                answers = {}
                for what, match_id in (
                    ('missing', uuid4()),
                    ('malformed', 'not-a-uuid'),
                    ('too long', 'x' * 300),
                    *((f'hidden {m}', m) for m in hidden),
                ):
                    answers[what] = capture(
                        surface.post(
                            action_path(surface, kind, match_id),
                            sent[kind],
                            native=native,
                        )
                    )
                    assert answers[what][0] == 404, (label, kind, what)
                distinct = set(answers.values())
                assert len(distinct) == 1, (label, kind, native, answers.keys())
                assert_clean(
                    f'{label} {kind} {native}',
                    next(iter(distinct)),
                    hidden_secrets(world),
                )


# -- test: hostile filters --

CRITICAL_QUERIES = ('scope', 'tournament', 'view=%3C', 'page=99')


def _hostile(world: World) -> list[str]:
    hidden = world.t_hidden
    far = world.t_far
    missing = uuid4()
    long_text = 'v' * 6000
    return [
        'scope=all',
        'scope=ALL',
        'scope=all%00',
        'scope=assigned&scope=all',
        'scope[]=all',
        'scope=',
        'scope=%20all',
        f'tournament={hidden}',
        f'tournament={far}',
        f'tournament={missing}',
        'tournament=not-a-uuid',
        f'tournament={hidden}%00',
        f'tournament={hidden}&tournament={world.t1}',
        f'tournament={world.t1}&tournament={hidden}',
        f'tournament={"a" * 5000}',
        f'tournament_id={hidden}&party_id={far}&user_id={world.cast.orga_c.id}',
        'view=bogus',
        'view=ALL',
        'view=all&view=due',
        'view=%3Cscript%3Ealert(1)%3C%2Fscript%3E',
        'state=bogus',
        "state=conflict'%20OR%201%3D1--",
        'state=tier-red%00',
        'state=%ZZ',
        'sort=bogus',
        'sort=urgency%3BDROP%20TABLE%20lan_tournaments',
        'page=0',
        'page=-1',
        'page=abc',
        'page=1e3',
        'page=2.5',
        'page=%20',
        'page=99999999999999999999',
        'page=%D9%A3',
        'page=1&page=2',
        f'view={long_text}',
        'view=%C3%BC%F0%9F%92%A5&sort=%00&page=%',
        '%00=%00&&&=&x',
        'debug=1&_anchor=x&return=http%3A%2F%2Fevil.test%2F',
    ]


def _visible(world: World) -> dict[str, set[str]]:
    return {
        'matches': world.visible_matches,
        'tournaments': world.visible_tournaments,
    }


def _option_values(panel: str) -> list[str]:
    select = re.search(
        r'<select id="lt-filter-tournament".*?</select>', panel, re.DOTALL
    )
    assert select
    return re.findall(r'<option value="([^"]*)"', select[0])


def test_bad_filters_never_widen_scope_or_500(shared_world, surfaces):
    world = shared_world
    cast = world.cast
    secrets = hidden_secrets(world)
    visible = _visible(world)
    viewers = [
        ('site', surfaces('site', cast.orga_a)),
        ('bote', surfaces('bote', cast.orga_a)),
        ('admin', surfaces('admin', cast.power)),
    ]

    for label, surface in viewers:
        # What the viewer gets without asking for anything.
        baseline_panel = panel_of(surface.page().get_data(as_text=True))
        baseline_tiles = tiles_of(baseline_panel)
        baseline_options = _option_values(baseline_panel)
        assert set(baseline_options) == {'', *visible['tournaments']}
        assert baseline_tiles != {}

        # The themed site runs the same routes as the generic one: it gets
        # the queries that go for the scope and for markup.
        queries = [
            query
            for query in _hostile(world)
            if label != 'bote' or query.startswith(CRITICAL_QUERIES)
        ]
        for query in queries:
            if label == 'admin' and query == 'scope=all':
                continue  # the administrator's own way to see more, below

            for kind, fetch in (('page', surface.page), ('poll', surface.poll)):
                response = fetch(query)
                where = f'{label} {kind} {query[:60]!r}'
                # A bad value is dropped with a notice: never a 500.
                assert response.status_code == 200, where
                assert (
                    response.headers.get('Cache-Control') == 'private, no-store'
                )
                # What the request itself carried may come back as the
                # notice about it; nothing else of the hidden side may.
                foreign = {
                    what: value
                    for what, value in secrets.items()
                    if value not in query
                }
                assert_clean(where, capture(response), foreign)

                body = response.get_data(as_text=True)
                panel = (
                    panel_of(body)
                    if kind == 'page'
                    else response.get_json()['html']
                )
                # No row of anyone else's, and the same tiles and the same
                # choices as without a query: nothing was widened.
                assert set(row_ids(panel)) <= visible['matches'], where
                assert tiles_of(panel) == baseline_tiles, where
                assert set(_option_values(panel)) == set(baseline_options), (
                    where
                )
                assert '<script>alert(1)</script>' not in body, where

    # A hidden tournament reads exactly like one that does not exist.
    for label, surface in viewers:
        for kind in ('page', 'poll'):
            fetch = surface.page if kind == 'page' else surface.poll
            missing = uuid4()
            answers = {}
            for what, value in (
                ('hidden', world.t_hidden),
                ('other party', world.t_far),
                ('missing', missing),
            ):
                response = fetch(f'tournament={value}')
                assert response.status_code == 200, (label, kind, what)
                text_body = response.get_data(as_text=True)
                answers[what] = text_body.replace(str(value), '{{id}}')
            assert len(set(answers.values())) == 1, (label, kind)

    # The one way to see more is the administrator's own: exactly `all`.
    admin = surfaces('admin', cast.power)
    widened = panel_of(admin.page('scope=all').get_data(as_text=True))
    assert str(world.hy) in row_ids(widened)
    assert str(world.far) not in row_ids(widened)
    assert world.names['t_far'] not in widened
    assert world.names['t_hidden'] in widened

    # A scoped orga has no admin dashboard to widen: the backend reads a
    # person without `admin.access` as nobody who is logged in.
    orga = surfaces('admin', cast.orga_a)
    for query in ('scope=all', ''):
        page = orga.page(query)
        poll = orga.poll(query)
        assert page.status_code == 302
        assert 'log_in' in page.headers['Location']
        assert poll.status_code == 401
        assert poll.get_json()['error'] == 'session_expired'
        assert_clean('scoped orga on the admin', capture(poll), secrets)
        assert_clean('scoped orga on the admin', capture(page), secrets)
    # Nor does somebody without an assignment on the site.
    outsider = surfaces('site', cast.outsider)
    for query in _hostile(world)[:12]:
        assert outsider.page(query).status_code == 403
        assert outsider.poll(query).status_code == 403


# -- test: a check of one orga is the next orga's to read --


def _record_of(row: str) -> dict:
    """Read the latest record of a row: who, when, what was written."""
    found = re.search(
        r'<div class="ltd-rec" id="[^"]*" tabindex="-1">\n'
        r'<p class="ltd-rech"><b>([^<]*)</b>(<span class="ltd-follow'
        r' is-(\w+)">)?.*?</p>(\n<p class="ltd-recc"><q>(.*?)</q></p>)?',
        row,
        flags=re.DOTALL,
    )
    assert found, 'no record in the row'
    history = re.search(
        r'<span class="ltd-dc">([^<]*)</span></button>\n'
        r'<ol class="ltd-hist"[^>]*>(.*?)</ol>',
        row,
        flags=re.DOTALL,
    )
    return {
        'text': html.unescape(found[1]),
        'follow': found[3],
        'comment': found[5],
        'history_count': history[1] if history else None,
        'history': history[2] if history else '',
    }


def _tier_of(row: str) -> str:
    return re.search(r'<div class="ltd-tier t-(\w+)">', row)[1]  # type: ignore[index]


def _wait_of(row: str) -> str:
    return re.search(r'<dt>Aktive Wartezeit gesamt</dt><dd>([^<]*)</dd>', row)[
        1
    ]  # type: ignore[index]


def _ack(surface: Surface, row: str, comment: str):
    """Send the check form of a row as its page rendered it."""
    action, fields = form_of(row, 'ltd-form')
    response = surface.post(action, {**fields, 'comment': comment})
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    assert response.get_json()['fragment']['html']
    return response


def _reads(surface: Surface, query: str = '') -> tuple[str, dict[str, str]]:
    """Read the list twice, as a reload and as a poll, and see they agree."""
    query = _admin_reads_assigned(surface, query)
    page = surface.page(query)
    poll = surface.poll(query)
    assert (page.status_code, poll.status_code) == (200, 200)
    panel = panel_of(page.get_data(as_text=True))
    assert poll.get_json()['html'] == panel
    return panel, rows_of(panel)


def test_other_authorized_orga_observes_shared_ack_on_reload_and_poll(
    world, surfaces, clock
):
    cast = world.cast
    sx = cast.suffix
    first = f'Beide <b>Teams</b> am Tresen & bereit „{sx}“'
    other = f'Zwei-Turnier-Kommentar {sx}'
    follow_up = f'Zweite Prüfung durch B {sx}'
    a_name, b_name = name_of(cast.orga_a), name_of(cast.orga_b)

    orga_a = surfaces('site', cast.orga_a)
    orga_b = surfaces('site', cast.orga_b)
    orga_b_bote = surfaces('bote', cast.orga_b)
    orga_c = surfaces('site', cast.orga_c)
    power = surfaces('admin', cast.power)
    yellow, bee = str(world.yellow), str(world.bee)

    # A checks the delay of a match of the first and one of the second
    # tournament, with the forms as the page renders them.
    _, rows = _reads(orga_a)
    assert _tier_of(rows[yellow]) == 'y' and _tier_of(rows[bee]) == 'y'
    assert '<div class="ltd-rec"' not in rows[yellow]
    waited = _wait_of(rows[yellow])
    _ack(orga_a, rows[yellow], first)
    _ack(orga_a, rows[bee], other)

    # B is assigned to the first tournament only: a reload, a poll and the
    # themed site all show who checked, when and what they wrote.
    for surface in (orga_b, orga_b_bote):
        panel, rows = _reads(surface)
        assert str(world.bee) not in panel
        record = _record_of(rows[yellow])
        assert a_name in record['text'] and '14:00' in record['text']
        assert record['comment'] == html.escape(first, quote=False)
        assert '<b>Teams</b>' not in panel  # the comment is text
        assert record['follow'] is None and record['history_count'] is None
        # The check reset the alert interval and nothing else.
        assert _tier_of(rows[yellow]) == 'g'
        assert _wait_of(rows[yellow]) == waited
        assert f'data-as-of="{NOW_ISO}"' in panel
        # Nothing of the second tournament, which only A and the
        # administrator may read.
        secrets = {
            'second tournament': world.names['t2'],
            'second comment': other,
            'second match': bee,
            'second only player': name_of(cast.players[6]),
        }
        assert_clean(f'{surface.name} B', capture(surface.page()), secrets)
        assert_clean(f'{surface.name} B poll', capture(surface.poll()), secrets)

    # The administrator is assigned to both and reads both.
    _, rows = _reads(power)
    assert a_name in _record_of(rows[yellow])['text']
    assert _record_of(rows[bee])['comment'] == html.escape(other, quote=False)

    # Neither the orga of the hidden tournament nor an outsider is told.
    secrets = {
        'checking orga': a_name,
        'first comment': first,
        'second comment': other,
        'first tournament': world.names['t1'],
        'second tournament': world.names['t2'],
        'first match': yellow,
        'second match': bee,
    }
    panel, rows = _reads(orga_c)
    assert set(rows) == {str(world.hy), str(world.hz), str(world.ht)}
    assert_clean('C page', capture(orga_c.page()), secrets)
    assert_clean('C poll', capture(orga_c.poll()), secrets)
    assert surfaces('site', cast.outsider).page().status_code == 403

    # Twenty minutes of active time later the delay is yellow again. A's
    # record stays, as the last one, until somebody writes a newer one.
    clock.now = NOW + timedelta(minutes=20)
    _, rows = _reads(orga_b)
    assert _tier_of(rows[yellow]) == 'y'
    record = _record_of(rows[yellow])
    assert record['follow'] == 'yellow' and a_name in record['text']
    assert 'Nochmals prüfen' in rows[yellow]
    _ack(orga_b, rows[yellow], follow_up)

    # A reads B's record on top of their own, at the new time.
    for fetch in ('reload', 'poll'):
        query = _admin_reads_assigned(orga_a, '')
        response = (
            orga_a.page(query) if fetch == 'reload' else orga_a.poll(query)
        )
        body = (
            panel_of(response.get_data(as_text=True))
            if fetch == 'reload'
            else response.get_json()['html']
        )
        row = rows_of(body)[yellow]
        record = _record_of(row)
        assert b_name in record['text'] and '14:20' in record['text']
        assert record['comment'] == follow_up
        assert record['history_count'] == '(2 in dieser Episode)'
        # The history lists both checks, newest first, each with its actor,
        # time and comment.
        history = record['history']
        assert history.index(b_name) < history.index(a_name)
        assert '14:20' in history and '14:00' in history
        assert follow_up in history
        assert html.escape(first, quote=False) in history
        assert _wait_of(row) == '40 min'
        assert _tier_of(row) == 'g'
        if fetch == 'poll':
            assert response.get_json()['as_of'] == LATER_ISO
        else:
            assert f'data-as-of="{LATER_ISO}"' in body
    assert_clean(
        'A after B',
        capture(orga_a.page()),
        {
            'hidden orga': name_of(cast.orga_c),
            'hidden tournament': world.names['t_hidden'],
        },
    )

    # The administrator reads the same newest record.
    _, rows = _reads(power)
    assert b_name in _record_of(rows[yellow])['text']


# -- test: the external hint says one thing, however much is behind it --


def _external_blocks(panel: str) -> list[str]:
    return re.findall(r'<div class="ltd-conf is-ext">.*?</div>', panel)


def test_external_hint_bytes_identical_for_one_or_many_hidden_overlaps(
    party, cast, surfaces, clock
):
    sx = cast.suffix
    p, h = cast.players, cast.hidden
    scene = Scene(party.id, cast.confirmer)

    # Three due matches of the orga's tournament. The second has waited
    # longer than the first, so without a conflict it leads the list.
    visible = scene.tournament(f'R7 Sichtbar {sx}')
    first = scene.duel(visible, p[0], p[1], wait=40)
    second = scene.duel(visible, p[2], p[3], wait=42)
    scene.duel(visible, p[4], p[5], wait=5)
    scene.assign(visible, cast.orga_a, cast.power)

    reached = [
        ('site', surfaces('site', cast.orga_a)),
        ('bote', surfaces('bote', cast.orga_a)),
        ('admin', surfaces('admin', cast.power)),
    ]

    def read() -> dict[str, tuple]:
        answers = {}
        for label, surface in reached:
            query = _admin_reads_assigned(surface, '')
            answers[f'{label} page'] = capture(surface.page(query))
            answers[f'{label} poll'] = capture(surface.poll(query))
            answers[f'{label} every view'] = capture(
                surface.page(_admin_reads_assigned(surface, 'view=all'))
            )
        return answers

    def panel(answers: dict, key: str) -> str:
        body = answers[key][2].decode()
        return (
            json.loads(body)['html'] if key.endswith('poll') else panel_of(body)
        )

    # Nobody else needs p0: no hint.
    none = read()
    for label, _ in reached:
        assert _external_blocks(panel(none, f'{label} page')) == []
        assert row_ids(panel(none, f'{label} page'))[:2] == [
            str(second),
            str(first),
        ]

    # One hidden match needs p0 as well.
    elsewhere = scene.tournament(f'R7 Verborgen Eins {sx}')
    scene.assign(elsewhere, cast.orga_c)
    scene.duel(elsewhere, p[0], h[0], wait=30)
    one = read()

    # Then many: more matches of the same hidden tournament, a second and
    # a third tournament, a team that p0 plays in, and everything that
    # does not count as demand (paused, coming, confirmed).
    scene.duel(elsewhere, p[0], h[3], wait=55)
    scene.duel(elsewhere, p[0], h[2], confirmed=True)
    other = scene.tournament(f'R7 Verborgen Zwei {sx}')
    scene.duel(other, p[0], h[1], wait=90)
    teams = scene.tournament(f'R7 Verborgen Teams {sx}')
    team = scene.team(teams, f'R7 Geheimteam {sx}', p[0], h[2])
    scene.for_team(teams, team, wait=12)
    third = scene.tournament(f'R7 Verborgen Drei {sx}', mode=ROUND_ROBIN)
    scene.duel(third, h[2], h[3], wait=8)
    scene.duel(third, p[0], h[0], round=2, wait=None)
    paused = scene.tournament(
        f'R7 Verborgen Pause {sx}', status=PAUSED, clock='frozen'
    )
    scene.duel(paused, p[0], h[1], wait=9)
    many = read()

    secrets = {
        f'hidden tournament {name}': name
        for name in (
            f'R7 Verborgen Eins {sx}',
            f'R7 Verborgen Zwei {sx}',
            f'R7 Verborgen Drei {sx}',
            f'R7 Verborgen Pause {sx}',
            f'R7 Verborgen Teams {sx}',
            f'R7 Geheimteam {sx}',
        )
    }
    for number, user in enumerate(h):
        secrets[f'hidden player {number}'] = name_of(user)

    for label, _ in reached:
        for kind in ('page', 'poll', 'every view'):
            key = f'{label} {kind}'
            # One hidden overlap or many: the same answer, byte for byte,
            # in status, headers, markup, attributes, ARIA, order and JSON.
            assert_same(key, one[key], many[key])
            assert_clean(key, many[key], secrets)
            assert one[key] != none[key], key

        blocks = _external_blocks(panel(many, f'{label} page'))
        # One person, one block: a name and one fixed sentence.
        assert len(blocks) == 1
        assert blocks[0] == _external_blocks(panel(one, f'{label} page'))[0]
        assert name_of(cast.players[0]) in blocks[0]
        assert '<a ' not in blocks[0] and 'href' not in blocks[0]
        assert re.sub(r'<[^>]+>', '', blocks[0]).strip() == (
            f'⇄{name_of(cast.players[0])} wird gleichzeitig in einer'
            ' Begegnung außerhalb deiner Turniere benötigt.'
        )
        # The hint weighs the same wherever it comes from: the conflicted
        # match leads the list and the order stays what one overlap made it.
        assert row_ids(panel(many, f'{label} page'))[:2] == [
            str(first),
            str(second),
        ]
        assert row_ids(panel(many, f'{label} page')) == row_ids(
            panel(one, f'{label} page')
        )
