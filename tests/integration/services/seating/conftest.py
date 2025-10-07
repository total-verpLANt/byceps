"""
:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.seating import seating_area_service
from byceps.services.ticketing import ticket_category_service


@pytest.fixture()
def seating_ticket_category(party):
    """Create a ticket category for seating tests."""
    return ticket_category_service.create_category(party.id, 'Seating Standard')


@pytest.fixture()
def seating_area(party):
    """Create a seating area for seating tests."""
    return seating_area_service.create_area(
        party.id, 'seating-test-area', 'Seating Test Area'
    )
