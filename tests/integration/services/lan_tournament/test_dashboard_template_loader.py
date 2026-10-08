from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from flask import g
from jinja2 import StrictUndefined, TemplateNotFound
import pytest

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    tournament_orga_service as orgas,
    tournament_service,
)
from byceps.services.lan_tournament.blueprints.admin import (
    views as admin_views,
)
from byceps.services.lan_tournament.blueprints.common import (
    views as common_views,
)
from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.dashboard_view_helpers import (
    build_dashboard_context,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisodeID,
    TrafficTier,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardMatchLocation,
    DashboardNonActionableCounts,
    DashboardPage,
    DashboardQuery,
    DashboardRow,
    DashboardRowState,
    DashboardSettings,
    DashboardTierCounts,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID
from byceps.util.uuid import generate_uuid7

from tests.helpers import create_site, log_in_user


TEMPLATE = 'common/lan_tournament/_dashboard_rows.html'
COMMON_DIR = Path(common_views.__file__).resolve().parent
ADMIN_BASE_URL = 'http://admin.acmecon.test/'
SITE_BASE_URL = 'http://www.acmecon.test/'

# The parent blueprint each real app registers, by app.
PARENTS = {
    'admin': (admin_views.blueprint, 'lan_tournament_admin'),
    'site': (site_views.blueprint, 'lan_tournament'),
}


@pytest.fixture(scope='module')
def apps(admin_app, site_app):
    return {'admin': admin_app, 'site': site_app}


def _context(surface: str) -> dict:
    """Build the context of one row the way the routes build it."""
    now = datetime(2026, 10, 8, 12, 0)
    row = DashboardRow(
        match_id=TournamentMatchID(generate_uuid7()),
        tournament_id=TournamentID(generate_uuid7()),
        tournament_name='Kupfer-Cup',
        game='Arena Five',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        location=DashboardMatchLocation(
            phase=1, bracket=Bracket.WINNERS, round=0, match_order=0
        ),
        contestant_names=('Kupferfüchse', 'Nachtbus'),
        orga_names=('Mara',),
        state=DashboardRowState.DUE,
        tier=TrafficTier.YELLOW,
        created_at=now,
        occupied_since=now,
        episode_opened_at=now,
        last_changed_at=now,
        total_active_wait_us=20 * 60_000_000,
        alert_interval_us=20 * 60_000_000,
        readiness_available=True,
        review_available=True,
        episode_id=MatchDueEpisodeID(generate_uuid7()),
    )
    page = DashboardPage(
        rows=(row,),
        as_of=now,
        total_count=1,
        page=1,
        per_page=50,
        total_pages=1,
        tier_counts=DashboardTierCounts(),
        non_actionable_counts=DashboardNonActionableCounts(),
    )
    settings = DashboardSettings(
        yellow_minutes=15,
        red_minutes=45,
        poll_seconds=30,
        page_size=50,
        threshold_source='deployment',
    )
    return build_dashboard_context(
        page,
        DashboardQuery(per_page=50),
        settings,
        surface=surface,  # type: ignore[arg-type]
        csrf_token='loader-token',
        party_id=PartyID('acmecon-2014') if surface == 'admin' else None,
    )


def _render_in(app, base_url, surface) -> str:
    with app.test_request_context(base_url):
        g.user = CurrentUser.create_anonymous(None)
        g.locales = []
        template = app.jinja_env.get_template(TEMPLATE)
        return str(
            template.module.render_rows(_context(surface), 'count-heading')
        )


def _source_path(app) -> Path:
    _, filename, _ = app.jinja_env.loader.get_source(app.jinja_env, TEMPLATE)
    return Path(filename).resolve()


def test_common_dashboard_templates_load_in_both_real_apps(apps):
    expected = COMMON_DIR / 'templates' / TEMPLATE
    assert expected.is_file()

    for surface, (_, parent_name) in PARENTS.items():
        app = apps[surface]
        name = f'{parent_name}.lan_tournament_common'

        # The child is registered below its own parent, through the loader
        # of the real app: the same file is found for both.
        assert app.blueprints[name] is common_views.blueprint
        assert _source_path(app) == expected
        template = app.jinja_env.get_template(TEMPLATE)
        assert {'render_rows', 'render_row'} <= set(template.module.__dict__)

    admin_html = _render_in(apps['admin'], ADMIN_BASE_URL, 'admin')
    site_html = _render_in(apps['site'], SITE_BASE_URL, 'site')

    # Real apps run under StrictUndefined, so a key the context lacks fails.
    for html, route in [
        (admin_html, '/lan-tournaments/for_party/acmecon-2014/dashboard/'),
        (site_html, '/lan-tournaments/orga-dashboard/'),
    ]:
        assert 'class="ltd-rows"' in html
        assert 'class="ltd-row"' in html
        assert 'Kupferfüchse' in html
        assert f'action="{route}matches/' in html
        assert 'name="csrf_token" value="loader-token"' in html


def test_the_real_apps_run_the_row_template_under_strict_undefined(apps):
    for app in apps.values():
        assert app.jinja_env.undefined is StrictUndefined


def test_template_only_blueprint_is_multi_app_safe(
    apps, make_admin_app, make_site_app, site
):
    child = common_views.blueprint

    # One module-level object, registered once below each parent.
    for parent, _ in PARENTS.values():
        children = [bp for bp, _options in parent._blueprints]
        assert children == [child]
        assert [o.get('url_prefix') for _, o in parent._blueprints] == [None]

    # It has no routes of its own beyond the inert static one, and no hooks.
    assert not (COMMON_DIR / 'static').exists()
    for surface, (_, parent_name) in PARENTS.items():
        prefix = f'{parent_name}.lan_tournament_common.'
        endpoints = {
            rule.endpoint
            for rule in apps[surface].url_map.iter_rules()
            if rule.endpoint.startswith(prefix)
        }
        assert endpoints <= {f'{prefix}static'}
    assert child.deferred_functions == []
    for hooks in (
        child.before_request_funcs,
        child.after_request_funcs,
        child.teardown_request_funcs,
        child.url_value_preprocessors,
        child.url_default_functions,
        child.error_handler_spec,
        child.cli.commands,
    ):
        assert not hooks
    # Flask gives every blueprint its own default (request, session, g).
    added = [
        function
        for functions in child.template_context_processors.values()
        for function in functions
        if function.__module__ != 'flask.templating'
    ]
    assert added == []

    # Further apps from the same blueprint objects register it again.
    second_admin = make_admin_app('admin-second.acmecon.test')
    second_site = make_site_app('www-second.acmecon.test', site.id)
    others = {'admin': second_admin, 'site': second_site}
    expected = COMMON_DIR / 'templates' / TEMPLATE
    for surface, (_, parent_name) in PARENTS.items():
        for app in (apps[surface], others[surface]):
            name = f'{parent_name}.lan_tournament_common'
            assert app.blueprints[name] is child
            assert [
                n
                for n in app.blueprints
                if n.endswith('.lan_tournament_common')
            ] == [name]
            assert _source_path(app) == expected

    # The first apps are untouched by the second registration.
    assert 'class="ltd-rows"' in _render_in(
        apps['admin'], ADMIN_BASE_URL, 'admin'
    )
    assert 'class="ltd-rows"' in _render_in(
        others['site'], 'http://www-second.acmecon.test/', 'site'
    )


# -------------------------------------------------------------------- #
# the dashboard wrappers, as the real apps load them

REPO_ROOT = Path(__file__).resolve().parents[4]
BOTE_SITE_ID = SiteID('totalverplant-36')
BOTE_OVERRIDES = REPO_ROOT / 'sites' / BOTE_SITE_ID / 'template_overrides'
SITE_TEMPLATES = Path(site_views.__file__).resolve().parent / 'templates'
PAGE = 'site/lan_tournament/dashboard.html'
PANEL = 'common/lan_tournament/_dashboard_panel.html'
BOTE_STYLE = 'site/lan_tournament/_bote_dashboard_style.html'
DASHBOARD_PATH = '/lan-tournaments/orga-dashboard'


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
            server_name='lt-test-dashboard-bote.test',
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
def orga(party, make_user):
    """An orga of one tournament of the site's party."""
    suffix = uuid4().hex[:8]
    user = make_user(f'f03loader{suffix}')
    log_in_user(user.id)

    result = tournament_service.create_tournament(
        party.id,
        f'Loader Cup {suffix}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.ONGOING,
        max_players=16,
    )
    tournament, _event = result.unwrap()
    orgas.assign_orga(tournament.id, user.id, user.id).unwrap()
    db.session.commit()

    return user


@pytest.fixture(scope='module')
def dashboard_admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'},
        screen_name=f'f03loaderadmin{uuid4().hex[:8]}',
    )
    log_in_user(user.id)
    return user


def _source_path_of(app, name) -> Path:
    _, filename, _ = app.jinja_env.loader.get_source(app.jinja_env, name)
    return Path(filename).resolve()


def _panel_of(html: str) -> str:
    """Return the panel as written: its root to its live region's close."""
    assert html.count('<section class="lt-dashboard') == 1
    end_marker = 'data-live></div>\n</section>'
    assert html.count(end_marker) == 1
    start = html.index('<section class="lt-dashboard')
    return html[start : html.index(end_marker) + len(end_marker)]


def test_bote_override_reuses_common_panel(
    site_app, bote_app, orga, make_client, monkeypatch
):
    common = COMMON_DIR / 'templates'

    # The override directory is chosen by site ID when the app is created:
    # the totalverplant app finds the wrapper and its style partial there,
    # the generic app finds the module's own wrapper and has no partial.
    assert _source_path_of(bote_app, PAGE) == BOTE_OVERRIDES / PAGE
    assert _source_path_of(bote_app, BOTE_STYLE) == BOTE_OVERRIDES / BOTE_STYLE
    assert _source_path_of(site_app, PAGE) == SITE_TEMPLATES / PAGE
    with pytest.raises(TemplateNotFound):
        site_app.jinja_env.get_template(BOTE_STYLE)
    # No override of the panel or the rows: one file serves every surface.
    for app in (site_app, bote_app):
        assert _source_path_of(app, PANEL) == common / PANEL
        assert _source_path_of(app, TEMPLATE) == common / TEMPLATE
    assert not (BOTE_OVERRIDES / PANEL).exists()
    assert not (BOTE_OVERRIDES / TEMPLATE).exists()

    # Both apps render the page for the same viewer and the same context.
    shared: dict = {}
    real_render = site_views.render_template

    def render_with_one_context(template, **context):
        if template == PAGE:
            if 'dashboard' not in shared:
                shared['dashboard'] = _context('site')
            context['dashboard'] = shared['dashboard']
        return real_render(template, **context)

    monkeypatch.setattr(site_views, 'render_template', render_with_one_context)
    generic = make_client(site_app, user_id=orga.id).get(DASHBOARD_PATH)
    bote = make_client(bote_app, user_id=orga.id).get(DASHBOARD_PATH)

    assert generic.status_code == bote.status_code == 200
    generic_html = generic.get_data(as_text=True)
    bote_html = bote.get_data(as_text=True)
    assert 'Kupferfüchse' in bote_html
    # The panel is the canonical one, byte for byte.
    assert _panel_of(bote_html) == _panel_of(generic_html)
    # The page around it is the site's own: its layout, its sheet, its theme.
    assert 'class="bote-page orga-dashboard-page"' in bote_html
    assert 'class="bote-page' not in generic_html
    assert (
        '/static_sites/totalverplant-36/style/totalverplant-36.css' in bote_html
    )
    assert 'html[data-theme="dark"] .orga-dashboard-page' in bote_html
    assert 'orga-dashboard-page' not in generic_html
    # The shared sheet loads before the theme, so the theme wins at equal
    # specificity, and the script comes once.
    assert bote_html.index(
        'style/lan_tournament_dashboard.css'
    ) < bote_html.index('html[data-theme="dark"] .orga-dashboard-page')
    assert bote_html.count('behavior/lan_tournament_dashboard.js') == 1


def test_the_real_wrappers_render_in_both_apps(
    admin_app, site_app, orga, dashboard_admin, party, make_client
):
    # No stand-in for the template call: the real routes build the context
    # and the real layouts of both apps draw the page around the panel.
    admin = make_client(admin_app, user_id=dashboard_admin.id).get(
        f'{ADMIN_BASE_URL}lan-tournaments/for_party/{party.id}/dashboard'
    )
    site = make_client(site_app, user_id=orga.id).get(DASHBOARD_PATH)

    for response, surface in ((admin, 'admin'), (site, 'site')):
        assert response.status_code == 200, surface
        html = response.get_data(as_text=True)
        assert html.count('data-lt-dashboard-root') == 1, surface
        assert f'data-surface="{surface}"' in _panel_of(html)
        assert 'style/lan_tournament_dashboard.css' in html
        assert 'behavior/lan_tournament_dashboard.js' in html
        assert response.headers['Cache-Control'] == 'private, no-store'
    assert 'class="lt-dashboard is-site"' in site.get_data(as_text=True)
    assert 'class="lt-dashboard"' in admin.get_data(as_text=True)
