from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
from flask import Blueprint, Flask, url_for
import flask_babel
from flask_babel import Babel, force_locale
from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)
import pytest

from byceps.services.lan_tournament.dashboard_view_helpers import (
    dashboard_labels,
)

from tests.unit.services.lan_tournament.test_dashboard_render import (
    context_of,
    panel_html,
)
from tests.unit.services.lan_tournament.test_dashboard_rows_render import (
    ack_summary,
    JONAS_COMMENT,
    kupfer_50,
    MARA_COMMENT,
    neon_50,
    Node,
    orbit_50,
    parse,
)


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / 'byceps/static/behavior/lan_tournament_dashboard.js'
SUITE = ROOT / 'tests/js/lan_tournament_dashboard.test.js'
MODULE = ROOT / 'byceps/services/lan_tournament/blueprints'
COMMON = MODULE / 'common/templates'
ADMIN = MODULE / 'admin/templates'
SITE = MODULE / 'site/templates'
PANEL = 'common/lan_tournament/_dashboard_panel.html'
PO = ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

LIST_PATH = {
    'admin': '/lan-tournaments/for_party/pixelnacht-36/dashboard',
    'site': '/lan-tournaments/orga-dashboard',
}

# The behaviours of the plan's `## Tests` row, by exact name.
PLAN_NAMES = [
    'test_no_overlapping_or_out_of_order_polls',
    'test_edit_submit_and_focus_are_preserved',
    'test_transient_failure_marks_stale',
    'test_permission_loss_clears_and_stops',
    'test_native_refresh_needs_no_js',
    'test_redirected_or_non_json_poll_is_session_loss',
    'test_focus_fallback_and_hold_reasons',
    'test_tile_toggle_keeps_focus_and_announces',
    'test_refusal_replaces_panel_and_keeps_draft',
    'test_manual_refresh_with_draft_asks_first',
    'test_gateway_5xx_and_csrf_errors_are_not_access_loss',
]

# Labels the script reads that the server does not send yet. The script
# falls back to texts it has (see the handoff); once the server sends them
# this set only shrinks.
PENDING_LABELS = {
    'tile_filtered_template',
    'tile_unfiltered_template',
    'ack_failed',
}


# ---------------------------------------------------------------- Node


def node_executable() -> str:
    """Return Node.js, or fail the run: the proof is never skipped."""
    path = shutil.which('node')
    if path is None:
        pytest.fail(
            'Node.js is required for the dashboard script tests'
            ' (`node --test tests/js/lan_tournament_dashboard.test.js`);'
            ' install it, the proof of the script cannot be skipped.'
        )
    return path


def run_node(*args: str, env=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed local node binary
        [node_executable(), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )


# ---------------------------------------------------------------- the world


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'dashboard-js-unit-test-only'
    app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    app.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    app.config['TIMEZONE'] = 'Europe/Berlin'
    Babel(app)

    def stub(**_):
        return ''

    def register(name, rules, prefix):
        blueprint = Blueprint(name, name)
        for rule, endpoint in rules:
            blueprint.add_url_rule(rule, endpoint=endpoint, view_func=stub)
        app.register_blueprint(blueprint, url_prefix=prefix)

    register(
        'lan_tournament_admin',
        [
            ('/for_party/<party_id>/dashboard', 'dashboard_for_party'),
            (
                '/for_party/<party_id>/dashboard/poll',
                'dashboard_poll_for_party',
            ),
            (
                '/for_party/<party_id>/dashboard/matches/<match_id>/pin',
                'dashboard_pin',
            ),
            (
                '/for_party/<party_id>/dashboard/matches/<match_id>/ack',
                'dashboard_ack',
            ),
            ('/matches/<match_id>', 'view_match'),
            ('/tournaments/<tournament_id>', 'view'),
        ],
        '/lan-tournaments',
    )
    register(
        'lan_tournament',
        [
            ('/', 'index'),
            ('/orga-dashboard', 'orga_dashboard'),
            ('/orga-dashboard/poll', 'orga_dashboard_poll'),
            ('/orga-dashboard/matches/<match_id>/pin', 'orga_dashboard_pin'),
            ('/orga-dashboard/matches/<match_id>/ack', 'orga_dashboard_ack'),
            ('/matches/<match_id>', 'view_match'),
            ('/<tournament_id>', 'view'),
        ],
        '/lan-tournaments',
    )
    register('authn_login_admin', [('/login', 'log_in_form')], '/admin-auth')
    register('authn_login', [('/login', 'log_in_form')], '/auth')

    return app


@pytest.fixture(autouse=True)
def request_context(app):
    with app.test_request_context('/lan-tournaments/orga-dashboard'):
        yield


@pytest.fixture(scope='module')
def german_translations():
    with PO.open('rb') as f:
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
    with force_locale('de'):
        yield


@pytest.fixture(scope='module')
def env():
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({}),
                FileSystemLoader([str(COMMON), str(ADMIN), str(SITE)]),
            ]
        ),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.globals['url_for'] = url_for
    env.globals['_'] = flask_babel.gettext
    env.filters['dateformat'] = flask_babel.format_date
    return env


def rich_panel(env, surface: str) -> str:
    """Render a page in which every hook of the script has an element."""
    mara = ack_summary('Mara', '14:15', MARA_COMMENT)
    jonas = ack_summary('Jonas', '14:10', JONAS_COMMENT, revision=2)
    rows = [
        kupfer_50(
            latest_acknowledgement=mara,
            recent_acknowledgements=(mara, jonas),
            acknowledgement_count=2,
        ),
        neon_50(),
        orbit_50(),
    ]
    context = context_of(
        rows,
        surface=surface,
        counts=(1, 2, 1),
        total=120,
        pages=3,
        query={'page': 2},
    )
    return panel_html(env, context)


# ----------------------------------------------------------------- selectors


def _compound_matches(node: Node, compound: str) -> bool:
    tag = re.match(r'[a-zA-Z][\w-]*', compound)
    if tag and node.tag != tag.group(0):
        return False
    for name in re.findall(r'\.([\w-]+)', re.sub(r'\[[^\]]*\]', '', compound)):
        if name not in node.classes:
            return False
    identifier = re.search(r'#([\w-]+)', re.sub(r'\[[^\]]*\]', '', compound))
    if identifier and node.attrs.get('id') != identifier.group(1):
        return False
    for name, value in re.findall(r'\[([\w-]+)(?:="([^"]*)")?\]', compound):
        if name not in node.attrs:
            return False
        if value and node.attrs[name] != value:
            return False
    return True


def select(root: Node, selector: str) -> list[Node]:
    """Return the nodes a selector of descendant steps matches."""
    found: list[Node] = []
    for alternative in selector.split(','):
        steps = alternative.split()
        for node in root.walk():
            if not _compound_matches(node, steps[-1]):
                continue
            at, parent = len(steps) - 2, node.parent
            while at >= 0 and parent is not None:
                if _compound_matches(parent, steps[at]):
                    at -= 1
                parent = parent.parent
            if at < 0:
                found.append(node)
    return found


# ------------------------------------------------------------ the tests


def test_the_node_suite_runs_without_skip():
    version = run_node('--version').stdout.strip()
    assert re.match(r'v(\d+)\.', version), version
    assert int(re.match(r'v(\d+)\.', version).group(1)) >= 18, version

    result = run_node('--test', '--test-reporter=tap', str(SUITE))

    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    summary = dict(re.findall(r'^# (\w+) (\d+)$', result.stdout, re.M))
    assert summary['fail'] == '0'
    assert summary['skipped'] == '0'
    assert summary['todo'] == '0'
    assert summary['cancelled'] == '0'
    passed = re.findall(r'^ok \d+ - (\S+)', result.stdout, re.M)
    assert [name for name in PLAN_NAMES if name not in passed] == []
    assert int(summary['pass']) == len(passed) >= len(PLAN_NAMES)


@pytest.mark.parametrize('name', PLAN_NAMES)
def test_each_named_behaviour_passes_in_the_node_suite(name):
    result = run_node(
        '--test',
        '--test-reporter=tap',
        f'--test-name-pattern=^{name}$',
        str(SUITE),
    )

    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert re.search(rf'^ok \d+ - {name}$', result.stdout, re.M)
    summary = dict(re.findall(r'^# (\w+) (\d+)$', result.stdout, re.M))
    assert summary['pass'] == '1'
    assert summary['fail'] == '0'


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_the_script_runs_on_the_real_markup(env, german, surface, tmp_path):
    # The same scenario as in the Node suite, on the panel the real
    # templates render (German catalogue, both surfaces).
    html = tmp_path / f'panel-{surface}.html'
    html.write_text(rich_panel(env, surface), encoding='utf-8')

    result = run_node(
        '--test',
        '--test-reporter=tap',
        '--test-name-pattern=test_the_script_runs_on_the_real_markup',
        str(SUITE),
        env={**os.environ, 'LT_DASHBOARD_PANEL': str(html)},
    )

    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert 'real-markup rows=3 source=real' in result.stdout
    assert re.search(
        r'^ok \d+ - test_the_script_runs_on_the_real_markup$',
        result.stdout,
        re.M,
    )


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_native_refresh_needs_no_js(env, german, surface):
    root = parse(rich_panel(env, surface)).one('section', 'lt-dashboard')
    list_path = LIST_PATH[surface]

    # The refresh is a plain GET form with its own submit button.
    form = root.one('form', 'ltd-refresh')
    assert form.attrs['method'] == 'get'
    assert form.attrs['action'] == list_path
    fields = {
        i.attrs['name']: i.attrs['value']
        for i in form.find_all('input', type='hidden')
    }
    expected = {'view', 'state', 'sort', 'page'}
    assert set(fields) == expected | (
        {'scope'} if surface == 'admin' else set()
    )
    assert fields['page'] == '2'
    button = form.one('button', **{'data-refresh': True})
    assert button.attrs['type'] == 'submit'
    assert button.text == 'Jetzt aktualisieren'

    # The status line holds the text for the browser without the script
    # next to the one for the script; the style sheet picks one.
    status = root.one('p', 'ltd-fst')
    assert status.one('span', 'ltd-fst-manual').text == (
        'Ohne JavaScript: nur manuelle Aktualisierung'
    )
    assert status.one('span', 'ltd-fst-auto').text

    # Nothing in the markup needs the script: no inline handler, no script
    # URL, every tile and every list control is a link or a GET form.
    for node in root.walk():
        assert not any(name.startswith('on') for name in node.attrs), node.tag
        assert not str(node.attrs.get('href', '')).startswith('javascript:')
    for tile in root.find_all('li', 'ltd-st'):
        assert (tile.find('a') is None) == ('is-zero' in tile.classes)
    assert root.one('form', 'ltd-filters').attrs['method'] == 'get'
    assert root.one('form', 'ltd-filters').find_all('button', type='submit')
    assert root.one('nav', 'ltd-pager').find_all('a')

    # The check and the pin are native POSTs; the check form is in the
    # markup and open, and the controls that only the script can serve are
    # hidden until it runs.
    for ack in root.find_all('form', **{'data-ack': True}):
        assert ack.attrs['method'] == 'post'
        assert ack.attrs['action'].endswith('/ack')
        assert 'hidden' not in ack.attrs
        assert 'data-open' not in ack.attrs
        names = {i.attrs['name'] for i in ack.find_all('input')}
        assert {'csrf_token', 'episode', 'revision', 'return'} <= names
        assert ack.one('textarea').attrs['name'] == 'comment'
        assert 'maxlength' not in ack.one('textarea').attrs
        assert 'hidden' in ack.one('button', **{'data-ack-cancel': True}).attrs
    for opener in root.find_all('button', **{'data-ack-open': True}):
        assert 'hidden' in opener.attrs
    for pin in root.find_all('form', 'ltd-pin'):
        assert pin.attrs['method'] == 'post'
        assert pin.attrs['action'].endswith('/pin')
        assert pin.one('button').attrs['type'] == 'submit'

    # The wrappers load the script deferred and nothing else needs it.
    for page in (ADMIN, SITE):
        source = next(page.rglob('dashboard.html')).read_text(encoding='utf-8')
        tag = re.search(
            r'<script[^>]*lan_tournament_dashboard\.js[^>]*>', source
        )
        assert tag is not None and ' defer' in tag.group(0)


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_every_hook_of_the_script_is_in_the_real_markup(env, german, surface):
    hooks = json.loads(
        run_node(
            '-e',
            'console.log(JSON.stringify(require(process.argv[1]).HOOKS))',
            str(SCRIPT),
        ).stdout
    )
    root = parse(rich_panel(env, surface))

    assert hooks
    missing = {
        name: selector
        for name, selector in hooks.items()
        if not select(root, selector)
    }
    assert missing == {}


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_the_overview_derivation_matches_the_real_routes(surface):
    from byceps.services.lan_tournament.blueprints.admin import (
        views as admin_views,
    )
    from byceps.services.lan_tournament.blueprints.site import (
        views as site_views,
    )

    views = {'admin': admin_views, 'site': site_views}[surface]
    app = Flask(f'real-{surface}')
    app.register_blueprint(views.blueprint, url_prefix='/lan-tournaments')
    name = views.blueprint.name
    with app.test_request_context('/'):
        if surface == 'admin':
            party = {'party_id': 'pixelnacht-36'}
            poll = url_for(f'{name}.dashboard_poll_for_party', **party)
            overview = url_for(f'{name}.index', **party)
        else:
            poll = url_for(f'{name}.orga_dashboard_poll')
            overview = url_for(f'{name}.index')

    derived = json.loads(
        run_node(
            '-e',
            'const m = require(process.argv[1]);'
            'console.log(JSON.stringify(m.overviewUrl('
            'process.argv[2], process.argv[3] + "?view=due", '
            '"https://lan.example/")))',
            str(SCRIPT),
            surface,
            poll,
        ).stdout
    )

    assert derived == overview


def test_the_script_reads_only_labels_the_server_sends():
    source = SCRIPT.read_text(encoding='utf-8')
    keys: set[str] = set()
    for match in re.finditer(r'\blabel\(', source):
        depth, at = 1, match.end()
        while depth and at < len(source):
            depth += {'(': 1, ')': -1}.get(source[at], 0)
            at += 1
        keys.update(
            re.findall(r"'([a-z]+(?:_[a-z]+)*)'", source[match.end() : at])
        )
    keys.update(re.findall(r'\blabels\.([a-z_]+)', source))
    keys.update(re.findall(r"\blabels\['([a-z_]+)'\]", source))
    keys -= {'true', 'false'}

    known = set(dashboard_labels())

    assert keys, 'the scan found no label'
    assert keys - known <= PENDING_LABELS
    # The labels that exist keep their meaning: templates are raw messages.
    for key in keys & known:
        assert key.endswith('_template') == ('%(' in dashboard_labels()[key]), (
            key
        )


def test_the_script_ships_no_build_names_or_internal_paths():
    source = SCRIPT.read_text(encoding='utf-8')

    for word in (
        'mockup',
        'skin-',
        'canvas',
        'figma',
        'claude',
        'kevins',
        '/workspace',
        'http://',
        'https://',
        '.design',
        'TODO',
    ):
        assert word not in source, word
    # Class names carry the namespace of the stylesheet.
    classes = set(re.findall(r"class: '([^']+)'", source))
    assert classes
    assert all(
        part.startswith(('ltd-', 'b-', 'pri', 'is-'))
        for entry in classes
        for part in entry.split()
    ), classes


def test_the_script_decides_nothing_and_inserts_no_markup():
    source = SCRIPT.read_text(encoding='utf-8')

    # No markup from strings, no code from strings, no hidden timers.
    for word in (
        'innerHTML',
        'outerHTML',
        'insertAdjacentHTML',
        'document.write',
        'eval(',
        'new Function',
        'setInterval',
        '.submit(',
    ):
        assert word not in source, word
    # It never writes a tier: the tier classes are only read.
    assert not re.search(r"classList\.(add|remove)\(\s*'t-", source)
    # The one place that posts is the viewer's own submit.
    posts = [m.start() for m in re.finditer(r"method: 'POST'", source)]
    assert len(posts) == 1
    assert source.count('submitAction(') == 2  # the definition, the handler
    # The number of requests it can start is the number of `send` calls.
    assert len(re.findall(r'\bsend\(', source)) == 3  # definition, GET, POST
