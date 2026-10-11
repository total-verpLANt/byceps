"""
tests.unit.services.lan_tournament.test_site_match_list_labels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
import pathlib
import re
from types import SimpleNamespace
from uuid import uuid4

from jinja2 import DictLoader, Environment, StrictUndefined
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as view_helpers,
)
from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
)
from tests.unit.services.lan_tournament.test_readiness_surface_policy import (
    _tournament,
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


def _make_match(
    *, bracket, round_number, match_order, phase=1, group_order=None
):
    return TournamentMatch(
        id=uuid4(),
        tournament_id=uuid4(),
        group_order=group_order,
        match_order=match_order,
        round=round_number,
        next_match_id=None,
        confirmed_by=None,
        created_at=datetime(2026, 10, 11, 12, 0),
        bracket=bracket,
        phase=phase,
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

    def render(matches, *, repository_order=False, tournament=None):
        if tournament is None:
            tournament = _tournament(
                name='Test Cup',
                party_id='test-party',
                elimination_mode=EliminationMode.ROUND_ROBIN,
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
        if repository_order:
            # Run the real projection over rows in repository order.
            monkeypatch.setattr(
                views.tournament_match_service,
                'get_matches_for_tournament_ordered',
                lambda tournament_id: list(matches),
            )
            monkeypatch.setattr(
                views.tournament_match_service,
                'get_contestants_for_tournament',
                lambda tournament_id: {},
            )
            monkeypatch.setattr(
                views,
                'build_match_readiness_projections',
                lambda t, ms, cs: readiness_by_match_id,
            )
        else:
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


def _group_titles(html):
    titles = re.findall(
        r"<h3 class='match-group__title'>(.*?)</h3>", html, flags=re.DOTALL
    )
    return [' '.join(re.sub(r'<[^>]+>', ' ', t).split()) for t in titles]


def _row_labels(html):
    labels = re.findall(
        r"<div class='match-number'>(.*?)</div>", html, flags=re.DOTALL
    )
    return [' '.join(label.split()) for label in labels]


def test_double_elimination_list_has_each_heading_once(render_list):
    def make(bracket, round_number, match_order):
        return _make_match(
            bracket=bracket, round_number=round_number, match_order=match_order
        )

    # The grand final sits between winners bracket round 1 matches and
    # the two brackets interleave, as `(round, match_order)` returns them.
    repository_order = [
        make(Bracket.WINNERS, 0, 0),
        make(Bracket.GRAND_FINAL, 0, 0),
        make(Bracket.WINNERS, 0, 1),
        make(Bracket.LOSERS, 1, 0),
        make(Bracket.WINNERS, 1, 0),
        make(Bracket.LOSERS, 1, 1),
        make(Bracket.LOSERS, 2, 0),
        make(Bracket.WINNERS, 2, 0),
        make(Bracket.LOSERS, 3, 0),
        make(Bracket.LOSERS, 4, 0),
    ]

    html = render_list(repository_order, repository_order=True)

    assert _group_titles(html) == [
        'Winners bracket Round 1',
        'Winners bracket Round 2',
        'Winners bracket Round 3',
        'Losers bracket Round 2',
        'Losers bracket Round 3',
        'Losers bracket Round 4',
        'Losers bracket Round 5',
        'Grand final',
    ]


def test_third_place_row_keeps_its_capitals(render_list, monkeypatch):
    monkeypatch.setattr(
        view_helpers,
        'gettext',
        lambda msgid, **kw: (
            'Spiel um Platz 3' if msgid == 'Third-place match' else msgid % kw
        ),
    )
    third_place = _make_match(
        bracket=Bracket.THIRD_PLACE, round_number=2, match_order=0
    )

    html = render_list([third_place])

    assert 'Spiel um Platz 3' in html
    assert 'Spiel um platz 3' not in html


def test_single_elimination_third_place_comes_after_the_final(render_list):
    final = _make_match(bracket=None, round_number=2, match_order=0)
    third_place = _make_match(
        bracket=Bracket.THIRD_PLACE, round_number=2, match_order=0
    )
    repository_order = [
        _make_match(bracket=None, round_number=0, match_order=0),
        _make_match(bracket=None, round_number=0, match_order=1),
        _make_match(bracket=None, round_number=1, match_order=0),
        third_place,
        final,
    ]

    html = render_list(repository_order, repository_order=True)

    assert _row_labels(html)[-2:] == ['Match 1', 'Third-place match']


def test_phased_list_separates_groups_and_playoffs(render_list):
    def group(group_order, round_number, match_order):
        return _make_match(
            bracket=None,
            round_number=round_number,
            match_order=match_order,
            group_order=group_order,
        )

    def playoff(round_number, match_order):
        return _make_match(
            bracket=None,
            round_number=round_number,
            match_order=match_order,
            phase=2,
        )

    # The playoff rows interleave with the group rows, as `(round,
    # match_order)` returns them: both phases start at round 0.
    repository_order = [
        group(0, 0, 0),
        playoff(0, 0),
        group(1, 0, 0),
        group(0, 0, 1),
        playoff(0, 1),
        group(1, 1, 0),
        group(0, 1, 0),
        playoff(1, 0),
    ]

    tournament = _tournament(
        name='Test Cup',
        party_id='test-party',
        elimination_mode=EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    html = render_list(
        repository_order, repository_order=True, tournament=tournament
    )

    titles = _group_titles(html)
    assert titles == [
        'Group A Round 1',
        'Group A Round 2',
        'Group B Round 1',
        'Group B Round 2',
        'Playoffs · Round 1',
        'Playoffs · Round 2',
    ]
    assert len(set(titles)) == len(titles)
    assert all(t.startswith('Group') for t in titles[:4])
    assert all(t.startswith('Playoffs') for t in titles[4:])
    assert html.count("data-bracket='Group A' aria-pressed") == 1
    assert html.count("data-bracket='Group B' aria-pressed") == 1


def test_group_rows_are_labelled_with_their_group(render_list):
    group_b = _make_match(
        bracket=None, round_number=0, match_order=0, group_order=1
    )

    html = render_list([group_b])

    assert "<span class='match-group__kicker'>Group B</span> Round 1" in html
    assert _row_labels(html) == ['Match 1']
    assert 'Group A' not in html


def test_plain_list_labels_are_unchanged(render_list):
    matches = [
        _make_match(bracket=None, round_number=0, match_order=0),
        _make_match(bracket=Bracket.WINNERS, round_number=1, match_order=2),
        _make_match(bracket=Bracket.LOSERS, round_number=1, match_order=0),
        _make_match(bracket=Bracket.GRAND_FINAL, round_number=0, match_order=0),
        _make_match(bracket=Bracket.THIRD_PLACE, round_number=2, match_order=0),
    ]

    for match in matches:
        assert view_helpers.build_site_match_list_label(
            match, tournament=_tournament()
        ) == view_helpers.build_match_label(match)

    html = render_list(matches, repository_order=True)

    assert 'Group' not in html
    assert 'Playoffs' not in html
    assert _group_titles(html) == [
        'Round 1',
        'Winners bracket Round 2',
        'Losers bracket Round 2',
        'Grand final',
    ]


def test_ffa_lobbies_are_listed_round_by_round_without_groups(render_list):
    def lobby(round_number, lobby_index):
        # Lobby `n` of a round is stored as `group_order=n, match_order=n`.
        return _make_match(
            bracket=None,
            round_number=round_number,
            match_order=lobby_index,
            group_order=lobby_index,
        )

    repository_order = [
        lobby(0, 0),
        lobby(0, 1),
        lobby(1, 0),
        lobby(1, 1),
        lobby(2, 0),
    ]
    tournament = _tournament(
        name='Test Cup',
        party_id='test-party',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    html = render_list(
        repository_order, repository_order=True, tournament=tournament
    )

    assert 'Group' not in html
    assert "data-bracket='Group" not in html
    assert _group_titles(html) == ['Round 1', 'Round 2', 'Round 3']
    assert _row_labels(html) == [
        'Match 1',
        'Match 2',
        'Match 1',
        'Match 2',
        'Match 1',
    ]
