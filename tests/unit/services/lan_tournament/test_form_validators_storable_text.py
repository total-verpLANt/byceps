"""
tests.unit.services.lan_tournament.test_form_validators_storable_text
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

PostgreSQL rejects NUL in text columns, so a free-text form field must
refuse it (and an unpaired surrogate) before any query runs.
"""

from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict
from wtforms import SelectField
from wtforms.validators import ValidationError

from byceps.services.lan_tournament.blueprints.admin import forms as admin_forms
from byceps.services.lan_tournament.blueprints.site import forms as site_forms
from byceps.services.lan_tournament.form_validators import storable_text
from byceps.services.lan_tournament.tournament_config_domain_service import (
    UNSTORABLE_CHARACTER_ERROR,
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


def _field(data):
    return SimpleNamespace(data=data)


# fmt: off
@pytest.mark.parametrize(
    'value',
    [
        'A\x00B',
        '\x00',
        'A\ud800B',
        '\udfff',
    ],
)
# fmt: on
def test_validator_refuses_unstorable_text(app, value):
    with app.test_request_context('/'):
        with pytest.raises(ValidationError) as excinfo:
            storable_text(None, _field(value))

        assert str(excinfo.value) == UNSTORABLE_CHARACTER_ERROR


# fmt: off
@pytest.mark.parametrize(
    'value',
    [
        None,
        '',
        'Team Rocket',
        'Zeile eins\nZeile zwei\tTab',
        'Grüße aus Köln ✓',
        '\U0001f600',
    ],
)
# fmt: on
def test_validator_accepts_storable_text(value):
    assert storable_text(None, _field(value)) is None


# fmt: off
_FIELDS = [
    (admin_forms.TournamentCreateForm, 'name'),
    (admin_forms.TournamentCreateForm, 'game'),
    (admin_forms.TournamentCreateForm, 'description'),
    (admin_forms.TournamentCreateForm, 'image_url'),
    (admin_forms.TournamentCreateForm, 'ruleset'),
    (admin_forms.TournamentCreateForm, 'image_alt_text'),
    (admin_forms.TournamentUpdateForm, 'name'),
    (admin_forms.TournamentUpdateForm, 'game'),
    (admin_forms.TournamentUpdateForm, 'description'),
    (admin_forms.TournamentUpdateForm, 'image_url'),
    (admin_forms.TournamentUpdateForm, 'ruleset'),
    (admin_forms.TournamentImportForm, 'image_alt_text'),
    (admin_forms.TeamCreateForm, 'name'),
    (admin_forms.TeamCreateForm, 'tag'),
    (admin_forms.TeamCreateForm, 'description'),
    (admin_forms.TeamCreateForm, 'image_url'),
    (admin_forms.TeamCreateForm, 'join_code'),
    (admin_forms.TeamUpdateForm, 'name'),
    (admin_forms.TeamUpdateForm, 'tag'),
    (admin_forms.TeamUpdateForm, 'description'),
    (admin_forms.TeamUpdateForm, 'image_url'),
    (admin_forms.TeamUpdateForm, 'join_code'),
    (admin_forms.TournamentOrgaAssignForm, 'screen_name'),
    (admin_forms.TournamentOrgaAssignForm, 'duties'),
    (admin_forms.MatchCorrectionForm, 'reason'),
    (admin_forms.MatchUnconfirmForm, 'reason'),
    (admin_forms.HighscoreSubmitForm, 'note'),
    (site_forms.SiteTeamCreateForm, 'name'),
    (site_forms.SiteTeamCreateForm, 'tag'),
    (site_forms.SiteTeamCreateForm, 'description'),
    (site_forms.SiteTeamCreateForm, 'join_code'),
    (site_forms.SiteTeamUpdateForm, 'name'),
    (site_forms.SiteTeamUpdateForm, 'tag'),
    (site_forms.SiteTeamUpdateForm, 'description'),
    (site_forms.SiteTeamUpdateForm, 'join_code'),
    (site_forms.HighscoreSubmitForm, 'note'),
    (site_forms.MatchCommentForm, 'comment'),
    (site_forms.OrgaMatchUnconfirmForm, 'reason'),
    (site_forms.OrgaMatchCorrectionForm, 'reason'),
]
# fmt: on

_FIELD_IDS = [
    f'{form.__module__.split(".")[-2]}.{form.__name__}.{name}'
    for form, name in _FIELDS
]


def _validate(form_class, field_name, value):
    form = form_class(MultiDict({field_name: value}))
    for field in form:
        if isinstance(field, SelectField) and field.choices is None:
            field.choices = []
    form.validate()
    return [str(error) for error in getattr(form, field_name).errors]


@pytest.mark.parametrize(
    ('form_class', 'field_name'),
    _FIELDS,
    ids=_FIELD_IDS,
)
def test_field_refuses_nul(app, form_class, field_name):
    with app.test_request_context('/'):
        errors = _validate(form_class, field_name, 'A\x00B')

        assert UNSTORABLE_CHARACTER_ERROR in errors


@pytest.mark.parametrize(
    ('form_class', 'field_name'),
    _FIELDS,
    ids=_FIELD_IDS,
)
def test_field_accepts_plain_text(app, form_class, field_name):
    with app.test_request_context('/'):
        errors = _validate(form_class, field_name, 'AB')

        assert UNSTORABLE_CHARACTER_ERROR not in errors


# fmt: off
@pytest.mark.parametrize(
    ('form_class', 'field_name'),
    [
        (admin_forms.TeamCreateForm, 'captain'),
        (admin_forms.AddParticipantForm, 'screen_name'),
        (admin_forms.AddTeamMemberForm, 'screen_name'),
    ],
)
# fmt: on
def test_screen_name_field_refuses_nul_before_the_user_lookup(
    app, form_class, field_name
):
    with app.test_request_context('/'):
        with patch.object(
            admin_forms.user_service,
            'find_user_by_screen_name',
            side_effect=AssertionError('NUL reached the user lookup'),
        ):
            errors = _validate(form_class, field_name, 'A\x00B')

        assert errors
