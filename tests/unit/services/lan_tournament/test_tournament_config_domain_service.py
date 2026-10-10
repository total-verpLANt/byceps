"""
tests.unit.services.lan_tournament.test_tournament_config_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import ast
import dataclasses
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
import time
from typing import Any
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_config_document as config_document,
    tournament_config_domain_service as config_service,
    tournament_domain_service,
    tournament_request_domain_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.validation_message import (
    ValidationMessage,
)
from byceps.services.lan_tournament.tournament_config_domain_service import (
    AT_LEAST_TWO_ERROR,
    AT_MOST_ERROR,
    CATEGORY_INVALID_ERROR,
    LENGTH_ERROR,
    MIN_ABOVE_MAX_ERROR,
    NAME_REQUIRED_ERROR,
    normalize_config,
    NUMBER_AT_LEAST_ERROR,
    playoff_config,
    POINT_TABLE_TEXT_ERROR,
    TournamentConfig,
    TournamentConfigInput,
    UNSTORABLE_CHARACTER_ERROR,
    WHOLE_NUMBER_ERROR,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    TournamentSettings,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


MAX_COUNT = tournament_request_domain_service.MAX_PARTICIPANT_LIMIT

SOLO_SE = {
    'contestant_type': 'SOLO',
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'SINGLE_ELIMINATION',
}
SOLO_FFA = {
    'contestant_type': 'SOLO',
    'game_format': 'FREE_FOR_ALL',
    'elimination_mode': 'SINGLE_ELIMINATION',
    'min_players': 8,
    'max_players': 32,
    'point_table': [10, 7, 5],
    'group_size_min': 3,
    'group_size_max': 4,
    'advancement_count': 2,
}
ROUND_ROBIN_PLAYOFFS = {
    'contestant_type': 'SOLO',
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'ROUND_ROBIN',
    'min_players': 12,
    'max_players': 24,
    'playoff_enabled': True,
    'playoff_group_count': 3,
    'playoff_qualifiers_per_group': 2,
    'playoff_elimination_mode': 'SINGLE_ELIMINATION',
    'playoff_release_mode': 'MANUAL',
}
HIGHSCORE_PLAYOFFS = {
    'contestant_type': 'SOLO',
    'game_format': 'HIGHSCORE',
    'score_ordering': 'HIGHER_IS_BETTER',
    'min_players': 8,
    'max_players': 48,
    'playoff_enabled': True,
    'playoff_qualifier_count': 16,
    'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
    'playoff_release_mode': 'AUTOMATIC',
    'point_table': [10, 7, 5, 3],
    'group_size_min': 3,
    'group_size_max': 4,
    'advancement_count': 2,
    'points_carry_to_losers': True,
}
HIGHSCORE_SINGLE_ELIMINATION_PLAYOFFS = HIGHSCORE_PLAYOFFS | {
    'playoff_elimination_mode': 'SINGLE_ELIMINATION',
    'advancement_count': 1,
}


def _input(**overrides) -> TournamentConfigInput:
    return TournamentConfigInput(
        **{'name': 'Cup', 'category': 'MAIN'} | overrides
    )


def _settings(**overrides) -> TournamentSettings:
    values = {f.name: None for f in dataclasses.fields(TournamentSettings)}
    return TournamentSettings(**values | overrides)


def _config(settings: TournamentSettings, **overrides) -> TournamentConfig:
    values: dict[str, Any] = {
        'name': 'Cup',
        'category': TournamentCategory.MAIN,
        'game': None,
        'description': None,
        'ruleset': None,
        'start_time': None,
        'points_carry_to_losers': None,
    }
    return TournamentConfig(**values | overrides, settings=settings)


def _errors(**overrides) -> dict[str, ValidationMessage]:
    match normalize_config(_input(**overrides)):
        case Err(errors):
            return errors
        case Ok(config):
            pytest.fail(f'expected errors, got {config}')


def _config_of(**overrides) -> TournamentConfig:
    match normalize_config(_input(**overrides)):
        case Ok(config):
            return config
        case Err(errors):
            pytest.fail(f'expected a config, got {errors}')


# fmt: off
_WIZARD_CASES = [
    pytest.param(
        SOLO_SE | {
            'min_players': 4,
            'max_players': 16,
            'min_teams': 2,
            'max_teams': 8,
            'min_players_in_team': 2,
            'max_players_in_team': 3,
            'score_ordering': 'HIGHER_IS_BETTER',
        },
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            min_players=4,
            max_players=16,
        )),
        id='solo_clears_team_fields_and_score_ordering',
    ),
    pytest.param(
        {
            'contestant_type': 'TEAM',
            'game_format': 'ONE_V_ONE',
            'elimination_mode': 'DOUBLE_ELIMINATION',
            'min_players': 2,
            'max_players': 8,
            'min_teams': 4,
            'max_teams': 16,
            'min_players_in_team': 2,
            'max_players_in_team': 3,
        },
        _config(_settings(
            contestant_type=ContestantType.TEAM,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
            min_teams=4,
            max_teams=16,
            min_players_in_team=2,
            max_players_in_team=3,
        )),
        id='team_clears_player_fields',
    ),
    pytest.param(
        {
            'contestant_type': 'SOLO',
            'game_format': 'HIGHSCORE',
            'elimination_mode': 'SINGLE_ELIMINATION',
            'score_ordering': 'LOWER_IS_BETTER',
            'max_players': 64,
        },
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            score_ordering=ScoreOrdering.LOWER_IS_BETTER,
            max_players=64,
        )),
        id='highscore_forces_elimination_none',
    ),
    pytest.param(
        ROUND_ROBIN_PLAYOFFS | {'playoff_qualifier_count': 99},
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            min_players=12,
            max_players=24,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=3,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
        )),
        id='round_robin_playoffs_drop_qualifier_count',
    ),
    pytest.param(
        ROUND_ROBIN_PLAYOFFS | {'playoff_enabled': False},
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            min_players=12,
            max_players=24,
        )),
        id='playoff_switch_off_drops_stale_values',
    ),
    pytest.param(
        HIGHSCORE_PLAYOFFS | {'point_table': '10, 7, 5, 3'},
        _config(
            _settings(
                contestant_type=ContestantType.SOLO,
                game_format=GameFormat.HIGHSCORE,
                elimination_mode=EliminationMode.NONE,
                score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
                min_players=8,
                max_players=48,
                point_table=[10, 7, 5, 3],
                group_size_min=3,
                group_size_max=4,
                advancement_count=2,
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
                playoff_qualifier_count=16,
                playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
            ),
            points_carry_to_losers=True,
        ),
        id='highscore_playoffs_carry_ffa_fields',
    ),
    pytest.param(
        SOLO_FFA | {'points_carry_to_losers': True},
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            min_players=8,
            max_players=32,
            point_table=[10, 7, 5],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
        )),
        id='ffa_single_elimination_ignores_carry_flag',
    ),
    pytest.param(
        {
            'contestant_type': 'TEAM',
            'game_format': 'FREE_FOR_ALL',
            'elimination_mode': 'DOUBLE_ELIMINATION',
            'min_teams': 8,
            'max_teams': 24,
            'min_players_in_team': 2,
            'max_players_in_team': 3,
            'point_table': [10, 8, 6, 4, 2, 1],
            'group_size_min': 4,
            'group_size_max': 6,
            'advancement_count': 2,
            'points_carry_to_losers': True,
        },
        _config(
            _settings(
                contestant_type=ContestantType.TEAM,
                game_format=GameFormat.FREE_FOR_ALL,
                elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
                min_teams=8,
                max_teams=24,
                min_players_in_team=2,
                max_players_in_team=3,
                point_table=[10, 8, 6, 4, 2, 1],
                group_size_min=4,
                group_size_max=6,
                advancement_count=2,
            ),
            points_carry_to_losers=True,
        ),
        id='ffa_double_elimination_keeps_carry_flag',
    ),
    pytest.param(
        SOLO_FFA | {
            'elimination_mode': 'DOUBLE_ELIMINATION',
            'points_carry_to_losers': False,
        },
        _config(
            _settings(
                contestant_type=ContestantType.SOLO,
                game_format=GameFormat.FREE_FOR_ALL,
                elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
                min_players=8,
                max_players=32,
                point_table=[10, 7, 5],
                group_size_min=3,
                group_size_max=4,
                advancement_count=2,
            ),
            points_carry_to_losers=False,
        ),
        id='ffa_double_elimination_keeps_false_carry_flag',
    ),
    pytest.param(
        SOLO_FFA | {'point_table': '10, 8,, 6 '},
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            min_players=8,
            max_players=32,
            point_table=[10, 8, 6],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
        )),
        id='ffa_point_table_text_is_parsed',
    ),
    pytest.param(
        SOLO_SE | {
            'point_table': [5, 3],
            'group_size_min': 3,
            'group_size_max': 4,
            'advancement_count': 2,
            'points_carry_to_losers': True,
        },
        _config(_settings(
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        )),
        id='non_ffa_drops_ffa_fields',
    ),
    pytest.param(
        SOLO_SE | {
            'name': '  Cup  ',
            'category': 'FUN',
            'game': ' Chess ',
            'description': 'a\r\nb\rc ',
            'ruleset': '\n rules\n',
            'start_time': datetime(2026, 10, 9, 18, 0),
        },
        _config(
            _settings(
                contestant_type=ContestantType.SOLO,
                game_format=GameFormat.ONE_V_ONE,
                elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            ),
            category=TournamentCategory.FUN,
            game='Chess',
            description='a\nb\nc',
            ruleset='rules',
            start_time=datetime(2026, 10, 9, 18, 0),
        ),
        id='text_fields_are_stripped_and_newlines_normalised',
    ),
]
# fmt: on


@pytest.mark.parametrize(('overrides', 'expected'), _WIZARD_CASES)
def test_normalize_config_matches_wizard_cases(overrides, expected):
    assert normalize_config(_input(**overrides)) == Ok(expected)


# fmt: off
@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('', ValidationMessage(NAME_REQUIRED_ERROR)),
        ('   ', ValidationMessage(NAME_REQUIRED_ERROR)),
        (
            'n' * 81,
            ValidationMessage(LENGTH_ERROR, (('max', 80), ('length', 81))),
        ),
        (
            ' ' + 'n' * 81 + ' ',
            ValidationMessage(LENGTH_ERROR, (('max', 80), ('length', 81))),
        ),
        ('n' * 80, None),
        (' ' + 'n' * 80 + ' ', None),
    ],
)
# fmt: on
def test_normalize_config_name_rules(name, expected):
    result = normalize_config(_input(**SOLO_SE, name=name))

    if expected is None:
        assert result.unwrap().name == name.strip()
    else:
        assert result.unwrap_err() == {'name': expected}


# fmt: off
@pytest.mark.parametrize(
    ('field', 'raw', 'expected_length'),
    [
        ('description', 'a\r\n' * 3_334, None),
        ('ruleset', 'a\r\n' * 3_334, None),
        ('description', 'a' + '\r' * 10_000, 10_001),
        ('ruleset', 'a' + '\r' * 10_000, 10_001),
        ('description', 'a' * 10_000, None),
        ('description', 'a' * 10_001, 10_001),
        ('game', 'g' * 80, None),
        ('game', 'g' * 81, 81),
    ],
)
# fmt: on
def test_normalize_config_text_length_counts_normalized_newlines(
    field, raw, expected_length
):
    result = normalize_config(_input(**SOLO_SE, **{field: raw}))

    if expected_length is None:
        assert getattr(result.unwrap(), field) == raw.replace(
            '\r\n', '\n'
        ).replace('\r', '\n').strip()
    else:
        max_length = 80 if field == 'game' else 10_000
        assert result.unwrap_err() == {
            field: ValidationMessage(
                LENGTH_ERROR, (('max', max_length), ('length', expected_length))
            )
        }


@pytest.mark.parametrize(
    ('field', 'raw'),
    [
        ('game', ' ' * 81),
        ('description', ' ' * 10_001),
        ('ruleset', ' ' * 10_001),
        ('description', '\r\n' * 6_000),
    ],
)
def test_normalize_config_whitespace_only_text_skips_length(field, raw):
    config = _config_of(**SOLO_SE, **{field: raw})

    assert getattr(config, field) == ''


@pytest.mark.parametrize('field', ['game', 'description', 'ruleset'])
def test_normalize_config_keeps_empty_and_none_apart(field):
    assert getattr(_config_of(**SOLO_SE, **{field: ''}), field) == ''
    assert getattr(_config_of(**SOLO_SE, **{field: None}), field) is None


# fmt: off
@pytest.mark.parametrize(
    ('field', 'raw'),
    [
        ('name', 'A\x00B'),
        ('name', 'A\ud800B'),
        ('name', '\ud800'),
        ('name', '\x00' + 'n' * 80),
        ('game', 'A\x00B'),
        ('game', 'A\ud800B'),
        ('game', '\ud800'),
        ('game', '\x00' + 'g' * 80),
        ('description', 'A\x00B'),
        ('description', 'A\ud800B'),
        ('description', '\ud800'),
        ('description', '\x00' + 'd' * 10_000),
        ('ruleset', 'A\x00B'),
        ('ruleset', 'A\ud800B'),
        ('ruleset', '\ud800'),
        ('ruleset', '\x00' + 'r' * 10_000),
    ],
)
# fmt: on
def test_normalize_config_refuses_unstorable_characters_before_length(
    field, raw
):
    assert _errors(**SOLO_SE, **{field: raw}) == {
        field: ValidationMessage(UNSTORABLE_CHARACTER_ERROR)
    }


def test_normalize_config_reports_unstorable_characters_per_field():
    errors = _errors(**SOLO_SE | {'name': 'A\x00B', 'ruleset': 'R\ud800'})

    assert errors == {
        'name': ValidationMessage(UNSTORABLE_CHARACTER_ERROR),
        'ruleset': ValidationMessage(UNSTORABLE_CHARACTER_ERROR),
    }


def test_config_document_reexports_the_unstorable_character_check():
    assert (
        config_document.has_unstorable_character
        is config_service.has_unstorable_character
    )
    assert (
        config_document.UNSTORABLE_CHARACTER_ERROR
        == config_service.UNSTORABLE_CHARACTER_ERROR
    )


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'max_field', 'label', 'n'),
    [
        (
            SOLO_SE | {'min_players': 4, 'max_players': 16,
                       'min_teams': 8, 'max_teams': 2},
            'max_teams', 'Min. teams', 8,
        ),
        (
            {'contestant_type': 'TEAM', 'game_format': 'ONE_V_ONE',
             'elimination_mode': 'SINGLE_ELIMINATION',
             'min_teams': 4, 'max_teams': 16,
             'min_players': 9, 'max_players': 3},
            'max_players', 'Min. players', 9,
        ),
        (
            SOLO_SE | {'min_players': 4, 'max_players': 16,
                       'min_players_in_team': 5, 'max_players_in_team': 2},
            'max_players_in_team', 'Min. players per team', 5,
        ),
    ],
)
# fmt: on
def test_normalize_config_checks_all_raw_pairs_before_clearing(
    overrides, max_field, label, n
):
    assert _errors(**overrides) == {
        max_field: ValidationMessage(
            MIN_ABOVE_MAX_ERROR, (('other', label), ('n', n))
        )
    }


def test_normalize_config_advancement_zero_uses_number_at_least():
    errors = _errors(**SOLO_FFA | {'advancement_count': 0})

    assert errors['advancement_count'] == ValidationMessage(
        'Number must be at least %(min)s.', (('min', 1),)
    )
    assert NUMBER_AT_LEAST_ERROR == 'Number must be at least %(min)s.'


@pytest.mark.parametrize('contestant_type', [None, ''])
def test_normalize_config_blank_type_is_refused_even_with_team_size(
    contestant_type,
):
    errors = _errors(
        contestant_type=contestant_type,
        game_format='ONE_V_ONE',
        elimination_mode='SINGLE_ELIMINATION',
        min_teams=4,
        max_teams=8,
        min_players_in_team=2,
        max_players_in_team=3,
    )

    assert errors == {
        'contestant_type': ValidationMessage(
            'Please choose whether individuals or teams compete.'
        )
    }


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'field', 'msgid'),
    [
        ({'category': 'main'}, 'category', CATEGORY_INVALID_ERROR),
        ({'category': ''}, 'category', CATEGORY_INVALID_ERROR),
        ({'category': None}, 'category', CATEGORY_INVALID_ERROR),
        (SOLO_SE | {'contestant_type': 'DUO'},
         'contestant_type', 'Invalid contestant type selected.'),
        (SOLO_SE | {'game_format': 'MOBA'},
         'game_format', 'Invalid game format selected.'),
        (SOLO_SE | {'elimination_mode': 'SWISS'},
         'elimination_mode', 'Invalid elimination mode selected.'),
        (SOLO_SE | {'score_ordering': 'SIDEWAYS'},
         'score_ordering', 'Invalid score ordering selected.'),
        (ROUND_ROBIN_PLAYOFFS | {'playoff_elimination_mode': 'SWISS'},
         'playoff_elimination_mode', 'Invalid elimination mode selected.'),
        (ROUND_ROBIN_PLAYOFFS | {'playoff_release_mode': 'LATER'},
         'playoff_release_mode', 'Invalid release mode selected.'),
        (HIGHSCORE_PLAYOFFS | {'playoff_release_mode': 'LATER'},
         'playoff_release_mode', 'Invalid release mode selected.'),
    ],
)
# fmt: on
def test_normalize_config_unknown_enum_name_is_field_error(
    overrides, field, msgid
):
    assert _errors(**overrides)[field] == ValidationMessage(msgid)


# fmt: off
_COUNT_CASES = [
    ('min_players', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('max_players', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('min_teams', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('max_teams', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('min_players_in_team', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('max_players_in_team', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('group_size_min', AT_LEAST_TWO_ERROR, MAX_COUNT),
    ('group_size_max', AT_LEAST_TWO_ERROR,
     tournament_domain_service.MAX_LOBBY_SIZE),
    ('advancement_count', NUMBER_AT_LEAST_ERROR, MAX_COUNT),
    ('playoff_group_count', WHOLE_NUMBER_ERROR,
     tournament_domain_service.MAX_PLAYOFF_GROUP_COUNT),
    ('playoff_qualifiers_per_group', WHOLE_NUMBER_ERROR, MAX_COUNT),
    ('playoff_qualifier_count', WHOLE_NUMBER_ERROR, MAX_COUNT),
]
# fmt: on


def _below_message(below: str) -> ValidationMessage:
    params = (('min', 1),) if below == NUMBER_AT_LEAST_ERROR else ()
    return ValidationMessage(below, params)


@pytest.mark.parametrize(('field', 'below', 'cap'), _COUNT_CASES)
def test_normalize_config_count_ranges(field, below, cap):
    minimum = 2 if below == AT_LEAST_TWO_ERROR else 1

    for value in (0, -1, minimum - 1):
        assert _errors(**SOLO_SE, **{field: value})[field] == (
            _below_message(below)
        )
    for value in (cap + 1, 10**400):
        assert _errors(**SOLO_SE, **{field: value})[field] == (
            ValidationMessage(AT_MOST_ERROR, (('max', cap),))
        )
    for value in (minimum, cap):
        match normalize_config(_input(**SOLO_SE, **{field: value})):
            case Err(errors):
                assert field not in errors


@pytest.mark.parametrize(
    'overrides',
    [
        SOLO_FFA,
        ROUND_ROBIN_PLAYOFFS,
        HIGHSCORE_PLAYOFFS,
        HIGHSCORE_SINGLE_ELIMINATION_PLAYOFFS,
    ],
)
@pytest.mark.parametrize(('field', 'below', 'cap'), _COUNT_CASES)
def test_normalize_config_hostile_counts_give_range_error(
    overrides, field, below, cap
):
    hostile = (
        -(10**400),
        -(2**1100),
        -(10**30),
        -(10**9),
        -cap,
        10**9,
        10**12,
        10**19,
        2**1100,
        10**400,
    )

    started = time.perf_counter()
    for value in hostile:
        errors = _errors(**overrides | {field: value})

        assert errors[field] == (
            _below_message(below)
            if value < 0
            else ValidationMessage(AT_MOST_ERROR, (('max', cap),))
        )
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize(
    'overrides',
    [HIGHSCORE_PLAYOFFS, HIGHSCORE_SINGLE_ELIMINATION_PLAYOFFS],
    ids=['double_elimination', 'single_elimination'],
)
@pytest.mark.parametrize('qualifier_count', [2, 16])
@pytest.mark.parametrize('group_size_max', [0, -1, -5, -255, False])
@pytest.mark.parametrize('group_size_min', [-1, -5, -10])
def test_normalize_config_two_hostile_group_sizes_give_range_errors(
    overrides, qualifier_count, group_size_max, group_size_min
):
    errors = _errors(
        **overrides
        | {
            'playoff_qualifier_count': qualifier_count,
            'group_size_min': group_size_min,
            'group_size_max': group_size_max,
        }
    )

    assert errors == {
        'group_size_min': ValidationMessage(AT_LEAST_TWO_ERROR),
        'group_size_max': ValidationMessage(AT_LEAST_TWO_ERROR),
    }


# fmt: off
_COUNT_AND_PAIR_ERRORS = [
    *[
        pytest.param(SOLO_FFA | {field: value}, id=f'{field}_{label}')
        for field, below, cap in _COUNT_CASES
        for label, value in (('below', 0), ('above', cap + 1))
    ],
    pytest.param(
        SOLO_SE | {'min_teams': 8, 'max_teams': 2},
        id='pair_min_teams_above_max_teams',
    ),
    pytest.param(
        SOLO_SE | {'min_players': 9, 'max_players': 3},
        id='pair_min_players_above_max_players',
    ),
    pytest.param(
        SOLO_SE | {'min_players_in_team': 5, 'max_players_in_team': 2},
        id='pair_min_players_in_team_above_max_players_in_team',
    ),
]
# fmt: on


@pytest.mark.parametrize('overrides', _COUNT_AND_PAIR_ERRORS)
def test_normalize_config_skips_domain_rules_on_count_or_pair_error(
    monkeypatch, overrides
):
    validate = Mock(
        wraps=tournament_domain_service.validate_tournament_settings
    )
    derive = Mock(wraps=tournament_domain_service.derive_contestant_type)
    monkeypatch.setattr(
        tournament_domain_service, 'validate_tournament_settings', validate
    )
    monkeypatch.setattr(
        tournament_domain_service, 'derive_contestant_type', derive
    )

    assert normalize_config(_input(**overrides)).is_err()

    validate.assert_not_called()
    derive.assert_not_called()


@pytest.mark.parametrize(
    'overrides',
    [
        SOLO_SE,
        SOLO_FFA,
        ROUND_ROBIN_PLAYOFFS,
        HIGHSCORE_PLAYOFFS,
        SOLO_SE | {'name': '', 'category': 'nope', 'game_format': 'MOBA'},
    ],
    ids=['ok_solo', 'ok_ffa', 'ok_round_robin', 'ok_highscore', 'field_errors'],
)
def test_normalize_config_runs_domain_rules_for_in_range_counts(
    monkeypatch, overrides
):
    validate = Mock(
        wraps=tournament_domain_service.validate_tournament_settings
    )
    derive = Mock(wraps=tournament_domain_service.derive_contestant_type)
    monkeypatch.setattr(
        tournament_domain_service, 'validate_tournament_settings', validate
    )
    monkeypatch.setattr(
        tournament_domain_service, 'derive_contestant_type', derive
    )

    normalize_config(_input(**overrides))

    validate.assert_called_once()
    derive.assert_called_once()


def test_tournament_category_names_equal_values():
    assert all(
        category.name == category.value for category in TournamentCategory
    )


def test_normalize_config_start_time_bounds():
    year_error = tournament_request_domain_service.YEAR_RANGE_ERROR_MESSAGE
    in_range = datetime(2026, 10, 9, 18, 0)
    aware = datetime(2026, 10, 9, 18, 0, tzinfo=timezone(timedelta(hours=2)))

    assert _config_of(**SOLO_SE, start_time=in_range).start_time == in_range
    assert _config_of(**SOLO_SE, start_time=aware).start_time == aware
    assert _config_of(**SOLO_SE, start_time=None).start_time is None
    for refused in (
        datetime(1999, 12, 30),
        datetime(2101, 1, 3),
        datetime.min,
        datetime.max,
    ):
        assert _errors(**SOLO_SE, start_time=refused) == {
            'start_time': ValidationMessage(year_error)
        }
    for accepted in (datetime(1999, 12, 31), datetime(2101, 1, 2)):
        assert _config_of(**SOLO_SE, start_time=accepted)


# fmt: off
@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ([10, 7, 5], [10, 7, 5]),
        ('10, 7,, 5 ', [10, 7, 5]),
        ('10,-3', [10, -3]),
    ],
)
# fmt: on
def test_normalize_config_point_table_list_and_text(raw, expected):
    config = _config_of(**SOLO_FFA | {'point_table': raw})

    assert config.settings.point_table == expected


@pytest.mark.parametrize('raw', ['10, x', '1.5', '9' * 5_000])
def test_normalize_config_point_table_text_must_be_integers(raw):
    errors = _errors(**SOLO_FFA | {'point_table': raw})

    assert errors['point_table'] == ValidationMessage(POINT_TABLE_TEXT_ERROR)


@pytest.mark.parametrize('raw', [None, '', ' , ,', []])
def test_normalize_config_point_table_empty_is_refused_for_ffa(raw):
    errors = _errors(**SOLO_FFA | {'point_table': raw})

    assert errors == {
        'point_table': ValidationMessage('Add points for at least place 1.')
    }


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'field', 'msgid'),
    [
        (SOLO_SE | {'elimination_mode': 'NONE'},
         'elimination_mode',
         ('This combination of game format and elimination mode is not '
          'supported.')),
        ({'contestant_type': 'SOLO', 'game_format': 'HIGHSCORE'},
         'score_ordering', 'Please choose which results are better.'),
        ({}, 'game_format', 'Please choose a game format.'),
        ({}, 'contestant_type',
         'Please choose whether individuals or teams compete.'),
        (SOLO_SE | {'playoff_enabled': True}, None, None),
        (SOLO_FFA | {'group_size_max': None}, 'group_size_max',
         'Required for Free-for-All.'),
        (ROUND_ROBIN_PLAYOFFS | {'playoff_group_count': None},
         'playoff_group_count', 'Please enter the number of groups.'),
    ],
)
# fmt: on
def test_normalize_config_runs_domain_rules(overrides, field, msgid):
    if field is None:
        assert normalize_config(_input(**overrides)).is_ok()
    else:
        assert _errors(**overrides)[field] == ValidationMessage(msgid)


def test_normalize_config_first_error_per_field_wins():
    # The range rule runs before the pair rule and the domain rules.
    errors = _errors(**SOLO_SE | {'min_players': 5, 'max_players': 0})
    assert errors == {'max_players': ValidationMessage(WHOLE_NUMBER_ERROR)}

    # An unknown name hides the domain's "please choose".
    errors = _errors(**SOLO_SE | {'game_format': 'MOBA'})
    assert errors['game_format'] == ValidationMessage(
        'Invalid game format selected.'
    )

    # An unknown playoff mode hides the domain's "please choose".
    errors = _errors(
        **ROUND_ROBIN_PLAYOFFS | {'playoff_elimination_mode': 'SWISS'}
    )
    assert errors == {
        'playoff_elimination_mode': ValidationMessage(
            'Invalid elimination mode selected.'
        )
    }

    # One error per field, whatever the number of broken rules.
    errors = _errors(name='', category='nope', **SOLO_SE | {'min_players': 0})
    assert sorted(errors) == ['category', 'min_players', 'name']


# fmt: off
@pytest.mark.parametrize(
    ('kwargs', 'expected_config', 'expected_errors'),
    [
        pytest.param(
            {'enabled': False, 'game_format': GameFormat.ONE_V_ONE,
             'elimination_mode': EliminationMode.ROUND_ROBIN,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': 8,
             'elimination_mode_name': 'SINGLE_ELIMINATION',
             'release_mode_name': 'MANUAL'},
            {}, {},
            id='switch_off',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.ONE_V_ONE,
             'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': 8,
             'elimination_mode_name': 'SINGLE_ELIMINATION',
             'release_mode_name': 'MANUAL'},
            {}, {},
            id='format_without_playoff_phase',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.FREE_FOR_ALL,
             'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': 8,
             'elimination_mode_name': 'SINGLE_ELIMINATION',
             'release_mode_name': 'MANUAL'},
            {}, {},
            id='free_for_all_has_no_playoff_phase',
        ),
        pytest.param(
            {'enabled': True, 'game_format': None,
             'elimination_mode': None,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': 8,
             'elimination_mode_name': 'SINGLE_ELIMINATION',
             'release_mode_name': 'MANUAL'},
            {}, {},
            id='no_format_yet',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.ONE_V_ONE,
             'elimination_mode': EliminationMode.ROUND_ROBIN,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': 8,
             'elimination_mode_name': 'DOUBLE_ELIMINATION',
             'release_mode_name': 'AUTOMATIC'},
            {'playoff_game_format': GameFormat.ONE_V_ONE,
             'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
             'playoff_group_count': 3,
             'playoff_qualifiers_per_group': 2,
             'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC},
            {},
            id='round_robin',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.HIGHSCORE,
             'elimination_mode': EliminationMode.NONE,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': 8,
             'elimination_mode_name': 'SINGLE_ELIMINATION',
             'release_mode_name': 'MANUAL'},
            {'playoff_game_format': GameFormat.FREE_FOR_ALL,
             'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
             'playoff_qualifier_count': 8,
             'playoff_release_mode': PlayoffReleaseMode.MANUAL},
            {},
            id='highscore',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.HIGHSCORE,
             'elimination_mode': EliminationMode.NONE,
             'group_count': None, 'qualifiers_per_group': None,
             'qualifier_count': 8,
             'elimination_mode_name': '', 'release_mode_name': None},
            {'playoff_game_format': GameFormat.FREE_FOR_ALL,
             'playoff_qualifier_count': 8},
            {},
            id='blank_names_stay_unset',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.ONE_V_ONE,
             'elimination_mode': EliminationMode.ROUND_ROBIN,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': None,
             'elimination_mode_name': 'NONE',
             'release_mode_name': 'MANUAL'},
            {'playoff_game_format': GameFormat.ONE_V_ONE,
             'playoff_elimination_mode': EliminationMode.NONE,
             'playoff_group_count': 3,
             'playoff_qualifiers_per_group': 2,
             'playoff_release_mode': PlayoffReleaseMode.MANUAL},
            {},
            id='mode_names_are_not_restricted_here',
        ),
        pytest.param(
            {'enabled': True, 'game_format': GameFormat.ONE_V_ONE,
             'elimination_mode': EliminationMode.ROUND_ROBIN,
             'group_count': 3, 'qualifiers_per_group': 2,
             'qualifier_count': None,
             'elimination_mode_name': 'SWISS',
             'release_mode_name': 'LATER'},
            {'playoff_game_format': GameFormat.ONE_V_ONE,
             'playoff_group_count': 3,
             'playoff_qualifiers_per_group': 2},
            {'playoff_elimination_mode': ValidationMessage(
                'Invalid elimination mode selected.'),
             'playoff_release_mode': ValidationMessage(
                'Invalid release mode selected.')},
            id='unknown_names',
        ),
    ],
)
# fmt: on
def test_playoff_config_gating(kwargs, expected_config, expected_errors):
    config, errors = playoff_config(**kwargs)

    assert tuple(config) == config_service.PLAYOFF_KWARGS
    assert config == dict.fromkeys(config_service.PLAYOFF_KWARGS) | (
        expected_config
    )
    assert errors == expected_errors


def _tournament(**overrides) -> Tournament:
    values: dict[str, Any] = {
        'id': TournamentID(generate_uuid()),
        'party_id': 'party-1',
        'name': 'Cup',
        'game': 'Chess',
        'description': 'Rules apply.',
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': datetime(2026, 10, 1, tzinfo=UTC),
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.DRAFT,
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    return Tournament(**values | overrides)


# fmt: off
@pytest.mark.parametrize(
    'tournament',
    [
        pytest.param(
            _tournament(min_players=4, max_players=16),
            id='solo_single_elimination',
        ),
        pytest.param(
            _tournament(
                contestant_type=ContestantType.TEAM,
                elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
                min_teams=4,
                max_teams=16,
                min_players_in_team=2,
                max_players_in_team=3,
                category=TournamentCategory.STAGE,
            ),
            id='team_double_elimination',
        ),
        pytest.param(
            _tournament(
                elimination_mode=EliminationMode.ROUND_ROBIN,
                min_players=12,
                max_players=24,
                playoff_game_format=GameFormat.ONE_V_ONE,
                playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
                playoff_group_count=3,
                playoff_qualifiers_per_group=2,
                playoff_release_mode=PlayoffReleaseMode.MANUAL,
            ),
            id='round_robin_with_playoffs',
        ),
        pytest.param(
            _tournament(
                game_format=GameFormat.FREE_FOR_ALL,
                min_players=8,
                max_players=32,
                point_table=[10, 7, 5],
                group_size_min=3,
                group_size_max=4,
                advancement_count=2,
            ),
            id='free_for_all_single_elimination',
        ),
        pytest.param(
            _tournament(
                contestant_type=ContestantType.TEAM,
                game_format=GameFormat.FREE_FOR_ALL,
                elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
                min_teams=8,
                max_teams=24,
                min_players_in_team=2,
                max_players_in_team=3,
                point_table=[10, 8, 6, 4, 2, 1],
                group_size_min=4,
                group_size_max=6,
                advancement_count=2,
                points_carry_to_losers=True,
            ),
            id='free_for_all_double_elimination',
        ),
        pytest.param(
            _tournament(
                game_format=GameFormat.HIGHSCORE,
                elimination_mode=EliminationMode.NONE,
                score_ordering=ScoreOrdering.LOWER_IS_BETTER,
                max_players=64,
            ),
            id='highscore',
        ),
        pytest.param(
            _tournament(
                game_format=GameFormat.HIGHSCORE,
                elimination_mode=EliminationMode.NONE,
                score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
                min_players=8,
                max_players=48,
                point_table=[10, 7, 5, 3],
                group_size_min=3,
                group_size_max=4,
                advancement_count=2,
                points_carry_to_losers=True,
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
                playoff_qualifier_count=16,
                playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
                start_time=datetime(2026, 11, 1, 18, 0),
            ),
            id='highscore_with_free_for_all_playoffs',
        ),
    ],
)
# fmt: on
def test_config_input_of_round_trips(tournament):
    config = normalize_config(
        config_service.config_input_of(tournament)
    ).unwrap()

    assert config.settings == tournament_service._settings_of(tournament)
    assert config.name == tournament.name
    assert config.category == tournament.category
    assert config.game == tournament.game
    assert config.description == tournament.description
    assert config.ruleset == tournament.ruleset
    assert config.start_time == tournament.start_time
    assert config.points_carry_to_losers == tournament.points_carry_to_losers


def test_config_input_of_exports_names_and_naive_utc_start_time():
    tournament = _tournament(
        category=TournamentCategory.USER_ORGANIZED,
        start_time=datetime(
            2026, 11, 1, 20, 0, tzinfo=timezone(timedelta(hours=2))
        ),
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        point_table=[3, 2, 1],
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=8,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )

    config_input = config_service.config_input_of(tournament)

    assert config_input.category == 'USER_ORGANIZED'
    assert config_input.start_time == datetime(2026, 11, 1, 18, 0)
    assert config_input.start_time.tzinfo is None
    assert config_input.contestant_type == 'SOLO'
    assert config_input.game_format == 'HIGHSCORE'
    assert config_input.elimination_mode == 'NONE'
    assert config_input.score_ordering == 'HIGHER_IS_BETTER'
    assert config_input.point_table == [3, 2, 1]
    assert config_input.point_table is not tournament.point_table
    assert config_input.points_carry_to_losers is False
    assert config_input.playoff_enabled is True
    assert config_input.playoff_elimination_mode == 'SINGLE_ELIMINATION'
    assert config_input.playoff_release_mode == 'MANUAL'
    assert config_service.config_input_of(_tournament()).playoff_enabled is False


def test_module_imports_are_pure():
    source = Path(config_service.__file__).read_text(encoding='utf-8')
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
