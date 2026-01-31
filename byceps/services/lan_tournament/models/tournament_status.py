from enum import Enum

TournamentStatus = Enum(
    'TournamentStatus',
    [
        'none',
        'draft',
        'registration_open',
        'registration_closed',
        'ongoing',
        'paused',
        'completed',
        'cancelled',
    ],
)
