"""
tests.unit.services.lan_tournament.test_tournament_request_reject_form
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

I2e: `TournamentRequestRejectForm.reason` must reject a disallowed
control character (NUL foremost) the same way
`tournament_request_service.reject_request` does, with the identical
message, so a form-error re-render preserves the submitted text
instead of the request only failing once it reaches the service.
"""

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentRequestRejectForm,
)


@pytest.fixture(scope='module')
def app():
    """A minimal Flask app with Babel wired up, as `LocalizedForm` needs."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def test_reject_form_accepts_plain_reason(app):
    with app.test_request_context('/'):
        form = TournamentRequestRejectForm(
            MultiDict({'reason': 'Duplicate of an existing request.'})
        )

        assert form.validate() is True


# fmt: off
@pytest.mark.parametrize(
    ('label', 'reason'),
    [
        ('nul',  'Bad reason\x00hidden'),
        ('bell', 'Bad reason\x07hidden'),
        ('lrm',  'Bad reason\u200ehidden'),
    ],
)
# fmt: on
def test_reject_form_rejects_disallowed_control_char(app, label, reason):
    with app.test_request_context('/'):
        form = TournamentRequestRejectForm(MultiDict({'reason': reason}))

        assert form.validate() is False
        assert form.reason.errors
        assert (
            str(form.reason.errors[0])
            == 'The reason must not contain control characters.'
        )


def test_reject_form_allows_newlines_and_tabs_in_reason(app):
    with app.test_request_context('/'):
        form = TournamentRequestRejectForm(
            MultiDict({'reason': 'Line one\r\nLine two\twith a tab'})
        )

        assert form.validate() is True
