"""
tests.unit.services.lan_tournament.test_tournament_request_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone, UTC

import pytest

from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.tournament_request_domain_service import (
    allowed_elimination_modes,
    analyze_field_gap,
    contains_disallowed_control_char,
    is_stale_accepted,
    is_within_service_datetime_bounds,
    is_year_in_range,
    MAX_YEAR,
    max_participant_limit,
    MIN_YEAR,
    normalize_datetime_to_utc,
    normalize_optional_text,
    STALE_ACCEPTED_AFTER,
    validate_request_fields,
    YEAR_RANGE_ERROR_MESSAGE,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


def _make_request(
    *,
    game_format: GameFormat = GameFormat.ONE_V_ONE,
    elimination_mode: EliminationMode = EliminationMode.SINGLE_ELIMINATION,
):
    return TournamentRequest(
        id=TournamentRequestID(generate_uuid()),
        party_id=PartyID('lan-2025'),
        number=1,
        proposer_id=UserID(generate_uuid()),
        created_at=NOW,
        status=TournamentRequestStatus.submitted,
        name='Casual Cup',
        game='Some Game',
        game_format=game_format,
        elimination_mode=elimination_mode,
        team_size=1,
        participant_limit=16,
        preferred_start_time=NOW,
        preferred_end_time=NOW,
        description='A friendly bracket.',
    )


# -------------------------------------------------------------------- #
# allowed_elimination_modes


def test_allowed_modes_marks_round_robin_invalid_for_ffa():
    pairs = allowed_elimination_modes(GameFormat.FREE_FOR_ALL)

    reasons = dict(pairs)
    assert reasons[EliminationMode.ROUND_ROBIN] == 'only_one_v_one'


def test_allowed_modes_marks_none_valid_only_for_highscore():
    for game_format in (GameFormat.ONE_V_ONE, GameFormat.FREE_FOR_ALL):
        reasons = dict(allowed_elimination_modes(game_format))
        assert reasons[EliminationMode.NONE] == 'only_highscore'

    highscore_reasons = dict(allowed_elimination_modes(GameFormat.HIGHSCORE))
    assert highscore_reasons[EliminationMode.NONE] is None


@pytest.mark.parametrize(
    'game_format',
    [GameFormat.ONE_V_ONE, GameFormat.FREE_FOR_ALL, GameFormat.HIGHSCORE],
)
def test_allowed_modes_returns_every_mode(game_format):
    pairs = allowed_elimination_modes(game_format)

    assert {mode for mode, _reason in pairs} == set(EliminationMode)


# -------------------------------------------------------------------- #
# max_participant_limit


# fmt: off
@pytest.mark.parametrize(
    ('party_capacity', 'team_size', 'expected'),
    [
        (100,  1, 100),
        (100,  4,  25),
        (101,  4,  25),
        (  7,  8,   0),
    ],
)
# fmt: on
def test_max_participant_limit_divides_capacity_by_team_size(
    party_capacity, team_size, expected
):
    assert max_participant_limit(party_capacity, team_size) == expected


def test_max_participant_limit_is_none_without_capacity():
    assert max_participant_limit(None, 4) is None


def test_max_participant_limit_guards_against_non_positive_team_size():
    assert max_participant_limit(100, 0) is None
    assert max_participant_limit(100, -1) is None


# -------------------------------------------------------------------- #
# validate_request_fields

_VALID_KWARGS = {
    'name': 'Casual Cup',
    'game': 'Some Game',
    'team_size': 1,
    'participant_limit': 16,
    'party_capacity': None,
    'preferred_start_time': NOW,
    'preferred_end_time': NOW,
    'description': 'A friendly bracket.',
    'game_format': GameFormat.ONE_V_ONE,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
}


def test_validate_accepts_well_formed_request():
    result = validate_request_fields(**_VALID_KWARGS)

    assert result.is_ok()


def test_validate_rejects_name_over_80_chars():
    kwargs = _VALID_KWARGS | {'name': 'x' * 81}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Request name must not exceed 80 characters.'


# fmt: off
@pytest.mark.parametrize('team_size', [0, -1, 65])
# fmt: on
def test_validate_rejects_team_size_out_of_range(team_size):
    kwargs = _VALID_KWARGS | {'team_size': team_size}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Team size must be between 1 and 64.'


def test_validate_rejects_participant_limit_below_two():
    kwargs = _VALID_KWARGS | {'participant_limit': 1}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Participant limit must be at least 2.'


def test_validate_rejects_limit_above_party_capacity():
    kwargs = _VALID_KWARGS | {
        'team_size': 4,
        'participant_limit': 30,
        'party_capacity': 100,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Participant limit exceeds party capacity.'


def test_validate_accepts_limit_at_party_capacity_boundary():
    kwargs = _VALID_KWARGS | {
        'team_size': 4,
        'participant_limit': 25,
        'party_capacity': 100,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


def test_validate_rejects_inverted_period():
    kwargs = _VALID_KWARGS | {
        'preferred_start_time': datetime(2025, 6, 15, 14, 0, 0),
        'preferred_end_time': datetime(2025, 6, 15, 13, 0, 0),
    }

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Preferred end time must not precede start time.'
    )


def test_validate_rejects_empty_description():
    kwargs = _VALID_KWARGS | {'description': '   '}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Description must not be empty.'


def test_validate_rejects_invalid_format_combination():
    kwargs = _VALID_KWARGS | {
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': EliminationMode.ROUND_ROBIN,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Invalid combination of game format and elimination mode.'
    )


# -------------------------------------------------------------------- #
# validate_request_fields -- name/game emptiness and control characters


# fmt: off
@pytest.mark.parametrize('name', ['', '   '])
# fmt: on
def test_validate_rejects_blank_name(name):
    kwargs = _VALID_KWARGS | {'name': name}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Request name must not be empty.'


# fmt: off
@pytest.mark.parametrize(
    ('label', 'name'),
    [
        ('zwsp', '\u200b'),
        ('bom', '\ufeff'),
        ('double_word_joiner', '\u2060\u2060'),
    ],
)
# fmt: on
def test_validate_rejects_blank_looking_name(label, name):
    """A name of only zero-width/format (`Cf`) chars is still blank."""
    kwargs = _VALID_KWARGS | {'name': name}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Request name must not be empty.'


# fmt: off
@pytest.mark.parametrize(
    ('label', 'name'),
    [
        # U+3164 HANGUL FILLER: category `Lo` (letter), renders blank
        # in most fonts. Caught only by a per-code-point denylist, not
        # by the general L/N/P/S category check -- rejecting all of
        # `Lo` would also reject every legitimate CJK/Hangul name.
        ('hangul_filler', '\u3164'),
        # U+2800 BRAILLE PATTERN BLANK: category `So` (symbol), same
        # reasoning.
        ('braille_blank', '\u2800'),
    ],
)
# fmt: on
def test_validate_accepts_blank_rendering_chars_in_visible_category(
    label, name
):
    kwargs = _VALID_KWARGS | {'name': name}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# fmt: off
@pytest.mark.parametrize(
    ('label', 'name'),
    [
        ('crlf',      'Cup\r\nBcc: x'),
        ('lf',        'Cup\nBcc: x'),
        ('nul',       'Cup\x00x'),
        ('tab',       'Cup\tx'),
        ('line_sep',  'Cup x'),
        ('para_sep',  'Cup x'),
        ('rlo_spoof', 'Cup\u202ex'),
        ('lri_spoof', 'Cup\u2066x'),
        ('lrm_spoof', 'Cup\u200ex'),
        ('rlm_spoof', 'Cup\u200fx'),
        ('alm_spoof', 'Cup\u061cx'),
    ],
)
# fmt: on
def test_validate_rejects_name_with_control_or_line_separator_chars(
    label, name
):
    kwargs = _VALID_KWARGS | {'name': name}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Request name must not contain line breaks or control characters.'
    )


def test_validate_accepts_name_with_a_plain_space():
    kwargs = _VALID_KWARGS | {'name': 'a b'}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# fmt: off
@pytest.mark.parametrize(
    ('label', 'name'),
    [
        # ZWJ (U+200D) joining a flag-sequence emoji.
        ('zwj_rainbow_flag',
         'Rainbow 🏳️‍🌈 Cup'),
        # ZWJ (U+200D) joining a ZWJ-sequence emoji.
        ('zwj_man_technologist',
         '👨‍💻 Hackers'),
        # ZWNJ (U+200C), required by Persian orthography.
        ('zwnj_persian',
         'می‌خواهم'),
        # Soft hyphen (U+00AD), a legitimate optional-break marker.
        ('soft_hyphen', 'Snow­board Cup'),
        # Cn: unassigned in the running interpreter's `unicodedata`,
        # as a newer emoji codepoint would be -- must not be
        # rejected just because this interpreter doesn't know it yet.
        ('cn_unassigned', 'Cup 🫩'),
    ],
)
# fmt: on
def test_validate_accepts_name_with_legitimate_unicode_formatting_chars(
    label, name
):
    kwargs = _VALID_KWARGS | {'name': name}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# fmt: off
@pytest.mark.parametrize(
    ('length', 'expect_ok'),
    [
        (80, True),
        (81, False),
    ],
)
# fmt: on
def test_validate_name_length_boundary(length, expect_ok):
    kwargs = _VALID_KWARGS | {'name': 'x' * length}

    result = validate_request_fields(**kwargs)

    assert result.is_ok() is expect_ok
    if not expect_ok:
        assert (
            result.unwrap_err()
            == 'Request name must not exceed 80 characters.'
        )


# fmt: off
@pytest.mark.parametrize('game', ['', '   '])
# fmt: on
def test_validate_rejects_blank_game(game):
    kwargs = _VALID_KWARGS | {'game': game}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Game must not be empty.'


# fmt: off
@pytest.mark.parametrize(
    ('label', 'game'),
    [
        ('zwsp', '\u200b'),
        ('bom', '\ufeff'),
        ('double_word_joiner', '\u2060\u2060'),
    ],
)
# fmt: on
def test_validate_rejects_blank_looking_game(label, game):
    kwargs = _VALID_KWARGS | {'game': game}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Game must not be empty.'


# fmt: off
@pytest.mark.parametrize(
    ('label', 'game'),
    [
        ('crlf', 'Chess\r\nBcc: x'),
        ('tab',  'Chess\tx'),
        ('nul',  'Chess\x00x'),
        ('lrm_spoof', 'Chess\u200ex'),
        ('rlm_spoof', 'Chess\u200fx'),
        ('alm_spoof', 'Chess\u061cx'),
    ],
)
# fmt: on
def test_validate_rejects_game_with_control_or_line_separator_chars(
    label, game
):
    kwargs = _VALID_KWARGS | {'game': game}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Game must not contain line breaks or control characters.'
    )


# fmt: off
@pytest.mark.parametrize(
    ('length', 'expect_ok'),
    [
        (80, True),
        (81, False),
    ],
)
# fmt: on
def test_validate_game_length_boundary(length, expect_ok):
    kwargs = _VALID_KWARGS | {'game': 'x' * length}

    result = validate_request_fields(**kwargs)

    assert result.is_ok() is expect_ok
    if not expect_ok:
        assert result.unwrap_err() == 'Game must not exceed 80 characters.'


# -------------------------------------------------------------------- #
# validate_request_fields -- participant_limit absolute cap


# fmt: off
@pytest.mark.parametrize(
    ('participant_limit', 'expect_ok'),
    [
        (1024, True),
        (1025, False),
    ],
)
# fmt: on
def test_validate_participant_limit_absolute_cap_boundary(
    participant_limit, expect_ok
):
    kwargs = _VALID_KWARGS | {
        'participant_limit': participant_limit,
        'party_capacity': None,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok() is expect_ok
    if not expect_ok:
        assert (
            result.unwrap_err() == 'Participant limit must not exceed 1024.'
        )


def test_validate_rejects_huge_participant_limit_without_party_capacity():
    """D1: an INTEGER-overflowing value with no capacity to bound it."""
    kwargs = _VALID_KWARGS | {
        'participant_limit': 3_000_000_000,
        'party_capacity': None,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == 'Participant limit must not exceed 1024.'


# -------------------------------------------------------------------- #
# validate_request_fields -- text length bounds


def test_validate_rejects_description_over_2000_chars():
    kwargs = _VALID_KWARGS | {'description': 'x' * 2001}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err() == 'Description must not exceed 2000 characters.'
    )


def test_validate_accepts_description_at_2000_chars():
    kwargs = _VALID_KWARGS | {'description': 'x' * 2000}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# fmt: off
@pytest.mark.parametrize(
    ('field', 'message'),
    [
        ('special_rules', 'Special rules must not exceed 2000 characters.'),
        ('notes',         'Notes must not exceed 2000 characters.'),
    ],
)
# fmt: on
def test_validate_rejects_optional_text_over_2000_chars(field, message):
    kwargs = _VALID_KWARGS | {field: 'x' * 2001}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == message


# fmt: off
@pytest.mark.parametrize('field', ['special_rules', 'notes'])
# fmt: on
def test_validate_accepts_optional_text_at_2000_chars(field):
    kwargs = _VALID_KWARGS | {field: 'x' * 2000}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


def test_validate_rejects_desired_template_over_200_chars():
    kwargs = _VALID_KWARGS | {'desired_template': 'x' * 201}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Desired template must not exceed 200 characters.'
    )


def test_validate_accepts_desired_template_at_200_chars():
    kwargs = _VALID_KWARGS | {'desired_template': 'x' * 200}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


def test_validate_accepts_none_optional_texts():
    kwargs = _VALID_KWARGS | {
        'special_rules': None,
        'notes': None,
        'desired_template': None,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# -------------------------------------------------------------------- #
# contains_disallowed_control_char (pure function)


# fmt: off
@pytest.mark.parametrize(
    ('label', 'value'),
    [
        ('nul',        'a\x00b'),
        ('bell',       'a\x07b'),
        ('vtab',       'a\x0bb'),
        ('formfeed',   'a\x0cb'),
        ('c1_nel',     'a\x85b'),
    ],
)
# fmt: on
def test_contains_disallowed_control_char_rejects_nul_and_other_cc(
    label, value
):
    assert contains_disallowed_control_char(value) is True


# fmt: off
@pytest.mark.parametrize(
    ('label', 'value'),
    [
        ('lf',     'a\nb'),
        ('crlf',   'a\r\nb'),
        ('cr',     'a\rb'),
        ('tab',    'a\tb'),
        ('plain',  'a plain line'),
        ('empty',  ''),
    ],
)
# fmt: on
def test_contains_disallowed_control_char_allows_newlines_and_tabs(
    label, value
):
    assert contains_disallowed_control_char(value) is False


# -------------------------------------------------------------------- #
# is_year_in_range (pure function)


# fmt: off
@pytest.mark.parametrize(
    ('label', 'year', 'expected'),
    [
        ('below_min',  MIN_YEAR - 1, False),
        ('at_min',     MIN_YEAR,     True),
        ('at_max',     MAX_YEAR,     True),
        ('above_max',  MAX_YEAR + 1, False),
    ],
)
# fmt: on
def test_is_year_in_range(label, year, expected):
    assert is_year_in_range(datetime(year, 6, 15, 14, 0, 0)) is expected


# -------------------------------------------------------------------- #
# validate_request_fields -- free-text control characters (NUL etc.)


# fmt: off
@pytest.mark.parametrize(
    ('field', 'message'),
    [
        ('description', 'Description must not contain control characters.'),
        ('special_rules', 'Special rules must not contain control characters.'),
        ('notes', 'Notes must not contain control characters.'),
        ('desired_template', 'Desired template must not contain control characters.'),
    ],
)
# fmt: on
def test_validate_rejects_nul_in_free_text_fields(field, message):
    kwargs = _VALID_KWARGS | {field: 'Line one\x00line two'}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == message


# fmt: off
@pytest.mark.parametrize('field', ['description', 'special_rules', 'notes'])
# fmt: on
def test_validate_allows_newlines_and_tabs_in_free_text_fields(field):
    kwargs = _VALID_KWARGS | {field: 'Line one\r\nLine two\twith a tab'}

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# fmt: off
@pytest.mark.parametrize(
    ('label', 'value'),
    [
        ('cr',  'Line one\rline two'),
        ('lf',  'Line one\nline two'),
        ('tab', 'Line one\tline two'),
    ],
)
# fmt: on
def test_validate_rejects_newline_or_tab_in_desired_template(label, value):
    """desired_template is single-line -- unlike description/special_rules/
    notes, it does not tolerate \\r/\\n/\\t (I2c)."""
    kwargs = _VALID_KWARGS | {'desired_template': value}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Desired template must not contain control characters.'
    )


# fmt: off
@pytest.mark.parametrize(
    ('field', 'message'),
    [
        ('description', 'Description must not contain control characters.'),
        ('special_rules', 'Special rules must not contain control characters.'),
        ('notes', 'Notes must not contain control characters.'),
        ('desired_template', 'Desired template must not contain control characters.'),
    ],
)
# fmt: on
def test_validate_rejects_bidi_marks_in_free_text_fields(field, message):
    kwargs = _VALID_KWARGS | {field: 'Line one\u200eline two'}

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == message


# -------------------------------------------------------------------- #
# validate_request_fields -- preferred datetime year bound


# fmt: off
@pytest.mark.parametrize(
    ('label', 'year', 'expect_ok'),
    [
        ('one_year_below_min', MIN_YEAR - 1, False),
        ('at_min',             MIN_YEAR,     True),
        ('at_max',             MAX_YEAR,     True),
        ('one_year_above_max', MAX_YEAR + 1, False),
    ],
)
# fmt: on
def test_validate_bounds_preferred_start_time_year(label, year, expect_ok):
    dt = datetime(year, 6, 15, 14, 0, 0)
    kwargs = _VALID_KWARGS | {
        'preferred_start_time': dt,
        'preferred_end_time': dt,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok() is expect_ok
    if not expect_ok:
        assert result.unwrap_err() == YEAR_RANGE_ERROR_MESSAGE


def test_validate_rejects_out_of_range_preferred_end_time_year():
    # Mid-year, not `MAX_YEAR + 1, 1, 1` -- that exact instant now
    # falls inside the one-day service slack (see
    # `is_within_service_datetime_bounds`) and is covered instead by
    # `test_validate_accepts_preferred_time_one_day_into_upper_slack`.
    kwargs = _VALID_KWARGS | {
        'preferred_start_time': NOW,
        'preferred_end_time': datetime(MAX_YEAR + 1, 6, 15, 0, 0, 0),
    }

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == YEAR_RANGE_ERROR_MESSAGE


def test_validate_rejects_year_1_regression_case():
    """The exact `0001-01-01T00:00` payload a DateTimeLocalField accepts
    unchecked, whose `flask_babel.to_utc` localization overflows."""
    dt = datetime(1, 1, 1, 0, 0, 0)
    kwargs = _VALID_KWARGS | {
        'preferred_start_time': dt,
        'preferred_end_time': dt,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_err()
    assert result.unwrap_err() == YEAR_RANGE_ERROR_MESSAGE


# -------------------------------------------------------------------- #
# is_within_service_datetime_bounds (pure function) -- I2d


# fmt: off
@pytest.mark.parametrize(
    ('label', 'dt', 'expected'),
    [
        ('one_day_below_min_year', datetime(MIN_YEAR - 1, 12, 31, 0, 0, 0), True),
        ('two_days_below_min_year', datetime(MIN_YEAR - 1, 12, 30, 23, 59, 59), False),
        ('one_day_above_max_year', datetime(MAX_YEAR + 1, 1, 1, 23, 59, 59), True),
        ('two_days_above_max_year', datetime(MAX_YEAR + 1, 1, 2, 0, 0, 1), False),
    ],
)
# fmt: on
def test_is_within_service_datetime_bounds(label, dt, expected):
    assert is_within_service_datetime_bounds(dt) is expected


def test_is_within_service_datetime_bounds_ignores_tzinfo():
    """Aware and naive datetimes with the same wall-clock value agree.

    `validate_request_fields` is called with an aware (UTC) datetime
    in production (after `normalize_datetime_to_utc`) but with naive
    datetimes throughout this test file, so the bound check must not
    care either way.
    """
    naive = datetime(MIN_YEAR - 1, 12, 31, 12, 0, 0)
    aware = naive.replace(tzinfo=UTC)

    assert is_within_service_datetime_bounds(naive) is True
    assert is_within_service_datetime_bounds(aware) is True


def test_validate_accepts_preferred_time_one_day_into_lower_slack():
    dt = datetime(MIN_YEAR - 1, 12, 31, 12, 0, 0)
    kwargs = _VALID_KWARGS | {
        'preferred_start_time': dt,
        'preferred_end_time': dt,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


def test_validate_accepts_preferred_time_one_day_into_upper_slack():
    dt = datetime(MAX_YEAR + 1, 1, 1, 12, 0, 0)
    kwargs = _VALID_KWARGS | {
        'preferred_start_time': dt,
        'preferred_end_time': dt,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


def test_validate_accepts_utc_converted_berlin_new_year_edge_case():
    """D-item I2d's exact repro: a local `2000-01-01T00:30` submission
    in Europe/Berlin (UTC+1 in winter) converts to `1999-12-31T23:30`
    UTC -- the form's local-year check (`year_in_range_validator`)
    accepts it as year 2000, and the service must not then refuse the
    UTC-converted value as year 1999.
    """
    import pytz

    local_dt = datetime(MIN_YEAR, 1, 1, 0, 30, 0)
    utc_dt = pytz.timezone('Europe/Berlin').localize(local_dt).astimezone(UTC)
    assert utc_dt.year == MIN_YEAR - 1  # sanity: the shift really occurred

    kwargs = _VALID_KWARGS | {
        'preferred_start_time': utc_dt,
        'preferred_end_time': utc_dt,
    }

    result = validate_request_fields(**kwargs)

    assert result.is_ok()


# -------------------------------------------------------------------- #
# analyze_field_gap


def test_field_gap_lists_two_blockers_for_ffa():
    request = _make_request(
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    gap = analyze_field_gap(request)

    assert gap.blocking == ['point_table', 'group_size_max']


def test_field_gap_has_no_blockers_for_one_v_one():
    request = _make_request(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    gap = analyze_field_gap(request)

    assert gap.blocking == []


def test_field_gap_supplied_count_is_twelve():
    request = _make_request()

    gap = analyze_field_gap(request)

    assert len(gap.supplied) == 12


def test_field_gap_admin_fills_count_is_seven():
    request = _make_request()

    gap = analyze_field_gap(request)

    assert len(gap.admin_fills) == 7


def test_field_gap_has_no_blockers_for_highscore():
    request = _make_request(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
    )

    gap = analyze_field_gap(request)

    assert gap.blocking == []


def test_field_gap_admin_fills_matches_create_tournament_gap():
    request = _make_request()

    gap = analyze_field_gap(request)

    assert set(gap.admin_fills) == {
        'contestant_type',
        'score_ordering',
        'min_players',
        'min_teams',
        'image_url',
        'advancement_count',
        'points_carry_to_losers',
    }


def test_field_gap_supplied_matches_request_fields():
    request = _make_request()

    gap = analyze_field_gap(request)

    assert set(gap.supplied) == {
        'name',
        'game',
        'game_format',
        'elimination_mode',
        'team_size',
        'participant_limit',
        'preferred_start_time',
        'preferred_end_time',
        'description',
        'ruleset',
        'party',
        'origin',
    }


# -------------------------------------------------------------------- #
# normalize_optional_text


def test_normalize_optional_text_maps_empty_string_to_none():
    assert normalize_optional_text('') is None
    assert normalize_optional_text('   ') is None
    assert normalize_optional_text(None) is None


def test_normalize_optional_text_passes_through_non_blank_value():
    assert normalize_optional_text('house rules apply') == 'house rules apply'


# -------------------------------------------------------------------- #
# normalize_datetime_to_utc


_BERLIN_SUMMER = timezone(timedelta(hours=2))

# fmt: off
@pytest.mark.parametrize(
    ('label', 'dt'),
    [
        ('naive',            datetime(2025, 6, 15, 14, 0, 0)),
        ('aware_utc',        datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)),
        ('aware_non_utc',    datetime(2025, 6, 15, 16, 0, 0, tzinfo=_BERLIN_SUMMER)),
    ],
)
# fmt: on
def test_normalize_datetime_to_utc_returns_the_same_instant_aware_utc(
    label, dt
):
    result = normalize_datetime_to_utc(dt)

    assert result.tzinfo is UTC
    assert result == datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)


def test_normalize_datetime_to_utc_is_idempotent_on_aware_utc_input():
    dt = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)

    assert normalize_datetime_to_utc(dt) == dt


# -------------------------------------------------------------------- #
# is_stale_accepted


_STALE_CHECK_NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)


def _make_decided_request(*, status, decided_at):
    return replace(_make_request(), status=status, decided_at=decided_at)


# fmt: off
@pytest.mark.parametrize(
    ('label', 'status', 'decided_at_age', 'expected'),
    [
        ('submitted_undecided',      TournamentRequestStatus.submitted,          None,                              False),
        ('accepted_47h59m',          TournamentRequestStatus.accepted,           timedelta(hours=47, minutes=59),   False),
        ('accepted_exactly_48h',     TournamentRequestStatus.accepted,           timedelta(hours=48),                True),
        ('accepted_10d',             TournamentRequestStatus.accepted,           timedelta(days=10),                 True),
        ('accepted_undecided',       TournamentRequestStatus.accepted,           None,                              False),
        ('tournament_created_10d',   TournamentRequestStatus.tournament_created, timedelta(days=10),                False),
        ('rejected_10d',             TournamentRequestStatus.rejected,           timedelta(days=10),                False),
    ],
)
# fmt: on
def test_is_stale_accepted(label, status, decided_at_age, expected):
    decided_at = (
        _STALE_CHECK_NOW - decided_at_age
        if decided_at_age is not None
        else None
    )
    request = _make_decided_request(status=status, decided_at=decided_at)

    assert is_stale_accepted(request, _STALE_CHECK_NOW) is expected


def test_is_stale_accepted_uses_the_48_hour_threshold_constant():
    assert STALE_ACCEPTED_AFTER == timedelta(hours=48)


def test_is_stale_accepted_does_not_raise_on_a_naive_now():
    naive_now = datetime(2025, 6, 15, 14, 0, 0)
    request = _make_decided_request(
        status=TournamentRequestStatus.accepted,
        decided_at=_STALE_CHECK_NOW - timedelta(days=10),
    )

    assert is_stale_accepted(request, naive_now) is True
