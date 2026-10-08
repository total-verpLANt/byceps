from dataclasses import dataclass, replace
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
    build_dashboard_context,
    format_comment_counter,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardNonActionableCounts,
    DashboardPage,
    DashboardQuery,
    DashboardRow,
    DashboardSettings,
    DashboardTierCounts,
    DashboardTournamentRef,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.unit.services.lan_tournament.test_dashboard_rows_render import (
    ack_summary,
    at,
    conflict_of_kupfer,
    EVERY_STATE,
    frame_panels,
    GREEN,
    JONAS_COMMENT,
    kupfer,
    kupfer_50,
    MARA_COMMENT,
    neon,
    neon_50,
    Node,
    orbit_50,
    parse,
    RED,
    timed,
)


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / 'byceps/services/lan_tournament/blueprints'
COMMON = MODULE / 'common/templates'
ADMIN = MODULE / 'admin/templates'
SITE = MODULE / 'site/templates'
PANEL = 'common/lan_tournament/_dashboard_panel.html'
ADMIN_PAGE = 'admin/lan_tournament/dashboard.html'
SITE_PAGE = 'site/lan_tournament/dashboard.html'
CSS_FILE = ROOT / 'byceps/static/style/lan_tournament_dashboard.css'
PO = ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'

PARTY = PartyID('pixelnacht-36')
EVIL = '<script>alert(1)</script>'

SOURCES = [
    COMMON / 'common/lan_tournament/_dashboard_panel.html',
    ADMIN / 'admin/lan_tournament/dashboard.html',
    SITE / 'site/lan_tournament/dashboard.html',
    CSS_FILE,
]

# What the layouts of the real apps give a page; the wrappers fill the rest.
LAYOUTS = {
    'layout/admin/lan_tournament.html': (
        '<title>{{ page_title|join(" | ") }}</title>'
        '{% block head %}{% endblock %}'
        '<nav id="tabs" data-tab="{{ current_tab }}"'
        ' data-party="{{ current_page_party.title }}"></nav>'
        '<main>{% block body %}{% endblock %}</main>'
        '{% block scripts %}{% endblock %}'
    ),
    'layout/site/lan_tournament.html': (
        '<title>{{ page_title|join(" | ") }}</title>'
        '{% block head %}{% endblock %}'
        '<main>{% block body %}{% endblock %}</main>'
        '{% block scripts %}{% endblock %}'
    ),
}


# ---------------------------------------------------------------- the world


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'dashboard-render-unit-test-only'
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
    # The orga line of the site wrapper is new; its German is still pending.
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


@pytest.fixture(scope='module')
def env():
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader(LAYOUTS),
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


# ------------------------------------------------------------- the fixtures


def settings() -> DashboardSettings:
    return DashboardSettings(
        yellow_minutes=15,
        red_minutes=45,
        poll_seconds=30,
        page_size=50,
        threshold_source='deployment',
    )


def tournament_ref(name: str, game: str | None = None):
    return DashboardTournamentRef(
        tournament_id=TournamentID(generate_uuid7()), name=name, game=game
    )


def page_of(
    rows,
    *,
    clock='14:50',
    counts=(0, 2, 1),
    total=None,
    page=1,
    pages=None,
    choices=(),
    cards=(),
    reason=None,
    breakdown=(2, 1, 1),
) -> DashboardPage:
    red, yellow, green = counts
    paused, pre_start, partial = breakdown
    if pages is None:
        pages = 1 if rows else 0
    return DashboardPage(
        rows=tuple(rows),
        as_of=at(clock),
        total_count=len(rows) if total is None else total,
        page=page,
        per_page=50,
        total_pages=pages,
        tier_counts=DashboardTierCounts(red=red, yellow=yellow, green=green),
        non_actionable_counts=DashboardNonActionableCounts(
            paused=paused, pre_start=pre_start, partial=partial
        ),
        leaderboard_only_tournaments=tuple(cards),
        tournament_choices=tuple(choices),
        empty_reason=reason,
    )


def context_of(
    rows,
    *,
    surface='admin',
    query=None,
    errors=None,
    token='token-1',
    **page_fields,
) -> dict[str, Any]:
    query_fields = dict(query or {})
    return build_dashboard_context(
        page_of(rows, **page_fields),
        DashboardQuery(per_page=50, **query_fields),
        settings(),
        surface=surface,
        csrf_token=token,
        party_id=PARTY if surface == 'admin' else None,
        query_errors=errors,
    )


def panel_html(env, context, **extra) -> str:
    return env.get_template(PANEL).render(dashboard=context, **extra)


def root_of(html: str) -> Node:
    return parse(html).one('section', 'lt-dashboard')


def one_row(row: DashboardRow, **kwargs) -> dict[str, Any]:
    return context_of([row], **kwargs)


def refusal(row_context: dict[str, Any], **fields) -> dict[str, Any]:
    """Return what a native POST hands the template when the check fails."""
    row = row_context['rows'][0]
    values = dict(
        kind='ack',
        match_id=row['match_id'],
        error='invalid',
        message='Der Stand hat sich geändert. Bitte aktualisieren.',
        detail=None,
        draft=None,
        draft_target=False,
        field_error=None,
    )
    values.update(fields)
    return values


def hrefs(node: Node) -> list[str]:
    return [a.attrs['href'] for a in node.find_all('a') if 'href' in a.attrs]


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# ---------------------------------------------------------------- the CSS


@dataclass(frozen=True)
class Rule:
    context: tuple[str, ...]
    selectors: tuple[str, ...]
    declarations: tuple[tuple[str, str], ...]


def _strip_comments(css: str) -> str:
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def _split_top(text: str, separator: str) -> list[str]:
    """Split at a separator that is outside every bracket."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _blocks(text: str) -> list[tuple[str, str]]:
    blocks, depth, start, body_start, prelude = [], 0, 0, 0, ''
    for index, char in enumerate(text):
        if char == '{':
            if depth == 0:
                prelude = text[start:index].strip()
                body_start = index + 1
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                blocks.append((prelude, text[body_start:index]))
                start = index + 1
    return blocks


def parse_css(text: str, context: tuple[str, ...] = ()) -> list[Rule]:
    rules: list[Rule] = []
    for prelude, body in _blocks(text):
        if prelude.startswith('@keyframes'):
            rules.append(Rule(context + (prelude,), (), ()))
        elif prelude.startswith('@'):
            rules.extend(parse_css(body, context + (prelude,)))
        else:
            declarations = []
            for part in _split_top(body, ';'):
                name, _, value = part.partition(':')
                declarations.append((name.strip(), ' '.join(value.split())))
            rules.append(
                Rule(
                    context,
                    tuple(_split_top(prelude, ',')),
                    tuple(declarations),
                )
            )
    return rules


CSS_TEXT = CSS_FILE.read_text(encoding='utf-8')
RULES = parse_css(_strip_comments(CSS_TEXT))


def declared(selector: str, *, context=()) -> dict[str, str]:
    """Merge what the rules say about one selector (it may be in a list)."""
    merged: dict[str, str] = {}
    for rule in RULES:
        if rule.context == context and selector in rule.selectors:
            merged.update(dict(rule.declarations))
    return merged


def tokens(selector: str) -> dict[str, str]:
    return {
        name: value
        for name, value in declared(selector).items()
        if name.startswith('--ltd-')
    }


def _linear(channel: int) -> float:
    value = channel / 255
    if value <= 0.03928:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    digits = color.lstrip('#')
    red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    return (
        0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)
    )


def contrast(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)))[::-1]
    return (high + 0.05) / (low + 0.05)


# ----------------------------------------------------------- strict render


def test_dashboard_strictundefined_all_row_states_helper_is_strict(env):
    assert env.undefined is StrictUndefined


@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize(
    'row', [row for _, row in EVERY_STATE], ids=[n for n, _ in EVERY_STATE]
)
def test_dashboard_strictundefined_all_row_states(env, row, surface):
    context = one_row(row, surface=surface)

    html = panel_html(env, context)

    root = root_of(html)
    assert len(parse(html).find_all(**{'data-lt-dashboard-root': True})) == 1
    assert len(root.find_all('li', 'ltd-row')) == 1
    # The poll renders the panel with the refusal set to none or not at all.
    assert html == panel_html(env, context, action=None)


@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize('name', list(frame_panels()))
def test_the_frames_of_the_design_render_whole_panels(env, name, surface):
    for clock, rows in frame_panels()[name]:
        html = panel_html(env, context_of(rows, surface=surface, clock=clock))

        root = root_of(html)
        assert len(root.find_all('li', 'ltd-row')) == len(rows)
        assert root.one('h2', id='ltd-count').text
        rows_list = root.one('ol', 'ltd-rows')
        assert rows_list.attrs['aria-labelledby'] == 'ltd-count'


VARIANT_REASONS = [
    'no_assignment',
    'no_current_demand',
    'no_actionable_fixtures',
    'no_filter_matches',
]
VARIANT_ERRORS = ['csrf_invalid', 'stale', 'refused', 'invalid']
VARIANT_NAMES = (
    [f'{reason}' for reason in VARIANT_REASONS]
    + ['page beyond the last', 'pager and cards', 'invalid query']
    + [f'tile {tier} active' for tier in ('red', 'yellow', 'green')]
    + [
        f'ack {error} target={target}'
        for error in VARIANT_ERRORS
        for target in (True, False)
    ]
    + ['pin refusal']
)


def build_variant(name: str, surface: str):
    """Return a context and the extra variables of one kind of panel."""
    rows = [neon_50(), kupfer_50(), orbit_50()]
    choices = [tournament_ref('Kupfer-Cup'), tournament_ref('Neon-Duell')]
    cards = [tournament_ref('Highscore Cup', 'Pixel Run')]

    if name in VARIANT_REASONS:
        return context_of([], surface=surface, reason=name, total=0), {}
    if name == 'page beyond the last':
        return (
            context_of([], surface=surface, query={'page': 2}, page=2, total=5),
            {},
        )
    if name == 'pager and cards':
        return (
            context_of(
                rows,
                surface=surface,
                query={'page': 5, 'view': 'all'},
                total=240,
                pages=9,
                cards=cards,
                choices=choices,
            ),
            {},
        )
    if name == 'invalid query':
        errors = {'sort': 'Sortierung gibt es nicht.'}
        return (
            context_of(rows, surface=surface, errors=errors, choices=choices),
            {},
        )
    if name.startswith('tile '):
        tier = name.split()[1]
        return (
            context_of(
                rows,
                surface=surface,
                query={'state': f'tier-{tier}'},
                counts=(1, 1, 1),
            ),
            {},
        )
    context = context_of(rows, surface=surface)
    if name == 'pin refusal':
        action = refusal(context, kind='pin', error='stale')
        return context, {'action': action}
    error, target = re.fullmatch(r'ack (\w+) target=(\w+)', name).groups()  # type: ignore[union-attr]
    action = refusal(
        context,
        error=error,
        draft_target=target == 'True',
        draft='Entwurf',
        detail='Mara war schneller.',
    )
    return context, {'action': action}


@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize('name', VARIANT_NAMES)
def test_every_panel_variant_renders_under_strict_undefined(env, name, surface):
    context, extra = build_variant(name, surface)

    html = panel_html(env, context, **extra)

    root = root_of(html)
    assert root.attrs['data-surface'] == surface
    assert root.find('div', 'ltd-sr') is not None


# ---------------------------------------------------------------- escaping


def test_dashboard_escapes_names_and_ack_comments(env, german):
    team = replace(
        conflict_of_kupfer(),
        user_display_name=EVIL,
        via_team_name=EVIL,
    )
    evil_check = ack_summary(EVIL, '14:15', EVIL)
    row = replace(
        kupfer_50(
            tournament_name=EVIL,
            game=EVIL,
            contestant_names=(EVIL, 'Nachtbus'),
            orga_names=(EVIL,),
            conflicts=(team,),
        ),
        latest_acknowledgement=evil_check,
        recent_acknowledgements=(evil_check,),
    )
    choice = tournament_ref(EVIL)
    context = context_of(
        [row],
        query={'tournament_id': choice.tournament_id},
        choices=[choice],
        cards=[tournament_ref(EVIL, EVIL)],
    )
    action = refusal(
        context,
        message=EVIL,
        detail=EVIL,
        draft=EVIL,
        draft_target=False,
        field_error={'field': 'comment', 'text': EVIL},
    )

    html = panel_html(env, context, action=action)

    assert EVIL not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert parse(html).find_all('script') == []
    assert parse(html).find_all('textarea', readonly=True)[0].text == EVIL


def test_the_script_labels_cannot_break_out_of_their_attribute(env):
    context = one_row(kupfer())
    hostile = 'Ti"tle\' <b>&amp;</b> </section><script>x()</script>'
    context['labels'] = {**context['labels'], 'title': hostile}

    html = panel_html(env, context)

    root = root_of(html)
    assert json.loads(root.attrs['data-labels'])['title'] == hostile
    assert '<script>x()' not in html
    assert '</section><script>' not in html
    assert parse(html).find_all('script') == []
    # The attribute is single quoted, so double quotes may stay in the JSON
    # and no single quote may.
    raw = re.search(r"data-labels='([^']*)'", html)
    assert raw is not None


# --------------------------------------------------- tier is not readiness


def test_tier_is_not_ready_and_not_colour_only(env, german):
    rows = [
        timed(kupfer, RED, 60, 45, ready_at_b=at('14:11'), ack='offered'),
        kupfer_50(),
        timed(kupfer, GREEN, 5, 5),
        neon(),
        orbit_50(),
    ]
    html = panel_html(env, context_of(rows, counts=(1, 2, 1)))
    root = root_of(html)

    for li in root.find_all('li', 'ltd-row'):
        tier = li.one('div', 'ltd-tier')
        # Shape, name and value: the tier never rests on colour alone.
        assert tier.one('span', 'ltd-ti').attrs['aria-hidden'] == 'true'
        assert tier.one('span', 'ltd-ti').text
        assert tier.one('span', 'ltd-tn').text
        assert tier.one('span', 'ltd-tv').text
        # Readiness is its own line and carries no tier class.
        for ready in li.find_all('p', 'ltd-ready'):
            for node in [ready, *ready.walk()]:
                assert not [c for c in node.classes if re.match(r't-\w+', c)]

    # A red row can be ready on both sides: two flags, two elements.
    red_row = root.find_all('li', 'ltd-row')[0]
    assert 't-r' in red_row.one('div', 'ltd-tier').classes
    assert red_row.one('b', 'ltd-rs').text == 'Beide bereit'
    assert 'rs-2' in red_row.one('b', 'ltd-rs').classes

    # Tier classes sit on the tier cell and the tiles only.
    carriers = {
        (node.tag, 'ltd-tier' in node.classes or 'ltd-st' in node.classes)
        for node in root.walk()
        if any(re.fullmatch(r't-(g|y|r)', c) for c in node.classes)
    }
    assert carriers <= {('div', True), ('li', True)}

    # Tiles: icon, count, name and range as text.
    for tile in root.find_all('li', 'ltd-st'):
        assert tile.one('span', 'ltd-sti').attrs['aria-hidden'] == 'true'
        assert tile.one('span', 'ltd-sti').text
        assert tile.one('b', 'ltd-stc').text.isdigit()
        assert tile.one('span', 'ltd-stn').text
        assert 'min' in tile.one('small').text

    # In the sheet the ready flags never use a tier token.
    for selector in (
        '.lt-dashboard .ltd-ready',
        '.lt-dashboard .ltd-rs',
        '.lt-dashboard .ltd-rd',
        '.lt-dashboard .ltd-rd.is-on',
        '.lt-dashboard .ltd-rd.is-on i',
    ):
        values = ' '.join(declared(selector).values())
        assert not re.search(r'--ltd-(g|y|r)-(bg|ink)', values), selector


# ----------------------------------------------------- mobile and scroll


CONTROLS = [
    '.lt-dashboard .ltd-btn',
    '.lt-dashboard .ltd-views a',
    '.lt-dashboard .ltd-scope label',
    '.lt-dashboard .ltd-links a',
    '.lt-dashboard .ltd-cl a',
    '.lt-dashboard .ltd-disc',
    '.lt-dashboard .ltd-fld select',
    '.lt-dashboard .ltd-form textarea',
    '.lt-dashboard .ltd-gap',
    '.lt-dashboard .ltd-st > a',
    '.lt-dashboard .ltd-st > span',
]
REFLOW = ('@container (max-width: 760px)',)


@pytest.mark.parametrize('selector', CONTROLS)
def test_mobile_controls_and_scroll_are_scoped(selector):
    assert declared(selector)['min-height'] == '44px'


def test_the_reflow_scrolls_only_the_views_strip(env):
    # The root is the container of the reflow, so no wrapper is needed.
    assert declared('.lt-dashboard')['container-type'] == 'inline-size'
    one_column = declared('.lt-dashboard .ltd-row article', context=REFLOW)
    assert one_column['grid-template-columns'] == 'minmax(0, 1fr)'
    assert one_column['grid-template-areas'] == (
        '"tier" "main" "time" "act" "ack"'
    )

    # Only the view links may scroll sideways, and only in the reflow.
    scrolling = {
        (rule.context, selector)
        for rule in RULES
        for selector in rule.selectors
        for name, value in rule.declarations
        if name in ('overflow', 'overflow-x') and value in ('auto', 'scroll')
    }
    assert scrolling == {(REFLOW, '.lt-dashboard .ltd-views')}

    # Actions are full width, the focus ring is the same everywhere.
    assert declared('.lt-dashboard .ltd-act .ltd-btn')['width'] == '100%'
    focus = declared('.lt-dashboard :focus-visible')
    assert focus['outline'] == '3px solid var(--ltd-focus)'
    assert focus['outline-offset'] == '2px'
    forced = declared(
        '.lt-dashboard :focus-visible',
        context=('@media (forced-colors: active)',),
    )
    assert forced['outline'] == '3px solid Highlight'

    # A hidden control stays hidden: `display: flex` must not beat `hidden`.
    assert declared('.lt-dashboard [hidden]')['display'] == 'none !important'

    # The markup has no table to scroll.
    html = panel_html(env, context_of([kupfer_50(), neon_50(), orbit_50()]))
    assert parse(html).find_all('table') == []


# ------------------------------------------------- the latest record stays


def test_latest_ack_actor_and_history_remain_visible_when_reescalated(
    env, german
):
    mara = ack_summary('Mara', '14:15', MARA_COMMENT)
    jonas = ack_summary('Jonas', '14:50', JONAS_COMMENT, revision=2)
    again = timed(
        kupfer,
        RED,
        75,
        46,
        latest_acknowledgement=jonas,
        recent_acknowledgements=(jonas, mara),
        acknowledgement_count=2,
        ack_revision=2,
        ack='offered',
    )
    html = panel_html(env, context_of([kupfer_50(), again]))

    first, second = root_of(html).find_all('li', 'ltd-row')

    # The renewed alert is a yellow row; Mara's check stays above it.
    record = first.one('div', 'ltd-rec')
    assert 'hidden' not in record.attrs
    assert record.one('p', 'ltd-rech').strings[0] == (
        'Zuletzt geprüft von Mara um 14:15'
    )
    assert record.one('p', 'ltd-recc').text == f'„{MARA_COMMENT}“'
    assert record.one('span', 'ltd-follow').strings == [
        '▲',
        'Erneute Verzögerung – nochmals prüfen',
    ]
    assert 'is-yellow' in record.one('span', 'ltd-follow').classes
    times = {
        item.one('dt').text: item.one('dd').text
        for item in first.one('dl', 'ltd-times').find_all('div')
    }
    assert times['Aktive Wartezeit gesamt'] == '30 min'
    assert first.one('div', 'ltd-tier').one('span', 'ltd-tv').text == (
        '15 min Alarmintervall'
    )
    opener = first.one('button', **{'data-ack-open': True})
    assert opener.text == 'Nochmals prüfen'

    # Another orga's later check names that orga; the history lists both.
    assert (
        second.one('p', 'ltd-rech')
        .strings[0]
        .startswith('Zuletzt geprüft von Jonas um 14:50')
    )
    assert 'is-red' in second.one('span', 'ltd-follow').classes
    disclosure = second.one('button', 'ltd-disc')
    assert disclosure.attrs['aria-expanded'] == 'false'
    history = second.one('ol', 'ltd-hist')
    assert history.attrs['id'] == disclosure.attrs['aria-controls']
    assert 'hidden' in history.attrs
    entries = [li.strings for li in history.find_all('li')]
    assert [entry[0] for entry in entries] == ['Jonas', 'Mara']
    assert 'aktuell' in ' '.join(entries[0])
    assert second.one('span', 'ltd-dc').text == '(2 in dieser Episode)'
    # The record itself is never inside the hidden list.
    assert (
        'ltd-hist' not in [c for c in record.classes]
        and record.parent is not history
    )


# ------------------------------------------------------------- the form


def test_ack_form_has_counter_and_no_maxlength(env, german):
    html = panel_html(env, one_row(neon()))
    form = root_of(html).one('form', 'ltd-form')

    textarea = form.one('textarea')
    assert 'maxlength' not in html
    assert textarea.attrs['name'] == 'comment'
    assert form.one('p', 'ltd-cnt').text == '0 / 500 Zeichen · nur Text'
    described = textarea.attrs['aria-describedby'].split()
    assert described == [
        form.one('p', 'ltd-help').attrs['id'],
        form.one('p', 'ltd-cnt').attrs['id'],
    ]
    assert form.one('label', 'ltd-lbl').text == 'Kommentar (optional)'
    assert form.one('label', 'ltd-lbl').attrs['for'] == textarea.attrs['id']
    assert 'aria-invalid' not in textarea.attrs

    # Over the limit the draft stays, nothing is cut, the counter is red.
    context = one_row(neon())
    draft = 'ä' * 501
    form_data = context['rows'][0]['ack']['form']
    form_data.update(
        open=True,
        draft=draft,
        counter_text=format_comment_counter(len(draft)),
        is_over=True,
        error_text='Kommentar ist zu lang: 501 von höchstens 500 Zeichen.',
    )
    action = refusal(
        context,
        error='invalid',
        draft=draft,
        draft_target=True,
        message=form_data['error_text'],
    )

    html = panel_html(env, context, action=action)

    assert 'maxlength' not in html
    form = root_of(html).one('form', 'ltd-form')
    assert 'data-open' in form.attrs
    assert form.one('textarea').text == draft
    assert form.one('textarea').attrs['aria-invalid'] == 'true'
    counter = form.one('p', 'ltd-cnt')
    assert 'is-over' in counter.classes
    assert counter.text == '501 / 500 Zeichen · nur Text'
    assert (
        form.one('textarea').attrs['aria-describedby'].split()[-1]
        == (form.one('p', 'ltd-err').attrs['id'])
    )
    # Shown in the form, so the panel adds no second notice or card.
    root = root_of(html)
    assert root.find_all('div', 'ltd-ban', role='alert') == []
    assert root.find_all('div', 'ltd-draft') == []


def test_a_refusal_that_hides_the_row_shows_the_notice_and_the_draft(
    env, german
):
    context = one_row(kupfer(ack='below'))
    action = refusal(
        context,
        message='Nicht festgehalten: Das Turnier wurde pausiert.',
        detail='Die Zeile zeigt jetzt den Server-Stand.',
        draft='Nachtbus wartet weiter.',
        draft_target=False,
        error='refused',
    )

    root = root_of(panel_html(env, context, action=action))

    banner = root.one('div', 'ltd-ban')
    assert banner.attrs['role'] == 'alert'
    assert 'b-err' in banner.classes
    assert banner.strings == [
        '!',
        'Nicht festgehalten: Das Turnier wurde pausiert.',
        'Die Zeile zeigt jetzt den Server-Stand.',
    ]
    card = root.one('div', 'ltd-draft')
    assert card.one('h2').text == 'Nicht gesendeter Entwurf'
    assert card.one('label').text == 'Entwurf (nur lesen)'
    assert card.one('textarea').attrs['readonly'] in (None, '', 'readonly')
    assert card.one('textarea').text == 'Nachtbus wartet weiter.'
    assert card.one('textarea').attrs['id'] == card.one('label').attrs['for']

    # Without a refusal there is neither.
    plain = root_of(panel_html(env, context))
    assert plain.find_all('div', 'ltd-ban') == []
    assert plain.find_all('div', 'ltd-draft') == []


# -------------------------------------------------------------- the tiles


def test_tier_tiles_toggle_and_zero_tiles_are_not_links(env, german):
    context = context_of(
        [neon_50(), kupfer_50()],
        counts=(0, 2, 1),
        query={'state': 'tier-yellow', 'sort': 'wait', 'page': 3},
        total=120,
        pages=3,
    )
    root = root_of(panel_html(env, context))

    red, yellow, green = root.find_all('li', 'ltd-st')

    # Zero is not a link and has no label to announce.
    assert 'is-zero' in red.classes
    assert red.find('a') is None
    assert red.find('span').attrs.get('aria-label') is None
    assert red.one('b', 'ltd-stc').text == '0'

    # The active tile points at the same list without the tier, on page 1.
    assert 'is-on' in yellow.classes
    link = yellow.one('a')
    assert link.attrs['aria-current'] == 'true'
    assert link.attrs['aria-label'] == (
        '2 Begegnungen: Verzögerung prüfen (15–44 min). '
        'Filter aufheben, alle Begegnungen zeigen'
    )
    query = query_of(link.attrs['href'])
    assert 'state' not in query
    assert 'page' not in query
    assert query['sort'] == ['wait']

    # Another tile switches the tier and starts again at page 1.
    assert 'is-on' not in green.classes
    other = green.one('a')
    assert 'aria-current' not in other.attrs
    assert other.attrs['aria-label'] == (
        '1 Begegnung: Unter Warnschwelle (unter 15 min). '
        'Liste auf diese Stufe filtern'
    )
    query = query_of(other.attrs['href'])
    assert query['state'] == ['tier-green']
    assert query['sort'] == ['wait']
    assert 'page' not in query

    # The counts are the whole scope: they do not follow the page.
    assert [t.one('b', 'ltd-stc').text for t in (red, yellow, green)] == [
        '0',
        '2',
        '1',
    ]
    assert root.one('p', 'ltd-lgn').text == (
        'Grün bedeutet nicht spielbereit. Bereitschaft steht separat.'
    )


# -------------------------------------------------------- the empty states


def test_empty_states_and_empty_page_are_distinct(env, german):
    def card(reason=None, surface='admin', **fields):
        context = context_of(
            [], surface=surface, reason=reason, total=0, **fields
        )
        return root_of(panel_html(env, context))

    # S6c: nothing is assigned. Scope stays, tiles and filters go.
    bare = card('no_assignment')
    assert bare.one('h2').text == (
        'Dir sind auf dieser Party keine Turniere zugewiesen.'
    )
    assert bare.find('section', 'ltd-sum') is None
    assert bare.find('form', 'ltd-filters') is None
    assert bare.find('div', 'ltd-count') is None
    assert bare.one('form', 'ltd-scope')
    assert [a.text for a in bare.find_all('a', 'ltd-btn')] == [
        'Alle Turniere dieser Party anzeigen'
    ]

    # S6d: nobody waits at the moment.
    demand = card('no_current_demand')
    assert demand.one('div', 'ltd-empty').one('h2').text == (
        'Gerade ist keine Begegnung fällig.'
    )
    assert [a.text for a in demand.one('div', 'ltd-frow').find_all('a')] == [
        'Kommende Begegnungen anzeigen',
        'Alle Begegnungen',
    ]
    assert demand.find('ol', 'ltd-rows') is None
    assert demand.find('div', 'ltd-count') is None

    # S6e: matches exist but none is actionable; the breakdown is counted.
    fixtures = card('no_actionable_fixtures')
    body = fixtures.one('div', 'ltd-empty').one('p').text
    assert body == (
        '4 Begegnungen in deinen Turnieren haben keinen aktuellen Bedarf: '
        '2 pausiert, 1 vor dem Turnierstart, 1 unvollständig.'
    )
    assert [a.text for a in fixtures.one('div', 'ltd-frow').find_all('a')] == [
        'Alle Begegnungen anzeigen'
    ]

    # S6f: the filters match nothing; they stay editable.
    choice = tournament_ref('Orbit-Lobby')
    filtered = card(
        'no_filter_matches',
        query={'tournament_id': choice.tournament_id, 'state': 'ready-both'},
        choices=[choice],
    )
    assert filtered.one('div', 'ltd-empty').one('h2').text == (
        'Keine Begegnung passt zu diesen Filtern.'
    )
    assert filtered.one('div', 'ltd-empty').one('p').text == (
        'Orbit-Lobby · Beide bereit · Aktuell fällig'
    )
    assert filtered.one('form', 'ltd-filters')
    assert filtered.one('section', 'ltd-sum')
    assert [a.text for a in filtered.one('div', 'ltd-frow').find_all('a')] == [
        'Filter zurücksetzen'
    ]

    # S6h: the requested page is gone after a change. It is not clamped:
    # the count stays, the card says which page, one link goes back.
    gone = context_of(
        [],
        query={'page': 2, 'sort': 'wait'},
        page=2,
        pages=1,
        total=5,
        counts=(0, 2, 3),
    )
    page = root_of(panel_html(env, gone))
    assert page.one('h2', id='ltd-count').text == '5 Begegnungen'
    assert page.one('div', 'ltd-count').one('p').text == (
        'Aktuell fällig · Zugewiesene Turniere · Seite 2 gibt es nicht mehr'
    )
    empty = page.one('div', 'ltd-empty')
    assert empty.one('h2').text == 'Seite 2 ist jetzt leer.'
    assert empty.one('p').text == (
        'Seit deinem letzten Stand sind Begegnungen bestätigt worden. '
        'Es gibt nur noch 1 Seite.'
    )
    (back,) = empty.find_all('a', 'ltd-btn')
    assert back.text == 'Zu Seite 1'
    assert 'page' not in query_of(back.attrs['href'])
    assert query_of(back.attrs['href'])['sort'] == ['wait']
    assert page.find('ol', 'ltd-rows') is None
    assert page.one('section', 'ltd-sum')

    # The five states say five different things.
    headings = {
        bare.one('div', 'ltd-empty').one('h2').text,
        demand.one('div', 'ltd-empty').one('h2').text,
        filtered.one('div', 'ltd-empty').one('h2').text,
        empty.one('h2').text,
    }
    assert len(headings) == 4
    assert fixtures.one('div', 'ltd-empty').one('p').text != (
        demand.one('div', 'ltd-empty').one('p').text
    )


# ------------------------------------------------------------ native forms


def test_native_forms_work_without_script_and_keep_the_validated_query(
    env, german
):
    choice = tournament_ref('Kupfer-Cup')
    context = context_of(
        [neon_50(), kupfer_50()],
        query={
            'view': 'all',
            'sort': 'wait',
            'state': 'conflict',
            'tournament_id': choice.tournament_id,
            'page': 3,
        },
        choices=[choice],
        page=3,
        total=120,
        pages=5,
    )
    root = root_of(panel_html(env, context))
    list_path = '/lan-tournaments/for_party/pixelnacht-36/dashboard'

    def hidden(form: Node) -> dict[str, str]:
        return {
            i.attrs['name']: i.attrs['value']
            for i in form.find_all('input', type='hidden')
        }

    # Filters: a GET form to the list, even when the page is a POST answer.
    filters = root.one('form', 'ltd-filters')
    assert filters.attrs['method'] == 'get'
    assert filters.attrs['action'] == list_path
    assert filters.attrs['role'] == 'search'
    assert hidden(filters) == {'scope': 'assigned', 'view': 'all'}
    names = [s.attrs['name'] for s in filters.find_all('select')]
    assert names == ['tournament', 'state', 'sort']
    selected = {
        s.attrs['name']: [
            o.attrs['value'] for o in s.find_all('option', selected=True)
        ]
        for s in filters.find_all('select')
    }
    assert selected == {
        'tournament': [str(choice.tournament_id)],
        'state': ['conflict'],
        'sort': ['wait'],
    }
    assert filters.one('button', 'ltd-btn').attrs['type'] == 'submit'
    # Applying starts at page 1: the form has no page field.
    assert 'page' not in hidden(filters)

    # Manual refresh: a GET form that returns to the same validated page.
    refresh = root.one('form', 'ltd-refresh')
    assert refresh.attrs['method'] == 'get'
    assert refresh.attrs['action'] == list_path
    assert hidden(refresh) == {
        'scope': 'assigned',
        'view': 'all',
        'state': 'conflict',
        'sort': 'wait',
        'tournament': str(choice.tournament_id),
        'page': '3',
    }
    assert refresh.one('button').attrs['type'] == 'submit'
    assert refresh.one('button').text == 'Jetzt aktualisieren'

    # Scope: a GET form of its own that drops the page and the scope field.
    scope = root.one('form', 'ltd-scope')
    assert scope.attrs['method'] == 'get'
    assert [
        (r.attrs['name'], r.attrs['value'], 'checked' in r.attrs)
        for r in scope.find_all('input', type='radio')
    ] == [('scope', 'assigned', True), ('scope', 'all', False)]
    assert 'page' not in hidden(scope)
    assert 'scope' not in hidden(scope)

    # Links keep the query and move one thing.
    pager = root.one('nav', 'ltd-pager')
    for link in pager.find_all('a'):
        query = query_of(link.attrs['href'])
        assert query['sort'] == ['wait']
        assert query['state'] == ['conflict']
        assert query['view'] == ['all']
    current = pager.one('a', 'is-cur')
    assert current.attrs['aria-current'] == 'page'
    assert current.text == '3'
    for view in root.one('nav', 'ltd-views').find_all('a'):
        assert query_of(view.attrs['href']).get('page') is None
        assert query_of(view.attrs['href'])['sort'] == ['wait']
    assert (
        root.one('nav', 'ltd-views').one('a', **{'aria-current': 'page'}).text
        == 'Alle Begegnungen'
    )

    # Pin and check forms are native POSTs (the rows test proves the fields).
    for form in root.find_all('form', 'ltd-pin'):
        assert form.attrs['method'] == 'post'
    for form in root.find_all('form', 'ltd-form'):
        assert form.attrs['method'] == 'post'
    # The controls only the script can use are hidden until it runs.
    for button in root.find_all('button', **{'data-ack-open': True}):
        assert 'hidden' in button.attrs

    # The site has no scope switch: one fixed line, no radios, no scope field.
    site = root_of(panel_html(env, context_of([neon_50()], surface='site')))
    assert site.find('form', 'ltd-scope') is None
    assert site.find_all('input', type='radio') == []
    assert site.one('p', 'ltd-scope').one('b').text == 'Zugewiesene Turniere'
    assert 'scope' not in hidden(site.one('form', 'ltd-refresh'))
    assert 'scope' not in hidden(site.one('form', 'ltd-filters'))


def test_the_panel_names_the_script_contract_in_its_root(env, german):
    context = context_of([neon_50()], query={'page': 1})
    root = root_of(panel_html(env, context))

    assert root.attrs['data-surface'] == 'admin'
    assert root.attrs['data-poll-url'] == context['poll']['url']
    assert root.attrs['data-poll-seconds'] == '30'
    assert root.attrs['data-as-of'] == context['freshness']['as_of']
    assert root.attrs['data-login-url'] == '/admin-auth/login'
    assert json.loads(root.attrs['data-labels']) == context['labels']
    assert root.attrs['aria-label'] == 'Orga-Dashboard'
    assert 'is-site' not in root.classes

    site = root_of(panel_html(env, context_of([], surface='site')))
    assert site.attrs['data-login-url'] == '/auth/login'
    assert 'is-site' in site.classes

    # Without a script the status line says the refresh is manual.
    status = root.one('p', 'ltd-fst')
    assert status.attrs['aria-live'] == 'polite'
    assert status.one('span', 'ltd-fst-manual').text == (
        'Ohne JavaScript: nur manuelle Aktualisierung'
    )
    assert status.one('span', 'ltd-fst-auto').text == 'Automatisch alle 30 s'
    assert root.one('div', 'ltd-sr').attrs['aria-live'] == 'polite'
    assert 'data-live' in root.one('div', 'ltd-sr').attrs
    # `.ltd-fst-auto` is shown only once the script has set `is-js`.
    assert declared('.lt-dashboard .ltd-fst-auto')['display'] == 'none'
    assert declared('.lt-dashboard.is-js .ltd-fst-manual')['display'] == 'none'


def test_the_check_form_is_open_in_the_markup_and_closes_under_the_script():
    plain = declared('.lt-dashboard .ltd-form')
    assert plain['display'] == 'flex'
    closed = declared('.lt-dashboard.is-js .ltd-form:not([data-open])')
    assert closed == {'display': 'none'}
    band = declared(
        '.lt-dashboard.is-js .ltd-ack:not(:has(.ltd-rec))'
        ':not(:has(.ltd-form[data-open]))'
    )
    assert band == {'display': 'none'}


# ------------------------------------------------------------- the wrappers


PARTY_OBJECT = SimpleNamespace(
    id='pixelnacht-36',
    title='Pixelnacht 36',
    starts_at=datetime(2026, 10, 2, 12, 0),
    ends_at=datetime(2026, 10, 5, 10, 0),
)


def page_of_wrapper(env, template, **context) -> Node:
    values = dict(
        dashboard=None,
        party=PARTY_OBJECT,
        action=None,
        unavailable=None,
    )
    values.update(context)
    return parse(env.get_template(template).render(**values))


def test_the_admin_wrapper_sits_in_the_party_layout(env, german):
    page = page_of_wrapper(
        env, ADMIN_PAGE, dashboard=context_of([neon_50(), kupfer_50()])
    )

    tabs = page.one('nav', id='tabs')
    assert tabs.attrs['data-tab'] == 'dashboard'
    assert tabs.attrs['data-party'] == 'Pixelnacht 36'
    assert page.one('title').text == (
        'Orga-Dashboard | LAN-Turniere | Pixelnacht 36'
    )
    main = page.one('main')
    assert main.one('h1', 'title').text == 'Orga-Dashboard'
    assert main.one('div', 'subtitle').text == (
        'Pixelnacht 36 · 2. Oktober 2026 – 5. Oktober 2026'
    )
    # One panel, the canonical one, and no second copy of its logic.
    assert len(main.find_all('section', 'lt-dashboard')) == 1
    assert len(main.find_all('li', 'ltd-row')) == 2
    links = [n.attrs['href'] for n in page.find_all('link')]
    assert links == ['/static/style/lan_tournament_dashboard.css']
    scripts = page.find_all('script')
    assert [s.attrs['src'] for s in scripts] == [
        '/static/behavior/lan_tournament_dashboard.js'
    ]
    assert 'defer' in scripts[0].attrs


def test_the_site_wrapper_has_the_breadcrumb_and_the_orga_line(env, german):
    page = page_of_wrapper(
        env, SITE_PAGE, dashboard=context_of([neon_50()], surface='site')
    )

    main = page.one('main')
    crumbs = main.one('nav', 'breadcrumbs')
    assert [li.text for li in crumbs.find_all('li')] == [
        'Turniere',
        'Orga-Dashboard',
    ]
    assert crumbs.one('a').attrs['href'] == '/lan-tournaments/'
    assert main.one('h1', 'title').text == 'Orga-Dashboard'
    assert main.one('div', 'subtitle').text == (
        'Pixelnacht 36 · 2. Oktober 2026 – 5. Oktober 2026 · nur für Orgas'
    )
    panel = main.one('section', 'lt-dashboard')
    assert 'is-site' in panel.classes
    assert panel.attrs['data-surface'] == 'site'
    assert page.find_all('section', 'lt-dashboard')[0] is panel


def test_the_wrappers_render_one_panel_for_both_surfaces(env, german):
    rows = [neon_50(), kupfer_50(), orbit_50()]
    admin = page_of_wrapper(env, ADMIN_PAGE, dashboard=context_of(rows)).one(
        'section', 'lt-dashboard'
    )
    site = page_of_wrapper(
        env, SITE_PAGE, dashboard=context_of(rows, surface='site')
    ).one('section', 'lt-dashboard')

    def shape(node: Node) -> list[tuple[str, tuple[str, ...]]]:
        return [
            (
                n.tag,
                tuple(c for c in n.classes if c not in ('is-site',)),
            )
            for n in node.walk()
        ]

    admin_rows = shape(admin.one('ol', 'ltd-rows'))
    site_rows = shape(site.one('ol', 'ltd-rows'))
    assert admin_rows == site_rows


@pytest.mark.parametrize('template', [ADMIN_PAGE, SITE_PAGE])
def test_the_unavailable_page_is_the_same_for_a_missing_and_a_hidden_match(
    env, german, template
):
    unavailable = dict(
        heading='Diese Begegnung ist nicht verfügbar.',
        detail='Sie existiert nicht oder du kannst sie nicht sehen.',
        back_label='Zurück zum Orga-Dashboard',
        back_url='/lan-tournaments/orga-dashboard',
    )

    def render(dashboard) -> str:
        return env.get_template(template).render(
            dashboard=dashboard,
            party=PARTY_OBJECT,
            action=None,
            unavailable=unavailable,
        )

    html = render(None)

    # Whatever the list would hold, the page says the same and no more.
    assert html == render(context_of([neon_50(), kupfer_50()]))
    page = parse(html)
    card = page.one('div', 'ltd-empty')
    assert card.one('h2').text == 'Diese Begegnung ist nicht verfügbar.'
    assert card.one('a', 'ltd-btn').text == 'Zurück zum Orga-Dashboard'
    assert card.one('a', 'ltd-btn').attrs['href'] == (
        '/lan-tournaments/orga-dashboard'
    )
    # Nothing of a list: no panel root, no row, no form, no script data.
    assert page.find_all(**{'data-lt-dashboard-root': True}) == []
    assert page.find_all('li', 'ltd-row') == []
    assert page.find_all('form') == []


# --------------------------------------------------------- the stylesheet


SELECTOR_CLASS = re.compile(r'\.(-?[A-Za-z_][\w-]*)')
ALLOWED_CLASS = re.compile(
    r'lt-dashboard|ltd-[\w-]+|is-[\w-]+|t-[\w-]+|s-[a-z]+|b-[a-z]+|rs-[012]'
    r'|pri'
)


def test_dashboard_css_is_namespaced():
    selectors = [s for rule in RULES for s in rule.selectors]
    assert selectors

    # Every rule hangs under the root class.
    for selector in selectors:
        assert re.match(r'\.lt-dashboard(?![\w-])', selector), selector

    # Inner names are `ltd-*`; nothing may meet `.lt-*` of the other sheets.
    for selector in selectors:
        for name in SELECTOR_CLASS.findall(re.sub(r'\[[^\]]*\]', '', selector)):
            assert ALLOWED_CLASS.fullmatch(name), (selector, name)

    # Tokens and animations are `ltd-*` too.
    declarations = [d for rule in RULES for d in rule.declarations]
    custom = {name for name, _ in declarations if name.startswith('--')}
    assert custom
    assert all(name.startswith('--ltd-') for name in custom), custom
    animations = [
        rule.context[-1].split()[1]
        for rule in RULES
        if rule.context and rule.context[-1].startswith('@keyframes')
    ]
    assert animations
    assert all(name.startswith('ltd-') for name in animations)

    # The root is the container of the reflow, and it declares its tokens
    # itself instead of inheriting those of `lan_tournament.css`.
    assert declared('.lt-dashboard')['container-type'] == 'inline-size'
    assert '--ltd-ink' in tokens('.lt-dashboard')
    assert '--ltd-ink' in tokens('.lt-dashboard.is-site')
    assert set(tokens('.lt-dashboard.is-site')) <= set(tokens('.lt-dashboard'))

    # A colour literal belongs to a token; the rules use tokens only.
    for rule in RULES:
        for name, value in rule.declarations:
            if not name.startswith('--ltd-'):
                assert not re.search(r'#[0-9a-fA-F]{3,8}\b', value), (
                    rule.selectors,
                    name,
                    value,
                )

    # Nothing of the build or the source ships in the sheet or the markup.
    forbidden = re.compile(
        r'claude|anthropic|figma|mockup|skin-|\.kevins|/workspace'
        r'|https?://|f03/|Source Sans',
        re.IGNORECASE,
    )
    for source in SOURCES:
        text = source.read_text(encoding='utf-8')
        assert not forbidden.search(text), (source.name, forbidden.search(text))


def test_the_panel_and_wrappers_use_no_translation_call_and_no_unsafe_output():
    for source in SOURCES[:3]:
        text = source.read_text(encoding='utf-8')
        assert '|safe' not in text
        assert 'Markup(' not in text
        assert 'autoescape' not in text
    panel = SOURCES[0].read_text(encoding='utf-8')
    assert '_(' not in panel
    assert 'gettext' not in panel
    # A wrapper's own copy is the title and the orga line only.
    for wrapper in SOURCES[1:3]:
        text = wrapper.read_text(encoding='utf-8')
        calls = set(re.findall(r"_\('([^']+)'\)", text))
        assert calls <= {
            'Orga dashboard',
            'LAN Tournaments',
            'Tournaments',
            'only for orgas',
        }


@pytest.mark.parametrize(
    'pair',
    [
        ('ink', 'bg'),
        ('ink', 'surf'),
        ('ink', 'surf-2'),
        ('ink', 'field'),
        ('ink', 'stale'),
        ('soft', 'bg'),
        ('soft', 'surf'),
        ('soft', 'surf-2'),
        ('soft', 'stale'),
        ('mute', 'bg'),
        ('mute', 'surf'),
        ('mute', 'surf-2'),
        ('mute', 'stale'),
        ('link', 'bg'),
        ('link', 'surf'),
        ('link', 'surf-2'),
        ('link', 'btn'),
        ('link', 'y-bg'),
        ('link', 'conf-bg'),
        ('link', 'info-bg'),
        ('ready', 'surf'),
        ('ready', 'surf-2'),
        ('pin', 'surf'),
        ('pin', 'surf-2'),
        ('rev', 'surf'),
        ('rev', 'surf-2'),
        ('err', 'bg'),
        ('err', 'surf'),
        ('err', 'surf-2'),
        ('err', 'field'),
        ('ok', 'surf'),
        ('info', 'surf'),
        ('warn-ink', 'surf'),
        ('g-ink', 'g-bg'),
        ('y-ink', 'y-bg'),
        ('r-ink', 'r-bg'),
        ('conf', 'conf-bg'),
        ('err', 'err-bg'),
        ('ok', 'ok-bg'),
        ('info', 'info-bg'),
        ('ink', 'err-bg'),
        ('ink', 'ok-bg'),
        ('ink', 'info-bg'),
        ('ink', 'btn'),
        ('ink', 'btn-h'),
        ('pri-ink', 'pri'),
        ('ink', 'y-bg'),
        ('soft', 'g-bg'),
    ],
    ids='-on-'.join,
)
@pytest.mark.parametrize('theme', ['.lt-dashboard', '.lt-dashboard.is-site'])
def test_site_dark_tokens_meet_contrast(theme, pair):
    """Text pairs of both token sets reach 4.5:1 (the site set is dark)."""
    palette = {**tokens('.lt-dashboard'), **tokens(theme)}
    foreground, background = (palette[f'--ltd-{name}'] for name in pair)

    assert contrast(foreground, background) >= 4.5, (theme, pair)


@pytest.mark.parametrize('theme', ['.lt-dashboard', '.lt-dashboard.is-site'])
def test_site_dark_tokens_meet_contrast_for_tiers_focus_and_borders(theme):
    """Tier borders, bars, focus and control edges reach 3:1 on every ground."""
    palette = {**tokens('.lt-dashboard'), **tokens(theme)}

    def color(name: str) -> str:
        return palette[f'--ltd-{name}']

    grounds = [
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
    for ground in grounds:
        # The focus ring (R41: the admin one was 2.99:1 on white).
        assert contrast(color('focus'), color(ground)) >= 3, (theme, ground)
    for tier in ('g', 'y', 'r'):
        # The tier bar and the tile frame sit on the card and on the ground.
        for ground in ('bg', 'surf'):
            assert contrast(color(f'{tier}-ink'), color(ground)) >= 3
    assert contrast(color('acc'), color('surf')) >= 3
    if theme.endswith('is-site'):
        # The site's control edges and the dark ground are derived from the
        # palette of the site: navy ground, light ink.
        assert contrast(color('line-s'), color('surf')) >= 3
        assert contrast(color('line-s'), color('field')) >= 3
        assert luminance(color('bg')) < 0.02
        assert luminance(color('ink')) > 0.8
    # The conflict links of the row are 44 px high (the design drew 32 px).
    assert declared('.lt-dashboard .ltd-cl a')['min-height'] == '44px'
