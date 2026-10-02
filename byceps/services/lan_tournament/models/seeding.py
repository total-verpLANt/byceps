"""
byceps.services.lan_tournament.models.seeding
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
from enum import Enum


class SeedingFormat(Enum):
    SINGLE_ELIMINATION = 'SE'
    DOUBLE_ELIMINATION = 'DE'
    ROUND_ROBIN = 'RR'
    FREE_FOR_ALL = 'FFA'


@dataclass(frozen=True, kw_only=True)
class SeedingState:
    format: SeedingFormat
    param: int  # RR: group count; FFA: max lobby size; SE/DE: 0
    tier_count: int  # 1 = no tiers
    roster: tuple[str, ...]  # canonical: sorted contestant IDs
    tiers: tuple[int, ...]  # aligned with roster
    seed_list: tuple[str, ...]
    layout: tuple[str | None, ...]  # None = bye (SE/DE only)
    draw_seed: int


@dataclass(frozen=True, kw_only=True)
class DecodedSeedCode:
    version: int
    format: SeedingFormat
    tier_count: int
    n: int
    param: int
    fingerprint: int
    draw_seed: int
    tiers_by_index: tuple[int, ...]
    seed_indices: tuple[int, ...]
    swaps: tuple[tuple[int, int], ...]
