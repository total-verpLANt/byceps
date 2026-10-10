"""
tests.unit.services.lan_tournament.test_tournament_config_document
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import ast
from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import (
    tournament_config_document as document,
    tournament_domain_service,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.lan_tournament.tournament_config_document import (
    DocumentProblem,
    export_filename,
    FORMAT,
    has_unstorable_character,
    MAX_DOCUMENT_BYTES,
    MAX_REPORTED_PROBLEMS,
    parse_document,
    serialize,
    TOURNAMENT_KEYS,
    VERSION,
)
from byceps.services.lan_tournament.tournament_config_domain_service import (
    TournamentConfigInput,
)

from tests.unit.services.lan_tournament.test_tournament_request_draft_copy import (  # noqa: E501
    _catalog,
    _german_or_none,
)


TEXT_KEYS = ('name', 'category')
TEXT_OR_NULL_KEYS = (
    'game',
    'description',
    'ruleset',
    'start_time',
    'contestant_type',
    'game_format',
    'elimination_mode',
    'score_ordering',
    'playoff_elimination_mode',
    'playoff_release_mode',
)
COUNT_KEYS = (
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
    'advancement_count',
    'group_size_min',
    'group_size_max',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
)
FLAG_KEYS = ('points_carry_to_losers', 'playoff_enabled')


def _doc(tournament: dict[str, Any] | None = None, /, **envelope: Any) -> bytes:
    body: dict[str, Any] = {
        'format': FORMAT,
        'version': VERSION,
        'tournament': {'name': 'Cup', 'category': 'MAIN'}
        if tournament is None
        else tournament,
    }
    body.update(envelope)
    return json.dumps(body).encode('utf-8')


def _with(**overrides: Any) -> bytes:
    return _doc({'name': 'Cup', 'category': 'MAIN', **overrides})


def _input(**overrides: Any) -> TournamentConfigInput:
    values: dict[str, Any] = {'name': 'Cup', 'category': 'MAIN'}
    values.update(overrides)
    return TournamentConfigInput(**values)


def _problems(raw: bytes) -> list[DocumentProblem]:
    result = parse_document(raw)
    assert result.is_err(), 'the document was accepted'
    return result.unwrap_err()


def _problem(location: str, msgid: str, **params: str | int) -> DocumentProblem:
    return DocumentProblem(
        location, ValidationMessage(msgid, tuple(params.items()))
    )


# fmt: off
ROUND_TRIP_INPUTS = [
    pytest.param(_input(), id='minimal'),
    pytest.param(
        _input(
            game='Counter-Strike',
            description='Line one\nLine two',
            ruleset='Best of 3',
            start_time=datetime(2026, 10, 9, 18, 0),
            contestant_type='TEAM',
            game_format='ONE_V_ONE',
            elimination_mode='DOUBLE_ELIMINATION',
            min_teams=2,
            max_teams=16,
            min_players_in_team=5,
            max_players_in_team=5,
        ),
        id='team-bracket',
    ),
    pytest.param(
        _input(
            category='FUN',
            contestant_type='SOLO',
            game_format='FREE_FOR_ALL',
            elimination_mode='DOUBLE_ELIMINATION',
            min_players=8,
            max_players=64,
            point_table=[10, 6, 3, 1],
            advancement_count=2,
            group_size_min=4,
            group_size_max=8,
            points_carry_to_losers=True,
        ),
        id='ffa-with-point-table',
    ),
    pytest.param(
        _input(
            game_format='HIGHSCORE',
            score_ordering='DESCENDING',
            playoff_enabled=True,
            playoff_qualifier_count=8,
            playoff_elimination_mode='SINGLE_ELIMINATION',
            playoff_release_mode='MANUAL',
        ),
        id='highscore-with-playoff',
    ),
    pytest.param(
        _input(name='Zürich 😀 "Cup"', game='', description='Ünï\tcode'),
        id='unicode-and-empty-text',
    ),
    pytest.param(
        _input(start_time=datetime(2026, 10, 9, 18, 0, 0, 123456)),
        id='microseconds',
    ),
]
# fmt: on


@pytest.mark.parametrize('config_input', ROUND_TRIP_INPUTS)
def test_serialize_then_parse_round_trips(config_input):
    result = parse_document(serialize(config_input))

    assert result.is_ok()
    assert result.unwrap() == config_input


def test_serialized_document_shape():
    config_input = _input(
        start_time=datetime(2026, 10, 9, 18, 0),
        game_format='ONE_V_ONE',
        elimination_mode='ROUND_ROBIN',
        point_table=[3, 1],
    )

    raw = serialize(config_input)
    parsed = json.loads(raw)

    assert raw.endswith(b'}\n')
    assert raw.startswith(b'{\n  "format"')
    assert list(parsed) == ['format', 'version', 'tournament']
    assert parsed['format'] == FORMAT == 'lan_tournament.config'
    assert parsed['version'] == VERSION == 1
    assert list(parsed['tournament']) == list(TOURNAMENT_KEYS)
    assert parsed['tournament']['category'] == 'MAIN'
    assert parsed['tournament']['game_format'] == 'ONE_V_ONE'
    assert parsed['tournament']['elimination_mode'] == 'ROUND_ROBIN'
    assert parsed['tournament']['start_time'] == '2026-10-09T18:00:00Z'
    assert parsed['tournament']['point_table'] == [3, 1]
    assert parsed['tournament']['playoff_enabled'] is False
    assert parsed['tournament']['description'] is None


def test_serialize_writes_text_as_is():
    raw = serialize(_input(name='Zürich 😀'))

    assert 'Zürich 😀'.encode() in raw


def test_serialize_refuses_point_table_text():
    with pytest.raises(TypeError):
        serialize(_input(point_table='10, 6, 3'))


def test_serialize_keeps_microseconds_and_empty_text():
    config_input = _input(
        start_time=datetime(2026, 10, 9, 18, 0, 0, 123456),
        game='',
        description=None,
    )

    raw = serialize(config_input)
    tournament = json.loads(raw)['tournament']

    assert tournament['start_time'] == '2026-10-09T18:00:00.123456Z'
    assert tournament['game'] == ''
    assert tournament['description'] is None
    assert parse_document(raw).unwrap() == config_input


def test_parse_missing_optional_keys_default():
    result = parse_document(_doc({'name': 'Cup', 'category': 'MAIN'}))

    assert result.unwrap() == TournamentConfigInput(name='Cup', category='MAIN')


@pytest.mark.parametrize('key', FLAG_KEYS)
def test_parse_reads_null_flag_as_false(key):
    assert getattr(parse_document(_with(**{key: None})).unwrap(), key) is False


def test_parse_passes_integers_through_unchanged():
    result = parse_document(
        _with(
            min_players=10**400,
            playoff_qualifier_count=-(10**400),
            group_size_max=0,
            advancement_count=-(2**1100),
        )
    )

    config_input = result.unwrap()
    assert config_input.min_players == 10**400
    assert config_input.playoff_qualifier_count == -(10**400)
    assert config_input.group_size_max == 0
    assert config_input.advancement_count == -(2**1100)


def test_parse_refuses_oversize_before_decoding():
    raw = b'\xff' * (MAX_DOCUMENT_BYTES + 1)

    with patch.object(document.json, 'loads') as loads:
        problems = _problems(raw)

    loads.assert_not_called()
    assert problems == [
        _problem('', document.DOCUMENT_TOO_LARGE_ERROR, max=256)
    ]


def test_parse_accepts_a_document_of_exactly_the_cap():
    raw = _doc()
    padded = raw + b' ' * (MAX_DOCUMENT_BYTES - len(raw))

    assert len(padded) == MAX_DOCUMENT_BYTES
    assert parse_document(padded).is_ok()
    assert [p.message.msgid for p in _problems(padded + b' ')] == [
        document.DOCUMENT_TOO_LARGE_ERROR
    ]


# fmt: off
@pytest.mark.parametrize(
    ('raw', 'msgid'),
    [
        pytest.param(b'\xff\xfe{}', document.DOCUMENT_NOT_UTF8_ERROR, id='utf16-bom'),
        pytest.param(b'{"name": "\xfc"}', document.DOCUMENT_NOT_UTF8_ERROR, id='latin-1'),
        pytest.param(b'"\xed\xa0\x80"', document.DOCUMENT_NOT_UTF8_ERROR, id='encoded-surrogate'),
        pytest.param(b'\xef\xbb\xbf' + _doc(), document.DOCUMENT_NOT_JSON_ERROR, id='utf8-bom'),
    ],
)
# fmt: on
def test_parse_refuses_non_utf8_and_bom(raw, msgid):
    assert _problems(raw) == [_problem('', msgid)]


# fmt: off
@pytest.mark.parametrize(
    'raw',
    [
        pytest.param(b'', id='empty'),
        pytest.param(b'{', id='truncated'),
        pytest.param(b'{"a": 1,}', id='trailing-comma'),
        pytest.param(b"{'a': 1}", id='single-quotes'),
        pytest.param(b'NaN', id='nan'),
        pytest.param(b'-Infinity', id='negative-infinity'),
        pytest.param(b'{"a": Infinity}', id='infinity-in-object'),
        pytest.param(b'{"a": 1} {"b": 2}', id='trailing-data'),
        pytest.param(b'\x00', id='raw-nul'),
        pytest.param(b'[' * 200_000, id='deep-nesting'),
        pytest.param(
            b'{"tournament": {"min_players": ' + b'9' * 5000 + b'}}',
            id='5000-digit-int',
        ),
    ],
)
# fmt: on
def test_parse_refuses_invalid_json(raw):
    assert _problems(raw) == [_problem('', document.DOCUMENT_NOT_JSON_ERROR)]


# fmt: off
@pytest.mark.parametrize(
    ('raw', 'key'),
    [
        pytest.param(
            b'{"format": "x", "format": "y"}', 'format', id='envelope'
        ),
        pytest.param(
            b'{"format": "lan_tournament.config", "version": 1, "tournament":'
            b' {"name": "a", "name": "b"}}',
            'name',
            id='tournament',
        ),
        pytest.param(
            b'{"unknown": {"deep": [{"k": 1, "k": 2}]}}', 'k', id='nested'
        ),
    ],
)
# fmt: on
def test_parse_refuses_duplicate_keys(raw, key):
    assert _problems(raw) == [
        _problem('', document.DOCUMENT_DUPLICATE_KEY_ERROR, key=key)
    ]


# fmt: off
@pytest.mark.parametrize(
    'raw',
    [
        pytest.param(b'[]', id='top-list'),
        pytest.param(b'"text"', id='top-string'),
        pytest.param(b'1', id='top-number'),
        pytest.param(b'null', id='top-null'),
        pytest.param(_doc(None, tournament=[]), id='tournament-list'),
        pytest.param(_doc(None, tournament='x'), id='tournament-string'),
        pytest.param(_doc(None, tournament=None), id='tournament-null'),
        pytest.param(_doc(None, tournament=1), id='tournament-number'),
        pytest.param(
            json.dumps({'format': FORMAT, 'version': VERSION}).encode(),
            id='tournament-missing',
        ),
    ],
)
# fmt: on
def test_parse_refuses_non_object(raw):
    assert _problems(raw) == [_problem('', document.DOCUMENT_NOT_OBJECT_ERROR)]


# fmt: off
@pytest.mark.parametrize(
    'version',
    [
        pytest.param(2, id='2'),
        pytest.param(0, id='0'),
        pytest.param('1', id='string'),
        pytest.param(True, id='true'),
        pytest.param(1.0, id='float'),
        pytest.param(None, id='null'),
        pytest.param([1], id='list'),
        pytest.param(..., id='missing'),
    ],
)
# fmt: on
def test_parse_refuses_wrong_version(version):
    body: dict[str, Any] = {'format': FORMAT, 'tournament': {'name': 5}}
    if version is not ...:
        body['version'] = version

    assert _problems(json.dumps(body).encode()) == [
        _problem('', document.DOCUMENT_WRONG_VERSION_ERROR, supported=1)
    ]


# fmt: off
@pytest.mark.parametrize(
    'fmt',
    [
        pytest.param('lan_tournament.config ', id='trailing-space'),
        pytest.param('other', id='other'),
        pytest.param(1, id='number'),
        pytest.param(None, id='null'),
        pytest.param(['lan_tournament.config'], id='list'),
        pytest.param(..., id='missing'),
    ],
)
# fmt: on
def test_parse_refuses_wrong_format_and_stops(fmt):
    body: dict[str, Any] = {'version': 2, 'tournament': {'name': 5}}
    if fmt is not ...:
        body['format'] = fmt

    assert _problems(json.dumps(body).encode()) == [
        _problem('', document.DOCUMENT_WRONG_FORMAT_ERROR)
    ]


def test_parse_reports_unknown_envelope_keys():
    raw = _doc(extra=1, other={'a': 1})

    assert _problems(raw) == [
        _problem('', document.UNKNOWN_KEY_ERROR, key='extra'),
        _problem('', document.UNKNOWN_KEY_ERROR, key='other'),
    ]


def test_parse_reports_unknown_tournament_keys():
    raw = _with(status='DRAFT', playoff_game_format='ONE_V_ONE')

    assert _problems(raw) == [
        _problem('', document.UNKNOWN_KEY_ERROR, key='status'),
        _problem('', document.UNKNOWN_KEY_ERROR, key='playoff_game_format'),
    ]


def test_parse_reports_unknown_keys_with_sanitised_echo():
    key = 'bad\nkey\t' + 'x' * 200

    [problem] = _problems(_with(**{key: 1}))

    echoed = dict(problem.message.params)['key']
    assert problem.location == ''
    assert problem.message.msgid == document.UNKNOWN_KEY_ERROR
    assert echoed == 'bad?key?' + 'x' * 56
    assert len(echoed) == 64
    assert echoed.isprintable()


@pytest.mark.parametrize('bad_key', ['\x00', '\ud800', 'a\x00b\udc00c'])
def test_unknown_key_echo_of_nul_and_surrogate_is_encodable(bad_key):
    raw = json.dumps(
        {
            'format': FORMAT,
            'version': VERSION,
            'tournament': {'name': 'Cup', 'category': 'MAIN', bad_key: 1},
        }
    ).encode('ascii')

    [problem] = _problems(raw)

    text = problem.message.msgid % dict(problem.message.params)
    assert text.encode('utf-8')
    assert problem.message.msgid == document.UNKNOWN_KEY_ERROR


@pytest.mark.parametrize(
    ('tournament', 'missing'),
    [
        pytest.param({}, ['name', 'category'], id='both'),
        pytest.param({'category': 'MAIN'}, ['name'], id='name'),
        pytest.param({'name': 'Cup'}, ['category'], id='category'),
    ],
)
def test_parse_reports_missing_required_keys(tournament, missing):
    assert _problems(_doc(tournament)) == [
        _problem(key, document.MISSING_KEY_ERROR) for key in missing
    ]


BAD_COUNT_VALUES: tuple[Any, ...] = (True, False, 1.0, 1.5, '8', [], {}, [1])


@pytest.mark.parametrize('key', COUNT_KEYS)
@pytest.mark.parametrize('value', BAD_COUNT_VALUES)
def test_parse_type_table_counts(key, value):
    assert _problems(_with(**{key: value})) == [
        _problem(key, document.EXPECTED_WHOLE_NUMBER_ERROR)
    ]


@pytest.mark.parametrize('key', TEXT_KEYS)
@pytest.mark.parametrize('value', [None, True, 5, 1.5, [], {}, ['x']])
def test_parse_type_table_required_text(key, value):
    assert _problems(_with(**{key: value})) == [
        _problem(key, document.EXPECTED_TEXT_ERROR)
    ]


@pytest.mark.parametrize('key', TEXT_OR_NULL_KEYS)
@pytest.mark.parametrize('value', [True, 5, 1.5, [], {}, ['x']])
def test_parse_type_table_optional_text(key, value):
    assert _problems(_with(**{key: value})) == [
        _problem(key, document.EXPECTED_TEXT_OR_NULL_ERROR)
    ]


@pytest.mark.parametrize('key', FLAG_KEYS)
@pytest.mark.parametrize('value', [0, 1, 'true', 'false', [], {}, 1.0])
def test_parse_type_table_flags(key, value):
    assert _problems(_with(**{key: value})) == [
        _problem(key, document.EXPECTED_BOOLEAN_ERROR)
    ]


# fmt: off
@pytest.mark.parametrize(
    'value',
    [
        pytest.param(5, id='number'),
        pytest.param('10,6,3', id='comma-text'),
        pytest.param(True, id='bool'),
        pytest.param({'1': 10}, id='object'),
        pytest.param([10, True], id='bool-item'),
        pytest.param([10, 1.5], id='float-item'),
        pytest.param([10, '6'], id='string-item'),
        pytest.param([10, None], id='null-item'),
        pytest.param([[10]], id='nested-list'),
    ],
)
# fmt: on
def test_parse_type_table_point_table(value):
    assert _problems(_with(point_table=value)) == [
        _problem('point_table', document.EXPECTED_NUMBER_LIST_ERROR)
    ]


@pytest.mark.parametrize('value', [None, [], [10, 6, 3], [0, -1]])
def test_parse_accepts_point_tables(value):
    result = parse_document(_with(point_table=value))

    assert result.unwrap().point_table == value


def test_point_table_over_place_cap_refused():
    maximum = tournament_domain_service.MAX_POINT_TABLE_PLACES
    msgid = tournament_domain_service.TOO_MANY_PLACES_MSGID

    assert parse_document(_with(point_table=[1] * maximum)).is_ok()
    assert _problems(_with(point_table=[1] * (maximum + 1))) == [
        _problem('point_table', msgid, max=maximum)
    ]
    assert _problems(_with(point_table=[True] * (maximum + 1))) == [
        _problem('point_table', msgid, max=maximum)
    ]


# fmt: off
@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        pytest.param('2026-10-09T18:00:00Z', datetime(2026, 10, 9, 18, 0), id='z'),
        pytest.param('2026-10-09T20:00:00+02:00', datetime(2026, 10, 9, 18, 0), id='plus-two'),
        pytest.param('2026-10-09T18:00:00-05:30', datetime(2026, 10, 9, 23, 30), id='minus-5-30'),
        pytest.param('2026-10-09T18:00:00.123456Z', datetime(2026, 10, 9, 18, 0, 0, 123456), id='microseconds'),
        pytest.param('2026-10-09T18:00Z', datetime(2026, 10, 9, 18, 0), id='no-seconds'),
        pytest.param('2026-10-09T18:00:00.' + '0' * 43 + 'Z', datetime(2026, 10, 9, 18, 0), id='64-chars'),
        pytest.param(None, None, id='null'),
    ],
)
# fmt: on
def test_parse_start_time_accepted(value, expected):
    result = parse_document(_with(start_time=value))

    assert result.unwrap().start_time == expected
    assert result.unwrap().start_time is None or (
        result.unwrap().start_time.tzinfo is None
    )


# fmt: off
@pytest.mark.parametrize(
    'value',
    [
        pytest.param('2026-10-09T18:00:00', id='naive'),
        pytest.param('2026-10-09', id='date-only'),
        pytest.param('garbage', id='garbage'),
        pytest.param('', id='empty'),
        pytest.param('2026-13-01T00:00:00Z', id='bad-month'),
        pytest.param('2026-10-09T18:00:00.' + '0' * 44 + 'Z', id='65-chars'),
        pytest.param('x' * 5000, id='very-long'),
        pytest.param('0001-01-01T00:00:00+01:00', id='overflow-below-year-1'),
        pytest.param('9999-12-31T23:59:59-01:00', id='overflow-above-year-9999'),
    ],
)
# fmt: on
def test_parse_start_time_refused(value):
    assert _problems(_with(start_time=value)) == [
        _problem('start_time', document.START_TIME_FORMAT_ERROR)
    ]


UNSTORABLE_KEYS = (
    'name',
    'category',
    'game',
    'description',
    'ruleset',
    'contestant_type',
    'game_format',
    'elimination_mode',
    'score_ordering',
    'playoff_elimination_mode',
    'playoff_release_mode',
    'start_time',
)


# fmt: off
@pytest.mark.parametrize('key', UNSTORABLE_KEYS)
@pytest.mark.parametrize(
    'value',
    [
        pytest.param('\x00', id='nul'),
        pytest.param('a\x00b', id='nul-inside'),
        pytest.param('\ud800', id='lone-high'),
        pytest.param('a\udc00', id='lone-low'),
        pytest.param('\ude00\ud83d', id='reversed-pair'),
        pytest.param('2026-10-09T18:00:00Z\x00', id='nul-after-time'),
    ],
)
# fmt: on
def test_parse_refuses_unstorable_characters(key, value):
    raw = _with(**{key: value})

    assert b'\\u' in raw
    assert _problems(raw) == [
        _problem(key, document.UNSTORABLE_CHARACTER_ERROR)
    ]


@pytest.mark.parametrize('key', TEXT_KEYS + ('game', 'description', 'ruleset'))
def test_parse_accepts_a_valid_surrogate_pair(key):
    raw = _with(**{key: 'x😀y'})

    assert b'\\ud83d\\ude00' in raw
    assert getattr(parse_document(raw).unwrap(), key) == 'x😀y'


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('', False),
        ('plain', False),
        ('Zürich 😀', False),
        ('\x01\x1f\x7f', False),
        (' ', False),
        ('\x00', True),
        ('a\x00', True),
        ('\ud800', True),
        ('\udfff', True),
        ('\ud83d', True),
    ],
)
def test_has_unstorable_character(value, expected):
    assert has_unstorable_character(value) is expected


@pytest.mark.parametrize('where', ['tournament', 'envelope'])
def test_parse_caps_reported_problems(where):
    extra = {f'unknown_{i}': i for i in range(100)}
    if where == 'tournament':
        raw = _with(**extra)
    else:
        raw = _doc(**extra)

    problems = _problems(raw)

    assert len(problems) == MAX_REPORTED_PROBLEMS + 1 == 21
    assert {p.message.msgid for p in problems} == {document.UNKNOWN_KEY_ERROR}


def test_parse_reports_every_field_problem_in_key_order():
    raw = _doc(
        {
            'name': 5,
            'category': 'MAIN',
            'min_players': True,
            'game': 5,
            'ruleset': 'ok',
            'bogus': 1,
        }
    )

    assert _problems(raw) == [
        _problem('', document.UNKNOWN_KEY_ERROR, key='bogus'),
        _problem('name', document.EXPECTED_TEXT_ERROR),
        _problem('game', document.EXPECTED_TEXT_OR_NULL_ERROR),
        _problem('min_players', document.EXPECTED_WHOLE_NUMBER_ERROR),
    ]


# fmt: off
@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        pytest.param('Zürich Cup', 'zurich-cup', id='umlaut'),
        pytest.param('Crème brûlée', 'creme-brulee', id='accents'),
        pytest.param('ＡＢ Cup', 'ab-cup', id='fullwidth'),
        pytest.param('CS:GO "Finals"', 'cs-go-finals', id='quotes'),
        pytest.param('a\r\nb', 'a-b', id='crlf'),
        pytest.param('../../etc/passwd', 'etc-passwd', id='path'),
        pytest.param('a"; filename="x', 'a-filename-x', id='header-injection'),
        pytest.param('', 'tournament', id='empty'),
        pytest.param('  --  ', 'tournament', id='only-separators'),
        pytest.param('日本語', 'tournament', id='no-ascii'),
        pytest.param('x' * 100, 'x' * 40, id='cut-to-40'),
        pytest.param('x' * 39 + ' y', 'x' * 39, id='no-trailing-dash-after-cut'),
    ],
)
# fmt: on
def test_export_filename_is_ascii_slug(name, expected):
    filename = export_filename(name, date(2026, 10, 9))

    assert filename == f'lan-tournament-{expected}-20261009.json'
    assert re.fullmatch(r'lan-tournament-[a-z0-9-]+-\d{8}\.json', filename)
    assert filename.isascii()


def test_worst_case_valid_export_fits_cap():
    config_input = _input(
        name='\x01' * 80,
        game='\x01' * 80,
        description='\x01' * 10_000,
        ruleset='\x01' * 10_000,
        point_table=[-999_999_999] * 64,
        start_time=datetime(2026, 10, 9, 18, 0, 0, 123456),
    )

    raw = serialize(config_input)

    assert len(raw) <= MAX_DOCUMENT_BYTES
    assert len(raw) > 120_000
    assert parse_document(raw).unwrap() == config_input


def test_key_types_cover_every_tournament_key():
    assert set(document._KEY_TYPES) == set(TOURNAMENT_KEYS)
    assert document.REQUIRED_KEYS <= set(TOURNAMENT_KEYS)


def test_document_msgids_have_german():
    msgids = {
        name: value
        for name, value in vars(document).items()
        if name.endswith('_ERROR') and isinstance(value, str)
    }
    assert len(msgids) == 16

    msgids['TOO_MANY_PLACES_MSGID'] = (
        tournament_domain_service.TOO_MANY_PLACES_MSGID
    )
    for name, msgid in msgids.items():
        forms = _german_or_none(_catalog().get(msgid))
        assert forms is not None, f'{name}: no German for {msgid!r}'
        for german in forms:
            assert re.findall(r'%\((\w+)\)s', german) == re.findall(
                r'%\((\w+)\)s', msgid
            ), name


def test_module_imports_are_pure():
    source = Path(document.__file__).read_text(encoding='utf-8')
    imported = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            imported.append(module)
            imported += [f'{module}.{alias.name}' for alias in node.names]

    assert imported
    for name in imported:
        parts = name.split('.')
        assert parts[0] not in {'flask', 'wtforms', 'sqlalchemy'}, name
        assert not name.startswith('byceps.database'), name
        assert not any(part.endswith('_repository') for part in parts), name
