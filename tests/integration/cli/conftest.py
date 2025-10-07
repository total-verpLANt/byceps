"""
:Copyright: 2014-2025 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.party.models import PartyID
from byceps.services.seating import seating_area_service
from byceps.services.ticketing import ticket_category_service

from tests.helpers import create_party, generate_token


@pytest.fixture()
def cli_party(make_brand):
    """Create a party for CLI tests."""
    brand = make_brand()
    party_id = PartyID(generate_token())
    party_title = f'CLI Test Party {generate_token()}'
    return create_party(brand, party_id, party_title)


@pytest.fixture()
def cli_ticket_category(cli_party):
    """Create a ticket category for CLI tests."""
    return ticket_category_service.create_category(
        cli_party.id, 'CLI Standard'
    )


@pytest.fixture()
def cli_seating_area(cli_party):
    """Create a seating area for CLI tests."""
    return seating_area_service.create_area(
        cli_party.id, 'cli-test-area', 'CLI Test Area'
    )
