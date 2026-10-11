"""
tests.unit.services.lan_tournament.test_site_match_seat_badges_render
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)
import pytest

from byceps.services.lan_tournament.tournament_personal_repository import (
    ContactSeat,
    ContactSeatArea,
)
from byceps.util.templatefilters import fallback


_SERVICES = Path(__file__).resolve().parents[4] / 'byceps/services'
_MATCH_TEMPLATE = 'site/lan_tournament/view_match.html'
_VOID_TAGS = {'br', 'hr', 'img', 'input', 'link', 'meta'}


class _Markup(HTMLParser):
    """Collect seat badge links and the classes the page renders."""

    def __init__(self, html):
        super().__init__()
        self.classes = Counter()
        self.badge_links = []
        self._stack = []
        self._link = None
        self.feed(html)

    def _inside(self, css_class):
        return any(css_class in classes for _, classes in self._stack)

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        attributes = dict(attrs)
        classes = set(attributes.get('class', '').split())
        self.classes.update(classes)
        if tag == 'a' and self._inside('lt-seat-badge'):
            self._link = {
                'href': attributes['href'],
                'text': '',
                'in_hover_card': self._inside('lt-hover-card'),
            }
        self._stack.append((tag, classes))

    def handle_endtag(self, tag):
        while self._stack:
            open_tag, _ = self._stack.pop()
            if open_tag == tag:
                break
        if tag == 'a' and self._link is not None:
            self.badge_links.append(self._link)
            self._link = None

    def handle_data(self, data):
        if self._link is not None:
            self._link['text'] += data


@pytest.fixture(scope='module')
def env():
    e = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        'layout/site/lan_tournament.html': (
                            '{% block head %}{% endblock %}'
                            '{% block body %}{% endblock %}'
                        ),
                        'site/lan_tournament/_match_readiness.html': '',
                        'site/lan_tournament/orga_actions.html': (
                            '{% macro render_orga_match_actions() %}'
                            '{{ kwargs|length }}{% endmacro %}'
                        ),
                    }
                ),
                FileSystemLoader(
                    [
                        _SERVICES / 'lan_tournament/blueprints/site/templates',
                        _SERVICES / 'seating/blueprints/site/templates',
                        _SERVICES / 'core/blueprints/common/templates',
                    ]
                ),
            ]
        ),
    )
    e.globals['_'] = lambda message, **values: (
        message % values if values else message
    )
    e.globals['url_for'] = _url_for
    e.filters['fallback'] = fallback
    # The comment list is empty here; the filters only need to exist.
    e.filters['dateformat'] = str
    e.filters['timeformat'] = lambda value, *args: str(value)
    return e


def _url_for(endpoint, **values):
    anchor = values.pop('_anchor', None)
    url = '/'.join(['', endpoint, *(str(v) for v in values.values())])
    return f'{url}#{anchor}' if anchor else url


def _seat(label):
    area = ContactSeatArea(slug='hall-a', title='Hall A')
    return ContactSeat(id=uuid4(), label=label, area=area)


def _seat_href(seat):
    return f'/seating.view_area/hall-a#seat-{seat.id}'


def _user(screen_name):
    return SimpleNamespace(id=uuid4(), screen_name=screen_name, deleted=False)


def _contestant(*, user=None, team=None):
    return SimpleNamespace(
        participant_id=user.id if user else None,
        team_id=team.id if team else None,
        score=1,
        placement=None,
        points=None,
    )


def _render(
    env,
    *,
    contestants,
    confirmed,
    users=(),
    teams=(),
    seat_links=None,
    team_members=None,
):
    match = SimpleNamespace(
        id=uuid4(),
        round=0,
        match_order=0,
        bracket=None,
        confirmed_by=uuid4() if confirmed else None,
    )
    tournament = SimpleNamespace(
        id=uuid4(),
        name='Seat Cup',
        contestant_type=None,
        game_format=None,
        elimination_mode=None,
    )
    context = {
        'tournament': tournament,
        'active_tab': 'matches',
        'match': match,
        'match_label': '',
        'contestants': contestants,
        'is_ffa': False,
        'teams_by_id': {t.id: t for t in teams},
        'participants_by_id': {u.id: u for u in users},
        'seats_by_user_id': {},
        'team_members_by_team_id': team_members or {},
        'seat_links_by_user_id': seat_links or {},
        'comments': [],
        'comment_users_by_id': {},
        'current_user_can_submit': False,
        'current_user_can_comment': False,
        'orga_confirm_available': False,
        'may_administrate': False,
        'max_match_score': 999_999_999,
        'correction_case': None,
        'affected_downstream_matches': [],
        'ack_match_ids': [],
        'is_walkover': False,
        'ffa_result_consumed': False,
        'results_editable': False,
        'phase_lock': None,
    }
    return env.get_template(_MATCH_TEMPLATE).render(**context)


def test_participant_seat_badge_is_always_visible_in_the_match_view(env):
    alice, bob = _user('Alice'), _user('Bob')
    alice_seat, bob_seat = _seat('A1'), _seat('B2')

    for confirmed in (False, True):
        html = _render(
            env,
            contestants=[_contestant(user=alice), _contestant(user=bob)],
            confirmed=confirmed,
            users=[alice, bob],
            seat_links={alice.id: [alice_seat], bob.id: [bob_seat]},
        )

        assert _Markup(html).badge_links == [
            {
                'href': _seat_href(alice_seat),
                'text': 'A1',
                'in_hover_card': False,
            },
            {
                'href': _seat_href(bob_seat),
                'text': 'B2',
                'in_hover_card': False,
            },
        ], f'confirmed={confirmed}'


def test_participant_without_seat_has_no_badge_in_the_match_view(env):
    badge_macro = env.from_string(
        "{% from 'site/lan_tournament/_seat_badge.html'"
        ' import render_seat_badge %}'
        '{{ render_seat_badge(seats) }}'
    )
    assert badge_macro.render(seats=None) == ''
    assert badge_macro.render(seats=[]) == ''

    alice, bob, carol = _user('Alice'), _user('Bob'), _user('Carol')
    alice_seat = _seat('A1')

    for confirmed in (False, True):
        html = _render(
            env,
            contestants=[_contestant(user=alice), _contestant(user=bob)],
            confirmed=confirmed,
            users=[alice, bob, carol],
            seat_links={alice.id: [alice_seat], bob.id: []},
        )

        links = _Markup(html).badge_links
        assert [link['href'] for link in links] == [_seat_href(alice_seat)]
        assert 'Bob' in html
        assert html.count('lt-seat-badge') == 1, f'confirmed={confirmed}'

        html = _render(
            env,
            contestants=[_contestant(user=alice), _contestant(user=carol)],
            confirmed=confirmed,
            users=[alice, bob, carol],
            seat_links={},
        )

        assert 'lt-seat-badge' not in html, f'confirmed={confirmed}'
        assert 'seating.view_area' not in html


def test_team_contestant_keeps_the_hover_card_and_gets_no_badge(env):
    alice, bob = _user('Alice'), _user('Bob')
    red = SimpleNamespace(id=uuid4(), name='Team Red')
    blue = SimpleNamespace(id=uuid4(), name='Team Blue')
    team_members = {
        red.id: [('Alice', 'A1')],
        blue.id: [('Bob', 'B2')],
    }

    for confirmed in (False, True):
        html = _render(
            env,
            contestants=[_contestant(team=red), _contestant(team=blue)],
            confirmed=confirmed,
            users=[alice, bob],
            teams=[red, blue],
            seat_links={alice.id: [_seat('A1')], bob.id: [_seat('B2')]},
            team_members=team_members,
        )

        markup = _Markup(html)
        assert markup.classes['lt-hover-card'] == 2, f'confirmed={confirmed}'
        assert markup.classes['lt-hover-member'] == 2
        assert 'Team Red' in html and 'Team Blue' in html
        assert 'Alice [A1]' in html and 'Bob [B2]' in html
        assert markup.classes['lt-seat-badge'] == 0
        assert 'seating.view_area' not in html
