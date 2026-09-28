from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentRequestRejectForm,
    TournamentRequestUpdateForm,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat


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


_VALID_FORM_DATA = {
    'name': 'Test Cup',
    'game': 'Test Game',
    'game_format': GameFormat.FREE_FOR_ALL.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '1',
    'participant_limit': '8',
    'preferred_start_time': '2026-06-01T10:00',
    'preferred_end_time': '2026-06-01T12:00',
    'description': 'A description.',
}


def test_elimination_mode_label_is_tournament_mode(app):
    with app.test_request_context('/'):
        form = TournamentRequestUpdateForm(MultiDict(_VALID_FORM_DATA))

        assert form.elimination_mode.label.text == 'Tournament mode'


def test_admin_update_form_disabled_option_reason(app):
    """Under FFA, Round Robin is disabled and names its valid format."""
    with app.test_request_context('/'):
        form = TournamentRequestUpdateForm(MultiDict(_VALID_FORM_DATA))
        form.set_format_choices()

        options = {
            value: label
            for value, label, _render_kw in form.elimination_mode.choices
        }

        assert options[EliminationMode.ROUND_ROBIN.value] == (
            'Everyone plays everyone (only 1v1)'
        )


def test_admin_update_form_uses_request_vocabulary(app):
    """The elimination-mode options use the request flow's labels."""
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {
            'game_format': GameFormat.ONE_V_ONE.value,
        }
        form = TournamentRequestUpdateForm(MultiDict(data))
        form.set_format_choices()

        options = {
            value: label
            for value, label, _render_kw in form.elimination_mode.choices
        }

        assert options == {
            EliminationMode.SINGLE_ELIMINATION.value: 'Single knockout',
            EliminationMode.DOUBLE_ELIMINATION.value: 'Double knockout',
            EliminationMode.ROUND_ROBIN.value: 'Everyone plays everyone',
            EliminationMode.NONE.value: ('No knockout (only Highscore)'),
        }


@pytest.mark.parametrize(
    ('reason', 'expected_errors'),
    [
        ('x' * 2000, []),
        ('x' * 2001, ['The reason must not exceed 2000 characters.']),
    ],
)
def test_reject_form_names_the_reason_length_limit(
    app, reason, expected_errors
):
    with app.test_request_context('/'):
        form = TournamentRequestRejectForm(MultiDict({'reason': reason}))

        form.validate()

        assert [str(e) for e in form.reason.errors] == expected_errors
