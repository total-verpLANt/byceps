"""
tests.unit.services.lan_tournament.test_admin_delete_view_initiator
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The tournament-delete route must name the admin who pressed the
button.

Deleting a tournament is the one action that outlives every other
record of itself: migration 013 deliberately lets the audit log
survive its subject so a correction cannot be erased by deleting the
tournament around it. That guarantee is worth little if the
``'tournament-deleted'`` entry it leaves behind has no initiator, so
the blueprint must thread ``g.user.id`` through rather than letting
the service default it to ``None``.
"""

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from byceps.services.lan_tournament.models.tournament import TournamentID

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID_STR = str(generate_uuid())
USER_ID = generate_uuid()

_V = 'byceps.services.lan_tournament.blueprints.admin.views'


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _call_delete(app):
    """Call the raw delete POST view, returning the patched service."""
    from byceps.services.lan_tournament.blueprints.admin import views

    raw_fn = views.delete.__wrapped__

    tournament = MagicMock()
    tournament.id = TOURNAMENT_ID
    tournament.name = 'Test Tournament'
    tournament.party_id = PARTY_ID_STR

    with (
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.tournament_service') as mock_service,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_get_tournament.return_value = tournament

        with app.test_request_context('/', method='POST'):
            with patch(f'{_V}.g') as mock_g:
                mock_g.user.id = USER_ID
                raw_fn(str(TOURNAMENT_ID))

        return mock_service


def test_delete_view_passes_the_acting_admin_as_initiator(app):
    mock_service = _call_delete(app)

    mock_service.delete_tournament.assert_called_once()
    args, kwargs = mock_service.delete_tournament.call_args

    initiator = kwargs.get('initiator_id', args[1] if len(args) > 1 else None)
    assert initiator == USER_ID, (
        'delete_tournament must receive the acting admin, otherwise the '
        "'tournament-deleted' audit entry records no initiator."
    )


def test_delete_view_passes_the_tournament_id(app):
    mock_service = _call_delete(app)

    args, _ = mock_service.delete_tournament.call_args
    assert args[0] == TOURNAMENT_ID
