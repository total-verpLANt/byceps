"""
tests.integration.services.lan_tournament.test_tournament_participant_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    TournamentMode,
    TournamentStatus,
)
from byceps.services.party.models import PartyID


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


def test_join_tournament(party, user1):
    tournament = tournament_service.create_tournament(
        PARTY_ID, 'Join Test Tournament', TournamentMode.single_player, 16
    ).unwrap()

    # Open for registration
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

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


def test_join_tournament_duplicate_fails(party, user1):
    tournament = tournament_service.create_tournament(
        PARTY_ID,
        'Duplicate Join Test Tournament',
        TournamentMode.single_player,
        16,
    ).unwrap()

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

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

    # Try to join again - should raise error
    with pytest.raises(ValueError):
        tournament_participant_service.join_tournament(tournament.id, user1)


def test_join_tournament_when_closed_fails(party, user2):
    tournament = tournament_service.create_tournament(
        PARTY_ID,
        'Closed Registration Test',
        TournamentMode.single_player,
        16,
    ).unwrap()

    # Keep it in scheduled status (not open for registration)

    # Try to join - should fail
    result = tournament_participant_service.join_tournament(
        tournament.id, user2.id
    )
    assert result.is_err()
    assert 'not open' in result.unwrap_err()


def test_leave_tournament(party, user1):
    tournament = tournament_service.create_tournament(
        PARTY_ID, 'Leave Test Tournament', TournamentMode.single_player, 16
    ).unwrap()

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    # Join tournament
    result = tournament_participant_service.join_tournament(
        tournament.id, user1.id
    )
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


def test_get_participants_for_tournament(party, user1, user2, user3):
    tournament = tournament_service.create_tournament(
        PARTY_ID,
        'Get Participants Test',
        TournamentMode.single_player,
        16,
    ).unwrap()

    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )

    # Join multiple users
    tournament_participant_service.join_tournament(tournament.id, user1.id)
    tournament_participant_service.join_tournament(tournament.id, user2.id)
    tournament_participant_service.join_tournament(tournament.id, user3.id)

    # Get all participants
    participants = tournament_participant_service.get_participants_for_tournament(
        tournament.id
    )

    assert len(participants) == 3
    participant_user_ids = [p.user_id for p in participants]
    assert user1.id in participant_user_ids
    assert user2.id in participant_user_ids
    assert user3.id in participant_user_ids
