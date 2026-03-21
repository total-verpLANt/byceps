from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class FfaDePoolData:
    """Aggregated per-pool standings and status for FFA-DE tournaments."""

    wb_standings: list[tuple[str, int]]
    lb_standings: list[tuple[str, int]]
    gf_standings: list[tuple[str, int]]
    wb_round_standings: list[dict]
    lb_round_standings: list[dict]
    wb_latest_round: int | None
    lb_latest_round: int | None
    wb_all_confirmed: bool
    lb_all_confirmed: bool
    gf_exists: bool
    gf_match_data: list[dict]
    pool_status: dict[str, int]
