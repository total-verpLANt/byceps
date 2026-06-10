from enum import Enum


class ContestantStatus(Enum):
    """Status of a contestant in an FFA match."""
    FINISHED = 'FINISHED'  # Normal completion
    DNS = 'DNS'            # Did Not Start
    DNF = 'DNF'            # Did Not Finish
    DQ = 'DQ'              # Disqualified
