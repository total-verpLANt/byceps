"""
byceps.services.lan_tournament.tournament_seeding_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import ceil
import secrets

from .models.seeding import SeedingFormat, SeedingState
from .seed_code import (
    canonical_roster,
    canonical_swaps,
    draw_shuffle,
    mulberry32,
)
from .tournament_domain_service import _standard_seed_order


_ELIMINATION_FORMATS = (
    SeedingFormat.SINGLE_ELIMINATION,
    SeedingFormat.DOUBLE_ELIMINATION,
)

MAX_TIER_COUNT = 8

PROBLEM_TWO_BYES = (
    'Match %(n)s has two byes. A match needs at least one contestant.'
)
PROBLEM_SMALL_GROUP = 'Every group needs at least two contestants.'
PROBLEM_FEW_GROUPS = 'Round robin groups need at least two groups.'
PROBLEM_DE_SIZE = 'Double elimination needs at least four contestants.'

MIN_DOUBLE_ELIMINATION = 4


@dataclass(frozen=True)
class Balance:
    counts: tuple[tuple[int, ...], ...]  # [group][tier]
    allowed: tuple[int, ...]  # [tier]: most members one group should hold
    over: tuple[tuple[int, int, int], ...]  # (group, tier, count)


@dataclass(frozen=True)
class Shortfall:
    """Where the roster gives less than the playoffs are configured for.

    `groups` and `configured_groups` are `None` outside a group stage.
    `knockout_fallback` marks a 1v1 double elimination that runs as single
    elimination because fewer than four contestants qualify.
    """

    configured_groups: int | None
    groups: int | None
    configured_qualifiers: int
    qualifiers: int
    knockout_fallback: bool = False


# -------------------------------------------------------------------- #
# geometry


def _next_pow2(n: int) -> int:
    size = 1
    while size < n:
        size *= 2
    return size


def layout_length(fmt: SeedingFormat, n: int) -> int:
    """Return the number of layout slots for `n` contestants."""
    if fmt in _ELIMINATION_FORMATS:
        return _next_pow2(max(n, 2))
    return n


def group_count(fmt: SeedingFormat, n: int, param: int) -> int:
    """Return the number of groups (lobbies) the layout is cut into."""
    if fmt is SeedingFormat.ROUND_ROBIN:
        return max(1, min(param, n // 2))
    if fmt is SeedingFormat.FREE_FOR_ALL:
        return max(1, ceil(n / max(2, param)))
    return 1


def _snake_groups[T](items: Sequence[T], groups: int) -> list[list[T]]:
    result: list[list[T]] = [[] for _ in range(groups)]
    for index, item in enumerate(items):
        column = index % groups
        if (index // groups) % 2 == 1:
            column = groups - 1 - column
        result[column].append(item)
    return result


def group_sizes(fmt: SeedingFormat, n: int, param: int) -> list[int]:
    """Return the size of each group, in layout order."""
    if fmt in _ELIMINATION_FORMATS:
        return [n]
    groups = _snake_groups(range(n), group_count(fmt, n, param))
    return [len(group) for group in groups]


def _knockout_fallback(qualifiers: int, *, double_elimination: bool) -> bool:
    return double_elimination and 2 <= qualifiers < MIN_DOUBLE_ELIMINATION


def group_shortfall(
    n: int,
    param: int,
    configured_groups: int,
    per_group: int,
    *,
    double_elimination: bool,
) -> Shortfall | None:
    """Return what a round robin group stage of `n` gives short, if anything."""
    sizes = group_sizes(SeedingFormat.ROUND_ROBIN, n, param)
    qualifiers = sum(min(per_group, size) for size in sizes)
    configured = configured_groups * per_group
    fallback = _knockout_fallback(
        qualifiers, double_elimination=double_elimination
    )
    if (
        len(sizes) == configured_groups
        and qualifiers == configured
        and not fallback
    ):
        return None
    return Shortfall(
        configured_groups, len(sizes), configured, qualifiers, fallback
    )


def qualifier_shortfall(
    qualifiers: int, configured: int, *, double_elimination: bool
) -> Shortfall | None:
    """Return what `qualifiers` of `configured` places give short, if anything."""
    fallback = _knockout_fallback(
        qualifiers, double_elimination=double_elimination
    )
    if qualifiers >= configured and not fallback:
        return None
    return Shortfall(None, None, configured, qualifiers, fallback)


def derive_layout(
    fmt: SeedingFormat, seed_list: Sequence[str], param: int
) -> tuple[str | None, ...]:
    """Return the layout the seed order produces before any manual fixes."""
    n = len(seed_list)
    if fmt in _ELIMINATION_FORMATS:
        size = layout_length(fmt, n)
        padded: list[str | None] = [*seed_list, *([None] * (size - n))]
        return tuple(padded[seed] for seed in _standard_seed_order(size))

    groups = _snake_groups(seed_list, group_count(fmt, n, param))
    return tuple(cid for group in groups for cid in group)


# -------------------------------------------------------------------- #
# draw


def new_draw_seed() -> int:
    """Return a fresh 32-bit draw seed."""
    return secrets.randbits(32)


def pure_draw(
    roster: Sequence[str],
    tiers: Sequence[int],
    tier_count: int,
    draw_seed: int,
) -> tuple[str, ...]:
    """Return the seed order: a shuffle inside each tier, tiers in order."""
    next_float = mulberry32(draw_seed)
    result: list[str] = []
    for tier in range(tier_count):
        members = [
            cid for cid, t in zip(roster, tiers, strict=True) if t == tier
        ]
        for i in range(len(members) - 1, 0, -1):
            j = int(next_float() * (i + 1))
            members[i], members[j] = members[j], members[i]
        result.extend(members)
    return tuple(result)


def split_tiers_evenly(
    seed_list: Sequence[str], tier_count: int
) -> tuple[int, ...]:
    """Return the tier of each seed position, in equal bands.

    The first bands take the remainder.
    """
    n = len(seed_list)
    base, extra = divmod(n, tier_count)
    tiers: list[int] = []
    for tier in range(tier_count):
        tiers.extend([tier] * (base + (1 if tier < extra else 0)))
    return tuple(tiers)


def _check_tier_count(tier_count: int) -> None:
    if not 1 <= tier_count <= MAX_TIER_COUNT:
        raise ValueError(f'tier_count must be 1..{MAX_TIER_COUNT}')


def _tiers_by_roster(
    roster: Sequence[str],
    seed_list: Sequence[str],
    seed_tiers: Sequence[int],
) -> tuple[int, ...]:
    by_id = dict(zip(seed_list, seed_tiers, strict=True))
    return tuple(by_id[cid] for cid in roster)


def initial_state(
    fmt: SeedingFormat,
    param: int,
    roster_ids: Sequence[str],
    *,
    tier_count: int,
    draw_seed: int,
    tiers: Sequence[int] | None = None,
    seed_list: Sequence[str] | None = None,
) -> SeedingState:
    """Return the first draft: a pure draw with the layout derived from it.

    FFA draws with one tier and cuts the result into `tier_count` equal
    bands. The other formats have no tiers.
    """
    roster = canonical_roster(roster_ids)
    if len(set(roster)) != len(roster):
        raise ValueError('roster contains duplicate IDs')
    if fmt is not SeedingFormat.FREE_FOR_ALL:
        tier_count = 1
    _check_tier_count(tier_count)

    if seed_list is not None and set(seed_list) != set(roster):
        raise ValueError('seed_list is not a permutation of the roster')
    if tiers is not None:
        if len(tiers) != len(roster):
            raise ValueError('tiers are not aligned with the roster')
        if any(not 0 <= t < tier_count for t in tiers):
            raise ValueError('tier out of range')

    if tiers is None:
        if seed_list is None:
            seed_list = draw_shuffle(roster, draw_seed)
        tiers = _tiers_by_roster(
            roster, seed_list, split_tiers_evenly(seed_list, tier_count)
        )
    elif seed_list is None:
        seed_list = pure_draw(roster, tiers, tier_count, draw_seed)

    seed_tuple = tuple(seed_list)
    return SeedingState(
        format=fmt,
        param=param,
        tier_count=tier_count,
        roster=roster,
        tiers=tuple(tiers),
        seed_list=seed_tuple,
        layout=derive_layout(fmt, seed_tuple, param),
        draw_seed=draw_seed,
    )


# -------------------------------------------------------------------- #
# moves


def _with_seed_list(
    state: SeedingState,
    seed_list: Sequence[str],
    *,
    tiers: Sequence[int] | None = None,
    tier_count: int | None = None,
    roster: Sequence[str] | None = None,
    draw_seed: int | None = None,
) -> SeedingState:
    """Return the state with a new seed list, layout re-derived, fixes gone."""
    seed_tuple = tuple(seed_list)
    return replace(
        state,
        seed_list=seed_tuple,
        layout=derive_layout(state.format, seed_tuple, state.param),
        tiers=state.tiers if tiers is None else tuple(tiers),
        tier_count=state.tier_count if tier_count is None else tier_count,
        roster=state.roster if roster is None else tuple(roster),
        draw_seed=state.draw_seed if draw_seed is None else draw_seed,
    )


def swap_slots(state: SeedingState, p: int, q: int) -> SeedingState:
    """Swap two layout slots; every other fix stays."""
    size = len(state.layout)
    if not (0 <= p < size and 0 <= q < size):
        raise ValueError('slot index out of range')
    layout = list(state.layout)
    layout[p], layout[q] = layout[q], layout[p]
    return replace(state, layout=tuple(layout))


def move_to_tier(
    state: SeedingState,
    contestant_id: str,
    tier: int,
    *,
    ref_id: str | None = None,
    after: bool = False,
) -> SeedingState:
    """Move a contestant into `tier`, next to `ref_id` or at its end."""
    if contestant_id not in state.seed_list:
        raise ValueError('unknown contestant')
    if not 0 <= tier < state.tier_count:
        raise ValueError('tier out of range')

    tier_of = dict(zip(state.roster, state.tiers, strict=True))
    if ref_id is not None and (
        ref_id == contestant_id or tier_of.get(ref_id) != tier
    ):
        raise ValueError('reference must be another member of the tier')
    tier_of[contestant_id] = tier

    rest = [cid for cid in state.seed_list if cid != contestant_id]
    if ref_id is not None:
        at = rest.index(ref_id) + (1 if after else 0)
    else:
        at = sum(1 for cid in rest if tier_of[cid] <= tier)
    rest.insert(at, contestant_id)

    return _with_seed_list(
        state, rest, tiers=[tier_of[cid] for cid in state.roster]
    )


def set_tier_count(state: SeedingState, tier_count: int) -> SeedingState:
    """Cut the current seed order into `tier_count` equal bands."""
    _check_tier_count(tier_count)
    tiers = _tiers_by_roster(
        state.roster,
        state.seed_list,
        split_tiers_evenly(state.seed_list, tier_count),
    )
    return _with_seed_list(
        state, state.seed_list, tiers=tiers, tier_count=tier_count
    )


def redraw(state: SeedingState, draw_seed: int) -> SeedingState:
    """Draw again inside the current tiers."""
    seed_list = pure_draw(
        state.roster, state.tiers, state.tier_count, draw_seed
    )
    return _with_seed_list(state, seed_list, draw_seed=draw_seed)


def reset_fixes(state: SeedingState) -> SeedingState:
    """Drop every manual swap; the layout follows the seed order again."""
    return _with_seed_list(state, state.seed_list)


def reseed_for_roster(
    state: SeedingState, roster_ids: Sequence[str]
) -> SeedingState:
    """Drop leavers; append new entrants to the end of the last tier."""
    roster = canonical_roster(roster_ids)
    if len(set(roster)) != len(roster):
        raise ValueError('roster contains duplicate IDs')
    current = set(roster)
    old_tiers = dict(zip(state.roster, state.tiers, strict=True))
    last_tier = state.tier_count - 1

    kept = [cid for cid in state.seed_list if cid in current]
    joiners = [cid for cid in roster if cid not in old_tiers]
    tier_of = {cid: old_tiers.get(cid, last_tier) for cid in roster}

    return _with_seed_list(
        state,
        [*kept, *joiners],
        tiers=[tier_of[cid] for cid in roster],
        roster=roster,
    )


# -------------------------------------------------------------------- #
# checks and readouts


def fix_count(state: SeedingState) -> int:
    """Return the number of manual swaps on top of the derived layout."""
    derived = derive_layout(state.format, state.seed_list, state.param)
    return len(canonical_swaps(derived, state.layout))


def is_pure_draw(state: SeedingState) -> bool:
    """Return whether the seed order is still what the draw produced."""
    if state.seed_list == pure_draw(
        state.roster, state.tiers, state.tier_count, state.draw_seed
    ):
        return True
    return (
        state.tier_count > 1
        and state.seed_list == draw_shuffle(state.roster, state.draw_seed)
        and state.tiers
        == _tiers_by_roster(
            state.roster,
            state.seed_list,
            split_tiers_evenly(state.seed_list, state.tier_count),
        )
    )


def two_bye_matches(state: SeedingState) -> list[int]:
    """Return the 1-based first-round matches whose two slots are byes."""
    if state.format not in _ELIMINATION_FORMATS:
        return []
    layout = state.layout
    return [
        k + 1
        for k in range(len(layout) // 2)
        if layout[2 * k] is None and layout[2 * k + 1] is None
    ]


def layout_problems(state: SeedingState) -> list[str]:
    """Return the msgids of every rule the layout breaks.

    One `PROBLEM_TWO_BYES` entry per offending match, in the order of
    `two_bye_matches`; the caller fills in `%(n)s`.
    """
    n = len(state.roster)
    fmt = state.format
    problems = [PROBLEM_TWO_BYES for _ in two_bye_matches(state)]

    if fmt is SeedingFormat.DOUBLE_ELIMINATION and n < MIN_DOUBLE_ELIMINATION:
        problems.append(PROBLEM_DE_SIZE)
    if fmt is SeedingFormat.ROUND_ROBIN and (
        group_count(fmt, n, state.param) < 2
    ):
        problems.append(PROBLEM_FEW_GROUPS)
    if fmt not in _ELIMINATION_FORMATS and any(
        size < 2 for size in group_sizes(fmt, n, state.param)
    ):
        problems.append(PROBLEM_SMALL_GROUP)
    return problems


def balance(state: SeedingState) -> Balance | None:
    """Return the tier spread over the lobbies (FFA only)."""
    if state.format is not SeedingFormat.FREE_FOR_ALL:
        return None

    n = len(state.layout)
    sizes = group_sizes(state.format, n, state.param)
    tier_of = dict(zip(state.roster, state.tiers, strict=True))
    tier_sizes = [0] * state.tier_count
    for tier in state.tiers:
        tier_sizes[tier] += 1
    allowed = tuple(ceil(size / len(sizes)) for size in tier_sizes)

    counts: list[tuple[int, ...]] = []
    start = 0
    for size in sizes:
        members = state.layout[start : start + size]
        start += size
        row = [0] * state.tier_count
        for cid in members:
            if cid is not None:
                row[tier_of[cid]] += 1
        counts.append(tuple(row))

    over = tuple(
        (group, tier, count)
        for group, row in enumerate(counts)
        for tier, count in enumerate(row)
        if count > allowed[tier]
    )
    return Balance(counts=tuple(counts), allowed=allowed, over=over)
