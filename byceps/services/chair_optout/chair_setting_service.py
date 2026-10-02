"""
byceps.services.chair_optout.chair_setting_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.services.party import party_setting_service
from byceps.services.party.models import PartyID


_RENTAL_SELECTION_SETTING = 'chair_rental_selection_enabled'


def is_rental_selection_enabled(party_id: PartyID) -> bool:
    """Return whether this party permits new rental chair selections."""
    return (
        party_setting_service.find_setting_value(
            party_id, _RENTAL_SELECTION_SETTING
        )
        == 'true'
    )


def should_show_rental_information(
    party_id: PartyID, *, has_rented_chair: bool
) -> bool:
    """Show rental filters and legend for enabled selection or retained answers."""
    return is_rental_selection_enabled(party_id) or has_rented_chair


def set_rental_selection_enabled(party_id: PartyID, enabled: bool) -> None:
    """Store the party's rental selection switch."""
    party_setting_service.create_or_update_setting(
        party_id, _RENTAL_SELECTION_SETTING, 'true' if enabled else 'false'
    )
