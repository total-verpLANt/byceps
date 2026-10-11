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
    """Claiming a nonexistent number re-renders the page with an error."""
    response = site_client.post(
        '/pizza/claim',
        data={'number': 'does-not-exist'},
    )
    assert response.status_code == 422

    content = response.get_data(as_text=True)
    assert 'Pizza number not found.' in content
    # The entered number stays in the field.
    assert 'value="DOES-NOT-EXIST"' in content


def test_claim_empty_number(site_client, site_app, party) -> None:
    response = site_client.post('/pizza/claim', data={'number': '  '})

    assert response.status_code == 422
    assert 'Please enter a pizza number.' in response.get_data(as_text=True)


def test_claim_too_long_number(site_client, site_app, party) -> None:
    response = site_client.post('/pizza/claim', data={'number': 'X' * 21})

    assert response.status_code == 422
    assert 'Pizza number is too long.' in response.get_data(as_text=True)


def test_claim_echoes_at_most_64_characters(
    site_client, site_app, party
) -> None:
    response = site_client.post('/pizza/claim', data={'number': 'Y' * 500})

    assert response.status_code == 422
    content = response.get_data(as_text=True)
    assert 'value="' + 'Y' * 64 + '"' in content
    assert 'Y' * 65 not in content


def test_claim_already_claimed_number(
    site_client, site_app, party, site_user, make_user
) -> None:
    """A number somebody else follows is a field error without a name."""
    other = make_user()
    entry = pizza_delivery_service.create_entry(
        party.id, 'CLAIM-TAKEN-01', other.id, site_user.id
    ).unwrap()

    response = site_client.post(
        '/pizza/claim', data={'number': 'claim-taken-01'}
    )
    assert response.status_code == 422

    content = response.get_data(as_text=True)
    assert 'Someone is already following this number.' in content
    assert other.screen_name not in content

    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.user_id == other.id

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_claim_of_called_number_warns_and_stays_on_target(
    site_client, site_app, party, site_user
) -> None:
    """An already called number is warned about; `next=board` is honoured."""
    entry = pizza_delivery_service.create_entry(
        party.id, 'CLAIM-CALLED-01', None, site_user.id
    ).unwrap()
    pizza_delivery_service.deliver_entry(entry.id).unwrap()

    response = site_client.post(
        '/pizza/claim',
        data={'number': 'CLAIM-CALLED-01', 'next': 'board'},
    )
    assert response.status_code == 302
    assert response.location.endswith('/pizza/')

    followed = site_client.get(response.location)
    content = followed.get_data(as_text=True)
    assert 'CLAIM-CALLED-01 has already been called.' in content
    assert 'color-warning' in content

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_claim_ignores_foreign_next_target(
    site_client, site_app, party, site_user
) -> None:
    """Only the known `board` value changes the redirect target."""
    entry = pizza_delivery_service.create_entry(
        party.id, 'CLAIM-NEXT-01', None, site_user.id
    ).unwrap()

    response = site_client.post(
        '/pizza/claim',
        data={'number': 'CLAIM-NEXT-01', 'next': 'https://evil.example/'},
    )
    assert response.status_code == 302
    assert response.location.endswith('/pizza/my-status')

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_claim_requires_login(make_client, site_app) -> None:
    """Unauthenticated claim gets redirected."""
    client = make_client(site_app)
    response = client.post(
        '/pizza/claim',
        data={'number': 'X'},
    )
    assert response.status_code in (301, 302, 403)
