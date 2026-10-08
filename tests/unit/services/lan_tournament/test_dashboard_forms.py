import functools
import pathlib
import re
from uuid import UUID

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po
from flask import Flask
from flask_babel import Babel
from flask_babel.speaklater import LazyString
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    dashboard_config,
    tournament_operational_domain_service as operational,
)
from byceps.services.lan_tournament.blueprints import (
    dashboard_csrf,
    dashboard_forms as forms_module,
)
from byceps.services.lan_tournament.blueprints.dashboard_forms import (
    DashboardAcknowledgementForm,
    DashboardPinForm,
    MAX_COMMENT_LENGTH,
    MAX_RETURN_LENGTH,
    parse_dashboard_episode_id,
    parse_dashboard_pin_state,
    parse_dashboard_revision,
)
from byceps.services.user.models import User
from byceps.util.l10n import LocalizedForm
from byceps.util.uuid import generate_uuid7


INT4_MAX = 2_147_483_647
EPISODE = '0190f7a2-1b2c-7d3e-8f40-123456789abc'
FORMS = (DashboardPinForm, DashboardAcknowledgementForm)

TOO_LONG_MSGID = (
    'Comment is too long: %(count)d of at most 500 characters.'
    ' Nothing was saved.'
)
TOO_LONG_GERMAN = (
    'Kommentar ist zu lang: %(count)d von höchstens 500 Zeichen.'
    ' Nichts wurde gespeichert.'
)
NOT_PLAIN_TEXT_MESSAGE = (
    'Comment contains characters that are not allowed in plain text.'
    ' Nothing was saved.'
)
CSRF_NOTICE_MSGID = (
    'The form check has expired. Please reload the page;'
    ' your draft stays visible.'
)
CSRF_NOTICE_GERMAN = (
    'Die Formularprüfung ist abgelaufen. Bitte die Seite neu laden;'
    ' dein Entwurf bleibt sichtbar.'
)

_ROOT = pathlib.Path(__file__).resolve().parents[4]
_PO_PATH = _ROOT / 'byceps/translations/de/LC_MESSAGES/messages.po'
_SOURCES = (
    _ROOT / 'byceps/services/lan_tournament/blueprints/dashboard_forms.py',
    _ROOT / 'byceps/services/lan_tournament/blueprints/dashboard_csrf.py',
)
_PLACEHOLDER = re.compile(r'%\((\w+)\)[#0 +-]*\d*(?:\.\d+)?[a-zA-Z]')

# fmt: off
_MALFORMED_REVISIONS = (
    None, '', '-1', '+1', ' 1', '1 ', '1\n', '1.0', '1e3', '1_000', '0x10',
    'True', 'False', 'abc', 'None', '\u0661', '\uff11',
    str(INT4_MAX + 1), '0' * 11, '9' * 11, '9' * 5000, '9' * 100_000,
    '\x00', '1\x00',
)
_MALFORMED_EPISODES = (
    None, '', 'abc', 'x' * 100_000, '0' * 36, '-' * 36,
    EPISODE.replace('-', ''), EPISODE.replace('-', '_'),
    '{' + EPISODE + '}', 'urn:uuid:' + EPISODE,
    EPISODE + '\n', ' ' + EPISODE, EPISODE[:-1], EPISODE + '0',
    'g' * 8 + EPISODE[8:], EPISODE.replace('a', '\u0661', 1),
    EPISODE[:10] + '\x00' + EPISODE[11:], 'True',
)
# fmt: on


@pytest.fixture
def request_context():
    app = Flask(__name__)
    app.config.update(TESTING=True, LOCALE='en', SECRET_KEY='unit-test-only')
    Babel(app)
    with app.test_request_context('/'):
        yield


def _ack_fields(**overrides) -> MultiDict:
    fields = MultiDict(
        {
            'csrf_token': 'a' * 43,
            'revision': '3',
            'episode': EPISODE,
            'comment': 'Checked with both teams.',
            'return': 'view=due&page=2',
        }
    )
    return _with(fields, overrides)


def _pin_fields(**overrides) -> MultiDict:
    fields = MultiDict(
        {
            'csrf_token': 'a' * 43,
            'revision': '3',
            'pinned': 'true',
            'return': 'view=due&page=2',
        }
    )
    return _with(fields, overrides)


def _with(fields: MultiDict, overrides: dict) -> MultiDict:
    for name, value in overrides.items():
        key = 'return' if name == 'return_to' else name
        fields.pop(key, None)
        if value is not None:
            fields[key] = value
    return fields


def _fields_for(form_class, **overrides) -> MultiDict:
    make = _pin_fields if form_class is DashboardPinForm else _ack_fields
    return make(**overrides)


def _case(form_class, field, raw):
    label = form_class.__name__.removeprefix('Dashboard')[:3]
    return pytest.param(
        form_class, field, raw, id=f'{label}-{field}-{repr(raw)[:24]}'
    )


def _render(form) -> str:
    return ''.join(str(field()) for field in form)


# --- bounded untrusted input ------------------------------------------------


@pytest.mark.parametrize(
    ('form_class', 'field', 'raw'),
    [
        *(
            _case(form_class, 'revision', raw)
            for form_class in FORMS
            for raw in _MALFORMED_REVISIONS
        ),
        *(
            _case(DashboardAcknowledgementForm, 'episode', raw)
            for raw in _MALFORMED_EPISODES
        ),
    ],
)
def test_versions_and_ids_reject_malformed_and_giant_inputs(
    request_context, form_class, field, raw
):
    form = form_class(_fields_for(form_class, **{field: raw}))

    assert form.validate() is False
    assert field in form.errors
    assert [str(message) for message in form.errors[field]] == [
        'Invalid form data.'
    ]
    assert getattr(form, field).data is None
    # No other field is blamed for it.
    assert set(form.errors) == {field}


# fmt: off
_NON_SCALARS = (
    True, False, -1, 1.0, float('nan'), [], ['1'], {'revision': 1}, b'1', (1,),
    object(), INT4_MAX + 1, 10**30,
)
_FORM_FIELDS = (
    (DashboardPinForm, 'revision'),
    (DashboardPinForm, 'pinned'),
    (DashboardAcknowledgementForm, 'revision'),
    (DashboardAcknowledgementForm, 'episode'),
)
# fmt: on


@pytest.mark.parametrize(
    ('form_class', 'field', 'raw'),
    [
        _case(form_class, field, raw)
        for form_class, field in _FORM_FIELDS
        for raw in _NON_SCALARS
        # A bool is the explicit pin state.
        if not (field == 'pinned' and type(raw) is bool)
    ],
)
def test_prefilled_non_scalar_and_bool_data_is_refused(
    request_context, form_class, field, raw
):
    form = form_class(data={field: raw})

    assert form.validate() is False
    assert field in form.errors
    assert getattr(form, field).data is None


@pytest.mark.parametrize(
    ('form_class', 'field'),
    [
        *_FORM_FIELDS,
        (DashboardAcknowledgementForm, 'comment'),
    ],
    ids=lambda value: getattr(value, '__name__', value),
)
def test_duplicate_values_are_refused(request_context, form_class, field):
    fields = _fields_for(form_class)
    fields.add(field, fields[field])

    form = form_class(fields)

    assert form.validate() is False
    assert field in form.errors


@pytest.mark.parametrize('form_class', FORMS)
@pytest.mark.parametrize('raw', ['0', '0001', '7', str(INT4_MAX)])
def test_revision_boundaries_are_accepted(request_context, form_class, raw):
    form = form_class(_fields_for(form_class, revision=raw))

    assert form.validate() is True
    assert form.revision.data == int(raw)
    assert type(form.revision.data) is int


@pytest.mark.parametrize(
    'raw', [EPISODE, EPISODE.upper(), '00000000-0000-0000-0000-000000000000']
)
def test_episode_id_is_a_uuid_in_canonical_form(request_context, raw):
    form = DashboardAcknowledgementForm(_ack_fields(episode=raw))

    assert form.validate() is True
    assert type(form.episode.data) is UUID
    assert form.episode.data == UUID(raw)


def test_prefilled_typed_data_renders_and_round_trips(request_context):
    episode = UUID(EPISODE)

    ack = DashboardAcknowledgementForm(data={'revision': 4, 'episode': episode})
    pin = DashboardPinForm(data={'revision': 0, 'pinned': False})

    assert (
        ack.revision()
        == '<input id="revision" name="revision" type="hidden" value="4">'
    )
    assert f'value="{EPISODE}"' in str(ack.episode())
    assert 'value="0"' in str(pin.revision())
    assert 'value="false"' in str(pin.pinned())


def test_parsers_refuse_bool_list_dict_and_giant_inputs():
    for raw in (True, False, [1], {'a': 1}, 1.5, None, '9' * 100_000, -1):
        with pytest.raises(ValueError):
            parse_dashboard_revision(raw)
    for raw in (True, [EPISODE], {'a': EPISODE}, None, 'x' * 100_000, 5):
        with pytest.raises(ValueError):
            parse_dashboard_episode_id(raw)
    for raw in (1, 0, None, 'True', 'TRUE', '1', [], ['true'], 'true '):
        with pytest.raises(ValueError):
            parse_dashboard_pin_state(raw)

    assert parse_dashboard_revision(INT4_MAX) == INT4_MAX
    assert parse_dashboard_episode_id(EPISODE) == UUID(EPISODE)
    assert parse_dashboard_pin_state('true') is True
    assert parse_dashboard_pin_state('false') is False


# --- the optional comment ---------------------------------------------------


# fmt: off
@pytest.mark.parametrize(('length', 'valid'), [
    (0, True), (1, True), (499, True), (500, True),
    (501, False), (1_000, False), (100_000, False), (5_000_000, False),
])
# fmt: on
def test_optional_comment_is_bounded(request_context, length, valid):
    text = 'x' * length
    form = DashboardAcknowledgementForm(_ack_fields(comment=text or None))
    if length == 0:
        form = DashboardAcknowledgementForm(_ack_fields(comment=''))

    assert form.validate() is valid

    rendered = str(form.comment())
    assert 'maxlength' not in rendered
    assert 'maxlength' not in _render(form)
    assert not getattr(form.comment.flags, 'maxlength', None)
    if valid:
        assert 'comment' not in form.errors
        assert form.comment.data == (text or None)
    else:
        assert [str(message) for message in form.errors['comment']] == [
            TOO_LONG_MSGID % {'count': length}
        ]
        # The draft is kept, in the data and in the rendered textarea.
        assert form.comment.data == text
        assert text in rendered


def test_comment_bound_is_the_bound_of_the_message():
    assert f'at most {MAX_COMMENT_LENGTH} characters' in TOO_LONG_MSGID
    assert MAX_COMMENT_LENGTH == 500


def test_comment_is_optional(request_context):
    missing = DashboardAcknowledgementForm(_ack_fields(comment=None))

    assert missing.validate() is True
    assert missing.comment.data is None
    assert 'comment' not in missing.errors


# fmt: off
@pytest.mark.parametrize(('raw', 'expected'), [
    ('', None), (' ', None), ('\n', None), ('\t \r\n ', None),
    ('  checked  ', 'checked'),
    ('line one\r\nline two', 'line one\nline two'),
    ('line one\rline two', 'line one\nline two'),
    ('a\n\nb', 'a\n\nb'),
])
# fmt: on
def test_comment_is_normalized(request_context, raw, expected):
    form = DashboardAcknowledgementForm(_ack_fields(comment=raw))

    assert form.validate() is True
    assert form.comment.data == expected


# fmt: off
@pytest.mark.parametrize(('text', 'valid', 'count'), [
    ('a\r\n' * 200 + 'a', True, 401),
    ('a\n' * 250 + 'a', False, 501),
    ('a\r\n' * 250 + 'a', False, 501),
    ('a\r' * 250 + 'a', False, 501),
    ('\U0001F600' * 500, True, 500),
    ('\U0001F600' * 501, False, 501),
])
# fmt: on
def test_comment_bound_counts_normalized_characters(
    request_context, text, valid, count
):
    form = DashboardAcknowledgementForm(_ack_fields(comment=text))

    assert form.validate() is valid
    if not valid:
        assert f'Comment is too long: {count} of' in str(
            form.errors['comment'][0]
        )


# fmt: off
@pytest.mark.parametrize('text', [
    '\x00', 'a\x00b', '\x07', '\x1b[31m', '\x7f', '\x85',
    '\u2028', '\u2029', '\u202e', '\u202a', '\u2066', '\u2069',
    '\ud800', 'ok \u202e txt.exe',
])
# fmt: on
def test_comment_refuses_control_and_bidi_characters(request_context, text):
    # Embedded, because `strip` would drop whitespace-like ones at the edge.
    form = DashboardAcknowledgementForm(_ack_fields(comment=f'a{text}b'))

    assert form.validate() is False
    assert [str(message) for message in form.errors['comment']] == [
        NOT_PLAIN_TEXT_MESSAGE
    ]
    assert 'maxlength' not in str(form.comment())


# fmt: off
@pytest.mark.parametrize('text', [
    'Ärger mit der Größe: ß, ö, ü', 'tab\tseparated', 'a\nb',
    '\U0001F600', '\U0001F468\u200d\U0001F469\u200d\U0001F467',
    '\u05e9\u05dc\u05d5\u05dd', '<b>bold</b> & "quoted"',
])
# fmt: on
def test_comment_accepts_plain_text_of_any_script(request_context, text):
    form = DashboardAcknowledgementForm(_ack_fields(comment=text))

    assert form.validate() is True
    assert form.comment.data == text


def test_comment_is_text_not_markup(request_context):
    markup = '<script>alert(1)</script>'
    form = DashboardAcknowledgementForm(_ack_fields(comment=markup))

    assert form.validate() is True
    assert form.comment.data == markup
    assert '<script>' not in str(form.comment())
    assert '&lt;script&gt;' in str(form.comment())


def test_a_giant_comment_is_refused_without_a_runaway(request_context):
    form = DashboardAcknowledgementForm(
        _ack_fields(comment='y' * 5_000_000 + '\x00')
    )

    assert form.validate() is False
    assert 'at most 500 characters' in str(form.errors['comment'][0])


# --- the explicit pin state -------------------------------------------------


# fmt: off
@pytest.mark.parametrize(('raw', 'expected'), [
    ('true', True), ('false', False),
])
# fmt: on
def test_pin_state_is_explicit(request_context, raw, expected):
    form = DashboardPinForm(_pin_fields(pinned=raw))

    assert form.validate() is True
    assert form.pinned.data is expected
    assert f'value="{raw}"' in str(form.pinned())


# fmt: off
@pytest.mark.parametrize('raw', [
    None, '', '1', '0', 'True', 'False', 'TRUE', 'FALSE', 'on', 'off', 'yes',
    'no', 'toggle', 'pin', 'unpin', ' true', 'true ', 'true\n', 'tru',
    'true,false', 'x' * 100_000,
])
# fmt: on
def test_pin_state_has_no_default_and_no_toggle(request_context, raw):
    # The previous state is offered as data, and the request still has to say
    # what it wants: a missing or unknown value is never read as a toggle or
    # as "unpin".
    for previous in (True, False, None):
        form = DashboardPinForm(
            _pin_fields(pinned=raw), data={'pinned': previous}
        )

        assert form.validate() is False
        assert 'pinned' in form.errors
        assert form.pinned.data is None


@pytest.mark.parametrize('previous', [True, False, None])
@pytest.mark.parametrize('raw', ['true', 'false'])
def test_pin_state_ignores_the_previous_state(request_context, previous, raw):
    form = DashboardPinForm(_pin_fields(pinned=raw), data={'pinned': previous})

    assert form.validate() is True
    assert form.pinned.data is (raw == 'true')


def test_pin_form_without_any_data_is_invalid(request_context):
    form = DashboardPinForm()

    assert form.validate() is False
    assert {'revision', 'pinned'} <= set(form.errors)


# --- carried fields ---------------------------------------------------------


def test_forms_carry_the_documented_hidden_fields(request_context):
    pin = DashboardPinForm(_pin_fields())
    ack = DashboardAcknowledgementForm(_ack_fields())

    assert [field.name for field in pin] == [
        'csrf_token', 'revision', 'return', 'pinned',
    ]  # fmt: skip
    assert [field.name for field in ack] == [
        'csrf_token', 'revision', 'return', 'episode', 'comment',
    ]  # fmt: skip
    assert isinstance(pin, LocalizedForm)
    assert isinstance(ack, LocalizedForm)
    assert pin.return_to.name == 'return'
    assert 'name="return"' in str(ack.return_to())
    assert pin.validate() is True
    assert ack.validate() is True


def test_rendered_forms_carry_no_maxlength_or_required_attribute(
    request_context,
):
    for form in (
        DashboardPinForm(_pin_fields()),
        DashboardAcknowledgementForm(_ack_fields(comment='x' * 501)),
        DashboardPinForm(),
        DashboardAcknowledgementForm(),
    ):
        rendered = _render(form)

        assert 'maxlength' not in rendered
        assert 'required' not in rendered


@pytest.mark.parametrize('form_class', FORMS)
def test_field_labels_are_lazy_localized_strings(request_context, form_class):
    form = form_class()

    for field in form:
        assert isinstance(field.label.text, LazyString), field.name


@pytest.mark.parametrize('form_class', FORMS)
def test_forms_carry_the_csrf_token_without_verifying_it(
    request_context, form_class
):
    user = CurrentUser.create_authenticated(
        User(
            id=generate_uuid7(),
            screen_name='Orga',
            initialized=True,
            suspended=False,
            deleted=False,
            avatar_url='',
        ),
        None,
        frozenset(),
    )
    real_token = dashboard_csrf.get_dashboard_csrf_token(user)

    for token in ('x' * 43, '', None, 'x' * 100_000, real_token):
        form = form_class(_fields_for(form_class, csrf_token=token))
        # Plain WTForms checks shape only; the caller verifies the binding
        # first, so a missing token answers `csrf_invalid`.
        assert form.validate() is True
        assert form.csrf_token.data == token
        assert dashboard_csrf.validate_dashboard_csrf(
            user, form.csrf_token.data
        ).is_ok() is (token == real_token)


@pytest.mark.parametrize('form_class', FORMS)
def test_return_target_is_bounded_and_dropped_not_refused(
    request_context, form_class
):
    fine = 'view=due&sort=urgency&page=2'
    for raw, expected in [
        (fine, fine),
        ('', ''),
        ('a' * MAX_RETURN_LENGTH, 'a' * MAX_RETURN_LENGTH),
        ('a' * (MAX_RETURN_LENGTH + 1), None),
        ('a' * 1_000_000, None),
        (None, None),
    ]:
        form = form_class(_fields_for(form_class, return_to=raw))

        assert form.validate() is True
        assert form.return_to.data == expected


@pytest.mark.parametrize('form_class', FORMS)
def test_return_target_takes_the_first_value_only(request_context, form_class):
    fields = _fields_for(form_class, return_to='view=due')
    fields.add('return', 'https://evil.example/')

    form = form_class(fields)

    assert form.validate() is True
    assert form.return_to.data == 'view=due'


# --- German catalogue -------------------------------------------------------


@functools.cache
def _catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f, locale='de')


def _german(msgid):
    message = _catalog().get(msgid)
    if message is None or message.fuzzy:
        return None
    return message.string or None


def _placeholders(text):
    return set(_PLACEHOLDER.findall(text.replace('%%', '')))


@functools.cache
def _extracted_msgids():
    found = set()
    for path in _SOURCES:
        for _, message, _, _ in extract_from_file(
            'python',
            path,
            keywords={'lazy_gettext': None, 'gettext': None},
        ):
            found.add(message)
    return found


def test_extraction_finds_the_form_and_csrf_msgids():
    assert {
        'Comment (optional)',
        'Invalid form data.',
        'CSRF token',
        CSRF_NOTICE_MSGID,
    } <= _extracted_msgids()


def test_form_and_csrf_msgids_have_german_with_the_same_placeholders():
    missing = []
    wrong_placeholders = []
    for msgid in sorted(_extracted_msgids()):
        german = _german(msgid)
        if german is None:
            missing.append(msgid)
        elif _placeholders(german) != _placeholders(msgid):
            wrong_placeholders.append(msgid)

    assert missing == []
    assert wrong_placeholders == []


@pytest.mark.parametrize(
    ('msgid', 'german'),
    [
        ('Comment (optional)', 'Kommentar (optional)'),
        (TOO_LONG_MSGID, TOO_LONG_GERMAN),
        (CSRF_NOTICE_MSGID, CSRF_NOTICE_GERMAN),
    ],
    ids=['label', 'too-long', 'csrf-notice'],
)
def test_design_copy_is_the_german_translation(msgid, german):
    assert _german(msgid) == german


def test_the_csrf_notice_is_the_design_copy_msgid():
    assert str(dashboard_csrf.CSRF_INVALID_NOTICE._args[0]) == CSRF_NOTICE_MSGID


# fmt: off
@pytest.mark.parametrize('code', [
    dashboard_config.INVALID_YELLOW_MINUTES_ERROR,
    dashboard_config.INVALID_RED_MINUTES_ERROR,
    dashboard_config.INVALID_THRESHOLD_ORDER_ERROR,
    dashboard_config.INVALID_POLL_SECONDS_ERROR,
    dashboard_config.INVALID_PAGE_SIZE_ERROR,
    operational.UNSUPPORTED_STARTED_CREATION_ERROR,
    operational.UNSUPPORTED_STATUS_TRANSITION_ERROR,
])
# fmt: on
def test_service_error_codes_of_this_wave_have_german(code):
    german = _german(code)

    assert german is not None
    assert german != code
    assert not _placeholders(german)


def test_german_copy_of_the_wave_uses_the_status_names_of_the_catalogue():
    started = _german(operational.UNSUPPORTED_STARTED_CREATION_ERROR)
    transition = _german(operational.UNSUPPORTED_STATUS_TRANSITION_ERROR)

    for status in ('Laufend', 'Pausiert', 'Abgeschlossen'):
        assert status in started
    for status in ('Laufend', 'Anmeldung geschlossen', 'Pausiert'):
        assert status in transition
    assert _german('Ongoing') == 'Laufend'
    assert _german('Paused') == 'Pausiert'
    assert _german('Completed') == 'Abgeschlossen'
    assert _german('Registration closed') == 'Anmeldung geschlossen'


def test_form_module_exports_the_bounds():
    assert forms_module.MAX_REVISION == INT4_MAX
    assert MAX_RETURN_LENGTH == 1024
