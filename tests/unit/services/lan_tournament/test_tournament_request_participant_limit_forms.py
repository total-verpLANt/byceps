"""
tests.unit.services.lan_tournament.test_tournament_request_participant_limit_forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

D1: an INTEGER-overflowing `participant_limit` (e.g. from a bypassed
client-side cap) must be rejected by both the site propose form and
the admin request-update form, not just by the domain service.

I1: a 309+ digit `IntegerField` value must be rejected by
`form.validate()`, not crash it. Stock `wtforms.validators.NumberRange`
calls `math.isnan(data)` unconditionally, which raises `OverflowError`
for an `int` too large to convert to a `float` (roughly >= 2**1024,
~309 decimal digits) instead of returning a bool; `IntegerField`
itself parses up to 4300 digits. `SafeNumberRange`
(`form_validators.py`) is the fix.
"""

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
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
    MAX_PARTICIPANT_LIMIT,
)


_OVERFLOWING_LIMIT = '3000000000'


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


_BASE_FORM_DATA = {
    'name': 'Test Cup',
    'game': 'Test Game',
    'game_format': GameFormat.ONE_V_ONE.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '1',
    'preferred_start_time': '2026-06-01T10:00',
    'preferred_end_time': '2026-06-01T12:00',
    'description': 'A description.',
}


def test_propose_form_rejects_overflowing_participant_limit(app):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {'participant_limit': _OVERFLOWING_LIMIT}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.participant_limit.errors


def test_propose_form_accepts_participant_limit_at_the_cap(app):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {
            'participant_limit': str(MAX_PARTICIPANT_LIMIT)
        }

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        form.validate()

        assert not form.participant_limit.errors


def test_admin_request_update_form_rejects_overflowing_participant_limit(app):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {'participant_limit': _OVERFLOWING_LIMIT}

        form = TournamentRequestUpdateForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.participant_limit.errors


def test_admin_request_update_form_accepts_participant_limit_at_the_cap(app):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {
            'participant_limit': str(MAX_PARTICIPANT_LIMIT)
        }

        form = TournamentRequestUpdateForm(MultiDict(data))
        form.set_format_choices()

        form.validate()

        assert not form.participant_limit.errors


# -------------------------------------------------------------------- #
# I1 -- 309/4300-digit ints must not crash NumberRange/SafeNumberRange


_309_DIGITS = '9' * 309
_4300_DIGITS = '9' * 4300


# fmt: off
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('participant_limit', _309_DIGITS, id='participant_limit_309'),
        pytest.param('participant_limit', _4300_DIGITS, id='participant_limit_4300'),
        pytest.param('team_size', _309_DIGITS, id='team_size_309'),
        pytest.param('team_size', _4300_DIGITS, id='team_size_4300'),
    ],
)
# fmt: on
def test_propose_form_rejects_huge_int_without_raising(app, field, value):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {field: value}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert getattr(form, field).errors


# fmt: off
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('participant_limit', _309_DIGITS, id='participant_limit_309'),
        pytest.param('participant_limit', _4300_DIGITS, id='participant_limit_4300'),
        pytest.param('team_size', _309_DIGITS, id='team_size_309'),
        pytest.param('team_size', _4300_DIGITS, id='team_size_4300'),
    ],
)
# fmt: on
def test_admin_request_update_form_rejects_huge_int_without_raising(
    app, field, value
):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {field: value}

        form = TournamentRequestUpdateForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert getattr(form, field).errors


def test_propose_form_participant_limit_still_renders_max_attribute(app):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {'participant_limit': '5'}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.participant_limit.flags.max == MAX_PARTICIPANT_LIMIT
        assert f'max="{MAX_PARTICIPANT_LIMIT}"' in str(form.participant_limit())


# fmt: off
@pytest.mark.parametrize(
    ('participant_limit', 'expect_ok'),
    [
        (str(MAX_PARTICIPANT_LIMIT), True),
        (str(MAX_PARTICIPANT_LIMIT + 1), False),
    ],
)
# fmt: on
def test_propose_form_participant_limit_boundary_unchanged(
    app, participant_limit, expect_ok
):
    with app.test_request_context('/'):
        data = _BASE_FORM_DATA | {'participant_limit': participant_limit}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        form.validate()

        assert (not form.participant_limit.errors) is expect_ok
