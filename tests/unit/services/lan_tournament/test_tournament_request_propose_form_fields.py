"""
tests.unit.services.lan_tournament.test_tournament_request_propose_form_fields
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

`TournamentProposeForm` (site) field fixes:

- `notes` gets its own `id` (`request_notes`), so it no longer collides
  with the homepage's `#notes` section, which the theme hides at
  `<= 720px`.
- A whitespace-only `name`/`game`/`description` fails validation
  instead of passing `InputRequired` (which only checks that a raw
  value was posted, not that it is useful).
- `preferred_end_time` before `preferred_start_time` is now a
  form-level error, reported in the same round as every other field
  error, not just a service-level one discovered on a second submit.
- The `participant_limit` below-min message never repeats the raw
  `MAX_PARTICIPANT_LIMIT` constant.
- `set_format_choices` exposes draft-ready `game_format_options` and
  per-mode `description` metadata for the bote override (Issue 7) to
  render instead of the raw enum labels.
"""

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

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
    'game_format': GameFormat.ONE_V_ONE.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '1',
    'participant_limit': '8',
    'preferred_start_time': '2026-06-01T10:00',
    'preferred_end_time': '2026-06-01T12:00',
    'description': 'A description.',
}


def test_notes_field_id_is_request_notes(app):
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_VALID_FORM_DATA))

        assert form.notes.id == 'request_notes'
        assert form.notes.name == 'notes'


def test_notes_label_is_notes_for_the_orga(app):
    """Issue 12: the request-specific label, not the shared "Notes"
    msgid used elsewhere in the module."""
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_VALID_FORM_DATA))

        assert form.notes.label.text == 'Notes for the orga'


def test_name_game_description_keep_the_required_flag(app):
    """A2 (Issue 6): `_required_stripped` replaced `InputRequired` as
    the emptiness check for `name`/`game`/`description`, but a plain
    function doesn't set `field_flags` the way a validator instance
    does -- these three fields silently lost the HTML `required`
    attribute (and its `aria-required` semantics) until it was added
    back explicitly."""
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_VALID_FORM_DATA))

        assert form.name.flags.required is True
        assert form.game.flags.required is True
        assert form.description.flags.required is True
        assert 'required' in str(form.name())


def test_whitespace_name_fails_validation(app):
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'name': '   '}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.name.errors


def test_whitespace_description_fails_validation(app):
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'description': '   '}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.description.errors


def test_end_before_start_is_a_form_error(app):
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {
            'preferred_start_time': '2026-06-01T12:00',
            'preferred_end_time': '2026-06-01T10:00',
        }

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert 'End is before the start (12:00)' in [
            str(e) for e in form.preferred_end_time.errors
        ]


def test_end_before_start_reports_time(app):
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {
            'preferred_start_time': '2026-06-01T14:00',
            'preferred_end_time': '2026-06-01T10:00',
        }

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert [str(e) for e in form.preferred_end_time.errors] == [
            'End is before the start (14:00)'
        ]


def test_blank_name_reports_required_field(app):
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'name': ''}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert [str(e) for e in form.name.errors] == ['Required field']


def test_error_summary_phrases(app):
    """Each field error maps to the draft's short summary phrase."""
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {
            'name': '',
            'participant_limit': '1',
            'preferred_start_time': '2026-06-01T12:00',
            'preferred_end_time': '2026-06-01T10:00',
        }

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.error_summary() == [
            ('name', 'Tournament name', 'is missing'),
            ('participant_limit', 'Participant limit', 'is too small'),
            ('preferred_end_time', 'Preferred end', 'is before the start'),
        ]


def test_mode_options_use_request_vocabulary(app):
    """The elimination-mode options use the request flow's labels."""
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'game_format': GameFormat.FREE_FOR_ALL.value}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        labels = {
            option['value']: str(option['label'])
            for option in form.elimination_mode_options
        }
        assert labels == {
            EliminationMode.SINGLE_ELIMINATION.value: 'Single knockout',
            EliminationMode.DOUBLE_ELIMINATION.value: 'Double knockout',
            EliminationMode.ROUND_ROBIN.value: 'Everyone plays everyone',
            EliminationMode.NONE.value: 'No knockout',
        }

        round_robin_option = next(
            option
            for option in form.elimination_mode_options
            if option['value'] == EliminationMode.ROUND_ROBIN.value
        )
        assert str(round_robin_option['reason']) == 'only One on one'


def test_all_three_draft_errors_reported_in_one_round(app):
    """Draft A3: a single submit surfaces every field-level error at
    once -- not one round of a whitespace name, then a second round
    for the limit, then a third for the swapped times.
    """
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {
            'name': '   ',
            'participant_limit': '1',
            'preferred_start_time': '2026-06-01T12:00',
            'preferred_end_time': '2026-06-01T10:00',
        }

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.name.errors
        assert form.participant_limit.errors
        assert form.preferred_end_time.errors


def test_participant_limit_message_does_not_mention_1024(app):
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'participant_limit': '1'}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.participant_limit.errors
        assert not any(
            '1024' in str(error) for error in form.participant_limit.errors
        )


def test_participant_limit_below_min_says_enter_at_least(app):
    """F3 (Issue 6): a below-`min` value gets the "Enter at least"
    message, naming the real minimum -- interpolated, not the raw
    `%(min)s` placeholder (fix 3, workspace-pv3b.23)."""
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'participant_limit': '1'}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.participant_limit.errors == ['Enter at least 2.']


def test_participant_limit_above_max_says_limit_too_high(app):
    """F3 (Issue 6): a value above the form's max (5000, the bug's own
    repro) must get a distinct, fully-interpolated message -- not
    "Enter at least 2." (the below-min message), never the raw 1024
    cap, and never a surviving `%(...)s` placeholder (fix 3,
    workspace-pv3b.23)."""
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'participant_limit': '5000'}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.participant_limit.errors == ['This limit is too high.']


def test_participant_limit_validator_keeps_min_and_max_attributes(app):
    """F3 (Issue 6): the fix must stay a single validator object on
    the field, still exposing `.min`/`.max` -- `propose_form.html`
    reads `.max` off it via `selectattr('max', 'defined')` to render
    `data-max-limit`."""
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_VALID_FORM_DATA))

        range_validators = [
            v
            for v in form.participant_limit.validators
            if hasattr(v, 'min') and hasattr(v, 'max')
        ]

        assert len(range_validators) == 1
        validator = range_validators[0]
        assert validator.min == 2
        assert validator.max == MAX_PARTICIPANT_LIMIT
        assert form.participant_limit.flags.max == MAX_PARTICIPANT_LIMIT


@pytest.mark.parametrize('raw_value', ['2e1', 'abc', '2.5'])
def test_participant_limit_non_integer_does_not_mention_1024(app, raw_value):
    """G2 (workspace-pv3b.19): a non-integer `participant_limit`
    (Chromium's `<input type=number>` accepts `2e1` even though
    `IntegerField` cannot parse it) must show exactly one error --
    `IntegerField`'s own "Not a valid integer value." -- never the
    range validator's "...between 2 and 1024." fired on top of it."""
    with app.test_request_context('/'):
        data = _VALID_FORM_DATA | {'participant_limit': raw_value}

        form = TournamentProposeForm(MultiDict(data))
        form.set_format_choices()

        assert form.validate() is False
        assert form.participant_limit.errors == ['Not a valid integer value.']
        assert not any(
            '1024' in str(error) for error in form.participant_limit.errors
        )


def test_participant_limit_validator_does_not_mutate_message(app):
    """G2 (workspace-pv3b.19): `_ParticipantLimitRange` is a single
    validator instance, built once at class-body time and shared by
    every `TournamentProposeForm` instance across every request.
    Assigning to `self.message` per call (the old implementation) is
    not thread-safe -- one request's bound-specific message could leak
    into a concurrent request's validation. Validating a below-min
    form and then an above-max form with that same shared instance
    must leave its `.message` untouched."""
    with app.test_request_context('/'):
        below_min_form = TournamentProposeForm(
            MultiDict(_VALID_FORM_DATA | {'participant_limit': '1'})
        )
        below_min_form.set_format_choices()

        validator = next(
            v
            for v in below_min_form.participant_limit.validators
            if hasattr(v, 'min') and hasattr(v, 'max')
        )
        message_before = validator.message

        below_min_form.validate()
        assert validator.message == message_before

        above_max_form = TournamentProposeForm(
            MultiDict(_VALID_FORM_DATA | {'participant_limit': '5000'})
        )
        above_max_form.set_format_choices()

        above_max_validator = next(
            v
            for v in above_max_form.participant_limit.validators
            if hasattr(v, 'min') and hasattr(v, 'max')
        )
        assert above_max_validator is validator

        above_max_form.validate()

        assert validator.message == message_before


def test_game_format_options_have_label_and_subtitle(app):
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_VALID_FORM_DATA))
        form.set_format_choices()

        assert len(form.game_format_options) == len(GameFormat)
        for option in form.game_format_options:
            assert option['value']
            assert str(option['label'])
            assert str(option['subtitle'])


def test_elimination_mode_options_have_description(app):
    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict(_VALID_FORM_DATA))
        form.set_format_choices()

        assert form.elimination_mode_options
        for option in form.elimination_mode_options:
            assert str(option['description'])
