"""
tests.unit.services.lan_tournament.test_match_comment_unstorable_text
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The comment routes of the orga and admin surfaces read `request.form`
without a form, so the service itself must refuse text PostgreSQL cannot
store.
"""

from unittest.mock import call, MagicMock, patch

from flask import Flask, g
import pytest

from byceps.services.lan_tournament import tournament_match_service
from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.tournament_config_domain_service import (
    UNSTORABLE_CHARACTER_ERROR,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err

from tests.helpers import generate_uuid


MATCH_ID = TournamentMatchID(generate_uuid())
COMMENT_ID = TournamentMatchCommentID(generate_uuid())
USER_ID = UserID(generate_uuid())

_SERVICE = 'byceps.services.lan_tournament.tournament_match_service'
_VIEWS = 'byceps.services.lan_tournament.blueprints.admin.views'


# fmt: off
_UNSTORABLE = ['A\x00B', '\x00', 'A\ud800B', '\udfff']
# fmt: on


@pytest.mark.parametrize('comment', _UNSTORABLE)
def test_add_comment_refuses_unstorable_text(comment):
    with patch(f'{_SERVICE}.tournament_repository') as mock_repo:
        result = tournament_match_service.add_comment(
            MATCH_ID, USER_ID, comment
        )

    assert result == Err(UNSTORABLE_CHARACTER_ERROR)
    mock_repo.create_match_comment.assert_not_called()


@pytest.mark.parametrize('comment', _UNSTORABLE)
def test_update_comment_refuses_unstorable_text(comment):
    with patch(f'{_SERVICE}.tournament_repository') as mock_repo:
        result = tournament_match_service.update_comment(COMMENT_ID, comment)

    assert result == Err(UNSTORABLE_CHARACTER_ERROR)
    mock_repo.update_match_comment.assert_not_called()


@pytest.mark.parametrize('comment', ['Gut gespielt ✓', 'Zeile\nZwei\tTab'])
def test_comment_with_storable_text_is_written(comment):
    with patch(f'{_SERVICE}.tournament_repository') as mock_repo:
        added = tournament_match_service.add_comment(MATCH_ID, USER_ID, comment)
        updated = tournament_match_service.update_comment(COMMENT_ID, comment)

    assert added.is_ok()
    assert updated.is_ok()
    mock_repo.create_match_comment.assert_called_once()
    mock_repo.update_match_comment.assert_called_once_with(COMMENT_ID, comment)


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def test_admin_comment_route_translates_the_service_error(app):
    with (
        patch(
            f'{_VIEWS}.gettext', side_effect=lambda msg, **kw: msg
        ) as gettext,
        patch(f'{_VIEWS}.flash_error') as flash_error,
        patch(f'{_VIEWS}.redirect_to'),
        patch(f'{_VIEWS}._get_match_or_404') as get_match,
        patch(f'{_VIEWS}.tournament_match_service') as match_service,
    ):
        get_match.return_value = MagicMock(id=MATCH_ID)
        match_service.add_comment.return_value = Err(UNSTORABLE_CHARACTER_ERROR)

        with app.test_request_context(
            '/', method='POST', data={'comment': 'A\x00B'}
        ):
            g.user = MagicMock(id=USER_ID)
            views.add_match_comment.__wrapped__(str(MATCH_ID))

    assert call(UNSTORABLE_CHARACTER_ERROR) in gettext.call_args_list
    flash_error.assert_called_once()
