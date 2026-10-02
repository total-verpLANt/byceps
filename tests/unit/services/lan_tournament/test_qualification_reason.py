import pytest

from byceps.services.lan_tournament import tournament_qualification_service
from byceps.services.lan_tournament.tournament_qualification_service import (
    MAX_REASON_LENGTH,
    validate_reason,
)
from byceps.util.result import Err, Ok


# fmt: off
@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('ok\r\nnext',          'ok\nnext'),
        ('ok\rnext',            'ok\nnext'),
        ('ok\nnext',            'ok\nnext'),
        ('  ok\n\nnext \r\n',   'ok\n\nnext'),
        ('a\u200db',            'a\u200db'),
    ],
)
# fmt: on
def test_line_breaks_are_kept_and_normalised(raw, expected):
    assert validate_reason(raw) == Ok(expected)


# fmt: off
@pytest.mark.parametrize(
    'raw',
    [
        '\u200b',
        '\u2800',
        '\u3164',
        '\ufeff',
        '\u034f',
        '\u115f\u1160',
        '\uffa0',
        ' \u200b ',
        '\u200b\n\u200b',
        '\u00a0\u2003',
    ],
)
# fmt: on
def test_reason_without_a_visible_character_is_refused(raw):
    assert validate_reason(raw) == Err('Please give a reason for the decision.')


# fmt: off
@pytest.mark.parametrize(
    'raw',
    [
        'tab\there',
        'nul\x00byte',
        'bell\x07',
        'bidi \u202e override',
        'bidi \u2066 isolate',
        'mark \u200e here',
        'sep \u2028 here',
        'sep \u2029 here',
    ],
)
# fmt: on
def test_tab_and_bidi_controls_stay_refused(raw):
    assert validate_reason(raw) == Err(
        'The reason must not contain control characters.'
    )


def test_length_is_counted_after_normalising():
    at_limit = 'x' * (MAX_REASON_LENGTH - 2) + '\r\n' + 'y'
    assert validate_reason(at_limit) == Ok(at_limit.replace('\r\n', '\n'))
    assert validate_reason('x' * MAX_REASON_LENGTH).is_ok()
    assert validate_reason('x' * (MAX_REASON_LENGTH + 1)) == Err(
        'The reason is too long.'
    )


def test_a_non_string_reason_is_refused():
    assert validate_reason(None) == Err('Please give a reason for the decision.')


def test_the_old_private_name_is_gone():
    assert not hasattr(tournament_qualification_service, '_validate_reason')


def test_unrelease_without_reason_names_the_release():
    result = validate_reason(
        '  ', missing='Please give a reason for taking the release back.'
    )

    assert result == Err('Please give a reason for taking the release back.')
