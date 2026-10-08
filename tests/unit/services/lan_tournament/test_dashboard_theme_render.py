from datetime import datetime
from io import BytesIO
from pathlib import Path
import re
from types import SimpleNamespace

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

from tests.unit.services.lan_tournament.test_dashboard_render import (
    build_variant,
    context_of,
    contrast,
    parse_css,
    RULES,
    VARIANT_NAMES,
)
from tests.unit.services.lan_tournament.test_dashboard_rows_render import (
    kupfer_50,
    neon_50,
    Node,
    orbit_50,
    parse,
)


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / 'byceps/services/lan_tournament/blueprints'
COMMON = MODULE / 'common/templates'
ADMIN = MODULE / 'admin/templates'
SITE = MODULE / 'site/templates'
BOTE = ROOT / 'sites/totalverplant-36'
OVERRIDES = BOTE / 'template_overrides'
WRAPPER = OVERRIDES / 'site/lan_tournament/dashboard.html'
PARTIAL = OVERRIDES / 'site/lan_tournament/_bote_dashboard_style.html'
GENERIC_WRAPPER = SITE / 'site/lan_tournament/dashboard.html'
PO = ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

PAGE = 'site/lan_tournament/dashboard.html'
PANEL = 'common/lan_tournament/_dashboard_panel.html'
STYLE_PARTIAL = 'site/lan_tournament/_bote_dashboard_style.html'

PARTY = SimpleNamespace(
    title='Pixelnacht 36',
    starts_at=datetime(2026, 10, 2, 12, 0),
    ends_at=datetime(2026, 10, 5, 10, 0),
)

# What the layouts of the real apps give a page; the wrappers fill the rest.
GENERIC_LAYOUTS = {
    'layout/site/lan_tournament.html': (
        '<title>{{ page_title|join(" | ") }}</title>'
        '{% block head %}{% endblock %}'
        '<main>{% block body %}{% endblock %}</main>'
        '{% block scripts %}{% endblock %}'
    ),
}
BOTE_LAYOUTS = {
    'layout/base.html': (
        '<title>{{ page_title|join(" | ") }}</title>'
        '<head>{% block head %}{% endblock %}</head>'
        '<body class="page-{{ current_page|default("") }}">'
        '{% block subnav %}{% endblock %}'
        '<main>{% block body required %}{% endblock %}</main>'
        '{% block scripts %}{% endblock %}</body>'
    ),
}


# ---------------------------------------------------------------- the world


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'dashboard-theme-unit-test-only'
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
    # The orga line is new; its German is still pending in the catalogue.
    if 'only for orgas' not in catalog:
        catalog.add('only for orgas', 'nur für Orgas')
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


def _environment(layouts, *paths) -> Environment:
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader(layouts),
                FileSystemLoader([str(path) for path in paths]),
            ]
        ),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.globals['url_for'] = url_for
    env.globals['_'] = flask_babel.gettext
    env.filters['dateformat'] = flask_babel.format_date
    return env


@pytest.fixture(scope='module')
def generic_env() -> Environment:
    """The generic site: the module's own templates only."""
    return _environment(GENERIC_LAYOUTS, COMMON, ADMIN, SITE)


@pytest.fixture(scope='module')
def bote_env() -> Environment:
    """The totalverplant site: its overrides come before the module's."""
    return _environment(BOTE_LAYOUTS, OVERRIDES, COMMON, ADMIN, SITE)


def render_page(env, **context) -> str:
    values = dict(
        dashboard=None,
        party=PARTY,
        action=None,
        unavailable=None,
    )
    values.update(context)
    return env.get_template(PAGE).render(**values)


UNAVAILABLE = dict(
    heading='Diese Begegnung ist nicht verfügbar.',
    detail='Sie existiert nicht oder du kannst sie nicht sehen.',
    back_label='Zurück zum Orga-Dashboard',
    back_url='/lan-tournaments/orga-dashboard',
)


# ------------------------------------------------------------ small readers


def operational_markup(html: str) -> str:
    """Return the panel exactly as written: one root, nothing around it."""
    assert html.count('<section class="lt-dashboard') == 1
    start = html.index('<section class="lt-dashboard')
    end = html.rindex('</section>') + len('</section>')
    return html[start:end]


def signature(node: Node) -> list[tuple]:
    """Return tag, attributes and own text of a subtree, in reading order."""
    return [
        (
            n.tag,
            tuple(sorted(n.attrs.items())),
            tuple(c.strip() for c in n.children if isinstance(c, str)),
        )
        for n in (node, *node.walk())
    ]


def without_comments(source: str) -> str:
    """Drop the template comments and the style element of the partial."""
    source = re.sub(r'\{#.*?#\}', '', source, flags=re.S)
    return re.sub(r'</?style>', '', source)


def partial_css() -> str:
    return re.sub(
        r'/\*.*?\*/',
        '',
        without_comments(PARTIAL.read_text('utf-8')),
        flags=re.S,
    )


PARTIAL_RULES = parse_css(partial_css())


def declarations(selector: str, *, context=()) -> dict[str, str]:
    """Merge what the partial says about one selector."""
    merged: dict[str, str] = {}
    for rule in PARTIAL_RULES:
        if rule.context == context and selector in rule.selectors:
            merged.update(dict(rule.declarations))
    return merged


LIGHT_SELECTOR = '.orga-dashboard-page'
DARK_SELECTOR = 'html[data-theme="dark"] .orga-dashboard-page'
TOKEN_SELECTOR = '.lt-dashboard.is-site'

# The inks of the page: the palette is the only place a colour is written.
PALETTE_NAMES = [
    '--paper',
    '--paperDark',
    '--paperLite',
    '--ink',
    '--inkSoft',
    '--inkMute',
    '--red',
    '--redDeep',
    '--blue',
    '--green',
    '--amber',
]


def palette(theme: str) -> dict[str, str]:
    light = {
        name: value
        for name, value in declarations(LIGHT_SELECTOR).items()
        if name.startswith('--')
    }
    if theme == 'light':
        return light
    dark = {
        name: value
        for name, value in declarations(DARK_SELECTOR).items()
        if name.startswith('--')
    }
    return {**light, **dark}


def theme_tokens() -> dict[str, str]:
    return {
        name: value
        for name, value in declarations(TOKEN_SELECTOR).items()
        if name.startswith('--ltd-')
    }


def _mix(foreground: str, share: int, background: str) -> str:
    f = [int(foreground[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(background[i : i + 2], 16) for i in (1, 3, 5)]
    mixed = [
        round(x * share / 100 + y * (100 - share) / 100)
        for x, y in zip(f, b, strict=True)
    ]
    return '#' + ''.join(f'{channel:02x}' for channel in mixed)


def resolve(token: str, theme: str) -> str:
    """Return the value of a token under one palette, with `var` followed."""
    inks = palette(theme)
    tokens = theme_tokens()

    def lookup(match: re.Match) -> str:
        name = match.group(1)
        if name in inks:
            return inks[name]
        # A token of the theme, or a font of the site itself.
        return resolve(name, theme) if name in tokens else match.group(0)

    value = re.sub(r'var\((--[\w-]+)\)', lookup, tokens[token])
    mix = re.fullmatch(
        r'color-mix\(in srgb, (#[0-9a-f]{6}) (\d+)%, (#[0-9a-f]{6})\)', value
    )
    if mix:
        return _mix(mix.group(1), int(mix.group(2)), mix.group(3))
    return value


def color(name: str, theme: str) -> str:
    return resolve(f'--ltd-{name}', theme)


# ------------------------------------------------- one panel, two wrappers


FRAME_ROWS = 'the frame rows'


def context_for(name: str):
    """Build the context in the test: it needs the request's `url_for`."""
    if name == FRAME_ROWS:
        rows = [neon_50(), kupfer_50(), orbit_50()]
        return context_of(rows, surface='site'), {}
    return build_variant(name, 'site')


@pytest.mark.parametrize('name', [FRAME_ROWS, *VARIANT_NAMES])
def test_generic_and_bote_have_identical_operational_subtree(
    generic_env, bote_env, german, name
):
    context, extra = context_for(name)
    values = dict(dashboard=context, action=extra.get('action'))

    generic = operational_markup(render_page(generic_env, **values))
    bote = operational_markup(render_page(bote_env, **values))
    alone = generic_env.get_template(PANEL).render(**values)

    # The panel is the canonical one: byte for byte what the poll renders.
    assert bote == generic == alone
    root = parse(bote).one('section', 'lt-dashboard')
    assert 'is-site' in root.classes
    assert root.attrs['data-surface'] == 'site'


def test_the_unavailable_card_is_the_same_on_both_wrappers(
    generic_env, bote_env, german
):
    generic = parse(render_page(generic_env, unavailable=UNAVAILABLE))
    bote = parse(render_page(bote_env, unavailable=UNAVAILABLE))

    card = bote.one('div', 'lt-dashboard')
    assert signature(card) == signature(generic.one('div', 'lt-dashboard'))
    assert 'is-site' in card.classes
    assert card.one('h2').text == UNAVAILABLE['heading']
    assert card.one('a', 'ltd-btn').attrs['href'] == UNAVAILABLE['back_url']
    # No list and no panel stand behind the card.
    assert not bote.find_all('section', 'lt-dashboard')
    assert not bote.find_all('li', 'ltd-row')


def test_the_bote_wrapper_adds_the_shell_and_no_business_markup(
    bote_env, german
):
    context = context_of([neon_50()], surface='site')
    page = parse(render_page(bote_env, dashboard=context))

    sheet = page.one('div', 'bote-page')
    assert 'orga-dashboard-page' in sheet.classes
    crumbs = sheet.one('nav', 'crumbs')
    assert crumbs.one('a').text == 'Turniere'
    assert crumbs.one('a').attrs['href'] == '/lan-tournaments/'
    assert [n.text for n in crumbs.find_all('span')] == ['»', 'Orga-Dashboard']
    folio = sheet.one('div', 'page-folio')
    assert [s.text for s in folio.find_all('span')] == [
        'Pixelnacht 36',
        '2. Oktober 2026 – 5. Oktober 2026',
    ]
    head = sheet.one('header', 'head')
    assert head.one('div', 'kick').text == 'Turnierbüro · nur für Orgas'
    assert head.one('h1').text == 'Orga-Dashboard'
    assert head.one('p', 'sub').text == (
        'Was gerade wartet – über alle deine Turniere.'
    )
    assert page.one('title').text == 'Orga-Dashboard | Pixelnacht 36'
    assert page.one('body').attrs['class'] == 'page-tournaments'

    # Shell, then the one panel: no other node carries the dashboard's names.
    panel = sheet.one('section', 'lt-dashboard')
    inside = {id(n) for n in panel.walk()} | {id(panel)}
    outside = [
        n
        for n in sheet.walk()
        if id(n) not in inside
        and (
            'lt-dashboard' in n.classes
            or any(c.startswith('ltd-') for c in n.classes)
        )
    ]
    assert len(sheet.find_all('section', 'lt-dashboard')) == 1
    assert outside == []


def test_the_bote_wrapper_repeats_nothing_of_the_panel_or_the_rows():
    source = WRAPPER.read_text('utf-8')

    assert (
        source.count(
            "{% include 'common/lan_tournament/_dashboard_panel.html' %}"
        )
        == 1
    )
    for forbidden in (
        '_dashboard_rows',
        'render_row',
        'data-lt-dashboard-root',
        'csrf',
    ):
        assert forbidden not in source

    # The one hand-written card is the generic wrapper's, character for character.
    def card(text: str) -> str:
        return text[
            text.index('{%- if unavailable') : text.index('{%- else %}')
        ]

    assert card(source) == card(GENERIC_WRAPPER.read_text('utf-8'))
    # Every class of the dashboard in the wrapper belongs to that card.
    classes = set(re.findall(r'ltd-[\w-]+|lt-dashboard', source))
    assert classes == set(re.findall(r'ltd-[\w-]+|lt-dashboard', card(source)))


# ------------------------------------------------------- assets and focus


def test_theme_override_loads_shared_assets_and_focus_tokens(bote_env, german):
    html = render_page(
        bote_env, dashboard=context_of([neon_50()], surface='site')
    )
    page = parse(html)

    head = page.one('head')
    sheets = [n.attrs['href'] for n in head.find_all('link')]
    assert sheets == ['/static/style/lan_tournament_dashboard.css']
    # The theme loads after the shared sheet, so equal specificity wins it.
    assert html.index('lan_tournament_dashboard.css') < html.index('<style>')
    assert head.find_all('style') != []
    scripts = page.find_all('script')
    assert [s.attrs['src'] for s in scripts] == [
        '/static/behavior/lan_tournament_dashboard.js'
    ]
    assert 'defer' in scripts[0].attrs

    # Focus: the shared 3 px ring, drawn in the theme's own focus token.
    ring = RULES_BY_SELECTOR['.lt-dashboard :focus-visible']
    assert ring['outline'] == '3px solid var(--ltd-focus)'
    assert 'focus' not in ' '.join(
        selector for rule in PARTIAL_RULES for selector in rule.selectors
    )
    assert not [
        name
        for rule in PARTIAL_RULES
        for name, _ in rule.declarations
        if name == 'outline'
    ]
    for theme in ('light', 'dark'):
        for ground in GROUNDS:
            assert contrast(color('focus', theme), color(ground, theme)) >= 3, (
                theme,
                ground,
            )


RULES_BY_SELECTOR = {
    selector: dict(rule.declarations)
    for rule in RULES
    if not rule.context
    for selector in rule.selectors
}
GROUNDS = [
    'bg',
    'surf',
    'surf-2',
    'field',
    'stale',
    'g-bg',
    'y-bg',
    'r-bg',
    'conf-bg',
    'err-bg',
    'info-bg',
    'ok-bg',
    'btn',
]


def test_the_theme_is_scoped_to_the_dashboard_page_and_its_panel():
    selectors = [s for rule in PARTIAL_RULES for s in rule.selectors]

    assert selectors
    for selector in selectors:
        assert selector.startswith(
            (
                '.orga-dashboard-page',
                'html[data-theme="dark"] .orga-dashboard-page',
                '.lt-dashboard.is-site',
            )
        ), selector
    # The dark set follows the site's own switch, not the system setting.
    css = partial_css()
    assert 'prefers-color-scheme' not in css
    assert '.dk' not in css
    assert 'data-theme="dark"' in css


def test_the_partial_is_plain_template_text_and_names_no_source():
    source = PARTIAL.read_text('utf-8')
    wrapper = WRAPPER.read_text('utf-8')

    # It parses, and it renders to its own style element only.
    env = Environment(autoescape=True)
    env.parse(source)
    rendered = env.from_string(source).render()
    assert rendered.strip().startswith('<style>')
    assert rendered.strip().endswith('</style>')
    assert '{#' not in rendered
    assert '{%' not in rendered
    assert '{{' not in rendered
    for text in (source, wrapper):
        for name in (
            'mockup',
            'skin-',
            'https://',
            'http://',
            '.kevins',
            '/workspace',
        ):
            assert name not in text, name


# ----------------------------------------------------------------- tokens


def test_bote_tokens_cover_light_and_dark():
    sheet = {
        name
        for rule in RULES
        if not rule.context and '.lt-dashboard' in rule.selectors
        for name, _ in rule.declarations
        if name.startswith('--ltd-')
    }
    declared_tokens = set(theme_tokens())

    # Every token of the shared sheet is set by the theme (the cell
    # padding is geometry that the design leaves alone).
    assert len(sheet) == 50
    assert sheet - declared_tokens == {'--ltd-pad'}
    # The optional font tokens come from the site's own fonts.
    assert declared_tokens - sheet == {
        '--ltd-font',
        '--ltd-head',
        '--ltd-meta',
        '--ltd-num',
        '--ltd-bfont',
    }

    # The palette is complete in both themes and the dark one replaces
    # every ink: no light ink is left on the dark ground.
    light, dark = palette('light'), palette('dark')
    assert set(light) == set(PALETTE_NAMES)
    assert set(declarations(DARK_SELECTOR)) == set(PALETTE_NAMES)
    assert all(light[name] != dark[name] for name in PALETTE_NAMES)

    fonts = {
        '--ltd-font',
        '--ltd-head',
        '--ltd-meta',
        '--ltd-num',
        '--ltd-bfont',
    }
    for theme in ('light', 'dark'):
        for token in declared_tokens - fonts - {'--ltd-line'}:
            value = resolve(token, theme)
            assert 'var(' not in value, (theme, token)
            assert 'color-mix' not in value, (theme, token)
        # The rule colour is the muted ink at 45 %, over whatever lies under.
        assert resolve('--ltd-line', theme) == (
            f'color-mix(in srgb, {palette(theme)["--inkMute"]} 45%, transparent)'
        )

    # Every colour token follows the switch.
    colors = [
        name
        for name in declared_tokens
        if resolve(name, 'light').startswith('#')
    ]
    assert len(colors) == 34
    for name in colors:
        assert resolve(name, 'light') != resolve(name, 'dark'), name


# fmt: off
DESIGN_VALUES = [
    ('light', 'bg', '#f1ead4'), ('light', 'surf', '#f7f1dc'),
    ('light', 'surf-2', '#f1ead4'), ('light', 'stale', '#e6dec0'),
    ('light', 'ink', '#1a1812'), ('light', 'soft', '#4a4435'),
    ('light', 'mute', '#6a5f47'), ('light', 'line-s', '#1a1812'),
    ('light', 'link', '#74170f'), ('light', 'focus', '#74170f'),
    ('light', 'pri', '#1a1812'), ('light', 'pri-ink', '#f1ead4'),
    ('light', 'g-ink', '#3b5a2a'), ('light', 'y-ink', '#755803'),
    ('light', 'r-ink', '#a8281c'), ('light', 'ready', '#1f3a64'),
    ('light', 'conf', '#74170f'), ('light', 'rev', '#a8281c'),
    ('light', 'y-bg', '#e2d9b9'), ('light', 'r-bg', '#ecd5c1'),
    ('dark', 'bg', '#1c1914'), ('dark', 'surf', '#262219'),
    ('dark', 'surf-2', '#1c1914'), ('dark', 'stale', '#14110d'),
    ('dark', 'ink', '#ece3cd'), ('dark', 'soft', '#c9bfa3'),
    ('dark', 'mute', '#a39779'), ('dark', 'line-s', '#ece3cd'),
    ('dark', 'link', '#f08a72'), ('dark', 'focus', '#f08a72'),
    ('dark', 'pri', '#ece3cd'), ('dark', 'pri-ink', '#1c1914'),
    ('dark', 'g-ink', '#a9c78c'), ('dark', 'y-ink', '#e3b655'),
    ('dark', 'r-ink', '#ee7a63'), ('dark', 'ready', '#9fb7dc'),
    ('dark', 'conf', '#f08a72'), ('dark', 'rev', '#ee7a63'),
    ('dark', 'y-bg', '#443a23'), ('dark', 'r-bg', '#422e23'),
]
# fmt: on


@pytest.mark.parametrize(
    ('theme', 'name', 'expected'),
    DESIGN_VALUES,
    ids=[f'{t}-{n}' for t, n, _ in DESIGN_VALUES],
)
def test_bote_tokens_are_the_design_values(theme, name, expected):
    assert color(name, theme) == expected


def test_bote_type_and_shape_follow_the_totalverplant_design():
    tokens = theme_tokens()

    assert tokens['--ltd-font'] == 'var(--body)'
    assert tokens['--ltd-head'] == 'var(--display)'
    assert tokens['--ltd-meta'] == tokens['--ltd-num'] == 'var(--mono)'
    assert tokens['--ltd-bfont'] == 'var(--mono)'
    assert [tokens[f'--ltd-{n}'] for n in ('fs', 'h2', 'h3', 'hw')] == [
        '18px',
        '26px',
        '23px',
        '900',
    ]
    assert tokens['--ltd-bw'] == '1.5px'
    assert tokens['--ltd-r'] == '0px'
    assert tokens['--ltd-rgap'] == '16px'
    assert tokens['--ltd-btt'] == 'uppercase'
    assert resolve('--ltd-shadow', 'light') == '4px 4px 0 #1a1812'
    assert resolve('--ltd-shadow', 'dark') == '4px 4px 0 #ece3cd'
    # The sheet is the page's column, not the generic site's 1140 px.
    assert declarations(TOKEN_SELECTOR)['max-width'] == 'none'


# fmt: off
TEXT_PAIRS = [
    ('ink', 'bg'), ('ink', 'surf'), ('ink', 'surf-2'), ('ink', 'field'),
    ('ink', 'stale'), ('soft', 'bg'), ('soft', 'surf'), ('soft', 'surf-2'),
    ('soft', 'stale'), ('mute', 'bg'), ('mute', 'surf'), ('mute', 'surf-2'),
    ('mute', 'stale'), ('link', 'bg'), ('link', 'surf'), ('link', 'surf-2'),
    ('link', 'btn'), ('link', 'y-bg'), ('link', 'conf-bg'),
    ('link', 'info-bg'), ('ready', 'surf'), ('ready', 'surf-2'),
    ('pin', 'surf'), ('pin', 'surf-2'), ('rev', 'surf'), ('rev', 'surf-2'),
    ('err', 'bg'), ('err', 'surf'), ('err', 'surf-2'), ('err', 'field'),
    ('ok', 'surf'), ('info', 'surf'), ('warn-ink', 'surf'),
    ('g-ink', 'g-bg'), ('y-ink', 'y-bg'), ('r-ink', 'r-bg'),
    ('conf', 'conf-bg'), ('err', 'err-bg'), ('ok', 'ok-bg'),
    ('info', 'info-bg'), ('ink', 'err-bg'), ('ink', 'ok-bg'),
    ('ink', 'info-bg'), ('ink', 'btn'), ('ink', 'btn-h'),
    ('pri-ink', 'pri'), ('ink', 'y-bg'), ('soft', 'g-bg'),
]
# fmt: on


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('pair', TEXT_PAIRS, ids='-on-'.join)
def test_bote_tokens_meet_contrast(theme, pair):
    """Text pairs reach 4.5:1 in the Tagesausgabe and in the Spaetausgabe."""
    foreground, background = (color(name, theme) for name in pair)

    assert contrast(foreground, background) >= 4.5, (theme, pair)


def test_bote_light_inks_depart_from_the_design_only_where_the_floor_demands():
    """Two light inks are a little darker than drawn, for 4.5:1 (user rule).

    The design's own values (yellow ink on its ground 4.43:1, muted ink on
    the stale ground 4.39:1) fall short; the values used reach 4.5:1 and are
    the nearest ones that do, so nothing else in the palette moves.
    """
    drawn = {'--amber': '#7a5c08', '--inkMute': '#6e634b'}
    used = palette('light')

    assert {name: used[name] for name in drawn} == {
        '--amber': '#755803',
        '--inkMute': '#6a5f47',
    }
    drawn_ground = _mix(drawn['--amber'], 16, used['--paperLite'])
    assert contrast(drawn['--amber'], drawn_ground) < 4.5
    assert contrast(drawn['--inkMute'], used['--paperDark']) < 4.5
    assert contrast(color('y-ink', 'light'), color('y-bg', 'light')) >= 4.5
    assert contrast(color('mute', 'light'), color('stale', 'light')) >= 4.5
    # Every other palette value is the design's.
    others = {
        '--paper': '#f1ead4',
        '--paperDark': '#e6dec0',
        '--paperLite': '#f7f1dc',
        '--ink': '#1a1812',
        '--inkSoft': '#4a4435',
        '--red': '#a8281c',
        '--redDeep': '#74170f',
        '--blue': '#1f3a64',
        '--green': '#3b5a2a',
    }
    assert {name: used[name] for name in others} == others


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_bote_tokens_meet_contrast_for_tiers_edges_and_the_focus_ring(theme):
    """Tier bars, tile frames, control edges and the ring reach 3:1."""
    for tier in ('g', 'y', 'r'):
        for ground in ('bg', 'surf'):
            assert (
                contrast(color(f'{tier}-ink', theme), color(ground, theme)) >= 3
            )
    for ground in ('bg', 'surf', 'field', 'btn'):
        assert contrast(color('line-s', theme), color(ground, theme)) >= 3
    for ground in GROUNDS:
        assert contrast(color('focus', theme), color(ground, theme)) >= 3
    assert contrast(color('acc', theme), color('surf', theme)) >= 3


def test_bote_dark_inks_are_lifted_above_the_live_palette():
    live = (BOTE / 'static/style/bote-theme.css').read_text('utf-8')
    dark = palette('dark')

    def live_ink(name: str) -> str:
        return re.search(rf'{name}:\s*(#[0-9a-f]{{6}})', live).group(1)  # type: ignore[union-attr]

    ground = dark['--paperLite']
    for ink in ('--inkMute', '--redDeep'):
        # The live ink fails 4.5:1 on the card ground; the lifted one passes.
        assert live_ink(ink) != dark[ink]
        assert contrast(live_ink(ink), ground) < 4.5, ink
        assert contrast(dark[ink], ground) >= 4.5, ink
