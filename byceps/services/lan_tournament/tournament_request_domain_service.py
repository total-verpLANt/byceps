"""
byceps.services.lan_tournament.tournament_request_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, UTC
import unicodedata

from byceps.util.result import Err, Ok, Result

from .models.elimination_mode import EliminationMode
from .models.game_format import (
    GameFormat,
    is_valid_combination,
    VALID_COMBINATIONS,
)
from .models.tournament_request import (
    TournamentFieldGap,
    TournamentRequest,
    TournamentRequestStatus,
)


MAX_NAME_LENGTH = 80
MAX_GAME_LENGTH = 80
MIN_TEAM_SIZE = 1
MAX_TEAM_SIZE = 64
MIN_PARTICIPANT_LIMIT = 2
MAX_PARTICIPANT_LIMIT = 1024
MAX_DESCRIPTION_LENGTH = 2000
MAX_TEXT_LENGTH = 2000
MAX_DESIRED_TEMPLATE_LENGTH = 200
MAX_REJECTION_REASON_LENGTH = 2000

# `flask_babel.to_utc` localizes a naive datetime through `pytz` before
# converting to UTC; a zone with a pre-1893 LMT offset (Europe/Berlin,
# +0:53:28) shifts a near-`datetime.min`/`datetime.max` value out of
# range and raises an unhandled `OverflowError`. `DateTimeLocalField`
# on its own accepts any year a browser lets through, so both the
# form layer (`year_in_range_validator`, below) and this service must
# bound it.
MIN_YEAR = 2000
MAX_YEAR = 2100

YEAR_RANGE_ERROR_MESSAGE = (
    f'Please enter a date between the years {MIN_YEAR} and {MAX_YEAR}.'
)

# How long an accepted request may sit without its tournament being
# created before the admin queue flags it as stale.
STALE_ACCEPTED_AFTER = timedelta(hours=48)

# The fields a submitted request fills in.
_SUPPLIED_FIELDS = [
    'name',
    'game',
    'game_format',
    'elimination_mode',
    'contestant_type',
    'team_size',
    'participant_limit',
    'preferred_start_time',
    'description',
    'ruleset',
    'party',
    'origin',
]

# The `create_tournament` fields a request never supplies; the admin
# must fill these in before accepting the request into a tournament.
_ADMIN_FILLED_FIELDS = [
    'score_ordering',
    'min_players',
    'min_teams',
    'advancement_count',
    'points_carry_to_losers',
    'image_url',
]

# Fields the request captures that have no `create_tournament`
# counterpart; the admin preview shows these for information only.
_INFO_ONLY_FIELDS = ['preferred_end_time']


def _formats_accepting(mode: EliminationMode) -> set[GameFormat]:
    return {fmt for fmt, m in VALID_COMBINATIONS if m is mode}


def allowed_elimination_modes(
    game_format: GameFormat,
) -> list[tuple[EliminationMode, str | None]]:
    """Return every elimination mode paired with its rejection reason.

    ``None`` marks a mode as valid for `game_format`; otherwise the
    string names why it is not. Both forms render every mode, the
    invalid ones disabled with the reason shown, rather than hiding
    them, so returning the full list (not just the valid subset) is
    what makes that possible.
    """
    pairs: list[tuple[EliminationMode, str | None]] = []
    for mode in EliminationMode:
        if (game_format, mode) in VALID_COMBINATIONS:
            pairs.append((mode, None))
            continue

        formats_accepting_mode = _formats_accepting(mode)
        if len(formats_accepting_mode) == 1:
            (only_format,) = formats_accepting_mode
            reason = f'only_{only_format.name.lower()}'
        else:
            reason = f'invalid_for_{game_format.name.lower()}'
        pairs.append((mode, reason))

    return pairs


def max_participant_limit(
    party_capacity: int | None, team_size: int
) -> int | None:
    """Return the highest participant limit the party capacity allows.

    ``None`` when the capacity is unknown, in which case no cap
    applies. A non-positive `team_size` cannot divide capacity
    meaningfully, so it is treated the same as an unknown capacity.
    """
    if party_capacity is None or team_size <= 0:
        return None

    return party_capacity // team_size


_BIDI_CONTROL_CHARS = frozenset(
    chr(cp)
    for cp in (
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    )
)


def _contains_unsafe_formatting_char(value: str) -> bool:
    """Return whether `value` holds a char unsafe for a header line.

    Catches C0/C1 controls (`Cc`: CR/LF, tab, NUL, U+0085) and the
    Unicode line/paragraph separators (`Zl`/`Zp`, U+2028/U+2029) --
    anything that could split a header line such as an email subject
    built from the request name -- plus the bidi marks/embedding/
    override/isolate controls (U+061C, U+200E, U+200F, U+202A-U+202E,
    U+2066-U+2069), which can spoof the displayed direction of
    surrounding text. Every other `Cf` (e.g. ZWJ U+200D, ZWNJ U+200C,
    soft hyphen U+00AD) and every `Cn`/`Co` char is allowed: those are
    legitimate in names (emoji sequences, several scripts' joiners)
    and rejecting a whole Unicode category by its letter, as an
    earlier version of this function did, over-rejected them along
    with emoji newer than the running Python's `unicodedata`.
    """
    return any(
        unicodedata.category(ch) in ('Cc', 'Zl', 'Zp')
        or ch in _BIDI_CONTROL_CHARS
        for ch in value
    )


_ALLOWED_MULTILINE_CONTROL_CHARS = frozenset('\n\r\t')


def contains_disallowed_control_char(value: str) -> bool:
    """Return whether `value` holds a char unsafe to persist.

    Unlike `_contains_unsafe_formatting_char` (used for single-line
    fields such as name/game, which rejects every `Cc`/`Zl`/`Zp`
    char), multi-line free-text fields -- description, special_rules,
    notes, and a rejection reason -- legitimately contain '\\n', '\\r'
    and '\\t'. Every other `Cc` char is rejected, NUL (0x00) foremost:
    PostgreSQL text columns cannot store it and raise a `DataError` on
    flush, which otherwise reaches the caller as an unhandled 500. The
    bidi marks/embedding/override/isolate controls in
    `_BIDI_CONTROL_CHARS` are rejected here too, same as in the
    single-line check, since they can spoof the displayed direction of
    a multi-line field's text just as well.
    """
    return any(
        (
            unicodedata.category(ch) == 'Cc'
            and ch not in _ALLOWED_MULTILINE_CONTROL_CHARS
        )
        or ch in _BIDI_CONTROL_CHARS
        for ch in value
    )


_VISIBLE_CATEGORY_PREFIXES = ('L', 'N', 'P', 'S')


def _looks_blank(value: str) -> bool:
    """Return whether `value` has no visibly-printable character.

    A name/game consisting solely of zero-width/formatting characters
    (`Cf`: ZWSP U+200B, BOM U+FEFF, word joiner U+2060, ...), plain
    control characters, marks, or separators passes `.strip()`
    unchanged (none of those are ASCII/Unicode whitespace) and
    `_contains_unsafe_formatting_char` (which only rejects `Cc`/`Zl`/
    `Zp`/bidi chars, not every invisible `Cf`), so it would otherwise
    be stored as a blank-looking name. `value` counts as non-blank
    here if it has at least one char in the Letter/Number/
    Punctuation/Symbol general category groups (`L*`/`N*`/`P*`/`S*`)
    -- the same broad grouping that keeps every legitimate script
    (CJK, Cyrillic, emoji, ...) usable. A rare code point that sits in
    one of those categories but renders blank in most fonts (e.g.
    U+3164 HANGUL FILLER is `Lo`, U+2800 BRAILLE PATTERN BLANK is
    `So`) is not caught by this general check; closing that would
    need a per-code-point denylist -- an unbounded, different problem
    (there is no end of blank-rendering Unicode tricks), not a
    category filter.
    """
    return not any(
        unicodedata.category(ch)[0] in _VISIBLE_CATEGORY_PREFIXES
        for ch in value
    )


def is_year_in_range(dt: datetime) -> bool:
    """Return whether `dt`'s year lies within `MIN_YEAR`-`MAX_YEAR`.

    See `MIN_YEAR` for why this bound exists.
    """
    return MIN_YEAR <= dt.year <= MAX_YEAR


# One calendar day of slack on each end of the `MIN_YEAR`-`MAX_YEAR`
# window, used by `is_within_service_datetime_bounds` (the
# post-UTC-conversion check in `validate_request_fields`), not by
# `year_in_range_validator` (the pre-conversion, local-time form
# check). `year_in_range_validator` runs on the browser-submitted
# local value; `normalize_datetime_to_utc` then converts it before
# `validate_request_fields` sees it, and a negative UTC offset (e.g.
# Europe/Berlin, UTC+1 in winter) can shift a `2000-01-01T00:30` local
# value -- which the form accepts as year 2000 -- to
# `1999-12-31T23:30` UTC, one year "below" `MIN_YEAR` by a strict
# `.year` check. The service bound only exists to keep this sane
# (guard against overflow/nonsense such as `datetime.min`/`.max`); the
# form is the actual user-facing enforcement, so widening the service
# bound by a day on each side is harmless. Naive throughout (matching
# `is_year_in_range`'s use of bare `.year`), so it compares safely
# whether the caller already normalized `dt` to aware UTC or not.
_SERVICE_MIN_DATETIME = datetime(MIN_YEAR, 1, 1) - timedelta(days=1)
_SERVICE_MAX_DATETIME = datetime(MAX_YEAR, 12, 31) + timedelta(days=2)


def is_within_service_datetime_bounds(dt: datetime) -> bool:
    """Return whether `dt` lies within the slack-widened service window.

    See `_SERVICE_MIN_DATETIME` for why this differs from
    `is_year_in_range`. `validate_request_fields` uses this instead of
    `is_year_in_range` for `preferred_start_time`/`preferred_end_time`.
    """
    naive_dt = dt.replace(tzinfo=None)
    return _SERVICE_MIN_DATETIME <= naive_dt <= _SERVICE_MAX_DATETIME


def year_in_range_validator(form, field) -> None:
    """WTForms validator: reject a datetime field outside the sane
    `MIN_YEAR`-`MAX_YEAR` scheduling window.

    Both `blueprints/site/forms.py` (`TournamentProposeForm`) and
    `blueprints/admin/forms.py` (`TournamentRequestUpdateForm`,
    `_BaseForm.start_time`, inherited by `TournamentCreateForm`/
    `TournamentUpdateForm`) attach this to every
    `preferred_start_time`/`preferred_end_time`/`start_time` field, so
    the window and its message are defined once. Without it, a
    `DateTimeLocalField` on its own accepts any year a browser lets
    through (`0001-01-01`, `9999-12-31`); the views only call
    `flask_babel.to_utc` after `form.validate()` succeeds, so this is
    the single point that has to catch it.
    """
    from flask_babel import lazy_gettext
    from wtforms.validators import ValidationError

    # `check_translations.py`'s AST scanner only catches a `gettext`/
    # `lazy_gettext`/`Err` call whose first argument is a string
    # literal -- referencing `YEAR_RANGE_ERROR_MESSAGE` by name here
    # would be invisible to it, the same way `Err(YEAR_RANGE_ERROR_MESSAGE)`
    # below is. This literal must stay byte-for-byte identical to that
    # constant; `test_tournament_request_datetime_bounds_forms.py`
    # cross-checks it.
    if field.data is not None and not is_year_in_range(field.data):
        raise ValidationError(
            lazy_gettext(
                'Please enter a date between the years 2000 and 2100.'
            )
        )


def validate_request_fields(
    *,
    name: str,
    game: str,
    team_size: int,
    participant_limit: int,
    party_capacity: int | None,
    preferred_start_time: datetime,
    preferred_end_time: datetime,
    description: str,
    game_format: GameFormat,
    elimination_mode: EliminationMode,
    special_rules: str | None = None,
    notes: str | None = None,
    desired_template: str | None = None,
) -> Result[None, str]:
    """Validate a tournament request's fields.

    This is the enforcement point for the participant-limit/party-
    capacity cap. A form-side script may mirror the cap live for
    convenience, but a POST that bypasses it must still be rejected
    here.
    """
    if not name.strip() or _looks_blank(name):
        return Err('Request name must not be empty.')

    if _contains_unsafe_formatting_char(name):
        return Err(
            'Request name must not contain line breaks or control '
            'characters.'
        )

    if len(name.strip()) > MAX_NAME_LENGTH:
        return Err('Request name must not exceed 80 characters.')

    if not game.strip() or _looks_blank(game):
        return Err('Game must not be empty.')

    if _contains_unsafe_formatting_char(game):
        return Err(
            'Game must not contain line breaks or control characters.'
        )

    if len(game.strip()) > MAX_GAME_LENGTH:
        return Err('Game must not exceed 80 characters.')

    if not (MIN_TEAM_SIZE <= team_size <= MAX_TEAM_SIZE):
        return Err('Team size must be between 1 and 64.')

    if participant_limit < MIN_PARTICIPANT_LIMIT:
        return Err('Participant limit must be at least 2.')

    if participant_limit > MAX_PARTICIPANT_LIMIT:
        return Err('Participant limit must not exceed 1024.')

    limit_cap = max_participant_limit(party_capacity, team_size)
    if limit_cap is not None and participant_limit > limit_cap:
        return Err('Participant limit exceeds party capacity.')

    if not is_within_service_datetime_bounds(preferred_start_time):
        return Err(YEAR_RANGE_ERROR_MESSAGE)

    if not is_within_service_datetime_bounds(preferred_end_time):
        return Err(YEAR_RANGE_ERROR_MESSAGE)

    if preferred_end_time < preferred_start_time:
        return Err('Preferred end time must not precede start time.')

    if not description.strip():
        return Err('Description must not be empty.')

    if contains_disallowed_control_char(description):
        return Err('Description must not contain control characters.')

    if len(description) > MAX_DESCRIPTION_LENGTH:
        return Err('Description must not exceed 2000 characters.')

    if special_rules is not None and contains_disallowed_control_char(
        special_rules
    ):
        return Err('Special rules must not contain control characters.')

    if special_rules is not None and len(special_rules) > MAX_TEXT_LENGTH:
        return Err('Special rules must not exceed 2000 characters.')

    if notes is not None and contains_disallowed_control_char(notes):
        return Err('Notes must not contain control characters.')

    if notes is not None and len(notes) > MAX_TEXT_LENGTH:
        return Err('Notes must not exceed 2000 characters.')

    if desired_template is not None and _contains_unsafe_formatting_char(
        desired_template
    ):
        return Err('Desired template must not contain control characters.')

    if (
        desired_template is not None
        and len(desired_template) > MAX_DESIRED_TEMPLATE_LENGTH
    ):
        return Err('Desired template must not exceed 200 characters.')

    if not is_valid_combination(game_format, elimination_mode):
        return Err('Invalid combination of game format and elimination mode.')

    return Ok(None)


def analyze_field_gap(request: TournamentRequest) -> TournamentFieldGap:
    """Compute which `create_tournament` fields a request leaves open.

    `blocking` mirrors `_validate_ffa_config`
    (`tournament_service.py:55-61`), which hard-rejects an FFA create
    missing `point_table` or `group_size_max`. Keep this list in sync
    with that function; if it gains a requirement and this list does
    not, the admin's preview under-reports and they still walk into
    the create-time rejection.
    """
    blocking = (
        ['point_table', 'group_size_max']
        if request.game_format is GameFormat.FREE_FOR_ALL
        else []
    )

    return TournamentFieldGap(
        supplied=list(_SUPPLIED_FIELDS),
        admin_fills=list(_ADMIN_FILLED_FIELDS),
        blocking=blocking,
        info_only=list(_INFO_ONLY_FIELDS),
    )


def normalize_datetime_to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to aware UTC.

    `flask_babel.to_utc` (the views' conversion point) returns a
    *naive* datetime that is, by contract, already UTC; an aware
    value is converted. Call this once at the service boundary, on
    every datetime before it reaches validation, comparison or
    persistence, so a `TIMESTAMPTZ` bind or a `changed_fields`
    comparison against a repository-read (aware) value never mixes
    naive and aware datetimes.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)

    return dt.astimezone(UTC)


def is_stale_accepted(request: TournamentRequest, now: datetime) -> bool:
    """Report whether an accepted request has waited too long for its
    tournament.

    F-17 is form-first: accepting a request does not create the
    tournament, an admin still has to open the create form. True iff
    the request is `accepted`, has been decided, and at least
    `STALE_ACCEPTED_AFTER` has elapsed since. `now` is normalized
    through `normalize_datetime_to_utc` (as is `decided_at`, which is
    always aware in practice), so a naive `now` cannot raise.
    """
    if request.status is not TournamentRequestStatus.accepted:
        return False

    if request.decided_at is None:
        return False

    aware_now = normalize_datetime_to_utc(now)
    decided_at = normalize_datetime_to_utc(request.decided_at)
    return aware_now - decided_at >= STALE_ACCEPTED_AFTER


def normalize_optional_text(value: str | None) -> str | None:
    """Map an empty or whitespace-only string to ``None``.

    WTForms hands back `''` for a blank textarea; without this, the
    `IS NULL` semantics of optional text columns such as
    `special_rules`, `notes` and `desired_template` break silently.
    """
    if value is None or not value.strip():
        return None

    return value
