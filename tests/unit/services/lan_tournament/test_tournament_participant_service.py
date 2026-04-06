"""
tests.unit.services.lan_tournament.test_tournament_participant_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, UTC
from unittest.mock import Mock, patch

import pytest

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament import tournament_participant_service
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')

MOCK_PREFIX = 'byceps.services.lan_tournament.tournament_participant_service'


# -------------------------------------------------------------------- #
# get_teams_below_minimum_size
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_get_teams_below_minimum_size_returns_undersized_teams(mock_repo):
    """Teams with fewer members than min_players_in_team are returned."""
    tournament = _create_tournament(min_players_in_team=3)
    team_a = _create_team('Team A')
    team_b = _create_team('Team B')

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = [team_a, team_b]
    mock_repo.get_team_member_counts.return_value = {
        team_a.id: 2,
        team_b.id: 5,
    }

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID
    )

    assert len(result) == 1
    assert result[0] == (team_a, 2)


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_get_teams_below_minimum_size_no_minimum_set(mock_repo):
    """When min_players_in_team is None, return empty list immediately."""
    tournament = _create_tournament(min_players_in_team=None)

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID, tournament=tournament
    )

    assert result == []
    mock_repo.get_teams_for_tournament.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_get_teams_below_minimum_size_all_teams_sufficient(mock_repo):
    """When all teams meet the minimum, return empty list."""
    tournament = _create_tournament(min_players_in_team=2)
    team_a = _create_team('Team A')
    team_b = _create_team('Team B')

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = [team_a, team_b]
    mock_repo.get_team_member_counts.return_value = {
        team_a.id: 3,
        team_b.id: 2,
    }

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID
    )

    assert result == []


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_get_teams_below_minimum_size_no_teams(mock_repo):
    """When no teams exist, return empty list."""
    tournament = _create_tournament(min_players_in_team=3)

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = []

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID
    )

    assert result == []
    mock_repo.get_team_member_counts.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_repository')
def test_get_teams_below_minimum_size_uses_prefetched_tournament(mock_repo):
    """When tournament is passed, the repo is not queried for it."""
    tournament = _create_tournament(min_players_in_team=2)
    team_a = _create_team('Team A')

    mock_repo.get_teams_for_tournament.return_value = [team_a]
    mock_repo.get_team_member_counts.return_value = {team_a.id: 1}

    result = tournament_participant_service.get_teams_below_minimum_size(
        TOURNAMENT_ID, tournament=tournament
    )

    mock_repo.get_tournament.assert_not_called()
    assert len(result) == 1
    assert result[0] == (team_a, 1)


# -------------------------------------------------------------------- #
# get_seats_for_users
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
def test_get_seats_for_users_empty_input(mock_db):
    """Empty user_ids set returns empty dict without querying."""
    result = tournament_participant_service.get_seats_for_users(
        set(), PARTY_ID
    )

    assert result == {}
    mock_db.session.scalars.assert_not_called()


@patch(f'{MOCK_PREFIX}.select')
@patch(f'{MOCK_PREFIX}.db')
def test_get_seats_for_users_returns_seat_labels(mock_db, _mock_select):
    """Returns mapping of user_id to seat label."""
    user_id_1 = UserID(generate_uuid())
    user_id_2 = UserID(generate_uuid())

    ticket_1 = _create_mock_ticket(user_id_1, seat_label='A-01')
    ticket_2 = _create_mock_ticket(user_id_2, seat_label='B-05')

    mock_db.session.scalars.return_value.all.return_value = [
        ticket_1,
        ticket_2,
    ]

    result = tournament_participant_service.get_seats_for_users(
        {user_id_1, user_id_2}, PARTY_ID
    )

    assert result == {user_id_1: 'A-01', user_id_2: 'B-05'}


@patch(f'{MOCK_PREFIX}.select')
@patch(f'{MOCK_PREFIX}.db')
def test_get_seats_for_users_deduplicates_users(mock_db, _mock_select):
    """When a user has multiple tickets, only the first seat is kept."""
    user_id = UserID(generate_uuid())

    ticket_1 = _create_mock_ticket(user_id, seat_label='A-01')
    ticket_2 = _create_mock_ticket(user_id, seat_label='A-02')

    mock_db.session.scalars.return_value.all.return_value = [
        ticket_1,
        ticket_2,
    ]

    result = tournament_participant_service.get_seats_for_users(
        {user_id}, PARTY_ID
    )

    assert result == {user_id: 'A-01'}


@patch(f'{MOCK_PREFIX}.select')
@patch(f'{MOCK_PREFIX}.db')
def test_get_seats_for_users_skips_users_without_seats(mock_db, _mock_select):
    """Users whose tickets have no occupied seat are omitted."""
    user_id_with_seat = UserID(generate_uuid())
    user_id_no_seat = UserID(generate_uuid())

    ticket_with_seat = _create_mock_ticket(
        user_id_with_seat, seat_label='C-03'
    )
    ticket_no_seat = _create_mock_ticket(user_id_no_seat, seat_label=None)
    ticket_no_seat.occupied_seat = None

    mock_db.session.scalars.return_value.all.return_value = [
        ticket_with_seat,
        ticket_no_seat,
    ]

    result = tournament_participant_service.get_seats_for_users(
        {user_id_with_seat, user_id_no_seat}, PARTY_ID
    )

    assert result == {user_id_with_seat: 'C-03'}


@patch(f'{MOCK_PREFIX}.select')
@patch(f'{MOCK_PREFIX}.db')
def test_get_seats_for_users_uses_area_title_fallback(mock_db, _mock_select):
    """When seat.label is None, falls back to seat.area.title."""
    user_id = UserID(generate_uuid())

    ticket = _create_mock_ticket(
        user_id, seat_label=None, area_title='VIP Area'
    )

    mock_db.session.scalars.return_value.all.return_value = [ticket]

    result = tournament_participant_service.get_seats_for_users(
        {user_id}, PARTY_ID
    )

    assert result == {user_id: 'VIP Area'}


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_tournament(**kwargs) -> Tournament:
    """Create a tournament for testing."""
    defaults = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'updated_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': None,
        'game_format': None,
        'elimination_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _create_team(name: str) -> TournamentTeam:
    """Create a tournament team for testing."""
    return TournamentTeam(
        id=TournamentTeamID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        name=name,
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=UserID(generate_uuid()),
        join_code=None,
        created_at=NOW,
    )


def _create_mock_ticket(
    user_id: UserID,
    *,
    seat_label: str | None,
    area_title: str | None = None,
) -> Mock:
    """Create a mock ticket with occupied seat for testing."""
    mock_area = Mock()
    mock_area.title = area_title

    mock_seat = Mock()
    mock_seat.label = seat_label
    mock_seat.area = mock_area

    mock_ticket = Mock()
    mock_ticket.used_by_id = user_id
    mock_ticket.occupied_seat = mock_seat
    return mock_ticket
