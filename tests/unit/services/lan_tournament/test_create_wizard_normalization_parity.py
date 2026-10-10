"""
tests.unit.services.lan_tournament.test_create_wizard_normalization_parity
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest
from wtforms.validators import Length

from byceps.services.lan_tournament import tournament_config_domain_service
from byceps.services.lan_tournament.blueprints.admin import forms
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)

from tests.unit.services.lan_tournament.test_create_wizard_playoffs import (
    _HIGHSCORE,
    _ROUND_ROBIN,
)
from tests.unit.services.lan_tournament.test_create_wizard_views_admin import (
    _post,
    _VALID_DATA,
    app,  # noqa: F401 -- imported fixture
)


_KWARGS = (
    'category',
    'game',
    'description',
    'image_url',
    'ruleset',
    'start_time',
    'min_players',
    'max_players',
    'min_teams',
    'max_teams',
    'min_players_in_team',
    'max_players_in_team',
    'contestant_type',
    'game_format',
    'elimination_mode',
    'score_ordering',
    'point_table',
    'advancement_count',
    'group_size_min',
    'group_size_max',
    'points_carry_to_losers',
    'playoff_game_format',
    'playoff_elimination_mode',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_release_mode',
)

# What `_VALID_DATA` becomes: every other kwarg stays `None`.
_DEFAULTS = dict.fromkeys(_KWARGS) | {
    'name': 'New Tournament',
    'category': TournamentCategory.MAIN,
    'contestant_type': ContestantType.SOLO,
    'game_format': GameFormat.ONE_V_ONE,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
}

_FFA = {
    **_VALID_DATA,
    'game_format': 'FREE_FOR_ALL',
    'point_table': '10, 8 ,6',
    'group_size_min': '3',
    'group_size_max': '4',
    'advancement_count': '2',
}
_FFA_KWARGS = {
    'game_format': GameFormat.FREE_FOR_ALL,
    'point_table': [10, 8, 6],
    'group_size_min': 3,
    'group_size_max': 4,
    'advancement_count': 2,
}

_FFA_WITHOUT_TABLE = {k: v for k, v in _FFA.items() if k != 'point_table'}
_BLANK_TYPE = {k: v for k, v in _VALID_DATA.items() if k != 'contestant_type'}
_ROUND_ROBIN_SWITCH_OFF = {
    k: v for k, v in _ROUND_ROBIN.items() if k != 'playoff_enabled'
}


def _create_kwargs(mocks) -> dict:
    create = mocks.tournament_svc.create_tournament
    create.assert_called_once()
    call = create.call_args
    return {'name': call.args[1]} | {key: call.kwargs[key] for key in _KWARGS}


def _field_errors(form) -> dict[str, list[str]]:
    return {
        field.name: [str(error) for error in field.errors]
        for field in form
        if field.errors
    }


# fmt: off
@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        pytest.param(
            {
                **_VALID_DATA,
                'min_players': '4',
                'max_players': '16',
                'min_teams': '2',
                'max_teams': '8',
                'min_players_in_team': '2',
                'max_players_in_team': '4',
            },
            {'min_players': 4, 'max_players': 16},
            id='solo-clears-team-fields',
        ),
        pytest.param(
            {
                **_VALID_DATA,
                'contestant_type': 'TEAM',
                'elimination_mode': 'DOUBLE_ELIMINATION',
                'min_players': '4',
                'max_players': '16',
                'min_teams': '4',
                'max_teams': '16',
                'min_players_in_team': '2',
                'max_players_in_team': '5',
            },
            {
                'contestant_type': ContestantType.TEAM,
                'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'min_teams': 4,
                'max_teams': 16,
                'min_players_in_team': 2,
                'max_players_in_team': 5,
            },
            id='team-clears-player-fields',
        ),
        pytest.param(
            {**_VALID_DATA, 'game': ' ' * 81},
            {'game': ''},
            id='game-of-81-spaces-is-accepted',
        ),
        pytest.param(
            {
                **_VALID_DATA,
                'game_format': 'HIGHSCORE',
                'elimination_mode': 'SINGLE_ELIMINATION',
                'score_ordering': 'LOWER_IS_BETTER',
            },
            {
                'game_format': GameFormat.HIGHSCORE,
                'elimination_mode': EliminationMode.NONE,
                'score_ordering': ScoreOrdering.LOWER_IS_BETTER,
            },
            id='highscore-forces-elimination-none',
        ),
        pytest.param(
            {**_VALID_DATA, 'score_ordering': 'HIGHER_IS_BETTER'},
            {},
            id='one-v-one-clears-score-ordering',
        ),
        pytest.param(
            _FFA,
            _FFA_KWARGS,
            id='ffa-se-parses-point-table',
        ),
        pytest.param(
            {
                **_FFA,
                'elimination_mode': 'DOUBLE_ELIMINATION',
                'points_carry_to_losers': 'y',
            },
            {
                **_FFA_KWARGS,
                'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'points_carry_to_losers': True,
            },
            id='ffa-de-carry-on',
        ),
        pytest.param(
            {**_FFA, 'elimination_mode': 'DOUBLE_ELIMINATION'},
            {
                **_FFA_KWARGS,
                'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'points_carry_to_losers': False,
            },
            id='ffa-de-carry-off',
        ),
        pytest.param(
            _ROUND_ROBIN,
            {
                'name': 'Group stage',
                'elimination_mode': EliminationMode.ROUND_ROBIN,
                'min_players': 12,
                'max_players': 24,
                'playoff_game_format': GameFormat.ONE_V_ONE,
                'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                'playoff_group_count': 3,
                'playoff_qualifiers_per_group': 2,
                'playoff_release_mode': PlayoffReleaseMode.MANUAL,
            },
            id='round-robin-with-playoffs',
        ),
        pytest.param(
            _ROUND_ROBIN_SWITCH_OFF,
            {
                'name': 'Group stage',
                'elimination_mode': EliminationMode.ROUND_ROBIN,
                'min_players': 12,
                'max_players': 24,
            },
            id='round-robin-playoff-switch-off',
        ),
        pytest.param(
            _HIGHSCORE,
            {
                'name': 'Leaderboard',
                'game_format': GameFormat.HIGHSCORE,
                'elimination_mode': EliminationMode.NONE,
                'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
                'min_players': 8,
                'max_players': 48,
                'point_table': [10, 7, 5, 3],
                'group_size_min': 3,
                'group_size_max': 4,
                'advancement_count': 2,
                'points_carry_to_losers': True,
                'playoff_game_format': GameFormat.FREE_FOR_ALL,
                'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'playoff_qualifier_count': 16,
                'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
            },
            id='highscore-with-ffa-playoffs',
        ),
        pytest.param(
            {
                **_VALID_DATA,
                'playoff_enabled': 'y',
                'playoff_group_count': '3',
                'playoff_qualifiers_per_group': '2',
                'playoff_elimination_mode': 'SINGLE_ELIMINATION',
                'playoff_release_mode': 'MANUAL',
            },
            {},
            id='one-v-one-se-playoff-switch-has-no-playoff',
        ),
        pytest.param(
            {**_VALID_DATA, 'point_table': 'x,y'},
            {},
            id='one-v-one-ignores-point-table',
        ),
        pytest.param(
            {**_VALID_DATA, 'description': 'a\r\nb'},
            {'description': 'a\nb'},
            id='description-newlines-normalised',
        ),
        pytest.param(
            {**_VALID_DATA, 'game': '  Quake '},
            {'game': 'Quake'},
            id='game-is-stripped',
        ),
        pytest.param(
            {**_VALID_DATA, 'category': 'FUN'},
            {'category': TournamentCategory.FUN},
            id='category-by-value',
        ),
        pytest.param(
            {**_VALID_DATA, 'game': '', 'description': '   '},
            {'description': ''},
            id='empty-game-and-blank-description',
        ),
        pytest.param(
            {**_VALID_DATA, 'ruleset': ' ' * 10_001},
            {'ruleset': ''},
            id='ruleset-of-10001-spaces-is-accepted',
        ),
        pytest.param(
            {**_VALID_DATA, 'name': '  Padded name  '},
            {'name': 'Padded name'},
            id='name-is-stripped',
        ),
        pytest.param(
            {**_VALID_DATA, 'image_url': ' https://example.org/a.png '},
            {'image_url': 'https://example.org/a.png'},
            id='image-url-is-stripped',
        ),
    ],
)
# fmt: on
def test_create_kwargs_match_golden(app, data, expected):  # noqa: F811
    mocks = _post(app, data)

    assert _create_kwargs(mocks) == {**_DEFAULTS, **expected}


# fmt: off
@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        pytest.param(
            {**_FFA, 'point_table': 'x'},
            {'point_table': ['Point table must be comma-separated integers.']},
            id='ffa-point-table-not-integers',
        ),
        pytest.param(
            _FFA_WITHOUT_TABLE,
            {'point_table': ['Add points for at least place 1.']},
            id='ffa-without-point-table',
        ),
        pytest.param(
            {**_VALID_DATA, 'elimination_mode': 'NONE'},
            {
                'elimination_mode': [
                    (
                        'This combination of game format and elimination '
                        'mode is not supported.'
                    )
                ]
            },
            id='invalid-format-and-mode-combination',
        ),
        pytest.param(
            _BLANK_TYPE,
            {
                'contestant_type': [
                    'Please choose whether individuals or teams compete.'
                ]
            },
            id='blank-contestant-type',
        ),
        pytest.param(
            {**_BLANK_TYPE, 'max_players_in_team': '5'},
            {
                'contestant_type': [
                    'Please choose whether individuals or teams compete.'
                ]
            },
            id='blank-contestant-type-with-team-size-5',
        ),
        pytest.param(
            {**_VALID_DATA, 'min_teams': '8', 'max_teams': '2'},
            {'max_teams': ['Must be at least "Min. teams" (8).']},
            id='solo-min-teams-above-max-teams',
        ),
        pytest.param(
            {**_FFA, 'advancement_count': '0'},
            {'advancement_count': ['Number must be at least 1.']},
            id='ffa-advancement-count-zero',
        ),
        pytest.param(
            {**_VALID_DATA, 'game_format': 'BOGUS'},
            {'game_format': ['Not a valid choice.']},
            id='tampered-game-format',
        ),
    ],
)
# fmt: on
def test_create_field_errors_match_golden(app, data, expected):  # noqa: F811
    mocks = _post(app, data)

    mocks.tournament_svc.create_tournament.assert_not_called()
    mocks.flash_error.assert_not_called()
    mocks.create_form.assert_called_once()
    assert _field_errors(mocks.form) == expected
    assert list(mocks.form.form_errors) == []


def test_form_caps_match_config_caps(app):  # noqa: F811
    with app.app_context():
        form = forms.TournamentCreateForm()

    length_caps = {
        name: [
            validator.max
            for validator in getattr(form, name).validators
            if isinstance(validator, Length)
        ]
        for name in ('name', 'game', 'description', 'ruleset')
    }

    assert length_caps == {
        'name': [tournament_config_domain_service.MAX_NAME_LENGTH],
        'game': [tournament_config_domain_service.MAX_GAME_LENGTH],
        'description': [tournament_config_domain_service.MAX_TEXT_LENGTH],
        'ruleset': [tournament_config_domain_service.MAX_TEXT_LENGTH],
    }
    assert forms.MAX_COUNT == tournament_config_domain_service.MAX_COUNT
