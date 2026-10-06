"""Real inherited server markup, not browser geometry or mobile interaction."""

from dataclasses import replace
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from flask import g, get_flashed_messages, url_for
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest

from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.navigation import Navigation
from byceps.util.result import Ok
from byceps.util.templatefilters import dim
from tests.unit.services.lan_tournament.test_admin_readiness_filters import (
    backend,  # noqa: F401 -- accepted read-only context fixture
)
from tests.unit.services.lan_tournament.test_readiness_surface_policy import (
    NOW,
    _row,
)
from tests.unit.services.lan_tournament.test_site_readiness_filters import (
    site,  # noqa: F401 -- dependency of backend fixture
)


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path('/tmp/opencode/f04-execution/issue21-3/render-artifacts')  # noqa: S108 -- approved evidence directory


@pytest.fixture(params=['generic', 'totalverplant-36'])
def render(backend, request, monkeypatch):  # noqa: F811 -- imported fixture
    theme = request.param
    monkeypatch.setattr(
        views,
        '_ffa_cut_tie_context',
        lambda _: {'ffa_cut_ties': [], 'js_strings': {}},
    )
    paths = []
    if theme != 'generic':
        paths.append(ROOT / 'sites/totalverplant-36/template_overrides')
    paths.extend(
        sorted((ROOT / 'byceps/services').glob('*/blueprints/admin/templates'))
    )
    paths.extend(
        sorted((ROOT / 'byceps/services').glob('*/blueprints/common/templates'))
    )
    env = Environment(
        loader=FileSystemLoader(paths),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.globals.update(
        _=lambda text, **values: text % values if values else text,
        ngettext=lambda single, plural, n, **values: (
            (single if n == 1 else plural) % values
        ),
        g=g,
        url_for=url_for,
        Navigation=Navigation,
        get_flashed_messages=get_flashed_messages,
        lan_tournament_pending_request_count=lambda _: 0,
        lan_tournament_has_orga_assignments=lambda _: False,
    )
    env.filters.update(
        dim=dim,
        numberformat=str,
        dateformat=lambda value, *args: value.date().isoformat(),
        timeformat=lambda value, *args: value.time().isoformat(),
    )
    backend.app.add_url_rule(
        '/shell/login', 'authn_login_admin.log_in_form', lambda: ''
    )

    def render_context(context, *, without_companion=False, administrate=False):
        with backend.app.test_request_context(
            f'/lan-tournaments/tournaments/{backend.state.tournament.id}/bracket'
        ):
            # Shell guest presentation avoids unrelated avatar/service dependencies;
            # canonical context comes from the accepted view-only GET fixture.
            g.user = SimpleNamespace(
                authenticated=False,
                has_permission=lambda permission: (
                    administrate and permission == 'lan_tournament.administrate'
                ),
            )
            g.current_locale = SimpleNamespace(language='en')
            name = 'admin/lan_tournament/bracket.html'
            if without_companion:
                source = env.loader.get_source(env, name)[0].replace(
                    "  {% include 'admin/lan_tournament/_readiness_bracket.html' %}\n\n",
                    '',
                )
                html = env.from_string(source).render(**context)
            else:
                html = env.get_template(name).render(**context)
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            (EVIDENCE / f'{request.node.name}-{uuid4()}.html').write_text(html)
            return html

    return render_context


def _index(html):
    return html.split('data-lt-readiness-index>', 1)[1].split('</section>', 1)[
        0
    ]


def _statuses(html):
    return re.findall(
        r"data-readiness-status='([^']+)'>(.*?)</span>\s*</li>",
        _index(html),
        re.S,
    )


def _labels(html):
    return [
        ' '.join(label.split())
        for label in re.findall(
            r"<a href='[^']+'><b>(.*?)</b></a>", _index(html), re.S
        )
    ]


def _private_row(state, *, count=2, claims=0, phase=1):
    match, contestants, pairing = _row(
        state.tournament, count=count, claims=claims, phase=phase
    )
    return (
        replace(match, bracket=Bracket.WINNERS),
        contestants,
        pairing,
    )


def _assert_privacy(html, rows):
    index = _index(html)
    for match, _, pairing in rows:
        for value in (
            match.ready_by_a,
            match.ready_by_b,
            match.pairing_id,
        ):
            if value:
                assert str(value) not in html
        if pairing:
            assert str(pairing.side_a.id) not in index
            assert str(pairing.side_b.id) not in index
    for key in (
        'ready_by',
        'readiness_history',
        'readiness_revision',
        'pairing_generation',
        'dispatch_token',
        'recipient',
        'invitation_hold',
        'ready/claim',
        'ready/revoke',
    ):
        assert key not in index
    assert '<form' not in index
    # Only native public links expose match IDs, not a second status payload.
    assert 'data-lt-readiness-match' not in index


def _assert_graph_unchanged(context, html, render, *, administrate=False):
    original = render(
        context, without_companion=True, administrate=administrate
    )
    companion = re.search(
        r"  \n<section aria-label='Match readiness'.*?</section>\n\n",
        html,
        re.S,
    )
    assert companion is not None
    assert html[: companion.start()] + html[companion.end() :] == original


def test_ac1_all_states_native_links_and_both_graph_renderings(backend, render):  # noqa: F811
    state = backend.state
    state.tournament = replace(
        state.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION
    )
    state.rows = [
        _private_row(state, count=count, claims=claims)
        for count, claims in ((0, 0), (1, 0), (2, 0), (2, 1), (2, 2))
    ]
    context = backend.context('bracket')
    html = render(context)
    assert [status for status, _ in _statuses(html)] == [
        'not_yet_occupied',
        'not_yet_occupied',
        'open',
        'partially_ready',
        'both_ready',
    ]
    assert 'Waiting for opponent' in _statuses(html)[0][1]
    assert 'Not ready' in _statuses(html)[2][1]
    assert 'Playable' not in _index(html)
    assert 'Both ready' in _statuses(html)[4][1]
    assert (
        'Side A ready' in _statuses(html)[4][1]
        and 'Side B ready' in _statuses(html)[4][1]
    )
    assert 'data-lt-assignment-legend' not in html
    for entry in context['match_data']:
        assert (
            entry['readiness']
            is context['readiness_by_match_id'][entry['match'].id]
        )
        assert f"href='/lan-tournaments/matches/{entry['match'].id}'" in _index(
            html
        )
    assert (
        "class='bracket-desktop'" in html and "class='bracket-mobile'" in html
    )
    assert 'bracket-match-card' in html and 'bracket-container' in html
    assert '<!DOCTYPE html>' in html and 'width=device-width' in html
    _assert_graph_unchanged(context, html, render)
    _assert_privacy(html, state.rows)


# fmt: off
@pytest.mark.parametrize('side', ['a', 'b'])
@pytest.mark.parametrize('reverse', [False, True])
# fmt: on
def test_ac1_logical_partial_side_labels_survive_association_order(backend, render, side, reverse):  # noqa: F811
    state = backend.state
    match, contestants, pairing = _private_row(state, claims=1)
    if side == 'b':
        match = replace(match, ready_at_a=None, ready_at_b=NOW, ready_by_a=None, ready_by_b=uuid4())
    state.rows = [(match, list(reversed(contestants)) if reverse else contestants, pairing)]
    context = backend.context('bracket')
    html = render(context)
    status = _statuses(html)[0][1]
    assert f'Side {side.upper()} ready' in status
    assert f'Side {"B" if side == "a" else "A"} not ready' in status
    assert 'Both ready' not in status
    assert context['match_data'][0]['contestants'] == state.rows[0][1]
    _assert_graph_unchanged(context, html, render)
    _assert_privacy(html, state.rows)


def test_ac1_empty_index_and_stale_claims(backend, render):  # noqa: F811
    state = backend.state
    state.rows = []
    assert 'No matches have been created yet.' in _index(render(backend.context('bracket')))
    match, contestants, pairing = _private_row(state, claims=2)
    state.rows = [(match, contestants, replace(pairing, generation=3))]
    html = render(backend.context('bracket'))
    assert 'Not ready' in _statuses(html)[0][1]
    assert 'Both ready' not in _statuses(html)[0][1]


def test_ac1_uses_the_supplied_localized_display_not_a_second_projection(backend, render):  # noqa: F811
    state = backend.state
    state.rows = [_private_row(state, claims=1)]
    context = backend.context('bracket')
    context['match_data'][0]['readiness_display']['label'] = 'LOCALIZED CANONICAL LABEL'
    html = render(context)
    assert 'LOCALIZED CANONICAL LABEL' in _statuses(html)[0][1]
    assert 'Side B not ready' in _statuses(html)[0][1]


# fmt: off
@pytest.mark.parametrize('mode', [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION])
@pytest.mark.parametrize('outcome', ['confirmed', 'defwin', 'completed', 'cancelled', 'paused'])
# fmt: on
def test_ac2_outcomes_preserve_graph_scores_and_mobile_results(backend, render, mode, outcome):  # noqa: F811
    state = backend.state
    state.tournament = replace(state.tournament, elimination_mode=mode)
    if outcome in {'completed', 'cancelled', 'paused'}:
        state.tournament = replace(state.tournament, tournament_status=getattr(TournamentStatus, outcome.upper()))
    match, contestants, pairing = _private_row(state, count=1 if outcome == 'defwin' else 2, claims=2)
    if outcome in {'confirmed', 'defwin'}:
        match = replace(match, confirmed_by=uuid4())
        contestants = [replace(c, score=9 - i) for i, c in enumerate(contestants)]
    state.rows = [(match, contestants, pairing)]
    context = backend.context('bracket')
    html = render(context)
    status, text = _statuses(html)[0]
    assert status == ('both_ready' if outcome == 'paused' else outcome)
    assert ('Both ready' if outcome == 'paused' else 'DEFWIN' if outcome == 'defwin' else outcome.capitalize()) in text
    if outcome != 'paused':
        assert 'Both ready' not in text and 'Side A ready' not in text
    if outcome in {'confirmed', 'defwin'}:
        assert re.search(r"class='bracket-contestant-score'>\s*9\s*</div>", html)
        assert 'confirmed' in html
    _assert_graph_unchanged(context, html, render)
    _assert_privacy(html, state.rows)


# fmt: off
@pytest.mark.parametrize('base,playoff', [(GameFormat.FREE_FOR_ALL, GameFormat.ONE_V_ONE), (GameFormat.ONE_V_ONE, GameFormat.FREE_FOR_ALL)])
@pytest.mark.parametrize('waiting', [None, 'groups'])
# fmt: on
def test_ac2_effective_phase_format_labels_and_existing_graph_selection(backend, render, monkeypatch, base, playoff, waiting):  # noqa: F811
    state = backend.state
    state.tournament = replace(state.tournament, game_format=base, playoff_game_format=playoff, playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    state.rows = [_private_row(state, phase=phase, claims=2) for phase in (1, 2)]
    monkeypatch.setattr(views.tournament_qualification_service, 'get_qualification', lambda _: Ok(SimpleNamespace(source='groups')))
    monkeypatch.setattr(views, 'participant_rankings', lambda *args: [])
    monkeypatch.setattr(views, 'contestant_names', lambda *args: {})
    monkeypatch.setattr(views, 'playoff_waiting_reason', lambda *args: waiting)
    context = backend.context('bracket')
    html = render(context)
    assert len(context['bracket_match_data']) == 1
    assert context['bracket_match_data'][0] is context['match_data'][1]
    assert context['bracket_match_data'][0]['match'].phase == 2
    for (status, text), (match, _, _) in zip(_statuses(html), state.rows, strict=True):
        effective = base if match.phase == 1 else playoff
        assert context['readiness_by_match_id'][match.id].supports_readiness is (effective == GameFormat.ONE_V_ONE)
        if effective == GameFormat.ONE_V_ONE:
            assert status == 'both_ready' and 'Both ready' in text
        else:
            assert 'Readiness is not available for this format.' in text
            assert 'Side A ready' not in text and 'Both ready' not in text
        assert context['match_labels'][str(match.id)] in _index(html)
    assert 'Group' in _index(html) and 'Playoffs' in _index(html)
    _assert_graph_unchanged(context, html, render)
    _assert_privacy(html, state.rows)


# fmt: off
@pytest.mark.parametrize('mode', [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION, EliminationMode.ROUND_ROBIN])
@pytest.mark.parametrize('outcome', ['open', 'confirmed', 'completed', 'cancelled'])
# fmt: on
def test_ac2_ffa_unavailable_unless_outcome_preserves_existing_standings(backend, render, mode, outcome):  # noqa: F811
    state = backend.state
    state.tournament = replace(state.tournament, game_format=GameFormat.FREE_FOR_ALL, elimination_mode=mode)
    if outcome in {'completed', 'cancelled'}:
        state.tournament = replace(state.tournament, tournament_status=getattr(TournamentStatus, outcome.upper()))
    match, contestants, pairing = _private_row(state, count=3, claims=2)
    if outcome == 'confirmed':
        match = replace(match, confirmed_by=uuid4())
        contestants = [replace(c, score=10 - i) for i, c in enumerate(contestants)]
    state.rows = [(match, contestants, pairing)]
    context = backend.context('bracket')
    html = render(context)
    text = _statuses(html)[0][1]
    assert ('Readiness is not available for this format.' if outcome == 'open' else outcome.capitalize()) in text
    assert 'Side A ready' not in text and 'Side B ready' not in text and 'Both ready' not in text
    assert 'Standings' in html or 'Pts' in html
    _assert_graph_unchanged(context, html, render)
    _assert_privacy(html, state.rows)


def test_ac2_pending_feeder_truth_and_association_order_are_unchanged(backend, render):  # noqa: F811
    state = backend.state
    first = _private_row(state, claims=2)
    final = _private_row(state, count=1)
    state.rows = [(replace(first[0], next_match_id=final[0].id), list(reversed(first[1])), first[2]), final]
    context = backend.context('bracket')
    assert context['match_data'][1]['has_pending_feeder'] is True
    html = render(context)
    assert _statuses(html)[1][0] == 'not_yet_occupied'
    assert 'Waiting for opponent' in _statuses(html)[1][1]
    assert 'badge-pending' in html
    _assert_graph_unchanged(context, html, render)
    assert context['match_data'][0]['contestants'] == state.rows[0][1]


# fmt: off
@pytest.mark.parametrize('mode', [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION])
@pytest.mark.parametrize('confirmed', [False, True])
# fmt: on
def test_ac2_existing_ffa_administration_controls_are_byte_preserved(backend, render, mode, confirmed):  # noqa: F811
    state = backend.state
    state.tournament = replace(state.tournament, game_format=GameFormat.FREE_FOR_ALL, elimination_mode=mode)
    match, contestants, pairing = _private_row(state, count=3)
    if confirmed:
        match = replace(match, confirmed_by=uuid4())
        contestants = [replace(c, score=10 - i) for i, c in enumerate(contestants)]
    state.rows = [(match, contestants, pairing)]
    context = backend.context('bracket')
    html = render(context, administrate=True)
    expected = ('Advance WB Round' if mode == EliminationMode.DOUBLE_ELIMINATION else 'Advance Round') if confirmed else 'Confirm all'
    assert expected in html
    _assert_graph_unchanged(context, html, render, administrate=True)
    assert '<form' not in _index(html)


def test_ac3_public_allowlist_escaping_and_no_history_or_work_reads(backend, render):  # noqa: F811
    state = backend.state
    state.rows = [_private_row(state, claims=2)]
    context = backend.context('bracket')
    entry = context['match_data'][0]
    assert set(entry['readiness_display']) == {'supported', 'status', 'label', 'ready_sides', 'side_labels', 'assignment_complete', 'assigned_contestant_count', 'original_occupied_since', 'pairing_started_at', 'ready_at_a', 'ready_at_b'}
    context['match_labels'] = {str(entry['match'].id): '<script>LABEL</script>'}
    html = render(context)
    assert '&lt;script&gt;LABEL&lt;/script&gt;' in _index(html)
    assert '<script>LABEL</script>' not in html
    _assert_privacy(html, state.rows)
    for forbidden in backend.forbidden:
        forbidden.assert_not_called()
    assert 'bracket_json' not in context
    assert 'readiness_history' not in context and 'readiness_history_users_by_id' not in context


def test_bracket_index_has_no_legend(backend, render):  # noqa: F811
    state = backend.state
    state.rows = [_private_row(state, claims=claims) for claims in (0, 1, 2)]
    html = render(backend.context('bracket'))
    assert _statuses(html)
    assert 'data-lt-assignment-legend' not in html
    assert 'Ready in the bracket diagram' not in html
    assert 'Playable' not in _index(html)
    assert '<h2>Match readiness</h2>' in _index(html)


def test_admin_index_labels_round_and_third_place(backend, render):  # noqa: F811
    state = backend.state
    state.tournament = replace(
        state.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION
    )
    fields = (
        {'bracket': None, 'round': 0, 'match_order': 0},
        {'bracket': None, 'round': 1, 'match_order': 1},
        {'bracket': Bracket.THIRD_PLACE, 'round': 1, 'match_order': 0},
        {'bracket': Bracket.GRAND_FINAL, 'round': 0, 'match_order': 0},
    )
    state.rows = []
    for values in fields:
        match, contestants, pairing = _row(state.tournament)
        state.rows.append((replace(match, **values), contestants, pairing))
    html = render(backend.context('bracket'))
    assert _labels(html) == [
        'Match R1 M1 · Round 1',
        'Match R2 M2 · Round 2',
        'Match P3 M1 · Third-place match',
        'Match GF M1 · Grand Final',
    ]
    assert {status for status, _ in _statuses(html)} == {'open'}
    assert re.search(
        r"</b></a> —\s*<span data-readiness-status='open'>", _index(html)
    )


def test_admin_index_status_symbols_are_decorative(backend, render):  # noqa: F811
    state = backend.state
    state.rows = [
        _private_row(state, count=count, claims=claims)
        for count, claims in ((1, 0), (2, 0), (2, 1), (2, 2))
    ]
    html = render(backend.context('bracket'))
    texts = [text for _, text in _statuses(html)]
    assert '<span' not in texts[0]
    for text, symbol, word in zip(
        texts[1:],
        '○◐✓',
        ('Not ready', 'Partially ready', 'Both ready'),
        strict=True,
    ):
        assert f"<span aria-hidden='true'>{symbol}</span>" in text
        assert word in text
