"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.services.pizza_delivery import pizza_delivery_service
from byceps.services.pizza_delivery.models import PizzaDeliveryStatus


BASE_URL = 'http://admin.acmecon.test'


def test_index_for_party_requires_permission(
    make_client, admin_app, party
) -> None:
    """Unauthenticated user gets redirected (not 200)."""
    client = make_client(admin_app)
    url = f'{BASE_URL}/pizza-deliveries/parties/{party.id}'
    response = client.get(url)
    # Without authentication, should get a redirect or 403.
    assert response.status_code in (301, 302, 403)


def test_index_for_party_empty(pizza_admin_client, party) -> None:
    """Authenticated orga sees page (even if empty)."""
    url = f'{BASE_URL}/pizza-deliveries/parties/{party.id}'
    response = pizza_admin_client.get(url)
    assert response.status_code == 200


def test_create_entry_via_form(pizza_admin_client, party) -> None:
    """POST creates entry, redirects to index."""
    url = f'{BASE_URL}/pizza-deliveries/parties/{party.id}'
    form_data = {
        'number': 'TEST-42',
    }
    response = pizza_admin_client.post(url, data=form_data)
    assert response.status_code == 302

    # Verify entry was created.
    entries = pizza_delivery_service.get_entries_for_party(party.id)
    test_entries = [e for e in entries if e.number == 'TEST-42']
    assert len(test_entries) == 1

    # Clean up.
    for entry in test_entries:
        pizza_delivery_service.delete_entry(entry.id)


def test_delete_entry_via_admin(
    pizza_admin_client, party, pizza_admin
) -> None:
    """DELETE removes entry."""
    # Create an entry first.
    result = pizza_delivery_service.create_entry(
        party.id, 'DEL-ADMIN-01', None, pizza_admin.id
    )
    assert result.is_ok()
    entry = result.unwrap()

    url = f'{BASE_URL}/pizza-deliveries/entries/{entry.id}'
    response = pizza_admin_client.delete(url)
    assert response.status_code == 204

    # Verify entry is gone.
    assert pizza_delivery_service.find_entry(entry.id) is None


def test_deliver_entry_via_admin(
    pizza_admin_client, party, pizza_admin
) -> None:
    """POST deliver marks entry as delivered."""
    # Create an entry first.
    result = pizza_delivery_service.create_entry(
        party.id, 'DELIV-ADMIN-01', None, pizza_admin.id
    )
    assert result.is_ok()
    entry = result.unwrap()
    assert entry.status == PizzaDeliveryStatus.REGISTERED

    url = f'{BASE_URL}/pizza-deliveries/entries/{entry.id}/deliver'
    response = pizza_admin_client.post(url)
    assert response.status_code == 204

    # Verify status changed.
    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.status == PizzaDeliveryStatus.DELIVERED

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_deliver_already_delivered_via_admin(
    pizza_admin_client, party, pizza_admin
) -> None:
    """Double deliver returns 204 but entry stays delivered."""
    result = pizza_delivery_service.create_entry(
        party.id, 'DELIV-ADMIN-02', None, pizza_admin.id
    )
    assert result.is_ok()
    entry = result.unwrap()

    # Deliver first time.
    pizza_delivery_service.deliver_entry(entry.id)

    # Try deliver again via admin.
    url = f'{BASE_URL}/pizza-deliveries/entries/{entry.id}/deliver'
    response = pizza_admin_client.post(url)
    # respond_no_content always returns 204.
    assert response.status_code == 204

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)
