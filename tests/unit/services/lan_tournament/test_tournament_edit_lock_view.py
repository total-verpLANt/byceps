"""
tests.unit.services.lan_tournament.test_tournament_edit_lock_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

View-layer tests verifying that the ``update()`` POST handler
correctly injects stored values for disabled (locked) fields so
that WTForms validation passes and the service receives the right
values.
"""

from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.score_ordering import (
    ScoreOrdering,
)
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
from byceps.util.result import Ok

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
    a.config['LOCALE'] = 'en'
    return a


def _make_tournament(
    status: TournamentStatus = TournamentStatus.ONGOING,
) -> Tournament:
    """Return a real Tournament dataclass with the given status."""
    return Tournament(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID_STR,
        name='Locked Tournament',
        game='CS2',
        description='Original description',
        image_url='https://example.com/img.png',
        ruleset='Original rules',
        start_time=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        created_at=datetime.now(UTC),
        updated_at=None,
        min_players=2,
        max_players=16,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=ContestantType.SOLO,
        tournament_status=status,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        score_ordering=None,
    )


def _call_update(app, tournament, form_data: dict):
    """Call the update view with the given POST form data.

    Returns the kwargs that ``tournament_service.update_tournament``
    was called with (excluding ``tournament_id``).
    """
    import dataclasses

    from byceps.services.lan_tournament.blueprints.admin import views

    updated = dataclasses.replace(
        tournament,
        description=form_data.get('description', tournament.description),
        image_url=form_data.get('image_url', tournament.image_url),
        ruleset=form_data.get('ruleset', tournament.ruleset),
        updated_at=datetime.now(UTC),
    )

    with app.test_request_context('/', method='POST', data=form_data):
        with (
            patch(
                f'{_V}._get_tournament_or_404',
                return_value=tournament,
            ),
            patch(
                f'{_V}.tournament_service.update_tournament',
                return_value=Ok(updated),
            ) as mock_update,
            patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
            patch(f'{_V}.to_user_timezone', side_effect=lambda dt: dt),
            patch(f'{_V}.to_utc', side_effect=lambda dt: dt),
            patch(f'{_V}.flash_success'),
            patch(f'{_V}.redirect_to'),
        ):
            views.update.__wrapped__(TOURNAMENT_ID_STR)

    return mock_update.call_args


# ------------------------------------------------------------------ #
# tests
# ------------------------------------------------------------------ #


def test_locked_tournament_submit_without_disabled_fields_succeeds(app):
    """When an ONGOING tournament is edited, the browser only submits
    description/image_url/ruleset (disabled fields are omitted).
    The view must inject stored values for the locked fields so the
    service receives the correct data.
    """
    tournament = _make_tournament(TournamentStatus.ONGOING)

    # Simulate what the browser actually sends: only the editable fields.
    form_data = {
        'description': 'Updated description',
        'image_url': 'https://example.com/new.png',
        'ruleset': 'Updated rules',
    }

    call_args = _call_update(app, tournament, form_data)

    assert call_args is not None, (
        'tournament_service.update_tournament was not called'
    )

    kwargs = call_args.kwargs

    # Locked fields should carry the stored (original) values.
    assert kwargs['name'] == 'Locked Tournament'
    assert kwargs['game'] == 'CS2'
    assert kwargs['contestant_type'] == ContestantType.SOLO
    assert kwargs['game_format'] == GameFormat.ONE_V_ONE
    assert kwargs['elimination_mode'] == EliminationMode.SINGLE_ELIMINATION
    assert kwargs['min_players'] == 2
    assert kwargs['max_players'] == 16

    # Editable fields should carry the submitted values.
    assert kwargs['description'] == 'Updated description'
    assert kwargs['image_url'] == 'https://example.com/new.png'
    assert kwargs['ruleset'] == 'Updated rules'


def test_unlocked_tournament_uses_submitted_values(app):
    """When a DRAFT tournament is edited, all submitted form values
    are used — no injection occurs.
    """
    tournament = _make_tournament(TournamentStatus.DRAFT)

    form_data = {
        'name': 'Renamed Tournament',
        'game': 'Valorant',
        'description': 'New description',
        'image_url': '',
        'ruleset': '',
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'score_ordering': '',
        'start_time': '',
        'min_players': '4',
        'max_players': '32',
        'min_teams': '',
        'max_teams': '',
        'min_players_in_team': '',
        'max_players_in_team': '',
    }

    call_args = _call_update(app, tournament, form_data)

    assert call_args is not None, (
        'tournament_service.update_tournament was not called'
    )

    kwargs = call_args.kwargs

    # All values should come from the submitted form data.
    assert kwargs['name'] == 'Renamed Tournament'
    assert kwargs['game'] == 'Valorant'
    assert kwargs['description'] == 'New description'
    assert kwargs['min_players'] == 4
    assert kwargs['max_players'] == 32


def test_locked_paused_tournament_also_injects_stored_values(app):
    """PAUSED status should behave identically to ONGOING for locking."""
    tournament = _make_tournament(TournamentStatus.PAUSED)

    form_data = {
        'description': 'Paused update',
        'image_url': '',
        'ruleset': 'New rules while paused',
    }

    call_args = _call_update(app, tournament, form_data)

    assert call_args is not None
    kwargs = call_args.kwargs

    # Locked fields carry stored values.
    assert kwargs['name'] == 'Locked Tournament'
    assert kwargs['game'] == 'CS2'
    assert kwargs['contestant_type'] == ContestantType.SOLO

    # Editable fields carry submitted values.
    assert kwargs['description'] == 'Paused update'
    assert kwargs['ruleset'] == 'New rules while paused'
