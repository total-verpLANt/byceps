"""
tests.integration.services.lan_tournament.test_tournament_participant_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import TournamentStatus
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2024-participant')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Participant')


@pytest.fixture(scope='module')
def user1(make_user):
    return make_user('ParticipantUser1')


@pytest.fixture(scope='module')
def user2(make_user):
    return make_user('ParticipantUser2')


@pytest.fixture(scope='module')
def user3(make_user):
    return make_user('ParticipantUser3')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Tournament Entry')


@pytest.fixture(scope='module')
def grant_ticket(ticket_category):
    """Give a user a valid (used) ticket for the party."""

    def _grant(user):
        return ticket_creation_service.create_ticket(
            ticket_category, user, user=user
        )

    return _grant


def _create_solo_tournament(name):
    result = tournament_service.create_tournament(PARTY_ID, name, max_players=16)
    assert result.is_ok()
    tournament, _ = result.unwrap()
    return tournament


def test_join_tournament(party, user1, grant_ticket):
    tournament = _create_solo_tournament('Join Test Tournament')

    # Open for registration
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    grant_ticket(user1)

    # Join tournament
    result = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )

    assert result.is_ok()
    participant, event = result.unwrap()

    assert participant is not None
    assert participant.tournament_id == tournament.id
    assert participant.user_id == user1.id

    # Verify participant count
    count = tournament_service.get_participant_count(tournament.id)
    assert count == 1


def test_join_tournament_duplicate_fails(party, user1, grant_ticket):
    tournament = _create_solo_tournament('Duplicate Join Test Tournament')

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    grant_ticket(user1)

    # Join once
    result1 = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
    assert result1.is_ok()

    # Try to join again - should fail
    result2 = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
    assert result2.is_err()
    assert 'already registered' in result2.unwrap_err()


def test_join_tournament_when_closed_fails(party, user2):
    tournament = _create_solo_tournament('Closed Registration Test')

    # Keep it in draft status (not open for registration)

    # Try to join - should fail
    result = tournament_participant_service.join_tournament(
        tournament.id, user2.id
    )
    assert result.is_err()
    assert 'not open' in result.unwrap_err()


def test_leave_tournament(party, user1, grant_ticket):
    tournament = _create_solo_tournament('Leave Test Tournament')

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    grant_ticket(user1)

    # Join tournament
    result = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
    assert result.is_ok()
    participant, _ = result.unwrap()

    # Verify joined
    count = tournament_service.get_participant_count(tournament.id)
    assert count == 1

    # Leave tournament
    leave_result = tournament_participant_service.leave_tournament(
        tournament.id, participant.id
    )
    assert leave_result.is_ok()

    # Verify left
    count = tournament_service.get_participant_count(tournament.id)
    assert count == 0


def test_get_participants_for_tournament(party, user1, user2, user3, grant_ticket):
    tournament = _create_solo_tournament('Get Participants Test')

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    # Join multiple users
    for user in (user1, user2, user3):
        grant_ticket(user)
        result = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert result.is_ok()

    # Get all participants
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament.id
        )
    )

    assert len(participants) == 3
    participant_user_ids = [p.user_id for p in participants]
    assert user1.id in participant_user_ids
    assert user2.id in participant_user_ids
    assert user3.id in participant_user_ids
