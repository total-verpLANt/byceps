"""
tests.integration.services.lan_tournament.test_tournament_request_validation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

D1: `submit_request` must reject an INTEGER-overflowing
`participant_limit` through the domain service, without ever reaching
the database -- an unbounded value with no party capacity to cap it
used to reach Postgres and raise `DataError` instead of returning
`Err`.
"""

from datetime import datetime, UTC

import pytest

from byceps.services.lan_tournament import tournament_request_service
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2026-request-validation')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Request Validation')


@pytest.fixture(scope='module')
def proposer(make_user):
    return make_user('RequestValidationProposer')


def _submit_kwargs(**overrides):
    kwargs = dict(
        party_capacity=None,
        name='Overflow Cup',
        game='Rocket League',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=3,
        participant_limit=8,
        preferred_start_time=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
        description='A friendly cup.',
    )
    kwargs.update(overrides)
    return kwargs


def test_submit_request_rejects_overflowing_participant_limit_without_capacity(
    party, proposer
):
    result = tournament_request_service.submit_request(
        party.id,
        proposer.id,
        **_submit_kwargs(party_capacity=None, participant_limit=3_000_000_000),
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Participant limit must not exceed 1024.'


def test_submit_request_accepts_participant_limit_at_the_cap(party, proposer):
    result = tournament_request_service.submit_request(
        party.id,
        proposer.id,
        **_submit_kwargs(party_capacity=None, participant_limit=1024),
    )

    assert result.is_ok()
