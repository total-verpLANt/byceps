"""
tests.unit.services.lan_tournament.test_ffa_cut_tie_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Renders the FFA lobby cut tie block of the admin and the site under
`StrictUndefined`, and the serializer behind it.
"""

from datetime import datetime, UTC
import html as html_parser
import pathlib
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
    tournament_match_service,
    tournament_qualification_service as service,
)
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
)


_ADMIN_DIR = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament'
)
_SITE_DIR = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament'
)

PRD = 'PRD SENTENCE'
LOCKED = (
    'A later round has already been built from this lobby, so its tie'
    ' decision can no longer be changed.'
)
WHEN = datetime(2026, 9, 30, 16, 42, tzinfo=UTC)
NAMES = {'a': 'Anna', 'b': 'Bernd', 'c': 'Cleo', 'd': 'Dora'}


def _translate(message, **params):
    return message % params if params else message


def _env(partial):
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'partial': partial.read_text()}),
    )
    env.globals['_'] = _translate
    env.globals['url_for'] = lambda endpoint, **values: (
        f'/{endpoint.lstrip(".")}/{values["tournament_id"]}'
    )
    env.filters['dateformat'] = lambda value: value.strftime('%Y-%m-%d')
    env.filters['timeformat'] = lambda value, kind='short': value.strftime(
        '%H:%M'
    )
    return env


@pytest.fixture(scope='module', params=['admin', 'site'])
def surface(request):
    directory = _ADMIN_DIR if request.param == 'admin' else _SITE_DIR
    endpoint = (
        '/qualification_decide/'
        if request.param == 'admin'
        else '/orga_qualification_decide/'
    )
    return SimpleNamespace(
        name=request.param,
        env=_env(directory / '_ffa_cut_ties.html'),
        action=endpoint + 't0',
    )


def _tie(
    scope='ffa:SE:0:0',
    *,
    decided=False,
    locked=False,
    decision=None,
    ids=('a', 'b', 'c'),
    rank_from=3,
    label='Round 1 · Lobby 1',
    status=None,
):
    return {
        'scope': scope,
        'scope_label': label,
        'kind': 'cut',
        'status': status or ('decided' if decided else 'open'),
        'decided': decided,
        'blocking': not decided,
        'locked': locked,
        'places': f'{rank_from}–{rank_from + len(ids) - 1}',
        'rank_from': rank_from,
        'contestants': [
            {'id': cid, 'name': NAMES[cid], 'points': 1} for cid in ids
        ],
        'text': PRD,
        'decision': decision,
        'withdraw_ids': list(ids) if decision is not None else [],
    }


def _decision(reason='Tiebreak at the table.', by='Ohrwurm'):
    return {'reason': reason, 'decided_by': by, 'decided_at': WHEN}


def _render(surface, ties):
    return surface.env.get_template('partial').render(
        ffa_cut_ties=ties,
        tournament=SimpleNamespace(id='t0'),
        js_strings={'cancel': 'Cancel'},
    )


def _text(markup):
    return re.sub(
        r'\s+', ' ', html_parser.unescape(re.sub(r'<[^>]+>', ' ', markup))
    )


def test_no_ties_render_nothing(surface):
    assert _render(surface, []).strip() == ''


def test_blocking_lobby_renders_the_sentence_the_board_and_the_reason(surface):
    markup = _render(surface, [_tie()])

    assert 'data-ffa-cut-ties' in markup
    assert 'data-lt-order-root' in markup
    assert 'data-blocker="cut"' in markup
    assert 'data-scope="ffa:SE:0:0"' in markup
    assert _text(markup).count(PRD) == 1
    assert 'Round 1 · Lobby 1' in markup
    assert 'Places 3–5 are equal on every criterion.' in _text(markup)
    assert 'Anna (1 ' in _text(markup)
    assert f'action="{surface.action}"' in markup
    assert '<input type="hidden" name="scope" value="ffa:SE:0:0">' in markup
    assert '<input type="hidden" name="action" value="save">' in markup
    assert 'data-lt-order-list' in markup
    assert markup.count('name="order"') == 3
    assert markup.count('data-order-value') == 3
    assert 'data-first-place="3"' in markup
    assert re.search(r'<textarea[^>]*name="reason"[^>]*required', markup)
    assert 'data-lt-reason-submit' in markup
    assert 'name="action" value="withdraw"' not in markup


def test_no_script_fallback_preselects_the_alphabetical_order(surface):
    markup = _render(surface, [_tie(ids=('c', 'a', 'b'))])

    selects = re.findall(r'<select.*?</select>', markup, re.S)
    assert len(selects) == 3
    selected = [
        re.search(r'<option value="(\w)" selected', s).group(1) for s in selects
    ]
    assert selected == ['a', 'b', 'c']


def test_decided_lobby_shows_the_order_and_the_withdraw_form(surface):
    tie = _tie(decided=True, ids=('c', 'a'), rank_from=3, decision=_decision())

    markup = _render(surface, [tie])

    text = _text(markup)
    assert 'data-decision="ffa:SE:0:0"' in markup
    assert 'data-blocker' not in markup
    assert 'Orga decision on file' in text
    assert '3. Cleo, 4. Anna' in text
    assert '„Tiebreak at the table.“' in text
    assert 'Ohrwurm' in text
    assert 'name="action" value="withdraw"' in markup
    assert re.findall(r'name="order" value="(\w)"', markup) == ['c', 'a']
    assert re.search(r'<textarea[^>]*name="reason"[^>]*required', markup)
    assert f'action="{surface.action}"' in markup


def test_locked_lobby_offers_no_form(surface):
    open_tie = _tie('ffa:SE:0:0', locked=True)
    decided = _tie(
        'ffa:SE:0:1', decided=True, locked=True, decision=_decision()
    )

    markup = _render(surface, [open_tie, decided])

    assert '<form' not in markup
    assert _text(markup).count(LOCKED) == 2


def test_only_the_listed_lobbies_render(surface):
    markup = _render(surface, [_tie('ffa:SE:0:1')])

    assert markup.count('data-blocker="cut"') == 1
    assert 'data-scope="ffa:SE:0:1"' in markup
    assert 'ffa:SE:0:0' not in markup


def test_block_marks_up_names_without_trusting_them(surface):
    tie = _tie()
    tie['contestants'][0]['name'] = '<script>alert(1)</script>'

    markup = _render(surface, [tie])

    assert '<script>alert(1)' not in markup
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in markup


# -------------------------------------------------------------------- #
# serializer


@pytest.fixture
def plain_translations(monkeypatch):
    monkeypatch.setattr(helpers, 'gettext', _translate)


def _service_tie(scope='ffa:WB:1:2', *, decision=None, decided=False):
    return service.FfaCutTie(
        scope=scope,
        pool='WB',
        round_number=1,
        lobby=2,
        rank_from=3,
        rank_to=4,
        contestants=(
            service.FfaTiedContestant(contestant_id='b', points=7),
            service.FfaTiedContestant(contestant_id='z', points=7),
        ),
        decided=decided,
        decision=decision,
        locked=False,
    )


def test_serializer_labels_and_shapes_an_open_tie(plain_translations):
    rows = helpers.serialize_ffa_cut_ties([_service_tie()], NAMES)

    assert rows == [
        {
            'scope': 'ffa:WB:1:2',
            'scope_label': 'Winners Pool · Round 2 · Lobby 3',
            'kind': 'cut',
            'status': 'open',
            'decided': False,
            'blocking': True,
            'locked': False,
            'places': '3–4',
            'rank_from': 3,
            'contestants': [
                {'id': 'b', 'name': 'Bernd', 'points': 7},
                {'id': 'z', 'name': 'z', 'points': 7},
            ],
            'text': tournament_match_service.QUALIFICATION_TIE_ERROR,
            'decision': None,
            'withdraw_ids': [],
        }
    ]


def test_serializer_carries_the_decision_for_orgas(plain_translations):
    decision = DecisionBlock(
        contestant_ids=('b', 'z'),
        reason='Coin toss.',
        decided_by='u0',
        decided_at=WHEN,
    )
    tie = _service_tie('ffa:SE:0:0', decision=decision, decided=True)

    [row] = helpers.serialize_ffa_cut_ties(
        [tie], NAMES, users={'u0': SimpleNamespace(screen_name='Ohrwurm')}
    )

    assert row['blocking'] is False
    assert row['status'] == 'decided'
    assert row['withdraw_ids'] == ['b', 'z']
    assert row['decision'] == {
        'reason': 'Coin toss.',
        'decided_by': 'Ohrwurm',
        'decided_at': WHEN,
    }


def test_outdated_lobby_decision_renders_with_withdraw_ids(surface):
    outdated = _tie(decision=_decision(), ids=('b', 'a'), status='outdated')
    outdated['decided'] = False
    outdated['blocking'] = False
    open_tie = _tie(ids=('a', 'c'))

    markup = _render(surface, [open_tie, outdated])

    text = _text(markup)
    assert 'Decision outdated' in text
    assert 'Orga decision on file' not in text
    assert 'data-decision-status="outdated"' in markup
    assert markup.count('data-blocker="cut"') == 1
    assert 'name="action" value="withdraw"' in markup
    withdraw = markup[markup.index('name="action" value="withdraw"') :]
    assert re.findall(r'name="order" value="(\w)"', withdraw) == ['b', 'a']
    assert 'Anna' in text and 'Bernd' in text


def test_outdated_decision_of_a_locked_lobby_offers_no_form(surface):
    outdated = _tie(
        decision=_decision(), ids=('b', 'a'), status='outdated', locked=True
    )

    markup = _render(surface, [outdated])

    assert 'Decision outdated' in _text(markup)
    assert '<form' not in markup
    assert _text(markup).count(LOCKED) == 1


@pytest.mark.parametrize(
    ('status', 'sentence'),
    [
        ('COMPLETED', 'The tournament is completed. Take back a result first.'),
        (
            'CANCELLED',
            'The tournament is cancelled. Tie decisions can no longer change.',
        ),
    ],
)
def test_a_terminal_tournament_offers_no_ffa_decision_form(
    surface, status, sentence
):
    ties = [
        _tie(),
        _tie(decision=_decision(), ids=('c', 'd'), decided=True),
        _tie(decision=_decision(), ids=('b', 'a'), status='outdated'),
    ]

    markup = surface.env.get_template('partial').render(
        ffa_cut_ties=ties,
        tournament=SimpleNamespace(
            id='t0', tournament_status=SimpleNamespace(name=status)
        ),
        js_strings={'cancel': 'Cancel'},
    )

    assert '<form' not in markup
    assert 'value="withdraw"' not in markup
    assert _text(markup).count(sentence) == 3


def test_serializer_appends_outdated_decisions(plain_translations):
    block = DecisionBlock(
        contestant_ids=('b', 'z'),
        reason='Coin toss.',
        decided_by='u0',
        decided_at=WHEN,
    )
    item = service.FfaOutdatedDecision(
        scope='ffa:WB:1:2',
        pool='WB',
        round_number=1,
        lobby=2,
        block=block,
        locked=False,
    )

    rows = helpers.serialize_ffa_cut_ties(
        [_service_tie()],
        NAMES,
        users={'u0': SimpleNamespace(screen_name='Ohrwurm')},
        outdated=[item],
    )

    assert [r['status'] for r in rows] == ['open', 'outdated']
    row = rows[1]
    assert row['decided'] is False
    assert row['blocking'] is False
    assert row['places'] is None
    assert row['withdraw_ids'] == ['b', 'z']
    assert row['scope_label'] == 'Winners Pool · Round 2 · Lobby 3'
    assert row['decision']['decided_by'] == 'Ohrwurm'
    assert [c['id'] for c in row['contestants']] == ['b', 'z']


def test_decided_and_outdated_blocks_of_one_scope_get_distinct_ids(surface):
    decided = _tie(decision=_decision(), ids=('c', 'd'), decided=True)
    outdated = _tie(decision=_decision(), ids=('b', 'a'), status='outdated')

    markup = _render(surface, [decided, outdated])

    ids = re.findall(r'\sid="([^"]+)"', markup)
    assert len(ids) == len(set(ids))
    targets = re.findall(r'data-lt-(?:reveal|hide)="([^"]+)"', markup)
    assert len(targets) == 4
    for_targets = re.findall(r'<label[^>]*\sfor="([^"]+)"', markup)
    assert for_targets
    for target in targets + for_targets:
        assert ids.count(target) == 1

    cards = re.findall(r'<section.*?</section>', markup, re.S)
    assert len(cards) == 2
    expected = [['c', 'd'], ['b', 'a']]
    for card, order in zip(cards, expected, strict=True):
        [reveal] = re.findall(r'data-lt-reveal="([^"]+)"', card)
        [form_id] = re.findall(r'<form[^>]*\sid="([^"]+)"', card)
        assert reveal == form_id
        assert re.findall(r'data-lt-hide="([^"]+)"', card) == [form_id]
        assert re.findall(r'name="order" value="(\w)"', card) == order
