"""
tests.unit.services.lan_tournament.test_admin_views_rr_standings
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the bracket admin/site view's round-robin standings
branch.  Verifies that RR tournaments compute and pass standings to
the template, while SE tournaments still render the bracket.
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

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
TOURNAMENT_ID_STR = str(TOURNAMENT_ID)
PARTY_ID_STR = str(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.admin.views'


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


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
    t.party_id = PARTY_ID_STR
    return t


def _make_party() -> MagicMock:
    p = MagicMock()
    p.id = PARTY_ID_STR
    return p


@contextmanager
def _patched_view():
    """Patch all Flask/Babel/BYCEPS view dependencies at once."""
    with (
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_match_service') as mock_match_svc,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
        patch(f'{_V}.build_contestant_name_lookups') as mock_name_lookups,
        patch(f'{_V}.build_hover_lookups') as mock_hover_lookups,
        patch(f'{_V}.build_round_robin_standings') as mock_rr_standings,
    ):
        mock_name_lookups.return_value = ({}, {})
        mock_hover_lookups.return_value = ({}, {})
        mock_match_svc.get_matches_for_tournament_ordered.return_value = []
        mock_match_svc.get_contestants_for_match.return_value = []
        mock_party_svc.get_party.return_value = _make_party()
        yield {
            'get_tournament': mock_get_tournament,
            'match_svc': mock_match_svc,
            'party_svc': mock_party_svc,
            'name_lookups': mock_name_lookups,
            'hover_lookups': mock_hover_lookups,
            'rr_standings': mock_rr_standings,
        }


def _call_bracket(app):
    """Call the raw bracket view function (past all decorators)."""
    from byceps.services.lan_tournament.blueprints.admin import (
        views,
    )

    # Unwrap @permission_required then @templated to get raw fn.
    raw_fn = views.bracket.__wrapped__.__wrapped__

    with app.test_request_context('/'):
        return raw_fn(TOURNAMENT_ID_STR)


# ------------------------------------------------------------------ #
# RR mode — standings computed
# ------------------------------------------------------------------ #


def test_bracket_view_rr_mode_shows_standings(app):
    """RR tournament bracket page computes and returns standings."""
    with _patched_view() as mocks:
        tournament = _make_tournament(GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN)
        mocks['get_tournament'].return_value = tournament
        mocks['rr_standings'].return_value = ['fake-standing']

        result = _call_bracket(app)

    mocks['rr_standings'].assert_called_once()
    call_args = mocks['rr_standings'].call_args[0][0]
    assert isinstance(call_args, list)  # match_data list
    assert result['standings'] == ['fake-standing']


# ------------------------------------------------------------------ #
# SE mode — no standings, bracket rendered
# ------------------------------------------------------------------ #


def test_bracket_view_se_mode_shows_bracket(app):
    """SE tournament still shows bracket, standings is None."""
    with _patched_view() as mocks:
        tournament = _make_tournament(
            GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION,
        )
        mocks['get_tournament'].return_value = tournament

        result = _call_bracket(app)

    mocks['rr_standings'].assert_not_called()
    assert result['standings'] is None
    assert 'match_data' in result
