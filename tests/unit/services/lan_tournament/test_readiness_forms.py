"""Real localized form validation; session token verification is caller-owned."""

from uuid import uuid4

from flask import Flask
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.readiness_csrf import (
    get_readiness_csrf_token,
    validate_readiness_csrf_token,
)
from byceps.services.lan_tournament.blueprints.readiness_forms import (
    MatchReadyClaimForm,
    MatchReadyRevokeForm,
    parse_readiness_revision,
)
from byceps.util.l10n import LocalizedForm


BIGINT_MAX = 9_223_372_036_854_775_807
FORMS = (MatchReadyClaimForm, MatchReadyRevokeForm)
REVISION_FIELDS = ('expected_pairing_generation', 'expected_readiness_revision')


@pytest.fixture
def request_context():
    app = Flask(__name__)
    app.config.update(TESTING=True, LOCALE='en', SECRET_KEY='unit-test-only')
    with app.test_request_context('/'):
        yield uuid4()


def _fields(user_id):
    return MultiDict({
        'csrf_token': get_readiness_csrf_token(user_id),
        'expected_pairing_generation': '3',
        'expected_readiness_revision': '7',
        'side': 'a',
    })


# fmt: off
@pytest.mark.parametrize('raw', [
    None, '', -1, '-1', True, False, 'True', 'False', 1.0, '1.0',
    '1e3', '+1', ' 1', '1 ', 'abc', '١', '１', '0x10',
    BIGINT_MAX + 1, str(BIGINT_MAX + 1), '0' * 20, '9' * 100_000,
])
# fmt: on
def test_revisions_are_bounded_and_bool_refused(raw):
    with pytest.raises(ValueError, match='^invalid_readiness_revision$'):
        parse_readiness_revision(raw)


# fmt: off
@pytest.mark.parametrize(('raw', 'expected'), [
    (0, 0), ('0', 0), (1, 1), ('0001', 1),
    (BIGINT_MAX, BIGINT_MAX), (str(BIGINT_MAX), BIGINT_MAX),
])
# fmt: on
def test_revision_parser_accepts_unsigned_bigint_boundaries(raw, expected):
    parsed = parse_readiness_revision(raw)
    assert parsed == expected
    assert type(parsed) is int


@pytest.mark.parametrize('form_class', FORMS)
@pytest.mark.parametrize('field', REVISION_FIELDS)
# fmt: off
@pytest.mark.parametrize('raw', [
    None, '', '-1', True, False, 'True', '1.0', '1e3', '+1',
    ' 1', '1 ', '١', 'abc', str(BIGINT_MAX + 1), '0' * 20, '9' * 100_000,
])
# fmt: on
def test_real_forms_reject_invalid_revisions(request_context, form_class, field, raw):
    fields = _fields(request_context)
    if raw is None:
        del fields[field]
    else:
        fields[field] = raw
    form = form_class(fields)

    assert isinstance(form, LocalizedForm)
    assert form.validate() is False
    assert field in form.errors


@pytest.mark.parametrize('form_class', FORMS)
@pytest.mark.parametrize('field', REVISION_FIELDS)
@pytest.mark.parametrize('raw', ['0', '0001', str(BIGINT_MAX)])
def test_real_forms_accept_revision_boundaries(request_context, form_class, field, raw):
    fields = _fields(request_context)
    fields[field] = raw
    form = form_class(fields)

    assert form.validate() is True
    assert getattr(form, field).data == int(raw)
    assert type(getattr(form, field).data) is int


@pytest.mark.parametrize('form_class', FORMS)
@pytest.mark.parametrize('field', REVISION_FIELDS)
def test_real_forms_refuse_duplicate_revision_fields(request_context, form_class, field):
    fields = _fields(request_context)
    fields.add(field, fields[field])
    form = form_class(fields)

    assert form.validate() is False
    assert field in form.errors


@pytest.mark.parametrize('form_class', FORMS)
@pytest.mark.parametrize('field', REVISION_FIELDS)
@pytest.mark.parametrize('raw', [True, False])
def test_prefilled_revision_data_refuses_bools(request_context, form_class, field, raw):
    form = form_class(data={field: raw})
    assert form.validate() is False
    assert field in form.errors


def test_revoke_form_has_same_fields_as_claim(request_context):
    fields = _fields(request_context)
    claim = MatchReadyClaimForm(fields)
    revoke = MatchReadyRevokeForm(fields)

    assert [field.name for field in revoke] == [field.name for field in claim]
    assert not hasattr(revoke, 'reason')
    assert revoke.validate() is True


@pytest.mark.parametrize('form_class', (MatchReadyClaimForm, MatchReadyRevokeForm))
@pytest.mark.parametrize('side', [None, '', 'A', 'c', 'ab', 'a' * 100_000])
def test_side_is_required_and_typed(request_context, form_class, side):
    fields = _fields(request_context)
    if side is None:
        del fields['side']
    else:
        fields['side'] = side
    form = form_class(fields)
    assert form.validate() is False
    assert 'side' in form.errors


@pytest.mark.parametrize('form_class', (MatchReadyClaimForm, MatchReadyRevokeForm))
@pytest.mark.parametrize('side', ['a', 'b'])
def test_both_logical_sides_validate(request_context, form_class, side):
    fields = _fields(request_context)
    fields['side'] = side
    form = form_class(fields)
    assert form.validate() is True
    assert form.side.data == side


@pytest.mark.parametrize('form_class', FORMS)
@pytest.mark.parametrize('token', [None, '', 'x' * 44, 'x' * 100_000])
def test_form_token_is_required_and_bounded(request_context, form_class, token):
    fields = _fields(request_context)
    if token is None:
        del fields['csrf_token']
    else:
        fields['csrf_token'] = token
    form = form_class(fields)
    assert form.validate() is False
    assert 'csrf_token' in form.errors


@pytest.mark.parametrize('form_class', FORMS)
def test_form_validation_is_not_session_csrf_verification(request_context, form_class):
    fields = _fields(request_context)
    real_token = fields['csrf_token']
    assert validate_readiness_csrf_token(real_token, request_context).is_ok()
    fields['csrf_token'] = 'x' * 43
    form = form_class(fields)

    # Plain WTForms validates shape only; native callers must verify binding.
    assert form.validate() is True
    assert validate_readiness_csrf_token(form.csrf_token.data, request_context).is_err()
    assert validate_readiness_csrf_token(real_token, uuid4()).is_err()
