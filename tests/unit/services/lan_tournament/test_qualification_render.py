"""
tests.unit.services.lan_tournament.test_qualification_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Renders the admin qualification page and the phase lock notice of the match
page under `StrictUndefined`.
"""

import dataclasses
from datetime import datetime, UTC
import pathlib
import re
from types import SimpleNamespace

from babel.messages.pofile import read_po
from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_match_service,
    tournament_qualification_domain_service as domain,
    tournament_qualification_service as service,
    tournament_seeding_domain_service as seeding_domain,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
)
from byceps.services.lan_tournament.tournament_seeding_service import (
    GenerationStatus,
    SeedingBoard,
)


_ADMIN_DIR = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament'
)
_CATALOGUE = pathlib.Path('byceps/translations/de/LC_MESSAGES/messages.po')

_LAYOUT = (
    '{% block head %}{% endblock %}{% block before_body %}{% endblock %}'
    '{% block body %}{% endblock %}{% block scripts %}{% endblock %}'
)

STRINGS = {
    'tie_cut': 'CUT TEXT',
    'tie_seeding': 'SEEDING TEXT',
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
JS_STRINGS = {
    'cancel': 'Cancel',
    'place': 'Place %(n)s',
    'order_moved': '%(name)s is now in place %(place)s.',
}
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


def _env(templates):
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {'layout/admin/lan_tournament.html': _LAYOUT, **templates}
        ),
    )
    env.globals['_'] = _translate
    env.globals['ngettext'] = _ntranslate
    env.globals['url_for'] = _url_for
    env.filters['dateformat'] = lambda value: value.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda value, kind='short': value.strftime(
        '%H:%M'
    )
    return env


@pytest.fixture(scope='module')
def template():
    env = _env(
        {
            'page': (_ADMIN_DIR / 'qualification.html').read_text(),
            'admin/lan_tournament/_seeding_board.html': (
                _ADMIN_DIR / '_seeding_board.html'
            ).read_text(),
        }
    )
    return env.get_template('page')


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
        released_by=None,
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


def _leaderboard(ids, *, tie=None):
    rows = [
        _entry(
            cid,
            rank=1 if tie and i < 2 else i + 1,
            shared=bool(tie and i < 2),
            value=100 - i,
        )
        for i, cid in enumerate(ids)
    ]
    return domain.Ranking(
        scope='leaderboard',
        entries=tuple(rows),
        ties=(tie,) if tie else (),
        open_matches=0,
    )


def _tournament(**overrides):
    values = {
        'id': 't0',
        'name': 'Kaffeefahrt',
        'party_id': 'p0',
        'contestant_type': SimpleNamespace(name='SOLO'),
        'tournament_status': SimpleNamespace(name='ONGOING'),
        'playoff_qualifiers_per_group': 1,
        'playoff_qualifier_count': 2,
        'leaderboard_closed_at': None,
        'playoff_game_format': None,
        'playoff_elimination_mode': SimpleNamespace(name='SINGLE_ELIMINATION'),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _block(ids, reason='Tiebreak at the table.'):
    return DecisionBlock(
        contestant_ids=ids, reason=reason, decided_by='u0', decided_at=WHEN
    )


def _decision(scope='group:1', reason='Tiebreak at the table.'):
    return {
        scope: QualificationDecision(
            id='d0',
            tournament_id='t0',
            scope=scope,
            blocks=(_block(('d', 'c'), reason),),
            reason=reason,
            decided_by='u0',
            decided_at=WHEN,
        )
    }


def _payload(state, *, decisions=None, tournament=None, strings=None):
    return helpers.serialize_qualification(
        state,
        NAMES,
        strings or STRINGS,
        tournament=tournament or _tournament(),
        decisions=decisions,
        users={'u0': SimpleNamespace(screen_name='Ohrwurm')},
    )


def _context(payload, **extra):
    context = {
        'party': SimpleNamespace(id='p0', title='Party'),
        'tournament': _tournament(),
        'qualification': payload,
        'playoff_version': None,
        'playoff_board': None,
        'audit_rows': [],
        'js_strings': JS_STRINGS,
    }
    context.update(extra)
    return context


def _blocked_payload(**kwargs):
    cut = _tie('group:0', ('a', 'b'), domain.TieKind.CUT)
    harmless = _tie('group:2', ('c', 'd'), domain.TieKind.HARMLESS, rank_from=3)
    decided = _tie('group:1', ('d', 'c'), domain.TieKind.CUT, decided=True)
    state = _state(
        rankings=[
            _group('group:0', 'abef', tie=cut),
            _group('group:1', 'cdgh', tie=decided),
            _group('group:2', 'ghab', tie=harmless),
        ],
        blockers=[cut],
        **kwargs,
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


def _section(html, marker):
    start = html.index(marker)
    return html[start : html.index('</section>', start)]


# -------------------------------------------------------------------- #
# strict render


def test_qualification_renders_strict(template):
    variants = {
        'blocked': _context(_blocked_payload()),
        'ready': _context(_ready_payload(), playoff_version=3),
        'ready without draft': _context(_ready_payload()),
        'released': _context(_ready_payload(released=True, can_unrelease=True)),
        'released for good': _context(_ready_payload(released=True)),
        'automatic': _context(
            _ready_payload(mode=PlayoffReleaseMode.AUTOMATIC)
        ),
        'suspended': _context(
            _ready_payload(mode=PlayoffReleaseMode.AUTOMATIC, suspended=True),
            playoff_version=1,
        ),
        'running': _context(
            _payload(
                _state(
                    rankings=[_group('group:0', 'ab', open_matches=1)],
                    open_match_count=1,
                )
            )
        ),
        'leaderboard': _context(
            _payload(
                _state(
                    source='leaderboard',
                    rankings=[_leaderboard('abcd')],
                ),
                tournament=_tournament(
                    playoff_game_format=SimpleNamespace(uses_placements=True),
                    playoff_elimination_mode=None,
                ),
            )
        ),
        'winner': _context(
            _payload(
                _state(
                    source='winner',
                    rankings=[
                        _leaderboard(
                            'ab',
                            tie=_tie(
                                'winner', ('a', 'b'), domain.TieKind.WINNER
                            ),
                        )
                    ],
                    blockers=[
                        _tie('winner', ('a', 'b'), domain.TieKind.WINNER)
                    ],
                )
            )
        ),
        'audit': _context(
            _ready_payload(),
            audit_rows=[
                {
                    'occurred_at': WHEN,
                    'event_type': 'qualification-tie-decided',
                    'who': None,
                    'label': 'Decided',
                    'details': 'Group A',
                    'children': [],
                }
            ],
        ),
    }

    for name, context in variants.items():
        html = template.render(context)
        assert 'data-lt-order-root' in html, name
        assert 'data-lt-strings' in html, name


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
        _context(_ready_payload(), audit_rows=_hostile_audit_rows())
    )

    _assert_reason_escaped(html)


def test_blocked_page_renders_the_tie_board_and_the_decision(template):
    html = template.render(_context(_blocked_payload()))

    assert 'data-blocker="cut"' in html
    assert 'CUT TEXT' in html
    assert 'name="scope" value="group:0"' in html
    assert 'data-decision="group:1"' in html
    assert 'Tiebreak at the table.' in html
    assert 'Ohrwurm' in html
    assert 'data-harmless="group:2"' in html
    assert 'HARMLESS TEXT' in html
    assert 'lt-seed-cut' in html


def test_tie_board_has_a_no_script_fallback_and_a_board(template):
    html = template.render(_context(_blocked_payload()))
    board = _section(html, 'data-blocker="cut"')

    assert board.count('<select class="lt-seed-inp" name="order"') == 2
    assert 'data-lt-order-fallback' in board
    assert re.search(
        r'<ol [^>]*data-lt-order-list[^>]*data-first-place="1"', board
    )
    assert board.count('data-order-move="up"') == 2
    assert board.count('data-order-move="down"') == 2
    assert board.count('data-order-value') == 2
    assert not re.search(r'<input[^>]*data-order-value[^>]*name=', board)
    assert re.search(r'<textarea[^>]*name="reason"[^>]* required', board)
    assert 'data-lt-reason-submit' in board


def test_places_follow_the_first_rank_of_the_tie(template):
    tie = _tie('group:0', ('a', 'b'), domain.TieKind.CUT, rank_from=2)
    state = _state(
        rankings=[_group('group:0', 'abef', tie=tie)], blockers=[tie]
    )

    html = template.render(_context(_payload(state)))

    assert 'data-first-place="2"' in html
    assert 'Place 2' in html
    assert 'Place 3' in html


def test_withdraw_form_asks_for_a_reason(template):
    html = template.render(_context(_blocked_payload()))
    decision = _section(html, 'data-decision="group:1"')

    assert 'name="action" value="withdraw"' in decision
    assert re.search(r'<textarea[^>]*name="reason"[^>]* required', decision)
    assert 'data-lt-reason-submit' in decision
    assert 'data-lt-reveal="withdraw-group-1-1"' in decision
    assert 'id="withdraw-group-1-1"' in decision


def _status_tournament(name):
    return _tournament(tournament_status=SimpleNamespace(name=name))


def test_completed_tournament_offers_no_withdraw(template):
    completed = _status_tournament('COMPLETED')

    html = template.render(_context(_blocked_payload(), tournament=completed))
    decision = _section(html, 'data-decision="group:1"')

    assert 'value="withdraw"' not in html
    assert 'The tournament is completed. Take back a result first.' in decision


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
    html = template.render(
        _context(_payload(state, tournament=cancelled), tournament=cancelled)
    )
    panel = _section(html, 'data-release-panel')

    assert '/qualification_unrelease' not in panel
    assert (
        'The release can only be taken back while the tournament is ongoing'
        ' or paused.'
    ) in panel


def test_decision_is_locked_after_the_release(template):
    html = template.render(_context(_blocked_payload(released=True)))
    decision = _section(html, 'data-decision="group:1"')

    assert 'name="action" value="withdraw"' not in decision
    assert 'Locked while the playoffs are released.' in decision


def test_decided_places_count_from_the_first_rank_of_the_tie(template):
    html = template.render(_context(_blocked_payload()))
    decision = _section(html, 'data-decision="group:1"')

    assert '1. D' in decision
    assert '2. C' in decision


def test_ready_page_offers_the_release_with_a_confirm(template):
    html = template.render(_context(_ready_payload(), playoff_version=7))
    panel = _section(html, 'data-release-panel')

    assert 'action="/qualification_release"' in panel
    assert 'name="version" value="7"' in panel
    assert 'data-confirm-title="Release the playoffs?"' in panel
    body = re.search(r'data-confirm-body="([^"]*)"', panel).group(1)
    assert len([line for line in body.split('\n') if line.strip()]) == 3
    assert 'data-confirm-ok="Release playoffs"' in panel


def _short_payload(**kwargs):
    """A ready payload with 1 of 4 places filled, as a DE fallback."""
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
        de_fallback=not kwargs.get('released'),
    )
    return _payload(
        state,
        tournament=_tournament(
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        ),
    )


def test_release_panel_and_confirm_show_the_shortfall(template):
    html = template.render(_context(_short_payload(), playoff_version=7))
    panel = _section(html, 'data-release-panel')

    assert panel.count('data-release-notice') == 2
    assert (
        'Only 3 of 4 playoff places are filled. The playoffs start smaller;'
        ' the top seeds get the byes.'
    ) in panel
    assert (
        'Double elimination needs at least 4 qualifiers. With 3, the'
        ' playoffs run as single elimination.'
    ) in panel
    body = re.search(r'data-confirm-body="([^"]*)"', panel).group(1)
    lines = [line for line in body.split('\n') if line.strip()]
    assert len(lines) == 5
    assert lines[3].startswith('Only 3 of 4 playoff places')
    assert lines[4].startswith('Double elimination needs')


def test_released_card_keeps_the_shortfall_notice(template):
    html = template.render(_context(_short_payload(released=True)))

    card = html[html.index('data-released') :]
    assert 'Only 3 of 4 playoff places are filled.' in card
    assert 'Double elimination needs' not in html


def test_shortfall_audit_events_have_a_label_and_details():
    assert (
        helpers.seeding_event_label('playoffs-shortfall')
        == 'Released with fewer qualifiers'
    )
    assert (
        helpers.seeding_event_label('playoffs-de-fallback')
        == 'Single instead of double elimination'
    )
    assert (
        helpers._seeding_event_details(
            'playoffs-shortfall', {'configured': 8, 'qualified': 5}
        )
        == '5 of 8 places filled'
    )
    assert (
        helpers._seeding_event_details(
            'playoffs-de-fallback',
            {'qualified': 3, 'from': 'DOUBLE_ELIMINATION'},
        )
        == '3 qualifiers'
    )


def test_a_full_field_shows_no_release_notice(template):
    html = template.render(_context(_ready_payload(), playoff_version=7))

    assert 'data-release-notice' not in html


def test_release_without_a_draft_offers_to_create_it(template):
    html = template.render(_context(_ready_payload()))
    panel = _section(html, 'data-release-panel')

    assert '/qualification_release' not in panel
    assert 'action="/qualification_draft_create"' in panel
    assert 'data-lt-draft-create' in panel
    assert 'Create playoff draft' in panel
    assert 'The playoff draft is missing.' in panel


def test_release_without_a_draft_of_a_paused_tournament_is_off(template):
    paused = _tournament(tournament_status=SimpleNamespace(name='PAUSED'))
    html = template.render(_context(_ready_payload(), tournament=paused))
    panel = _section(html, 'data-release-panel')

    assert 'data-lt-draft-create' not in panel
    assert re.search(r'<button[^>]*disabled[^>]*>Release playoffs', panel)
    assert 'The playoff draft is missing.' in panel


def test_blocked_release_is_off_with_the_reason(template):
    html = template.render(_context(_blocked_payload()))
    panel = _section(html, 'data-release-panel')

    assert '/qualification_release' not in panel
    assert 'Needs all group results final and no blocking tie.' in panel
    assert 'Blocked' in panel


def test_released_page_takes_the_release_back_with_a_reason(template):
    html = template.render(
        _context(_ready_payload(released=True, can_unrelease=True))
    )
    panel = _section(html, 'data-release-panel')

    assert 'data-released' in html
    assert 'action="/qualification_unrelease"' in panel
    assert re.search(r'<textarea[^>]*name="reason"[^>]* required', panel)
    assert 'data-confirm-danger' in panel
    assert 'data-lt-reveal="unrelease"' in panel


def test_release_that_cannot_be_undone_says_why(template):
    html = template.render(_context(_ready_payload(released=True)))
    panel = _section(html, 'data-release-panel')

    assert '/qualification_unrelease' not in panel
    assert 'A playoff result is confirmed.' in panel
    assert 'Playoffs running' in html


def test_automatic_release_needs_no_button(template):
    html = template.render(
        _context(_ready_payload(mode=PlayoffReleaseMode.AUTOMATIC))
    )
    panel = _section(html, 'data-release-panel')

    assert '/qualification_release' not in panel
    assert 'No button needed' in panel


def test_suspended_automatic_release_can_be_released_by_hand(template):
    html = template.render(
        _context(
            _ready_payload(mode=PlayoffReleaseMode.AUTOMATIC, suspended=True),
            playoff_version=2,
        )
    )
    panel = _section(html, 'data-release-panel')

    assert 'Automatic release suspended.' in panel
    assert 'name="version" value="2"' in panel


def test_running_leaderboard_offers_to_close_the_qualification(template):
    payload = _payload(
        _state(source='leaderboard', rankings=[_leaderboard('abcd')]),
        tournament=_tournament(
            playoff_game_format=SimpleNamespace(uses_placements=True),
            playoff_elimination_mode=None,
        ),
    )

    html = template.render(_context(payload))
    panel = _section(html, 'data-release-panel')

    assert 'action="/leaderboard_close"' in panel
    assert 'Close qualification' in panel
    assert '/qualification_release' not in panel
    assert 'Value → Orga decision.' in html


def test_cut_line_sits_below_the_last_qualifier(template):
    html = template.render(_context(_ready_payload()))
    table = _section(html, 'data-ranking="group:0"')

    assert table.count('lt-seed-cut') == 1
    assert table.index('>A<') < table.index('lt-seed-cut') < table.index('>B<')


# -------------------------------------------------------------------- #
# the PRD sentence


def _catalogue_entry(msgid):
    with _CATALOGUE.open('rb') as handle:
        catalogue = read_po(handle, locale='de')
    return catalogue[msgid].string


def test_cut_tie_blocker_text_verbatim_de(template):
    german = _catalogue_entry(tournament_match_service.QUALIFICATION_TIE_ERROR)
    assert german == (
        'Qualifikation kann aufgrund eines Gleichstands nicht automatisch'
        ' bestimmt werden. Orgaentscheidung erforderlich.'
    )
    strings = {**STRINGS, 'tie_cut': german}
    cut = _tie('group:0', ('a', 'b'), domain.TieKind.CUT)
    state = _state(
        rankings=[_group('group:0', 'abef', tie=cut)], blockers=[cut]
    )

    html = template.render(_context(_payload(state, strings=strings)))

    blocker = _section(html, 'data-blocker="cut"')
    assert german in blocker
    assert blocker.index(german) < blocker.index('data-lt-order')


# -------------------------------------------------------------------- #
# lock notice of the match page


@pytest.fixture(scope='module')
def lock_env():
    src = (_ADMIN_DIR / 'view_match.html').read_text()
    start = src.index('{%- set phase1_locked')
    end = src.index('{%- endif %}{# end of the phase lock #}')
    end += len('{%- endif %}')
    env = _env({'region': src[start:end]})
    env.globals['render_icon'] = lambda *a, **k: ''
    env.globals['g'] = SimpleNamespace(
        user=SimpleNamespace(has_permission=lambda _p: True)
    )
    return env


def _render_lock(
    lock_env, *, released, phase, has_playoffs=True, is_walkover=True
):
    return lock_env.get_template('region').render(
        tournament=SimpleNamespace(
            id='t0',
            has_playoffs=has_playoffs,
            playoff_released_at=WHEN if released else None,
        ),
        match=SimpleNamespace(id='m0', confirmed_by='admin', phase=phase),
        is_walkover=is_walkover,
    )


def test_released_group_match_shows_the_lock_notice(lock_env):
    html = _render_lock(lock_env, released=True, phase=1, is_walkover=False)

    assert "id='phase-lock'" in html
    assert 'Group results are locked.' in html
    assert "href='/qualification'" in html
    assert 'decided by a walkover' not in html


@pytest.mark.parametrize(
    'released, phase, has_playoffs',
    [
        (False, 1, True),
        (True, 2, True),
        (True, None, True),
        (True, 1, False),
    ],
)
def test_other_matches_keep_their_forms(
    lock_env, released, phase, has_playoffs
):
    html = _render_lock(
        lock_env, released=released, phase=phase, has_playoffs=has_playoffs
    )

    assert 'phase-lock' not in html
    assert 'decided by a walkover' in html


def test_a_walkover_keeps_its_own_notice_after_the_release(lock_env):
    html = _render_lock(lock_env, released=True, phase=1, is_walkover=True)

    assert 'phase-lock' not in html
    assert 'decided by a walkover' in html


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
        labels=NAMES,
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


def _embedded(template, board, **kwargs):
    return template.render(
        _context(
            _ready_payload(),
            playoff_version=board.version,
            playoff_board=helpers.seeding_board_payload(board),
            **kwargs,
        )
    )


@pytest.mark.parametrize('stale_structure', [False, True])
def test_board_payload_carries_stale_structure(stale_structure):
    board = dataclasses.replace(
        _playoff_board(), stale_structure=stale_structure
    )

    payload = helpers.seeding_board_payload(board)

    assert payload['stale_structure'] is stale_structure


def test_ready_page_embeds_the_draft_board(template):
    html = _embedded(template, _playoff_board())

    assert html.count('data-lt-seed-root') == 1
    assert 'data-lt-playoff-draft' in html
    assert 'data-action-url="/qualification_draft_action"' in html
    assert 'data-page-url="/qualification"' in html
    assert 'name="version" value="4" data-lt-playoff-version' in html
    assert 'lt-seed-auditp' in html.split('data-lt-playoff-draft')[1]
    # the page's own audit, none from the board
    assert html.count('lt-seed-auditp') == 1


def test_embedded_board_shows_the_origin_and_the_seed(template):
    html = _embedded(template, _playoff_board())

    chips = dict(
        re.findall(
            r'<span class="lt-seed-org">([AB][12])</span>'
            r'<span class="lt-seed-seed">#([1-4])</span>',
            html,
        )
    )
    assert chips == {'A1': '1', 'B1': '2', 'A2': '3', 'B2': '4'}
    assert 'Qualified as A1' in html


def test_clean_draft_has_no_separation_offer(template):
    html = _embedded(template, _playoff_board())

    assert 'is-same' not in html
    assert 'Separate same-group pairings' not in html
    assert 'name="action" value="separate"' not in html


def test_same_group_pairings_are_flagged_with_the_separate_button(template):
    html = _embedded(template, _playoff_board(swaps=[(1, 3)]))

    assert html.count('lt-seed-match is-same') == 2
    assert 'Both from group A: A (A1) and C (A2).' in html
    banner = html[html.index('lt-seed-nt nt-warn') :]
    banner = banner[: banner.index('</form>')]
    assert '2 first-round matches pair players from one group.' in banner
    assert 'M1: A (A1) and C (A2) are both from group A.' in banner
    assert 'M2: B (B1) and D (B2) are both from group B.' in banner
    assert 'The release is still possible.' in banner
    assert 'action="/qualification_draft_action"' in banner
    assert 'name="action" value="separate"' in banner
    assert 'Separate same-group pairings' in banner


def test_a_single_clash_is_named_in_the_banner_title(template):
    scope = {'a': 'group:0', 'd': 'group:0', 'b': 'group:1', 'c': 'group:2'}
    html = _embedded(template, _playoff_board(scope=scope))

    assert html.count('lt-seed-match is-same') == 1
    title = re.search(r'<span>(M1: [^<]*)</span>', html).group(1)
    assert title == 'M1: A (A1) and D (A2) are both from group A.'
    assert 'One pairing' not in html
    assert 'A round 1 pairing from the same group.' in html


def test_separated_draft_is_clean_again(template):
    board = _playoff_board(swaps=[(1, 3), (1, 2)])
    assert board.same_group_matches == ()
    html = _embedded(template, board)

    assert 'is-same' not in html
    assert 'name="action" value="separate"' not in html


def test_released_draft_offers_no_generate_form_and_no_release_promise(
    template,
):
    html = _embedded(template, _playoff_board(swaps=[(1, 3)]))

    assert 'The release generates the playoffs from this draft.' in html
    assert 'Generate bracket from this code' not in html

    generated = _embedded(
        template,
        _playoff_board(swaps=[(1, 3)], generation=GenerationStatus.DIFFERS),
    )
    assert 'The release is still possible.' not in generated
    assert 'Separate same-group pairings' in generated


def test_no_draft_no_embedded_board(template):
    html = template.render(_context(_ready_payload(), playoff_version=None))

    assert 'data-lt-seed-root' not in html
    assert 'data-lt-playoff-draft' not in html


def test_leaderboard_shows_when_each_value_was_submitted(template):
    payload = helpers.serialize_qualification(
        _state(source='leaderboard', rankings=[_leaderboard('abcd')]),
        NAMES,
        STRINGS,
        tournament=_tournament(
            playoff_game_format=SimpleNamespace(uses_placements=True),
            playoff_elimination_mode=None,
        ),
        submitted_at={'a': datetime(2026, 9, 30, 20, 12, tzinfo=UTC)},
    )

    html = template.render(_context(payload))

    assert '>Time submitted</th>' in html
    assert '<time datetime="2026-09-30T20:12:00+00:00"' in html
    assert '>20:12</time>' in html
    assert html.count('<time ') == 1


def test_group_tables_have_no_time_column(template):
    html = template.render(_context(_ready_payload()))

    assert 'Time submitted' not in html
    assert '<time ' not in html


def test_close_qualification_asks_for_a_confirmation(template):
    payload = _payload(
        _state(source='leaderboard', rankings=[_leaderboard('abcd')]),
        tournament=_tournament(
            playoff_game_format=SimpleNamespace(uses_placements=True),
            playoff_elimination_mode=None,
        ),
    )

    panel = _section(template.render(_context(payload)), 'data-release-panel')

    assert 'data-confirm-title="Close the qualification?"' in panel
    body = re.search(r'data-confirm-body="([^"]*)"', panel).group(1)
    assert len([line for line in body.split('\n') if line.strip()]) == 2
    assert 'data-confirm-ok="Close qualification"' in panel


def test_match_lock_notice_has_two_wordings(lock_env):
    def render(can_unrelease):
        return lock_env.get_template('region').render(
            tournament=SimpleNamespace(
                id='t0', has_playoffs=True, playoff_released_at=WHEN
            ),
            match=SimpleNamespace(id='m0', confirmed_by='admin', phase=1),
            is_walkover=False,
            can_unrelease=can_unrelease,
        )

    open_ = render(True)
    final = render(False)

    assert 'Take the release back first.' in open_
    assert 'locked for good' not in open_
    assert 'The playoffs are running. Group results are locked for good.' in (
        final
    )
    assert 'the release can no longer be undone' in final
    assert 'Take the release back first.' not in final
    assert "href='/qualification'" in final


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
