from enum import Enum

TournamentMode = Enum(
    'TournamentMode',
    [
        'none',
        'single-elimination',
        'double-elimination',
        'round-robin',
        'highscore',
    ],
)
