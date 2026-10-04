from unittest.mock import patch

from byceps.services.more.blueprints.admin.item_service import MoreItem


def _make_original_items():
    """Return a representative list of original party items."""
    return [
        MoreItem(
            label='Organizer Presence',
            icon='date-okay',
            url='/orga_presence',
            required_permission='orga_presence.view',
        ),
        MoreItem(
            label='Sold Products',
            icon='shop-order',
            url='/sold_products',
            required_permission='shop_order.view',
        ),
        MoreItem(
            label='Timetable',
            icon='date',
            url='/timetable',
            required_permission='timetable.update',
        ),
        MoreItem(
            label='Tournaments',
            icon='trophy',
            url='/tourney',
            required_permission='tourney.view',
        ),
    ]


class _FakeParty:
    def __init__(self):
        self.id = 'test-party-2025'


def test_patched_function_replaces_old_tourney_entry():
    from byceps.services.lan_tournament.blueprints.admin.views import (
        _get_party_items_with_lan_tournaments,
    )

    party = _FakeParty()

    with (
        patch(
            'byceps.services.lan_tournament.blueprints.admin.views'
            '._original_get_party_items',
            return_value=_make_original_items(),
        ),
        patch(
            'byceps.services.lan_tournament.blueprints.admin.views.url_for',
            return_value='/lan_tournament/overview/test-party-2025',
        ),
    ):
        items = _get_party_items_with_lan_tournaments(party)

    old_tourney = [i for i in items if i.required_permission == 'tourney.view']
    assert len(old_tourney) == 0

    lan_tournament = [
        i for i in items if i.required_permission == 'lan_tournament.view'
    ]
    assert len(lan_tournament) == 1
    assert 'LAN Tournaments' in lan_tournament[0].label
    assert lan_tournament[0].icon == 'trophy'


def test_patched_function_preserves_other_items():
    from byceps.services.lan_tournament.blueprints.admin.views import (
        _get_party_items_with_lan_tournaments,
    )

    party = _FakeParty()

    with (
        patch(
            'byceps.services.lan_tournament.blueprints.admin.views'
            '._original_get_party_items',
            return_value=_make_original_items(),
        ),
        patch(
            'byceps.services.lan_tournament.blueprints.admin.views.url_for',
            return_value='/lan_tournament/overview/test-party-2025',
        ),
    ):
        items = _get_party_items_with_lan_tournaments(party)

    permissions = {i.required_permission for i in items}
    assert 'orga_presence.view' in permissions
    assert 'shop_order.view' in permissions
    assert 'timetable.update' in permissions

    # 3 original (minus tourney) + 1 lan_tournament = 4
    assert len(items) == 4
