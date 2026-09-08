"""
tests.integration.services.lan_tournament.test_tournament_log_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2024-logentry')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Log Entry')


@pytest.fixture(scope='module')
def initiator(make_user):
    return make_user('LogEntryInitiator')


@pytest.fixture(scope='module')
def tournament(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Log Entry Test Tournament',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    return tournament


def test_create_and_get_log_entry(tournament, initiator):
    entries_before = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )

    tournament_log_service.create_log_entry(
        'tournament-created',
        tournament.id,
        initiator.id,
        data={'title': 'Log Entry Test Tournament'},
    )

    entries_after = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert len(entries_after) == len(entries_before) + 1

    entry = entries_after[-1]
    assert entry.event_type == 'tournament-created'
    assert entry.tournament_id == tournament.id
    assert entry.initiator_id == initiator.id
    assert entry.data == {'title': 'Log Entry Test Tournament'}
    assert entry.occurred_at is not None


def test_create_log_entry_defaults_to_empty_data(tournament):
    tournament_log_service.create_log_entry(
        'tournament-updated', tournament.id, None
    )

    entries = tournament_log_service.get_entries_for_tournament(tournament.id)
    matching = [e for e in entries if e.event_type == 'tournament-updated']
    assert len(matching) == 1

    entry = matching[0]
    assert entry.data == {}
    assert entry.initiator_id is None


def test_persist_log_entry_flush_only_does_not_commit(tournament, initiator):
    from uuid import UUID

    from byceps.database import db

    entries_before_count = len(
        tournament_log_service.get_entries_for_tournament(tournament.id)
    )

    tournament_log_service.create_log_entry(
        'flush-only-entry', tournament.id, initiator.id, commit=False
    )

    # Flushed but not committed: visible inside this transaction.
    entries_in_tx = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert len(entries_in_tx) == entries_before_count + 1
    assert isinstance(entries_in_tx[-1].id, UUID)

    # Roll back the uncommitted flush to leave no residue.
    db.session.rollback()

    entries_after_rollback = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert len(entries_after_rollback) == entries_before_count
