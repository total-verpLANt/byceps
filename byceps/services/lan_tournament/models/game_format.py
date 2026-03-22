from enum import Enum


class GameFormat(Enum):
    """How individual matches work within a tournament."""
    ONE_V_ONE = 'ONE_V_ONE'        # Two contestants per match
    FREE_FOR_ALL = 'FREE_FOR_ALL'  # N contestants per match with placements
    HIGHSCORE = 'HIGHSCORE'        # No matches — solo score submissions

    @property
    def label(self) -> str:
        """Return a human-readable label (not translated)."""
        labels = {
            GameFormat.ONE_V_ONE: '1v1',
            GameFormat.FREE_FOR_ALL: 'Free-for-All',
            GameFormat.HIGHSCORE: 'Highscore',
        }
        return labels[self]

    @property
    def has_match_structure(self) -> bool:
        """Whether the tournament uses matches at all."""
        return self != GameFormat.HIGHSCORE

    @property
    def uses_placements(self) -> bool:
        """Whether matches use placement-based scoring (FFA)."""
        return self == GameFormat.FREE_FOR_ALL

    @property
    def requires_bracket_generation(self) -> bool:
        """Whether bracket must be generated upfront before tournament starts.

        Only 1v1 modes need upfront bracket generation (next_match_id wiring).
        FFA generates rounds on-the-fly. Highscore has no structure.
        This lives on GameFormat (not EliminationMode) because bracket generation
        is a function of how matches are structured, not how elimination works.
        """
        return self == GameFormat.ONE_V_ONE


# Import here to avoid circular imports at module level
from .elimination_mode import EliminationMode  # noqa: E402

VALID_COMBINATIONS: set[tuple[GameFormat, EliminationMode]] = {
    (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
    (GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION),
    (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN),
    (GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION),
    (GameFormat.FREE_FOR_ALL, EliminationMode.DOUBLE_ELIMINATION),
    (GameFormat.HIGHSCORE, EliminationMode.NONE),
}


def is_valid_combination(game_format: GameFormat, elimination_mode: EliminationMode) -> bool:
    return (game_format, elimination_mode) in VALID_COMBINATIONS
