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


@pytest.fixture
def audit_tournament(party):
    from byceps.util.uuid import generate_uuid7
    return tournament_service.create_tournament(PARTY_ID, f'Audit {generate_uuid7()}', contestant_type=ContestantType.SOLO).unwrap()[0]


def _recent(tournament, *, prefixes=('seeding-',), limit=200, statuses=()):
    return tournament_log_service.get_recent_entries_for_tournament(tournament.id, prefixes, limit=limit, registration_statuses=statuses)


def test_recent_entries_keep_only_the_prefixes_newest_first(audit_tournament):
    from datetime import datetime, UTC
    from byceps.services.lan_tournament.models.tournament_log_entry import TournamentLogEntry, TournamentLogEntryID
    from byceps.util.uuid import generate_uuid7
    when = datetime.now(UTC)
    ids = []
    for event in ['seeding-drawn', 'unrelated', 'seeding-swapped']:
        entry = TournamentLogEntry(id=TournamentLogEntryID(generate_uuid7()), occurred_at=when, event_type=event, tournament_id=audit_tournament.id, initiator_id=None, data={'test': 1})
        tournament_log_service.persist_log_entry(entry)
        if event.startswith('seeding-'):
            ids.append(entry.id)
    from byceps.database import db
    from byceps.services.lan_tournament.dbmodels.tournament_log_entry import DbTournamentLogEntry
    stored = db.session.get(DbTournamentLogEntry, max(ids))
    entries = _recent(audit_tournament)
    assert [e.id for e in entries] == sorted(ids, reverse=True)
    assert entries[0].data is not stored.data
    entries[0].data['test'] = 2
    assert stored.data == {'test': 1}
    assert _recent(audit_tournament)[0].data == {'test': 1}


def test_recent_entries_respect_the_limit(audit_tournament):
    for _ in range(3):
        tournament_log_service.create_log_entry('seeding-drawn', audit_tournament.id, None)
    assert len(_recent(audit_tournament, limit=2)) == 2


def test_recent_entries_ignore_other_tournaments(audit_tournament, tournament):
    tournament_log_service.create_log_entry('seeding-drawn', tournament.id, None)
    assert _recent(audit_tournament) == []


# fmt: off
@pytest.mark.parametrize('prefix, literal, impostor', [('seeding_', 'seeding_drawn', 'seedingXdrawn'), ('seeding%', 'seeding%drawn', 'seedingXdrawn'), ('seeding/', 'seeding/drawn', 'seedingXdrawn')])
# fmt: on
def test_recent_entries_treat_like_wildcards_literally(audit_tournament, prefix, literal, impostor):
    for event in [literal, impostor]:
        tournament_log_service.create_log_entry(event, audit_tournament.id, None)
    assert [e.event_type for e in _recent(audit_tournament, prefixes=(prefix,))] == [literal]


def test_recent_entries_reject_empty_prefixes(audit_tournament):
    with pytest.raises(ValueError):
        _recent(audit_tournament, prefixes=())


def test_recent_seeding_entries_match_the_existing_audit_predicate(audit_tournament):
    from byceps.services.lan_tournament import lan_tournament_view_helpers as helpers
    events = [(prefix + 'test', {}) for prefix in (*helpers.SEEDING_LOG_PREFIXES, 'participant-')]
    events += [('tournament-status-changed', data) for data in [{'new_status': 'REGISTRATION_OPEN'}, {'new_status': 'REGISTRATION_CLOSED'}, {'new_status': 'ONGOING'}, {}, {'new_status': None}]]
    for event, data in events:
        tournament_log_service.create_log_entry(event, audit_tournament.id, None, data=data)
    expected = [e for e in tournament_log_service.get_entries_for_tournament(audit_tournament.id) if helpers.is_seeding_audit_entry(e)]
    actual = _recent(audit_tournament, prefixes=(*helpers.SEEDING_LOG_PREFIXES, 'participant-'), statuses=tuple(helpers._REGISTRATION_STATUS_LABELS))
    assert {e.id for e in actual} == {e.id for e in expected}


def test_audit_predicate_is_applied_before_the_limit(audit_tournament):
    for event, data in [('participant-added', {}), ('tournament-status-changed', {'new_status': 'REGISTRATION_OPEN'})]:
        tournament_log_service.create_log_entry(event, audit_tournament.id, None, data=data)
    for i in range(205):
        tournament_log_service.create_log_entry('tournament-status-changed', audit_tournament.id, None, data={'new_status': 'ONGOING'} if i % 2 else {}, commit=False)
    from byceps.database import db
    db.session.commit()
    entries = _recent(audit_tournament, prefixes=('participant-',), limit=1, statuses=('REGISTRATION_OPEN',))
    assert len(entries) == 1
    assert entries[0].data == {'new_status': 'REGISTRATION_OPEN'}
    from flask_babel import force_locale
    from byceps.services.lan_tournament import lan_tournament_view_helpers as helpers
    with force_locale('en'):
        context = helpers.seeding_audit_context(audit_tournament.id, {})
    assert len(context['audit_rows']) == 2
    assert context['audit_limit'] is None
