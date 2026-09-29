"""
byceps.services.lan_tournament.models.validation_message
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from typing import NamedTuple


class ValidationMessage(NamedTuple):
    msgid: str
    params: tuple[tuple[str, str | int], ...] = ()
