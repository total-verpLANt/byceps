"""
tests.unit.services.lan_tournament.test_tournament_service_bracket_check
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Verify that ``change_status()`` enforces (or skips) the bracket guard
depending on the tournament's ``GameFormat.requires_bracket_generation`` flag.
"""

from datetime import datetime
from unittest.mock import patch

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
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
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _build_valid_se_bracket(tournament_id):
    """Minimal single-match SE bracket (1 final, 2 contestants) that
    ``validate_bracket_for_start`` accepts: a lone terminal match with
    no next_match_id/loser_next_match_id and >= 2 contestants."""
    match = TournamentMatch(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=tournament_id,
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    contestants = [
        TournamentMatchToContestant(
            id=TournamentMatchToContestantID(generate_uuid()),
            tournament_match_id=match.id,
            team_id=None,
            participant_id=TournamentParticipantID(generate_uuid()),
            score=None,
            created_at=NOW,
        )
        for _ in range(2)
    ]
    return [match], {match.id: contestants}


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
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
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
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_bracket_mode_without_matches_fails(
    mock_signals, mock_repository, mock_match_repo
):
    """A SINGLE_ELIMINATION tournament must NOT start with an invalid
    bracket -- the start validation must fire.

    Drives the REAL ``validate_bracket_for_start`` (no patch of it):
    the repository backing ``tournament_match_service`` is mocked to
    report zero generated matches, so the actual gate inside
    ``change_status`` must be the thing that blocks the transition.
    """
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_match_repo.get_matches_for_tournament_ordered.return_value = []
    mock_match_repo.get_contestants_for_tournament.return_value = {}

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    assert 'Cannot start tournament:' in result.unwrap_err()
    assert 'no matches generated' in result.unwrap_err()


# -------------------------------------------------------------------- #
# bracket mode (SE) with valid bracket -> allowed
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_bracket_mode_with_matches_succeeds(
    mock_signals, mock_repository, mock_match_repo
):
    """A SINGLE_ELIMINATION tournament with a structurally valid
    bracket CAN start.

    Drives the REAL ``validate_bracket_for_start`` (no patch of it):
    the repository backing ``tournament_match_service`` is mocked to
    return a structurally valid single-match bracket, so the actual
    gate inside ``change_status`` must be the thing that permits the
    transition.
    """
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    matches, contestants_by_match = _build_valid_se_bracket(tournament.id)

    mock_repository.get_tournament.return_value = tournament
    mock_match_repo.get_matches_for_tournament_ordered.return_value = matches
    mock_match_repo.get_contestants_for_tournament.return_value = (
        contestants_by_match
    )

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()

    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert event.new_status == TournamentStatus.ONGOING


# -------------------------------------------------------------------- #
# invalid bracket blocks start without any override
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_blocked_on_invalid_bracket_without_override(
    mock_signals, mock_repository, mock_validate
):
    """A corrupted bracket blocks the transition to ONGOING; the status
    stays unchanged (hard error)."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_validate.return_value = [
        'match abc is missing next_match_id',
    ]
    update_mock = mock_repository.update_tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    update_mock.assert_not_called()
    mock_signals.tournament_status_changed.send.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_error_lists_violations(
    mock_signals, mock_repository, mock_validate
):
    """The Err message contains the individual violation strings."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    violations = [
        'match abc is missing next_match_id',
        'no grand-final match',
    ]

    mock_repository.get_tournament.return_value = tournament
    mock_validate.return_value = violations

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    message = result.unwrap_err()
    for violation in violations:
        assert violation in message
