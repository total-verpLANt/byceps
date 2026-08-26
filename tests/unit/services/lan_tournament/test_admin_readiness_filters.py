"""Backend GET contexts/real templates with strict read-only mock boundaries.

Fixed batch evidence is not PostgreSQL statement or browser instrumentation.
"""

from dataclasses import replace
from html import unescape
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from flask import Flask, g, url_for
from flask_babel import Babel
from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)
import pytest

from byceps.services.lan_tournament.blueprints.admin import views as admin
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.navigation import Navigation
from byceps.util.framework import templating
from byceps.util.templatefilters import dim
from tests.unit.services.lan_tournament.test_site_readiness_filters import (
    _personal_rows,
    site,  # noqa: F401 -- accepted shared read-boundary fixture
)
from tests.unit.services.lan_tournament.test_readiness_surface_policy import (
    NOW,
    _row,
)


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path('/tmp/opencode/f04-execution/issue20/render-artifacts')  # noqa: S108 -- approved evidence directory


@pytest.fixture
def backend(site, monkeypatch):  # noqa: F811 -- imported fixture
    app = Flask(__name__)
    app.config.update(
        TESTING=True, SECRET_KEY='backend-read-only', BABEL_DEFAULT_LOCALE='en'
    )
    Babel(app)
    app.register_blueprint(admin.blueprint, url_prefix='/lan-tournaments')
    monkeypatch.setattr(
        admin, '_get_tournament_or_404', lambda _: site.tournament
    )
    monkeypatch.setattr(
        admin.party_service,
        'get_party',
        lambda _: SimpleNamespace(id=site.tournament.party_id, title='Party'),
    )
    monkeypatch.setattr(
        admin.tournament_match_service, 'has_matches', lambda _: bool(site.rows)
    )
    monkeypatch.setattr(
        admin.tournament_service,
        'get_participant_counts_for_tournaments',
        lambda _: {},
    )
    monkeypatch.setattr(admin, 'resolve_winner_display_name', lambda _: None)
    monkeypatch.setattr(admin, 'resolve_podium_display_names', lambda _: {})
    monkeypatch.setattr(
        admin,
        'start_gate',
        lambda _: SimpleNamespace(state='ready', notices=()),
    )
    names, hover = Mock(return_value=({}, {})), Mock(return_value=({}, {}))
    monkeypatch.setattr(admin, 'build_contestant_name_lookups', names)
    monkeypatch.setattr(admin, 'build_hover_lookups', hover)
    monkeypatch.setattr(admin, '_ffa_cut_tie_context', lambda _: {})
    monkeypatch.setattr(admin, 'ffa_grand_final_offer', lambda _: None)
    forbidden = []
    for owner, name in (
        (admin.tournament_match_service, 'get_contestants_for_match'),
        (admin.tournament_match_service, 'get_match_readiness'),
        (admin.tournament_repository, 'get_match_pairing'),
        (admin.user_service, 'get_users_indexed_by_id'),
        (admin.tournament_repository, 'commit_session'),
        (admin.tournament_repository, 'rollback_session'),
    ):
        mock = Mock(
            side_effect=AssertionError('Forbidden GET collaborator: ' + name)
        )
        monkeypatch.setattr(owner, name, mock)
        forbidden.append(mock)

    def context(surface='matches_for_tournament', query=''):
        with app.test_request_context('/lan-tournaments/' + query):
            g.user = SimpleNamespace(
                id=site.user_id,
                authenticated=True,
                has_permission=lambda _: False,
            )
            g.current_locale = SimpleNamespace(language='en')
            return getattr(admin, surface).__wrapped__.__wrapped__(
                str(site.tournament.id)
            )

    def render(
        context_,
        surface='matches_for_tournament',
        theme='generic',
        administrate=False,
    ):
        paths = []
        if theme != 'generic':
            paths.append(ROOT / 'sites/totalverplant-36/template_overrides')
        paths.extend(
            sorted(
                (ROOT / 'byceps/services').glob('*/blueprints/admin/templates')
            )
        )
        paths.extend(
            sorted(
                (ROOT / 'byceps/services').glob('*/blueprints/common/templates')
            )
        )
        env = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader(
                        {
                            'layout/admin/lan_tournament.html': '{% block head %}{% endblock %}{% block before_body %}{% endblock %}{% block body %}{% endblock %}{% block scripts %}{% endblock %}'
                        }
                    ),
                    FileSystemLoader(paths),
                ]
            ),
            undefined=StrictUndefined,
            autoescape=True,
        )
        env.globals.update(
            _=lambda text, **values: text % values if values else text,
            url_for=url_for,
            g=g,
            Navigation=Navigation,
        )
        env.filters.update(
            dim=dim,
            dateformat=lambda value, *a: value.date().isoformat(),
            timeformat=lambda value, *a: value.time().isoformat(),
        )
        with app.test_request_context(
            '/lan-tournaments/tournaments/' + str(site.tournament.id)
        ):
            g.user = SimpleNamespace(
                has_permission=lambda permission: (
                    administrate and permission == 'lan_tournament.administrate'
                )
            )
            html = env.get_template(
                'admin/lan_tournament/' + surface + '.html'
            ).render(**context_)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / f'{surface}-{theme}-{uuid4()}.html').write_text(html)
        return html

    return SimpleNamespace(
        state=site,
        context=context,
        render=render,
        app=app,
        batches=(*site.batches[:3], names, hover),
        forbidden=forbidden,
    )


BUCKETS = ('waiting', 'not_ready', 'partially_ready', 'both_ready', 'no_readiness', 'finished')
ICONS = {'waiting': '⧗', 'not_ready': '○', 'partially_ready': '◐', 'both_ready': '✓', 'no_readiness': '⧗', 'finished': '■', 'all': '◉'}
LABELS = {'waiting': 'Waiting for opponent', 'not_ready': 'Not ready', 'partially_ready': 'Partially ready', 'both_ready': 'Both ready', 'no_readiness': 'Open (no readiness)', 'finished': 'Finished', 'all': 'All'}
TAB = re.compile(r'<a href="([^"]+)"( aria-current="page")?>\s*<span class="lt-match-tabs__icon" aria-hidden="true">(.)</span>\s*(.+?)\s*<span class="lt-match-tabs__count">(\d+)</span>\s*</a>')


def _tabs(html):
    nav = re.search(r'<nav class="(lt-match-tabs[^"]*)" aria-label="([^"]*)">(.*?)</nav>', html, re.S)
    return nav.group(1), nav.group(2), TAB.findall(nav.group(3))


def _text(fragment):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', '', fragment))).strip()


# fmt: off
@pytest.mark.parametrize('only', ['', 'ready', 'playable', 'open', *BUCKETS, 'all', 'unknown'])
# fmt: on
def test_site_and_admin_share_filter_counts(backend, only):
    state = backend.state
    state.rows = [_row(state.tournament, count=count, claims=claims) for count, claims in ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))]
    buckets = ['waiting', 'waiting', 'not_ready', 'partially_ready', 'both_ready']
    query = '?only=' + only if only else ''
    result = backend.context(query=query)
    public = state.context(query=query)
    quantities = dict(all=5, waiting=2, not_ready=1, partially_ready=1, both_ready=1, no_readiness=0, finished=0)
    assert result['match_quantities'] == public['match_quantities'] == quantities
    assert sum(quantities[bucket] for bucket in BUCKETS) == quantities['all']
    assert result['match_filter_options'] == public['match_filter_options']
    assert [key for key, _, _ in result['match_filter_options']] == [*(b for b in BUCKETS if b != 'no_readiness'), 'all']
    offered = [key for key, _, _ in result['match_filter_options']]
    expected = only if only in offered else 'all'
    assert result['only'] == public['only'] == expected
    wanted = [m.id for (m, _, _), bucket in zip(state.rows, buckets, strict=True) if expected in ('all', bucket)]
    assert [e['match'].id for e in result['match_data']] == [e['match'].id for e in public['match_data']] == wanted
    _, _, tabs = _tabs(backend.render(result))
    assert [url.rsplit('=', 1)[1] for url, current, *_ in tabs if current] == [expected]
    overview = backend.context('view')
    assert overview['match_quantities'] == result['match_quantities']
    assert overview['match_filter_options'] == result['match_filter_options']
    assert set(result['readiness_by_match_id']) == {e['match'].id for e in result['match_data']}


def test_admin_default_filter_is_all(backend):
    state = backend.state
    state.rows = [_row(state.tournament, count=count, claims=claims) for count, claims in ((0, 0), (2, 0), (2, 1), (2, 2))]
    for query in ('', '?only=', '?only=playable', '?only=open', '?only=ready', '?only=unknown'):
        result = backend.context(query=query)
        assert result['only'] == 'all'
        assert [e['match'].id for e in result['match_data']] == [m.id for m, _, _ in state.rows]
    assert backend.context('view')['only'] == 'all'
    html = backend.render(backend.context())
    _, _, tabs = _tabs(html)
    assert [(key.rsplit('=', 1)[1], current) for key, current, *_ in tabs if current] == [('all', ' aria-current="page"')]
    assert 'No matches have been created yet.' not in html


# fmt: off
@pytest.mark.parametrize('surface', ['matches_for_tournament', 'view'])
@pytest.mark.parametrize('only', ['all', 'both_ready', 'finished'])
# fmt: on
def test_admin_tabs_have_design_icons(backend, surface, only):
    state = backend.state
    rows = [_row(state.tournament, count=count, claims=claims) for count, claims in ((1, 0), (2, 0), (2, 1), (2, 2), (2, 2))]
    state.rows = [*rows[:4], (replace(rows[4][0], confirmed_by=uuid4()), *rows[4][1:])]
    html = backend.render(backend.context(surface, '?only=' + only), surface)
    cls, label, tabs = _tabs(html)
    assert label == 'Filter matches'
    assert ('lt-match-tabs--compact' in cls) is (surface == 'view')
    # The overview lists every match, so its box stays on All.
    active = only if surface == 'matches_for_tournament' else 'all'
    assert [(url.rsplit('=', 1)[1], icon, text, int(count)) for url, _, icon, text, count in tabs] == [
        ('waiting', ICONS['waiting'], LABELS['waiting'], 1),
        ('not_ready', ICONS['not_ready'], LABELS['not_ready'], 1),
        ('partially_ready', ICONS['partially_ready'], LABELS['partially_ready'], 1),
        ('both_ready', ICONS['both_ready'], LABELS['both_ready'], 1),
        ('finished', ICONS['finished'], LABELS['finished'], 1),
        ('all', ICONS['all'], LABELS['all'], 5),
    ]
    assert [url.rsplit('=', 1)[1] for url, current, *_ in tabs if current] == [active]
    assert all(url.startswith(f'/lan-tournaments/tournaments/{state.tournament.id}/matches?only=') for url, *_ in tabs)
    assert 'main-tab' not in html and 'only=playable' not in html and 'only=open' not in html
    assert 'style/lan_tournament_match_admin.css' in html
    css = (ROOT / 'byceps/static/style/lan_tournament_match_admin.css').read_text()
    assert re.search(r'\.lt-match-tabs a \{[^}]*min-height: 44px;', css)
    assert re.search(r'\.lt-match-tabs a\[aria-current\] \{[^}]*border-bottom-color: var\(--ltc-tab-current\);', css)


def test_match_tab_rules_use_tokens_not_raw_colours():
    css = (ROOT / 'byceps/static/style/lan_tournament_match_admin.css').read_text()
    rules = re.findall(r'(?m)^(\.lt-match-tabs[^{]*)\{([^}]*)\}', css)
    assert {selector.strip() for selector, _ in rules} >= {
        '.lt-match-tabs', '.lt-match-tabs a', '.lt-match-tabs a:hover',
        '.lt-match-tabs a[aria-current]', '.lt-match-tabs__icon', '.lt-match-tabs__count',
    }
    for selector, body in rules:
        assert not re.search(r'#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\(', body), selector
        for prop, value in re.findall(r'([a-z-]*color):\s*([^;]+);', body):
            assert re.fullmatch(r'var\(--ltc-tab-[a-z]+\)', value.strip()), (selector, prop, value)
    used = {token for _, body in rules for token in re.findall(r'var\((--ltc-tab-[a-z]+)\)', body)}
    assert used == {'--ltc-tab-ink', '--ltc-tab-icon', '--ltc-tab-hover', '--ltc-tab-current', '--ltc-tab-count'}
    root = re.search(r'(?m)^:root \{([^}]*)\}', css).group(1)
    defined = dict(re.findall(r'(--ltc-tab-[a-z]+):\s*([^;]+);', root))
    assert set(defined) == used
    assert defined['--ltc-tab-ink'] == '#333333'
    assert defined['--ltc-tab-icon'] == '#777777'
    assert defined['--ltc-tab-hover'] == 'var(--color-disabled, #aaaaaa)'
    assert defined['--ltc-tab-current'] == 'var(--color-info, #2196f3)'
    assert defined['--ltc-tab-count'] == 'var(--dimmed-color, #888888)'


def test_admin_tabs_show_no_readiness_bucket_only_when_present(backend):
    state = backend.state
    state.tournament = replace(state.tournament, game_format=GameFormat.FREE_FOR_ALL)
    state.rows = [_row(state.tournament, claims=0)]
    tabs = _tabs(backend.render(backend.context()))[2]
    assert [(url.rsplit('=', 1)[1], icon, text, int(count)) for url, _, icon, text, count in tabs if 'no_readiness' in url] == [
        ('no_readiness', '⧗', 'Open (no readiness)', 1),
    ]
    assert [url.rsplit('=', 1)[1] for url, *_ in tabs] == [*BUCKETS, 'all']
    state.tournament = replace(state.tournament, game_format=GameFormat.ONE_V_ONE)
    tabs = _tabs(backend.render(backend.context()))[2]
    assert [url.rsplit('=', 1)[1] for url, *_ in tabs] == [b for b in BUCKETS if b != 'no_readiness'] + ['all']


def test_admin_overview_list_uses_round_match_labels(backend):
    state = backend.state
    rows = [_row(state.tournament, count=count, claims=claims) for count, claims in ((2, 0), (2, 0), (2, 1), (1, 0), (2, 2))]
    shapes = [(0, 0, None), (0, 1, None), (1, 0, None), (1, 1, None), (0, 0, Bracket.THIRD_PLACE)]
    state.rows = [(replace(match, round=r, match_order=order, bracket=bracket), *rest) for (match, *rest), (r, order, bracket) in zip(rows, shapes, strict=True)]
    state.rows[0] = (replace(state.rows[0][0], confirmed_by=uuid4()), *state.rows[0][1:])
    ids = [c.participant_id for _, contestants, _ in state.rows for c in contestants]
    users = {pid: SimpleNamespace(id=uuid4(), screen_name=f'Test_User_{n}', deleted=False) for n, pid in enumerate(ids, 1)}
    backend.batches[3].return_value = ({}, users)
    result = backend.context('view')
    html = backend.render(result, 'view')
    box = html.split('data-match-readiness-overview', 1)[1].split('</section>', 1)[0]
    items = re.findall(r'<li data-readiness-status="[^"]+">(.*?)</li>', box, re.S)
    assert [_text(item) for item in items] == [
        'R1 M1: Test_User_1 vs Test_User_2 — Confirmed',
        'R1 M2: Test_User_3 vs Test_User_4 — ○ Not ready',
        'R2 M1: Test_User_5 vs Test_User_6 — ◐ Partially ready · Side A ready · Side B not ready',
        'R2 M2: Test_User_7 vs — — Waiting for opponent',
        'P3 M1: Test_User_8 vs Test_User_9 — ✓ Both ready',
    ]
    listing = backend.render(backend.context(), 'matches_for_tournament')
    refs = [_text(cell) for cell in re.findall(r'<tr>\s*<td>(.*?)</td>', listing, re.S)]
    assert refs == [_text(item).split(':', 1)[0] for item in items]
    assert 'Match 1' not in box and 'Match 2' not in box


def test_admin_list_status_column_and_empty_state_follow_design(backend):
    state = backend.state
    rows = [_row(state.tournament, count=count, claims=claims) for count, claims in ((2, 0), (2, 1), (2, 1), (2, 2), (1, 0), (2, 2))]
    rows[2] = (replace(rows[2][0], ready_at_a=None, ready_at_b=NOW), *rows[2][1:])
    rows[5] = (replace(rows[5][0], confirmed_by=uuid4()), *rows[5][1:])
    state.rows = rows
    html = backend.render(backend.context('matches_for_tournament', '?only=all'))
    cells = re.findall(r'<td class="match-list-status"[^>]*>(.*?)</td>', html, re.S)
    assert [_text(cell) for cell in cells] == [
        '○ Not ready',
        '◐ Partially ready · Side A ready · Side B not ready',
        '◐ Partially ready · Side B ready · Side A not ready',
        '✓ Both ready',
        'Waiting for opponent',
        'Confirmed',
    ]
    assert all(f'<span aria-hidden="true">{icon}</span>' in cell for icon, cell in zip('○◐◐✓', cells[:4], strict=True))
    assert 'class="tag' in cells[5] and 'aria-hidden' not in cells[4] + cells[5]
    state.rows = rows[:5]
    empty = backend.render(backend.context('matches_for_tournament', '?only=finished'))
    assert "<p class='dimmed'>" in empty and 'No matches with this readiness state.' in empty
    assert '<th>' not in empty and 'aria-current="page"' in empty
    state.rows = []
    assert 'No matches have been created yet.' in backend.render(backend.context())
    state.tournament = replace(state.tournament, game_format=GameFormat.FREE_FOR_ALL)
    state.rows = [_row(state.tournament, count=2)]
    ffa = backend.render(backend.context('matches_for_tournament', '?only=no_readiness'))
    assert [_text(cell) for cell in re.findall(r'<td class="match-list-status"[^>]*>(.*?)</td>', ffa, re.S)] == ['Open (no readiness)']


def test_backend_and_site_share_tournament_scope(backend):
    state = backend.state
    rows = _personal_rows(state)
    public = state.context(query='?only=both_ready')
    result = backend.context(query='?only=both_ready')
    assert public['match_quantities'] == result['match_quantities'] == dict(all=4, waiting=0, not_ready=1, partially_ready=2, both_ready=1, no_readiness=0, finished=0)
    assert [e['match'].id for e in public['match_data']] == [e['match'].id for e in result['match_data']] == [rows[-1][0].id]
    assert backend.context('view')['match_quantities'] == result['match_quantities']


# fmt: off
@pytest.mark.parametrize('surface', ['matches_for_tournament', 'view'])
@pytest.mark.parametrize('theme', ['generic', 'totalverplant-36'])
@pytest.mark.parametrize('mode,label', [
    ('empty', 'Waiting for opponent'), ('one', 'Waiting for opponent'),
    ('assigned', 'Not ready'), ('a', 'Side A ready'),
    ('b', 'Side B ready'), ('both', 'Both ready'),
    ('confirmed', 'Confirmed'), ('defwin', 'DEFWIN'),
    ('completed', 'Completed'), ('cancelled', 'Cancelled'),
    ('ffa', 'Open (no readiness)'),
    ('stale', 'Not ready'),
])
# fmt: on
def test_backend_labels_outcome_precedence_and_privacy(backend, surface, theme, mode, label):
    state = backend.state
    if mode in {'completed', 'cancelled'}:
        state.tournament = replace(state.tournament, tournament_status=getattr(TournamentStatus, mode.upper()))
    if mode == 'ffa':
        state.tournament = replace(state.tournament, game_format=GameFormat.FREE_FOR_ALL)
    match, contestants, pairing = _row(state.tournament, count=0 if mode == 'empty' else 1 if mode in {'one', 'defwin'} else 2, claims=0 if mode == 'assigned' else 1 if mode in {'a', 'b'} else 2)
    if mode == 'b':
        match = replace(match, ready_at_a=None, ready_at_b=NOW)
    if mode in {'confirmed', 'defwin'}:
        match = replace(match, confirmed_by=uuid4())
    if mode == 'stale':
        pairing = replace(pairing, generation=3)
    state.rows = [(match, list(reversed(contestants)), pairing)]
    result = backend.context(surface, '?only=all')
    html = backend.render(result, surface, theme)
    status = html.split('data-readiness-status="', 1)[1].split('</td>' if surface == 'matches_for_tournament' else '</li>', 1)[0]
    assert label in status
    if mode in {'a', 'b'}:
        assert ('Side B not ready' if mode == 'a' else 'Side A not ready') in status
    if mode in {'confirmed', 'defwin', 'completed', 'cancelled', 'ffa', 'stale'}:
        assert 'Both ready' not in status and 'Side A ready' not in status
        assert result['match_quantities']['both_ready'] == 0
    assert all(str(actor) not in html for actor in (match.ready_by_a, match.ready_by_b) if actor)
    assert 'ready/claim' not in html and 'ready/revoke' not in html
    assert 'only=both_ready' in html and 'only=all' in html
    assert 'only=playable' not in html and 'only=open' not in html
    if surface == 'matches_for_tournament' and mode == 'one':
        assert 'defwin - Advanced' not in html


# fmt: off
@pytest.mark.parametrize('surface', ['matches_for_tournament', 'view', 'bracket'])
@pytest.mark.parametrize('base,playoff', [(GameFormat.FREE_FOR_ALL, GameFormat.ONE_V_ONE), (GameFormat.ONE_V_ONE, GameFormat.FREE_FOR_ALL)])
# fmt: on
def test_effective_phase_format_and_labels(backend, surface, base, playoff):
    state = backend.state
    state.tournament = replace(state.tournament, game_format=base, playoff_game_format=playoff)
    state.rows = [_row(state.tournament, phase=phase, claims=2) for phase in (1, 2)]
    result = backend.context(surface, '?only=all')
    for match, contestants, _ in state.rows:
        projection = result['readiness_by_match_id'][match.id]
        assert projection.supports_readiness is ((base if match.phase == 1 else playoff) == GameFormat.ONE_V_ONE)
        assert result['match_labels'][str(match.id)].startswith('Group' if match.phase == 1 else 'Playoffs')
        entry = next(e for e in result['match_data'] if e['match'].id == match.id)
        assert entry['contestants'] == contestants and entry['readiness'] is projection
    if surface != 'bracket':
        html = backend.render(result, surface)
        assert all(label in html for label in result['match_labels'].values())


# fmt: off
@pytest.mark.parametrize('surface', ['matches_for_tournament', 'view', 'bracket'])
@pytest.mark.parametrize('format_,mode', [(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION), (GameFormat.FREE_FOR_ALL, EliminationMode.DOUBLE_ELIMINATION)])
# fmt: on
def test_fixed_batch_growth_and_repeated_get_no_writes(backend, surface, format_, mode):
    state = backend.state
    state.tournament = replace(state.tournament, game_format=format_, elimination_mode=mode)
    observed = []
    for quantity in (10, 100):
        state.rows = [_row(state.tournament, claims=2) for _ in range(quantity)]
        original = list(state.rows)
        for _ in range(2):
            for batch in backend.batches:
                batch.reset_mock()
            result = backend.context(surface, '?only=all')
            assert len(result['readiness_by_match_id']) == quantity
            state.batches[2].assert_called_once_with([m.id for m, _, _ in state.rows])
            vector = tuple(batch.call_count for batch in backend.batches)
            assert vector == ((1, 1, 1, 1, 0) if surface == 'view' else (1, 1, 1, 1, 1))
            observed.append(vector)
            assert state.rows == original
    assert observed[0] == observed[1] == observed[2] == observed[3]
    for forbidden in backend.forbidden:
        forbidden.assert_not_called()


# fmt: off
@pytest.mark.parametrize('surface', ['matches', '', 'bracket'])
@pytest.mark.parametrize('permission', [None, 'lan_tournament.administrate', 'lan_tournament.maintain', 'lan_tournament.*'])
# fmt: on
def test_backend_permission_refuses_before_reads(backend, surface, permission):
    @backend.app.before_request
    def viewer():
        g.user = SimpleNamespace(authenticated=True, has_permission=lambda p: p == permission)
    response = backend.app.test_client().get(f'/lan-tournaments/tournaments/{backend.state.tournament.id}' + ('/' + surface if surface else ''))
    assert response.status_code == 403
    for batch in backend.batches:
        batch.assert_not_called()


def test_bracket_handoff_is_canonical_and_allowlisted(backend):
    state = backend.state
    state.rows = [_row(state.tournament, claims=1)]
    result = backend.context('bracket')
    entry = result['match_data'][0]
    assert entry['readiness'] is result['readiness_by_match_id'][entry['match'].id]
    assert entry['readiness_display']['label'] == 'Side A ready'
    assert set(entry['readiness_display']) == {'supported', 'status', 'label', 'ready_sides', 'side_labels', 'assignment_complete', 'assigned_contestant_count', 'original_occupied_since', 'pairing_started_at', 'ready_at_a', 'ready_at_b'}
    assert not {'readiness_history', 'readiness_history_users_by_id', 'can_view_readiness_history'} & result.keys()
    assert entry['contestants'] == state.rows[0][1]
    assert entry['has_pending_feeder'] is False


# fmt: off
@pytest.mark.parametrize('surface', ['matches_for_tournament', 'view'])
# fmt: on
def test_registered_backend_get_view_permission_and_no_writes(backend, monkeypatch, surface):
    state = backend.state
    state.rows = [_row(state.tournament, claims=2)]
    monkeypatch.setattr(templating, 'render_template', lambda name, **context: backend.render(context, surface))

    @backend.app.before_request
    def viewer():
        g.user = SimpleNamespace(id=state.user_id, authenticated=True, has_permission=lambda p: p == 'lan_tournament.view')
        g.current_locale = SimpleNamespace(language='en')

    path = f'/lan-tournaments/tournaments/{state.tournament.id}' + ('/matches?only=all' if surface == 'matches_for_tournament' else '')
    client = backend.app.test_client()
    for _ in range(2):
        response = client.get(path)
        assert response.status_code == 200
        assert 'Both ready' in response.get_data(as_text=True)
        assert 'Set-Cookie' not in response.headers
    for forbidden in backend.forbidden:
        forbidden.assert_not_called()
    assert state.batches[2].call_count == 2
