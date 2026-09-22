"""
tests.unit.services.lan_tournament.test_change_status_locking
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import tournament_service
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_service'


def _create_tournament(status: TournamentStatus) -> Tournament:
    return Tournament(
        id=TournamentID(generate_uuid()),
        party_id=PartyID('test-party'),
        name='Test Tournament',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=None,
        tournament_status=status,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )


@pytest.fixture
def repository():
    with (
        patch(f'{_S}.tournament_repository') as repository,
        patch(f'{_S}.signals'),
        patch(f'{_S}.create_log_entry'),
    ):
        yield repository


def _call_names(repository) -> list[str]:
    return [name for name, _, _ in repository.mock_calls]


def test_tournament_is_locked_before_it_is_read(repository):
    tournament = _create_tournament(TournamentStatus.ONGOING)
    repository.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    )

    assert result.is_ok()
    repository.lock_tournament_for_update.assert_called_once_with(tournament.id)
    names = _call_names(repository)
    assert names.index('lock_tournament_for_update') < names.index(
        'get_tournament'
    )


def test_invalid_transition_releases_the_lock(repository):
    tournament = _create_tournament(TournamentStatus.COMPLETED)
    repository.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    )

    assert result.is_err()
    repository.rollback_session.assert_called_once_with()
    repository.update_tournament.assert_not_called()


def test_refused_start_releases_the_lock(repository):
    tournament = _create_tournament(TournamentStatus.REGISTRATION_CLOSED)
    repository.get_tournament.return_value = tournament

    with patch(
        f'{_S}.tournament_match_service.validate_bracket_for_start',
        return_value=['no matches generated'],
    ):
        result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING
        )

    assert result.is_err()
    repository.rollback_session.assert_called_once_with()
    repository.update_tournament.assert_not_called()
