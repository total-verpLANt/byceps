from enum import Enum


class Bracket(Enum):
    WINNERS = 'WB'
    LOSERS = 'LB'
    GRAND_FINAL = 'GF'
    THIRD_PLACE = 'P3'  # Forward declaration — JS renderer ready, server-side generation TBD
