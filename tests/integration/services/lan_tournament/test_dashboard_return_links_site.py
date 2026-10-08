from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_orga_service as orgas,
    tournament_repository as repo,
    tournament_service,
)
from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import TournamentID
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
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID
from byceps.util.uuid import uuid7

from tests.helpers import create_site, http_client, log_in_user


REPO_ROOT = Path(__file__).resolve().parents[4]
BASE = '/lan-tournaments'
LIST_PATH = f'{BASE}/orga-dashboard'
TOTALVERPLANT = SiteID('totalverplant-36')

# The test client sends the msgids, independent of the compiled catalogue.
ENGLISH = {'Accept-Language': 'en'}
BACK_LABEL = '← Back to the orga dashboard'

# Distinct from every word the pages print: any echo of input contains it.
CANARY = 'canaryf03x26'

PAGES = ['view', 'view_match']

# fmt: off
SHOWN_TO = ['orga', 'other_orga']
HIDDEN_FROM = [
    'anonymous', 'participant', 'outsider', 'viewer', 'power', 'abroad',
]
# fmt: on


# -------------------------------------------------------------------- #
# the world


@dataclass
class World:
    """Tournaments of two parties, with the people around them.

    `orga` is assigned to tournament A of the site's party, `other_orga`
    to its tournament B and `abroad` to tournament C of another party.
    `power` holds the global administrate permission without any
    assignment and `viewer` the global view permission.
    """

    party_id: PartyID
    a: TournamentID
    b: TournamentID
    c: TournamentID
    draft: TournamentID
    a1: TournamentMatchID
    c1: TournamentMatchID
    users: dict[str, object]


def _tournament(party_id, name, status=TournamentStatus.ONGOING):
    result = tournament_service.create_tournament(
        party_id,
        name,
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=status,
        max_players=16,
    )
    tournament, _event = result.unwrap()
    return tournament.id


def _duel(tournament_id, player_a, player_b) -> TournamentMatchID:
    now = repo.get_operation_time()
    match_id = TournamentMatchID(uuid7())
    db.session.add(
        DbTournamentMatch(
            match_id, tournament_id, now, match_order=1, round=1, phase=1
        )
    )
    db.session.flush()
    for user in (player_a, player_b):
        participant = DbTournamentParticipant(
            TournamentParticipantID(uuid7()), user.id, tournament_id, now
        )
        db.session.add(participant)
        db.session.flush()
        db.session.add(
            DbTournamentMatchToContestant(
                TournamentMatchToContestantID(uuid7()),
                match_id,
                now,
                participant_id=participant.id,
            )
        )
    db.session.commit()
    return match_id


@pytest.fixture(scope='module')
def other_party(make_party, brand):
    party_id = PartyID(f'f03r-{uuid4().hex[:12]}')
    return make_party(brand, party_id, f'F03 return links {party_id}')


@pytest.fixture(scope='module')
def world(party, other_party, make_user, make_admin) -> World:
    suffix = uuid4().hex[:8]
    orga, other_orga, abroad, participant, opponent, outsider = [
        make_user(f'f03r{label}{suffix}') for label in 'oxzqpk'
    ]
    power = make_admin(
        {'lan_tournament.administrate'}, screen_name=f'f03rpower{suffix}'
    )
    viewer = make_admin(
        {'lan_tournament.view'}, screen_name=f'f03rview{suffix}'
    )
    users = {
        'orga': orga,
        'other_orga': other_orga,
        'abroad': abroad,
        'participant': participant,
        'outsider': outsider,
        'power': power,
        'viewer': viewer,
    }
    for user in (*users.values(), opponent):
        log_in_user(user.id)

    a = _tournament(party.id, f'Return Links A {suffix}')
    b = _tournament(party.id, f'Return Links B {suffix}')
    c = _tournament(other_party.id, f'Return Links C {suffix}')
    draft = _tournament(
        party.id, f'Return Links D {suffix}', TournamentStatus.DRAFT
    )
    a1 = _duel(a, participant, opponent)
    c1 = _duel(c, participant, opponent)

    for tournament_id, user in ((a, orga), (b, other_orga), (c, abroad)):
        orgas.assign_orga(tournament_id, user.id, user.id).unwrap()
    db.session.commit()

    return World(
        party_id=party.id,
        a=a,
        b=b,
        c=c,
        draft=draft,
        a1=a1,
        c1=c1,
        users=users,
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
def totalverplant_site(party) -> Iterator[Site]:
    # Its `template_overrides` directory decides the layout of every page.
    # The id exists once per database, so a module that found the site
    # of another one binds it to its own party and gives it back.
    previous = site_service.find_site(TOTALVERPLANT)
    if previous is None:
        site = create_site(
            TOTALVERPLANT,
            party.brand_id,
            server_name='totalverplant-36.acmecon.test',
            party_id=party.id,
        )
    else:
        site = _bound_to(previous, party.id)

    yield site

    if previous is None:
        site_service.delete_site(TOTALVERPLANT)
    else:
        _bound_to(previous, previous.party_id)


@pytest.fixture(scope='module')
def totalverplant_app(database, make_site_app, totalverplant_site):
    app = make_site_app(totalverplant_site.server_name, totalverplant_site.id)
    with app.app_context():
        return app


@pytest.fixture(scope='module', params=['generic', 'totalverplant'])
def app(request, site_app, totalverplant_app):
    return {'generic': site_app, 'totalverplant': totalverplant_app}[
        request.param
    ]


@pytest.fixture(autouse=True)
def _leave_no_transaction():
    yield
    db.session.rollback()


# -------------------------------------------------------------------- #
# helpers


class _BackLinks(HTMLParser):
    """Collect the back links of a page: `p.ltd-back` with its parts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[dict[str, str | None]] = []
        self._current: dict[str, str | None] | None = None
        self._part: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'p' and 'ltd-back' in (attributes.get('class') or '').split():
            self._current = {'href': None, 'label': '', 'context': ''}
            self.found.append(self._current)
        elif self._current is not None and tag == 'a':
            self._current['href'] = attributes.get('href')
            self._part = 'label'
        elif self._current is not None and tag == 'span':
            self._part = 'context'

    def handle_endtag(self, tag):
        if tag == 'p':
            self._current = None
        if tag in ('a', 'span', 'p'):
            self._part = None

    def handle_data(self, data):
        if self._current is not None and self._part is not None:
            self._current[self._part] += data  # type: ignore[operator]


def _links(response) -> list[dict[str, str | None]]:
    parser = _BackLinks()
    parser.feed(response.get_data(as_text=True))
    parser.close()
    return parser.found


def _path(world, page) -> str:
    return {
        'view': f'{BASE}/{world.a}',
        'view_match': f'{BASE}/matches/{world.a1}',
    }[page]


def _anchor(world, page) -> str:
    return {'view': '', 'view_match': f'lt-row-{world.a1}'}[page]


def _get(app, path, *, user=None, ret=None, headers=ENGLISH):
    url = path if ret is None else f'{path}?{urlencode({"return": ret})}'
    with http_client(app, user_id=None if user is None else user.id) as client:
        return client.get(url, headers=headers)


def _viewer(world, who):
    return None if who == 'anonymous' else world.users[who]


def _one_link(response) -> dict[str, str | None]:
    assert response.status_code == 200
    (link,) = _links(response)
    return link


def _assert_list_link(link, world, page, expected_query) -> None:
    parts = urlsplit(link['href'])
    assert (parts.scheme, parts.netloc) == ('', '')
    assert parts.path == LIST_PATH
    assert dict(parse_qsl(parts.query, strict_parsing=bool(parts.query))) == (
        expected_query
    )
    assert parts.fragment == _anchor(world, page)


# -------------------------------------------------------------------- #
# the link is rebuilt from the validated list context


# fmt: off
VALID_RETURNS = {
    'page_two_of_all': (
        lambda w: 'view=all&state=all&sort=tournament&page=2',
        lambda w: {'view': 'all', 'sort': 'tournament', 'page': '2'},
    ),
    'one_filter': (
        lambda w: 'state=tier-red',
        lambda w: {'state': 'tier-red'},
    ),
    'default_list_written_out': (
        lambda w: 'view=due&state=all&sort=urgency&page=1',
        lambda w: {},
    ),
    'a_tournament_filter': (
        lambda w: f'tournament={w.b}&sort=wait',
        lambda w: {'tournament': str(w.b), 'sort': 'wait'},
    ),
    'upper_case_id_is_rebuilt_canonically': (
        lambda w: f'tournament={str(w.b).upper()}',
        lambda w: {'tournament': str(w.b)},
    ),
}
# fmt: on


@pytest.mark.parametrize('case', VALID_RETURNS)
@pytest.mark.parametrize('page', PAGES)
def test_site_back_link_rebuilds_validated_list_url(app, world, page, case):
    raw, expected = VALID_RETURNS[case]

    response = _get(
        app, _path(world, page), user=world.users['orga'], ret=raw(world)
    )

    link = _one_link(response)
    _assert_list_link(link, world, page, expected(world))
    assert link['label'] == BACK_LABEL
    assert link['context'].startswith('(')
    assert link['context'].endswith(')')


@pytest.mark.parametrize('page', PAGES)
def test_site_back_link_carries_the_context_of_the_list(app, world, page):
    response = _get(
        app,
        _path(world, page),
        user=world.users['orga'],
        ret='view=all&sort=tournament&page=3',
    )

    assert _one_link(response)['context'] == (
        '(All matches · Tournament · Page 3)'
    )


@pytest.mark.parametrize('page', PAGES)
def test_site_back_link_is_rendered_once(app, world, page):
    response = _get(
        app, _path(world, page), user=world.users['orga'], ret='page=2'
    )

    html = response.get_data(as_text=True)
    assert html.count('class="ltd-back"') == 1
    assert html.count(BACK_LABEL) == 1


# -------------------------------------------------------------------- #
# authority: the same server check as the index button


@pytest.mark.parametrize('who', HIDDEN_FROM)
@pytest.mark.parametrize('page', PAGES)
def test_site_back_link_hidden_without_dashboard_authority(
    app, world, page, who
):
    path = _path(world, page)
    viewer = _viewer(world, who)

    with_return = _get(app, path, user=viewer, ret='view=all&page=2')

    # Page permissions are unchanged: the public page answers as ever.
    assert with_return.status_code == 200
    assert _links(with_return) == []
    assert BACK_LABEL not in with_return.get_data(as_text=True)
    assert 'Location' not in with_return.headers


@pytest.mark.parametrize('who', HIDDEN_FROM)
@pytest.mark.parametrize('page', PAGES)
def test_site_page_without_authority_does_not_depend_on_return(
    app, world, page, who
):
    path = _path(world, page)
    viewer = _viewer(world, who)

    plain = _get(app, path, user=viewer)
    hostile = _get(
        app, path, user=viewer, ret=f'view=all&{CANARY}=https://x.test/'
    )

    assert hostile.status_code == plain.status_code == 200
    assert hostile.get_data() == plain.get_data()


@pytest.mark.parametrize('who', SHOWN_TO)
@pytest.mark.parametrize('page', PAGES)
def test_site_back_link_is_shown_to_an_orga_of_the_party(app, world, page, who):
    # The authority is the party's: an orga of tournament B sees the link
    # on tournament A as well, as the index button does.
    response = _get(
        app, _path(world, page), user=world.users[who], ret='page=2'
    )

    _assert_list_link(_one_link(response), world, page, {'page': '2'})


def test_site_back_link_follows_assignment_and_revocation(
    app, world, make_user
):
    late = make_user(f'f03rlate{uuid4().hex[:8]}')
    log_in_user(late.id)
    path = _path(world, 'view_match')

    assert _links(_get(app, path, user=late, ret='page=2')) == []

    orgas.assign_orga(world.a, late.id, late.id).unwrap()
    db.session.commit()
    assert len(_links(_get(app, path, user=late, ret='page=2'))) == 1

    orgas.revoke_orga(world.a, late.id, late.id).unwrap()
    db.session.commit()
    assert _links(_get(app, path, user=late, ret='page=2')) == []


@pytest.mark.parametrize('page', PAGES)
def test_site_back_link_needs_an_assignment_in_the_current_party(
    app, world, page
):
    # `abroad` is an orga, but of another party; `power` may administrate
    # every tournament but is assigned to none.
    for who in ('abroad', 'power'):
        response = _get(
            app, _path(world, page), user=world.users[who], ret='page=2'
        )

        assert response.status_code == 200
        assert _links(response) == []


# -------------------------------------------------------------------- #
# an invalid value falls back to the default list


# fmt: off
INVALID_RETURNS = {
    'unknown_view':          'view=bogus',
    'unknown_state':         'state=tier-purple',
    'unknown_sort':          'sort=random',
    'page_zero':             'page=0',
    'page_negative':         'page=-1',
    'page_text':             'page=two',
    'page_above_the_limit':  'page=1000001',
    'duplicate_key':         'sort=urgency&sort=wait',
    'unknown_key':           'view=all&extra=1',
    'tournament_not_a_uuid': 'tournament=not-a-uuid',
    'valid_part_and_bad':    'view=all&page=0',
    'absolute_url':          'https://elsewhere.test/orga-dashboard',
    'scheme_relative_url':   '//elsewhere.test/orga-dashboard',
    'path':                  '/lan-tournaments/orga-dashboard?view=all',
    'path_with_dots':        '../../admin',
    'pair_without_equals':   'view',
    'markup':                '"><script>alert(1)</script>',
    'too_long':              'view=all&' + 'x=1&' * 400,
}
# fmt: on


@pytest.mark.parametrize('case', INVALID_RETURNS)
@pytest.mark.parametrize('page', PAGES)
def test_site_invalid_return_falls_back_to_default_list(app, world, page, case):
    response = _get(
        app,
        _path(world, page),
        user=world.users['orga'],
        ret=INVALID_RETURNS[case],
    )

    _assert_list_link(_one_link(response), world, page, {})


# fmt: off
SCOPE_RETURNS = {
    'scope_all':          ('scope=all&view=all',    {'view': 'all'}),
    'scope_assigned':     ('scope=assigned',        {}),
    'scope_nonsense':     ('scope=nonsense&page=2', {'page': '2'}),
    'scope_empty':        ('scope=&sort=wait',      {'sort': 'wait'}),
}
# fmt: on


@pytest.mark.parametrize('case', SCOPE_RETURNS)
@pytest.mark.parametrize('page', PAGES)
def test_site_return_scope_value_is_ignored(app, world, page, case):
    raw, expected = SCOPE_RETURNS[case]

    response = _get(app, _path(world, page), user=world.users['orga'], ret=raw)

    link = _one_link(response)
    _assert_list_link(link, world, page, expected)
    assert 'scope' not in link['href']


@pytest.mark.parametrize('ret', [None, ''], ids=['absent', 'empty'])
@pytest.mark.parametrize('who', SHOWN_TO)
@pytest.mark.parametrize('page', PAGES)
def test_site_no_return_means_no_link(app, world, page, who, ret):
    response = _get(app, _path(world, page), user=world.users[who], ret=ret)

    assert response.status_code == 200
    assert _links(response) == []
    assert BACK_LABEL not in response.get_data(as_text=True)


# -------------------------------------------------------------------- #
# input never reaches the page


# fmt: off
HOSTILE_RETURNS = {
    'absolute_url':   f'https://{CANARY}.evil.test/phish?x=1',
    'scheme_relative': f'//{CANARY}.evil.test/x',
    'path':           f'/{CANARY}/../admin',
    'javascript_url': f'javascript:alert("{CANARY}")',
    'script_tag':     f'"><script>{CANARY}()</script>',
    'attribute_break': f'view=all" onmouseover="{CANARY}',
    'unknown_key':    f'view=all&{CANARY}=1',
    'unknown_value':  f'view={CANARY}',
    'bad_tournament': f'tournament=<img src=x onerror={CANARY}>',
    'bad_page':       f"page='\"><svg onload={CANARY}>",
    'crlf':           f'page=1\r\nSet-Cookie: {CANARY}=1\r\nLocation: //x.test',
    'encoded_markup': f'view=%3Cscript%3E{CANARY}%3C%2Fscript%3E',
    'long':           f'view=all&{CANARY}=' + 'y' * 3000,
}
# fmt: on


@pytest.mark.parametrize('case', HOSTILE_RETURNS)
@pytest.mark.parametrize('who', ['orga', 'anonymous'])
@pytest.mark.parametrize('page', PAGES)
def test_site_return_never_reflects_raw_input(app, world, page, who, case):
    response = _get(
        app,
        _path(world, page),
        user=_viewer(world, who),
        ret=HOSTILE_RETURNS[case],
    )

    assert response.status_code == 200
    assert 'Location' not in response.headers
    assert CANARY.encode() not in response.get_data()
    assert CANARY not in str(response.headers)

    links = _links(response)
    if who == 'anonymous':
        assert links == []
    else:
        (link,) = links
        _assert_list_link(link, world, page, {})


@pytest.mark.parametrize('page', PAGES)
def test_site_return_reflects_only_validated_values(app, world, page):
    # The one thing a link may carry is what the parser accepted, rebuilt:
    # the spelling of the request (upper case, leading zeros) is not kept.
    response = _get(
        app,
        _path(world, page),
        user=world.users['orga'],
        ret=f'tournament={str(world.b).upper()}&sort=wait&page=0007',
    )

    _assert_list_link(
        _one_link(response),
        world,
        page,
        {'tournament': str(world.b), 'sort': 'wait', 'page': '7'},
    )
    html = response.get_data(as_text=True)
    assert str(world.b).upper() not in html
    assert 'page=0007' not in html


# -------------------------------------------------------------------- #
# page access stays as it was


@pytest.mark.parametrize('who', ['anonymous', 'orga', 'power'])
def test_site_return_does_not_open_a_page_the_site_hides(app, world, who):
    viewer = _viewer(world, who)
    ret = 'view=all'

    # A draft, a tournament of another party, a match of another party and
    # an unknown match answer 404 exactly as without `return`.
    paths = (
        f'{BASE}/{world.draft}',
        f'{BASE}/{world.c}',
        f'{BASE}/matches/{world.c1}',
        f'{BASE}/matches/{uuid4()}',
    )
    for path in paths:
        plain = _get(app, path, user=viewer)
        with_return = _get(app, path, user=viewer, ret=ret)

        assert plain.status_code == with_return.status_code == 404
        assert _links(with_return) == []


def _source_of(app, name) -> Path:
    _, filename, _ = app.jinja_env.loader.get_source(app.jinja_env, name)
    return Path(filename).resolve()


@pytest.mark.parametrize(
    'name',
    ['site/lan_tournament/view.html', 'site/lan_tournament/view_match.html'],
)
def test_site_pages_have_no_override_on_either_site(
    site_app, totalverplant_app, name
):
    # The tests above render the blueprint's own page on both sites. A site
    # override of either page would have to carry the link block itself.
    expected = Path(site_views.__file__).resolve().parent / 'templates' / name
    assert expected.is_file()

    assert _source_of(site_app, name) == expected
    assert _source_of(totalverplant_app, name) == expected


def test_the_second_site_renders_with_its_own_layout(
    site_app, totalverplant_app
):
    overrides = (
        REPO_ROOT / 'sites' / TOTALVERPLANT / 'template_overrides' / 'layout'
    )

    assert _source_of(totalverplant_app, 'layout/base.html') == (
        overrides / 'base.html'
    )
    assert _source_of(site_app, 'layout/base.html') != overrides / 'base.html'
