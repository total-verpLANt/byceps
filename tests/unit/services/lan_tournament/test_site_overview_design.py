"""
tests.unit.services.lan_tournament.test_site_overview_design
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import re
from types import SimpleNamespace

import pytest

from tests.unit.services.lan_tournament.test_site_seat_badges_render import (
    _player,
    _render,
    make_env,
    ROOT,
)


SITE_CSS = ROOT / 'sites/totalverplant-36/static/style/totalverplant-36.css'
CORE_CSS = ROOT / 'byceps/static/style/lan_tournament.css'
TEMPLATES = ROOT / 'byceps/services/lan_tournament/blueprints/site/templates'


@pytest.fixture(scope='module')
def env():
    return make_env()


def _players(count):
    return [_player(f'Player {n}') for n in range(count)]


def _section(html, css_class):
    """Return the whole element whose class list starts with `css_class`."""
    start = re.search(rf'<(\w+) class="{css_class}[ "]', html)
    assert start, css_class
    tag = start.group(1)
    depth = 0
    for token in re.finditer(rf'<{tag}\b|</{tag}>', html[start.start() :]):
        depth += 1 if token.group(0).startswith('<' + tag) else -1
        if depth == 0:
            return html[start.start() : start.start() + token.end()]
    raise AssertionError(f'unclosed {css_class}')


def test_header_carries_status_stamp_capacity_card_and_plain_tags(env):
    players = _players(6)
    html = _render(
        env,
        players,
        {},
        tournament_fields={
            'tournament_status': SimpleNamespace(name='ONGOING'),
            'max_players': 16,
        },
        elimination_mode_label='Single elimination',
    )

    assert 'class="ov-stamp ov-stamp--success">Ongoing<' in html
    assert '<b class="ov-cap__num">6 <small>/ 16</small></b>' in html
    assert 'ov-cap__bar--available' in html
    assert 'width: 38%' in html
    assert '<p class="ov-cap__pct">38 %</p>' in html
    tags = _section(html, 'ov-tags')
    assert 'Main tournaments' in tags and 'Single elimination' in tags
    assert 'ov-icon' not in tags


def test_capacity_card_without_a_limit_has_no_bar(env):
    html = _render(env, _players(3), {})

    assert '<b class="ov-cap__num">3</b>' in html
    assert 'ov-cap__bar' not in html
    assert 'ov-cap__pct' not in html
    assert 'ov-stamp' not in html


def test_fact_box_carries_one_icon_per_fact(env):
    env.filters['dateformat'] = lambda value, pattern: 'Samstag'
    env.filters['timeformat'] = lambda value, style: '09:09'
    orga = SimpleNamespace(
        user=SimpleNamespace(screen_name='MoD'), duties='Hilfe'
    )
    html = _render(
        env,
        _players(1),
        {},
        tournament_fields={
            'start_time': object(),
            'game_format': SimpleNamespace(label='Free-for-All'),
            'elimination_mode': object(),
            'contestant_type': SimpleNamespace(name='SOLO'),
        },
        orgas=[orga],
        elimination_mode_label='Single elimination',
    )

    facts = _section(html, 'ov-facts')
    for glyph in ('calendar', 'gamepad', 'person', 'award'):
        assert f'ov-icon--{glyph}' in facts
    assert facts.count('class="ov-fact"') == 4
    assert 'Samstag, 09:09' in facts
    assert '<small>Single elimination</small>' in facts
    assert 'MoD <em>(Hilfe)</em>' in facts


def test_facts_and_text_are_omitted_when_empty(env):
    html = _render(env, _players(1), {})

    assert 'ov-facts' not in html
    assert 'ov-txt' not in html


def test_description_and_ruleset_carry_a_heading_icon(env):
    html = _render(
        env,
        _players(1),
        {},
        tournament_fields={'description': 'toll', 'ruleset': 'regel 1'},
    )

    text = _section(html, 'ov-txt')
    assert 'ov-icon--info' in text and 'ov-icon--document' in text
    assert 'Description' in text and 'Ruleset' in text


def test_player_list_is_numbered_with_a_column_head(env):
    html = _render(env, _players(11), {})

    head = _section(html, 'ov-th')
    assert head.count('<span>Player</span>') == 2
    assert head.count('<span>Seat</span>') == 2
    numbers = re.findall(r'<span class="ov-no">(\d+)</span>', html)
    assert numbers[0] == '01' and numbers[-1] == '11'
    assert '11 Players' in html
    assert html.count('ov-icon--person') == 11


def test_overview_makes_no_third_party_requests(env):
    html = _render(env, _players(2), {})

    for text in (
        html,
        CORE_CSS.read_text(),
        SITE_CSS.read_text(),
        (TEMPLATES / 'site/lan_tournament/_overview_icons.html').read_text(),
    ):
        assert 'googleapis' not in text
        assert 'gstatic' not in text
        assert 'Material Symbols' not in text


def test_paper_site_sets_the_large_fact_icons():
    css = SITE_CSS.read_text()

    rule = re.search(r'body\.lt-dark \.ov-fact \.ov-icon \{([^}]*)\}', css)
    assert rule
    assert 'width: 52px' in rule.group(1) and 'height: 52px' in rule.group(1)
