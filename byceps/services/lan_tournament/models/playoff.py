"""
byceps.services.lan_tournament.models.playoff
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from enum import Enum


class PlayoffReleaseMode(Enum):
    """How the playoff phase of a tournament is released."""

    AUTOMATIC = 'AUTOMATIC'
    MANUAL = 'MANUAL'
