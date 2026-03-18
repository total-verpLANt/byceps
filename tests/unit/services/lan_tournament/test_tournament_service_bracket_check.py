"""
tests.unit.services.lan_tournament.test_tournament_service_bracket_check
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Verify that ``change_status()`` enforces (or skips) the bracket guard
depending on the tournament's ``TournamentMode.requires_bracket`` flag.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime
from unittest.mock import patch

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


def _create_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('test-party'),
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
        'tournament_mode': TournamentMode.SINGLE_ELIMINATION,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


# -------------------------------------------------------------------- #
# bracketless mode (HIGHSCORE) bypasses the bracket guard
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_bracketless_mode_without_matches_succeeds(
    mock_signals, mock_repository
):
    """A HIGHSCORE tournament can transition to ONGOING even without
    any generated matches, because its mode does not require a bracket."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        tournament_mode=TournamentMode.HIGHSCORE,
    )

    mock_repository.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()

    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert event.old_status == TournamentStatus.REGISTRATION_CLOSED
    assert event.new_status == TournamentStatus.ONGOING

    # Bracket helper must NOT have been consulted
    mock_repository.get_matches_for_tournament.assert_not_called()


# -------------------------------------------------------------------- #
# bracket mode (SE) without matches -> blocked
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_bracket_mode_without_matches_fails(
    mock_signals, mock_repository
):
    """A SINGLE_ELIMINATION tournament must NOT start without generated
    matches -- the bracket guard must fire."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_repository.get_matches_for_tournament.return_value = []

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    assert 'Cannot start tournament without generated brackets' in result.unwrap_err()


# -------------------------------------------------------------------- #
# bracket mode (SE) with matches -> allowed
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_bracket_mode_with_matches_succeeds(
    mock_signals, mock_repository
):
    """A SINGLE_ELIMINATION tournament with generated matches CAN start."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    mock_repository.get_tournament.return_value = tournament
    # Simulate at least one generated match
    mock_repository.get_matches_for_tournament.return_value = [object()]

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()

    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert event.new_status == TournamentStatus.ONGOING
