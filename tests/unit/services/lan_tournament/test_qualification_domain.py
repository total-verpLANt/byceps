import inspect

import pytest

from byceps.services.lan_tournament import (
    tournament_qualification_domain_service as q,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    _standard_seed_order,
)
from byceps.util.result import Err, Ok


def _m(a, b, score_a, score_b, *, confirmed=True):
    return q.MatchResult(
        a=a, b=b, score_a=score_a, score_b=score_b, confirmed=confirmed
    )


def _ranks(ranking):
    return [(e.contestant_id, e.rank) for e in ranking.entries]


def _three_group(prefix, margin):
    """x beats y and z, y beats z, all by `margin` goals."""
    x, y, z = (f'{prefix}{c}' for c in 'xyz')
    return (
        [x, y, z],
        [_m(x, y, margin, 0), _m(x, z, margin, 0), _m(y, z, margin, 0)],
    )


def test_rank_round_robin_unambiguous():
    ids = ['a', 'b', 'c']
    results = [_m('a', 'b', 2, 0), _m('a', 'c', 1, 0), _m('b', 'c', 3, 1)]

    ranking = q.rank_round_robin('group:1', ids, results)

    assert _ranks(ranking) == [('a', 1), ('b', 2), ('c', 3)]
    assert ranking.ties == ()
    assert ranking.open_matches == 0
    assert [e.shared for e in ranking.entries] == [False] * 3
    assert [e.decided_by for e in ranking.entries] == [None] * 3
    assert ranking.entries[0].row.points == 6
    assert ranking.entries[1].row.diff == 0


def test_rank_round_robin_head_to_head_breaks_tie():
    # a and b both have 6 points; b has the far better difference,
    # but a won the direct meeting.
    ids = ['a', 'b', 'c', 'd']
    results = [
        _m('a', 'b', 1, 0),
        _m('a', 'c', 0, 5),
        _m('a', 'd', 1, 0),
        _m('b', 'c', 5, 0),
        _m('b', 'd', 5, 0),
        _m('c', 'd', 2, 2),
    ]

    ranking = q.rank_round_robin('group:1', ids, results)

    assert _ranks(ranking) == [('a', 1), ('b', 2), ('c', 3), ('d', 4)]
    assert ranking.entries[0].decided_by == 'head_to_head'
    assert ranking.entries[1].decided_by == 'head_to_head'
    assert ranking.entries[0].row.points == ranking.entries[1].row.points
    assert ranking.entries[0].row.diff < ranking.entries[1].row.diff
    assert ranking.ties == ()


def test_head_to_head_skipped_when_unequal_meetings():
    # A chain a > b > c > d > a, so all four have 3 points and a and c
    # never met. The mini-table would see unequal meetings.
    ids = ['a', 'b', 'c', 'd']
    results = [
        _m('a', 'b', 5, 0),
        _m('b', 'c', 1, 0),
        _m('c', 'd', 3, 0),
        _m('d', 'a', 1, 0),
    ]

    ranking = q.rank_round_robin('group:1', ids, results)

    assert _ranks(ranking) == [('a', 1), ('c', 2), ('d', 3), ('b', 4)]
    assert {e.decided_by for e in ranking.entries} == {'difference'}
    assert q._head_to_head_parts(tuple(ids), results) == [tuple(ids)]
    chain = [_m('a', 'b', 1, 0), _m('b', 'c', 1, 0)]
    assert q._head_to_head_parts(('a', 'b', 'c'), chain) == [('a', 'b', 'c')]


def test_head_to_head_applies_to_equal_meetings_between_pairs():
    results = [_m('a', 'b', 0, 1), _m('a', 'b', 4, 0)]

    assert q._head_to_head_parts(('a', 'b'), results) == [('a', 'b')]
    assert q._head_to_head_parts(('a', 'b'), results[:1]) == [('b',), ('a',)]


def test_unconfirmed_results_count_as_open_not_points():
    results = [_m('a', 'b', 9, 0, confirmed=False), _m('a', 'c', 1, 0)]

    ranking = q.rank_round_robin('group:1', ['a', 'b', 'c'], results)

    assert ranking.open_matches == 1
    assert ranking.entries[0].row.points == 3
    assert {e.contestant_id: e.row.played for e in ranking.entries} == {
        'a': 1,
        'b': 0,
        'c': 1,
    }


def _tie_2_to_4():
    """a wins all; b, c and d beat each other in a perfect cycle."""
    ids = ['a', 'b', 'c', 'd']
    results = [
        _m('a', 'b', 1, 0),
        _m('a', 'c', 1, 0),
        _m('a', 'd', 1, 0),
        _m('b', 'c', 1, 0),
        _m('c', 'd', 1, 0),
        _m('d', 'b', 1, 0),
    ]
    return ids, results


def test_cut_tie_classified_cut():
    ids, results = _tie_2_to_4()
    ranking = q.rank_round_robin('group:1', ids, results)

    classified = q.classify_ties(ranking, cut=2, plain_winner=False)

    (tie,) = classified.ties
    assert tie.kind is q.TieKind.CUT
    assert (tie.rank_from, tie.rank_to) == (2, 4)
    assert set(tie.contestant_ids) == {'b', 'c', 'd'}
    assert not tie.decided
    assert [e.shared for e in ranking.entries] == [False, True, True, True]
    assert {e.rank for e in ranking.entries[1:]} == {2}
    assert q.blocking_ties([classified]) == (tie,)
    result = q.qualifiers_top_per_scope([ranking], 2)
    assert result == Err((tie,))


def test_cut_tie_resolved_by_decision_orders_qualifiers():
    ids, results = _tie_2_to_4()
    ranking = q.rank_round_robin(
        'group:1', ids, results, orders=[['c', 'b', 'd']]
    )

    assert _ranks(ranking) == [('a', 1), ('c', 2), ('b', 3), ('d', 4)]
    assert ranking.entries[1].decided_by == 'orga'
    assert ranking.ties[0].decided
    result = q.qualifiers_top_per_scope([ranking], 2)
    assert [x.contestant_id for x in result.unwrap()] == ['a', 'c']


def test_decision_not_covering_the_whole_tie_leaves_it_open():
    ids, results = _tie_2_to_4()

    ranking = q.rank_round_robin('group:1', ids, results, orders=[['c', 'b']])

    assert not ranking.ties[0].decided
    assert ranking.entries[1].shared


def _two_ties(first, second):
    """Two value ties: the `first` pair on 5, the `second` pair on 3."""
    values = {first[0]: 5, first[1]: 5, second[0]: 3, second[1]: 3}
    return values


def test_two_blocks_decide_only_their_own_ties():
    ranking = q.rank_by_value(
        'leaderboard',
        _two_ties('ac', 'bd'),
        higher_is_better=True,
        orders=[['a', 'c'], ['b', 'd']],
    )

    assert [t.decided for t in ranking.ties] == [True, True]
    assert _ranks(ranking) == [('a', 1), ('c', 2), ('b', 3), ('d', 4)]
    assert ranking.outdated == ()


def test_blocks_for_other_member_sets_leave_the_new_ties_open():
    ranking = q.rank_by_value(
        'leaderboard',
        _two_ties('cb', 'ad'),
        higher_is_better=True,
        orders=[['a', 'c'], ['b', 'd']],
    )

    assert [t.decided for t in ranking.ties] == [False, False]
    assert {frozenset(t.contestant_ids) for t in ranking.ties} == {
        frozenset('bc'),
        frozenset('ad'),
    }
    assert all(e.shared for e in ranking.entries)
    assert ranking.outdated == (('a', 'c'), ('b', 'd'))


def test_rank_by_value_block_decides_only_an_exact_tie():
    values = {'a': 5, 'b': 5, 'c': 5, 'd': 1}

    wider = q.rank_by_value(
        'leaderboard', values, higher_is_better=True, orders=[['a', 'b']]
    )
    exact = q.rank_by_value(
        'leaderboard',
        values,
        higher_is_better=True,
        orders=[['c', 'a', 'b']],
    )

    assert not wider.ties[0].decided
    assert wider.outdated == (('a', 'b'),)
    assert exact.ties[0].decided
    assert _ranks(exact) == [('c', 1), ('a', 2), ('b', 3), ('d', 4)]


def _tie_1_to_2():
    """a and b drew and both beat c."""
    return ['a', 'b', 'c'], [
        _m('a', 'b', 0, 0),
        _m('a', 'c', 1, 0),
        _m('b', 'c', 1, 0),
    ]


def test_group_winner_tie_classified_seeding():
    ids, results = _tie_1_to_2()
    ranking = q.rank_round_robin('group:1', ids, results)

    (tie,) = q.classify_ties(ranking, cut=2, plain_winner=False).ties

    assert tie.kind is q.TieKind.SEEDING
    assert (tie.rank_from, tie.rank_to) == (1, 2)
    assert isinstance(q.qualifiers_top_per_scope([ranking], 2), Err)
    (tie_q1,) = q.classify_ties(ranking, cut=1, plain_winner=False).ties
    assert tie_q1.kind is q.TieKind.CUT


def test_band_tie_q3_classified_seeding():
    values = {'a': 10, 'b': 7, 'c': 7, 'd': 3, 'e': 1}
    ranking = q.rank_by_value('group:1', values, higher_is_better=True)

    (tie,) = q.classify_ties(ranking, cut=3, plain_winner=False).ties

    assert tie.kind is q.TieKind.SEEDING
    assert (tie.rank_from, tie.rank_to) == (2, 3)
    assert isinstance(q.qualifiers_top_k(ranking, 3), Err)


def test_harmless_tie_below_cut():
    values = {'a': 10, 'b': 8, 'c': 5, 'd': 5}
    ranking = q.rank_by_value('leaderboard', values, higher_is_better=True)

    classified = q.classify_ties(ranking, cut=2, plain_winner=False)

    (tie,) = classified.ties
    assert tie.kind is q.TieKind.HARMLESS
    assert q.blocking_ties([classified]) == ()
    result = q.qualifiers_top_k(ranking, 2)
    assert [x.contestant_id for x in result.unwrap()] == ['a', 'b']


def test_rank_by_value_never_uses_time():
    parameters = inspect.signature(q.rank_by_value).parameters
    assert set(parameters) == {
        'scope',
        'values',
        'higher_is_better',
        'orders',
    }

    forward = q.rank_by_value(
        'leaderboard', {'a': 5, 'b': 5, 'c': 1}, higher_is_better=True
    )
    backward = q.rank_by_value(
        'leaderboard', {'c': 1, 'b': 5, 'a': 5}, higher_is_better=True
    )

    for ranking in (forward, backward):
        assert [e.shared for e in ranking.entries] == [True, True, False]
        assert [e.rank for e in ranking.entries] == [1, 1, 3]
        assert ranking.ties[0].decided is False
        assert {e.decided_by for e in ranking.entries} == {None}
    assert {e.value for e in forward.entries} == {5, 1}

    decided = q.rank_by_value(
        'leaderboard',
        {'a': 5, 'b': 5, 'c': 1},
        higher_is_better=True,
        orders=[['b', 'a']],
    )
    assert _ranks(decided) == [('b', 1), ('a', 2), ('c', 3)]


def test_rank_by_value_lower_is_better():
    ranking = q.rank_by_value(
        'leaderboard', {'a': 30, 'b': 10, 'c': 20}, higher_is_better=False
    )

    assert _ranks(ranking) == [('b', 1), ('c', 2), ('a', 3)]


def test_crossover_seed_list_winners_then_runners_up():
    rankings = []
    for prefix, margin in (('g1', 1), ('g2', 5), ('g3', 3)):
        ids, results = _three_group(prefix, margin)
        rankings.append(q.rank_round_robin(prefix, ids, results))
    # 4 points only, but by far the best difference
    rankings.append(
        q.rank_round_robin(
            'g4',
            ['g4x', 'g4y', 'g4z'],
            [
                _m('g4x', 'g4y', 9, 0),
                _m('g4x', 'g4z', 0, 0),
                _m('g4y', 'g4z', 1, 0),
            ],
        )
    )

    qualifiers = q.qualifiers_top_per_scope(rankings, 2).unwrap()
    seeds = q.crossover_seed_list(qualifiers).unwrap()

    assert [x.contestant_id for x in seeds] == [
        'g2x',
        'g3x',
        'g1x',
        'g4x',
        'g2y',
        'g3y',
        'g1y',
        'g4y',
    ]
    assert [x.rank for x in seeds] == [1, 1, 1, 1, 2, 2, 2, 2]


def _unequal_group_winners():
    """Winner a1 (4-group) and b1 (5-group), 9 points each.

    a1 won all 3 matches by one goal, b1 won 3 of 4 by five goals. Raw
    totals put b1 first (difference 14 against 3), per match a1 is.
    """
    a_ids = ['a1', 'a2', 'a3', 'a4']
    a_results = [
        _m('a1', 'a2', 1, 0),
        _m('a1', 'a3', 1, 0),
        _m('a1', 'a4', 1, 0),
        _m('a2', 'a3', 0, 0),
        _m('a2', 'a4', 0, 0),
        _m('a3', 'a4', 0, 0),
    ]
    b_ids = ['b1', 'b2', 'b3', 'b4', 'b5']
    b_results = [
        _m('b1', 'b2', 5, 0),
        _m('b1', 'b3', 5, 0),
        _m('b1', 'b4', 5, 0),
        _m('b1', 'b5', 0, 1),
        *(
            _m(x, y, 0, 0)
            for i, x in enumerate(b_ids[1:])
            for y in b_ids[1:][i + 1 :]
        ),
    ]
    rankings = [
        q.rank_round_robin('group:0', a_ids, a_results),
        q.rank_round_robin('group:1', b_ids, b_results),
    ]
    return q.qualifiers_top_per_scope(rankings, 1).unwrap()


def test_crossover_compares_points_per_match():
    qualifiers = _unequal_group_winners()
    assert [x.row.points for x in qualifiers] == [9, 9]
    assert [x.row.played for x in qualifiers] == [3, 4]

    seeds = q.crossover_seed_list(qualifiers).unwrap()

    assert [x.contestant_id for x in seeds] == ['a1', 'b1']


def _equal_groups():
    rankings = []
    for prefix in ('g1', 'g3'):
        ids, results = _three_group(prefix, 1)
        rankings.append(q.rank_round_robin(prefix, ids, results))
    return q.qualifiers_top_per_scope(rankings, 2).unwrap()


def test_qualifiers_take_every_entry_when_group_smaller_than_q():
    ranking = q.rank_by_value(
        'leaderboard', {'a': 2, 'b': 1}, higher_is_better=True
    )

    assert len(q.qualifiers_top_k(ranking, 5).unwrap()) == 2


def _origin(layout_groups):
    return dict(layout_groups)


def test_separate_same_group_finds_swap():
    seeds = [f's{i}' for i in range(1, 9)]
    layout = tuple(seeds[i] for i in _standard_seed_order(8))
    assert layout == ('s1', 's8', 's4', 's5', 's2', 's7', 's3', 's6')
    origin = {
        's1': 'A', 's8': 'A', 's4': 'D', 's5': 'D',
        's2': 'B', 's7': 'C', 's3': 'C', 's6': 'B',
    }  # fmt: skip

    assert q.same_group_matches(layout, origin) == [1, 2]

    result = q.separate_same_group(layout, origin)

    assert result is not None
    new_layout, swaps = result
    assert swaps == [(1, 3)]
    assert new_layout == ('s1', 's5', 's4', 's8', 's2', 's7', 's3', 's6')
    assert q.same_group_matches(new_layout, origin) == []


def test_separate_same_group_leaves_clean_layout_alone():
    layout = ('s1', 's4', 's2', 's3')
    origin = {'s1': 'A', 's2': 'B', 's3': 'A', 's4': 'B'}

    assert q.separate_same_group(layout, origin) == (layout, [])


def test_separate_same_group_gives_up_when_impossible():
    layout = ('s1', 's4', 's2', 's3')
    origin = dict.fromkeys(layout, 'A')

    assert q.separate_same_group(layout, origin) is None


def test_separate_same_group_never_moves_a_bye():
    layout = ('s1', 's2', 's3', None)
    origin = {'s1': 'A', 's2': 'A', 's3': 'B'}

    assert q.same_group_matches(layout, origin) == [1]
    assert q.separate_same_group(layout, origin) is None


def test_plain_round_robin_winner_tie():
    ids, results = _tie_1_to_2()
    ranking = q.rank_round_robin('plain', ids, results)

    result = q.plain_round_robin_winner(ranking)

    assert isinstance(result, Err)
    tie = result.unwrap_err()
    assert tie.kind is q.TieKind.WINNER
    assert set(tie.contestant_ids) == {'a', 'b'}

    decided = q.rank_round_robin('plain', ids, results, orders=[['b', 'a']])
    assert q.plain_round_robin_winner(decided) == Ok('b')


def test_plain_round_robin_winner_unambiguous_and_lower_tie_ignored():
    ids, results = _tie_2_to_4()

    ranking = q.rank_round_robin('plain', ids, results)

    assert q.plain_round_robin_winner(ranking) == Ok('a')


def test_plain_round_robin_winner_needs_entries():
    empty = q.rank_round_robin('plain', [], [])

    with pytest.raises(ValueError):
        q.plain_round_robin_winner(empty)


def test_crossover_exact_cross_group_tie_is_a_seeding_tie():
    qualifiers = _equal_groups()

    ranking = q.rank_crossover(qualifiers)

    assert ranking.scope == q.CROSSOVER_SCOPE == 'crossover'
    assert [
        (t.contestant_ids, t.rank_from, t.rank_to, t.decided, t.kind)
        for t in ranking.ties
    ] == [
        (('g1x', 'g3x'), 1, 2, False, q.TieKind.SEEDING),
        (('g1y', 'g3y'), 3, 4, False, q.TieKind.SEEDING),
    ]
    assert len(q.blocking_ties([ranking])) == 2
    blockers = q.crossover_seed_list(qualifiers).unwrap_err()
    assert [b.contestant_ids for b in blockers] == [
        ('g1x', 'g3x'),
        ('g1y', 'g3y'),
    ]


def test_crossover_tie_decided_by_a_block_orders_the_seeds():
    qualifiers = _equal_groups()
    orders = [('g3x', 'g1x'), ('g1y', 'g3y')]

    ranking = q.rank_crossover(qualifiers, orders)
    seeds = q.crossover_seed_list(qualifiers, orders).unwrap()

    assert q.blocking_ties([ranking]) == ()
    assert [x.contestant_id for x in seeds] == ['g3x', 'g1x', 'g1y', 'g3y']
    assert [(e.contestant_id, e.rank) for e in ranking.entries] == [
        ('g3x', 1),
        ('g1x', 2),
        ('g1y', 3),
        ('g3y', 4),
    ]
    assert ranking.outdated == ()


def test_crossover_block_for_other_members_leaves_the_tie_open():
    qualifiers = _equal_groups()

    ranking = q.rank_crossover(qualifiers, [('g3x', 'g1x', 'g1y')])

    assert len(q.blocking_ties([ranking])) == 2
    assert ranking.outdated == (('g3x', 'g1x', 'g1y'),)


def test_exactly_two_qualifiers_need_no_crossover_decision():
    qualifiers = _equal_groups()
    final = tuple(x for x in qualifiers if x.rank == 1)
    assert len(final) == 2

    seeds = q.crossover_seed_list(final).unwrap()

    assert {x.contestant_id for x in seeds} == {'g1x', 'g3x'}
    assert q.crossover_is_exempt(final)
    assert not q.crossover_is_exempt(qualifiers)


def test_inactive_contestant_is_not_ranked_but_results_still_count():
    # r leads on points, but is no longer in the tournament.
    ids = ['r', 'a', 'b']
    results = [_m('r', 'a', 2, 0), _m('r', 'b', 2, 0), _m('a', 'b', 1, 0)]

    ranking = q.rank_round_robin('group:0', ids, results, active_ids={'a', 'b'})

    assert _ranks(ranking) == [('a', 1), ('b', 2)]
    assert ranking.entries[0].row.points == 3
    assert ranking.entries[0].row.lost == 1
    assert ranking.entries[1].row.points == 0


def test_inactive_contestant_without_active_ids_is_ranked():
    ids = ['r', 'a']
    results = [_m('r', 'a', 2, 0)]

    ranking = q.rank_round_robin('group:0', ids, results)

    assert _ranks(ranking) == [('r', 1), ('a', 2)]


def test_tie_with_an_inactive_contestant_disappears():
    # r and a tie on points and scores, and r beat a head-to-head.
    ids = ['r', 'a', 'b']
    results = [_m('r', 'a', 1, 0), _m('a', 'b', 1, 0), _m('b', 'r', 1, 0)]
    full = q.classify_ties(
        q.rank_round_robin('group:0', ids, results), cut=1, plain_winner=False
    )
    assert full.ties

    ranking = q.classify_ties(
        q.rank_round_robin('group:0', ids, results, active_ids={'a', 'b'}),
        cut=1,
        plain_winner=False,
    )

    assert _ranks(ranking) == [('a', 1), ('b', 2)]
    assert ranking.ties == ()
    assert q.blocking_ties([ranking]) == ()
    assert [
        x.contestant_id for x in q.qualifiers_top_k(ranking, 1).unwrap()
    ] == ['a']


def test_tie_among_active_contestants_stays_with_an_inactive_one_around():
    ids = ['r', 'a', 'b']
    results = [_m('r', 'a', 1, 0), _m('r', 'b', 1, 0)]

    ranking = q.rank_round_robin(
        'group:0', ids, results, orders=[['b', 'a']], active_ids={'a', 'b'}
    )

    assert _ranks(ranking) == [('b', 1), ('a', 2)]
    assert [e.decided_by for e in ranking.entries] == ['orga', 'orga']
    assert ranking.ties[0].decided


def test_a_block_naming_an_inactive_contestant_still_orders_the_rest():
    ids = ['r', 'a', 'b']

    ranking = q.rank_round_robin(
        'group:0', ids, [], orders=[['b', 'r', 'a']], active_ids={'a', 'b'}
    )

    assert _ranks(ranking) == [('b', 1), ('a', 2)]
    assert ranking.ties[0].decided
    assert ranking.outdated == ()


def test_plain_round_robin_winner_skips_an_inactive_leader():
    ids = ['r', 'a', 'b']
    results = [_m('r', 'a', 2, 0), _m('r', 'b', 2, 0), _m('a', 'b', 1, 0)]

    ranking = q.rank_round_robin('plain', ids, results, active_ids={'a', 'b'})

    assert q.plain_round_robin_winner(ranking) == Ok('a')
