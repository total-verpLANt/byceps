from enum import Enum


class EliminationMode(Enum):
    """How the tournament bracket/elimination structure works."""
    SINGLE_ELIMINATION = 'SINGLE_ELIMINATION'
    DOUBLE_ELIMINATION = 'DOUBLE_ELIMINATION'
    ROUND_ROBIN = 'ROUND_ROBIN'
    NONE = 'NONE'  # No elimination structure (highscore)

    @property
    def has_pools(self) -> bool:
        """Whether the mode uses Winners/Losers pool separation."""
        return self == EliminationMode.DOUBLE_ELIMINATION
