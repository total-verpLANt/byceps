"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.services.pizza_delivery import pizza_delivery_service


def test_board_shows_only_delivered(
    site_client, site_app, party, site_user
) -> None:
    """Board page shows only delivered entries."""
    # Create one registered and one delivered entry.
    r1 = pizza_delivery_service.create_entry(
        party.id, 'BOARD-REG', None, site_user.id
    )
    r2 = pizza_delivery_service.create_entry(
        party.id, 'BOARD-DEL', None, site_user.id
    )
    assert r1.is_ok() and r2.is_ok()

    # Deliver the second one.
    pizza_delivery_service.deliver_entry(r2.unwrap().id)

    response = site_client.get('/pizza/')
    assert response.status_code == 200
    content = response.get_data(as_text=True)

    # Delivered entry should be visible, registered should not.
    assert 'BOARD-DEL' in content
    assert 'BOARD-REG' not in content

    # Clean up.
    pizza_delivery_service.delete_entry(r1.unwrap().id)
    pizza_delivery_service.delete_entry(r2.unwrap().id)


def test_claim_entry_success(
    site_client, site_app, party, site_user
) -> None:
    """POST claim links user to pizza number."""
    result = pizza_delivery_service.create_entry(
        party.id, 'CLAIM-SITE-01', None, site_user.id
    )
    assert result.is_ok()
    entry = result.unwrap()

    response = site_client.post(
        '/pizza/claim',
        data={'number': 'CLAIM-SITE-01'},
    )
    # Should redirect to my_status.
    assert response.status_code == 302

    # Verify user_id is set.
    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.user_id == site_user.id

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_claim_nonexistent_number(
    site_client, site_app, party
) -> None:
    """Claiming nonexistent number redirects with error."""
    response = site_client.post(
        '/pizza/claim',
        data={'number': 'DOES-NOT-EXIST'},
    )
    assert response.status_code == 302


def test_claim_requires_login(make_client, site_app) -> None:
    """Unauthenticated claim gets redirected."""
    client = make_client(site_app)
    response = client.post(
        '/pizza/claim',
        data={'number': 'X'},
    )
    assert response.status_code in (301, 302, 403)
