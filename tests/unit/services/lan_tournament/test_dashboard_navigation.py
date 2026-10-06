"""
tests.unit.services.lan_tournament.test_dashboard_navigation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pathlib
import re
from types import SimpleNamespace

from babel.messages.pofile import read_po
from flask import Flask, g
from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.dashboard_view_helpers import (
    DASHBOARD_ENDPOINTS,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    group_tournaments_by_category,
)
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.util.navigation import Navigation


_MODULE = pathlib.Path('byceps/services/lan_tournament/blueprints')
_COMMON = pathlib.Path('byceps/services/core/blueprints/common/templates')
_ADMIN_LAYOUT = _MODULE / 'admin/templates/layout/admin/lan_tournament.html'
_ADMIN_DASHBOARD_PAGE = (
    _MODULE / 'admin/templates/admin/lan_tournament/dashboard.html'
)
_GENERIC_INDEX = _MODULE / 'site/templates/site/lan_tournament/index.html'
_BOTE_INDEX = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament/index.html'
)
_CORE = pathlib.Path('byceps/services/core/blueprints')
_ADMIN_MACROS = _CORE / 'admin/templates/macros/admin.html'
_MISC_MACROS = _CORE / 'common/templates/macros/misc.html'
_ICON_MACROS = _CORE / 'common/templates/macros/icons.html'
_CATALOGUE = pathlib.Path('byceps/translations/de/LC_MESSAGES/messages.po')

_VIEW = 'lan_tournament.view'
_REQUEST_VIEW = 'lan_tournament.request_view'
_MAINTAIN = 'lan_tournament.maintain'
_ADMINISTRATE = 'lan_tournament.administrate'
_EVERYTHING = {_VIEW, _REQUEST_VIEW, _MAINTAIN, _ADMINISTRATE}

_ADMIN_DASHBOARD = DASHBOARD_ENDPOINTS['admin']['list']
_SITE_DASHBOARD = DASHBOARD_ENDPOINTS['site']['list']

_PARTY_ID = 'pixelnacht-36'


# ------------------------------------------------------------ admin layout


@pytest.fixture(scope='module')
def app() -> Flask:
    app = Flask(__name__)
    app.config['TESTING'] = True
    return app


@pytest.fixture(scope='module')
def admin_layout():
    """Load the real layout file; only its parent layout is a stub."""
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'layout': _ADMIN_LAYOUT.read_text(),
                'layout/admin/base.html': (
                    '{% block before_body %}{% endblock %}'
                ),
                'macros/admin.html': _ADMIN_MACROS.read_text(),
                'macros/misc.html': _MISC_MACROS.read_text(),
                'macros/icons.html': _ICON_MACROS.read_text(),
            }
        ),
    )
    return env.get_template('layout')


def _render_tabs(app, template, *, permissions, current_tab='overview'):
    """Return `(label, href, is_current)` of each tab the user sees."""
    with app.test_request_context('/'):
        # `Navigation` reads the real Flask `g`.
        g.user = SimpleNamespace(
            has_permission=lambda permission: permission in permissions
        )
        html = template.render(
            _=lambda message, **kw: message % kw if kw else message,
            url_for=lambda endpoint, **kw: f'{endpoint}@{kw["party_id"]}',
            current_page_party=SimpleNamespace(id=_PARTY_ID),
            current_tab=current_tab,
            Navigation=Navigation,
            lan_tournament_pending_request_count=lambda party_id: 0,
            lan_tournament_has_orga_assignments=lambda party_id: False,
        )

    return [
        (' '.join(label.split()), href, bool(current))
        for href, current, label in re.findall(
            r'<a href="([^"]*)" class="main-tab( main-tab--current)?">'
            r'(.*?)</a>',
            html,
            re.S,
        )
    ]


def _labels(tabs):
    return [label for label, _, _ in tabs]


# fmt: off
_ADMIN_TAB_CASES = [
    # permissions,                 tab labels the user sees
    ({_ADMINISTRATE},              ['Dashboard']),
    ({_VIEW, _ADMINISTRATE},       ['Overview', 'Dashboard', 'Tournaments']),
    ({_VIEW},                      ['Overview', 'Tournaments']),
    ({_VIEW, _REQUEST_VIEW, _MAINTAIN},
                                   ['Overview', 'Tournaments',
                                    'Tournament requests', 'Maintenance']),
    ({_REQUEST_VIEW},              ['Tournament requests']),
    ({_MAINTAIN},                  ['Maintenance']),
    (set(),                        []),
]
# fmt: on


@pytest.mark.parametrize(('permissions', 'expected'), _ADMIN_TAB_CASES)
def test_admin_dashboard_tab_is_administrate_only(
    app, admin_layout, permissions, expected
):
    tabs = _render_tabs(app, admin_layout, permissions=permissions)

    assert _labels(tabs) == expected


def test_admin_dashboard_tab_follows_overview(app, admin_layout):
    tabs = _render_tabs(app, admin_layout, permissions=_EVERYTHING)

    assert _labels(tabs) == [
        'Overview',
        'Dashboard',
        'Tournaments',
        'Tournament requests',
        'Maintenance',
    ]
    dashboard = tabs[1]
    assert dashboard[1] == f'{_ADMIN_DASHBOARD}@{_PARTY_ID}'
    assert [href for _, href, _ in tabs].count(dashboard[1]) == 1


# fmt: off
_CURRENT_TAB_CASES = [
    # current_tab,    marked tab
    ('overview',      'Overview'),
    ('dashboard',     'Dashboard'),
    ('tournaments',   'Tournaments'),
    ('requests',      'Tournament requests'),
    ('maintenance',   'Maintenance'),
]
# fmt: on


@pytest.mark.parametrize(('current_tab', 'marked'), _CURRENT_TAB_CASES)
def test_admin_tabs_mark_exactly_the_current_tab(
    app, admin_layout, current_tab, marked
):
    tabs = _render_tabs(
        app, admin_layout, permissions=_EVERYTHING, current_tab=current_tab
    )

    assert [label for label, _, current in tabs if current] == [marked]


def test_the_dashboard_page_marks_the_tab_the_layout_offers():
    source = _ADMIN_DASHBOARD_PAGE.read_text()

    assert re.search(r"{%-?\s*set current_tab\s*=\s*'dashboard'\s*-?%}", source)


# ------------------------------------------------------------ site indexes


@pytest.fixture(scope='module', params=['generic', 'bote'])
def index_template(request):
    """Load a real index template; only its parent layout is a stub."""
    path, parent = {
        'generic': (_GENERIC_INDEX, 'layout/site/lan_tournament.html'),
        'bote': (_BOTE_INDEX, 'layout/base.html'),
    }[request.param]
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'index': path.read_text(),
                parent: '{% block body %}{% endblock %}',
                'macros/misc.html': _MISC_MACROS.read_text(),
                'macros/icons.html': _ICON_MACROS.read_text(),
                **{name: path.read_text() for name, path in _PARTIALS.items()},
            }
        ),
    )
    env.globals['_'] = lambda message, **kw: message % kw if kw else message
    env.globals['url_for'] = _url_for
    env.globals['render_icon'] = lambda *a, **kw: ''
    env.filters['dateformat'] = lambda value, *a, **kw: ''
    env.filters['timeformat'] = lambda value, *a, **kw: ''
    return env.get_template('index')


_PARTIALS = {
    'site/lan_tournament/_overview_nav.html': _MODULE
    / 'site/templates/site/lan_tournament/_overview_nav.html',
    'lan_tournament/_category_empty.html': _COMMON
    / 'lan_tournament/_category_empty.html',
    'lan_tournament/_category_filter.html': _COMMON
    / 'lan_tournament/_category_filter.html',
}


def _url_for(endpoint, **values):
    """Resolve a blueprint-relative endpoint like Flask does on the site."""
    if endpoint == 'static':
        return f'/static/{values["filename"]}'

    return f'lan_tournament{endpoint}' if endpoint.startswith('.') else endpoint


def _site_context(monkeypatch, *, answer):
    """Return what the site blueprint gives its templates, plus a call log.

    The authority is the real lazy object of the context processor. Only
    the check behind it is replaced, so the log tells how often (and
    whether) a page asked for it.
    """
    asked = []

    def may_view_orga_dashboard():
        asked.append(answer)
        return answer

    monkeypatch.setattr(
        site_views, 'may_view_orga_dashboard', may_view_orga_dashboard
    )

    context = {}
    # The overview processor reads `request.endpoint`, which is `None` here.
    with Flask(__name__).test_request_context('/'):
        for processor in site_views.blueprint.template_context_processors[None]:
            context.update(processor())

    # Flask's default processor offers its own `g`; the test supplies one.
    context.pop('g', None)
    context.pop('request', None)

    return context, asked


def _render_index(template, *, authenticated, populated=False, **context):
    tournaments = []
    if populated:
        tournaments = [
            SimpleNamespace(
                id='t-1',
                name='Cup',
                category=TournamentCategory.MAIN,
                position=0,
                game=None,
                image_url=None,
                tournament_status=None,
                contestant_type=None,
                max_players=None,
                max_teams=None,
                start_time=None,
            )
        ]

    context.setdefault('has_orga_assignments', False)
    return template.render(
        page_title='Tournaments',
        tournaments=tournaments,
        tournament_groups=group_tournaments_by_category(tournaments),
        categories=[],
        category_filter=None,
        category_filter_args={},
        total_count=len(tournaments),
        participant_counts={},
        team_counts={},
        g=SimpleNamespace(user=SimpleNamespace(authenticated=authenticated)),
        **context,
    )


def _links_to(html, endpoint):
    return re.findall(
        rf'<a [^>]*href="{re.escape(endpoint)}"[^>]*>(.*?)</a>', html, re.S
    )


@pytest.mark.parametrize('populated', [False, True], ids=['empty', 'populated'])
def test_scoped_dashboard_link_is_visible_on_generic_and_bote(
    index_template, monkeypatch, populated
):
    context, asked = _site_context(monkeypatch, answer=True)

    html = _render_index(
        index_template, authenticated=True, populated=populated, **context
    )

    assert _links_to(html, _SITE_DASHBOARD) == ['Orga dashboard']
    assert asked == [True]
    assert 'href="lan_tournament.propose_form"' in html
    assert 'href="lan_tournament.my_requests"' in html


# fmt: off
_NO_LINK_CASES = [
    # id,                      authenticated, authority
    ('anonymous-no-key',       False,         None),
    ('anonymous-denied',       False,         False),
    ('anonymous-claimed',      False,         True),
    ('non-orga-no-key',        True,          None),
    ('non-orga-denied',        True,          False),
]
# fmt: on


@pytest.mark.parametrize(
    ('authenticated', 'authority'),
    [case[1:] for case in _NO_LINK_CASES],
    ids=[case[0] for case in _NO_LINK_CASES],
)
def test_anonymous_and_non_orga_have_no_dashboard_link(
    index_template, monkeypatch, authenticated, authority
):
    context, asked = {}, []
    if authority is not None:
        context, asked = _site_context(monkeypatch, answer=authority)

    html = _render_index(index_template, authenticated=authenticated, **context)

    assert _links_to(html, _SITE_DASHBOARD) == []
    assert 'Orga dashboard' not in html
    assert 'orga_dashboard' not in html
    assert ('href="lan_tournament.propose_form"' in html) is authenticated
    if not authenticated:
        # An anonymous visitor costs no authority check at all.
        assert asked == []


# --------------------------------------------------------------- the copy


def test_the_navigation_labels_have_german():
    with _CATALOGUE.open('rb') as file:
        catalogue = read_po(file)

    assert catalogue.get('Dashboard').string == 'Dashboard'
    assert catalogue.get('Orga dashboard').string == 'Orga-Dashboard'
