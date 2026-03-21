"""
tests.unit.services.lan_tournament.test_admin_views_generate_bracket
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the generate_bracket admin view dispatch logic.

Tests verify that the correct service function is called for each
GameFormat + EliminationMode combination, and that HIGHSCORE/wrong-status
modes return error flashes without invoking any bracket generation.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
TOURNAMENT_ID_STR = str(TOURNAMENT_ID)
ADMIN_USER_ID = UserID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.admin.views'


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app that provides a request context."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    return a


def _make_tournament(
    game_format: GameFormat,
    elimination_mode: EliminationMode,
) -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.game_format = game_format
    t.elimination_mode = elimination_mode
    t.tournament_status = TournamentStatus.REGISTRATION_CLOSED
    return t


@contextmanager
def _patched_view():
    """Patch all Flask/Babel/BYCEPS view dependencies at once."""
    with (
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'redirect_to': mock_redirect_to,
            'match_svc': mock_match_svc,
            'get_tournament': mock_get_tournament,
        }


def _call(app, tournament, *, force: str = 'false'):
    from flask import g

    from byceps.services.lan_tournament.blueprints.admin import views

    with app.test_request_context(
        '/',
        method='POST',
        data={'force': force},
    ):
        mock_user = MagicMock()
        mock_user.id = ADMIN_USER_ID
        g.user = mock_user
        views.generate_bracket.__wrapped__(TOURNAMENT_ID_STR)


# -------------------------------------------------------------------- #
# mode dispatch — correct service function called
# -------------------------------------------------------------------- #


def test_se_mode_calls_single_elimination_bracket(app):
    """SINGLE_ELIMINATION dispatches to generate_single_elimination_bracket."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
        mocks['get_tournament'].return_value = tournament
        mocks[
            'match_svc'
        ].generate_single_elimination_bracket.return_value = Ok(3)

        _call(app, tournament)

    mocks[
        'match_svc'
    ].generate_single_elimination_bracket.assert_called_once_with(
        TOURNAMENT_ID, force_regenerate=False, initiator_id=ADMIN_USER_ID
    )
    mocks['match_svc'].generate_double_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_round_robin_bracket.assert_not_called()


def test_de_mode_calls_double_elimination_bracket(app):
    """DOUBLE_ELIMINATION dispatches to generate_double_elimination_bracket."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION)
        mocks['get_tournament'].return_value = tournament
        mocks[
            'match_svc'
        ].generate_double_elimination_bracket.return_value = Ok(7)

        _call(app, tournament)

    mocks[
        'match_svc'
    ].generate_double_elimination_bracket.assert_called_once_with(
        TOURNAMENT_ID, force_regenerate=False, initiator_id=ADMIN_USER_ID
    )
    mocks['match_svc'].generate_single_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_round_robin_bracket.assert_not_called()


def test_rr_mode_calls_round_robin_bracket(app):
    """ROUND_ROBIN dispatches to generate_round_robin_bracket."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN)
        mocks['get_tournament'].return_value = tournament
        mocks['match_svc'].generate_round_robin_bracket.return_value = Ok(6)

        _call(app, tournament)

    mocks['match_svc'].generate_round_robin_bracket.assert_called_once_with(
        TOURNAMENT_ID, force_regenerate=False
    )
    mocks['match_svc'].generate_single_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_double_elimination_bracket.assert_not_called()


# -------------------------------------------------------------------- #
# force_regenerate flag
# -------------------------------------------------------------------- #


def test_force_regenerate_true_passed_to_service(app):
    """force=true query param is forwarded as force_regenerate=True."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
        mocks['get_tournament'].return_value = tournament
        mocks[
            'match_svc'
        ].generate_single_elimination_bracket.return_value = Ok(3)

        _call(app, tournament, force='true')

    mocks[
        'match_svc'
    ].generate_single_elimination_bracket.assert_called_once_with(
        TOURNAMENT_ID, force_regenerate=True, initiator_id=ADMIN_USER_ID
    )


# -------------------------------------------------------------------- #
# HIGHSCORE mode — no bracket generation, error flash
# -------------------------------------------------------------------- #


def test_highscore_mode_flashes_error_no_bracket_generation(app):
    """HIGHSCORE flashes an error and skips all bracket service calls."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.HIGHSCORE, EliminationMode.NONE)
        mocks['get_tournament'].return_value = tournament

        _call(app, tournament)

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()
    mocks['match_svc'].generate_single_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_double_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_round_robin_bracket.assert_not_called()


# -------------------------------------------------------------------- #
# Ok / Err result handling
# -------------------------------------------------------------------- #


def test_ok_result_flashes_success(app):
    """Ok result from bracket service produces a success flash."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
        mocks['get_tournament'].return_value = tournament
        mocks[
            'match_svc'
        ].generate_single_elimination_bracket.return_value = Ok(3)

        _call(app, tournament)

    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_err_result_flashes_error(app):
    """Err result from bracket service produces an error flash."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
        mocks['get_tournament'].return_value = tournament
        mocks[
            'match_svc'
        ].generate_single_elimination_bracket.return_value = Err(
            'Already generated'
        )

        _call(app, tournament)

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


# -------------------------------------------------------------------- #
# status guard — wrong status blocks generation
# -------------------------------------------------------------------- #


def test_wrong_status_blocks_bracket_generation(app):
    """REGISTRATION_OPEN status blocks bracket generation with an error flash."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
        tournament.tournament_status = TournamentStatus.REGISTRATION_OPEN
        mocks['get_tournament'].return_value = tournament

        _call(app, tournament)

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()
    mocks['match_svc'].generate_single_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_double_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_round_robin_bracket.assert_not_called()


# -------------------------------------------------------------------- #
# unknown tournament mode — wildcard case
# -------------------------------------------------------------------- #


def test_unknown_game_format_flashes_error(app):
    """Non-standard game_format hits the wildcard case and flashes error."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
        tournament.game_format = 'NOT_A_REAL_FORMAT'
        tournament.elimination_mode = 'NOT_A_REAL_MODE'
        mocks['get_tournament'].return_value = tournament

        _call(app, tournament)

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()
    mocks['match_svc'].generate_single_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_double_elimination_bracket.assert_not_called()
    mocks['match_svc'].generate_round_robin_bracket.assert_not_called()


# -------------------------------------------------------------------- #
# force param read from POST form body
# -------------------------------------------------------------------- #


def test_force_param_read_from_form_body(app):
    """force=true in POST body is forwarded as force_regenerate=True (RR)."""
    with _patched_view() as mocks:
        tournament = _make_tournament(
            GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN,
        )
        mocks['get_tournament'].return_value = tournament
        mocks['match_svc'].generate_round_robin_bracket.return_value = Ok(6)

        _call(app, tournament, force='true')

    mocks['match_svc'].generate_round_robin_bracket.assert_called_once_with(
        TOURNAMENT_ID, force_regenerate=True
    )
