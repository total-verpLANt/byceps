"""Actual inherited no-JS HTML with canonical GET contexts and mocked reads."""

from dataclasses import replace
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from flask import Flask, g, request as flask_request, url_for, get_flashed_messages
from flask_babel import Babel
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest

from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus
from byceps.util.result import Ok
from tests.unit.services.lan_tournament.test_readiness_surface_policy import NOW, _row
from tests.unit.services.lan_tournament.test_site_group_standings_render import _ranking
from tests.unit.services.lan_tournament.test_site_readiness_filters import site as site


ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(params=['generic', 'totalverplant-36'])
def render(site, request, tmp_path):
    theme = request.param
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='bracket-render-test', BABEL_DEFAULT_LOCALE='en')
    Babel(app)
    app.register_blueprint(views.blueprint, url_prefix='/lan-tournaments')
    for endpoint in (
        'user_settings.view', 'user_profile.view', 'dashboard.index',
        'ticketing.index_mine', 'shop_orders.index', 'authn_login.log_in_form',
        'authn_login.log_out', 'authn_login.log_in',
    ):
        app.add_url_rule('/shell/' + endpoint, endpoint, lambda: '', methods=['GET', 'POST'])
    paths = [ROOT / 'byceps/services/lan_tournament/blueprints/site/templates']
    if theme != 'generic':
        paths.insert(0, ROOT / 'sites/totalverplant-36/template_overrides')
    paths.extend(sorted((ROOT / 'byceps/services').glob('*/blueprints/site/templates')))
    paths.extend(sorted((ROOT / 'byceps/services').glob('*/blueprints/common/templates')))
    env = Environment(loader=FileSystemLoader(paths), undefined=StrictUndefined, autoescape=True)
    env.globals.update(
        _=lambda text, **values: text % values if values else text,
        ngettext=lambda single, plural, n, **values: (single if n == 1 else plural) % values,
        url_for=url_for, url_for_site_file=lambda filename: '/site/' + filename,
        g=g, request=flask_request, now=NOW,
        get_flashed_messages=get_flashed_messages, get_nav_menu_items=lambda _: [],
    )
    env.filters.update(
        dateformat=lambda value, *a: value.date().isoformat(),
        timeformat=lambda value, *a: value.time().isoformat(),
        numberformat=str,
    )

    def render_context(context, *, macro=None):
        # Native tournament routes are registered, not synthetic URL stubs.
        with app.test_request_context('/lan-tournaments/'):
            g.user = SimpleNamespace(authenticated=False, has_permission=lambda _: False)
            g.party = SimpleNamespace(id=site.tournament.party_id)
            g.current_locale = SimpleNamespace(language='en')
            g.site = SimpleNamespace(is_intranet=False)
            if macro:
                html = str(env.get_template('site/lan_tournament/_standings.html').module.render_phase_match_list(
                    context['playoff_matches'], context['readiness_by_match_id'],
                ))
            else:
                html = env.get_template('site/lan_tournament/bracket.html').render(**context)
            (tmp_path / (theme + ('-phase' if macro else '-bracket') + '.html')).write_text(html)
            return html

    return render_context


def _index(html):
    return html.split('data-lt-readiness-index>', 1)[1].split('</section>', 1)[0]


def _statuses(html):
    return re.findall(r"<li data-lt-readiness-match='([^']+)'>.*?<span class='lt-ml__status'>(.*?)</span>", _index(html), re.S)


def _private_row(site, *, count=2, claims=0, phase=1):
    match, contestants, pairing = _row(site.tournament, count=count, claims=claims, phase=phase)
    return match, contestants, pairing


def _assert_private_absent(html, rows):
    for match, _, pairing in rows:
        for value in (match.ready_by_a, match.ready_by_b, match.pairing_id):
            if value:
                assert str(value) not in html
        if pairing:
            assert str(pairing.side_a.id) not in _index(html)
            assert str(pairing.side_b.id) not in _index(html)
    for key in ('ready_by', 'readiness_revision', 'pairing_generation', 'dispatch_token', 'invitation_hold', 'ready/claim', 'ready/revoke'):
        assert key not in html
    assert '<form' not in _index(html)


def test_server_index_has_all_states_and_real_detail_links(site, render):
    site.tournament = replace(site.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_private_row(site, count=count, claims=claims) for count, claims in ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))]
    context = site.context('bracket')
    html = render(context)
    statuses = _statuses(html)
    assert [match_id for match_id, _ in statuses] == [str(m.id) for m, _, _ in site.rows]
    for (_, status), expected in zip(statuses, ('Waiting for opponent', 'Waiting for opponent', 'Not ready', 'Partially ready', 'Both ready'), strict=True):
        assert expected in status
    for match, _, _ in site.rows:
        assert f"href='/lan-tournaments/matches/{match.id}'" in _index(html)
    assert html.index('</noscript>') < html.index('data-lt-readiness-index')
    assert 'bracket-match-card' in html.split('<noscript>', 1)[1].split('</noscript>', 1)[0]
    _assert_private_absent(html, site.rows)


# fmt: off
@pytest.mark.parametrize('side', ['a', 'b'])
@pytest.mark.parametrize('reversed_rows', [False, True])
# fmt: on
def test_logical_ready_side_is_stable_not_row_order(site, render, side, reversed_rows):
    match, contestants, pairing = _private_row(site, claims=1)
    if side == 'b':
        match = replace(match, ready_at_a=None, ready_at_b=NOW, ready_by_a=None, ready_by_b=uuid4())
    site.rows = [(match, list(reversed(contestants)) if reversed_rows else contestants, pairing)]
    status = _statuses(render(site.context('bracket')))[0][1]
    assert f'Side {side.upper()} ready' in status
    assert f'Side {"B" if side == "a" else "A"} not ready' in status
    assert 'Both ready' not in status


def test_assignment_never_turns_occupancy_into_both_ready(site, render):
    site.tournament = replace(site.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_private_row(site)]
    html = render(site.context('bracket'))
    assert 'Not ready' in _statuses(html)[0][1]
    assert 'Playable' not in _index(html)
    assert 'Both ready' not in _statuses(html)[0][1]
    assert 'Side A ready' not in _statuses(html)[0][1]


def test_stale_pair_claims_are_not_rendered(site, render):
    match, contestants, pairing = _private_row(site, claims=2)
    site.rows = [(match, contestants, replace(pairing, generation=3))]
    status = _statuses(render(site.context('bracket')))[0][1]
    assert 'Not ready' in status
    assert 'Both ready' not in status


# fmt: off
@pytest.mark.parametrize('mode', [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION])
@pytest.mark.parametrize('outcome', ['confirmed', 'defwin', 'completed', 'cancelled', 'paused'])
# fmt: on
def test_noscript_outcomes_and_original_fallback_preserved(site, render, mode, outcome):
    site.tournament = replace(site.tournament, elimination_mode=mode)
    if outcome in {'completed', 'cancelled', 'paused'}:
        site.tournament = replace(site.tournament, tournament_status=getattr(TournamentStatus, outcome.upper()))
    match, contestants, pairing = _private_row(site, count=1 if outcome == 'defwin' else 2, claims=2)
    match = replace(match, bracket=Bracket.WINNERS)
    if outcome in {'confirmed', 'defwin'}:
        match = replace(match, confirmed_by=uuid4())
        contestants = [replace(c, score=9 - i) for i, c in enumerate(contestants)]
    site.rows = [(match, contestants, pairing)]
    html = render(site.context('bracket'))
    fallback = html.split('<noscript>', 1)[1].split('</noscript>', 1)[0]
    assert 'bracket-match-card' in fallback
    status = _statuses(html)[0][1]
    assert {'paused': 'Both ready', 'defwin': 'DEFWIN'}.get(outcome, outcome.capitalize()) in status
    if outcome != 'paused':
        assert 'Both ready' not in status and 'Side A ready' not in status
    if outcome in {'confirmed', 'defwin'}:
        assert 'confirmed' in fallback
        assert re.search(r"class='bracket-contestant-score'>\s*9\s*</div>", fallback)
    _assert_private_absent(html, site.rows)


# fmt: off
@pytest.mark.parametrize('format_', [GameFormat.FREE_FOR_ALL, GameFormat.ONE_V_ONE])
# fmt: on
def test_ffa_and_round_robin_standings_keep_results(site, render, format_):
    site.tournament = replace(site.tournament, game_format=format_, elimination_mode=EliminationMode.ROUND_ROBIN)
    match, contestants, pairing = _private_row(site, count=3 if format_ == GameFormat.FREE_FOR_ALL else 2, claims=2)
    site.rows = [(replace(match, confirmed_by=uuid4()), [replace(c, score=10 - i) for i, c in enumerate(contestants)], pairing)]
    context = site.context('bracket')
    html = render(context)
    assert "class='rr-standings'" in html and 'Pts' in html
    assert 'Confirmed' in _statuses(html)[0][1]
    assert 'Both ready' not in _statuses(html)[0][1]
    assert '<noscript>' not in html
    if format_ == GameFormat.FREE_FOR_ALL:
        assert 'Avg' in html and len(context['ffa_standings']) == 3
    else:
        assert 'Diff' in html and len(context['standings']) == 2
    _assert_private_absent(html, site.rows)


def test_unconfirmed_ffa_has_no_manufactured_claim_capability(site, render):
    site.tournament = replace(site.tournament, game_format=GameFormat.FREE_FOR_ALL)
    site.rows = [_private_row(site, claims=2)]
    context = site.context('bracket')
    html = render(context)
    assert 'Readiness unavailable for this format' in _statuses(html)[0][1]
    assert 'Side A ready' not in _statuses(html)[0][1]
    assert not context['readiness_by_match_id'][site.rows[0][0].id].supports_readiness
    _assert_private_absent(html, site.rows)


def _qualification(site, monkeypatch, *, waiting=None, rankings=None):
    state = SimpleNamespace(source='groups', released_at=NOW)
    monkeypatch.setattr(views.tournament_qualification_service, 'get_qualification', lambda _: Ok(state))
    monkeypatch.setattr(views, 'participant_rankings', lambda *a: rankings or [])
    monkeypatch.setattr(views, 'contestant_names', lambda *a: {})
    monkeypatch.setattr(views, 'playoff_waiting_reason', lambda *a: waiting)
    monkeypatch.setattr(views, 'playoff_origin_labels', lambda *a: {})


# fmt: off
@pytest.mark.parametrize('base_format', [GameFormat.ONE_V_ONE, GameFormat.FREE_FOR_ALL])
# fmt: on
def test_phase_list_uses_shared_projection_retaining_scores_links(site, render, monkeypatch, base_format):
    site.tournament = replace(site.tournament, game_format=base_format, playoff_game_format=GameFormat.ONE_V_ONE,
                              playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_private_row(site, phase=2, claims=n) for n in (0, 1, 2)]
    match, contestants, pairing = _private_row(site, phase=2, claims=2)
    site.rows.append((replace(match, confirmed_by=uuid4()), [replace(c, score=3 - i) for i, c in enumerate(contestants)], pairing))
    _qualification(site, monkeypatch)
    context = site.context('bracket', '?phase=2')
    html = render(context)
    phase = render(context, macro=True)
    assert 'Not ready' in phase and 'Partially ready' in phase and 'Both ready' in phase
    assert 'Assigned / Playable' not in phase
    assert '3:2' in phase and 'Confirmed' in phase
    assert 'Side A ready' in phase and 'Side B not ready' in phase
    assert 'Pending' not in phase
    for row in context['playoff_matches']:
        assert f"href='/lan-tournaments/matches/{row['match_id']}'" in phase
    assert phase in html
    _assert_private_absent(html, site.rows)


def test_phase_list_uses_logical_side_b_and_outcome_precedence(site, render, monkeypatch):
    site.tournament = replace(site.tournament, playoff_game_format=GameFormat.ONE_V_ONE,
                              playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    match, contestants, pairing = _private_row(site, phase=2, claims=1)
    match = replace(match, ready_at_a=None, ready_at_b=NOW)
    site.rows = [(match, list(reversed(contestants)), pairing)]
    _qualification(site, monkeypatch)
    context = site.context('bracket', '?phase=2')
    phase = render(context, macro=True)
    assert 'Side B ready' in phase and 'Side A not ready' in phase
    assert 'Both ready' not in phase
    site.tournament = replace(site.tournament, tournament_status=TournamentStatus.COMPLETED)
    context = site.context('bracket', '?phase=2')
    phase = render(context, macro=True)
    assert 'Completed' in phase and 'Side B ready' not in phase


def test_playoff_empty_state_has_readonly_index(site, render, monkeypatch):
    site.tournament = replace(site.tournament, playoff_game_format=GameFormat.ONE_V_ONE,
                              playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    _qualification(site, monkeypatch)
    context = site.context('bracket', '?phase=2')
    html = render(context)
    assert context['phase_view'] == 2 and context['bracket_json'] is None
    assert 'No matches have been created yet.' in _index(html)
    assert _statuses(html) == []


# fmt: off
@pytest.mark.parametrize('phase,waiting,ffa', [(1, 'release', False), (2, 'tie', False), (2, None, False), (2, None, True)])
# fmt: on
def test_qualification_playoff_phase_presentation_intact(site, render, monkeypatch, phase, waiting, ffa):
    site.tournament = replace(site.tournament, playoff_game_format=GameFormat.FREE_FOR_ALL if ffa else GameFormat.ONE_V_ONE,
                              playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_private_row(site, phase=1), _private_row(site, phase=2, count=3 if ffa else 2, claims=2)]
    _qualification(site, monkeypatch, waiting=waiting, rankings=[_ranking((1, 2, 3))])
    context = site.context('bracket', f'?phase={phase}')
    html = render(context)
    assert 'Group phase' in html and 'Playoffs' in html and "aria-current='page'" in html
    assert [match_id for match_id, _ in _statuses(html)] == [str(site.rows[phase - 1][0].id)]
    if waiting:
        assert 'Playoffs: waiting for release' in html
    if phase == 1:
        assert 'Group A' in html and 'Player 3' in html and 'Qualification line' in html
    elif not waiting and ffa:
        assert "class='lt-lobby'" in html and 'Lobby' in html and 'rr-standings' in html
        assert 'Readiness unavailable for this format' in _statuses(html)[0][1]
    elif not waiting:
        assert "class='lt-ml'" in html and 'Both ready' in _statuses(html)[0][1]
    _assert_private_absent(html, site.rows)


def test_escaping_and_empty_server_index(site, render):
    site.tournament = replace(site.tournament, name='<script>Bad & Cup</script>')
    html = render(site.context('bracket'))
    assert '&lt;script&gt;Bad &amp; Cup&lt;/script&gt;' in html
    assert '<script>Bad' not in html
    assert 'No matches have been created yet.' in _index(html)


def test_bracket_index_has_no_legend(site, render):
    site.tournament = replace(site.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_private_row(site, claims=claims) for claims in (0, 1, 2)]
    html = render(site.context('bracket'))
    assert _statuses(html)
    assert 'data-lt-assignment-legend' not in html
    assert 'Ready in the bracket diagram' not in html
    assert 'Playable' not in _index(html)
    assert '<h2>Match readiness</h2>' in _index(html)


def test_phase_list_fallback_without_readiness_is_open_without_readiness(site, render, monkeypatch):
    site.tournament = replace(site.tournament, playoff_game_format=GameFormat.ONE_V_ONE,
                              playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    site.rows = [_private_row(site, phase=2)]
    _qualification(site, monkeypatch)
    context = site.context('bracket', '?phase=2')
    phase = render({**context, 'readiness_by_match_id': {}}, macro=True)
    assert 'Open (no readiness)' in phase
    assert 'Assigned / Playable' not in phase


def test_site_index_defwin_uses_the_shared_msgid(site, render):
    site.tournament = replace(site.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    match, contestants, pairing = _private_row(site, count=1, claims=2)
    match = replace(match, bracket=Bracket.WINNERS, confirmed_by=uuid4())
    site.rows = [(match, [replace(c, score=9) for c in contestants], pairing)]
    html = render(site.context('bracket'))
    status = _statuses(html)[0][1]
    assert 'DEFWIN' in status
    assert 'Defwin' not in _index(html)


def _css_rule(css, selector):
    found = re.search(r'(?m)^[ \t]*' + re.escape(selector) + r'\s*\{([^}]*)\}', css)
    assert found, selector
    return found.group(1)


def test_bracket_readiness_tag_is_styled():
    css = (ROOT / 'byceps/static/style/lan_tournament_bracket.css').read_text()
    base = _css_rule(css, '.lt-match-readiness')
    pill = _css_rule(css, '.lt-match-status')
    for declaration in ('display: inline-flex', 'border-radius: 999px', 'white-space: nowrap',
                        'grid-column: 2', 'grid-row: 1', 'justify-self: end'):
        assert declaration in base and declaration in pill
    variants = {'both_ready': '--lt-brand-success', 'partially_ready': '--lt-brand-blue', 'open': '--lt-brand-orange'}
    for status, token in variants.items():
        assert token in _css_rule(css, f'.lt-match-readiness[data-readiness-status="{status}"]')
    waiting = _css_rule(css, '.lt-match-readiness[data-readiness-status="not_yet_occupied"]')
    assert 'border-style: dashed' in waiting
    tokens = {token for rule in (base, waiting, *(_css_rule(css, f'.lt-match-readiness[data-readiness-status="{status}"]') for status in variants))
              for token in re.findall(r'var\((--[a-z-]+)', rule)}
    assert tokens <= set(re.findall(r'var\((--[a-z-]+)', css.split('.lt-match-readiness', 1)[0]))
    print_css = css[css.rindex('@media print'):]
    assert '.lt-match-status--done' in print_css
    assert '.lt-match-readiness[data-readiness-status]' in print_css


def _labels(html):
    return [' '.join(label.split()) for label in re.findall(r"<li data-lt-readiness-match='[^']+'>\s*<a href='[^']+'>(.*?)</a>", _index(html), re.S)]


def test_site_index_labels_use_one_based_rounds(site, render):
    site.tournament = replace(site.tournament, elimination_mode=EliminationMode.DOUBLE_ELIMINATION)
    fields = (
        {'bracket': None, 'round': 0},
        {'bracket': None, 'round': 1},
        {'bracket': Bracket.WINNERS, 'round': 0},
        {'bracket': Bracket.THIRD_PLACE, 'round': 1},
        {'bracket': Bracket.GRAND_FINAL, 'round': 0},
    )
    site.rows = []
    for values in fields:
        match, contestants, pairing = _row(site.tournament)
        site.rows.append((replace(match, **values), contestants, pairing))
    html = render(site.context('bracket'))
    assert _labels(html) == [
        'Match 1 · Round 1',
        'Match 2 · Round 2',
        'Match 3 · WB · Round 1',
        'Match 4 · Third-place match',
        'Match 5 · Grand Final',
    ]
    assert 'Round 0' not in _index(html)
