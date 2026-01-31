from __future__ import annotations

from dataclasses import dataclass

@dataclass(kw_only=True)
class TournamentSeed:
    match_order : int
    entryA : str
    entryB : str
