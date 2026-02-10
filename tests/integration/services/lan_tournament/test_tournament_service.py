"""
tests.integration.services.lan_tournament.test_tournament_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from byceps.services.lan_tournament import tournament_service
from byceps.services.lan_tournament.models import (
    ContestantType,
    TournamentMode,
    TournamentStatus,
)
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2024')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2024')


def test_create_tournament(party):
    title = 'Test Tournament 1'
    max_players = 16

    tournament, event = tournament_service.create_tournament(
        PARTY_ID,
        title,
        max_players=max_players,
        tournament_mode=TournamentMode.SINGLE_PLAYER,
        contestant_type=ContestantType.PLAYER,
    )

    assert tournament is not None
    assert tournament.party_id == PARTY_ID
    assert tournament.name == title
    assert tournament.tournament_mode == TournamentMode.SINGLE_PLAYER
    assert tournament.max_players == max_players
    assert tournament.tournament_status == TournamentStatus.SCHEDULED


def test_find_tournament(party):
    title = 'Test Tournament 2'

    created, _ = tournament_service.create_tournament(
        PARTY_ID,
        title,
        tournament_mode=TournamentMode.TEAMS,
        contestant_type=ContestantType.TEAM,
        max_teams=8,
    )

    found = tournament_service.find_tournament(created.id)

    assert found is not None
    assert found.id == created.id
    assert found.name == title


def test_get_tournaments_for_party(party):
    title1 = 'Test Tournament 3a'
    title2 = 'Test Tournament 3b'

    tournament1, _ = tournament_service.create_tournament(
        PARTY_ID, title1, max_players=16
    )
    tournament2, _ = tournament_service.create_tournament(
        PARTY_ID, title2, max_teams=8
    )

    tournaments = tournament_service.get_tournaments_for_party(PARTY_ID)

    assert len(tournaments) >= 2
    tournament_ids = [t.id for t in tournaments]
    assert tournament1.id in tournament_ids
    assert tournament2.id in tournament_ids


def test_update_tournament(party):
    title = 'Test Tournament 4'
    new_title = 'Updated Tournament 4'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    updated = tournament_service.update_tournament(
        tournament.id,
        name=new_title,
        max_players=32,
        min_players_in_team=2,
        max_players_in_team=4,
        tournament_mode=TournamentMode.TEAMS,
    )

    assert updated.id == tournament.id
    assert updated.name == new_title
    assert updated.tournament_mode == TournamentMode.TEAMS
    assert updated.max_players == 32
    assert updated.min_players_in_team == 2
    assert updated.max_players_in_team == 4


def test_change_status(party):
    title = 'Test Tournament 5'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    assert tournament.tournament_status == TournamentStatus.SCHEDULED

    # Change to REGISTRATION_OPEN
    result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    assert result.is_ok()
    updated, _ = result.unwrap()
    assert updated.tournament_status == TournamentStatus.REGISTRATION_OPEN

    # Change to REGISTRATION_CLOSED
    result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    )
    assert result.is_ok()
    updated, _ = result.unwrap()
    assert updated.tournament_status == TournamentStatus.REGISTRATION_CLOSED


def test_start_tournament(party):
    title = 'Test Tournament 6'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    result = tournament_service.start_tournament(tournament.id)
    assert result.is_ok()
    started, _ = result.unwrap()
    assert started.tournament_status == TournamentStatus.ONGOING


def test_pause_tournament(party):
    title = 'Test Tournament 7'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    # Start first
    tournament_service.start_tournament(tournament.id)

    # Then pause
    result = tournament_service.pause_tournament(tournament.id)
    assert result.is_ok()
    paused, _ = result.unwrap()
    assert paused.tournament_status == TournamentStatus.PAUSED


def test_resume_tournament(party):
    title = 'Test Tournament 8'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    # Start, pause, then resume
    tournament_service.start_tournament(tournament.id)
    tournament_service.pause_tournament(tournament.id)

    result = tournament_service.resume_tournament(tournament.id)
    assert result.is_ok()
    resumed, _ = result.unwrap()
    assert resumed.tournament_status == TournamentStatus.ONGOING


def test_end_tournament(party):
    title = 'Test Tournament 9'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    # Start first
    tournament_service.start_tournament(tournament.id)

    # Then end
    result = tournament_service.end_tournament(tournament.id)
    assert result.is_ok()
    ended, _ = result.unwrap()
    assert ended.tournament_status == TournamentStatus.COMPLETED


def test_delete_tournament(party):
    title = 'Test Tournament 10'

    tournament, _ = tournament_service.create_tournament(
        PARTY_ID, title, max_players=16
    )

    tournament_id = tournament.id

    # Verify it exists
    assert tournament_service.find_tournament(tournament_id) is not None

    # Delete it
    tournament_service.delete_tournament(tournament_id)

    # Verify it's gone
    assert tournament_service.find_tournament(tournament_id) is None
