"""
tests.integration.services.lan_tournament.test_log_entry_data_count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from uuid import uuid4

import pytest

from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2026-logdatacount')

EVENT_TYPE = 'match-ready-claimed'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Log Data Count')


@pytest.fixture(scope='module')
def initiator(make_user):
    return make_user('LogDataCountInitiator')


def _make_tournament():
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Log Data Count {uuid4().hex}',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    return tournament


@pytest.fixture()
def tournament(party):
    return _make_tournament()


@pytest.fixture()
def other_tournament(party):
    return _make_tournament()


def _log(tournament, initiator, data, event_type=EVENT_TYPE) -> None:
    tournament_log_service.create_log_entry(
        event_type, tournament.id, initiator.id, data=data
    )


def _count(tournament, data, event_type=EVENT_TYPE) -> int:
    return tournament_repository.count_log_entries_with_data(
        tournament.id, event_type, data
    )


def test_count_matches_event_type_and_all_data_values(tournament, initiator):
    _log(tournament, initiator, {'match_id': 'm1', 'side': 'A', 'extra': 'x'})
    _log(tournament, initiator, {'match_id': 'm1', 'side': 'A'})
    _log(tournament, initiator, {'match_id': 'm1', 'side': 'B'})
    _log(tournament, initiator, {'match_id': 'm2', 'side': 'A'})

    assert _count(tournament, {'match_id': 'm1', 'side': 'A'}) == 2
    assert _count(tournament, {'match_id': 'm1', 'side': 'B'}) == 1
    assert _count(tournament, {'match_id': 'm1'}) == 3
    assert _count(tournament, {'side': 'A'}) == 3
    assert _count(tournament, {'match_id': 'm2', 'side': 'B'}) == 0
    assert _count(tournament, {'match_id': 'm1', 'extra': 'x'}) == 1


def test_count_ignores_other_tournament_event_type_and_values(
    tournament, other_tournament, initiator
):
    data = {'match_id': 'm1', 'side': 'A'}
    _log(tournament, initiator, data)
    _log(other_tournament, initiator, data)
    _log(other_tournament, initiator, data)
    _log(tournament, initiator, data, event_type='match-ready-revoked')
    _log(tournament, initiator, {'match_id': 'm1', 'side': 'a'})
    _log(tournament, initiator, {'match_id': 'm1'})

    assert _count(tournament, data) == 1
    assert _count(other_tournament, data) == 2
    assert _count(tournament, data, event_type='match-ready-revoked') == 1
    assert _count(tournament, data, event_type='match-lobby-announced') == 0
    assert _count(tournament, {'match_id': 'm1', 'side': 'a'}) == 1
    assert _count(tournament, {'missing_key': 'A'}) == 0


def test_count_compares_numbers_as_text(tournament, initiator):
    _log(tournament, initiator, {'match_id': 'm1', 'pairing_generation': 3})
    _log(tournament, initiator, {'match_id': 'm1', 'pairing_generation': 13})
    _log(tournament, initiator, {'match_id': 'm1', 'pairing_generation': '3'})

    assert _count(tournament, {'pairing_generation': '3'}) == 2
    assert _count(tournament, {'pairing_generation': '13'}) == 1
    assert _count(tournament, {'pairing_generation': '03'}) == 0
    assert _count(tournament, {'pairing_generation': '3.0'}) == 0
    assert (
        _count(tournament, {'match_id': 'm1', 'pairing_generation': '3'}) == 2
    )
