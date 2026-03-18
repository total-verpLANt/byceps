from enum import Enum


class TournamentMode(Enum):
    SINGLE_ELIMINATION = 1
    DOUBLE_ELIMINATION = 2
    ROUND_ROBIN = 3
    HIGHSCORE = 4

    @property
    def requires_bracket(self) -> bool:
        """Whether this mode needs a generated bracket before starting."""
        return self in (
            TournamentMode.SINGLE_ELIMINATION,
            TournamentMode.DOUBLE_ELIMINATION,
            TournamentMode.ROUND_ROBIN,
        )
