from datetime import datetime, timedelta, UTC

import pytest

from byceps.services.pizza_delivery.blueprints.site.views import (
    _get_fresh_entry_ids,
    _is_fresh,
    _ready_first,
)
from byceps.services.pizza_delivery.models import (
    PizzaDeliveryEntry,
    PizzaDeliveryStatus,
)
from byceps.util.uuid import generate_uuid7


NOW = datetime(2026, 10, 3, 19, 42, tzinfo=UTC)


def build_entry(number, status, delivered_at=None) -> PizzaDeliveryEntry:
    return PizzaDeliveryEntry(
        id=generate_uuid7(),
        party_id='party',
        number=number,
        user_id=None,
        status=status,
        created_at=NOW,
        updated_at=NOW,
        created_by_id=generate_uuid7(),
        delivered_at=delivered_at,
    )


# fmt: off
@pytest.mark.parametrize(
    'age, expected',
    [
        (timedelta(0),                      True),
        (timedelta(minutes=9, seconds=59),  True),
        (timedelta(minutes=10),             False),
        (timedelta(hours=1),                False),
        (timedelta(seconds=-5),             False),
    ],
)
# fmt: on
def test_is_fresh_window(age, expected):
    entry = build_entry('A-1', PizzaDeliveryStatus.DELIVERED, NOW - age)

    assert _is_fresh(entry, NOW) is expected


def test_is_fresh_treats_naive_call_time_as_utc():
    naive = (NOW - timedelta(minutes=3)).replace(tzinfo=None)
    entry = build_entry('A-1', PizzaDeliveryStatus.DELIVERED, naive)

    assert _is_fresh(entry, NOW)


def test_entry_without_call_time_is_never_fresh():
    entry = build_entry('A-1', PizzaDeliveryStatus.DELIVERED)

    assert not _is_fresh(entry, NOW)


def test_fresh_entry_ids_only_contain_fresh_entries():
    fresh = build_entry('A-1', 'delivered', NOW - timedelta(minutes=2))
    stale = build_entry('A-2', 'delivered', NOW - timedelta(minutes=30))

    assert _get_fresh_entry_ids([fresh, stale], NOW) == {fresh.id}


def test_ready_first_puts_newest_call_first_and_keeps_waiting_order():
    entries = [
        build_entry('P-1', PizzaDeliveryStatus.PENDING),
        build_entry(
            'D-OLD',
            PizzaDeliveryStatus.DELIVERED,
            NOW - timedelta(minutes=30),
        ),
        build_entry('P-2', PizzaDeliveryStatus.PENDING),
        build_entry(
            'D-NEW',
            PizzaDeliveryStatus.DELIVERED,
            NOW - timedelta(minutes=2),
        ),
    ]

    assert [e.number for e in _ready_first(entries)] == [
        'D-NEW',
        'D-OLD',
        'P-1',
        'P-2',
    ]
