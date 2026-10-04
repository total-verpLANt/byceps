"""
byceps.services.chair_planning.blueprints.admin.navigation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from functools import wraps

from flask import current_app, url_for
from flask_babel import gettext

from byceps.services.more.blueprints.admin import item_service
from byceps.services.more.blueprints.admin.item_service import MoreItem
from byceps.services.party.models import Party


def install_party_navigation() -> None:
    """Compose chair navigation with the currently installed party menu."""
    if (
        getattr(item_service, '_chair_planning_party_navigation_wrapper', None)
        is not None
    ):
        return

    original = item_service.get_party_items

    @wraps(original)
    def get_party_items_with_chairs(party: Party) -> list[MoreItem]:
        items = list(original(party))
        if 'chair_planning_admin' not in current_app.blueprints:
            return items

        url = url_for('chair_planning_admin.index', party_id=party.id)
        if not any(item.url == url for item in items):
            items.insert(
                0,
                MoreItem(
                    label=gettext('Seat management'),
                    icon='seating-area',
                    url=url,
                    required_permission='seating.view',
                ),
            )
        return items

    item_service._chair_planning_party_navigation_wrapper = (
        get_party_items_with_chairs
    )
    item_service.get_party_items = get_party_items_with_chairs
