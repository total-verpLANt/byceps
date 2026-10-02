"""
tests.unit.services.lan_tournament.test_seeding_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
import pathlib
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
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
    return SeedingBoard(
        tournament_id='t0',
        target=target,
        state=state,
        version=3,
        code=None if problems else 'S1A8-EZAJ-YXM0-155F-CQ9A',
        stale=stale,
        labels={cid: f'Player {cid[1:]}' for cid in ids},
        stale_leavers=tuple(leavers),
        stale_joiners=tuple(joiners),
        stale_leaver_ids=(),
        stale_joiner_ids=(),
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
    assert 'Placement locked' in html


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
                'layout/admin/lan_tournament.html': _LAYOUT,
                'orga': _ORGA_ACTIONS_TEMPLATE.read_text(),
                'macros/icons.html': (
                    '{% macro render_icon(name) %}{% endmacro %}'
                ),
                'macros/lan_tournament.html': (
                    '{% macro render_match_ref(m) %}{{ m.id }}{% endmacro %}'
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
        ),
        board=payload,
        audit_rows=list(rows),
    )


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
