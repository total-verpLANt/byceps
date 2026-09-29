import json
from pathlib import Path
from uuid import UUID

import pytest

from byceps.services.lan_tournament import tournament_match_service
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImageID,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    check_point_count,
    check_point_values,
    create_tournament,
    MAX_POINT_TABLE_PLACES,
    MAX_POINTS_PER_PLACE,
    TournamentSettings,
    validate_tournament_settings,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


_CASES_PATH = Path(__file__).parent / 'data' / 'create_wizard_rule_cases.json'
_CASES = json.loads(_CASES_PATH.read_text(encoding='utf-8'))

_INT_FIELDS = (
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
    'group_size_min',
    'group_size_max',
    'advancement_count',
)


def _enum(enum_class, name):
    return enum_class[name] if name else None


def _settings_from_values(values: dict) -> TournamentSettings:
    """Build settings from raw form strings, as the fixture stores them."""
    point_table = values.get('point_table')
    return TournamentSettings(
        contestant_type=_enum(ContestantType, values.get('contestant_type')),
        game_format=_enum(GameFormat, values.get('game_format')),
        elimination_mode=_enum(EliminationMode, values.get('elimination_mode')),
        score_ordering=_enum(ScoreOrdering, values.get('score_ordering')),
        point_table=(
            [int(points) for points in point_table] if point_table else None
        ),
        **{
            field: int(values[field]) if values.get(field) else None
            for field in _INT_FIELDS
        },
    )


def _settings(**overrides) -> TournamentSettings:
    fields = {
        'contestant_type': None,
        'game_format': None,
        'elimination_mode': None,
        'score_ordering': None,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'point_table': None,
        'group_size_min': None,
        'group_size_max': None,
        'advancement_count': None,
    }
    fields.update(overrides)
    return TournamentSettings(**fields)


def _msgids(settings: TournamentSettings, *, require_structure: bool):
    result = validate_tournament_settings(
        settings, require_structure=require_structure
    )
    if result.is_ok():
        return {}
    return {
        field: message.msgid for field, message in result.unwrap_err().items()
    }


def _errors(settings: TournamentSettings, *, require_structure: bool):
    result = validate_tournament_settings(
        settings, require_structure=require_structure
    )
    assert result.is_err()
    return result.unwrap_err()


@pytest.mark.parametrize('case', _CASES, ids=[case['name'] for case in _CASES])
def test_parity_case(case):
    settings = _settings_from_values(case['values'])

    actual = _msgids(settings, require_structure=case['require_structure'])

    assert actual == case['expected']


def test_parity_fixture_has_enough_cases():
    assert len(_CASES) >= 24
    assert len({case['name'] for case in _CASES}) == len(_CASES)


def test_structure_required_when_require_structure():
    assert _msgids(_settings(), require_structure=True) == {
        'contestant_type': (
            'Please choose whether individuals or teams compete.'
        ),
        'game_format': 'Please choose a game format.',
        'elimination_mode': 'Please choose an elimination mode.',
    }


def test_structure_optional_when_not_require_structure():
    assert validate_tournament_settings(
        _settings(), require_structure=False
    ).is_ok()


def test_highscore_needs_score_ordering_and_skips_mode_required():
    settings = _settings(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
    )

    assert _msgids(settings, require_structure=True) == {
        'score_ordering': 'Please choose which results are better.'
    }


def test_combination_error_lands_on_elimination_mode():
    settings = _settings(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        point_table=[3, 2, 1],
        group_size_max=4,
    )

    errors = _errors(settings, require_structure=True)

    assert list(errors) == ['elimination_mode']


def test_pairs_checked_only_for_chosen_contestant_type():
    values = dict(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        min_players=9,
        max_players=2,
        min_teams=9,
        max_teams=2,
    )

    solo = _errors(
        _settings(contestant_type=ContestantType.SOLO, **values),
        require_structure=True,
    )
    team = _errors(
        _settings(contestant_type=ContestantType.TEAM, **values),
        require_structure=True,
    )

    assert list(solo) == ['max_players']
    assert list(team) == ['max_teams']


def test_pair_error_carries_label_and_minimum_as_params():
    settings = _settings(
        contestant_type=ContestantType.TEAM,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        min_players_in_team=5,
        max_players_in_team=2,
    )

    message = _errors(settings, require_structure=True)['max_players_in_team']

    assert dict(message.params) == {
        'other': 'Min. players per team',
        'n': 5,
    }


def test_ffa_requires_point_table_and_group_size_max():
    settings = _settings(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    assert _msgids(settings, require_structure=True) == {
        'point_table': 'Add points for at least place 1.',
        'group_size_max': 'Required for Free-for-All.',
    }


@pytest.mark.parametrize(
    ('places', 'valid'),
    [
        (MAX_POINT_TABLE_PLACES, True),
        (MAX_POINT_TABLE_PLACES + 1, False),
    ],
)
def test_ffa_point_table_at_most_64_places(places, valid):
    settings = _settings(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=[1] * places,
        group_size_max=4,
    )

    result = validate_tournament_settings(settings, require_structure=True)

    assert MAX_POINT_TABLE_PLACES == 64
    assert result.is_ok() is valid
    if not valid:
        message = result.unwrap_err()['point_table']
        assert message.msgid == 'At most %(max)s places.'
        assert dict(message.params) == {'max': 64}


def _ffa_settings(point_table):
    return _settings(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=point_table,
        group_size_max=4,
    )


def test_point_ceiling_equals_the_match_score_ceiling():
    assert MAX_POINTS_PER_PLACE == tournament_match_service.MAX_MATCH_SCORE
    assert MAX_POINTS_PER_PLACE < 2**31


_TOO_HIGH = ('Points may be at most %(max)s.', {'max': 999_999_999})
_TOO_LOW = ('Points may be at least %(min)s.', {'min': -999_999_999})


# fmt: off
@pytest.mark.parametrize(
    ('point_table', 'expected'),
    [
        ([MAX_POINTS_PER_PLACE, 0],                         None),
        ([10, -MAX_POINTS_PER_PLACE],                       None),
        ([10, MAX_POINTS_PER_PLACE + 1],                    _TOO_HIGH),
        ([10**12],                                          _TOO_HIGH),
        ([10**300],                                         _TOO_HIGH),
        ([-MAX_POINTS_PER_PLACE - 1],                       _TOO_LOW),
        ([10, -(10**300)],                                  _TOO_LOW),
        ([-MAX_POINTS_PER_PLACE - 1, MAX_POINTS_PER_PLACE + 1], _TOO_HIGH),
    ],
)
# fmt: on
def test_ffa_point_values_are_bounded_on_both_sides(point_table, expected):
    result = validate_tournament_settings(
        _ffa_settings(point_table), require_structure=True
    )

    assert result.is_ok() is (expected is None)
    if expected is not None:
        message = result.unwrap_err()['point_table']
        assert (message.msgid, dict(message.params)) == expected


def test_too_many_places_are_reported_before_too_high_values():
    result = validate_tournament_settings(
        _ffa_settings([10**12] * (MAX_POINT_TABLE_PLACES + 1)),
        require_structure=True,
    )

    assert result.unwrap_err()['point_table'].msgid == 'At most %(max)s places.'


def test_check_point_values_returns_nothing_for_a_valid_table():
    assert check_point_values([10, 8, 0, -1]) is None


def test_check_point_count_accepts_the_highest_allowed_count():
    assert check_point_count([1] * MAX_POINT_TABLE_PLACES) is None


def test_check_point_count_names_the_limit_for_one_place_too_many():
    message = check_point_count([1] * (MAX_POINT_TABLE_PLACES + 1))

    assert message is not None
    assert message.msgid == 'At most %(max)s places.'
    assert dict(message.params) == {'max': 64}


def test_group_size_min_not_above_max():
    settings = _settings(
        contestant_type=ContestantType.TEAM,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        point_table=[3, 2, 1],
        group_size_min=6,
        group_size_max=4,
    )

    message = _errors(settings, require_structure=True)['group_size_min']

    assert (
        message.msgid == 'Must not be larger than the max. group size (%(n)s).'
    )
    assert dict(message.params) == {'n': 4}


def test_group_min_infeasible_for_max_teams_and_max_players():
    common = dict(
        game_format=GameFormat.FREE_FOR_ALL,
        point_table=[3, 2, 1],
        group_size_min=4,
        group_size_max=6,
    )

    team = _errors(
        _settings(
            contestant_type=ContestantType.TEAM,
            elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
            max_teams=3,
            **common,
        ),
        require_structure=True,
    )['group_size_min']
    solo = _errors(
        _settings(
            contestant_type=ContestantType.SOLO,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            max_players=3,
            **common,
        ),
        require_structure=True,
    )['group_size_min']

    assert team.msgid.startswith('With at most %(n)s teams')
    assert dict(team.params) == {'n': 3, 'min': 4}
    assert solo.msgid.startswith('With at most %(n)s players')
    assert dict(solo.params) == {'n': 3, 'min': 4}


def test_advancement_below_smallest_group():
    settings = _settings(
        contestant_type=ContestantType.TEAM,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        point_table=[3, 2, 1],
        group_size_min=4,
        group_size_max=6,
        advancement_count=4,
    )

    message = _errors(settings, require_structure=True)['advancement_count']

    assert message.msgid == (
        'Fewer than %(n)s must advance from the smallest group (%(n)s teams).'
    )
    assert dict(message.params) == {'n': 4}


def test_only_the_first_error_per_field_is_kept():
    settings = _settings(
        contestant_type=ContestantType.TEAM,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        point_table=[3, 2, 1],
        max_teams=1,
        group_size_min=6,
        group_size_max=4,
    )

    message = _errors(settings, require_structure=True)['group_size_min']

    assert message.msgid.startswith('Must not be larger')


def test_domain_create_tournament_passes_image_fields_and_token():
    image_id = TournamentImageID(generate_uuid())
    token = UUID('0196f6b5-3c1a-7d3e-9b0a-6b1f0c2d4e5f')

    tournament, _ = create_tournament(
        PartyID('lan-2025'),
        'Cover Cup',
        image_id=image_id,
        image_alt_text='Banner',
        creation_token=token,
    )

    assert tournament.image_id == image_id
    assert tournament.image_alt_text == 'Banner'
    assert tournament.creation_token == token


def test_domain_create_tournament_defaults_image_fields_and_token_to_none():
    tournament, _ = create_tournament(PartyID('lan-2025'), 'Plain Cup')

    assert tournament.image_id is None
    assert tournament.image_alt_text is None
    assert tournament.creation_token is None
