"""Participant group layouts and the positional qualification line."""

from pathlib import Path
import re
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)


_TEMPLATE = Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/_standings.html'
)


def _ranking(ranks=(), *, is_table=True):
    return {
        'is_table': is_table,
        'scope': 'group:0',
        'label': 'Group A',
        'open_matches': 0,
        'has_tie': len(set(ranks)) != len(ranks),
        'cut': 2,
        'entries': [
            {
                'name': f'Player {index}',
                'rank': rank,
                'rank_label': str(rank),
                'shared': ranks.count(rank) > 1,
                'status': 'tie' if ranks.count(rank) > 1 else 'open',
                'status_label': 'Open',
                'decided_by': None,
                'decided_by_label': None,
                'value': 10,
                'stats': {
                    'played': 2,
                    'won': 1,
                    'drawn': 0,
                    'lost': 1,
                    'diff': 0,
                    'score_for': 1,
                    'points': 3,
                }
                if is_table
                else None,
            }
            for index, rank in enumerate(ranks, start=1)
        ],
    }


def _render(rankings):
    env = Environment(undefined=StrictUndefined, autoescape=True)
    env.globals['_'] = lambda text, **params: text % params if params else text
    env.globals['ngettext'] = lambda singular, plural, n, **params: (
        (singular if n == 1 else plural) % params
    )
    env.filters['numberformat'] = str
    return str(
        env.from_string(_TEMPLATE.read_text()).module.render_group_standings(
            rankings
        )
    )


def test_an_empty_first_group_keeps_the_table_layout():
    html = _render([_ranking(), _ranking((1, 2))])
    cards = re.findall(r'<section.*?</section>', html, re.S)
    assert len(cards) == 2
    assert 'lt-st--board' not in cards[1]
    assert 'Pld.' in cards[1]
    assert 'Points → Head-to-head' in html


def test_each_card_uses_its_own_layout():
    html = _render([_ranking((1,)), _ranking((1,), is_table=False)])
    first, second = re.findall(r'<section.*?</section>', html, re.S)
    assert 'Pld.' in first
    assert 'lt-st--board' in second
    assert 'Pld.' not in second


def _assert_cut_after_second_row(ranks):
    html = _render([_ranking(ranks)])
    assert html.count("class='lt-cut'") == 1
    assert (
        html.index('Player 2')
        < html.index("class='lt-cut'")
        < html.index('Player 3')
    )


def test_positional_cut_line_is_kept_through_a_shared_rank():
    _assert_cut_after_second_row((1, 2, 2))


def test_cut_line_below_a_clear_rank():
    _assert_cut_after_second_row((1, 2, 3))


@pytest.mark.parametrize('source', ['groups', 'leaderboard', 'winner'])
def test_participant_rankings_carry_the_source_layout(monkeypatch, source):
    full = {'rankings': [_ranking(), _ranking((1, 2))]}
    monkeypatch.setattr(
        helpers, 'serialize_qualification', lambda *args, **kwargs: full
    )
    monkeypatch.setattr(helpers, 'gettext', lambda text: text)
    rankings = helpers.participant_rankings(
        SimpleNamespace(source=source), {}, {}, SimpleNamespace()
    )
    assert [ranking['is_table'] for ranking in rankings] == [
        source == 'groups'
    ] * 2
