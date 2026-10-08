"""The way back to the orga dashboard from the admin tournament and match page.

The real admin app, templates, permissions and PostgreSQL. The `return`
parameter is only ever parsed and rebuilt: nothing of it may reach the page.
"""

from datetime import datetime, UTC
from html import unescape
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
import flask_babel
import pytest
from sqlalchemy import event

from byceps.database import db
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_match_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.blueprints.admin import (
    views as admin_views,
)
from byceps.services.lan_tournament.dashboard_view_helpers import (
    build_dashboard_return,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardQuery,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import uuid7

from tests.helpers import log_in_user


BASE = 'http://admin.acmecon.test/lan-tournaments'
LIST_PATH = '/lan-tournaments/for_party/{party_id}/dashboard'

# Not the default list on any field, so a link to the default list is a
# fallback and not a coincidence.
RETURN_VALID = build_dashboard_return(
    DashboardQuery(
        scope='all', view='all', sort='tournament', page=3, per_page=50
    ),
    surface='admin',
)
# The rebuilt URL leaves out every default (here the state).
VALID_PARAMETERS = {
    'scope': ['all'],
    'view': ['all'],
    'sort': ['tournament'],
    'page': ['3'],
}

MARKER = 'F03MARK'

# Every value has a valid, non-default part where it can, so only a whole
# rejection reads as the default list.
FALLBACK_VALUES = {
    'a URL': f'https://{MARKER}.example/x',
    'a protocol-relative URL': f'//{MARKER}.example/x',
    'a path': f'/lan-tournaments/{MARKER}/dashboard',
    'markup': f'<script>{MARKER}()</script>',
    'an attribute breakout': f'"><img src=x onerror={MARKER}>',
    'a script scheme': f'javascript:{MARKER}',
    'an unknown key': f'view=all&{MARKER}=1',
    'a foreign party': f'view=all&party_id={MARKER}',
    'an unknown view': f'view={MARKER}&sort=wait',
    'a malformed pair': f'view=all&{MARKER}',
    'a duplicate key': 'view=all&view=upcoming',
    'a malformed escape': f'view=all&sort=%zz{MARKER}',
    'too long': 'page=2&' + 'view=all&' * 200,
}

_PARAGRAPH = re.compile(
    r'<p class="block" data-dashboard-return>(.*?)</p>', re.DOTALL
)
_WITH_LINE_BREAK = re.compile(
    r'\n<p class="block" data-dashboard-return>.*?</p>', re.DOTALL
)
_LINK = re.compile(
    r'<a href="([^"]*)">([^<]*)</a> <span class="dimmed">([^<]*)</span>'
)


# -- fixtures --


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'F03RlPlayer{i}') for i in range(4)]


@pytest.fixture(scope='module')
def administrator(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'},
        screen_name='F03RlAdministrator',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def view_only(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.view'}, screen_name='F03RlViewOnly'
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def no_view(make_admin):
    user = make_admin({'admin.access'}, screen_name='F03RlNoView')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def administrator_client(make_client, admin_app, administrator):
    return make_client(admin_app, user_id=administrator.id)


@pytest.fixture(scope='module')
def view_only_client(make_client, admin_app, view_only):
    return make_client(admin_app, user_id=view_only.id)


@pytest.fixture(scope='module')
def no_view_client(make_client, admin_app, no_view):
    return make_client(admin_app, user_id=no_view.id)


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


@pytest.fixture
def german(monkeypatch, german_translations):
    """Read the catalogue as it stands, not the compiled one on disk."""
    monkeypatch.setattr(
        flask_babel.Domain,
        'get_translations',
        lambda self: german_translations,
    )


class Site:
    """A party with one tournament and one match, ready to be opened."""

    def __init__(self, make_party, brand, players, label: str) -> None:
        self.party_id = PartyID(f'f03rl-{label}-{uuid4().hex[:10]}')
        make_party(brand, self.party_id, f'F03 return {self.party_id}')

        result = tournament_service.create_tournament(
            self.party_id,
            f'Return links {self.party_id}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result
        self.tournament, _ = result.unwrap()

        for user in players[:2]:
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(uuid7()),
                    user_id=user.id,
                    tournament_id=self.tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()

        generated = (
            tournament_match_service.generate_single_elimination_bracket(
                self.tournament.id
            )
        )
        assert generated.is_ok(), generated
        (match,) = tournament_match_service.get_matches_for_tournament_ordered(
            self.tournament.id
        )
        self.match_id = match.id

    @property
    def pages(self) -> tuple[tuple[str, str, object | None], ...]:
        """The two destination pages: name, URL and the row of the anchor."""
        return (
            ('view', f'{BASE}/tournaments/{self.tournament.id}', None),
            ('view_match', f'{BASE}/matches/{self.match_id}', self.match_id),
        )

    @property
    def list_path(self) -> str:
        return LIST_PATH.format(party_id=self.party_id)


@pytest.fixture(scope='module')
def sites(admin_app, make_party, brand, players):
    sites = (
        Site(make_party, brand, players, 'a'),
        Site(make_party, brand, players, 'b'),
    )
    yield sites
    db.session.rollback()
    for site in sites:
        if tournament_repository.find_tournament(site.tournament.id):
            tournament_service.delete_tournament(site.tournament.id)


@pytest.fixture(scope='module')
def site(sites):
    return sites[0]


# -- reading a response --


def _get(client, url: str, **query):
    return client.get(url, query_string=query or None)


def _back_link(response) -> tuple[str, str, str] | None:
    """Return href, text and context of the link, None if the page has none."""
    html = response.get_data(as_text=True)
    paragraphs = _PARAGRAPH.findall(html)
    if not paragraphs:
        return None

    (paragraph,) = paragraphs
    found = _LINK.fullmatch(paragraph.strip())
    assert found is not None, paragraph

    href, text, context = (unescape(part) for part in found.groups())
    return href, text, context


def _target(href: str) -> tuple[str, dict[str, list[str]], str]:
    """Split a link into path, parameters and anchor, and refuse any host."""
    parts = urlsplit(href)
    assert (parts.scheme, parts.netloc) == ('', ''), href
    assert href.startswith('/') and not href.startswith('//'), href

    parameters = parse_qs(parts.query, keep_blank_values=True)
    return parts.path, parameters, parts.fragment


def _anchor(match_id) -> str:
    return '' if match_id is None else f'lt-row-{match_id}'


# -- the named tests --


def test_admin_back_link_rebuilds_validated_list_url(
    administrator_client, sites, german
):
    site, other = sites

    for name, url, match_id in site.pages:
        response = _get(administrator_client, url, **{'return': RETURN_VALID})

        assert response.status_code == 200, name
        link = _back_link(response)
        assert link is not None, name
        href, text, context = link
        path, parameters, fragment = _target(href)
        assert path == site.list_path, name
        assert parameters == VALID_PARAMETERS, name
        assert fragment == _anchor(match_id), name
        assert text == '← Zurück zum Orga-Dashboard', name
        assert context == '(Alle Begegnungen · Turnier · Seite 3)', name

        # Nothing else on the page depends on the parameter.
        plain = _get(administrator_client, url)
        assert _back_link(plain) is None, name
        assert _WITH_LINE_BREAK.sub('', response.get_data(as_text=True)) == (
            plain.get_data(as_text=True)
        ), name

    # The party comes from the tournament, not from the request.
    for name, url, match_id in other.pages:
        response = _get(
            administrator_client,
            url,
            **{'return': RETURN_VALID, 'party_id': str(site.party_id)},
        )

        path, _, fragment = _target(_back_link(response)[0])
        assert path == other.list_path, name
        assert fragment == _anchor(match_id), name


def test_admin_back_link_hidden_without_dashboard_authority(
    administrator_client,
    view_only_client,
    no_view_client,
    anonymous_client,
    site,
):
    for name, url, _ in site.pages:
        # The same request shows the link to an administrator ...
        allowed = _get(administrator_client, url, **{'return': RETURN_VALID})
        assert _back_link(allowed) is not None, name

        # ... but not to a viewer without the dashboard permission, whose
        # page works as it did.
        for value in (RETURN_VALID, 'garbage', ''):
            seen = _get(view_only_client, url, **{'return': value})
            assert seen.status_code == 200, (name, value)
            assert _back_link(seen) is None, (name, value)
            assert b'data-dashboard-return' not in seen.data, (name, value)
            assert site.list_path.encode() not in seen.data, (name, value)

        plain = _get(view_only_client, url)
        valid = _get(view_only_client, url, **{'return': RETURN_VALID})
        assert valid.get_data() == plain.get_data(), name

        # The page permissions are unchanged.
        refused = _get(no_view_client, url, **{'return': RETURN_VALID})
        assert refused.status_code == 403, name
        assert b'data-dashboard-return' not in refused.data, name

        anonymous = _get(anonymous_client, url, **{'return': RETURN_VALID})
        assert anonymous.status_code == 403, name
        assert anonymous.status_code == _get(anonymous_client, url).status_code
        assert b'data-dashboard-return' not in anonymous.data, name


def test_admin_invalid_return_falls_back_to_default_list(
    administrator_client, site, german
):
    for name, url, match_id in site.pages:
        for kind, value in FALLBACK_VALUES.items():
            response = _get(administrator_client, url, **{'return': value})

            assert response.status_code == 200, (name, kind)
            link = _back_link(response)
            assert link is not None, (name, kind)
            href, text, context = link
            path, parameters, fragment = _target(href)
            assert path == site.list_path, (name, kind)
            assert parameters == {}, (name, kind)
            assert fragment == _anchor(match_id), (name, kind)
            assert text == '← Zurück zum Orga-Dashboard', (name, kind)
            assert context == '(Aktuell fällig · Dringlichkeit · Seite 1)', (
                name,
                kind,
            )

        # Nothing to go back to without a value.
        assert _back_link(_get(administrator_client, url)) is None, name
        empty = _get(administrator_client, url, **{'return': ''})
        assert empty.status_code == 200, name
        assert _back_link(empty) is None, name


def test_admin_return_never_reflects_raw_input(administrator_client, site):
    values = [*FALLBACK_VALUES.values(), f'{RETURN_VALID}&{MARKER}=1']
    values.append(f'{RETURN_VALID}&view={MARKER}')

    for name, url, _ in site.pages:
        for value in values:
            response = _get(administrator_client, url, **{'return': value})

            assert response.status_code == 200, (name, value)
            assert 'Location' not in response.headers, (name, value)
            assert MARKER.encode() not in response.data, (name, value)
            assert MARKER.lower().encode() not in response.data.lower(), (
                name,
                value,
            )
            # The one link is a path of this app, with no host and no scheme.
            link = _back_link(response)
            assert link is not None, (name, value)
            path, parameters, _ = _target(link[0])
            assert path == site.list_path, (name, value)
            assert set(parameters) <= set(VALID_PARAMETERS), (name, value)


# -- further coverage of the decisions behind them --


def test_admin_back_link_follows_the_locale(administrator_client, site):
    for name, url, _ in site.pages:
        link = _back_link(
            _get(administrator_client, url, **{'return': RETURN_VALID})
        )

        assert link is not None, name
        assert link[1] == '← Back to the orga dashboard', name


def test_admin_back_link_costs_no_database_statement(
    administrator_client, site
):
    def count(url: str, **query) -> int:
        statements: list[str] = []

        def record(connection, cursor, statement, *_):
            statements.append(statement)

        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            response = _get(administrator_client, url, **query)
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)

        assert response.status_code == 200
        return len(statements)

    for name, url, _ in site.pages:
        without = count(url)
        assert count(url, **{'return': RETURN_VALID}) == without, name
        assert count(url, **{'return': 'garbage'}) == without, name


def test_admin_broken_dashboard_configuration_leaves_the_pages_alone(
    administrator_client, site, monkeypatch
):
    monkeypatch.setattr(
        admin_views, 'get_dashboard_settings', lambda: Err('invalid_poll')
    )

    for name, url, _ in site.pages:
        response = _get(administrator_client, url, **{'return': RETURN_VALID})

        assert response.status_code == 200, name
        assert _back_link(response) is None, name
