"""
tests.unit.services.lan_tournament.test_site_playoff_lobby_rounds
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The site lists the phase-2 FFA lobbies under one heading per pool and round.
"""

import pathlib
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.bracket import Bracket


_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/_standings.html'
)


@pytest.fixture(autouse=True)
def _plain_gettext(monkeypatch):
    monkeypatch.setattr(views, 'gettext', lambda s, **kw: s)


def _entry(bracket, round_number, group_order, names):
    match = SimpleNamespace(
        id=f'{bracket}-{round_number}-{group_order}',
        bracket=bracket,
        round=round_number,
        group_order=group_order,
        confirmed_by=None,
    )
    contestants = [
        SimpleNamespace(team_id=None, participant_id=name) for name in names
    ]
    return {'match': match, 'contestants': contestants}


def _rounds(entries):
    participants = {
        name: SimpleNamespace(screen_name=name, id=name)
        for entry in entries
        for name in (c.participant_id for c in entry['contestants'])
    }
    return views._playoff_lobby_rounds(entries, {}, participants)


def _de_entries():
    return [
        _entry(Bracket.WINNERS, 1, 0, ['w1', 'w2']),
        _entry(Bracket.LOSERS, 1, 0, ['l1', 'l2']),
        _entry(Bracket.WINNERS, 2, 0, ['w3', 'w4']),
        _entry(Bracket.LOSERS, 2, 0, ['l3', 'l4']),
        _entry(Bracket.GRAND_FINAL, 3, 0, ['g1', 'g2']),
    ]


def test_each_pool_and_round_gets_its_own_section():
    sections = _rounds(_de_entries())

    assert [(s['pool'], s['number']) for s in sections] == [
        ('Winners Pool', 1),
        ('Winners Pool', 2),
        ('Losers Pool', 1),
        ('Losers Pool', 2),
        ('Grand Final', None),
    ]
    assert [[lobby['names'] for lobby in s['lobbies']] for s in sections] == [
        [['w1', 'w2']],
        [['w3', 'w4']],
        [['l1', 'l2']],
        [['l3', 'l4']],
        [['g1', 'g2']],
    ]


def test_lobbies_of_one_pool_and_round_share_a_section():
    sections = _rounds(
        [
            _entry(Bracket.WINNERS, 1, 1, ['b']),
            _entry(Bracket.WINNERS, 1, 0, ['a']),
            _entry(Bracket.LOSERS, 1, 0, ['c']),
        ]
    )

    assert len(sections) == 2
    assert [lobby['names'] for lobby in sections[0]['lobbies']] == [
        ['a'],
        ['b'],
    ]


def test_single_elimination_keeps_one_numbered_heading_per_round():
    sections = _rounds(
        [
            _entry(None, 0, 0, ['a', 'b']),
            _entry(None, 0, 1, ['c', 'd']),
            _entry(None, 1, 0, ['a', 'c']),
        ]
    )

    assert [(s['pool'], s['number']) for s in sections] == [
        (None, 1),
        (None, 2),
    ]
    assert len(sections[0]['lobbies']) == 2


def test_the_template_names_the_pool_in_the_heading():
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader({'standings': _TEMPLATE.read_text()}),
        extensions=['jinja2.ext.i18n'],
    )
    env.install_null_translations(newstyle=True)
    env.globals['url_for'] = lambda endpoint, **k: endpoint

    html = env.get_template('standings').module.render_playoff_lobbies(
        _rounds(_de_entries())
    )

    headings = [
        line.strip()
        for line in html.splitlines()
        if line.strip().startswith('<h3')
    ]
    assert len(headings) == 5
    assert len(set(headings)) == 5
    assert any('Winners Pool' in h and 'Round 1' in h for h in headings)
    assert any('Losers Pool' in h and 'Round 1' in h for h in headings)
    assert any('Grand Final' in h and 'Round' not in h for h in headings)
