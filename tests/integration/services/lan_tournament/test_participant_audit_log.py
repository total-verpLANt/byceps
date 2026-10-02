"""
tests.integration.services.lan_tournament.test_participant_audit_log
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Leaving and removing a participant leaves an audit entry.
"""

from itertools import count

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_log_service,
    tournament_participant_service as participant_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-participant-audit')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Participant Audit')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Participant Audit Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'ParticipantAudit{i:02d}') for i in range(4)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('ParticipantAuditAdmin')


def _open_tournament(users, *, ticketed=True, ticket_category=None):
    """Create an open tournament holding every user in `users`."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Participant Audit {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        max_players=16,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    changed = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    assert changed.is_ok(), changed.unwrap_err()

    participants = []
    for user in users:
        added = participant_service.admin_add_participant(
            tournament.id, user.id
        )
        assert added.is_ok(), added.unwrap_err()
        participants.append(added.unwrap()[0])
    return tournament, participants


def _entries(tournament_id, event_type):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament_id
        )
        if e.event_type == event_type
    ]


@pytest.fixture
def left_signals():
    received = []

    def receiver(sender, *, event):
        received.append(event)

    signals.participant_left.connect(receiver)
    yield received
    signals.participant_left.disconnect(receiver)


def test_leaving_logs_participant_left(party, users, left_signals):
    tournament, participants = _open_tournament(users[:3])
    leaver = participants[1]

    result = participant_service.leave_tournament(tournament.id, leaver.id)

    assert result.is_ok(), result.unwrap_err()
    entries = _entries(tournament.id, 'participant-left')
    assert len(entries) == 1
    assert entries[0].initiator_id == leaver.user_id
    assert entries[0].data == {
        'participant_id': str(leaver.id),
        'user_id': str(leaver.user_id),
        'team_id': None,
        'roster_before': 3,
        'roster_after': 2,
    }
    assert [e.participant_id for e in left_signals] == [leaver.id]


def test_admin_removal_logs_participant_removed(party, users, admin):
    tournament, participants = _open_tournament(users[:3])
    removed = participants[0]

    result = participant_service.admin_remove_participant(
        tournament.id, removed.id, initiator=admin
    )

    assert result.is_ok(), result.unwrap_err()
    entries = _entries(tournament.id, 'participant-removed')
    assert len(entries) == 1
    assert entries[0].initiator_id == admin.id
    assert entries[0].data == {
        'participant_id': str(removed.id),
        'user_id': str(removed.user_id),
        'team_id': None,
        'roster_before': 3,
        'roster_after': 2,
    }


def test_ticketless_sweep_logs_each_removal(
    party, users, admin, ticket_category
):
    tournament, participants = _open_tournament(users[:4])
    for user in users[:1]:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    ticketless = participants[1:]

    result = participant_service.remove_participants_without_tickets(
        tournament.id, PARTY_ID, initiator_id=admin.id
    )

    assert result.unwrap() == 3
    entries = _entries(tournament.id, 'participant-removed')
    assert {e.data['participant_id'] for e in entries} == {
        str(p.id) for p in ticketless
    }
    assert {e.initiator_id for e in entries} == {admin.id}
    assert sorted(
        (e.data['roster_before'], e.data['roster_after']) for e in entries
    ) == [(2, 1), (3, 2), (4, 3)]


def test_a_failed_audit_keeps_the_participant_and_sends_no_signal(
    party, users, monkeypatch, left_signals
):
    tournament, participants = _open_tournament(users[:2])
    leaver = participants[0]

    def fail(*args, **kwargs):
        raise RuntimeError('audit write failed')

    monkeypatch.setattr(tournament_log_service, 'create_log_entry', fail)
    rollbacks = []
    real_rollback = tournament_repository.rollback_session

    def spy_rollback():
        rollbacks.append(1)
        real_rollback()

    monkeypatch.setattr(tournament_repository, 'rollback_session', spy_rollback)

    with pytest.raises(RuntimeError):
        participant_service.leave_tournament(tournament.id, leaver.id)

    assert rollbacks
    assert left_signals == []
    with db.engine.connect() as connection:
        remaining = connection.execute(
            text(
                'SELECT count(*) FROM lan_tournament_participants'
                ' WHERE id = :id'
            ),
            {'id': str(leaver.id)},
        ).scalar_one()
    assert remaining == 1


def test_leaving_commits_once(party, users, monkeypatch):
    tournament, participants = _open_tournament(users[:2])
    leaver = participants[0]

    commits = []
    real_commit = db.session.commit

    def spy_commit():
        commits.append(1)
        real_commit()

    monkeypatch.setattr(db.session, 'commit', spy_commit)

    result = participant_service.leave_tournament(tournament.id, leaver.id)

    assert result.is_ok(), result.unwrap_err()
    assert len(commits) == 1
    with db.engine.connect() as connection:
        remaining = connection.execute(
            text(
                'SELECT count(*) FROM lan_tournament_participants'
                ' WHERE id = :id'
            ),
            {'id': str(leaver.id)},
        ).scalar_one()
        logged = connection.execute(
            text(
                'SELECT count(*) FROM lan_tournament_log_entries'
                ' WHERE tournament_id = :id'
                " AND event_type = 'participant-left'"
            ),
            {'id': str(tournament.id)},
        ).scalar_one()
    assert remaining == 0
    assert logged == 1
