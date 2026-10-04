"""
byceps.services.chair_optout.presentation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import gettext

from byceps.services.ticketing.models.ticket import ChairSource


def get_chair_source_label(source: ChairSource) -> str:
    """Return the translated label for a Core chair source."""
    match source:
        case ChairSource.user:
            return gettext('Brings own chair')
        case ChairSource.venue:
            return gettext('Needs a provided chair')
        case ChairSource.rental:
            return gettext('rented')
        case _:
            return gettext('Not specified yet')
