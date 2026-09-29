"""
tests.unit.services.lan_tournament.test_create_wizard_forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin.forms import (
    MAX_COUNT,
    TournamentCreateForm,
    TournamentUpdateForm,
)


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


_COUNT_FIELDS = [
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
]
_CEILING_FIELDS = [
    *_COUNT_FIELDS,
    'group_size_min',
    'group_size_max',
    'advancement_count',
]


def _set_choices(form):
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()


def _validate(form_class, **data):
    formdata = MultiDict({'name': 'Cup', **data})
    form = form_class(formdata)
    _set_choices(form)
    form.validate()
    return form


def _errors(form, field_name):
    return [str(e) for e in getattr(form, field_name).errors]


def test_name_whitespace_only_is_rejected(app):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, name='   ')

        assert _errors(form, 'name') == ['Please enter a name.']


def test_name_is_stripped(app):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, name='  Cup  ')

        assert form.name.data == 'Cup'
        assert not form.name.errors


@pytest.mark.parametrize('field_name', _COUNT_FIELDS)
@pytest.mark.parametrize('value', ['0', '-1'])
def test_count_below_one_is_rejected(app, field_name, value):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: value})

        assert _errors(form, field_name) == ['Whole numbers from 1 only.']


@pytest.mark.parametrize('field_name', _COUNT_FIELDS)
@pytest.mark.parametrize('value', ['1e2', 'abc'])
def test_count_non_integer_is_rejected(app, field_name, value):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: value})

        assert _errors(form, field_name) == ['Whole numbers from 1 only.']


@pytest.mark.parametrize('field_name', _CEILING_FIELDS)
@pytest.mark.parametrize('value', ['1025', '3000000000'])
def test_count_above_ceiling_is_rejected(app, field_name, value):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: value})

        assert _errors(form, field_name) == ['At most 1024.']


def test_count_ceiling_itself_is_accepted(app):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, max_players=str(MAX_COUNT))

        assert not form.max_players.errors


@pytest.mark.parametrize('field_name', _CEILING_FIELDS)
def test_count_with_400_digits_is_a_field_error_not_a_crash(app, field_name):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: '9' * 400})

        assert form.errors.get(field_name)


# fmt: off
@pytest.mark.parametrize(
    ('min_field', 'max_field', 'min_label'),
    [
        ('min_players',         'max_players',         'Min. players'),
        ('min_teams',           'max_teams',           'Min. teams'),
        ('min_players_in_team', 'max_players_in_team', 'Min. players per team'),
    ],
)
# fmt: on
def test_min_greater_than_max_errors_on_max_field(
    app, min_field, max_field, min_label
):
    with app.test_request_context('/'):
        form = _validate(
            TournamentCreateForm, **{min_field: '8', max_field: '4'}
        )

        assert _errors(form, max_field) == [
            f'Must be at least "{min_label}" (8).'
        ]
        assert not getattr(form, min_field).errors


def test_min_equal_to_max_is_accepted(app):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, min_teams='4', max_teams='4')

        assert not form.max_teams.errors


def test_update_form_also_rejects_min_greater_than_max(app):
    with app.test_request_context('/'):
        form = _validate(TournamentUpdateForm, min_players='8', max_players='4')

        assert _errors(form, 'max_players') == [
            'Must be at least "Min. players" (8).'
        ]


def test_create_form_image_url_rejects_non_http_scheme(app):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, image_url='ftp://x/y')

        assert _errors(form, 'image_url') == [
            (
                'Only complete http or https addresses, '
                'e.g. https://example.org/image.png'
            )
        ]


def test_create_form_image_url_accepts_https(app):
    with app.test_request_context('/'):
        form = _validate(
            TournamentCreateForm, image_url='https://example.org/a.png'
        )

        assert not form.image_url.errors


def test_update_form_accepts_relative_served_image_path(app):
    with app.test_request_context('/'):
        form = _validate(
            TournamentUpdateForm,
            image_url='/data/parties/p/lan_tournament/images/x.png',
        )

        assert not form.image_url.errors


def test_image_alt_text_max_200(app):
    with app.test_request_context('/'):
        ok = _validate(TournamentCreateForm, image_alt_text='a' * 200)
        too_long = _validate(TournamentCreateForm, image_alt_text='a' * 201)

        assert not ok.image_alt_text.errors
        assert too_long.image_alt_text.errors


def test_create_form_has_wizard_hidden_fields(app):
    with app.test_request_context('/'):
        form = TournamentCreateForm()

        for field_name in ('submission_token', 'image_id', 'from_request_id'):
            assert 'type="hidden"' in str(getattr(form, field_name)())


@pytest.mark.parametrize('field_name', ['description', 'ruleset'])
def test_text_field_length_counts_crlf_as_one_character(app, field_name):
    raw = '\r\n'.join(['x' * 49] * 200)
    assert len(raw) == 10198

    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: raw})

        assert _errors(form, field_name) == []
        assert '\r' not in getattr(form, field_name).data
        assert len(getattr(form, field_name).data) == 9999


@pytest.mark.parametrize('field_name', ['description', 'ruleset'])
def test_text_field_over_limit_after_crlf_normalisation_is_rejected(
    app, field_name
):
    raw = '\r\n'.join(['x' * 49] * 200) + 'xy'

    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: raw})

        assert _errors(form, field_name) == [
            'At most 10000 characters – currently 10001.'
        ]


@pytest.mark.parametrize('field_name', ['description', 'ruleset'])
def test_text_field_lone_cr_is_normalised_to_lf(app, field_name):
    with app.test_request_context('/'):
        form = _validate(TournamentCreateForm, **{field_name: 'a\rb\r\nc'})

        assert getattr(form, field_name).data == 'a\nb\nc'
