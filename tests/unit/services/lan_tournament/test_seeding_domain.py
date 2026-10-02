from dataclasses import replace

import pytest

from byceps.services.lan_tournament import (
    tournament_seeding_domain_service as s,
)
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.seed_code import draw_shuffle
from byceps.services.lan_tournament.tournament_domain_service import (
    snake_seed_groups,
)


SE = SeedingFormat.SINGLE_ELIMINATION
DE = SeedingFormat.DOUBLE_ELIMINATION
RR = SeedingFormat.ROUND_ROBIN
FFA = SeedingFormat.FREE_FOR_ALL


def _ids(n):
    return [f'c{i:02d}' for i in range(n)]


def test_derive_layout_se_matches_standard_order():
    seeds = _ids(6)

    layout = s.derive_layout(SE, seeds, 0)

    assert layout == (
        'c00', None, 'c03', 'c04', 'c01', None, 'c02', 'c05',
    )  # fmt: skip
    assert s.derive_layout(SE, _ids(8), 0) == tuple(
        _ids(8)[i] for i in (0, 7, 3, 4, 1, 6, 2, 5)
    )


def test_derive_layout_se_single_contestant_pair_has_two_slots():
    assert s.derive_layout(SE, _ids(2), 0) == ('c00', 'c01')


# fmt: off
@pytest.mark.parametrize(
    ('n', 'size_max'),
    [
        (8, 4),
        (9, 4),
        (10, 4),
        (13, 4),
        (16, 8),
        (7, 3),
    ],
)
# fmt: on
def test_derive_layout_snake_groups_match_snake_seed_groups(n, size_max):
    seeds = _ids(n)
    expected_groups = snake_seed_groups(seeds, 2, size_max).unwrap()

    layout = s.derive_layout(FFA, seeds, size_max)

    assert layout == tuple(cid for g in expected_groups for cid in g)
    assert s.group_sizes(FFA, n, size_max) == [len(g) for g in expected_groups]
    # RR with the same group count produces the same serpentine
    assert s.derive_layout(RR, seeds, len(expected_groups)) == layout


# fmt: off
@pytest.mark.parametrize(
    ('fmt', 'n', 'param', 'expected'),
    [
        (SE, 5, 0, 8),
        (SE, 1, 0, 2),
        (DE, 8, 0, 8),
        (RR, 7, 2, 7),
        (FFA, 9, 4, 9),
    ],
)
# fmt: on
def test_layout_length(fmt, n, param, expected):
    assert s.layout_length(fmt, n) == expected


# fmt: off
@pytest.mark.parametrize(
    ('fmt', 'n', 'param', 'groups'),
    [
        (RR, 8, 3, 3),
        (RR, 5, 4, 2),
        (RR, 1, 4, 1),
        (FFA, 9, 4, 3),
        (FFA, 8, 1, 4),
        (SE, 8, 0, 1),
    ],
)
# fmt: on
def test_group_count(fmt, n, param, groups):
    assert s.group_count(fmt, n, param) == groups


def test_pure_draw_is_reproducible_for_seed():
    roster = tuple(_ids(12))
    tiers = tuple(i % 3 for i in range(12))

    first = s.pure_draw(roster, tiers, 3, 4242)
    again = s.pure_draw(roster, tiers, 3, 4242)
    other = s.pure_draw(roster, tiers, 3, 4243)

    assert first == again
    assert first != other
    assert sorted(first) == list(roster)
    assert [tiers[roster.index(cid)] for cid in first] == sorted(tiers)


def test_pure_draw_single_tier_equals_draw_shuffle():
    roster = tuple(_ids(10))

    assert s.pure_draw(roster, (0,) * 10, 1, 12345) == draw_shuffle(
        roster, 12345
    )


def test_new_draw_seed_fits_32_bits():
    assert all(0 <= s.new_draw_seed() < 2**32 for _ in range(20))


# fmt: off
@pytest.mark.parametrize(
    ('n', 'tier_count', 'expected'),
    [
        (7, 3, (0, 0, 0, 1, 1, 2, 2)),
        (8, 4, (0, 0, 1, 1, 2, 2, 3, 3)),
        (5, 1, (0, 0, 0, 0, 0)),
        (2, 3, (0, 1)),
    ],
)
# fmt: on
def test_split_tiers_evenly_remainder_goes_to_first_tiers(
    n, tier_count, expected
):
    assert s.split_tiers_evenly(_ids(n), tier_count) == expected


def _tiered_state(n=8, tier_count=2, fmt=FFA, param=4):
    return s.initial_state(
        fmt, param, _ids(n), tier_count=tier_count, draw_seed=99
    )


def test_initial_state_ffa_splits_the_single_tier_draw():
    state = _tiered_state()

    assert state.seed_list == draw_shuffle(_ids(8), 99)
    assert [
        state.tiers[state.roster.index(cid)] for cid in state.seed_list
    ] == [0, 0, 0, 0, 1, 1, 1, 1]
    assert state.layout == s.derive_layout(FFA, state.seed_list, 4)
    assert s.is_pure_draw(state)
    assert s.fix_count(state) == 0


@pytest.mark.parametrize('fmt', [SE, DE, RR])
def test_initial_state_ignores_tier_count_outside_ffa(fmt):
    state = s.initial_state(
        fmt, 2 if fmt is RR else 0, _ids(8), tier_count=4, draw_seed=1
    )

    assert state.tier_count == 1
    assert set(state.tiers) == {0}


def test_initial_state_prefill_keeps_given_order_and_tiers():
    seeds = tuple(reversed(_ids(6)))
    tiers = (0, 0, 0, 1, 1, 1)

    state = s.initial_state(
        FFA, 3, _ids(6), tier_count=2, draw_seed=5, tiers=tiers,
        seed_list=seeds,
    )  # fmt: skip

    assert state.seed_list == seeds
    assert state.tiers == tiers
    assert not s.is_pure_draw(state)


def test_initial_state_with_tiers_only_draws_inside_tiers():
    tiers = (0, 1, 0, 1, 0, 1)

    state = s.initial_state(
        FFA, 3, _ids(6), tier_count=2, draw_seed=5, tiers=tiers
    )

    assert {state.seed_list[0], state.seed_list[1], state.seed_list[2]} == {
        'c00', 'c02', 'c04',
    }  # fmt: skip
    assert s.is_pure_draw(state)


def test_initial_state_rejects_bad_input():
    with pytest.raises(ValueError):
        s.initial_state(FFA, 4, _ids(4), tier_count=9, draw_seed=1)
    with pytest.raises(ValueError):
        s.initial_state(
            FFA, 4, _ids(4), tier_count=2, draw_seed=1, tiers=(0, 1, 2, 0)
        )
    with pytest.raises(ValueError):
        s.initial_state(SE, 0, ['a', 'a'], tier_count=1, draw_seed=1)


def test_move_to_tier_resets_fixes():
    state = _tiered_state()
    fixed = s.swap_slots(state, 0, 5)
    assert s.fix_count(fixed) == 1
    mover = state.seed_list[0]

    moved = s.move_to_tier(fixed, mover, 1)

    assert s.fix_count(moved) == 0
    assert moved.layout == s.derive_layout(FFA, moved.seed_list, 4)
    assert moved.tiers[moved.roster.index(mover)] == 1
    assert moved.seed_list[-1] == mover
    assert sorted(moved.seed_list) == sorted(state.seed_list)
    assert not s.is_pure_draw(moved)


def test_move_to_tier_places_relative_to_reference():
    state = _tiered_state()
    mover = state.seed_list[0]
    ref = state.seed_list[5]

    before = s.move_to_tier(state, mover, 1, ref_id=ref)
    after = s.move_to_tier(state, mover, 1, ref_id=ref, after=True)

    assert before.seed_list.index(mover) + 1 == before.seed_list.index(ref)
    assert after.seed_list.index(mover) == after.seed_list.index(ref) + 1


def test_move_to_tier_keeps_seed_list_tier_sorted_when_moving_up():
    state = _tiered_state()
    mover = state.seed_list[7]

    moved = s.move_to_tier(state, mover, 0)

    tiers_in_order = [
        moved.tiers[moved.roster.index(cid)] for cid in moved.seed_list
    ]
    assert tiers_in_order == sorted(tiers_in_order)
    assert moved.seed_list.index(mover) == 4


def test_move_to_tier_rejects_bad_input():
    state = _tiered_state()
    with pytest.raises(ValueError):
        s.move_to_tier(state, 'nobody', 0)
    with pytest.raises(ValueError):
        s.move_to_tier(state, state.seed_list[0], 2)
    with pytest.raises(ValueError):
        s.move_to_tier(
            state, state.seed_list[0], 0, ref_id=state.seed_list[7]
        )


def test_swap_slots_keeps_other_fixes():
    state = _tiered_state()
    once = s.swap_slots(state, 0, 5)
    twice = s.swap_slots(once, 2, 7)

    assert s.fix_count(once) == 1
    assert s.fix_count(twice) == 2
    assert twice.layout[0] == state.layout[5]
    assert twice.layout[5] == state.layout[0]
    assert twice.layout[2] == state.layout[7]
    assert twice.seed_list == state.seed_list
    with pytest.raises(ValueError):
        s.swap_slots(state, 0, 8)


def test_set_tier_count_recuts_bands_and_resets_fixes():
    state = s.swap_slots(_tiered_state(), 0, 5)

    cut = s.set_tier_count(state, 4)

    assert cut.tier_count == 4
    assert cut.seed_list == state.seed_list
    assert sorted(cut.tiers) == [0, 0, 1, 1, 2, 2, 3, 3]
    assert s.fix_count(cut) == 0
    with pytest.raises(ValueError):
        s.set_tier_count(state, 0)


def test_redraw_keeps_tiers_and_resets_fixes():
    state = s.swap_slots(_tiered_state(), 0, 5)

    drawn = s.redraw(state, 7)

    assert drawn.draw_seed == 7
    assert drawn.tiers == state.tiers
    assert s.fix_count(drawn) == 0
    assert s.is_pure_draw(drawn)
    assert set(drawn.seed_list[:4]) == set(state.seed_list[:4])


def test_reset_fixes_restores_derived_layout():
    state = s.swap_slots(_tiered_state(), 0, 5)

    assert s.reset_fixes(state).layout == s.derive_layout(
        FFA, state.seed_list, 4
    )


def test_reseed_for_roster_drops_leaver_appends_joiner():
    state = _tiered_state()
    leaver = state.seed_list[1]
    roster = [cid for cid in _ids(8) if cid != leaver] + ['c99']

    reseeded = s.reseed_for_roster(s.swap_slots(state, 0, 5), roster)

    assert reseeded.roster == tuple(sorted(roster))
    assert leaver not in reseeded.seed_list
    assert reseeded.seed_list[-1] == 'c99'
    assert reseeded.seed_list[:-1] == tuple(
        cid for cid in state.seed_list if cid != leaver
    )
    assert reseeded.tiers[reseeded.roster.index('c99')] == 1
    for cid in reseeded.roster[:-1]:
        assert (
            reseeded.tiers[reseeded.roster.index(cid)]
            == state.tiers[state.roster.index(cid)]
        )
    assert s.fix_count(reseeded) == 0
    assert reseeded.draw_seed == state.draw_seed


def test_layout_problems_two_byes():
    # 5 contestants in an 8 bracket: byes face seeds 1-3, none doubled
    state = s.initial_state(SE, 0, _ids(5), tier_count=1, draw_seed=1)
    assert s.layout_problems(state) == []

    # move the bye of match 6 next to the bye of match 1
    broken = s.swap_slots(state, 0, 5)
    assert broken.layout[:2] == (None, None)

    assert s.layout_problems(broken) == [s.PROBLEM_TWO_BYES]
    assert s.two_bye_matches(broken) == [1]


def test_layout_problems_format_rules():
    de3 = s.initial_state(DE, 0, _ids(3), tier_count=1, draw_seed=1)
    rr_one_group = s.initial_state(RR, 1, _ids(6), tier_count=1, draw_seed=1)
    ffa_single = s.initial_state(FFA, 4, _ids(1), tier_count=1, draw_seed=1)

    assert s.PROBLEM_DE_SIZE in s.layout_problems(de3)
    assert s.layout_problems(rr_one_group) == [s.PROBLEM_FEW_GROUPS]
    assert s.PROBLEM_SMALL_GROUP in s.layout_problems(ffa_single)
    ok = s.initial_state(RR, 2, _ids(6), tier_count=1, draw_seed=1)
    assert s.layout_problems(ok) == []


def test_balance_is_none_outside_ffa():
    state = s.initial_state(RR, 2, _ids(6), tier_count=1, draw_seed=1)

    assert s.balance(state) is None


def test_balance_flags_cluster():
    # 8 in lobbies of 4: two lobbies, tiers of 4 -> at most 2 per lobby
    state = s.initial_state(
        FFA, 4, _ids(8), tier_count=2, draw_seed=1,
        tiers=(0, 0, 0, 0, 1, 1, 1, 1),
        seed_list=tuple(_ids(8)),
    )  # fmt: skip
    clustered = replace(state, layout=tuple(_ids(8)))

    result = s.balance(clustered)

    assert result is not None
    assert result.counts == ((4, 0), (0, 4))
    assert result.allowed == (2, 2)
    assert result.over == ((0, 0, 4), (1, 1, 4))


def test_balance_clean_for_snake_layout():
    state = s.initial_state(
        FFA, 4, _ids(8), tier_count=2, draw_seed=1,
        tiers=(0, 0, 0, 0, 1, 1, 1, 1),
        seed_list=tuple(_ids(8)),
    )  # fmt: skip

    result = s.balance(state)

    assert result is not None
    assert result.counts == ((2, 2), (2, 2))
    assert result.over == ()


def test_balance_allowed_rounds_up_for_uneven_tiers():
    state = s.initial_state(
        FFA, 4, _ids(9), tier_count=2, draw_seed=1,
        tiers=(0, 0, 0, 0, 0, 1, 1, 1, 1),
        seed_list=tuple(_ids(9)),
    )  # fmt: skip

    result = s.balance(state)

    assert result is not None
    assert result.allowed == (2, 2)
    assert result.over == ()


# fmt: off
@pytest.mark.parametrize(
    ('n', 'param', 'groups', 'per_group', 'de', 'expected'),
    [
        (7, 4, 4, 2, False, s.Shortfall(4, 3, 8, 6)),
        (5, 2, 2, 3, False, s.Shortfall(2, 2, 6, 5)),
        (6, 4, 4, 1, True, s.Shortfall(4, 3, 4, 3, True)),
        (8, 4, 4, 1, True, None),
        (16, 4, 4, 2, False, None),
    ],
)
# fmt: on
def test_group_shortfall(n, param, groups, per_group, de, expected):
    assert (
        s.group_shortfall(n, param, groups, per_group, double_elimination=de)
        == expected
    )


# fmt: off
@pytest.mark.parametrize(
    ('qualifiers', 'configured', 'de', 'expected'),
    [
        (5, 8, False, s.Shortfall(None, None, 8, 5)),
        (8, 8, False, None),
        (3, 4, True, s.Shortfall(None, None, 4, 3, True)),
        (4, 4, True, None),
        (1, 4, True, s.Shortfall(None, None, 4, 1)),
    ],
)
# fmt: on
def test_qualifier_shortfall(qualifiers, configured, de, expected):
    assert (
        s.qualifier_shortfall(qualifiers, configured, double_elimination=de)
        == expected
    )
