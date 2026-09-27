"""
tests.unit.services.lan_tournament.test_tournament_request_index_nav
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

workspace-vxrc.3, issue l: the tournament-request flow
(`/lan-tournaments/requests/propose`, `/lan-tournaments/requests`) was
only ever linked from the two request templates themselves -- a
logged-in user landing on the site's own tournament index page had no
way to discover it. This renders the lan_tournament site index page
(base template + the totalverplant-36 override) under
`StrictUndefined` and proves the new "Propose a tournament" / "My
tournament requests" nav links show only for an authenticated user
and point at the right endpoints, on both surfaces.

Follows the sibling `test_tournament_request_render_site.py` /
`_render_bote.py` pattern: each template is reduced to just its
`{% block body %}` content, with the macros its top-of-file
`{% from %}` imports would otherwise have pulled in supplied as
environment globals instead.
"""

import pathlib
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
from markupsafe import Markup
import pytest


_BASE_INDEX_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/index.html'
)
_BOTE_INDEX_TEMPLATE = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament/index.html'
)


def _snippet(path: pathlib.Path) -> str:
    """Return just the file's `{% block body %}` content."""
    src = path.read_text()
    start = src.index('{% block body %}') + len('{% block body %}')
    end = src.rindex('{%- endblock %}')
    return src[start:end]


def _make_env(templates: dict[str, str]) -> Environment:
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(templates),
    )
    e.globals['_'] = lambda s, **kw: s % kw if kw else s
    e.globals['url_for'] = lambda endpoint, **k: (
        '/static/' + k['filename']
        if endpoint == 'static'
        else '/' + endpoint.lstrip('.')
    )
    e.globals['render_icon'] = lambda *a, **k: ''
    e.globals['render_tag'] = lambda label, **k: Markup(  # noqa: S704
        f'<span class="tag {k.get("class", "")}">{label}</span>'
    )
    e.filters['dateformat'] = lambda dt, *a, **k: (
        dt.strftime('%Y-%m-%d') if dt else ''
    )
    e.filters['timeformat'] = lambda dt, *a, **k: (
        dt.strftime('%H:%M') if dt else ''
    )
    return e


@pytest.fixture(scope='module')
def base_env() -> Environment:
    return _make_env({'index': _snippet(_BASE_INDEX_TEMPLATE)})


@pytest.fixture(scope='module')
def bote_env() -> Environment:
    return _make_env({'index': _snippet(_BOTE_INDEX_TEMPLATE)})


def _render_index(env, *, authenticated: bool, **ctx):
    base_ctx = {
        'page_title': 'Tournaments',
        'tournaments': [],
        'participant_counts': {},
        'team_counts': {},
        'g': SimpleNamespace(user=SimpleNamespace(authenticated=authenticated)),
    }
    base_ctx.update(ctx)
    return env.get_template('index').render(**base_ctx)


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_index_shows_request_nav_links_when_authenticated(env_name, request):
    env = request.getfixturevalue(env_name)

    html = _render_index(env, authenticated=True)

    assert 'href="/propose_form"' in html
    assert 'href="/my_requests"' in html
    assert 'Propose a tournament' in html
    assert 'My tournament requests' in html


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_index_hides_request_nav_links_when_anonymous(env_name, request):
    env = request.getfixturevalue(env_name)

    html = _render_index(env, authenticated=False)

    assert 'href="/propose_form"' not in html
    assert 'href="/my_requests"' not in html
    assert 'Propose a tournament' not in html
    assert 'My tournament requests' not in html


@pytest.mark.parametrize('env_name', ['base_env', 'bote_env'])
def test_index_nav_links_render_alongside_a_populated_tournament_list(
    env_name, request
):
    """The nav links sit outside the tournaments loop/empty-state
    branch, so they must still show when tournaments exist."""
    env = request.getfixturevalue(env_name)
    tournament = SimpleNamespace(
        id='t-1',
        name='Cup',
        game=None,
        image_url=None,
        tournament_status=None,
        contestant_type=None,
        max_players=None,
        max_teams=None,
        start_time=None,
    )

    html = _render_index(env, authenticated=True, tournaments=[tournament])

    assert 'href="/propose_form"' in html
    assert 'href="/my_requests"' in html
    assert 'Cup' in html
