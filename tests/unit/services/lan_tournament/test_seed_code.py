# ruff: noqa: S311
from random import Random

import pytest

from byceps.services.lan_tournament import seed_code
from byceps.services.lan_tournament.models.seeding import (
    SeedingFormat,
    SeedingState,
)
from byceps.services.lan_tournament.seed_code import (
    apply_swaps,
    canonical_roster,
    canonical_swaps,
    decode_seed_code,
    draw_shuffle,
    encode_seed_code,
    format_seed_code,
    mulberry32,
    roster_fingerprint,
    state_from_code,
)
from byceps.util.result import Err, Ok


def _derive(fmt, seed_list, param):
    if fmt not in (
        SeedingFormat.SINGLE_ELIMINATION,
        SeedingFormat.DOUBLE_ELIMINATION,
    ):
        return tuple(seed_list)
    size = 1
    while size < len(seed_list):
        size *= 2
    padded = list(seed_list) + [None] * (size - len(seed_list))
    return tuple(padded[::2] + padded[1::2])


def _make_state(fmt, n, rng, *, swap_count=0, tier_count=1):
    roster = canonical_roster(
        f'c-{rng.getrandbits(40):x}-{i}' for i in range(n)
    )
    seed_list = tuple(rng.sample(roster, n))
    tiers = tuple(rng.randrange(tier_count) for _ in roster)
    param = (
        0
        if fmt
        in (SeedingFormat.SINGLE_ELIMINATION, SeedingFormat.DOUBLE_ELIMINATION)
        else 4
    )
    derived = list(_derive(fmt, seed_list, param))
    for _ in range(swap_count):
        i, j = rng.sample(range(len(derived)), 2)
        derived[i], derived[j] = derived[j], derived[i]
    state = SeedingState(
        format=fmt,
        param=param,
        tier_count=tier_count,
        roster=roster,
        tiers=tiers,
        seed_list=seed_list,
        layout=tuple(derived),
        draw_seed=rng.getrandbits(32),
    )
    return state, _derive(fmt, seed_list, param)


def _round_trip(state, derived):
    code = encode_seed_code(state, derived)
    match decode_seed_code(code):
        case Ok(decoded):
            pass
        case Err(e):
            pytest.fail(e)
    match state_from_code(decoded, reversed(state.roster), _derive):
        case Ok(restored):
            return code, restored
        case Err(e):
            pytest.fail(e)


# fmt: off
@pytest.mark.parametrize('fmt', list(SeedingFormat))
@pytest.mark.parametrize('n', range(2, 65))
# fmt: on
def test_seed_code_round_trip_all_formats(fmt, n):
    rng = Random(f'{fmt.value}-{n}')
    tier_count = 1 + n % 4
    state, derived = _make_state(
        fmt, n, rng, swap_count=n % 4, tier_count=tier_count
    )

    code, restored = _round_trip(state, derived)

    assert code.startswith('S')
    assert restored == state


def test_seed_code_round_trip_without_swaps_or_tiers():
    state, derived = _make_state(SeedingFormat.ROUND_ROBIN, 8, Random(1))

    _, restored = _round_trip(state, derived)

    assert restored == state


def test_seed_code_typo_is_caught():
    state, derived = _make_state(
        SeedingFormat.SINGLE_ELIMINATION, 20, Random(7), swap_count=3
    )
    code = encode_seed_code(state, derived)

    for position in range(1, len(code)):
        original = code[position]
        for replacement in seed_code._ALPHABET:
            if replacement == original:
                continue
            typo = code[:position] + replacement + code[position + 1 :]
            assert decode_seed_code(typo) == Err(
                'The check character does not match. Check for a typo.'
            ), (position, replacement)


def test_seed_code_normalizes_case_and_confusables():
    state, derived = _make_state(
        SeedingFormat.DOUBLE_ELIMINATION, 12, Random(3), swap_count=2
    )
    code = encode_seed_code(state, derived)
    expected = decode_seed_code(code)
    assert expected.is_ok()

    mangled = format_seed_code(code).lower().replace('0', 'o').replace('1', 'l')
    mangled = mangled.replace('-', ' - ', 1)

    assert decode_seed_code(mangled) == expected
    assert decode_seed_code(code.replace('1', 'I')) == expected


def test_seed_code_other_roster_rejected():
    state, derived = _make_state(
        SeedingFormat.FREE_FOR_ALL, 10, Random(5), swap_count=1
    )
    decoded = decode_seed_code(encode_seed_code(state, derived)).unwrap()

    same_size_other = [f'other-{i}' for i in range(10)]
    smaller = state.roster[:-1]

    for ids in (same_size_other, smaller, [*state.roster, 'extra']):
        assert state_from_code(decoded, ids, _derive) == Err(
            'This code belongs to a different roster.'
        )


def test_seed_code_unknown_version_rejected():
    code = seed_code._encode_items(
        [(seed_code.SEED_CODE_VERSION + 1, 16), (0, 8), (0, 8)]
    )

    assert decode_seed_code(code) == Err('Unknown seed code version.')


def test_seed_code_extra_data_rejected():
    fields = [
        (seed_code.SEED_CODE_VERSION, 16),
        (0, 8),  # format
        (0, 8),  # tier count - 1
        (1, 1024),  # n - 1
        (0, 256),  # param
        (123, 2**32),  # fingerprint
        (456, 2**32),  # draw seed
        (1, 2),  # Lehmer digit
        (0, 1024),  # swap count
    ]

    assert decode_seed_code(seed_code._encode_items(fields)).is_ok()
    assert decode_seed_code(
        seed_code._encode_items([*fields, (5, 1024)])
    ) == Err('The seed code carries extra data.')


# fmt: off
@pytest.mark.parametrize('raw', ['', 'S', 'S0', 'X1234', 'S12U4', 'S12*4'])
# fmt: on
def test_seed_code_garbage_is_damaged(raw):
    assert decode_seed_code(raw) == Err('The seed code is damaged.')


def test_roster_fingerprint_is_order_independent():
    ids = ['b', 'a', 'd', 'c']

    assert roster_fingerprint(ids) == roster_fingerprint(sorted(ids))
    assert roster_fingerprint(ids) == roster_fingerprint(reversed(ids))
    assert roster_fingerprint(ids) != roster_fingerprint(['a', 'b', 'c', 'e'])


def test_roster_fingerprint_matches_fnv1a_vector():
    assert roster_fingerprint([]) == 0x811C9DC5
    assert roster_fingerprint(['a']) == 0xE40C292C


def test_canonical_swaps_treats_byes_as_interchangeable():
    derived = ['a', None, None, 'b']

    assert canonical_swaps(derived, ['a', None, None, 'b']) == []
    assert canonical_swaps(derived, ['a', None, 'b', None]) == [(2, 3)]
    assert canonical_swaps(['a', 'b', None, None], [None, 'b', 'a', None]) == [(0, 2)]


def test_canonical_swaps_apply_reproduces_layout():
    rng = Random(9)
    derived = ['a', 'b', 'c', None, 'd', None, 'e', 'f']
    for _ in range(50):
        layout = list(derived)
        rng.shuffle(layout)

        swaps = canonical_swaps(derived, layout)

        assert list(apply_swaps(derived, swaps)) == layout
        assert len(swaps) <= len(derived) - 1


def test_canonical_swaps_rejects_non_permutation():
    with pytest.raises(ValueError):
        canonical_swaps(['a', 'b'], ['a', 'c'])


def test_format_seed_code_groups_of_four():
    assert format_seed_code('S0123456789AB') == 'S012-3456-789A-B'
    assert format_seed_code('S012') == 'S012'


def test_mulberry32_is_pinned():
    next_float = mulberry32(12345)

    assert [int(next_float() * 2**32) for _ in range(3)] == [
        4207900869,
        1317490944,
        2079646450,
    ]
    assert draw_shuffle(range(10), 12345) == (6, 4, 8, 0, 1, 7, 5, 3, 2, 9)


def test_draw_shuffle_is_permutation_and_deterministic():
    items = [f'c{i}' for i in range(30)]

    assert sorted(draw_shuffle(items, 99)) == sorted(items)
    assert draw_shuffle(items, 99) == draw_shuffle(items, 99)
    assert draw_shuffle(items, 99) != draw_shuffle(items, 100)


def test_seed_code_length_stays_short():
    for n in (8, 20, 64):
        for swap_count in (0, 3):
            state, derived = _make_state(
                SeedingFormat.SINGLE_ELIMINATION, n, Random(n), swap_count=swap_count
            )
            assert len(encode_seed_code(state, derived)) <= 100


def _largest_legal_code():
    n = 1024
    rng = Random('largest')
    state, derived = _make_state(
        SeedingFormat.FREE_FOR_ALL, n, rng, tier_count=8
    )
    layout = (*derived[1:], *derived[:1])
    state = SeedingState(
        format=state.format,
        param=state.param,
        tier_count=state.tier_count,
        roster=state.roster,
        tiers=state.tiers,
        seed_list=state.seed_list,
        layout=layout,
        draw_seed=state.draw_seed,
    )
    return state, derived


# fmt: off
@pytest.mark.parametrize(
    'fmt',
    [SeedingFormat.SINGLE_ELIMINATION, SeedingFormat.FREE_FOR_ALL],
    ids=['se', 'ffa'],
)
# fmt: on
def test_seed_code_round_trips_1024_contestants(fmt):
    state, derived = _make_state(fmt, 1024, Random(1024), swap_count=5)

    _, restored = _round_trip(state, derived)

    assert restored == state


def test_seed_code_refuses_1025_contestants():
    state, derived = _make_state(
        SeedingFormat.FREE_FOR_ALL, 1025, Random(1025)
    )

    with pytest.raises(ValueError):
        encode_seed_code(state, derived)


def test_stored_n_minus_one_of_zero_is_damaged():
    fields = [
        (seed_code.SEED_CODE_VERSION, 16),
        (0, 8),  # format
        (0, 8),  # tier count - 1
        (0, 1024),  # n - 1
        (0, 256),  # param
        (123, 2**32),  # fingerprint
        (456, 2**32),  # draw seed
    ]

    assert decode_seed_code(seed_code._encode_items(fields)) == Err(
        'The seed code is damaged.'
    )


def test_largest_legal_code_round_trips_under_the_cap():
    state, derived = _largest_legal_code()

    code, restored = _round_trip(state, derived)

    assert restored == state
    assert len(format_seed_code(code)) < seed_code.MAX_CODE_LENGTH


def test_over_long_code_is_refused_before_the_fold(monkeypatch):
    folds = []
    real_fold = seed_code._fold_digits
    monkeypatch.setattr(
        seed_code,
        '_fold_digits',
        lambda digits: (folds.append(len(digits)), real_fold(digits))[1],
    )

    refused = decode_seed_code('S' + '2' * seed_code.MAX_CODE_LENGTH)

    assert refused == Err('The seed code is too long.')
    assert folds == []


def test_over_long_code_counts_hyphens_and_whitespace(monkeypatch):
    monkeypatch.setattr(
        seed_code,
        '_fold_digits',
        lambda digits: pytest.fail('fold reached'),
    )

    padded = 'S' + ' ' * seed_code.MAX_CODE_LENGTH

    assert decode_seed_code(padded) == Err('The seed code is too long.')


def test_ligature_padded_code_is_refused_after_normalising(monkeypatch):
    monkeypatch.setattr(
        seed_code,
        '_fold_digits',
        lambda digits: pytest.fail('fold reached'),
    )
    count = seed_code.MAX_CODE_LENGTH - 2
    digits = [seed_code._ALPHABET.index(c) for c in 'FF1' * count]
    check = seed_code._ALPHABET[seed_code._check_value(digits)]
    padded = 'S' + '\ufb03' * count + check

    assert len(padded) <= seed_code.MAX_CODE_LENGTH
    assert len(seed_code._normalize(padded)) > seed_code.MAX_CODE_LENGTH
    assert decode_seed_code(padded) == Err('The seed code is too long.')


def test_code_at_the_cap_is_decoded_not_refused():
    code = 'S' + '2' * (seed_code.MAX_CODE_LENGTH - 1)

    assert decode_seed_code(code) != Err('The seed code is too long.')
