"""
tests.unit.services.lan_tournament.test_site_seat_badges_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament.tournament_personal_repository import (
    ContactSeat,
    ContactSeatArea,
)
from byceps.util.templatefilters import fallback


ROOT = Path(__file__).resolve().parents[4]
SITE_TEMPLATES = (
    ROOT / 'byceps/services/lan_tournament/blueprints/site/templates'
)
SEATING_MACROS = (
    ROOT / 'byceps/services/seating/blueprints/site/templates/macros'
)

VIEW = 'site/lan_tournament/view.html'
STYLESHEET = '/static?filename=style/lan_tournament_personal.css'
LEGACY_LABEL = 'Hall, B-07'


def _url_for(endpoint, **values):
    anchor = values.pop('_anchor', None)
    query = '&'.join(f'{key}={value}' for key, value in values.items())
    url = f'/{endpoint}' + (f'?{query}' if query else '')
    return f'{url}#{anchor}' if anchor else url


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                VIEW: (SITE_TEMPLATES / VIEW).read_text(),
                'site/lan_tournament/_seat_badge.html': (
                    SITE_TEMPLATES / 'site/lan_tournament/_seat_badge.html'
                ).read_text(),
                'macros/seating.html': (
                    SEATING_MACROS / 'seating.html'
                ).read_text(),
                'layout/site/lan_tournament.html': (
                    '{% block head %}<!-- layout head -->{% endblock %}'
                    '{% block body %}{% endblock %}'
                ),
                'macros/icons.html': (
                    '{% macro render_icon(name) %}{% endmacro %}'
                ),
                'macros/misc.html': (
                    '{% macro render_tag(text) %}{% endmacro %}'
                ),
                'macros/lan_tournament.html': (
                    '{% macro render_tournament_subnav(t, tab) %}{% endmacro %}'
                ),
                'site/lan_tournament/orga_actions.html': (
                    '{% macro render_orga_tournament_status_actions('
                    't, may_administrate, start_gate, grand_final, '
                    'winner_tie) %}{% endmacro %}'
                ),
            }
        ),
    )
    e.filters['fallback'] = fallback
    e.globals['_'] = lambda s, **kw: s
    e.globals['url_for'] = _url_for
    return e


def _seat(label='B-07'):
    return ContactSeat(
        id=uuid4(),
        label=label,
        area=ContactSeatArea(slug='main-hall', title='Main hall'),
    )


def _player(screen_name):
    user = SimpleNamespace(id=uuid4(), screen_name=screen_name, deleted=False)
    participant = SimpleNamespace(user_id=user.id)
    return user, participant


def _render(env, players, seat_links, *, teams=(), member_counts=None):
    tournament = SimpleNamespace(
        id='t0',
        name='Cup',
        image_url=None,
        image_alt_text=None,
        game=None,
        tournament_status=None,
        start_time=None,
        game_format=None,
        elimination_mode=None,
        contestant_type=None,
        max_teams=None,
        max_players=None,
        description=None,
        ruleset=None,
    )
    return env.get_template(VIEW).render(
        tournament=tournament,
        active_tab='overview',
        may_administrate=False,
        start_gate=None,
        grand_final=None,
        winner_tie=None,
        only='all',
        match_filter_options=[],
        winner_name=None,
        is_team_tournament=bool(teams),
        team_count=len(teams),
        participant_count=len(players),
        orgas=[],
        can_join=False,
        can_leave=False,
        current_user_participant=None,
        teams=list(teams),
        member_counts=member_counts or {},
        participants=[participant for _, participant in players],
        users_by_id={user.id: user for user, _ in players},
        seats_by_user_id={user.id: LEGACY_LABEL for user, _ in players},
        seat_links_by_user_id=seat_links,
    )


def _rows(html):
    return re.findall(r'<li class="lt-player-row">(.*?)</li>', html, re.DOTALL)


def _badge(seat):
    return (
        '<span class="personal-seat-links lt-seat-badge">'
        f'<a href="/seating.view_area?slug=main-hall#seat-{seat.id}">'
        f'{seat.label}</a></span>'
    )


def test_checked_in_player_with_seat_gets_a_linked_badge(env):
    user, participant = _player('Alice')
    seat = _seat()

    html = _render(env, [(user, participant)], {user.id: [seat]})

    (row,) = _rows(html)
    assert _badge(seat) in row
    assert row.index('lt-player-sep') < row.index('lt-seat-badge')
    assert LEGACY_LABEL not in html
    assert 'lt-player-seat' not in html
    assert html.index('<!-- layout head -->') < html.index(STYLESHEET)


def test_checked_in_player_without_seat_gets_no_badge_or_separator(env):
    seated, seated_participant = _player('Seated')
    unknown, unknown_participant = _player('Unknown')
    emptied, emptied_participant = _player('Emptied')
    seat = _seat()

    html = _render(
        env,
        [
            (seated, seated_participant),
            (unknown, unknown_participant),
            (emptied, emptied_participant),
        ],
        {seated.id: [seat], emptied.id: []},
    )

    seated_row, unknown_row, emptied_row = _rows(html)
    assert _badge(seat) in seated_row
    for row in (unknown_row, emptied_row):
        assert 'lt-seat-badge' not in row
        assert 'personal-seat-links' not in row
        assert 'lt-player-sep' not in row
    assert html.count('lt-seat-badge') == 1
    assert html.count('lt-player-sep') == 1


def test_team_tournament_player_list_uses_badges(env):
    alice, alice_participant = _player('Alice')
    bob, bob_participant = _player('Bob')
    alice_seat = _seat('A-01')
    bob_seat = _seat('B-02')
    team = SimpleNamespace(id=uuid4(), name='Team Red')

    html = _render(
        env,
        [(alice, alice_participant), (bob, bob_participant)],
        {alice.id: [alice_seat], bob.id: [bob_seat]},
        teams=[team],
        member_counts={team.id: 2},
    )

    team_part, player_part = html.split('<details', 1)
    assert '2 members' in team_part
    assert 'lt-seat-badge' not in team_part
    alice_row, bob_row = _rows(player_part)
    assert _badge(alice_seat) in alice_row
    assert _badge(bob_seat) in bob_row
    assert LEGACY_LABEL not in html
