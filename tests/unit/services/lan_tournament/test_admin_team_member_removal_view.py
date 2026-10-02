"""
tests.unit.services.lan_tournament.test_admin_team_member_removal_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The admin removal of a team member names the acting admin, so that an
automatic playoff release the removal makes due is confirmed by them and
not by the captain.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest

from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


TEAM_ID = TournamentTeamID(generate_uuid())
MEMBER_USER_ID = UserID(generate_uuid())
ADMIN_USER_ID = UserID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.admin.views'


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    return a


@contextmanager
def _patched_view():
    team = MagicMock()
    team.id = TEAM_ID
    with (
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.tournament_team_service') as mock_team_svc,
        patch(f'{_V}._get_team_or_404', return_value=team),
    ):
        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'team_svc': mock_team_svc,
        }


def _call(app):
    from flask import g

    from byceps.services.lan_tournament.blueprints.admin import views

    with app.test_request_context('/', method='POST'):
        admin = MagicMock()
        admin.id = ADMIN_USER_ID
        g.user = admin
        views.admin_remove_team_member.__wrapped__(
            str(TEAM_ID), str(MEMBER_USER_ID)
        )


def test_the_removal_names_the_acting_admin(app):
    with _patched_view() as mocks:
        mocks['team_svc'].remove_team_member.return_value = Ok(MagicMock())

        _call(app)

    mocks['team_svc'].remove_team_member.assert_called_once_with(
        TEAM_ID, MEMBER_USER_ID, initiator_id=ADMIN_USER_ID
    )
    mocks['flash_success'].assert_called_once()


def test_a_refused_removal_flashes_the_error(app):
    with _patched_view() as mocks:
        mocks['team_svc'].remove_team_member.return_value = Err('refused')

        _call(app)

    mocks['flash_error'].assert_called_once_with('refused')
    mocks['flash_success'].assert_not_called()
