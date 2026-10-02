"""
tests.unit.services.lan_tournament.test_admin_subnav_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import html as htmllib
import pathlib
import re
from types import SimpleNamespace

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest


_ADMIN_DIR = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament'
)

_PAGE = (
    "{% from 'admin/lan_tournament/_subnav.html' import render_admin_subnav %}"
    '{{ render_admin_subnav(tournament, active_tab) }}'
)


@pytest.fixture(scope='module')
def template():
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'page': _PAGE,
                'admin/lan_tournament/_subnav.html': (
                    _ADMIN_DIR / '_subnav.html'
                ).read_text(),
            }
        ),
    )
    env.globals['_'] = lambda message: message
    env.globals['url_for'] = lambda endpoint, **k: '/' + endpoint.lstrip('.')
    return env.get_template('page')


def _tournament(
    *,
    game_format='SINGLE_ELIMINATION',
    team=False,
    has_playoffs=True,
    elimination_mode=None,
):
    return SimpleNamespace(
        id='t0',
        contestant_type=SimpleNamespace(name='TEAM' if team else 'SOLO'),
        game_format=SimpleNamespace(name=game_format),
        has_playoffs=has_playoffs,
        elimination_mode=(
            SimpleNamespace(name=elimination_mode) if elimination_mode else None
        ),
    )


def _tabs(html):
    """Return (label, is_current) for each tab."""
    return [
        (
            htmllib.unescape(re.sub(r'\s+', ' ', label).strip()),
            'button--current' in cls,
        )
        for cls, label in re.findall(
            r'<a class="([^"]*)" href="[^"]*">(.*?)</a>', html, re.S
        )
    ]


def _current(html):
    return [label for label, current in _tabs(html) if current]


@pytest.mark.parametrize(
    ('active_tab', 'label'),
    [
        ('overview', 'Overview'),
        ('seeding', 'Seeding'),
        ('qualification', 'Qualification & release'),
    ],
)
def test_subnav_on_overview_seeding_and_qualification(
    template, active_tab, label
):
    html = template.render(tournament=_tournament(), active_tab=active_tab)

    assert _current(html) == [label]
    assert 'Participants' in html and 'Orgas' in html


def test_subnav_shows_teams_only_for_team_tournaments(template):
    solo = template.render(tournament=_tournament(), active_tab='')
    team = template.render(tournament=_tournament(team=True), active_tab='')

    assert 'Teams' not in solo
    assert 'Teams' in team


def test_subnav_hides_seeding_for_highscore(template):
    html = template.render(
        tournament=_tournament(game_format='HIGHSCORE'), active_tab='overview'
    )

    labels = [label for label, _ in _tabs(html)]
    assert 'Seeding' not in labels
    assert 'Leaderboard' in labels


def test_subnav_label_qualification_and_release(template):
    html = template.render(tournament=_tournament(), active_tab='')

    labels = [label for label, _ in _tabs(html)]
    assert 'Qualification & release' in labels
    assert 'Qualification' not in labels


def test_subnav_hides_qualification_without_playoffs(template):
    html = template.render(
        tournament=_tournament(has_playoffs=False), active_tab=''
    )

    assert 'Qualification' not in html
