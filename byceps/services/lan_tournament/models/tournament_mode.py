from enum import Enum

class TournamentMode(Enum):
    NOT_SET = 0
    SINGLE_ELIMINATION = 1
    DOUBLE_ELIMINATION = 2
    ROUND_ROBIN = 3
    HIGHSCORE = 4
