"""
tests.unit.services.lan_tournament.test_match_display_order
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
from functools import partial
from uuid import uuid4

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_match_label,
    build_site_match_list_label,
    match_display_sort_key,
)
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


_SINGLE_ELIMINATION = _tournament(
    elimination_mode=EliminationMode.SINGLE_ELIMINATION
)
_DOUBLE_ELIMINATION = _tournament(
    elimination_mode=EliminationMode.DOUBLE_ELIMINATION
)
_ROUND_ROBIN = _tournament(elimination_mode=EliminationMode.ROUND_ROBIN)
_ROUND_ROBIN_THEN_ELIMINATION = _tournament(
    elimination_mode=EliminationMode.ROUND_ROBIN,
    playoff_game_format=GameFormat.ONE_V_ONE,
    playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
)
_FFA = _tournament(
    game_format=GameFormat.FREE_FOR_ALL,
    elimination_mode=EliminationMode.SINGLE_ELIMINATION,
)
_FFA_THEN_ELIMINATION = _tournament(
    game_format=GameFormat.FREE_FOR_ALL,
    elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    playoff_game_format=GameFormat.ONE_V_ONE,
    playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
)
_ROUND_ROBIN_THEN_FFA = _tournament(
    elimination_mode=EliminationMode.ROUND_ROBIN,
    playoff_game_format=GameFormat.FREE_FOR_ALL,
    playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
)


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


def _display_order(matches, tournament):
    return sorted(
        matches, key=partial(match_display_sort_key, tournament=tournament)
    )


def test_double_elimination_orders_winners_then_losers_then_grand_final():
    winners = [
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=0),
        _make_match(bracket=Bracket.WINNERS, round_number=0, match_order=1),
        _make_match(bracket=Bracket.WINNERS, round_number=1, match_order=0),
        _make_match(bracket=Bracket.WINNERS, round_number=2, match_order=0),
    ]
    losers = [
        _make_match(bracket=Bracket.LOSERS, round_number=1, match_order=0),
        _make_match(bracket=Bracket.LOSERS, round_number=1, match_order=1),
        _make_match(bracket=Bracket.LOSERS, round_number=2, match_order=0),
        _make_match(bracket=Bracket.LOSERS, round_number=3, match_order=0),
        _make_match(bracket=Bracket.LOSERS, round_number=4, match_order=0),
    ]
    grand_final = _make_match(
        bracket=Bracket.GRAND_FINAL, round_number=0, match_order=0
    )
    expected = [*winners, *losers, grand_final]
    repository_order = sorted(expected, key=lambda m: (m.round, m.match_order))
    assert repository_order != expected

    actual = _display_order(repository_order, _DOUBLE_ELIMINATION)

    assert actual == expected


def test_single_elimination_puts_third_place_after_the_final():
    rounds = [
        _make_match(bracket=None, round_number=0, match_order=0),
        _make_match(bracket=None, round_number=0, match_order=1),
        _make_match(bracket=None, round_number=1, match_order=0),
        _make_match(bracket=None, round_number=2, match_order=0),
    ]
    third_place = _make_match(
        bracket=Bracket.THIRD_PLACE, round_number=2, match_order=0
    )
    repository_order = [*rounds[:3], third_place, rounds[3]]

    actual = _display_order(repository_order, _SINGLE_ELIMINATION)

    assert actual == [*rounds, third_place]


def test_phase_two_follows_phase_one():
    round_robin = [
        _make_match(bracket=None, round_number=0, match_order=0, phase=1),
        _make_match(bracket=None, round_number=1, match_order=0, phase=1),
    ]
    playoff = _make_match(bracket=None, round_number=0, match_order=0, phase=2)
    repository_order = [round_robin[0], playoff, round_robin[1]]

    actual = _display_order(repository_order, _ROUND_ROBIN_THEN_ELIMINATION)

    assert actual == [*round_robin, playoff]


def test_round_robin_keeps_round_then_match_order():
    first = _make_match(
        bracket=None, round_number=0, match_order=0, group_order=0
    )
    second = _make_match(
        bracket=None, round_number=0, match_order=1, group_order=0
    )
    third = _make_match(
        bracket=None, round_number=1, match_order=0, group_order=0
    )
    repository_order = [first, second, third]

    actual = _display_order(reversed(repository_order), _ROUND_ROBIN)

    assert actual == repository_order


def test_phase_one_groups_sort_before_rounds():
    def make(group_order, round_number, match_order):
        return _make_match(
            bracket=None,
            round_number=round_number,
            match_order=match_order,
            group_order=group_order,
        )

    group_a = [make(0, 0, 0), make(0, 0, 1), make(0, 1, 0)]
    group_b = [make(1, 0, 0), make(1, 1, 0), make(1, 1, 1)]
    repository_order = sorted(
        [*group_b, *group_a], key=lambda m: (m.round, m.match_order)
    )
    assert repository_order != [*group_a, *group_b]

    actual = _display_order(repository_order, _ROUND_ROBIN)

    assert actual == [*group_a, *group_b]


def test_playoff_label_carries_the_playoffs_kicker():
    playoff = _make_match(
        bracket=Bracket.WINNERS, round_number=0, match_order=0, phase=2
    )

    label = build_site_match_list_label(
        playoff, tournament=_ROUND_ROBIN_THEN_ELIMINATION
    )

    assert label.startswith('Playoffs · ')
    assert label.endswith(build_match_label(playoff))


def test_round_robin_group_label_names_the_group():
    group_b = _make_match(
        bracket=None, round_number=0, match_order=1, group_order=1
    )

    label = build_site_match_list_label(group_b, tournament=_ROUND_ROBIN)

    assert label == 'Group B, ' + build_match_label(group_b)


def _ffa_lobbies(phase):
    # Lobby `n` of a round is stored as `group_order=n, match_order=n`.
    return [
        _make_match(
            bracket=None,
            round_number=round_number,
            match_order=lobby,
            group_order=lobby,
            phase=phase,
        )
        for round_number, lobby in [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0)]
    ]


def test_ffa_lobbies_sort_round_by_round():
    expected = _ffa_lobbies(phase=1)

    actual = _display_order(reversed(expected), _FFA)

    assert actual == expected


def test_ffa_lobby_label_is_the_plain_match_label():
    labels = [
        build_site_match_list_label(match, tournament=_FFA)
        for match in _ffa_lobbies(phase=1)
    ]

    assert labels == [
        'Round 1, match 1',
        'Round 1, match 2',
        'Round 2, match 1',
        'Round 2, match 2',
        'Round 3, match 1',
    ]


def test_playoff_ffa_lobbies_have_no_group_term():
    lobbies = _ffa_lobbies(phase=2)

    keys = [
        match_display_sort_key(match, tournament=_ROUND_ROBIN_THEN_FFA)
        for match in lobbies
    ]

    assert {key[1] for key in keys} == {-1}
    assert _display_order(reversed(lobbies), _ROUND_ROBIN_THEN_FFA) == lobbies


def test_playoff_ffa_lobby_labels_carry_only_the_playoffs_kicker():
    labels = [
        build_site_match_list_label(match, tournament=_ROUND_ROBIN_THEN_FFA)
        for match in _ffa_lobbies(phase=2)
    ]

    assert labels[0] == 'Playoffs · Round 1, match 1'
    assert all(label.startswith('Playoffs · ') for label in labels)
    assert not any('Group' in label for label in labels)


def test_round_robin_groups_stay_groups_before_ffa_playoffs():
    groups = [
        _make_match(bracket=None, round_number=0, match_order=0, group_order=0),
        _make_match(bracket=None, round_number=1, match_order=0, group_order=0),
        _make_match(bracket=None, round_number=0, match_order=0, group_order=1),
    ]
    lobbies = _ffa_lobbies(phase=2)
    tournament = _ROUND_ROBIN_THEN_FFA

    actual = _display_order(reversed([*groups, *lobbies]), tournament)

    assert actual == [*groups, *lobbies]
    assert [
        build_site_match_list_label(match, tournament=tournament)
        for match in groups
    ] == [
        'Group A, Round 1, match 1',
        'Group A, Round 2, match 1',
        'Group B, Round 1, match 1',
    ]


def test_ffa_lobbies_before_elimination_playoffs_stay_plain():
    lobbies = _ffa_lobbies(phase=1)
    playoff = _make_match(bracket=None, round_number=0, match_order=0, phase=2)
    tournament = _FFA_THEN_ELIMINATION

    actual = _display_order(reversed([*lobbies, playoff]), tournament)

    assert actual == [*lobbies, playoff]
    assert [
        build_site_match_list_label(match, tournament=tournament)
        for match in lobbies
    ] == [build_match_label(match) for match in lobbies]
    assert build_site_match_list_label(
        playoff, tournament=tournament
    ).startswith('Playoffs · ')
