from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
import pytest

from byceps.util.result import Err, Ok


_V = 'byceps.services.lan_tournament.blueprints.site.views'
CAPTAIN_LEAVE_ERROR = (
    'Team captain cannot leave while team has other members. '
    'Transfer captain role first or have other members leave.'
)


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config.update(TESTING=True, LOCALE='en', SECRET_KEY='unit-test-only')
    return app


def _leave(app, outcome):
    from byceps.services.lan_tournament.blueprints.site import views

    user_id = uuid4()
    participant = SimpleNamespace(id=uuid4(), user_id=user_id)
    with (
        app.app_context(),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg) as gettext,
        patch(f'{_V}.flash_error') as flash_error,
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.redirect_to'),
        patch(
            f'{_V}._get_tournament_or_404',
            return_value=SimpleNamespace(id=uuid4(), name='T'),
        ),
        patch(f'{_V}.tournament_participant_service') as service,
        patch(f'{_V}.g') as g,
    ):
        g.user = SimpleNamespace(id=user_id)
        service.get_participants_for_tournament.return_value = [participant]
        service.leave_tournament.return_value = outcome
        with app.test_request_context('/', method='POST'):
            views.leave.__wrapped__(str(uuid4()))
    return gettext, flash_error


def test_the_service_error_of_a_refused_leave_is_translated(app):
    gettext, flash_error = _leave(app, Err(CAPTAIN_LEAVE_ERROR))

    assert any(
        call.args == (CAPTAIN_LEAVE_ERROR,) and not call.kwargs
        for call in gettext.call_args_list
    )
    flash_error.assert_called_once()


def test_a_successful_leave_flashes_no_error(app):
    _gettext, flash_error = _leave(app, Ok(object()))

    flash_error.assert_not_called()
