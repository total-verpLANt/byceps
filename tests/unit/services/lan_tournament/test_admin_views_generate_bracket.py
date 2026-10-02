"""
tests.unit.services.lan_tournament.test_admin_views_generate_bracket
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the legacy generate routes of the admin blueprint.

The routes only send the orga to the seeding page: the seeding service is
the one entry for initial generation, so no generator may run from here.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest

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
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
TOURNAMENT_ID_STR = str(TOURNAMENT_ID)
ADMIN_USER_ID = UserID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.admin.views'

_GENERATORS = (
    'generate_single_elimination_bracket',
    'generate_double_elimination_bracket',
    'generate_round_robin_bracket',
    'generate_ffa_round',
)

# fmt: off
_MODES = [
    (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
    (GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION),
    (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN),
    (GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION),
    (GameFormat.HIGHSCORE, EliminationMode.NONE),
]
# fmt: on


@pytest.fixture(scope='module')
def app():
    """Provide a minimal Flask app with a request context."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    return a


def _make_tournament(game_format, elimination_mode) -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.game_format = game_format
    t.elimination_mode = elimination_mode
    t.tournament_status = TournamentStatus.REGISTRATION_CLOSED
    return t


@contextmanager
def _patched_view(tournament):
    with (
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}.tournament_seeding_service') as mock_seeding_svc,
        patch(f'{_V}._get_tournament_or_404', return_value=tournament),
    ):
        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'redirect_to': mock_redirect_to,
            'match_svc': mock_match_svc,
            'seeding_svc': mock_seeding_svc,
        }


def _call(app, view_name, *, force='false'):
    from flask import g

    from byceps.services.lan_tournament.blueprints.admin import views

    with app.test_request_context('/', method='POST', data={'force': force}):
        mock_user = MagicMock()
        mock_user.id = ADMIN_USER_ID
        g.user = mock_user
        getattr(views, view_name).__wrapped__(TOURNAMENT_ID_STR)


def _assert_no_generation(mocks):
    for name in _GENERATORS:
        getattr(mocks['match_svc'], name).assert_not_called()
    mocks['seeding_svc'].generate_from_seeding.assert_not_called()
    mocks['flash_success'].assert_not_called()
    mocks['flash_error'].assert_not_called()


def _assert_redirected_to_seeding(mocks):
    mocks['redirect_to'].assert_called_once_with(
        '.seeding', tournament_id=TOURNAMENT_ID
    )


@pytest.mark.parametrize(('game_format', 'elimination_mode'), _MODES)
@pytest.mark.parametrize('force', ['false', 'true'])
def test_generate_bracket_redirects_to_seeding(
    app, game_format, elimination_mode, force
):
    tournament = _make_tournament(game_format, elimination_mode)
    with _patched_view(tournament) as mocks:
        _call(app, 'generate_bracket', force=force)

    _assert_redirected_to_seeding(mocks)
    _assert_no_generation(mocks)


@pytest.mark.parametrize(
    'status',
    [TournamentStatus.REGISTRATION_OPEN, TournamentStatus.ONGOING],
)
def test_generate_bracket_redirects_in_every_status(app, status):
    tournament = _make_tournament(
        GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION
    )
    tournament.tournament_status = status
    with _patched_view(tournament) as mocks:
        _call(app, 'generate_bracket')

    _assert_redirected_to_seeding(mocks)
    _assert_no_generation(mocks)


def test_generate_ffa_round_without_rounds_redirects_to_seeding(app):
    tournament = _make_tournament(
        GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION
    )
    with _patched_view(tournament) as mocks:
        mocks['match_svc'].has_matches.return_value = False
        _call(app, 'generate_ffa_round_action')

    _assert_redirected_to_seeding(mocks)
    _assert_no_generation(mocks)


def test_generate_ffa_round_without_rounds_redirects_for_de(app):
    tournament = _make_tournament(
        GameFormat.FREE_FOR_ALL, EliminationMode.DOUBLE_ELIMINATION
    )
    with _patched_view(tournament) as mocks:
        mocks['match_svc'].has_matches.return_value = False
        _call(app, 'generate_ffa_round_action')

    _assert_redirected_to_seeding(mocks)
    _assert_no_generation(mocks)


@pytest.mark.parametrize(
    'elimination_mode',
    [
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ],
)
def test_generate_ffa_round_with_rounds_redirects_to_seeding(
    app, elimination_mode
):
    tournament = _make_tournament(GameFormat.FREE_FOR_ALL, elimination_mode)
    with _patched_view(tournament) as mocks:
        mocks['match_svc'].has_matches.return_value = True
        _call(app, 'generate_ffa_round_action')

    _assert_redirected_to_seeding(mocks)
    _assert_no_generation(mocks)


def test_generate_ffa_round_rejects_other_formats(app):
    tournament = _make_tournament(
        GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION
    )
    with _patched_view(tournament) as mocks:
        _call(app, 'generate_ffa_round_action')

    mocks['flash_error'].assert_called_once()
    mocks['match_svc'].generate_ffa_round.assert_not_called()
