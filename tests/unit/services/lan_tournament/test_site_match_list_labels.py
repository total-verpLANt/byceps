"""
tests.unit.services.lan_tournament.test_site_match_list_labels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
import pathlib
from types import SimpleNamespace
from uuid import uuid4

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


_TEMPLATE = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/site/templates'
    '/site/lan_tournament/matches.html'
)

_MACROS = '''
{% macro render_hover_name(name) %}{{ name }}{% endmacro %}
{% macro render_tournament_subnav(tournament, active_tab) %}{% endmacro %}
{% macro render_match_ref(match) -%}
{{ match.bracket.value }} R{{ match.round + 1 }} M{{ match.match_order + 1 }}
{%- endmacro %}
'''


def _make_match(*, bracket, round_number, match_order):
    return TournamentMatch(
        id=uuid4(),
        tournament_id=uuid4(),
        group_order=None,
        match_order=match_order,
        round=round_number,
        next_match_id=None,
        confirmed_by=None,
        created_at=datetime(2026, 10, 11, 12, 0),
        bracket=bracket,
    )


@pytest.fixture
def render_list(monkeypatch):
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'matches': _TEMPLATE.read_text(),
                'layout/site/lan_tournament.html': (
                    '{% block body %}{% endblock %}'
                ),
                'macros/lan_tournament.html': _MACROS,
            }
        ),
    )
    env.globals['_'] = lambda s, **kw: s
    env.globals['url_for'] = lambda endpoint, **kw: endpoint

    def render(matches):
        tournament = SimpleNamespace(
            id=uuid4(),
            name='Test Cup',
            party_id='test-party',
            tournament_status=TournamentStatus.ONGOING,
        )
        match_data = [
            {
                'match': match,
                'contestants': [],
                'readiness': SimpleNamespace(
                    match_id=match.id,
                    filter_bucket='waiting',
                    outcome=None,
                    ready_sides=(),
                ),
            }
            for match in matches
        ]
        readiness_by_match_id = {
            entry['match'].id: entry['readiness'] for entry in match_data
        }

        monkeypatch.setattr(
            views, '_get_tournament_or_404', lambda tournament_id: tournament
        )
        monkeypatch.setattr(
            views,
            '_site_match_projections',
            lambda t: (match_data, readiness_by_match_id),
        )
        monkeypatch.setattr(
            views.tournament_participant_service,
            'get_participants_for_tournament',
            lambda tournament_id: [],
        )
        monkeypatch.setattr(
            views, 'build_contestant_name_lookups', lambda *a, **kw: ({}, {})
        )
        monkeypatch.setattr(
            views, 'build_hover_lookups', lambda *a, **kw: ({}, {})
        )
        monkeypatch.setattr(
            views,
            'g',
            SimpleNamespace(user=SimpleNamespace(authenticated=False)),
        )
        monkeypatch.setattr(views, 'request', SimpleNamespace(args={}))

        context = views.matches.__wrapped__(str(tournament.id))
        return env.get_template('matches').render(**context)

    return render


def test_match_list_spells_out_the_bracket_position(render_list):
    winners = _make_match(
        bracket=Bracket.WINNERS, round_number=1, match_order=2
    )
    final = _make_match(
        bracket=Bracket.GRAND_FINAL, round_number=0, match_order=0
    )

    html = render_list([winners, final])

    assert "<span class='match-group__kicker'>Winners bracket</span> Round 2" in html
    assert 'Match 3' in html
    assert "<span class='match-group__kicker'>Grand final</span>" not in html
    assert 'Grand final' in html


def test_match_list_no_longer_shows_only_the_short_code(render_list):
    winners = _make_match(
        bracket=Bracket.WINNERS, round_number=1, match_order=2
    )

    html = render_list([winners])

    assert 'WB R2 M3' not in html
    assert 'Winners bracket' in html
    assert 'Match 3' in html


def test_match_list_groups_consecutive_matches_under_one_heading(render_list):
    matches = [
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=0),
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=1),
        _make_match(bracket=Bracket.WINNERS, round_number=1, match_order=0),
    ]

    html = render_list(matches)

    assert html.count("<section class='match-group'") == 2
    assert html.count('</section>') == 2
    assert html.count('match-group__title') == 2


def test_bracket_filter_lists_each_bracket_once(render_list):
    matches = [
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=0),
        _make_match(bracket=Bracket.WINNERS, round_number=1, match_order=0),
        _make_match(bracket=Bracket.LOSERS, round_number=0, match_order=0),
    ]

    html = render_list(matches)

    assert "id='match-bracket-bar'" in html
    assert html.count("data-bracket='Winners bracket' aria-pressed") == 1
    assert html.count("data-bracket='Losers bracket' aria-pressed") == 1


def test_bracket_filter_is_hidden_for_a_single_bracket(render_list):
    matches = [
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=0),
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=1),
    ]

    html = render_list(matches)

    assert 'match-bracket-bar' not in html
