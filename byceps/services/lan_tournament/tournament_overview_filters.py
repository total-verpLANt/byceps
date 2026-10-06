"""Validated URL filters shared by the tournament read-side views."""

from .models.tournament_category import TournamentCategory
from .models.tournament import Tournament


def available_categories(
    tournaments: list[Tournament],
) -> list[TournamentCategory]:
    """Keep enum order and derive choices before applying the category filter."""
    occupied = {t.category for t in tournaments}
    return [category for category in TournamentCategory if category in occupied]


def parse_category(raw: str) -> TournamentCategory | None:
    if raw == 'ALL':
        return None
    return TournamentCategory(raw)


def parse_assignment(raw: str) -> str:
    if raw not in ('all', 'mine'):
        raise ValueError('Invalid assignment filter')
    return raw
