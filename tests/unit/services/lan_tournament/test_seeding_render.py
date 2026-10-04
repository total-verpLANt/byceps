"""
tests.unit.services.lan_tournament.test_seeding_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from datetime import datetime, UTC
import pathlib
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    seed_code,
    tournament_seeding_domain_service as domain,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.tournament_match_service import (
    UndersizedPool,
)
from byceps.services.lan_tournament.tournament_seeding_service import (
    GenerationStatus,
    SeedingBoard,
)


_ADMIN_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/seeding.html'
)

_SITE_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/seeding.html'
)
_ORGA_ACTIONS_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/orga_actions.html'
)
_SITE_BOARD = _SITE_TEMPLATE.with_name('_seeding_board.html')
_ADMIN_BOARD = _ADMIN_TEMPLATE.with_name('_seeding_board.html')
_ADMIN_SUBNAV = _ADMIN_TEMPLATE.with_name('_subnav.html')
_BOTE_DIR = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
)
_BOTE_TEMPLATE = _BOTE_DIR / 'seeding.html'
_BOTE_STYLE = _BOTE_DIR / '_bote_seeding_style.html'

SE = SeedingFormat.SINGLE_ELIMINATION
DE = SeedingFormat.DOUBLE_ELIMINATION
RR = SeedingFormat.ROUND_ROBIN
FFA = SeedingFormat.FREE_FOR_ALL

_LAYOUT = (
    '{% block head %}{% endblock %}{% block before_body %}{% endblock %}'
    '{% block body %}{% endblock %}{% block scripts %}{% endblock %}'
)


def _translate(message, **params):
    return message % params if params else message


def _ntranslate(singular, plural, num, **params):
    params.setdefault('num', num)
    return (singular if num == 1 else plural) % params


@pytest.fixture(autouse=True)
def plain_translations(monkeypatch):
    monkeypatch.setattr(helpers, 'gettext', _translate)
    monkeypatch.setattr(helpers, 'ngettext', _ntranslate)


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'layout/admin/lan_tournament.html': _LAYOUT,
                'admin': _ADMIN_TEMPLATE.read_text(),
                'admin/lan_tournament/_seeding_board.html': (
                    _ADMIN_BOARD.read_text()
                ),
                'admin/lan_tournament/_subnav.html': (
                    _ADMIN_SUBNAV.read_text()
                ),
            }
        ),
    )
    e.globals['_'] = _translate
    e.globals['ngettext'] = _ntranslate
    e.globals['url_for'] = lambda endpoint, **k: '/' + endpoint.lstrip('.')
    e.filters['dateformat'] = lambda value: value.strftime('%Y-%m-%d')
    e.filters['timeformat'] = lambda value, kind='short': value.strftime(
        '%H:%M'
    )
    return e


def _markup(html):
    """Return the page without its JSON and script payloads."""
    return re.sub(r'<script.*?</script>', '', html, flags=re.S)


def _ids(n):
    return [f'c{i:02d}' for i in range(n)]


def _board(
    fmt=SE,
    n=12,
    *,
    param=0,
    tier_count=1,
    layout_swaps=(),
    stale=False,
    generation=GenerationStatus.NOT_GENERATED,
    generated_code=None,
    locked_reason=None,
    leavers=(),
    joiners=(),
    new=(),
    stale_structure=False,
    stale_ranks=False,
    target='initial',
    undersized=(),
    byes=(),
    notices=(),
    leaver_ids=(),
    joiner_ids=(),
    real_code=False,
):
    ids = _ids(n)
    state = domain.initial_state(
        fmt, param, ids, tier_count=tier_count, draw_seed=0x5EED0012
    )
    for p, q in layout_swaps:
        state = domain.swap_slots(state, p, q)
    problems, params = [], []
    two_byes = iter(domain.two_bye_matches(state))
    for msgid in domain.layout_problems(state):
        problems.append(msgid)
        params.append(
            {'n': next(two_byes)} if msgid == domain.PROBLEM_TWO_BYES else {}
        )
    code = None if problems else 'S1A8-EZAJ-YXM0-155F-CQ9A'
    if real_code and not problems:
        code = seed_code.format_seed_code(
            seed_code.encode_seed_code(
                state,
                domain.derive_layout(
                    state.format, state.seed_list, state.param
                ),
            )
        )
    return SeedingBoard(
        tournament_id='t0',
        target=target,
        state=state,
        version=3,
        code=code,
        stale=stale,
        labels={cid: f'Player {cid[1:]}' for cid in ids},
        stale_leavers=tuple(leavers),
        stale_joiners=tuple(joiners),
        stale_leaver_ids=tuple(leaver_ids),
        stale_joiner_ids=tuple(joiner_ids),
        new_entrant_ids=tuple(new),
        problems=tuple(problems),
        problem_params=tuple(params),
        balance=domain.balance(state),
        fix_count=domain.fix_count(state),
        pure_draw=domain.is_pure_draw(state),
        generated_code=generated_code,
        generation=generation,
        locked_reason=locked_reason,
        stale_structure=stale_structure,
        stale_ranks=stale_ranks,
        undersized=undersized,
        byes=byes,
        notices=tuple(msgid for msgid, _ in notices),
        notice_params=tuple(params for _, params in notices),
    )


def _payload(board):
    payload = helpers.seeding_board_payload(board)
    payload.setdefault('stale_structure', board.stale_structure)
    return payload


@pytest.mark.parametrize(
    'generation', [GenerationStatus.NOT_GENERATED, GenerationStatus.DIFFERS]
)
@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize('condition', ['fresh', 'stale', 'invalid'])
def test_regenerate_refusal_disables_the_button(
    env, site_env, generation, surface, condition
):
    reason = (
        'A match already has a confirmed result. Take it back before you regenerate.'
    )
    board = dataclasses.replace(
        _board(
            generation=generation,
            stale=condition == 'stale',
            layout_swaps=[(4, 1)] if condition == 'invalid' else (),
        ),
        regenerate_refusal=reason,
    )
    payload = _payload(board)
    assert payload.get('regenerate_refusal') == reason
    def render(b, **kwargs):
        if surface == 'admin':
            return _render(env, b, **kwargs)
        return _render_site(site_env, b, **kwargs)

    html = _markup(render(board))
    assert f'<span class="lt-seed-why">{reason}</span>' in html
    form = re.search(
        r'<form[^>]*action="/[^"]*seeding_generate".*?</form>', html, re.S
    ).group()
    assert re.search(r'<button[^>]* disabled', form)
    assert 'data-confirm-title' not in form
    # Missing optional payload keys must remain safe under StrictUndefined.
    absent = _markup(render(board, drop=('regenerate_refusal',)))
    assert reason not in absent


@pytest.mark.parametrize(
    'generation', [GenerationStatus.NOT_GENERATED, GenerationStatus.DIFFERS]
)
@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_regenerate_refusal_payload_is_rendered(
    env, site_env, monkeypatch, generation, surface
):
    reason = (
        'A match already has a confirmed result. Take it back before you regenerate.'
    )
    real_payload = helpers.seeding_board_payload

    def payload(board):
        return real_payload(board) | {'regenerate_refusal': reason}

    monkeypatch.setattr(helpers, 'seeding_board_payload', payload)
    board = _board(generation=generation)
    html = _markup(
        _render(env, board) if surface == 'admin' else _render_site(site_env, board)
    )
    assert f'<span class="lt-seed-why">{reason}</span>' in html
    form = re.search(
        r'<form[^>]*action="/[^"]*seeding_generate".*?</form>', html, re.S
    ).group()
    assert re.search(r'<button[^>]* disabled', form)


def _render(
    env,
    board,
    *,
    status='REGISTRATION_CLOSED',
    team=False,
    rows=(),
    drop=(),
):
    payload = _payload(board)
    for key in drop:
        del payload[key]
    return env.get_template('admin').render(
        party=SimpleNamespace(id='p0', title='Party'),
        tournament=SimpleNamespace(
            id='t0',
            name='Blitzschach am Kamin',
            tournament_status=SimpleNamespace(name=status),
            contestant_type=SimpleNamespace(name='TEAM' if team else 'SOLO'),
            game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
            has_playoffs=False,
            elimination_mode=None,
        ),
        board=payload,
        audit_rows=list(rows),
    )


@pytest.mark.parametrize(
    ('fmt', 'n', 'param', 'tier_count'),
    [
        (SE, 12, 0, 1),
        (SE, 2, 0, 1),
        (DE, 8, 0, 1),
        (RR, 8, 2, 1),
        (RR, 5, 1, 1),
        (FFA, 20, 5, 4),
        (FFA, 7, 4, 2),
    ],
)
def test_admin_seeding_renders_strict(env, fmt, n, param, tier_count):
    html = _render(
        env,
        _board(fmt, n, param=param, tier_count=tier_count),
        team=fmt is DE,
    )

    assert 'data-lt-seed-root' in html
    assert 'name="p"' in html
    assert 'lt-seed-chip' in html
    assert html.count('data-lt-seed-board') == 1
    assert '%(' not in _markup(html)


def test_admin_seeding_renders_strict_for_every_generation_state(env):
    for generation in GenerationStatus:
        html = _render(
            env,
            _board(
                generation=generation,
                generated_code='S1A8EZAJYXM0155F',
                locked_reason='The seeding is locked.'
                if generation is GenerationStatus.LOCKED
                else None,
            ),
        )
        assert '%(' not in _markup(html)


def test_admin_seeding_renders_audit_trail_without_raw_keys(env):
    rows = helpers.seeding_audit_rows(
        [
            SimpleNamespace(
                occurred_at=datetime(2026, 9, 30, 18, 2, tzinfo=UTC),
                event_type='seeding-swapped',
                initiator_id='u1',
                data={'seed_code': 'S1A8EZAJYXM0155FCQ9AG0C413'},
            ),
            SimpleNamespace(
                occurred_at=datetime(2026, 9, 30, 18, 1, tzinfo=UTC),
                event_type='playoffs-released',
                initiator_id=None,
                data={},
            ),
            SimpleNamespace(
                occurred_at=datetime(2026, 9, 30, 18, 0, tzinfo=UTC),
                event_type='seeding-brand-new-event',
                initiator_id='u1',
                data={},
            ),
        ],
        {'u1': SimpleNamespace(screen_name='Ohrwurm')},
    )

    html = _render(env, _board(), rows=rows)

    assert 'Swapped' in html
    assert 'Playoffs released' in html
    assert 'Other action' in html
    assert 'Ohrwurm' in html
    assert 'data-event="seeding-swapped"' in html
    assert '>seeding-swapped<' not in html
    assert '>seeding-brand-new-event<' not in html


def test_stale_banner_rendered(env):
    board = _board(
        stale=True,
        leavers=('Kaffeesatz',),
        joiners=('Zahnrad',),
        generation=GenerationStatus.DIFFERS,
        generated_code='S1A8EZAJYXM0155F',
    )

    html = _render(env, board)

    banner = html[html.index('data-lt-seed-banners') :]
    assert 'nt-err' in banner
    assert 'Kaffeesatz' in banner
    assert 'Zahnrad' in banner
    assert 'value="reseed_keep_tiers"' in banner
    assert '<s>S1A8-EZAJ-YXM0-155F-CQ9A</s>' in html
    assert 'lt-seed-stl' in html
    assert ' disabled' in html[html.index('lt-seed-bw') :]


def test_stale_banner_without_known_leaver_uses_the_generic_sentence(env):
    html = _render(env, _board(stale=True))

    assert 'The roster changed since the seed code was made.' in html
    assert 'No longer taking part' not in html


def test_structure_stale_banner_names_the_structure_and_offers_the_reseed(env):
    html = _render(env, _board(stale=True, stale_structure=True))

    banner = html[html.index('data-lt-seed-banners') :]
    assert 'The tournament structure changed since the seeding was drawn.' in (
        banner
    )
    assert 'The roster changed since' not in banner
    assert 'value="reseed_keep_tiers"' in banner


def test_ranks_stale_banner_offers_the_reprefill(env):
    html = _render(
        env, _board(stale=True, stale_ranks=True, target='playoff')
    )

    banner = html[html.index('data-lt-seed-banners') :]
    assert 'The qualification ranks changed after this draft was made.' in (
        banner
    )
    assert 'The roster changed since' not in banner
    assert 'value="reprefill"' in banner
    assert 'Re-prefill from qualification' in banner
    assert 'value="reseed_keep_tiers"' in banner


def test_the_reprefill_is_offered_for_every_stale_playoff_draft(env):
    roster = _render(env, _board(stale=True, target='playoff'))
    structure = _render(
        env, _board(stale=True, stale_structure=True, target='playoff')
    )
    initial = _render(env, _board(stale=True, stale_structure=True))

    for html in (roster, structure):
        assert 'value="reprefill"' in html[html.index('data-lt-seed-banners') :]
    assert 'value="reprefill"' not in initial


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_ranks_stale_banner_offers_the_reprefill(site_env, name):
    html = _render_site(
        site_env,
        _board(stale=True, stale_ranks=True, target='playoff'),
        name=name,
    )

    banner = html[html.index('data-lt-seed-banners') :]
    assert 'The qualification ranks changed after this draft was made.' in (
        banner
    )
    assert 'value="reprefill"' in banner
    assert 'Re-prefill from qualification' in banner


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_structure_stale_banner_names_the_structure(site_env, name):
    html = _render_site(
        site_env, _board(stale=True, stale_structure=True), name=name
    )

    banner = html[html.index('data-lt-seed-banners') :]
    assert 'The tournament structure changed since the seeding was drawn.' in (
        banner
    )
    assert 'value="reseed_keep_tiers"' in banner


_UNDERSIZED = (UndersizedPool(Bracket.LOSERS, 0, 3, (3,), 4),)
_UNDERSIZED_TEXT = (
    'Because participants were removed, the losers pool has only 3'
    ' contestants. Lobby sizes: 3, below the minimum of 4.'
)


_NOTICES = (
    (
        (
            'The roster gives %(groups)s groups instead of the configured'
            ' %(configured)s.'
        ),
        {'groups': 3, 'configured': 4},
    ),
    (
        'With fewer than 4 qualifiers the playoffs run as single knockout.',
        {},
    ),
)
_NOTICE_TEXTS = (
    'The roster gives 3 groups instead of the configured 4.',
    'With fewer than 4 qualifiers the playoffs run as single knockout.',
)


def test_board_renders_notices(env, site_env):
    board = _board(RR, 6, param=3, notices=_NOTICES)
    pages = [
        _render(env, board),
        _render_site(site_env, board, name='site'),
        _render_site(site_env, board, name='bote'),
    ]

    for html in pages:
        banner = html[html.index('data-lt-seed-banners') :]
        for text in _NOTICE_TEXTS:
            assert text in banner
        assert banner.index(_NOTICE_TEXTS[0]) < banner.index(_NOTICE_TEXTS[1])


def test_board_renders_without_notices_and_without_the_key(env, site_env):
    board = _board(RR, 6, param=3)
    pages = [
        _render(env, board),
        _render(env, board, drop=('notices',)),
        _render_site(site_env, board, name='site'),
        _render_site(site_env, board, name='site', drop=('notices',)),
        _render_site(site_env, board, name='bote', drop=('notices',)),
    ]

    for html in pages:
        assert 'instead of the configured' not in html
        assert 'single knockout' not in html


def test_undersized_pool_notice_is_rendered(env):
    html = _render(env, _board(FFA, 8, param=4, undersized=_UNDERSIZED))

    banner = html[html.index('data-lt-seed-banners') :]
    assert _UNDERSIZED_TEXT in banner
    assert 'nt-warn' in banner


def test_no_undersized_notice_without_a_short_pool(env):
    html = _render(env, _board(FFA, 8, param=4))

    assert 'Because participants were removed' not in html


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_undersized_pool_notice_is_rendered(site_env, name):
    html = _render_site(
        site_env,
        _board(FFA, 8, param=4, undersized=_UNDERSIZED),
        name=name,
    )

    assert _UNDERSIZED_TEXT in html[html.index('data-lt-seed-banners') :]


def test_grand_final_audit_names_its_size():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-generated',
        initiator_id=None,
        data={'target': 'ffa:GF', 'contestants': ['c1', 'c2', 'c3']},
    )

    (row,) = helpers.seeding_audit_rows([entry], {})

    assert row['details'] == 'Grand Final with 3 contestants'


def test_undersized_audit_entry_has_a_label_and_details():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-lobby-undersized',
        initiator_id=None,
        data={'pool': 'LB', 'round': 0, 'count': 3, 'lobbies': [3], 'minimum': 4},
    )

    (row,) = helpers.seeding_audit_rows([entry], {})

    assert row['label'] == 'Lobby below the minimum'
    assert row['details'] == (
        'Removed participants: the losers pool has lobbies of 3,'
        ' below the minimum of 4.'
    )


_BYE_TEXT = (
    'Bye: Player 03 is alone in the losers pool and gets no lobby this'
    ' round. They join the next losers round, or the Grand Final.'
)


def test_bye_notice_names_the_contestant(env):
    html = _render(env, _board(FFA, 8, param=4, byes=('Player 03',)))

    banner = html[html.index('data-lt-seed-banners') :]
    assert _BYE_TEXT in banner


def test_no_bye_notice_without_a_bye(env):
    html = _render(env, _board(FFA, 8, param=4))

    assert 'is alone in the losers pool' not in html


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_bye_notice_names_the_contestant(site_env, name):
    html = _render_site(
        site_env,
        _board(FFA, 8, param=4, byes=('Player 03',)),
        name=name,
    )

    assert _BYE_TEXT in html[html.index('data-lt-seed-banners') :]


def test_bye_audit_entry_names_the_contestant():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-lobby-bye',
        initiator_id=None,
        data={'pool': 'LB', 'round': 0, 'contestant': 'c03'},
    )

    (row,) = helpers.seeding_audit_rows([entry], {}, {'c03': 'Player 03'})

    assert row['label'] == 'Bye'
    assert row['details'] == 'Bye: Player 03 waits in the losers pool, round 0.'


def test_invalid_layout_shows_the_match_and_blocks_generating(env):
    html = _render(env, _board(layout_swaps=[(4, 1)]))

    assert 'lt-seed-match is-inv' in html
    assert 'has two byes' in html
    assert 'Layout invalid. Generating is blocked.' in html
    assert 'No code while the layout is invalid.' in html
    assert 'Layout invalid: Match' in html


def test_locked_board_has_no_handles_or_forms(env):
    html = _render(
        env,
        _board(
            generation=GenerationStatus.LOCKED,
            locked_reason='The seeding is locked once the tournament has started.',
        ),
        status='ONGOING',
    )

    assert 'lt-seed-hd' not in html
    assert 'name="p"' not in html
    assert 'data-replay' not in html
    assert 'aria-disabled="true"' in html
    assert 'The tournament has started.' in html


def test_ffa_board_renders_tiers_and_balance(env):
    html = _render(env, _board(FFA, 20, param=5, tier_count=4))

    assert html.count('<section class="lt-seed-tier"') == 4
    assert 'data-panel="tier"' in html
    assert 'Lobby balance' in html
    assert 'name="contestant_id"' in html
    assert 'name="n" value="4"' in html


def test_board_json_carries_no_markup_breaking_content(env):
    board = _board()
    payload = helpers.seeding_board_payload(board)
    payload['layout']['matches'][0]['slots'][0]['name'] = '</script><b>x'

    html = env.get_template('admin').render(
        party=SimpleNamespace(id='p0', title='Party'),
        tournament=SimpleNamespace(
            id='t0',
            name='T',
            tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
            contestant_type=SimpleNamespace(name='SOLO'),
            game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
            has_playoffs=False,
            elimination_mode=None,
        ),
        board=payload,
        audit_rows=[],
    )

    assert '</script><b>x' not in html
    assert html.count('</script>') == 3


def test_script_is_included_in_the_scripts_block():
    text = _ADMIN_TEMPLATE.read_text()

    block = text[text.index('{% block scripts %}') :]
    assert 'behavior/lan_tournament_seeding.js' in block
    assert 'behavior/sortable.min.js' in block


def test_no_build_attribution_in_shipped_assets():
    shipped = [
        _ADMIN_TEMPLATE,
        _ADMIN_BOARD,
        pathlib.Path('byceps/static/behavior/lan_tournament_seeding.js'),
        pathlib.Path('byceps/static/style/lan_tournament_seeding.css'),
    ]
    for path in shipped:
        text = path.read_text().lower()
        assert 'claude' not in text, path
        assert 'kevins_workflow' not in text, path


@pytest.fixture(scope='module')
def site_env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'layout/site/lan_tournament.html': _LAYOUT,
                'layout/base.html': _LAYOUT,
                'site/lan_tournament/_bote_seeding_style.html': (
                    _BOTE_STYLE.read_text()
                ),
                'site/lan_tournament/_seeding_board.html': (
                    _SITE_BOARD.read_text()
                ),
                'site': _SITE_TEMPLATE.read_text(),
                'bote': _BOTE_TEMPLATE.read_text(),
                'admin': _ADMIN_TEMPLATE.read_text(),
                'admin/lan_tournament/_seeding_board.html': (
                    _ADMIN_BOARD.read_text()
                ),
                'admin/lan_tournament/_subnav.html': (
                    _ADMIN_SUBNAV.read_text()
                ),
                'layout/admin/lan_tournament.html': _LAYOUT,
                'orga': _ORGA_ACTIONS_TEMPLATE.read_text(),
                'macros/icons.html': (
                    '{% macro render_icon(name) %}{% endmacro %}'
                ),
                'macros/lan_tournament.html': (
                    '{% macro render_match_ref(m) %}{{ m.id }}{% endmacro %}'
                    "{% macro render_tournament_subnav(t, active_tab='') %}"
                    '<div class="subnav" data-active="{{ active_tab }}">'
                    '{{ t.id }}</div>{% endmacro %}'
                ),
            }
        ),
    )
    e.globals['_'] = _translate
    e.globals['ngettext'] = _ntranslate
    e.globals['url_for'] = lambda endpoint, **k: '/' + endpoint.lstrip('.')
    e.filters['dateformat'] = lambda value: value.strftime('%Y-%m-%d')
    e.filters['timeformat'] = lambda value, kind='short': value.strftime(
        '%H:%M'
    )
    return e


def _render_site(
    env, board, *, name='site', status='REGISTRATION_CLOSED', rows=(), drop=()
):
    payload = _payload(board)
    for key in drop:
        del payload[key]
    return env.get_template(name).render(
        tournament=SimpleNamespace(
            id='t0',
            name='Blitzschach am Kamin',
            tournament_status=SimpleNamespace(name=status),
            contestant_type=SimpleNamespace(name='SOLO'),
            game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
            has_playoffs=False,
            elimination_mode=None,
        ),
        board=payload,
        audit_rows=list(rows),
    )


def test_site_orga_pages_render_the_subnav(site_env):
    html = _render_site(site_env, _board(), name='site')

    assert html.index('class="breadcrumbs"') < html.index('class="subnav"')
    assert '<div class="subnav" data-active="">t0</div>' in html
    head = html[html.index('<header class="head">') :]
    head = head[: head.index('</header>')]
    assert 'Orga actions · Blitzschach am Kamin' in head
    assert '<h1 class="title">Seeding</h1>' in head


def _board_root(html):
    """Return the markup of the `lt-seed` root, closing tag included."""
    start = html.index('<div class="lt-seed')
    depth = 0
    for tag in re.finditer(r'<(/?)div\b', html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[start : start + tag.end() + len('>')]
    raise AssertionError('unbalanced root')


@pytest.mark.parametrize(
    ('fmt', 'n', 'param', 'tier_count'),
    [
        (SE, 12, 0, 1),
        (DE, 8, 0, 1),
        (RR, 8, 2, 1),
        (FFA, 20, 5, 4),
    ],
)
@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_seeding_renders_strict(site_env, name, fmt, n, param, tier_count):
    html = _render_site(
        site_env,
        _board(fmt, n, param=param, tier_count=tier_count),
        name=name,
    )

    assert 'class="lt-seed lt-seed-site"' in html
    assert 'data-lt-seed-root' in html
    assert 'name="p"' in html
    assert html.count('data-lt-seed-board') == 1
    assert '%(' not in _markup(html)


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_seeding_renders_strict_for_every_generation_state(site_env, name):
    for generation in GenerationStatus:
        html = _render_site(
            site_env,
            _board(
                generation=generation,
                generated_code='S1A8EZAJYXM0155F',
                locked_reason='The seeding is locked.'
                if generation is GenerationStatus.LOCKED
                else None,
            ),
            name=name,
            status='ONGOING'
            if generation is GenerationStatus.LOCKED
            else 'REGISTRATION_CLOSED',
        )
        assert '%(' not in _markup(html)


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_seeding_posts_to_the_orga_routes(site_env, name):
    html = _render_site(
        site_env,
        _board(generation=GenerationStatus.DIFFERS, generated_code='S1A8EZAJ'),
        name=name,
    )

    assert 'data-action-url="/orga_seeding_action"' in html
    assert 'data-page-url="/orga_seeding"' in html
    assert 'action="/orga_seeding_generate"' in html
    assert '"/seeding_action"' not in html
    assert '"/seeding_generate"' not in html


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_seeding_stale_banner_offers_the_reseed(site_env, name):
    html = _render_site(
        site_env,
        _board(stale=True, leavers=('Kaffeesatz',), joiners=('Zahnrad',)),
        name=name,
    )

    banner = html[html.index('data-lt-seed-banners') :]
    assert 'nt-err' in banner
    assert 'Kaffeesatz' in banner
    assert 'value="reseed_keep_tiers"' in banner
    assert '<s>S1A8-EZAJ-YXM0-155F-CQ9A</s>' in html


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_seeding_renders_the_audit_trail(site_env, name):
    rows = helpers.seeding_audit_rows(
        [
            SimpleNamespace(
                occurred_at=datetime(2026, 9, 30, 18, 2, tzinfo=UTC),
                event_type='seeding-swapped',
                initiator_id='u1',
                data={'seed_code': 'S1A8EZAJYXM0155FCQ9AG0C413'},
            ),
        ],
        {'u1': SimpleNamespace(screen_name='Ohrwurm')},
    )

    html = _render_site(site_env, _board(), name=name, rows=rows)

    assert 'lt-seed-audit' in html
    assert 'Ohrwurm' in html
    assert '>seeding-swapped<' not in html


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_site_locked_board_has_no_handles_or_forms(site_env, name):
    html = _render_site(
        site_env,
        _board(
            generation=GenerationStatus.LOCKED,
            locked_reason='The seeding is locked once the tournament has started.',
        ),
        name=name,
        status='ONGOING',
    )

    assert 'lt-seed-hd' not in html
    assert 'name="p"' not in html
    assert 'data-replay' not in html


def test_site_board_markup_matches_the_admin_board(site_env):
    board = _board(FFA, 20, param=5, tier_count=4, stale=True)
    admin = site_env.get_template('admin').render(
        party=SimpleNamespace(id='p0', title='Party'),
        tournament=SimpleNamespace(
            id='t0',
            name='Blitzschach am Kamin',
            tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
            contestant_type=SimpleNamespace(name='SOLO'),
            game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
            has_playoffs=False,
            elimination_mode=None,
        ),
        board=_payload(board),
        audit_rows=[],
    )
    site = _render_site(site_env, board)

    def normalize(root):
        return root.replace('orga_seeding', 'seeding').replace(
            'lt-seed lt-seed-site', 'lt-seed'
        )

    assert normalize(_board_root(site)) == _board_root(admin)


def test_bote_override_renders_the_same_board_as_the_generic_page(site_env):
    board = _board(
        generation=GenerationStatus.DIFFERS,
        generated_code='S1A8EZAJYXM0155F',
        stale=True,
        leavers=('Kaffeesatz',),
    )

    assert _board_root(_render_site(site_env, board, name='bote')) == (
        _board_root(_render_site(site_env, board))
    )


def test_bote_override_wraps_the_board_in_the_theme_page(site_env):
    html = _render_site(site_env, _board(), name='bote')

    assert 'bote-page tournament-page seeding-page' in html
    assert 'class="crumbs"' in html
    assert '<style>' in html
    assert '--ls-ink: var(--ink)' in html


@pytest.mark.parametrize('path', [_SITE_TEMPLATE, _BOTE_TEMPLATE])
def test_site_scripts_are_included_in_the_scripts_block(path):
    text = path.read_text()

    block = text[text.index('{% block scripts %}') :]
    assert 'behavior/lan_tournament_seeding.js' in block
    assert 'behavior/sortable.min.js' in block


@pytest.mark.parametrize('status', ['REGISTRATION_CLOSED', 'ONGOING', 'PAUSED'])
def test_orga_actions_link_to_the_seeding(site_env, status):
    out = site_env.get_template(
        'orga'
    ).module.render_orga_tournament_status_actions(
        SimpleNamespace(
            id='t0',
            tournament_status=SimpleNamespace(name=status),
            game_format=SimpleNamespace(name='ONE_V_ONE'),
        ),
        True,
    )

    assert 'href="/orga_seeding"' in out
    assert 'Open seeding' in out


def test_orga_actions_offer_no_seeding_for_highscore(site_env):
    out = site_env.get_template(
        'orga'
    ).module.render_orga_tournament_status_actions(
        SimpleNamespace(
            id='t0',
            tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
            game_format=SimpleNamespace(name='HIGHSCORE'),
        ),
        True,
    )

    assert 'Start tournament' in out
    assert 'orga_seeding' not in out


def test_orga_actions_link_is_absent_without_the_right(site_env):
    out = site_env.get_template(
        'orga'
    ).module.render_orga_tournament_status_actions(
        SimpleNamespace(
            id='t0',
            tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
            game_format=SimpleNamespace(name='ONE_V_ONE'),
        ),
        False,
    )

    assert 'orga_seeding' not in out


def test_no_build_attribution_in_site_assets():
    for path in (_SITE_TEMPLATE, _SITE_BOARD, _BOTE_TEMPLATE, _BOTE_STYLE):
        text = path.read_text().lower()
        assert 'claude' not in text, path
        assert 'kevins_workflow' not in text, path


_NEU = '<span class="lt-seed-neu">new</span>'


def _marked_boards():
    return [
        _board(SE, 8, new=['c03']),
        _board(FFA, 8, param=4, tier_count=2, new=['c03']),
    ]


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('name', ['admin', 'site', 'bote'])
def test_new_entrant_is_marked_on_every_surface(site_env, name, index):
    board = _marked_boards()[index]

    html = _markup(
        _render_site(site_env, board, name=name)
        if name != 'admin'
        else site_env.get_template('admin').render(
            party=SimpleNamespace(id='p0', title='Party'),
            tournament=SimpleNamespace(
                id='t0',
                name='Blitzschach am Kamin',
                tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
                contestant_type=SimpleNamespace(name='SOLO'),
                game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
                has_playoffs=False,
                elimination_mode=None,
            ),
            board=helpers.seeding_board_payload(board),
            audit_rows=[],
        )
    )

    assert html.count(_NEU) == (1 if index == 0 else 2)
    marked = re.search(
        r'<span class="lt-seed-nm">Player 03</span>\s*' + re.escape(_NEU), html
    )
    assert marked


@pytest.mark.parametrize('name', ['site', 'bote'])
def test_no_marker_without_new_entrants(site_env, name):
    board = _board(FFA, 8, param=4, tier_count=2)

    html = _render_site(site_env, board, name=name)

    assert 'lt-seed-neu' not in _markup(html)


def test_admin_renders_the_new_entrant_marker(env):
    html = _markup(_render(env, _board(SE, 8, new=['c03'])))

    assert html.count(_NEU) == 1


@pytest.mark.parametrize('path', [_SITE_TEMPLATE, _BOTE_TEMPLATE])
def test_site_pages_share_one_board_partial(path):
    text = path.read_text()

    assert "{% include 'site/lan_tournament/_seeding_board.html' %}" in text
    assert 'lt-seed-root' not in text
    assert 'data-lt-seed-root' not in text
    assert 'lt-seed-neu' not in text
    assert 'data-lt-seed-root' in _SITE_BOARD.read_text()


def _render_empty(env, name, *, status='REGISTRATION_OPEN'):
    return env.get_template(name).render(
        party=SimpleNamespace(id='p0', title='Party'),
        tournament=SimpleNamespace(
            id='t0',
            name='Blitzschach am Kamin',
            tournament_status=(
                SimpleNamespace(name=status) if status else None
            ),
            contestant_type=SimpleNamespace(name='SOLO'),
            game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
            has_playoffs=False,
            elimination_mode=None,
        ),
        board=None,
    )


@pytest.mark.parametrize('surface', ['admin', 'site', 'bote'])
def test_board_none_renders_the_empty_state(site_env, surface):
    html = _render_empty(site_env, surface)

    assert 'Initial placement (round 1)' in html
    assert 'Registration open' in html
    assert 'The seeding opens once registration is closed.' in html
    assert 'nt-info' in html
    assert 'data-lt-seed-board' not in html
    assert 'name="p"' not in html
    assert 'data-replay' not in html
    assert '%(' not in _markup(html)


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_board_none_renders_without_a_tournament_status(site_env, surface):
    html = _render_empty(site_env, surface, status=None)

    assert 'The seeding opens once registration is closed.' in html


def test_board_none_renders_the_empty_state_on_the_admin_env(env):
    html = _render_empty(env, 'admin')

    assert 'Initial placement (round 1)' in html
    assert 'The seeding opens once registration is closed.' in html


@pytest.mark.parametrize('surface', ['admin', 'site'])
def test_invalid_layout_shows_no_code(site_env, surface):
    board = dataclasses.replace(
        _board(layout_swaps=[(4, 1)]), code='S1A8-EZAJ-YXM0-155F-CQ9A'
    )
    if surface == 'admin':
        html = site_env.get_template('admin').render(
            party=SimpleNamespace(id='p0', title='Party'),
            tournament=SimpleNamespace(
                id='t0',
                name='Blitzschach am Kamin',
                tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
                contestant_type=SimpleNamespace(name='SOLO'),
                game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
                has_playoffs=False,
                elimination_mode=None,
            ),
            board=_payload(board),
            audit_rows=[],
        )
    else:
        html = _render_site(site_env, board)

    markup = _markup(html)
    assert 'No code while the layout is invalid.' in markup
    assert 'id="lt-seed-code"' not in markup
    assert 'Copy code' not in markup


@pytest.mark.parametrize('name', ['admin', 'site'])
def test_stale_generated_board_blocks_generation(site_env, name):
    def render(stale):
        board = _board(
            stale=stale,
            generation=GenerationStatus.MATCHES,
            generated_code='S1A8EZAJYXM0155F',
        )
        if name == 'admin':
            return _markup(
                site_env.get_template('admin').render(
                    party=SimpleNamespace(id='p0', title='Party'),
                    tournament=SimpleNamespace(
                        id='t0',
                        name='Blitzschach am Kamin',
                        tournament_status=SimpleNamespace(
                            name='REGISTRATION_CLOSED'
                        ),
                        contestant_type=SimpleNamespace(name='SOLO'),
                        game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
                        has_playoffs=False,
                        elimination_mode=None,
                    ),
                    board=_payload(board),
                    audit_rows=[],
                )
            )
        return _markup(_render_site(site_env, board))

    stale = render(True)
    assert 'Generating blocked: seed code outdated.' in stale
    assert (
        'Re-seed first. Starting stays blocked until the board is generated'
        ' again.' in stale
    )
    assert 'The board matches the generated state.' not in stale

    fresh = render(False)
    assert 'The board matches the generated state.' in fresh
    assert 'Generating blocked: seed code outdated.' not in fresh


@pytest.mark.parametrize('name', ['admin', 'site'])
def test_swap_form_preselects_two_slots(site_env, name):
    board = _board(SE, 8)
    if name == 'admin':
        html = site_env.get_template('admin').render(
            party=SimpleNamespace(id='p0', title='Party'),
            tournament=SimpleNamespace(
                id='t0',
                name='Blitzschach am Kamin',
                tournament_status=SimpleNamespace(name='REGISTRATION_CLOSED'),
                contestant_type=SimpleNamespace(name='SOLO'),
                game_format=SimpleNamespace(name='SINGLE_ELIMINATION'),
                has_playoffs=False,
                elimination_mode=None,
            ),
            board=_payload(board),
            audit_rows=[],
        )
    else:
        html = _render_site(site_env, board)

    def select(select_id):
        match = re.search(
            rf'<select[^>]*id="{select_id}".*?</select>', html, flags=re.S
        )
        return match.group(0)

    assert '<option value="1" selected>' in select('seeding-swap-q')
    assert select('seeding-swap-q').count('selected') == 1
    assert 'selected' not in select('seeding-swap-p')


# fmt: off
@pytest.mark.parametrize(('target', 'expected'), [
    ('ffa:SE:0', 'Round 1'),
    ('ffa:SE:1', 'Round 2 · prefilled from the standings after round 1'),
    ('ffa:SE:2', 'Round 3 · prefilled from the standings after round 2'),
    ('ffa:WB:2', 'Round 3 · winners pool, prefilled from the standings after round 2'),
    ('ffa:LB:3', 'Round 4 · losers pool, prefilled from the standings after round 3'),
])
# fmt: on
def test_ffa_round_label_is_one_based(target, expected):
    assert helpers.seeding_target_label(target) == expected


def test_group_target_label_has_groups_suffix():
    assert (
        helpers.seeding_target_label('initial', RR)
        == 'Initial placement (round 1) · groups'
    )
    assert helpers.seeding_target_label('initial', SE) == (
        'Initial placement (round 1)'
    )
    assert helpers.seeding_target_label('initial') == (
        'Initial placement (round 1)'
    )
    assert _payload(_board(RR, 8, param=2))['target_label'] == (
        'Initial placement (round 1) · groups'
    )
    assert _payload(_board(SE, 8))['target_label'] == (
        'Initial placement (round 1)'
    )


def test_fingerprint_line_names_code_and_current_roster(env, site_env):
    board = _board(
        stale=True,
        real_code=True,
        leavers=('Player 03',),
        leaver_ids=('c03',),
        joiners=('Zahnrad',),
        joiner_ids=('c99',),
    )
    payload = _payload(board)
    ids = [i for i in _ids(12) if i != 'c03'] + ['c99']
    current = f'{seed_code.roster_fingerprint(ids):08x}'
    in_code = f'{seed_code.roster_fingerprint(_ids(12)):08x}'

    assert payload['code_meta']['fingerprint'] == in_code
    assert payload['code_meta']['current_fingerprint'] == current
    line = (
        f'Fingerprint in the code {in_code}, current roster {current}.'
    )
    assert line in _render(env, board)
    assert line in _render_site(site_env, board)


def test_code_meta_decodes_the_code_and_falls_back_to_the_state():
    real = _payload(_board(real_code=True))['code_meta']
    assert real['draw_number'] == '5eed0012'
    assert real['current_fingerprint'] == real['fingerprint']

    fake = _payload(_board())['code_meta']
    assert fake['draw_number'] == '5eed0012'
    assert fake['fingerprint'] == f'{seed_code.roster_fingerprint(_ids(12)):08x}'


def test_regenerate_dialog_uses_short_codes(env, site_env):
    board = _board(
        real_code=True,
        generation=GenerationStatus.DIFFERS,
        generated_code='S1A8EZAJYXM0155FCQ9AG0C413',
    )
    payload = _payload(board)
    code = board.code.replace('-', '')
    assert payload['code_short'] == seed_code.format_seed_code(code)[:9] + '-…'
    assert payload['generated_code_short'] == 'S1A8-EZAJ-…'

    for html in (_render(env, board), _render_site(site_env, board)):
        body = re.search(r'data-confirm-body="([^"]*)"', html).group(1)
        assert body == (
            f'The bracket is generated again from {payload["code_short"]}.'
            ' The previous code S1A8-EZAJ-… stays in the audit trail. Until'
            ' you regenerate, a start uses the generated state.'
        )
        assert board.code not in body
        assert 'Current board' not in html
        assert 'Seeding board: <code>' in html


def _generated_row(**overrides):
    row = {
        'occurred_at': datetime(2026, 9, 30, 18, 5, tzinfo=UTC),
        'event_type': 'bracket-generated',
        'who': 'Ohrwurm',
        'initiator_id': 'u1',
        'label': 'Bracket generated',
        'details': '',
        'children': [],
        'target': 'initial',
        'seed_code': 'S1A8EZAJYXM0155FCQ9AG0C413',
        'time_label': '30.09. 18:05',
    }
    return row | overrides


def test_generated_notice_names_the_actor_and_time_of_the_generation(
    env, site_env
):
    board = _board(
        generation=GenerationStatus.MATCHES,
        generated_code='S1A8EZAJYXM0155FCQ9AG0C413',
    )
    rows = [
        _generated_row(who='Spaetling', time_label='18:09', target='playoff'),
        _generated_row(who='Nachgenerierer', time_label='18:07'),
        _generated_row(who=None, time_label='18:06', seed_code='OTHER'),
        _generated_row(),
    ]

    admin = _render(env, board, rows=rows)
    site = _render_site(site_env, board, rows=rows)

    for html in (admin, site):
        assert 'Nachgenerierer · 18:07. The board matches' in html
        assert 'Spaetling' not in _markup(html).split('The board matches')[0]


def test_generated_notice_has_no_prefix_without_a_matching_row(env, site_env):
    board = _board(
        generation=GenerationStatus.MATCHES,
        generated_code='S1A8EZAJYXM0155FCQ9AG0C413',
    )
    rows = [
        _generated_row(target='playoff'),
        _generated_row(seed_code='OTHER'),
        _generated_row(event_type='seeding-swapped'),
    ]

    for html in (
        _render(env, board),
        _render(env, board, rows=rows),
        _render_site(site_env, board, rows=rows),
    ):
        assert '<p>The board matches the generated state.</p>' in html
        assert 'Ohrwurm' not in _markup(html).split('The board matches')[0]


def test_locked_initial_board_says_the_tournament_started(env, site_env):
    board = _board(
        generation=GenerationStatus.LOCKED,
        locked_reason='The seeding is locked once the tournament has started.',
    )
    for html in (
        _render(env, board, status='ONGOING'),
        _render_site(site_env, board, status='ONGOING'),
    ):
        assert 'The tournament has started.' in html
        assert 'Placement locked' not in html
        assert '<span class="lt-seed-tag t-ok">Running</span>' in html
        assert '>Ongoing<' not in html


def test_locked_ffa_round_says_round_running(env, site_env):
    board = _board(
        FFA,
        12,
        param=4,
        tier_count=3,
        target='ffa:WB:2',
        generation=GenerationStatus.LOCKED,
        locked_reason='A result is confirmed.',
    )
    for html in (
        _render(env, board, status='ONGOING'),
        _render_site(site_env, board, status='ONGOING'),
    ):
        assert 'Round 3 is running.' in html
        assert 'The tournament has started.' not in html
        assert 'Placement locked' not in html


def test_board_uses_module_msgids_for_sum_time_and_moves(env, site_env):
    rows = [_generated_row()]
    boards = [
        _board(FFA, 12, param=4, tier_count=3),
        _board(SE, 8, generation=GenerationStatus.MATCHES),
    ]
    for board in boards:
        for html in (
            _render(env, board, rows=rows),
            _render_site(site_env, board, rows=rows),
        ):
            markup = _markup(html)
            for shared in ('Total', 'Time', 'Move up', 'Move down'):
                assert shared not in markup
            assert '>At<' in markup
    html = _render(env, boards[0], rows=rows)
    assert 'Tier sum' in html
    assert 'Move higher' in html
    assert 'Move lower' in html
    assert '30.09. 18:05' in html


def test_audit_rows_carry_target_and_seed_code_and_a_time_label():
    entries = [
        SimpleNamespace(
            occurred_at=datetime(2026, 9, 30, 18, 4, tzinfo=UTC),
            event_type='seeding-swapped',
            initiator_id='u1',
            data={'target': 'initial', 'seed_code': 'S1A8EZAJYXM0155F'},
        ),
        SimpleNamespace(
            occurred_at=datetime(2026, 9, 30, 18, 2, tzinfo=UTC),
            event_type='seeding-swapped',
            initiator_id='u1',
            data={},
        ),
        SimpleNamespace(
            occurred_at=datetime(2026, 9, 29, 9, 5),
            event_type='playoffs-released',
            initiator_id=None,
            data={},
        ),
    ]

    rows = helpers.seeding_audit_rows(
        entries,
        {'u1': SimpleNamespace(screen_name='Ohrwurm')},
        now=datetime(2026, 9, 30, 20, 0, tzinfo=UTC),
    )

    assert [r['target'] for r in rows] == ['initial', None]
    assert rows[0]['seed_code'] == 'S1A8EZAJYXM0155F'
    assert rows[0]['time_label'] == '18:04'
    assert [c['target'] for c in rows[0]['children']] == ['initial', None]
    assert rows[0]['children'][1]['seed_code'] is None
    assert rows[1]['time_label'] == '29.09. 09:05'


def test_bote_seeding_page_renders_the_subnav(site_env):
    html = _render_site(site_env, _board(), name='bote')

    assert '<div class="subnav" data-active="">t0</div>' in html
    page = html[html.index('<header class="head">') :]
    assert page.index('</header>') < page.index('class="subnav"')
    assert page.index('class="subnav"') < page.index('lt-seed-site')
    assert '.seeding-page .subnav' in html


def _playoff_board(fmt=SE, n=8, **overrides):
    kwargs = {'param': 4, 'tier_count': 3} if fmt is FFA else {}
    board = _board(fmt, n, target='playoff', **kwargs)
    return dataclasses.replace(board, **overrides)


def _tags(html):
    return re.findall(r'<span class="lt-seed-tag [^"]*">([^<]*)</span>', html)


def _surfaces(env, site_env, board, **kwargs):
    return (
        _render(env, board, **kwargs),
        _render_site(site_env, board, **kwargs),
    )


def test_prefilled_label(env, site_env):
    prefilled = _playoff_board(prefilled=True, fix_count=2)
    by_hand = _playoff_board(prefilled=False)

    assert _payload(prefilled)['prefilled'] is True
    assert _payload(by_hand)['prefilled'] is False
    for html in _surfaces(env, site_env, prefilled):
        assert '<dd>prefilled</dd>' in html
        assert '<dd>pure draw</dd>' not in html
        assert '>Reset layout fixes</span>' not in html
        assert 'Reset layout fixes (2)' in html
        assert '2 swaps' in html
    for html in _surfaces(env, site_env, by_hand):
        assert '<dd>prefilled</dd>' not in html
        assert '<dd>pure draw</dd>' in html


def test_prefilled_ffa_playoff_names_the_tier_origin(env, site_env):
    prefilled = _playoff_board(FFA, 12, prefilled=True)
    by_hand = _playoff_board(FFA, 12, prefilled=False)
    initial = dataclasses.replace(
        _board(FFA, 12, param=4, tier_count=3), prefilled=True
    )

    assert _payload(by_hand)['tier_origin'] is None
    assert _payload(initial)['tier_origin'] is None
    for html in _surfaces(env, site_env, prefilled):
        assert (
            'From the leaderboard: places 1–4 tier A, 5–8 tier B,'
            ' 9–12 tier C. A tie across a band edge would only be a hint'
            ' here, not an obstacle.'
        ) in html
        assert 'Origin of the tiers' in html
    for html in _surfaces(env, site_env, by_hand):
        assert 'From the leaderboard' not in html


def test_playoff_draft_tags(env, site_env):
    draft = _playoff_board()
    generated = _playoff_board(
        generation=GenerationStatus.MATCHES,
        generated_code='S1A8EZAJYXM0155FCQ9AG0C413',
    )
    by_system = [
        _generated_row(target='playoff', who=None, time_label='21:47')
    ]
    by_actor = [_generated_row(target='playoff')]

    for html in _surfaces(env, site_env, draft, status='ONGOING'):
        assert 'Seeding draft' in _tags(html)
        assert 'Not generated' not in _tags(html)
    for html in _surfaces(env, site_env, generated, rows=by_system):
        assert 'Generated by System' in _tags(html)
        assert 'Editable until the first result' in _tags(html)
    for html in _surfaces(env, site_env, generated, rows=by_actor):
        tags = _tags(html)
        assert 'Generated' in tags
        assert 'Generated by System' not in tags
        assert 'Editable until the first result' not in tags
    initial = _board(generation=GenerationStatus.NOT_GENERATED)
    for html in _surfaces(env, site_env, initial):
        assert 'Not generated' in _tags(html)
        assert 'Seeding draft' not in _tags(html)


def test_playoff_generator_follows_the_current_regeneration(env, site_env):
    code = 'S1A8EZAJYXM0155FCQ9AG0C413'
    board = _playoff_board(
        generation=GenerationStatus.MATCHES, generated_code=code
    )
    rows = [
        _generated_row(target='initial', who='Anfaenger'),
        _generated_row(
            event_type='bracket-regenerated',
            target='playoff',
            who='Ohrwurm',
            time_label='22:01',
        ),
        _generated_row(target='ffa:WB:1', who='Runde'),
        _generated_row(target='playoff', who=None, time_label='21:47'),
    ]

    for html in _surfaces(env, site_env, board, rows=rows):
        assert 'Ohrwurm · 22:01. The board matches' in html
        assert 'Generated by System' not in _tags(html)
        assert _tags(html).count('Generated') == 1

    auto_only = rows[3:]
    for html in _surfaces(env, site_env, board, rows=auto_only):
        assert 'Generated by System' in _tags(html)
        assert 'System · 21:47. The board matches' in html


def test_purged_log_shows_no_generator(env, site_env):
    code = 'S1A8EZAJYXM0155FCQ9AG0C413'
    board = _playoff_board(
        generation=GenerationStatus.MATCHES, generated_code=code
    )
    foreign = [
        _generated_row(target='initial', who=None),
        _generated_row(target='playoff', who=None, seed_code='OTHER'),
        _generated_row(target='playoff', event_type='seeding-swapped'),
    ]

    for rows in ((), foreign):
        for html in _surfaces(env, site_env, board, rows=rows):
            tags = _tags(html)
            assert 'Generated' in tags
            assert 'Generated by System' not in tags
            assert 'Editable until the first result' not in tags
            assert '<p>The board matches the generated state.</p>' in html
        assert 'Generated by System' not in _tags(_render(env, board))


def _same_group_board(**overrides):
    ids = _ids(8)
    origins = {
        cid: f'{chr(ord("A") + i // 2)}{i % 2 + 1}'
        for i, cid in enumerate(ids)
    }
    return _playoff_board(
        origin_labels=origins, same_group_matches=(1,), **overrides
    )


def _clash(board):
    return _payload(board)['layout']['matches'][0]['same_group_info']


def test_same_group_notice_before_the_target_card(env, site_env):
    board = _same_group_board()

    clash = _clash(board)
    sentence = (
        f'M1: {clash["a"]["name"]} ({clash["a"]["origin"]})'
        f' and {clash["b"]["name"]} ({clash["b"]["origin"]})'
        f' are both from group {clash["group"]}.'
    )
    for html in _surfaces(env, site_env, board):
        markup = _markup(html)
        notice = markup.index('Separate same-group pairings')
        card = markup.index('class="lt-seed-tgt"')
        assert markup.index(sentence) < card
        assert notice < card
        assert 'data-lt-seed-banners' in markup[card:]
        assert 'Separate same-group pairings' not in markup[card:]


def test_same_group_lines_in_the_placement_check(env, site_env):
    board = _same_group_board()
    clean = _playoff_board()

    clash = _clash(board)
    line = (
        f'<li><i aria-hidden="true">▲</i>M1: {clash["a"]["name"]}'
        f' and {clash["b"]["name"]} come from group {clash["group"]}.</li>'
    )
    for html in _surfaces(env, site_env, board):
        check = _markup(html).split('Placement check')[1].split('Seed code')[0]
        assert line in check
    stale = _same_group_board(stale=True)
    for html in _surfaces(env, site_env, stale):
        assert line in html
    for html in _surfaces(env, site_env, clean):
        assert 'come from group' not in html


def test_playoff_format_line_starts_with_playoffs(env, site_env):
    for html in _surfaces(env, site_env, _playoff_board()):
        assert 'Playoffs · Single knockout · 8 Players' in html
    for html in _surfaces(env, site_env, _board()):
        assert 'Playoffs · ' not in _markup(html)
