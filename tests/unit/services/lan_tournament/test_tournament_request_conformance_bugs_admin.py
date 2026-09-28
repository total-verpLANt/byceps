import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, g
from flask_babel import Babel
import pytest

from byceps.services.lan_tournament.blueprints.admin import views


_V = 'byceps.services.lan_tournament.blueprints.admin.views'

_MATCH_ADMIN_CSS = pathlib.Path(
    'byceps/static/style/lan_tournament_match_admin.css'
)


def _rule_block(src: str, selector: str) -> str:
    """Return the declaration block of `selector`."""
    start = src.index(selector)
    end = src.index('}', start)
    return src[start : end + 1]


def test_b1_reject_summary_is_border_box():
    """`.lt-wide` uses `box-sizing: border-box`."""
    src = _MATCH_ADMIN_CSS.read_text()

    rule = _rule_block(src, '.lt-wide {')

    assert 'box-sizing: border-box' in rule


def test_b5_admin_details_are_block_small_dimmed():
    """The admin `.details` spans have a rule under the wrapper."""
    src = _MATCH_ADMIN_CSS.read_text()

    rule = _rule_block(src, '.lt-request-page .details {')

    assert 'display: block' in rule
    assert 'font-size: 0.6875rem' in rule


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app for `test_request_context`, with Babel wired up."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def _reject_with_reason(app, reason):
    """Post `reason` to `reject_request`; return what it did."""
    request_id = '11111111-1111-1111-1111-111111111111'
    user = MagicMock()
    user.has_permission.return_value = True

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data={'reason': reason}),
    ):
        mock_repo.find_request.return_value = SimpleNamespace(
            id=request_id, party_id='p1'
        )
        mock_view_request.return_value = 'rendered-detail'
        g.user = user

        result = views.reject_request(request_id)

    return result, mock_view_request, mock_redirect_to, mock_flash_error


def test_b6_blank_reject_reason_is_one_field_error(app):
    """A blank reject reason is one field error, without flash or redirect."""
    for reason in ('', '   '):
        result, mock_view_request, mock_redirect_to, mock_flash_error = (
            _reject_with_reason(app, reason)
        )

        mock_flash_error.assert_not_called()
        mock_redirect_to.assert_not_called()
        mock_view_request.assert_called_once()
        erroneous_form = mock_view_request.call_args.kwargs[
            'erroneous_reject_form'
        ]
        assert erroneous_form.reason.errors == [
            'A reason is required to reject a request.'
        ]
        assert result == 'rendered-detail'
