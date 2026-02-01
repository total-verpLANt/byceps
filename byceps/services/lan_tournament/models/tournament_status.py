from enum import Enum

class TournamentStatus(Enum):
    NONE = 0
    DRAFT = 1
    REGISTRATION_OPEN = 2
    REGISTRATION_CLOSED = 3
    ONGOING = 4
    PAUSED = 5
    COMPLETED = 6
    CANCELLED = 7
