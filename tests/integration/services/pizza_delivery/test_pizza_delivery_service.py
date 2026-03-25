"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.party.models import Party
from byceps.services.pizza_delivery import pizza_delivery_service
from byceps.services.pizza_delivery.errors import (
    DuplicateDeliveryNumberError,
    PizzaDeliveryEntryAlreadyClaimedError,
    PizzaDeliveryEntryAlreadyDeliveredError,
    PizzaDeliveryEntryNotDeliveredError,
    PizzaDeliveryEntryNotFoundError,
    PizzaDeliveryNumberNotFoundError,
)
from byceps.services.pizza_delivery.models import PizzaDeliveryEntryID, PizzaDeliveryStatus
from byceps.services.user.models import User
from byceps.util.uuid import generate_uuid7


def test_create_entry(admin_app, party: Party, user: User) -> None:
    """Create a pizza delivery entry and verify fields match."""
    result = pizza_delivery_service.create_entry(
        party.id, 'A-001', None, user.id
    )
    assert result.is_ok()

    entry = result.unwrap()
    assert entry.party_id == party.id
    assert entry.number == 'A-001'
    assert entry.user_id is None
    assert entry.created_by_id == user.id

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_create_entry_with_user(
    admin_app, party: Party, user: User
) -> None:
    """Create an entry linked to a user."""
    result = pizza_delivery_service.create_entry(
        party.id, 'B-002', user.id, user.id
    )
    assert result.is_ok()

    entry = result.unwrap()
    assert entry.user_id == user.id

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_create_entry_duplicate_number_fails(
    admin_app, party: Party, user: User
) -> None:
    """Same party+number returns Err(DuplicateDeliveryNumberError)."""
    result1 = pizza_delivery_service.create_entry(
        party.id, 'DUP-001', None, user.id
    )
    assert result1.is_ok()

    result2 = pizza_delivery_service.create_entry(
        party.id, 'DUP-001', None, user.id
    )
    assert result2.is_err()
    error = result2.unwrap_err()
    assert isinstance(error, DuplicateDeliveryNumberError)
    assert error.party_id == party.id
    assert error.number == 'DUP-001'

    # Clean up.
    pizza_delivery_service.delete_entry(result1.unwrap().id)


def test_delete_entry(admin_app, party: Party, user: User) -> None:
    """Create then delete, verify gone."""
    result = pizza_delivery_service.create_entry(
        party.id, 'DEL-001', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()

    delete_result = pizza_delivery_service.delete_entry(entry.id)
    assert delete_result.is_ok()

    # Verify gone.
    assert pizza_delivery_service.find_entry(entry.id) is None


def test_delete_nonexistent_entry_fails(admin_app) -> None:
    """Returns Err(PizzaDeliveryEntryNotFoundError)."""
    fake_id = PizzaDeliveryEntryID(generate_uuid7())
    result = pizza_delivery_service.delete_entry(fake_id)
    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, PizzaDeliveryEntryNotFoundError)
    assert error.entry_id == fake_id


def test_get_entries_for_party(
    admin_app, party: Party, user: User
) -> None:
    """Create 3 entries, verify all returned ordered by created_at desc."""
    entries_created = []
    for i in range(3):
        result = pizza_delivery_service.create_entry(
            party.id, f'LIST-{i:03d}', None, user.id
        )
        assert result.is_ok()
        entries_created.append(result.unwrap())

    entries = pizza_delivery_service.get_entries_for_party(party.id)

    # Should contain at least the 3 we created.
    created_ids = {e.id for e in entries_created}
    returned_ids = {e.id for e in entries}
    assert created_ids.issubset(returned_ids)

    # Verify ordering is created_at desc (most recent first).
    for i in range(len(entries) - 1):
        assert entries[i].created_at >= entries[i + 1].created_at

    # Clean up.
    for entry in entries_created:
        pizza_delivery_service.delete_entry(entry.id)


def test_get_entries_empty_party(
    admin_app, party: Party
) -> None:
    """Empty party returns []."""
    from byceps.services.party.models import PartyID

    # Use a party ID that won't have any entries.
    entries = pizza_delivery_service.get_entries_for_party(
        PartyID('nonexistent-party-for-pizza-test')
    )
    assert entries == []


def test_deliver_entry(admin_app, party: Party, user: User) -> None:
    """Deliver an entry, verify status changes to 'delivered'."""
    result = pizza_delivery_service.create_entry(
        party.id, 'DELIV-001', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()
    assert entry.status == PizzaDeliveryStatus.PENDING

    deliver_result = pizza_delivery_service.deliver_entry(entry.id)
    assert deliver_result.is_ok()
    delivered = deliver_result.unwrap()
    assert delivered.status == PizzaDeliveryStatus.DELIVERED

    # Verify persisted.
    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.status == PizzaDeliveryStatus.DELIVERED

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_deliver_already_delivered_fails(
    admin_app, party: Party, user: User
) -> None:
    """Delivering twice returns AlreadyDeliveredError."""
    result = pizza_delivery_service.create_entry(
        party.id, 'DELIV-002', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()

    deliver_result1 = pizza_delivery_service.deliver_entry(entry.id)
    assert deliver_result1.is_ok()

    deliver_result2 = pizza_delivery_service.deliver_entry(entry.id)
    assert deliver_result2.is_err()
    error = deliver_result2.unwrap_err()
    assert isinstance(error, PizzaDeliveryEntryAlreadyDeliveredError)

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_deliver_nonexistent_entry_fails(admin_app) -> None:
    """Delivering a bogus ID returns NotFoundError."""
    fake_id = PizzaDeliveryEntryID(generate_uuid7())
    result = pizza_delivery_service.deliver_entry(fake_id)
    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, PizzaDeliveryEntryNotFoundError)


def test_claim_entry_by_number(
    admin_app, party: Party, user: User
) -> None:
    """Claim an entry by number, verify user_id is set."""
    result = pizza_delivery_service.create_entry(
        party.id, 'CLAIM-001', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()
    assert entry.user_id is None

    claim_result = pizza_delivery_service.claim_entry_by_number(
        party.id, 'CLAIM-001', user.id
    )
    assert claim_result.is_ok()
    claimed = claim_result.unwrap()
    assert claimed.user_id == user.id

    # Verify persisted.
    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.user_id == user.id

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_claim_already_claimed_fails(
    admin_app, party: Party, user: User, make_user
) -> None:
    """Claiming an already-claimed entry returns AlreadyClaimedError."""
    result = pizza_delivery_service.create_entry(
        party.id, 'CLAIM-002', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()

    # First claim succeeds.
    claim_result1 = pizza_delivery_service.claim_entry_by_number(
        party.id, 'CLAIM-002', user.id
    )
    assert claim_result1.is_ok()

    # Second claim by another user fails.
    other_user = make_user()
    claim_result2 = pizza_delivery_service.claim_entry_by_number(
        party.id, 'CLAIM-002', other_user.id
    )
    assert claim_result2.is_err()
    error = claim_result2.unwrap_err()
    assert isinstance(error, PizzaDeliveryEntryAlreadyClaimedError)

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_claim_nonexistent_number_fails(
    admin_app, party: Party, user: User
) -> None:
    """Claiming a bogus number returns NumberNotFoundError."""
    result = pizza_delivery_service.claim_entry_by_number(
        party.id, 'NONEXISTENT-999', user.id
    )
    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, PizzaDeliveryNumberNotFoundError)


def test_get_entries_for_party_with_status_filter(
    admin_app, party: Party, user: User
) -> None:
    """Status filter returns only matching entries."""
    # Create 2 entries, deliver one.
    r1 = pizza_delivery_service.create_entry(
        party.id, 'FILT-001', None, user.id
    )
    r2 = pizza_delivery_service.create_entry(
        party.id, 'FILT-002', None, user.id
    )
    assert r1.is_ok() and r2.is_ok()
    e1 = r1.unwrap()
    e2 = r2.unwrap()

    pizza_delivery_service.deliver_entry(e1.id)

    delivered = pizza_delivery_service.get_entries_for_party(
        party.id, status=PizzaDeliveryStatus.DELIVERED
    )
    registered = pizza_delivery_service.get_entries_for_party(
        party.id, status=PizzaDeliveryStatus.PENDING
    )

    delivered_ids = {e.id for e in delivered}
    registered_ids = {e.id for e in registered}

    assert e1.id in delivered_ids
    assert e2.id in registered_ids
    assert e1.id not in registered_ids
    assert e2.id not in delivered_ids

    # Clean up.
    pizza_delivery_service.delete_entry(e1.id)
    pizza_delivery_service.delete_entry(e2.id)


def test_get_claimed_entries_for_user(
    admin_app, party: Party, user: User
) -> None:
    """Claimed entries for user returns only that user's entries."""
    r1 = pizza_delivery_service.create_entry(
        party.id, 'MY-001', None, user.id
    )
    r2 = pizza_delivery_service.create_entry(
        party.id, 'MY-002', None, user.id
    )
    assert r1.is_ok() and r2.is_ok()
    e1 = r1.unwrap()
    e2 = r2.unwrap()

    # Claim only the first one.
    pizza_delivery_service.claim_entry_by_number(
        party.id, 'MY-001', user.id
    )

    claimed = pizza_delivery_service.get_claimed_entries_for_user(
        user.id, party.id
    )
    claimed_ids = {e.id for e in claimed}
    assert e1.id in claimed_ids
    assert e2.id not in claimed_ids

    # Clean up.
    pizza_delivery_service.delete_entry(e1.id)
    pizza_delivery_service.delete_entry(e2.id)


def test_undeliver_entry(admin_app, party: Party, user: User) -> None:
    """Create → deliver → undeliver → verify status reverts to PENDING."""
    result = pizza_delivery_service.create_entry(
        party.id, 'UNDEL-001', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()
    assert entry.status == PizzaDeliveryStatus.PENDING

    deliver_result = pizza_delivery_service.deliver_entry(entry.id)
    assert deliver_result.is_ok()
    assert deliver_result.unwrap().status == PizzaDeliveryStatus.DELIVERED

    undeliver_result = pizza_delivery_service.undeliver_entry(entry.id)
    assert undeliver_result.is_ok()
    undelivered = undeliver_result.unwrap()
    assert undelivered.status == PizzaDeliveryStatus.PENDING

    # Verify persisted.
    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.status == PizzaDeliveryStatus.PENDING

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_undeliver_not_delivered_fails(
    admin_app, party: Party, user: User
) -> None:
    """Undelivering a pending entry returns NotDeliveredError."""
    result = pizza_delivery_service.create_entry(
        party.id, 'UNDEL-002', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()
    assert entry.status == PizzaDeliveryStatus.PENDING

    undeliver_result = pizza_delivery_service.undeliver_entry(entry.id)
    assert undeliver_result.is_err()
    error = undeliver_result.unwrap_err()
    assert isinstance(error, PizzaDeliveryEntryNotDeliveredError)
    assert error.entry_id == entry.id

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)


def test_undeliver_nonexistent_entry_fails(admin_app) -> None:
    """Undelivering a bogus ID returns NotFoundError."""
    fake_id = PizzaDeliveryEntryID(generate_uuid7())
    result = pizza_delivery_service.undeliver_entry(fake_id)
    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, PizzaDeliveryEntryNotFoundError)
    assert error.entry_id == fake_id


def test_deliver_then_undeliver_then_deliver_again(
    admin_app, party: Party, user: User
) -> None:
    """Full round-trip: pending → delivered → pending → delivered."""
    result = pizza_delivery_service.create_entry(
        party.id, 'UNDEL-003', None, user.id
    )
    assert result.is_ok()
    entry = result.unwrap()
    assert entry.status == PizzaDeliveryStatus.PENDING

    # First deliver.
    deliver_result1 = pizza_delivery_service.deliver_entry(entry.id)
    assert deliver_result1.is_ok()
    assert deliver_result1.unwrap().status == PizzaDeliveryStatus.DELIVERED

    # Undeliver back to pending.
    undeliver_result = pizza_delivery_service.undeliver_entry(entry.id)
    assert undeliver_result.is_ok()
    assert undeliver_result.unwrap().status == PizzaDeliveryStatus.PENDING

    # Re-deliver.
    deliver_result2 = pizza_delivery_service.deliver_entry(entry.id)
    assert deliver_result2.is_ok()
    assert deliver_result2.unwrap().status == PizzaDeliveryStatus.DELIVERED

    # Verify final persisted state.
    found = pizza_delivery_service.find_entry(entry.id)
    assert found is not None
    assert found.status == PizzaDeliveryStatus.DELIVERED

    # Clean up.
    pizza_delivery_service.delete_entry(entry.id)
