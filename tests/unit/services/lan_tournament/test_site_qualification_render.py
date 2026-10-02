"""
tests.unit.services.lan_tournament.test_site_qualification_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Renders the site qualification page, in its generic and its bote form,
and the orga action links under `StrictUndefined`.
"""

import dataclasses
from datetime import datetime, UTC
import html as html_parser
import pathlib
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_qualification_domain_service as domain,
    tournament_qualification_service as service,
    tournament_seeding_domain_service as seeding_domain,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
)
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.tournament_seeding_service import (
    GenerationStatus,
    SeedingBoard,
)


_SITE_DIR = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament'
)
_BOTE_DIR = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
)
_BOTE_STYLE = _BOTE_DIR / '_bote_qualification_style.html'

_LAYOUT = (
    '{% block head %}{% endblock %}{% block before_body %}{% endblock %}'
    '{% block body %}{% endblock %}{% block scripts %}{% endblock %}'
)

STRINGS = {
    'tie_cut': 'CUT TEXT',
    'tie_seeding': 'SEEDING TEXT',
    'tie_group_win': 'GROUP WIN TEXT',
    'tie_winner': 'WINNER TEXT',
    'tie_harmless': 'HARMLESS TEXT',
    'status_qualified': 'Qualified',
    'status_out': 'Eliminated',
    'status_tie': 'Tie',
    'status_open': 'Open',
    'decided_by_orga': 'Orga decision',
    'decided_by_difference': 'Difference',
    'scope_leaderboard': 'Leaderboard',
    'scope_winner': 'Tournament win',
    'scope_group': 'Group %(letter)s',
}
_SUBNAV_STUB = (
    "{% macro render_tournament_subnav(tournament, active_tab='') %}"
    '<div class="subnav" data-active="{{ active_tab }}">'
    '{{ tournament.id }}</div>{% endmacro %}'
)
NAMES = {cid: cid.upper() for cid in 'abcdefgh'}
WHEN = datetime(2026, 9, 30, 16, 42, tzinfo=UTC)
EVIL_REASON = '<script>alert(1)</script> & "quoted"'


def _translate(message, **params):
    return message % params if params else message


def _ntranslate(singular, plural, num, **params):
    params.setdefault('num', num)
    return (singular if num == 1 else plural) % params


def _url_for(endpoint, **values):
    query = '&'.join(
        f'{k}={v}' for k, v in values.items() if k != 'tournament_id'
    )
    return f'/{endpoint.lstrip(".")}' + (f'?{query}' if query else '')


def _env(loader_files):
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'layout/site/lan_tournament.html': _LAYOUT,
                'layout/base.html': _LAYOUT,
                **loader_files,
            }
        ),
    )
    env.globals['_'] = _translate
    env.globals['ngettext'] = _ntranslate
    env.globals['url_for'] = _url_for
    env.filters['dateformat'] = lambda value: value.strftime('%Y-%m-%d')
    env.filters['numberformat'] = lambda value: f'{value:,}'.replace(',', '.')
    env.filters['timeformat'] = lambda value, kind='short': value.strftime(
        '%H:%M'
    )
    env.filters['datetimeformat'] = lambda value, pattern: value.strftime(
        pattern.replace('dd', '%d')
        .replace('MM', '%m')
        .replace('HH', '%H')
        .replace('mm', '%M')
    )
    return env


def _read(path):
    return path.read_text()


@pytest.fixture(scope='module')
def generic_env():
    return _env(
        {
            'generic': _read(_SITE_DIR / 'qualification.html'),
            'site/lan_tournament/_qualification_panels.html': _read(
                _SITE_DIR / '_qualification_panels.html'
            ),
            'site/lan_tournament/_seeding_board.html': _read(
                _SITE_DIR / '_seeding_board.html'
            ),
            'macros/lan_tournament.html': _SUBNAV_STUB,
        }
    )


@pytest.fixture(scope='module')
def bote_env():
    return _env(
        {
            'bote': _read(_BOTE_DIR / 'qualification.html'),
            'site/lan_tournament/_qualification_panels.html': _read(
                _SITE_DIR / '_qualification_panels.html'
            ),
            'site/lan_tournament/_seeding_board.html': _read(
                _SITE_DIR / '_seeding_board.html'
            ),
            'macros/lan_tournament.html': _SUBNAV_STUB,
            'site/lan_tournament/_bote_qualification_style.html': _read(
                _BOTE_STYLE
            ),
        }
    )


def _row(cid, played, won, drawn, lost, points, score_for, score_against):
    return domain.ResultRow(
        contestant_id=cid,
        played=played,
        won=won,
        drawn=drawn,
        lost=lost,
        points=points,
        score_for=score_for,
        score_against=score_against,
    )


def _entry(cid, rank, *, shared=False, decided_by=None, row=None, value=None):
    return domain.RankedEntry(
        contestant_id=cid,
        rank=rank,
        shared=shared,
        decided_by=decided_by,
        row=row,
        value=value,
    )


def _tie(scope, ids, kind, *, decided=False, rank_from=1):
    return domain.TieBlock(
        scope=scope,
        contestant_ids=ids,
        rank_from=rank_from,
        rank_to=rank_from + len(ids) - 1,
        decided=decided,
        kind=kind,
    )


def _state(
    *,
    rankings,
    blockers=(),
    source='groups',
    ready=False,
    qualifiers=None,
    released=False,
    released_by=None,
    mode=PlayoffReleaseMode.MANUAL,
    suspended=False,
    can_unrelease=False,
    open_match_count=0,
):
    return service.QualificationState(
        tournament_id='t0',
        source=source,
        rankings=tuple(rankings),
        blockers=tuple(blockers),
        open_match_count=open_match_count,
        total_match_count=6,
        ready=ready,
        qualifiers=qualifiers,
        released_at=WHEN if released else None,
        released_by=released_by,
        release_mode=mode,
        auto_release_suspended=suspended,
        can_unrelease=can_unrelease,
    )


def _group(scope, ids, *, tie=None, open_matches=0):
    rows = [
        _entry(
            cid,
            rank=1 if tie and i < 2 else i + 1,
            shared=bool(tie and i < 2),
            row=_row(cid, 3, 3 - i, 0, i, 9 - 3 * i, 7 - i, 2 + i),
        )
        for i, cid in enumerate(ids)
    ]
    ties = (tie,) if tie else ()
    return domain.Ranking(
        scope=scope,
        entries=tuple(rows),
        ties=ties,
        open_matches=open_matches,
    )


def _tournament(**overrides):
    values = {
        'playoff_qualifiers_per_group': 1,
        'playoff_qualifier_count': 2,
        'leaderboard_closed_at': None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _block(ids, reason='Tiebreak at the table.'):
    return DecisionBlock(
        contestant_ids=ids, reason=reason, decided_by='u0', decided_at=WHEN
    )


def _decision(scope='group:1'):
    return {
        scope: QualificationDecision(
            id='d0',
            tournament_id='t0',
            scope=scope,
            blocks=(_block(('d', 'c')),),
            reason='Tiebreak at the table.',
            decided_by='u0',
            decided_at=WHEN,
        )
    }


def _payload(state, *, decisions=None, tournament=None):
    return helpers.serialize_qualification(
        state,
        NAMES,
        STRINGS,
        tournament=tournament or _tournament(),
        decisions=decisions,
        users={'u0': SimpleNamespace(screen_name='Ohrwurm')},
    )


def _page_tournament(status='ONGOING', **overrides):
    values = {
        'id': 't0',
        'name': 'Kaffeefahrt',
        'contestant_type': SimpleNamespace(name='SOLO'),
        'tournament_status': SimpleNamespace(name=status),
        'playoff_game_format': None,
        'playoff_elimination_mode': SimpleNamespace(name='SINGLE_ELIMINATION'),
        'playoff_qualifier_count': 2,
        'playoff_qualifiers_per_group': 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(payload, **extra):
    context = {
        'tournament': _page_tournament(),
        'qualification': payload,
        'playoff_version': None,
        'playoff_release_open': False,
        'playoff_board': None,
        'js_strings': {'cancel': 'Cancel', 'place': 'Place %(n)s'},
        'audit_rows': [],
    }
    context.update(extra)
    return context


def _blocked_payload():
    cut = _tie('group:0', ('a', 'b'), domain.TieKind.CUT)
    harmless = _tie('group:1', ('c', 'd'), domain.TieKind.HARMLESS, rank_from=3)
    decided = _tie('group:1', ('d', 'c'), domain.TieKind.CUT, decided=True)
    state = _state(
        rankings=[
            _group('group:0', 'abef', tie=cut),
            _group('group:1', 'cdgh', tie=decided),
            _group('group:2', 'ghab', tie=harmless),
        ],
        blockers=[cut],
    )
    return _payload(state, decisions=_decision())


def _ready_payload(**kwargs):
    state = _state(
        rankings=[_group('group:0', 'ab'), _group('group:1', 'cd')],
        ready=True,
        qualifiers=(
            domain.Qualifier(
                contestant_id='a', scope='group:0', rank=1, row=None
            ),
        ),
        **kwargs,
    )
    return _payload(state)


def _banner(html):
    """Return the released banner, up to the next section."""
    start = html.index('class="lt-seed-nt nt-ok lt-qual-released"')
    return html[start : html.index('<section', start)]


def _panel(html):
    start = html.index('data-release-panel')
    return html[start : html.index('</section>', start)]


@pytest.fixture(params=['generic', 'bote'])
def template(request, generic_env, bote_env):
    env = generic_env if request.param == 'generic' else bote_env
    return env.get_template(request.param)


# -------------------------------------------------------------------- #
# page


def test_blocked_page_renders_the_tie_form_and_the_decision(template):
    html = template.render(_context(_blocked_payload()))

    assert 'data-blocker="cut"' in html
    assert 'CUT TEXT' in html
    assert html.count('data-qualification-decide') == 1
    assert 'action="/orga_qualification_decide"' in html
    assert 'name="scope" value="group:0"' in html
    assert html.count('<select name="order"') == 2
    assert re.search(r'<textarea[^>]* name="reason"[^>]* required', html)
    assert 'data-decision="group:1"' in html
    assert 'Tiebreak at the table.' in html
    assert 'Ohrwurm' in html
    assert 'data-harmless="group:2"' in html
    assert 'HARMLESS TEXT' in html
    assert 'lt-qual-cut' in html
    assert 'data-lt-qual' in html


def test_decision_below_the_top_place_is_numbered_from_its_place(template):
    tie = _tie(
        'group:1', ('d', 'c'), domain.TieKind.CUT, decided=True, rank_from=3
    )
    state = _state(rankings=[_group('group:1', 'abcd', tie=tie)])
    payload = _payload(state, decisions=_decision())

    html = template.render(_context(payload))

    assert re.search(r'3\. D,\s+4\. C', html)
    assert '1. D' not in html


def test_reason_textareas_do_not_take_the_core_form_control_height(template):
    html = template.render(_context(_blocked_payload()))

    classes = re.findall(r'<textarea class="([^"]*)"', html)
    assert len(classes) == 2
    assert all('form-control' not in c.split() for c in classes)


def _status_tournament(name):
    return _page_tournament(name)


def test_completed_tournament_offers_no_withdraw(template):
    completed = _status_tournament('COMPLETED')

    html = template.render(_context(_blocked_payload(), tournament=completed))

    assert 'value="withdraw"' not in html
    assert 'The tournament is completed. Take back a result first.' in html


def test_cancelled_tournament_offers_no_decision_forms(template):
    cancelled = _status_tournament('CANCELLED')

    html = template.render(_context(_blocked_payload(), tournament=cancelled))

    assert 'value="save"' not in html
    assert 'value="withdraw"' not in html
    assert 'name="order"' not in html
    sentence = (
        'The tournament is cancelled. Tie decisions can no longer change.'
    )
    assert html.count(sentence) == 2


def test_a_running_tournament_still_offers_both_forms(template):
    html = template.render(_context(_blocked_payload()))

    assert 'value="save"' in html
    assert 'value="withdraw"' in html


def test_release_of_a_terminal_tournament_says_why_it_stays(template):
    cancelled = _status_tournament('CANCELLED')
    state = _state(
        rankings=[_group('group:0', 'ab'), _group('group:1', 'cd')],
        ready=True,
        qualifiers=(
            domain.Qualifier(
                contestant_id='a', scope='group:0', rank=1, row=None
            ),
        ),
        released=True,
    )
    payload = _payload(
        state,
        tournament=_tournament(tournament_status=cancelled.tournament_status),
    )

    html = template.render(_context(payload, tournament=cancelled))

    assert '/orga_qualification_unrelease' not in html
    assert (
        'The release can only be taken back while the tournament is ongoing'
        ' or paused.'
    ) in html


def test_withdraw_form_asks_for_a_reason(template):
    html = template.render(_context(_blocked_payload()))

    form = html[html.index('name="action" value="withdraw"') :]
    assert 'name="reason"' in form.split('</form>')[0]
    assert 'required' in form.split('</form>')[0]


def test_ready_page_offers_the_release_with_the_version(template):
    html = template.render(
        _context(
            _ready_payload(),
            playoff_version=4,
            playoff_release_open=True,
        )
    )

    assert 'action="/orga_qualification_release"' in html
    assert 'name="version" value="4"' in html
    assert 'href="/orga_seeding?target=playoff"' in html
    assert 'data-confirm-title="Release the playoffs?"' in html


def _short_payload(game_format, **kwargs):
    state = dataclasses.replace(
        _state(
            rankings=[_group('group:0', 'ab'), _group('group:1', 'cd')],
            ready=True,
            qualifiers=(
                domain.Qualifier(
                    contestant_id='a', scope='group:0', rank=1, row=None
                ),
                domain.Qualifier(
                    contestant_id='c', scope='group:1', rank=1, row=None
                ),
                domain.Qualifier(
                    contestant_id='e', scope='group:2', rank=1, row=None
                ),
            ),
            **kwargs,
        ),
        configured_qualifier_count=4,
        de_fallback=game_format is GameFormat.ONE_V_ONE,
    )
    return _payload(
        state, tournament=_tournament(playoff_game_format=game_format)
    )


def test_release_panel_shows_the_shortfall_and_the_fallback(template):
    html = template.render(
        _context(
            _short_payload(GameFormat.ONE_V_ONE),
            playoff_version=4,
            playoff_release_open=True,
        )
    )

    assert html.count('lt-qual-shortfall') == 2
    assert (
        'Only 3 of 4 playoff places are filled. The playoffs start smaller;'
        ' the top seeds get the byes.'
    ) in html
    assert 'Double elimination needs at least 4 qualifiers. With 3,' in html
    form = re.search(r'<form[^>]*qualification_release[^>]*>', html, re.S)
    body = html_parser.unescape(
        re.search(r'data-confirm-body="([^"]*)"', form.group(0)).group(1)
    )
    lines = [line for line in body.split('\n') if line.strip()]
    assert len(lines) == 5
    assert lines[3].startswith('Only 3 of 4 playoff places')
    assert lines[4].startswith('Double elimination needs')


def test_ffa_release_panel_names_the_smaller_lobbies(template):
    html = template.render(
        _context(
            _short_payload(GameFormat.FREE_FOR_ALL),
            playoff_version=4,
            playoff_release_open=True,
        )
    )

    assert 'The first playoff round may have smaller lobbies' in html
    assert 'Double elimination needs' not in html


def test_released_page_keeps_the_shortfall_notice(template):
    html = template.render(
        _context(_short_payload(GameFormat.FREE_FOR_ALL, released=True))
    )

    assert 'Only 3 of 4 playoff places are filled.' in html
    assert 'data-release-notice' in html


def test_a_full_field_shows_no_release_notice(template):
    html = template.render(
        _context(_ready_payload(), playoff_version=4, playoff_release_open=True)
    )

    assert 'lt-qual-shortfall' not in html
    assert 'data-release-notice' not in html


def test_release_confirmation_lists_what_happens(template):
    html = template.render(
        _context(
            _ready_payload(),
            playoff_version=4,
            playoff_release_open=True,
        )
    )

    form = re.search(r'<form[^>]*qualification_release[^>]*>', html, re.S)
    body = html_parser.unescape(
        re.search(r'data-confirm-body="([^"]*)"', form.group(0)).group(1)
    )
    assert len([line for line in body.split('\n') if line.strip()]) == 3
    assert 'onsubmit' not in form.group(0)
    assert 'data-lt-playoff-version' in html


def test_ready_page_without_a_stored_draft_offers_to_create_it(template):

    html = template.render(
        _context(_ready_payload(), playoff_release_open=True)
    )

    assert 'name="version"' not in html
    assert 'action="/orga_qualification_draft_create"' in html
    assert 'data-lt-draft-create' in html
    assert 'Create playoff draft' in html


def test_unready_page_offers_no_draft_creation(template):
    html = template.render(_context(_blocked_payload()))

    assert 'data-lt-draft-create' not in html


def test_unready_page_says_what_is_missing(template):
    html = template.render(_context(_blocked_payload()))

    assert 'Needs all group results final and no blocking tie.' in html
    assert 'name="version"' not in html


def test_released_page_offers_the_unrelease_with_a_reason(template):
    html = template.render(
        _context(_ready_payload(released=True, can_unrelease=True))
    )
    banner = _banner(html)

    assert 'action="/orga_qualification_unrelease"' in banner
    assert re.search(r'<textarea[^>]* name="reason"[^>]* required', banner)
    assert 'data-confirm-title="Undo the release?"' in banner
    assert 'action="/orga_qualification_release"' not in html


def test_released_page_after_a_playoff_result_is_final(template):
    html = template.render(
        _context(_ready_payload(released=True, can_unrelease=False))
    )

    assert 'A playoff result is confirmed.' in html
    assert '/orga_qualification_unrelease' not in html


def test_automatic_release_shows_the_suspension(template):
    html = template.render(
        _context(
            _ready_payload(mode=PlayoffReleaseMode.AUTOMATIC, suspended=True),
            playoff_version=2,
            playoff_release_open=True,
        )
    )

    assert 'Automatic release suspended.' in html
    assert 'name="version" value="2"' in html


def test_leaderboard_page_offers_the_close_before_any_release(template):
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=(
            _entry('a', 1, value=50),
            _entry('b', 2, value=40),
            _entry('c', 3, value=30),
        ),
        ties=(),
        open_matches=0,
    )
    state = _state(rankings=[ranking], source='leaderboard')

    html = template.render(_context(_payload(state)))

    assert 'action="/orga_leaderboard_close"' in html
    assert 'Close qualification' in html
    assert 'action="/orga_qualification_release"' not in html
    assert 'name="version"' not in html


def test_winner_page_has_no_release_panel(template):
    ranking = domain.Ranking(
        scope='winner',
        entries=(_entry('a', 1), _entry('b', 2)),
        ties=(),
        open_matches=0,
    )
    state = _state(rankings=[ranking], source='winner')

    html = template.render(_context(_payload(state)))

    assert 'data-release-panel' not in html
    assert 'data-ranking="winner"' in html


def test_audit_rows_are_listed(template):
    rows = [
        SimpleNamespace(
            event_type='qualification-tie-decided',
            occurred_at=WHEN,
            who='Ohrwurm',
            label='Tie decided',
            details='Group B',
        ),
        SimpleNamespace(
            event_type='playoffs-released',
            occurred_at=WHEN,
            who=None,
            label='Released',
            details='',
        ),
    ]

    html = template.render(_context(_blocked_payload(), audit_rows=rows))

    assert 'data-event="qualification-tie-decided"' in html
    assert 'data-event="playoffs-released"' in html
    assert 'System' in html


def _hostile_audit_rows():
    entries = [
        SimpleNamespace(
            occurred_at=WHEN,
            event_type='playoffs-unreleased',
            initiator_id='u0',
            data={'reason': EVIL_REASON, 'auto_release_suspended': False},
        ),
        SimpleNamespace(
            occurred_at=WHEN,
            event_type='qualification-tie-withdrawn',
            initiator_id='u0',
            data={
                'scope': 'group:0',
                'contestant_ids': ['a', 'b'],
                'reason': EVIL_REASON,
                'decision_reason': EVIL_REASON,
            },
        ),
    ]
    return helpers.seeding_audit_rows(
        entries, {'u0': SimpleNamespace(screen_name='Ohrwurm')}, names=NAMES
    )


def _assert_reason_escaped(html):
    assert '<script>alert(1)</script>' not in html
    assert html.count('&lt;script&gt;alert(1)&lt;/script&gt;') >= 3


def test_audit_trail_shows_the_reasons_escaped(template, monkeypatch):
    monkeypatch.setattr(helpers, 'gettext', _translate)
    monkeypatch.setattr(helpers, 'ngettext', _ntranslate)

    html = template.render(
        _context(_blocked_payload(), audit_rows=_hostile_audit_rows())
    )

    _assert_reason_escaped(html)


def test_generic_page_loads_the_seeding_assets(generic_env):
    html = generic_env.get_template('generic').render(
        _context(_blocked_payload())
    )

    assert 'data-lt-order-root' in html
    assert 'data-lt-strings' in html
    assert 'behavior/lan_tournament_seeding.js' in html


def test_site_orga_pages_render_the_subnav(generic_env):
    html = generic_env.get_template('generic').render(
        _context(_blocked_payload())
    )

    assert html.index('class="breadcrumbs"') < html.index('class="subnav"')
    assert '<div class="subnav" data-active="">t0</div>' in html
    head = html[html.index('<header class="head">') :]
    head = head[: head.index('</header>')]
    assert 'Orga actions · Kaffeefahrt' in head
    assert '<h1 class="title">Qualification &amp; release</h1>' in head


# -------------------------------------------------------------------- #
# bote look


def test_bote_page_is_the_slip_with_its_style(bote_env):
    html = bote_env.get_template('bote').render(_context(_blocked_payload()))

    assert 'bote-page tournament-page qualification-page' in html
    assert '.tournament-page.qualification-page' in html
    assert 'lt-seed-site' in html


def test_bote_style_is_plain_css():
    text = _BOTE_STYLE.read_text()

    assert '{%' not in text
    assert '{{' not in text
    assert '{#' not in text
    assert 'seeding-page' not in text


# -------------------------------------------------------------------- #
# orga action links


@pytest.fixture(scope='module')
def actions_env():
    return _env(
        {
            'macros/icons.html': (
                '{% macro render_icon(name) %}{% endmacro %}'
            ),
            'macros/lan_tournament.html': (
                '{% macro render_match_ref(m) %}{% endmacro %}'
            ),
            'orga': _read(_SITE_DIR / 'orga_actions.html'),
        }
    )


def _actions(env, status='ONGOING', **tournament):
    values = {
        'id': 't0',
        'tournament_status': SimpleNamespace(name=status),
        'game_format': SimpleNamespace(name='ONE_V_ONE'),
        'elimination_mode': SimpleNamespace(name='SINGLE_ELIMINATION'),
        'has_playoffs': False,
        'playoff_released_at': None,
    }
    values.update(tournament)
    return env.get_template(
        'orga'
    ).module.render_orga_tournament_status_actions(
        SimpleNamespace(**values), True
    )


def test_actions_link_to_the_qualification_for_a_playoff_tournament(
    actions_env,
):
    out = _actions(actions_env, has_playoffs=True)

    assert 'href="/orga_qualification"' in out


def test_actions_omit_the_qualification_without_playoffs(actions_env):
    assert 'orga_qualification' not in _actions(actions_env)


def test_actions_offer_the_ffa_advance_while_ongoing(actions_env):
    out = _actions(
        actions_env, game_format=SimpleNamespace(name='FREE_FOR_ALL')
    )

    assert 'action="/orga_advance_ffa_round"' in out
    assert 'name="pool"' not in out


def test_actions_offer_both_pools_for_a_double_elimination_ffa(actions_env):
    out = _actions(
        actions_env,
        game_format=SimpleNamespace(name='FREE_FOR_ALL'),
        elimination_mode=SimpleNamespace(name='DOUBLE_ELIMINATION'),
    )

    assert 'name="pool" value="WB"' in out
    assert 'name="pool" value="LB"' in out


def test_actions_offer_no_advance_for_a_one_v_one_tournament(actions_env):
    assert 'advance_ffa_round' not in _actions(actions_env)


def test_actions_offer_no_advance_before_the_playoffs_are_released(
    actions_env,
):
    out = _actions(
        actions_env,
        game_format=SimpleNamespace(name='HIGHSCORE'),
        has_playoffs=True,
        playoff_game_format=SimpleNamespace(name='FREE_FOR_ALL'),
        playoff_elimination_mode=SimpleNamespace(name='SINGLE_ELIMINATION'),
    )

    assert 'advance_ffa_round' not in out


def test_actions_offer_the_advance_for_released_ffa_playoffs(actions_env):
    out = _actions(
        actions_env,
        game_format=SimpleNamespace(name='HIGHSCORE'),
        has_playoffs=True,
        playoff_released_at=WHEN,
        playoff_game_format=SimpleNamespace(name='FREE_FOR_ALL'),
        playoff_elimination_mode=SimpleNamespace(name='SINGLE_ELIMINATION'),
    )

    assert 'action="/orga_advance_ffa_round"' in out


def test_actions_offer_no_advance_while_not_ongoing(actions_env):
    out = _actions(
        actions_env,
        status='PAUSED',
        game_format=SimpleNamespace(name='FREE_FOR_ALL'),
    )

    assert 'advance_ffa_round' not in out


# -------------------------------------------------------------------- #
# embedded playoff draft

_SCOPE = {'a': 'group:0', 'c': 'group:0', 'b': 'group:1', 'd': 'group:1'}
_RANK = {'a': 1, 'b': 1, 'c': 2, 'd': 2}


def _playoff_board(
    swaps=(), *, generation=GenerationStatus.NOT_GENERATED, scope=None
):
    scope = scope or _SCOPE
    ids = ('a', 'b', 'c', 'd')
    state = seeding_domain.initial_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        ids,
        tier_count=1,
        draw_seed=0x5EED0012,
        seed_list=ids,
    )
    for p, q in swaps:
        state = seeding_domain.swap_slots(state, p, q)
    return SeedingBoard(
        tournament_id='t0',
        target='playoff',
        state=state,
        version=4,
        code='S1A8-EZAJ-YXM0-155F-CQ9A',
        stale=False,
        labels={cid: cid.upper() for cid in ids},
        stale_leavers=(),
        stale_joiners=(),
        stale_leaver_ids=(),
        stale_joiner_ids=(),
        new_entrant_ids=(),
        problems=(),
        problem_params=(),
        balance=seeding_domain.balance(state),
        fix_count=seeding_domain.fix_count(state),
        pure_draw=seeding_domain.is_pure_draw(state),
        generated_code=None,
        generation=generation,
        locked_reason=None,
        origin_labels={
            cid: f'{"ABC"[int(scope[cid][-1])]}{_RANK[cid]}' for cid in ids
        },
        same_group_matches=tuple(
            domain.same_group_matches(state.layout, scope)
        ),
    )


@pytest.fixture(autouse=True)
def plain_translations(monkeypatch):
    monkeypatch.setattr(helpers, 'gettext', _translate)
    monkeypatch.setattr(helpers, 'ngettext', _ntranslate)


def _embedded(template, board):
    return template.render(
        _context(
            _ready_payload(),
            playoff_version=board.version,
            playoff_release_open=True,
            playoff_board=helpers.seeding_board_payload(board),
        )
    )


def test_ready_page_embeds_the_draft_board(template):
    html = _embedded(template, _playoff_board())

    assert html.count('data-lt-seed-root') == 1
    assert 'data-lt-playoff-draft' in html
    assert 'data-action-url="/orga_qualification_draft_action"' in html
    assert 'data-page-url="/orga_qualification"' in html
    assert 'name="version" value="4" data-lt-playoff-version' in html
    assert html.count('lt-seed-auditp') == 0
    chips = dict(
        re.findall(
            r'<span class="lt-seed-org">([AB][12])</span>'
            r'<span class="lt-seed-seed">#([1-4])</span>',
            html,
        )
    )
    assert chips == {'A1': '1', 'B1': '2', 'A2': '3', 'B2': '4'}


def test_same_group_pairing_is_flagged_with_the_separate_button(template):
    html = _embedded(template, _playoff_board(swaps=[(1, 3)]))

    assert html.count('lt-seed-match is-same') == 2
    assert 'Both from group A: A (A1) and C (A2).' in html
    assert 'name="action" value="separate"' in html
    assert 'Separate same-group pairings' in html


def test_clean_draft_has_no_separate_button(template):
    html = _embedded(template, _playoff_board())

    assert 'is-same' not in html
    assert 'name="action" value="separate"' not in html


def test_page_without_a_draft_has_no_board(template):
    html = template.render(_context(_ready_payload()))

    assert 'data-lt-seed-root' not in html


def test_site_ordering_board_carries_every_script_hook(template):
    html = template.render(_context(_blocked_payload()))

    form = re.search(
        r'<form[^>]*data-qualification-decide[^>]*>.*?</form>', html, re.S
    ).group(0)
    for hook in (
        'data-lt-order',
        'data-lt-reason-form',
        'data-lt-order-fallback',
        'data-lt-qual-order',
        'data-lt-order-list',
        'data-first-place="1"',
        'data-order-place',
        'data-order-move="up"',
        'data-order-move="down"',
        'data-order-value',
        'data-lt-reason',
        'data-lt-reason-submit',
        'data-lt-reason-why',
    ):
        assert hook in form, hook
    assert 'data-lt-order-root' in html
    assert 'data-lt-qual' in html
    assert 'data-lt-strings' in html


def test_withdraw_is_revealed_by_the_script(template):
    html = template.render(_context(_blocked_payload()))

    assert 'data-lt-reveal="withdraw-group-1-1"' in html
    assert 'id="withdraw-group-1-1"' in html
    assert 'data-lt-reveal-target' in html
    assert 'data-lt-hide="withdraw-group-1-1"' in html


def test_leaderboard_shows_when_each_value_was_submitted(template):
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=(_entry('a', 1, value=50), _entry('b', 2, value=40)),
        ties=(),
        open_matches=0,
    )
    payload = helpers.serialize_qualification(
        _state(rankings=[ranking], source='leaderboard'),
        NAMES,
        STRINGS,
        submitted_at={'a': datetime(2026, 9, 30, 20, 12, tzinfo=UTC)},
    )

    html = template.render(_context(payload))

    assert '>Time submitted</th>' in html
    assert '>20:12</time>' in html
    assert html.count('<time ') == 1


def test_close_qualification_asks_for_a_confirmation(template):
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=(_entry('a', 1, value=50), _entry('b', 2, value=40)),
        ties=(),
        open_matches=0,
    )
    html = template.render(
        _context(_payload(_state(rankings=[ranking], source='leaderboard')))
    )

    form = re.search(r'<form[^>]*orga_leaderboard_close[^>]*>', html, re.S)
    assert 'data-confirm-title="Close the qualification?"' in form.group(0)
    assert 'data-confirm-ok="Close qualification"' in form.group(0)


# -------------------------------------------------------------------- #
# lock notice in the orga match actions


def _match_actions(env, *, released, phase, phase_lock=None, walkover=False):
    macro = env.get_template('orga').module.render_orga_match_actions
    return macro(
        match=SimpleNamespace(id='m0', confirmed_by='u0', phase=phase),
        tournament=SimpleNamespace(
            id='t0',
            has_playoffs=True,
            playoff_released_at=WHEN if released else None,
        ),
        contestants=[],
        teams_by_id={},
        participants_by_id={},
        may_administrate=True,
        max_match_score=99,
        correction_case=None,
        affected_downstream_matches=[],
        ack_match_ids=[],
        is_ffa=False,
        is_walkover=walkover,
        ffa_result_consumed=False,
        results_editable=True,
        phase_lock=phase_lock,
    )


def test_released_group_match_shows_the_first_lock_wording(actions_env):
    html = _match_actions(actions_env, released=True, phase=1)

    assert 'id="phase-lock"' in html
    assert 'Take the release back first.' in html
    assert 'locked for good' not in html
    assert 'action="/orga_correct_match_result"' not in html
    assert 'href="/orga_qualification?_anchor=unrelease"' in html


def test_orga_match_lock_notice_links_to_unrelease(actions_env):
    html = _match_actions(actions_env, released=True, phase=1)

    notice = re.search(r'<div class="lt-seed-nt nt-info" id="phase-lock".*?'
                       r'</div>\s*</div>', html, re.S).group(0)
    assert '<i aria-hidden="true">i</i>' in notice
    assert re.search(
        r'<b><a href="/orga_qualification\?_anchor=unrelease">', notice
    )
    assert 'used to be.' in notice

    final = _match_actions(
        actions_env, released=True, phase=1, phase_lock='final'
    )
    assert '_anchor=unrelease' not in final
    assert 'href="/orga_qualification"' in final


def test_group_match_after_a_playoff_result_shows_the_final_wording(
    actions_env,
):
    html = _match_actions(
        actions_env, released=True, phase=1, phase_lock='final'
    )

    assert 'The playoffs are running. Group results are locked for good.' in (
        html
    )
    assert 'the release can no longer be undone' in html
    assert 'Take the release back first.' not in html
    assert 'action="/orga_correct_match_result"' not in html


@pytest.mark.parametrize(
    ('released', 'phase', 'walkover'),
    [(False, 1, False), (True, 2, False), (True, 1, True)],
)
def test_other_matches_keep_their_correction_form(
    actions_env, released, phase, walkover
):
    html = _match_actions(
        actions_env, released=released, phase=phase, walkover=walkover
    )

    assert 'phase-lock' not in html
    assert ('walkover' in html) == walkover
    assert ('orga_correct_match_result' in html) == (not walkover)


def test_two_decided_ties_in_one_scope_are_numbered_by_their_own_places(
    template,
):
    seeding = _tie(
        'leaderboard', ('b', 'a'), domain.TieKind.SEEDING, decided=True,
        rank_from=2,
    )  # fmt: skip
    cut = _tie(
        'leaderboard', ('d', 'c'), domain.TieKind.CUT, decided=True,
        rank_from=4,
    )  # fmt: skip
    entries = tuple(
        _entry(cid, rank=rank, value=100 - rank)
        for cid, rank in (('e', 1), ('b', 2), ('a', 3), ('d', 4), ('c', 5))
    )
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=entries,
        ties=(seeding, cut),
        open_matches=0,
    )
    decisions = {
        'leaderboard': QualificationDecision(
            id='d0',
            tournament_id='t0',
            scope='leaderboard',
            blocks=(_block(('b', 'a')), _block(('d', 'c'))),
            reason='Tiebreak at the table.',
            decided_by='u0',
            decided_at=WHEN,
        )
    }
    payload = _payload(_state(rankings=[ranking]), decisions=decisions)

    html = template.render(_context(payload))

    first, second = _decision_lines(html)
    assert re.fullmatch(r'Leaderboard: 2\. B, 3\. A\.', first), first
    assert re.fullmatch(r'Leaderboard: 4\. D, 5\. C\.', second), second


def _decision_lines(html):
    return [
        ' '.join(found.group(1).split())
        for found in re.finditer(
            r'data-decision="leaderboard".*?<p>(.*?)</p>', html, re.S
        )
    ]


def test_outdated_decision_renders_the_notice_and_the_withdraw_ids(template):
    ranking = dataclasses.replace(
        _group('group:1', 'abcd'), outdated=(('d', 'c'),)
    )
    payload = _payload(_state(rankings=[ranking]), decisions=_decision())

    html = template.render(_context(payload))

    (row,) = payload['decisions']
    assert row['status'] == 'outdated'
    assert row['withdraw_ids'] == ['d', 'c']
    assert [c['place'] for c in row['contestants']] == [None, None]
    assert 'Decision outdated' in html
    assert 'Orga decision on file' not in html
    assert 'data-decision-status="outdated"' in html
    assert 'This decision no longer matches a tie.' in html
    withdraw = html[html.index('name="action" value="withdraw"') :]
    assert re.findall(r'name="order" value="(\w)"', withdraw) == ['d', 'c']


# -------------------------------------------------------------------- #
# released and closed banners


def _hs_payload(**kwargs):
    closed = kwargs.pop('closed', True)
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=tuple(
            _entry(cid, i + 1, value=50 - i) for i, cid in enumerate('abc')
        ),
        ties=(),
        open_matches=0,
    )
    return _payload(
        _state(source='leaderboard', rankings=[ranking], **kwargs),
        tournament=_tournament(leaderboard_closed_at=WHEN if closed else None),
    )


def test_highscore_release_card_says_leaderboard_locked(template):
    html = template.render(_context(_hs_payload(released=True)))

    assert (
        'The playoff matches are generated. The leaderboard is locked.'
        in _banner(html)
    )
    assert 'Group results are locked.' not in html


def test_groups_release_card_says_group_results_locked(template):
    banner = _banner(template.render(_context(_ready_payload(released=True))))

    assert 'The playoff matches are generated. Group results are locked.' in (
        banner
    )
    assert 'leaderboard is locked' not in banner


def test_released_banner_shows_the_actual_mode(template):
    by_hand = template.render(
        _context(
            _ready_payload(
                released=True,
                released_by='u0',
                mode=PlayoffReleaseMode.AUTOMATIC,
            )
        )
    )
    by_system = template.render(
        _context(_ready_payload(released=True, mode=PlayoffReleaseMode.MANUAL))
    )

    assert 'Released (manual) by Ohrwurm · 30.09. 16:42 ·' in _banner(by_hand)
    assert 'Released (automatic) by System · 30.09. 16:42 ·' in _banner(
        by_system
    )


def test_released_banner_names_the_time_of_day_for_today(template):
    state = dataclasses.replace(
        _state(
            rankings=[_group('group:0', 'ab')],
            ready=True,
            released=True,
            released_by='u0',
        ),
        released_at=datetime.now(UTC).replace(hour=9, minute=5),
    )

    banner = _banner(template.render(_context(_payload(state))))

    assert 'Released (manual) by Ohrwurm · 09:05 ·' in banner


_UNRELATED_ROWS = [
    {
        'occurred_at': WHEN,
        'event_type': 'bracket-generated',
        'who': None,
        'label': 'BRACKET LABEL',
        'details': 'BRACKET DETAILS',
        'children': [],
    },
    {
        'occurred_at': WHEN,
        'event_type': 'qualification-tie-decided',
        'who': 'Ohrwurm',
        'label': 'TIE LABEL',
        'details': 'TIE DETAILS',
        'children': [],
    },
]


def test_automatic_release_banner_uses_the_generic_trigger(template):
    html = template.render(
        _context(
            _ready_payload(released=True, mode=PlayoffReleaseMode.AUTOMATIC),
            audit_rows=_UNRELATED_ROWS,
        )
    )
    banner = _banner(html)

    assert 'Trigger: automatic, as soon as nothing blocked any more.' in banner
    for leaked in ('BRACKET', 'TIE LABEL', 'TIE DETAILS', 'decided'):
        assert leaked not in banner
    assert 'Trigger:' not in _banner(
        template.render(
            _context(_ready_payload(released=True, released_by='u0'))
        )
    )


def test_released_banner_names_the_seed_code_the_lock_and_the_players(template):
    board = dataclasses.replace(
        _playoff_board(generation=GenerationStatus.LOCKED),
        generated_code='S1A8EZAJYXM0155FCQ9A',
    )
    html = template.render(
        _context(
            _ready_payload(released=True, released_by='u0'),
            playoff_board=helpers.seeding_board_payload(board),
        )
    )

    assert (
        'Playoff seeding code S1A8-EZAJ-… · Group results locked'
        ' · Players notified'
    ) in _banner(html)


def test_released_banner_without_a_code_skips_it(template):
    html = template.render(
        _context(_ready_payload(released=True, released_by='u0'))
    )

    assert 'Group results locked · Players notified' in _banner(html)
    assert 'Playoff seeding code' not in html


def test_unrelease_form_sits_in_the_released_banner(template):
    html = template.render(
        _context(_ready_payload(released=True, can_unrelease=True))
    )

    assert 'action="/orga_qualification_unrelease"' in _banner(html)
    assert 'data-lt-reveal="unrelease"' in _banner(html)
    assert '/orga_qualification_unrelease' not in _panel(html)
    assert 'Open playoff seeding' not in _banner(html)


def test_blocked_unrelease_is_explained_in_the_banner_not_the_panel(template):
    html = template.render(_context(_ready_payload(released=True)))

    assert 'A playoff result is confirmed.' in _banner(html)
    assert 'A playoff result is confirmed.' not in _panel(html)


_CLOSED_ROW = {
    'occurred_at': WHEN,
    'event_type': 'qualification-leaderboard-closed',
    'who': 'Ohrwurm',
    'label': 'CLOSED LABEL',
    'details': '',
    'children': [],
}


def _closed_banner(html):
    start = html.index('class="lt-seed-nt nt-info lt-qual-closed"')
    return html[start : html.index('<section', start)]


def test_closed_leaderboard_banner_names_who_and_when(template):
    html = template.render(
        _context(_hs_payload(), audit_rows=[_UNRELATED_ROWS[1], _CLOSED_ROW])
    )

    banner = _closed_banner(html)
    assert 'Qualification closed by Ohrwurm · 30.09. 16:42.' in banner
    assert 'New submissions are locked.' in banner
    assert 'Qualification closed: 30.09. 16:42' in _panel(html)


def test_closed_leaderboard_banner_shows_the_time_of_day_for_today(template):
    today = dict(
        _CLOSED_ROW, occurred_at=datetime.now(UTC).replace(hour=21, minute=30)
    )
    html = template.render(_context(_hs_payload(), audit_rows=[today]))

    assert 'Qualification closed by Ohrwurm · 21:30.' in _closed_banner(html)


def test_closed_leaderboard_banner_waits_for_the_closing_row(template):
    html = template.render(_context(_hs_payload()))

    assert 'class="lt-seed-nt nt-info lt-qual-closed"' in html
    assert 'Qualification closed by' not in html


@pytest.mark.parametrize(
    'make_payload',
    [
        lambda: _hs_payload(closed=False),
        lambda: _hs_payload(released=True),
        _ready_payload,
    ],
)
def test_no_closed_banner_when_open_released_or_groups(template, make_payload):
    html = template.render(_context(make_payload(), audit_rows=[_CLOSED_ROW]))

    assert 'class="lt-seed-nt nt-info lt-qual-closed"' not in html


# -------------------------------------------------------------------- #
# header and tables


def _head(html):
    start = html.index('lt-seed-tgt')
    return html[start : html.index('lt-seed-tags', start)]


def test_site_shows_the_phases_block(template):
    html = template.render(_context(_ready_payload()))

    head = _head(html)
    assert '>Phases<' in head
    assert 'Phase 1: Group stage · Phase 2: Playoffs (Single knockout)' in head
    assert 'Qualified: the best 1 of each group' in head
    assert '· Release mode: Manual' in head
    assert 'Kaffeefahrt' not in head


def test_site_phases_block_names_the_leaderboard_and_the_lobbies(template):
    tournament = _page_tournament(
        playoff_game_format=SimpleNamespace(uses_placements=True)
    )

    html = template.render(
        _context(
            _hs_payload(mode=PlayoffReleaseMode.AUTOMATIC),
            tournament=tournament,
        )
    )

    head = _head(html)
    assert (
        'Phase 1: Highscore · Phase 2: Playoffs (Free for all, lobbies)' in head
    )
    assert 'Qualified: the top 2 of the leaderboard' in head
    assert '· Release mode: Automatic' in head


def test_group_header_names_a_blocking_tie(template):
    html = template.render(_context(_blocked_payload()))

    tied = _section(html, 'data-ranking="group:0"')
    other = _section(html, 'data-ranking="group:2"')
    assert 'Tie · decision needed' in tied
    assert 'Tie · decision needed' not in other


def _tied_payload(*, decided=False):
    tie = _tie('group:0', ('a', 'b'), domain.TieKind.CUT, decided=decided)
    return _payload(
        _state(
            rankings=[_group('group:0', 'abef', tie=tie)],
            blockers=[] if decided else [tie],
        )
    )


def _section(html, marker):
    start = html.index(marker)
    return html[start : html.index('</section>', start)]


def test_cut_line_runs_through_a_blocking_tie(template):
    html = template.render(_context(_tied_payload()))
    table = _section(html, 'data-ranking="group:0"')

    cut = re.search(r'<tr class="lt-qual-cut"', table).start()
    assert table.count('lt-qual-cut') == 1
    assert table.index('>A<') < cut < table.index('>B<')


def test_cut_line_sits_below_the_last_row_of_a_clean_cut(template):
    html = template.render(_context(_ready_payload()))
    table = _section(html, 'data-ranking="group:0"')

    assert table.count('lt-qual-cut') == 1
    assert table.index('>A<') < table.index('lt-qual-cut') < table.index('>B<')


def test_cut_line_spans_every_column_of_its_table(template):
    leaderboard = _section(
        template.render(_context(_hs_payload(closed=False))),
        'data-ranking="leaderboard"',
    )
    groups = _section(
        template.render(_context(_ready_payload())), 'data-ranking="group:0"'
    )

    for table in (leaderboard, groups):
        heads = len(re.findall(r'<th scope="col"', table))
        assert f'<td colspan="{heads}">' in table
    assert '<td colspan="6">' in leaderboard
    assert '<td colspan="11">' in groups


def test_diff_has_a_sign(template):
    rows = [
        _entry('a', 1, row=_row('a', 3, 3, 0, 0, 9, 7, 2)),
        _entry('b', 2, row=_row('b', 3, 1, 1, 1, 4, 3, 3)),
        _entry('c', 3, row=_row('c', 3, 0, 0, 3, 0, 1, 6)),
    ]
    ranking = domain.Ranking(
        scope='group:0', entries=tuple(rows), ties=(), open_matches=0
    )
    html = template.render(
        _context(_payload(_state(rankings=[ranking], ready=True)))
    )

    diffs = re.findall(
        r'<td class="c-n">([^<]*)</td>\s*<td class="c-n">\d+</td>\s*<td class="c-pk">',
        html,
    )
    assert diffs == ['+5', '0', '−5']


def test_values_use_numberformat(template):
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=(_entry('a', 1, value=58420), _entry('b', 2, value=999)),
        ties=(),
        open_matches=0,
    )
    html = template.render(
        _context(_payload(_state(rankings=[ranking], source='leaderboard')))
    )

    assert '<td class="c-n">58.420</td>' in html
    assert '<td class="c-n">999</td>' in html


def test_group_headers_are_the_short_forms_with_titles(template):
    html = template.render(_context(_ready_payload()))

    table = _section(html, 'data-ranking="group:0"')
    assert '<th scope="col" class="c-rk">#</th>' in table
    assert '<th scope="col" class="c-nm">Player</th>' in table
    assert 'title="Matches played">Pld.</th>' in table
    assert '>Pts</th>' in table
    assert 'Participant' not in table
    assert '<h3>' in table
    assert 'lt-seed-panel' not in table


def test_group_name_header_follows_the_contestant_type(template):
    teams = _page_tournament(contestant_type=SimpleNamespace(name='TEAM'))

    html = template.render(_context(_ready_payload(), tournament=teams))

    assert '<th scope="col" class="c-nm">Team</th>' in html


def _qualifier(cid, scope, rank):
    return domain.Qualifier(contestant_id=cid, scope=scope, rank=rank, row=None)


def test_qualified_list_has_origin_labels(template):
    state = _state(
        rankings=[_group('group:0', 'ab'), _group('group:1', 'cd')],
        ready=True,
        qualifiers=(
            _qualifier('a', 'group:0', 1),
            _qualifier('b', 'group:0', 2),
            _qualifier('c', 'group:1', 1),
            _qualifier('d', 'group:1', 2),
        ),
    )

    html = template.render(_context(_payload(state)))

    assert 'A1 A, A2 B, B1 C, B2 D.' in re.sub(r'\s+', ' ', _panel(html))


def test_leaderboard_qualified_list_is_numbered_by_rank(template):
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=(_entry('a', 1, value=50), _entry('b', 2, value=40)),
        ties=(),
        open_matches=0,
    )
    state = _state(
        source='leaderboard',
        rankings=[ranking],
        ready=True,
        qualifiers=(
            _qualifier('a', 'leaderboard', 1),
            _qualifier('b', 'leaderboard', 2),
        ),
    )

    html = template.render(_context(_payload(state)))

    assert '1. A, 2. B.' in re.sub(r'\s+', ' ', _panel(html))


def test_release_panel_is_named_release_and_generation(template):
    html = template.render(
        _context(_ready_payload(), playoff_version=3, playoff_release_open=True)
    )
    panel = _panel(html)

    assert '<h3>Release &amp; generation</h3>' in panel
    assert 'Playoff release' not in panel
    assert 'Before the release you can adjust the playoff draft below.' in panel
    assert '<h3>Audit log</h3>' in html


def test_release_checklist_rows_carry_a_state_and_a_glyph(template):
    html = template.render(_context(_blocked_payload()))

    panel = _panel(html)
    rows = re.findall(r'<li><span class="lt-seed-ckt">.*?</li>', panel, re.S)
    states = [re.search(r'lt-seed-ckm (m-\w+)', row).group(1) for row in rows]
    assert states == ['m-ok', 'm-err', 'm-mute']
    assert '<i aria-hidden="true">✕</i>Blocked' in panel
    assert 'Release is manual: the orga checks the playoff draft' in panel


def test_automatic_release_explains_that_no_button_is_needed(template):
    html = template.render(
        _context(_ready_payload(mode=PlayoffReleaseMode.AUTOMATIC))
    )

    panel = _panel(html)
    assert 'Release is automatic: the playoffs are created' in panel
    assert 'No button needed: the system releases the playoffs' in panel


def test_leaderboard_chain_says_the_time_does_not_decide(template):
    ranking = domain.Ranking(
        scope='leaderboard',
        entries=(_entry('a', 1, value=50), _entry('b', 2, value=40)),
        ties=(),
        open_matches=0,
    )

    html = template.render(
        _context(_payload(_state(rankings=[ranking], source='leaderboard')))
    )

    assert 'Order on a tie' in html
    assert 'Value → Orga decision.' in html
    assert 'The time does not decide a tie.' in html


def test_site_playoff_draft_empty_state(template):
    waiting = template.render(_context(_ready_payload()))
    staged = template.render(
        _context(_ready_payload(), playoff_version=4, playoff_release_open=True)
    )
    embedded = _embedded(template, _playoff_board())
    released = template.render(_context(_ready_payload(released=True)))

    assert 'class="lt-seed-panels"' in waiting
    panels = waiting[waiting.index('class="lt-seed-panels"') :]
    assert panels.index('data-release-panel') < panels.index('Playoff draft')
    assert (
        'The draft opens prefilled as soon as the qualifiers are set.'
        in waiting
    )
    assert 'The qualifiers are set. The draft is prefilled crosswise.' in staged
    assert 'href="/orga_seeding?target=playoff"' in staged
    for html in (embedded, released):
        assert 'The draft opens prefilled' not in html
        assert 'The qualifiers are set. The draft is prefilled' not in html


def test_blocking_tie_sentence_has_no_appended_names(template):
    html = template.render(_context(_tied_payload()))

    assert re.search(r'equal on every criterion\.\s*</p>', html)
