from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class TournamentSeed:
    match_order: int
    entry_a: str
    entry_b: str
