"""
tests.unit.services.lan_tournament.test_tournament_request_datetime_bounds_forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A `DateTimeLocalField` accepts any year a browser lets through
(`0001-01-01`, `9999-12-31`). Localizing one of those through
`flask_babel.to_utc` (the views' conversion point, always called
after `form.validate()`) overflows `datetime`'s range in a zone with
a pre-1893 LMT offset (Europe/Berlin) and 500s. `year_in_range_validator`
(`tournament_request_domain_service`) bounds every affected field:
`TournamentProposeForm`/`TournamentRequestUpdateForm`'s
`preferred_start_time`/`preferred_end_time`, and `_BaseForm.start_time`
(`TournamentCreateForm`/`TournamentUpdateForm`).
"""

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentCreateForm,
    TournamentRequestUpdateForm,
)
from byceps.services.lan_tournament.blueprints.site.forms import (
    TournamentProposeForm,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.tournament_request_domain_service import (
    YEAR_RANGE_ERROR_MESSAGE,
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


_REQUEST_FORM_DATA = {
    'name': 'Test Cup',
    'game': 'Test Game',
    'game_format': GameFormat.ONE_V_ONE.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '1',
    'participant_limit': '8',
    'preferred_start_time': '2026-06-01T10:00',
    'preferred_end_time': '2026-06-01T12:00',
    'description': 'A description.',
}


# -------------------------------------------------------------------- #
# TournamentProposeForm (site)


def test_propose_form_rejects_year_1_preferred_start_time(app):
    with app.test_request_context('/'):
        data = _REQUEST_FORM_DATA | {'preferred_start_time': '0001-01-01T00:00'}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert str(YEAR_RANGE_ERROR_MESSAGE) in [
            str(e) for e in form.preferred_start_time.errors
        ]


def test_propose_form_rejects_year_9999_preferred_end_time(app):
    with app.test_request_context('/'):
        data = _REQUEST_FORM_DATA | {'preferred_end_time': '9999-12-31T23:59'}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert str(YEAR_RANGE_ERROR_MESSAGE) in [
            str(e) for e in form.preferred_end_time.errors
        ]


def test_propose_form_accepts_in_range_preferred_times(app):
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_REQUEST_FORM_DATA))
        form.set_format_choices()

        form.validate()

        assert not form.preferred_start_time.errors
        assert not form.preferred_end_time.errors


# -------------------------------------------------------------------- #
# TournamentRequestUpdateForm (admin)


def test_admin_request_update_form_rejects_year_1_preferred_start_time(app):
    with app.test_request_context('/'):
        data = _REQUEST_FORM_DATA | {'preferred_start_time': '0001-01-01T00:00'}

        form = TournamentRequestUpdateForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert str(YEAR_RANGE_ERROR_MESSAGE) in [
            str(e) for e in form.preferred_start_time.errors
        ]


def test_admin_request_update_form_accepts_in_range_preferred_times(app):
    with app.test_request_context('/'):
        form = TournamentRequestUpdateForm(MultiDict(_REQUEST_FORM_DATA))
        form.set_format_choices()

        form.validate()

        assert not form.preferred_start_time.errors
        assert not form.preferred_end_time.errors


# -------------------------------------------------------------------- #
# TournamentCreateForm (admin) -- pre-existing start_time, same root cause


def test_admin_tournament_create_form_rejects_year_1_start_time(app):
    with app.test_request_context('/'):
        data = {'name': 'Test Cup', 'start_time': '0001-01-01T00:00'}

        form = TournamentCreateForm(MultiDict(data))
        form.set_contestant_type_choices()
        form.set_game_format_choices()
        form.set_elimination_mode_choices()
        form.set_score_ordering_choices()

        assert form.validate() is False
        assert str(YEAR_RANGE_ERROR_MESSAGE) in [
            str(e) for e in form.start_time.errors
        ]


def test_admin_tournament_create_form_accepts_blank_start_time(app):
    """`start_time` is `Optional()`: leaving it blank must not trip the
    year-range validator (it never runs, per `Optional()`'s
    `StopValidation`)."""
    with app.test_request_context('/'):
        data = {'name': 'Test Cup'}

        form = TournamentCreateForm(MultiDict(data))
        form.set_contestant_type_choices()
        form.set_game_format_choices()
        form.set_elimination_mode_choices()
        form.set_score_ordering_choices()

        form.validate()

        assert not form.start_time.errors


def test_admin_tournament_create_form_accepts_in_range_start_time(app):
    with app.test_request_context('/'):
        data = {'name': 'Test Cup', 'start_time': '2026-06-01T10:00'}

        form = TournamentCreateForm(MultiDict(data))
        form.set_contestant_type_choices()
        form.set_game_format_choices()
        form.set_elimination_mode_choices()
        form.set_score_ordering_choices()

        form.validate()

        assert not form.start_time.errors
