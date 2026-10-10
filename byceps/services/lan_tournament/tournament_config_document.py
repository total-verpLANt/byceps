"""
byceps.services.lan_tournament.tournament_config_document
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, UTC
import json
import re
from typing import Any
import unicodedata

from byceps.services.lan_tournament import tournament_domain_service
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.lan_tournament.tournament_config_domain_service import (
    has_unstorable_character,
    TournamentConfigInput,
    UNSTORABLE_CHARACTER_ERROR,
)
from byceps.util.result import Err, Ok, Result


FORMAT = 'lan_tournament.config'
VERSION = 1
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_REPORTED_PROBLEMS = 20
_MAX_ECHO_LENGTH = 64
_MAX_START_TIME_LENGTH = 64
_MAX_EXPORT_SLUG_LENGTH = 40

DOCUMENT_TOO_LARGE_ERROR = 'The file is larger than %(max)s KiB.'
DOCUMENT_NOT_UTF8_ERROR = 'The file is not UTF-8 text.'
DOCUMENT_NOT_JSON_ERROR = 'The file is not valid JSON.'
DOCUMENT_DUPLICATE_KEY_ERROR = 'The key "%(key)s" appears more than once.'
DOCUMENT_NOT_OBJECT_ERROR = 'Expected a JSON object.'
DOCUMENT_WRONG_FORMAT_ERROR = 'This is not a tournament configuration file.'
DOCUMENT_WRONG_VERSION_ERROR = (
    'Unsupported file version. This instance reads version %(supported)s.'
)
UNKNOWN_KEY_ERROR = 'Unknown key "%(key)s".'
MISSING_KEY_ERROR = 'This key is missing.'
EXPECTED_TEXT_ERROR = 'Expected text.'
EXPECTED_TEXT_OR_NULL_ERROR = 'Expected text or null.'
EXPECTED_WHOLE_NUMBER_ERROR = 'Expected a whole number or null.'
EXPECTED_BOOLEAN_ERROR = 'Expected true or false.'
EXPECTED_NUMBER_LIST_ERROR = 'Expected a list of whole numbers or null.'
START_TIME_FORMAT_ERROR = (
    'Expected an ISO 8601 time with a UTC offset, e.g. 2026-10-09T18:00:00Z.'
)

TOURNAMENT_KEYS: tuple[str, ...] = tuple(
    f.name for f in dataclasses.fields(TournamentConfigInput)
)
REQUIRED_KEYS = frozenset({'name', 'category'})

_ENVELOPE_KEYS = frozenset({'format', 'version', 'tournament'})

_TEXT = 'text'
_TEXT_OR_NULL = 'text_or_null'
_TIME = 'time'
_COUNT = 'count'
_NUMBERS = 'numbers'
_FLAG = 'flag'

_KEY_TYPES: dict[str, str] = {
    'name': _TEXT,
    'category': _TEXT,
    'game': _TEXT_OR_NULL,
    'description': _TEXT_OR_NULL,
    'ruleset': _TEXT_OR_NULL,
    'start_time': _TIME,
    'contestant_type': _TEXT_OR_NULL,
    'game_format': _TEXT_OR_NULL,
    'elimination_mode': _TEXT_OR_NULL,
    'score_ordering': _TEXT_OR_NULL,
    'min_players': _COUNT,
    'max_players': _COUNT,
    'min_teams': _COUNT,
    'max_teams': _COUNT,
    'min_players_in_team': _COUNT,
    'max_players_in_team': _COUNT,
    'point_table': _NUMBERS,
    'advancement_count': _COUNT,
    'group_size_min': _COUNT,
    'group_size_max': _COUNT,
    'points_carry_to_losers': _FLAG,
    'playoff_enabled': _FLAG,
    'playoff_group_count': _COUNT,
    'playoff_qualifiers_per_group': _COUNT,
    'playoff_qualifier_count': _COUNT,
    'playoff_elimination_mode': _TEXT_OR_NULL,
    'playoff_release_mode': _TEXT_OR_NULL,
}


@dataclass(frozen=True)
class DocumentProblem:
    location: str  # a tournament key, or '' for the whole document
    message: ValidationMessage


class _DuplicateKey(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def serialize(config_input: TournamentConfigInput) -> bytes:
    """Return the config as a versioned JSON document."""
    tournament: dict[str, Any] = {}
    for key in TOURNAMENT_KEYS:
        value = getattr(config_input, key)
        if key == 'start_time' and value is not None:
            value = value.isoformat() + 'Z'
        elif key == 'point_table' and value is not None:
            if isinstance(value, str):
                raise TypeError('The point table must be a list of integers.')
            value = list(value)
        tournament[key] = value

    document = {'format': FORMAT, 'version': VERSION, 'tournament': tournament}
    return (
        json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
        + b'\n'
    )


def parse_document(
    raw: bytes,
) -> Result[TournamentConfigInput, list[DocumentProblem]]:
    """Parse a document, or return the first problems found."""
    problems: list[DocumentProblem] = []

    def add_message(location: str, message: ValidationMessage) -> None:
        if len(problems) <= MAX_REPORTED_PROBLEMS:
            problems.append(DocumentProblem(location, message))

    def add(location: str, msgid: str, **params: str | int) -> None:
        add_message(location, ValidationMessage(msgid, tuple(params.items())))

    if len(raw) > MAX_DOCUMENT_BYTES:
        add('', DOCUMENT_TOO_LARGE_ERROR, max=MAX_DOCUMENT_BYTES // 1024)
        return Err(problems)

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        add('', DOCUMENT_NOT_UTF8_ERROR)
        return Err(problems)

    try:
        document = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_refuse_constant,
        )
    except _DuplicateKey as exc:
        add('', DOCUMENT_DUPLICATE_KEY_ERROR, key=_echo(exc.key))
        return Err(problems)
    except (ValueError, RecursionError):
        add('', DOCUMENT_NOT_JSON_ERROR)
        return Err(problems)

    if not isinstance(document, dict):
        add('', DOCUMENT_NOT_OBJECT_ERROR)
        return Err(problems)

    for key in document:
        if key not in _ENVELOPE_KEYS:
            add('', UNKNOWN_KEY_ERROR, key=_echo(key))

    if document.get('format') != FORMAT:
        add('', DOCUMENT_WRONG_FORMAT_ERROR)
        return Err(problems)

    version = document.get('version')
    if type(version) is not int or version != VERSION:
        add('', DOCUMENT_WRONG_VERSION_ERROR, supported=VERSION)
        return Err(problems)

    tournament = document.get('tournament')
    if not isinstance(tournament, dict):
        add('', DOCUMENT_NOT_OBJECT_ERROR)
        return Err(problems)

    for key in tournament:
        if key not in _KEY_TYPES:
            add('', UNKNOWN_KEY_ERROR, key=_echo(key))

    values: dict[str, Any] = {}
    for key in TOURNAMENT_KEYS:
        if key not in tournament:
            if key in REQUIRED_KEYS:
                add(key, MISSING_KEY_ERROR)
            continue

        value, problem = _read_value(_KEY_TYPES[key], tournament[key])
        if problem is not None:
            add_message(key, problem)
        else:
            values[key] = value

    if problems:
        return Err(problems)

    return Ok(TournamentConfigInput(**values))


def export_filename(name: str, day: date) -> str:
    """Return an ASCII-only download file name for a tournament."""
    ascii_name = (
        unicodedata.normalize('NFKD', name)
        .encode('ascii', 'ignore')
        .decode('ascii')
        .lower()
    )
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_name).strip('-')
    slug = slug[:_MAX_EXPORT_SLUG_LENGTH].rstrip('-') or 'tournament'
    return f'lan-tournament-{slug}-{day:%Y%m%d}.json'


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _refuse_constant(name: str) -> Any:
    raise ValueError(f'Constant {name} is not allowed.')


def _echo(key: str) -> str:
    """Reduce a document key to printable characters for a message."""
    return ''.join(
        c if c.isprintable() else '?' for c in key[:_MAX_ECHO_LENGTH]
    )


def _read_value(kind: str, value: Any) -> tuple[Any, ValidationMessage | None]:
    """Return the value to store, or what is wrong with it."""
    if kind == _FLAG:
        if value is None:
            return False, None
        if type(value) is bool:
            return value, None
        return None, ValidationMessage(EXPECTED_BOOLEAN_ERROR)

    if value is None:
        if kind == _TEXT:
            return None, ValidationMessage(EXPECTED_TEXT_ERROR)
        return None, None

    if kind == _COUNT:
        if type(value) is int:
            return value, None
        return None, ValidationMessage(EXPECTED_WHOLE_NUMBER_ERROR)

    if kind == _NUMBERS:
        return _read_number_list(value)

    return _read_text(kind, value)


def _read_text(kind: str, value: Any) -> tuple[Any, ValidationMessage | None]:
    if type(value) is not str:
        msgid = (
            EXPECTED_TEXT_ERROR
            if kind == _TEXT
            else EXPECTED_TEXT_OR_NULL_ERROR
        )
        return None, ValidationMessage(msgid)

    if has_unstorable_character(value):
        return None, ValidationMessage(UNSTORABLE_CHARACTER_ERROR)

    if kind == _TIME:
        start_time = _parse_start_time(value)
        if start_time is None:
            return None, ValidationMessage(START_TIME_FORMAT_ERROR)
        return start_time, None

    return value, None


def _read_number_list(value: Any) -> tuple[Any, ValidationMessage | None]:
    if not isinstance(value, list):
        return None, ValidationMessage(EXPECTED_NUMBER_LIST_ERROR)

    maximum = tournament_domain_service.MAX_POINT_TABLE_PLACES
    if len(value) > maximum:
        return None, ValidationMessage(
            tournament_domain_service.TOO_MANY_PLACES_MSGID,
            (('max', maximum),),
        )

    if any(type(item) is not int for item in value):
        return None, ValidationMessage(EXPECTED_NUMBER_LIST_ERROR)

    return list(value), None


def _parse_start_time(value: str) -> datetime | None:
    """Return the time as naive UTC, or `None` if it is not acceptable."""
    if len(value) > _MAX_START_TIME_LENGTH:
        return None

    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None
