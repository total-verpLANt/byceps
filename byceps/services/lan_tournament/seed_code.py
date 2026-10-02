"""
byceps.services.lan_tournament.seed_code
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Callable, Iterable, Sequence

from byceps.util.result import Err, Ok, Result

from .models.seeding import DecodedSeedCode, SeedingFormat, SeedingState


SEED_CODE_VERSION = 1

_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
_PREFIX = 'S'
_CONFUSABLES = str.maketrans({'O': '0', 'I': '1', 'L': '1'})

_FORMATS = tuple(SeedingFormat)

_VERSION_RADIX = 16
_FORMAT_RADIX = 8
_TIER_COUNT_RADIX = 8
_N_RADIX = 1024
_PARAM_RADIX = 256
MAX_PARAM = _PARAM_RADIX - 1
_U32_RADIX = 2**32
_SWAP_COUNT_RADIX = 1024
_SWAP_INDEX_RADIX = 1024

_ERR_CHECK = 'The check character does not match. Check for a typo.'
_ERR_VERSION = 'Unknown seed code version.'
_ERR_DAMAGED = 'The seed code is damaged.'
_ERR_EXTRA = 'The seed code carries extra data.'
_ERR_ROSTER = 'This code belongs to a different roster.'
_ERR_TOO_LONG = 'The seed code is too long.'

# The largest legal code (1024 contestants, 8 tiers, 1023 swaps) has about
# 6.5k characters, about 8.1k grouped in blocks of four; the cap leaves
# room for whitespace around the blocks.
MAX_CODE_LENGTH = 16_384


def canonical_roster(ids: Iterable[str]) -> tuple[str, ...]:
    """Return the contestant IDs in canonical (sorted) order."""
    return tuple(sorted(ids))


def roster_fingerprint(ids: Iterable[str]) -> int:
    """Return the FNV-1a 32 hash of the sorted, comma-joined IDs."""
    value = 0x811C9DC5
    for byte in ','.join(sorted(ids)).encode('utf-8'):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def mulberry32(seed: int) -> Callable[[], float]:
    """Return a mulberry32 generator yielding floats in [0, 1)."""
    state = seed & 0xFFFFFFFF

    def imul(a: int, b: int) -> int:
        return (a * b) & 0xFFFFFFFF

    def next_float() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = imul(state ^ (state >> 15), state | 1)
        t ^= (t + imul(t ^ (t >> 7), t | 61)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / _U32_RADIX

    return next_float


def draw_shuffle[T](items: Iterable[T], draw_seed: int) -> tuple[T, ...]:
    """Return the items in a Fisher-Yates order driven by `mulberry32`."""
    result = list(items)
    next_float = mulberry32(draw_seed)
    for i in range(len(result) - 1, 0, -1):
        j = int(next_float() * (i + 1))
        result[i], result[j] = result[j], result[i]
    return tuple(result)


def canonical_swaps(
    derived: Sequence[str | None], layout: Sequence[str | None]
) -> list[tuple[int, int]]:
    """Return the swaps turning `derived` into `layout`; byes are equal."""
    if len(derived) != len(layout) or sorted(
        derived, key=lambda x: (x is not None, x or '')
    ) != sorted(layout, key=lambda x: (x is not None, x or '')):
        raise ValueError('layout is not a permutation of the derived layout')

    current = list(derived)
    swaps: list[tuple[int, int]] = []
    for i, wanted in enumerate(layout):
        if current[i] == wanted:
            continue
        j = next(k for k in range(i + 1, len(current)) if current[k] == wanted)
        current[i], current[j] = current[j], current[i]
        swaps.append((i, j))
    return swaps


def apply_swaps(
    derived: Sequence[str | None], swaps: Iterable[tuple[int, int]]
) -> tuple[str | None, ...]:
    """Return `derived` with the swaps applied in order."""
    result = list(derived)
    for i, j in swaps:
        result[i], result[j] = result[j], result[i]
    return tuple(result)


def _check_value(digits: Sequence[int]) -> int:
    # Odd weights are invertible mod 32, so any single wrong character
    # changes the sum.
    return sum((2 * i + 1) * v for i, v in enumerate(digits)) % 32


def _lehmer_digits(indices: Sequence[int]) -> list[int]:
    remaining = sorted(indices)
    digits = []
    for index in indices[:-1]:
        position = remaining.index(index)
        digits.append(position)
        del remaining[position]
    return digits


def _encode_items(items: Iterable[tuple[int, int]]) -> str:
    value = 0
    for v, radix in reversed(list(items)):
        if not 0 <= v < radix:
            raise ValueError(f'value {v} out of range for radix {radix}')
        value = value * radix + v

    digits = []
    while value:
        value, digit = divmod(value, 32)
        digits.append(digit)
    digits.reverse()
    if not digits:
        digits = [0]
    digits.append(_check_value(digits))
    return _PREFIX + ''.join(_ALPHABET[d] for d in digits)


def encode_seed_code(state: SeedingState, derived: Sequence[str | None]) -> str:
    """Encode the seeding state relative to the derived layout."""
    n = len(state.roster)
    if not 2 <= n <= _N_RADIX:
        raise ValueError('roster size out of range')
    if not 1 <= state.tier_count <= _TIER_COUNT_RADIX:
        raise ValueError('tier count out of range')
    if len(state.tiers) != n or len(state.seed_list) != n:
        raise ValueError('tiers and seed list must align with the roster')

    index_of = {contestant: i for i, contestant in enumerate(state.roster)}
    seed_indices = [index_of[contestant] for contestant in state.seed_list]
    swaps = canonical_swaps(derived, state.layout)

    items: list[tuple[int, int]] = [
        (SEED_CODE_VERSION, _VERSION_RADIX),
        (_FORMATS.index(state.format), _FORMAT_RADIX),
        (state.tier_count - 1, _TIER_COUNT_RADIX),
        (n - 1, _N_RADIX),
        (state.param, _PARAM_RADIX),
        (roster_fingerprint(state.roster), _U32_RADIX),
        (state.draw_seed, _U32_RADIX),
    ]
    if state.tier_count > 1:
        items.extend((tier, state.tier_count) for tier in state.tiers)
    items.extend(
        (digit, n - i) for i, digit in enumerate(_lehmer_digits(seed_indices))
    )
    items.append((len(swaps), _SWAP_COUNT_RADIX))
    for a, b in swaps:
        items.append((a, _SWAP_INDEX_RADIX))
        items.append((b, _SWAP_INDEX_RADIX))
    return _encode_items(items)


def _normalize(raw: str) -> str:
    cleaned = ''.join(raw.split()).replace('-', '')
    return cleaned.upper().translate(_CONFUSABLES)


class _Reader:
    def __init__(self, value: int) -> None:
        self.value = value

    def take(self, radix: int) -> int:
        self.value, digit = divmod(self.value, radix)
        return digit


def _fold_digits(digits: Sequence[int]) -> int:
    value = 0
    for digit in digits:
        value = value * 32 + digit
    return value


def decode_seed_code(raw: str) -> Result[DecodedSeedCode, str]:
    """Decode and validate a seed code without knowing the roster."""
    if len(raw) > MAX_CODE_LENGTH:
        return Err(_ERR_TOO_LONG)

    code = _normalize(raw)
    if len(code) > MAX_CODE_LENGTH:
        return Err(_ERR_TOO_LONG)

    if (
        len(code) < 3
        or not code.startswith(_PREFIX)
        or any(c not in _ALPHABET for c in code[1:])
    ):
        return Err(_ERR_DAMAGED)

    digits = [_ALPHABET.index(c) for c in code[1:]]
    check = digits.pop()
    if _check_value(digits) != check:
        return Err(_ERR_CHECK)

    reader = _Reader(_fold_digits(digits))

    version = reader.take(_VERSION_RADIX)
    if version != SEED_CODE_VERSION:
        return Err(_ERR_VERSION)

    format_index = reader.take(_FORMAT_RADIX)
    tier_count = reader.take(_TIER_COUNT_RADIX) + 1
    n = reader.take(_N_RADIX) + 1
    param = reader.take(_PARAM_RADIX)
    fingerprint = reader.take(_U32_RADIX)
    draw_seed = reader.take(_U32_RADIX)
    if format_index >= len(_FORMATS) or n < 2:
        return Err(_ERR_DAMAGED)

    if tier_count > 1:
        tiers = tuple(reader.take(tier_count) for _ in range(n))
    else:
        tiers = (0,) * n

    remaining = list(range(n))
    seed_indices = []
    for i in range(n - 1):
        seed_indices.append(remaining.pop(reader.take(n - i)))
    seed_indices.append(remaining[0])

    swap_count = reader.take(_SWAP_COUNT_RADIX)
    swaps = tuple(
        (reader.take(_SWAP_INDEX_RADIX), reader.take(_SWAP_INDEX_RADIX))
        for _ in range(swap_count)
    )
    if any(a == b for a, b in swaps):
        return Err(_ERR_DAMAGED)
    if reader.value:
        return Err(_ERR_EXTRA)

    return Ok(
        DecodedSeedCode(
            version=version,
            format=_FORMATS[format_index],
            tier_count=tier_count,
            n=n,
            param=param,
            fingerprint=fingerprint,
            draw_seed=draw_seed,
            tiers_by_index=tiers,
            seed_indices=tuple(seed_indices),
            swaps=swaps,
        )
    )


def format_seed_code(code: str) -> str:
    """Group the code in blocks of four joined by hyphens."""
    return '-'.join(code[i : i + 4] for i in range(0, len(code), 4))


def state_from_code(
    decoded: DecodedSeedCode,
    roster_ids: Iterable[str],
    derive: Callable[
        [SeedingFormat, tuple[str, ...], int], tuple[str | None, ...]
    ],
) -> Result[SeedingState, str]:
    """Rebuild the seeding state for a roster; `derive` supplies the layout."""
    roster = canonical_roster(roster_ids)
    if len(roster) != decoded.n or roster_fingerprint(roster) != (
        decoded.fingerprint
    ):
        return Err(_ERR_ROSTER)

    seed_list = tuple(roster[i] for i in decoded.seed_indices)
    derived = derive(decoded.format, seed_list, decoded.param)
    if any(
        not (0 <= a < len(derived) and 0 <= b < len(derived))
        for a, b in decoded.swaps
    ):
        return Err(_ERR_DAMAGED)

    return Ok(
        SeedingState(
            format=decoded.format,
            param=decoded.param,
            tier_count=decoded.tier_count,
            roster=roster,
            tiers=decoded.tiers_by_index,
            seed_list=seed_list,
            layout=apply_swaps(derived, decoded.swaps),
            draw_seed=decoded.draw_seed,
        )
    )
