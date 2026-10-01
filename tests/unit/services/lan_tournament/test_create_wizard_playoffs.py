"""
tests.unit.services.lan_tournament.test_create_wizard_playoffs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
import json
import pathlib
import re
import shutil
import subprocess
from unittest.mock import MagicMock, patch

from flask import Flask, g
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament import tournament_service as edit_rules
from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentCreateForm,
    TournamentUpdateForm,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_create_wizard_strings,
    CREATE_WIZARD_STEP_FIELDS,
    first_error_step,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    TournamentSettings,
    validate_tournament_settings,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


_V = 'byceps.services.lan_tournament.blueprints.admin.views'

_PLAYOFF_FIELDS = (
    'playoff_enabled',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_elimination_mode',
    'playoff_release_mode',
)
_PLAYOFF_KWARGS = (
    'playoff_game_format',
    'playoff_elimination_mode',
    'playoff_group_count',
    'playoff_qualifiers_per_group',
    'playoff_qualifier_count',
    'playoff_release_mode',
)

_ROUND_ROBIN = {
    'category': 'MAIN',
    'name': 'Group stage',
    'contestant_type': 'SOLO',
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'ROUND_ROBIN',
    'min_players': '12',
    'max_players': '24',
    'playoff_enabled': 'y',
    'playoff_group_count': '3',
    'playoff_qualifiers_per_group': '2',
    'playoff_elimination_mode': 'SINGLE_ELIMINATION',
    'playoff_release_mode': 'MANUAL',
}
_HIGHSCORE = {
    'category': 'MAIN',
    'name': 'Leaderboard',
    'contestant_type': 'SOLO',
    'game_format': 'HIGHSCORE',
    'score_ordering': 'HIGHER_IS_BETTER',
    'min_players': '8',
    'max_players': '48',
    'playoff_enabled': 'y',
    'playoff_qualifier_count': '16',
    'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
    'playoff_release_mode': 'AUTOMATIC',
    'point_table': '10, 7, 5, 3',
    'group_size_min': '3',
    'group_size_max': '4',
    'advancement_count': '2',
    'points_carry_to_losers': 'y',
}


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def _create_form(data: dict) -> TournamentCreateForm:
    form = TournamentCreateForm(MultiDict(data))
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    return form


def _parse(app, data: dict):
    """Validate and parse a create POST; return the form and submission."""
    with app.test_request_context('/', method='POST'):
        g.user = MagicMock()
        form = _create_form(data)
        form.validate()
        sub = views._parse_create_submission(
            form, MagicMock(id='p1', brand_id='b1')
        )
        return form, sub


def _playoff_kwargs(settings) -> dict:
    return {name: getattr(settings, name) for name in _PLAYOFF_KWARGS}


# --------------------------------------------------------------------- #
# step table
# --------------------------------------------------------------------- #


def test_step_table_has_playoffs_step():
    steps = CREATE_WIZARD_STEP_FIELDS

    assert len(steps) == 6
    assert steps[4] == _PLAYOFF_FIELDS
    assert steps[5] == ('from_request_id', 'submission_token')
    # The scoring step keeps its fields: the JS table is pinned to it.
    assert steps[3][0] == 'score_ordering'

    tabled = {name for names in steps for name in names}
    form_fields = {
        name for name in dir(TournamentCreateForm) if name.startswith('playoff')
    }
    assert form_fields == set(_PLAYOFF_FIELDS)
    assert form_fields <= tabled


# fmt: off
@pytest.mark.parametrize(
    'field',
    [
        'point_table',
        'group_size_min',
        'group_size_max',
        'advancement_count',
        'points_carry_to_losers',
    ],
)
# fmt: on
def test_first_error_step_maps_ffa_fields_for_highscore(app, field):
    with app.test_request_context('/'):
        highscore = _create_form({'game_format': 'HIGHSCORE'})
        getattr(highscore, field).errors = ['bad']
        free_for_all = _create_form({'game_format': 'FREE_FOR_ALL'})
        getattr(free_for_all, field).errors = ['bad']

        assert first_error_step(highscore) == 4
        assert first_error_step(free_for_all) == 3


def test_first_error_step_keeps_score_ordering_on_the_scoring_step(app):
    with app.test_request_context('/'):
        form = _create_form({'game_format': 'HIGHSCORE'})
        form.score_ordering.errors = ['bad']
        form.playoff_qualifier_count.errors = ['bad']

        assert first_error_step(form) == 3


@pytest.mark.parametrize('field', _PLAYOFF_FIELDS)
def test_first_error_step_maps_playoff_fields_to_the_playoffs_step(app, field):
    with app.test_request_context('/'):
        form = _create_form({'game_format': 'ONE_V_ONE'})
        getattr(form, field).errors = ['bad']

        assert first_error_step(form) == 4


# --------------------------------------------------------------------- #
# create: parsing
# --------------------------------------------------------------------- #


def test_parse_create_submission_passes_playoff_kwargs(app):
    form, sub = _parse(app, _ROUND_ROBIN)

    assert sub is not None, form.errors
    assert _playoff_kwargs(sub.settings) == {
        'playoff_game_format': GameFormat.ONE_V_ONE,
        'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
        'playoff_group_count': 3,
        'playoff_qualifiers_per_group': 2,
        'playoff_qualifier_count': None,
        'playoff_release_mode': PlayoffReleaseMode.MANUAL,
    }


def test_highscore_playoffs_carry_the_free_for_all_fields(app):
    form, sub = _parse(app, _HIGHSCORE)

    assert sub is not None, form.errors
    settings = sub.settings
    assert _playoff_kwargs(settings) == {
        'playoff_game_format': GameFormat.FREE_FOR_ALL,
        'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
        'playoff_group_count': None,
        'playoff_qualifiers_per_group': None,
        'playoff_qualifier_count': 16,
        'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
    }
    assert settings.point_table == [10, 7, 5, 3]
    assert (settings.group_size_min, settings.group_size_max) == (3, 4)
    assert settings.advancement_count == 2
    assert settings.game_format is GameFormat.HIGHSCORE
    assert settings.elimination_mode is EliminationMode.NONE
    assert sub.points_carry_to_losers is True


def test_highscore_single_elimination_playoffs_drop_the_carry_flag(app):
    data = {**_HIGHSCORE, 'playoff_elimination_mode': 'SINGLE_ELIMINATION'}

    form, sub = _parse(app, data)

    assert sub is not None, form.errors
    assert sub.points_carry_to_losers is None


# fmt: off
@pytest.mark.parametrize(
    'data',
    [
        {**_ROUND_ROBIN, 'playoff_enabled': ''},
        {**_HIGHSCORE, 'playoff_enabled': ''},
        # A stale switch of a skipped step must not reach the service.
        {**_ROUND_ROBIN, 'elimination_mode': 'SINGLE_ELIMINATION'},
        {**_ROUND_ROBIN, 'game_format': 'FREE_FOR_ALL',
         'elimination_mode': 'SINGLE_ELIMINATION', 'point_table': '5',
         'group_size_max': '4', 'advancement_count': '1'},
    ],
    ids=['rr-off', 'highscore-off', 'plain-se', 'ffa'],
)
# fmt: on
def test_no_playoff_kwargs_without_a_playoff_phase(app, data):
    form, sub = _parse(app, data)

    assert sub is not None, form.errors
    assert set(_playoff_kwargs(sub.settings).values()) == {None}


def test_highscore_without_playoffs_ignores_the_free_for_all_fields(app):
    data = {**_HIGHSCORE, 'playoff_enabled': ''}

    form, sub = _parse(app, data)

    assert sub is not None, form.errors
    assert sub.settings.point_table is None
    assert sub.settings.group_size_max is None
    assert sub.points_carry_to_losers is None


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'field', 'message'),
    [
        ({'playoff_group_count': '1'}, 'playoff_group_count',
         'At least two groups are needed.'),
        ({'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
          'playoff_group_count': '2', 'playoff_qualifiers_per_group': '1'},
         'playoff_qualifiers_per_group',
         'Double elimination playoffs need at least 4 qualifiers in total.'),
        ({'playoff_release_mode': ''}, 'playoff_release_mode',
         'Please choose how the playoffs are released.'),
        ({'playoff_elimination_mode': ''}, 'playoff_elimination_mode',
         'Please choose a playoff elimination mode.'),
        ({'playoff_group_count': ''}, 'playoff_group_count',
         'Please enter the number of groups.'),
    ],
)
# fmt: on
def test_round_robin_playoff_rules_land_on_the_fields(
    app, overrides, field, message
):
    form, sub = _parse(app, {**_ROUND_ROBIN, **overrides})

    assert sub is None
    assert getattr(form, field).errors == [message]


def test_highscore_qualifiers_must_reach_the_minimum_lobby_size(app):
    data = {**_HIGHSCORE, 'playoff_qualifier_count': '2', 'group_size_min': '3'}

    form, sub = _parse(app, data)

    assert sub is None
    assert form.playoff_qualifier_count.errors == [
        'Qualifiers must be at least the minimum group size.'
    ]
    assert first_error_step(form) == 4


def test_highscore_free_for_all_errors_send_the_wizard_to_the_playoffs(app):
    data = {**_HIGHSCORE, 'point_table': ''}

    form, sub = _parse(app, data)

    assert sub is None
    assert form.point_table.errors
    assert first_error_step(form) == 4


@pytest.mark.parametrize('field', _PLAYOFF_FIELDS[1:4])
def test_playoff_counts_are_bounded(app, field):
    form, _ = _parse(app, {**_ROUND_ROBIN, field: '99999999999999999999'})

    ceiling = 255 if field == 'playoff_group_count' else 1024
    assert form.errors[field] == [f'At most {ceiling}.']


def test_a_playoff_count_of_hundreds_of_digits_is_a_field_error(app):
    form, sub = _parse(app, {**_ROUND_ROBIN, 'playoff_group_count': '9' * 400})

    assert sub is None
    assert form.playoff_group_count.errors


# --------------------------------------------------------------------- #
# create: view
# --------------------------------------------------------------------- #


def test_create_passes_the_playoff_kwargs_to_the_service(app):
    tournament = MagicMock(id='t1', name='Group stage', party_id='p1')
    with (
        patch(f'{_V}._get_party_or_404', return_value=MagicMock(id='p1')),
        patch(f'{_V}.tournament_service') as tournament_service,
        patch(f'{_V}.tournament_request_repository'),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.redirect_to'),
        app.test_request_context('/', method='POST', data=_ROUND_ROBIN),
    ):
        tournament_service.find_tournament_by_creation_token.return_value = None
        tournament_service.create_tournament.return_value = Ok(
            (tournament, MagicMock())
        )
        g.user = MagicMock()

        views.create.__wrapped__('p1')

    kwargs = tournament_service.create_tournament.call_args.kwargs
    assert {name: kwargs[name] for name in _PLAYOFF_KWARGS} == {
        'playoff_game_format': GameFormat.ONE_V_ONE,
        'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
        'playoff_group_count': 3,
        'playoff_qualifiers_per_group': 2,
        'playoff_qualifier_count': None,
        'playoff_release_mode': PlayoffReleaseMode.MANUAL,
    }


def test_ffa_create_without_a_cut_shows_the_field_error(app):
    data = {
        **_ROUND_ROBIN,
        'game_format': 'FREE_FOR_ALL',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'playoff_enabled': '',
        'point_table': '5',
        'group_size_max': '4',
    }
    with (
        patch(f'{_V}._get_party_or_404', return_value=MagicMock(id='p1')),
        patch(f'{_V}.tournament_service') as tournament_service,
        patch(f'{_V}.tournament_request_repository'),
        patch(f'{_V}.create_form') as create_form,
        app.test_request_context('/', method='POST', data=data),
    ):
        tournament_service.find_tournament_by_creation_token.return_value = None
        g.user = MagicMock()

        views.create.__wrapped__('p1')

    tournament_service.create_tournament.assert_not_called()
    form = create_form.call_args.args[1]
    assert form.advancement_count.errors == [
        'Please enter how many advance per lobby.'
    ]


# --------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------- #


def _tournament(status=TournamentStatus.DRAFT, **overrides) -> Tournament:
    values = dict(
        id=TournamentID(generate_uuid()),
        party_id='p1',
        name='Group stage',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=datetime.now(UTC),
        min_players=12,
        max_players=24,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=ContestantType.SOLO,
        tournament_status=status,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        score_ordering=None,
    )
    return Tournament(**{**values, **overrides})


def _playoff_tournament(**overrides) -> Tournament:
    return _tournament(
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=3,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
        **overrides,
    )


_UPDATE_BASE = {
    'category': 'MAIN',
    'name': 'Group stage',
    'contestant_type': 'SOLO',
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'ROUND_ROBIN',
    'min_players': '12',
    'max_players': '24',
}


def _gettext(msg, **params):
    return msg % params if params else msg


def _call_update(app, tournament, data, *, user=None):
    """Run the update view; return the service call and the flashes."""
    flashes = []
    with (
        patch(f'{_V}._get_tournament_or_404', return_value=tournament),
        patch(
            f'{_V}.tournament_service.update_tournament',
            return_value=Ok(tournament),
        ) as update_tournament,
        patch(f'{_V}.gettext', side_effect=_gettext),
        patch(f'{_V}.to_user_timezone', side_effect=lambda dt: dt),
        patch(f'{_V}.to_utc', side_effect=lambda dt: dt),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.flash_error', side_effect=flashes.append),
        patch(f'{_V}.update_form') as update_form,
        patch(f'{_V}.redirect_to'),
        app.test_request_context('/', method='POST', data=data),
    ):
        g.user = user or MagicMock()
        views.update.__wrapped__(str(tournament.id))
    return update_tournament, update_form, flashes


def test_update_passes_all_six_playoff_kwargs(app):
    data = {
        **_UPDATE_BASE,
        'playoff_enabled': 'y',
        'playoff_group_count': '4',
        'playoff_qualifiers_per_group': '2',
        'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
        'playoff_release_mode': 'AUTOMATIC',
    }

    update_tournament, _, flashes = _call_update(app, _tournament(), data)

    assert flashes == []
    kwargs = update_tournament.call_args.kwargs
    assert {name: kwargs[name] for name in _PLAYOFF_KWARGS} == {
        'playoff_game_format': GameFormat.ONE_V_ONE,
        'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
        'playoff_group_count': 4,
        'playoff_qualifiers_per_group': 2,
        'playoff_qualifier_count': None,
        'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
    }


def test_update_with_the_switch_off_clears_the_stored_config(app):
    update_tournament, _, _ = _call_update(
        app, _playoff_tournament(), _UPDATE_BASE
    )

    kwargs = update_tournament.call_args.kwargs
    for name in _PLAYOFF_KWARGS:
        assert name in kwargs, name
        assert kwargs[name] is None, name


def test_update_of_an_ongoing_tournament_reposts_the_switch_and_group_count_only(
    app,
):
    tournament = _playoff_tournament(status=TournamentStatus.ONGOING)

    update_tournament, _, flashes = _call_update(
        app,
        tournament,
        {
            'description': 'New text',
            'playoff_qualifiers_per_group': '3',
            'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
            'playoff_release_mode': 'AUTOMATIC',
        },
    )

    assert flashes == []
    kwargs = update_tournament.call_args.kwargs
    assert {name: kwargs[name] for name in _PLAYOFF_KWARGS} == {
        'playoff_game_format': GameFormat.ONE_V_ONE,
        'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
        'playoff_group_count': 3,
        'playoff_qualifiers_per_group': 3,
        'playoff_qualifier_count': None,
        'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
    }


def test_update_of_a_released_tournament_reposts_every_playoff_field(app):
    tournament = _playoff_tournament(
        status=TournamentStatus.COMPLETED,
        playoff_released_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    update_tournament, _, flashes = _call_update(
        app, tournament, {**_UPDATE_BASE, 'description': 'New text'}
    )

    assert flashes == []
    kwargs = update_tournament.call_args.kwargs
    assert {name: kwargs[name] for name in _PLAYOFF_KWARGS} == {
        'playoff_game_format': GameFormat.ONE_V_ONE,
        'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
        'playoff_group_count': 3,
        'playoff_qualifiers_per_group': 2,
        'playoff_qualifier_count': None,
        'playoff_release_mode': PlayoffReleaseMode.MANUAL,
    }


def _highscore_playoff_tournament(**overrides) -> Tournament:
    return _tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=views.ScoreOrdering.HIGHER_IS_BETTER,
        point_table=[10, 7, 5, 3],
        group_size_min=3,
        group_size_max=4,
        advancement_count=2,
        points_carry_to_losers=True,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        playoff_qualifier_count=16,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
        **overrides,
    )


def test_update_of_an_ongoing_highscore_takes_its_playoff_fields_from_the_form(
    app,
):
    tournament = _highscore_playoff_tournament(
        status=TournamentStatus.ONGOING
    )

    update_tournament, _, flashes = _call_update(
        app,
        tournament,
        {
            'description': 'New text',
            'playoff_qualifier_count': '8',
            'playoff_elimination_mode': 'SINGLE_ELIMINATION',
            'playoff_release_mode': 'AUTOMATIC',
            'point_table': '6, 4, 2',
            'group_size_min': '2',
            'group_size_max': '4',
            'advancement_count': '1',
        },
    )

    assert flashes == []
    kwargs = update_tournament.call_args.kwargs
    assert kwargs['playoff_qualifier_count'] == 8
    assert kwargs['playoff_elimination_mode'] == (
        EliminationMode.SINGLE_ELIMINATION
    )
    assert kwargs['point_table'] == [6, 4, 2]
    assert kwargs['group_size_min'] == 2
    assert kwargs['points_carry_to_losers'] is None


def test_update_of_a_released_highscore_keeps_the_stored_config(app):
    tournament = _highscore_playoff_tournament(
        status=TournamentStatus.ONGOING,
        playoff_released_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    update_tournament, _, flashes = _call_update(
        app, tournament, {'description': 'New text'}
    )

    assert flashes == []
    kwargs = update_tournament.call_args.kwargs
    assert kwargs['playoff_qualifier_count'] == 16
    assert kwargs['playoff_elimination_mode'] == (
        EliminationMode.DOUBLE_ELIMINATION
    )
    assert kwargs['point_table'] == [10, 7, 5, 3]
    assert kwargs['points_carry_to_losers'] is True


def test_update_passes_the_initiator_to_the_service(app):
    user = MagicMock()

    update_tournament, _, _ = _call_update(
        app, _playoff_tournament(), _UPDATE_BASE, user=user
    )

    assert update_tournament.call_args.kwargs['initiator_id'] == user.id


def test_update_reports_a_playoff_rule_translated_on_the_field(app):
    data = {
        **_UPDATE_BASE,
        'playoff_enabled': 'y',
        'playoff_group_count': '2',
        'playoff_qualifiers_per_group': '1',
        'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
        'playoff_release_mode': 'MANUAL',
    }

    update_tournament, update_form, flashes = _call_update(
        app, _tournament(), data
    )

    update_tournament.assert_not_called()
    assert flashes == [
        'Double elimination playoffs need at least 4 qualifiers in total.'
    ]
    form = update_form.call_args.args[1]
    assert isinstance(form, TournamentUpdateForm)
    assert form.playoff_qualifiers_per_group.errors == flashes


def test_update_highscore_playoff_errors_are_translated_with_their_params(app):
    data = {
        'category': 'MAIN',
        'name': 'Leaderboard',
        'contestant_type': 'SOLO',
        'game_format': 'HIGHSCORE',
        'score_ordering': 'HIGHER_IS_BETTER',
        'max_players': '2',
        'playoff_enabled': 'y',
        'playoff_qualifier_count': '4',
        'playoff_elimination_mode': 'SINGLE_ELIMINATION',
        'playoff_release_mode': 'MANUAL',
        'point_table': '10, 5',
        'group_size_min': '3',
        'group_size_max': '4',
    }

    update_tournament, update_form, flashes = _call_update(
        app, _tournament(), data
    )

    update_tournament.assert_not_called()
    assert flashes == [
        (
            'With at most 2 players no group of at least 3 can form. '
            'Lower the minimum or raise "Max. players" in step 3.'
        )
    ]


# --------------------------------------------------------------------- #
# update form
# --------------------------------------------------------------------- #


def _update_env():
    from jinja2 import DictLoader, Environment, StrictUndefined
    from markupsafe import Markup

    from tests.unit.services.lan_tournament import (
        test_create_wizard_render as render,
    )

    root = pathlib.Path(
        'byceps/services/lan_tournament/blueprints/admin/templates'
        '/admin/lan_tournament'
    )
    env = Environment(
        undefined=StrictUndefined,
        autoescape=True,
        loader=DictLoader(
            {
                'update_form.html': (root / 'update_form.html').read_text(),
                'admin/lan_tournament/_constraint_toggle.html': (
                    root / '_constraint_toggle.html'
                ).read_text(),
                'layout/admin/lan_tournament.html': render._LAYOUT_STUB,
                'macros/admin.html': render._ADMIN_MACROS_STUB,
                'macros/forms.html': render._FORMS_MACRO_SRC,
                'macros/icons.html': render._ICONS_STUB,
            }
        ),
    )
    env.globals['_'] = lambda s, **kw: (Markup(s) % kw) if kw else s  # noqa: S704
    env.globals['url_for'] = render._url_for
    return env


def _render_update_form(app, tournament, *, is_locked=False):
    locked = edit_rules.locked_playoff_fields(tournament)
    governed = edit_rules.playoff_fields(tournament)
    with app.test_request_context('/'):
        data = {
            'name': tournament.name,
            'contestant_type': 'SOLO',
            'game_format': tournament.game_format.name,
            'elimination_mode': tournament.elimination_mode.name,
            'score_ordering': '',
            'point_table': '',
            'playoff_enabled': tournament.has_playoffs,
            'playoff_elimination_mode': (
                tournament.playoff_elimination_mode.name
                if tournament.playoff_elimination_mode
                else ''
            ),
            'playoff_release_mode': (
                tournament.playoff_release_mode.name
                if tournament.playoff_release_mode
                else ''
            ),
            'playoff_group_count': tournament.playoff_group_count,
            'playoff_qualifiers_per_group': (
                tournament.playoff_qualifiers_per_group
            ),
            'playoff_qualifier_count': tournament.playoff_qualifier_count,
        }
        form = TournamentUpdateForm(data=data)
        form.set_contestant_type_choices()
        form.set_game_format_choices()
        form.set_elimination_mode_choices()
        form.set_score_ordering_choices()
        return _update_env().get_template('update_form.html').render(
            party=MagicMock(),
            tournament=tournament,
            form=form,
            is_locked=is_locked,
            locked_playoff_fields=locked,
            ffa_locked=(
                'point_table' in locked
                if 'point_table' in governed
                else is_locked
            ),
        )


def _tag(out: str, name: str) -> str:
    """Return the input or select tag of a field, padded with spaces."""
    match = re.search(rf'<(?:input|select)[^>]*name="{name}"[^>]*>', out)
    assert match is not None, name
    return match.group(0).replace('<', '< ').replace('>', ' >')


def test_update_form_renders_the_playoff_section_with_stored_values(app):
    out = _render_update_form(app, _playoff_tournament())

    assert 'id="playoff-constraints"' in out
    assert ' checked ' in _tag(out, 'playoff_enabled')
    assert 'value="3"' in _tag(out, 'playoff_group_count')
    assert 'value="2"' in _tag(out, 'playoff_qualifiers_per_group')
    assert '<option selected value="SINGLE_ELIMINATION">' in out
    assert '<option selected value="MANUAL">' in out
    assert 'Editable until the release.' in out
    assert '%(' not in out


def test_update_form_locks_the_switch_and_group_count_once_started(app):
    out = _render_update_form(
        app,
        _playoff_tournament(status=TournamentStatus.ONGOING),
        is_locked=True,
    )

    for name in ('playoff_enabled', 'playoff_group_count'):
        assert 'disabled="disabled"' in _tag(out, name), name
    for name in (
        'playoff_qualifiers_per_group',
        'playoff_qualifier_count',
        'playoff_elimination_mode',
        'playoff_release_mode',
    ):
        assert 'disabled' not in _tag(out, name), name
    assert 'The playoff settings stay editable until the release.' in out


def test_update_form_locks_every_playoff_field_after_the_release(app):
    out = _render_update_form(
        app,
        _playoff_tournament(
            status=TournamentStatus.COMPLETED,
            playoff_released_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )

    for name in (
        'playoff_enabled',
        'playoff_group_count',
        'playoff_qualifiers_per_group',
        'playoff_qualifier_count',
        'playoff_elimination_mode',
        'playoff_release_mode',
    ):
        assert 'disabled="disabled"' in _tag(out, name), name
    assert 'Locked while the playoffs are released.' in out


# fmt: off
@pytest.mark.parametrize(
    ('status', 'released', 'locked'),
    [
        (TournamentStatus.ONGOING, False, False),
        (TournamentStatus.PAUSED, False, False),
        (TournamentStatus.ONGOING, True, True),
        (TournamentStatus.COMPLETED, True, True),
    ],
)
# fmt: on
def test_update_form_offers_the_highscore_phase_two_fields_until_the_release(
    app, status, released, locked
):
    tournament = _highscore_playoff_tournament(
        status=status,
        playoff_released_at=datetime(2026, 9, 1, tzinfo=UTC) if released else None,
    )

    out = _render_update_form(
        app,
        tournament,
        is_locked=status in (TournamentStatus.ONGOING, TournamentStatus.PAUSED),
    )

    for name in (
        'point_table',
        'group_size_min',
        'group_size_max',
        'advancement_count',
        'points_carry_to_losers',
    ):
        assert ('disabled' in _tag(out, name)) is locked, name
    assert ('data-locked' in out) is locked


def test_update_form_locks_the_free_for_all_fields_of_an_ongoing_tournament(
    app,
):
    tournament = _tournament(
        status=TournamentStatus.ONGOING,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=[3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
    )

    out = _render_update_form(app, tournament, is_locked=True)

    for name in ('group_size_min', 'group_size_max', 'advancement_count'):
        assert 'disabled' in _tag(out, name), name
    assert 'data-locked' in out


def test_update_form_warns_before_saving_a_format_without_playoffs(app):
    out = _render_update_form(app, _playoff_tournament())

    assert 'id="playoff-removal-note"' in out
    assert (
        'This format has no playoff phase. The playoff settings are removed '
        'when you save.'
    ) in out
    assert '!isRr && !isHs && !!(playoffEnabled' in out


def test_update_form_script_shows_highscore_playoffs_in_the_ffa_block(app):
    out = _render_update_form(app, _playoff_tournament())

    assert 'isHs && playoffsOn' in out
    assert 'isRr && playoffsOn' in out
    assert "gf === 'ONE_V_ONE' && em === 'ROUND_ROBIN'" in out


# --------------------------------------------------------------------- #
# the rules module twins the server
# --------------------------------------------------------------------- #

_RULES_JS = pathlib.Path('byceps/static/behavior/lan_tournament_create_wizard_rules.js')
_NODE = shutil.which('node')
_SERVED_FIELDS = (*_PLAYOFF_FIELDS, 'point_table', 'group_size_min', 'group_size_max', 'advancement_count')

_SOLO = {'category': 'MAIN', 'contestant_type': 'SOLO', 'min_players': '12', 'max_players': '24'}


def _js(expression: str, payload) -> object:
    script = (
        f'const r = require({str(_RULES_JS.resolve())!r});'
        'const input = JSON.parse(process.argv[1]);'
        f'console.log(JSON.stringify({expression}));'
    )
    result = subprocess.run(  # noqa: S603 -- fixed local node binary
        [_NODE, '-e', script, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def _settings(values: dict) -> TournamentSettings:
    def number(name):
        raw = values.get(name)
        return int(raw) if raw not in (None, '') else None

    def enum(enum_class, name):
        return enum_class[values[name]] if values.get(name) else None

    game_format = enum(GameFormat, 'game_format')
    mode = enum(EliminationMode, 'elimination_mode')
    rr = game_format is GameFormat.ONE_V_ONE and mode is (
        EliminationMode.ROUND_ROBIN
    )
    hs = game_format is GameFormat.HIGHSCORE
    on = bool(values.get('playoff_enabled')) and (rr or hs)
    ffa = game_format is GameFormat.FREE_FOR_ALL or (on and hs)
    table = values.get('point_table')
    return TournamentSettings(
        contestant_type=enum(ContestantType, 'contestant_type'),
        game_format=game_format,
        elimination_mode=mode,
        score_ordering=views.ScoreOrdering.HIGHER_IS_BETTER if hs else None,
        min_players=number('min_players'),
        max_players=number('max_players'),
        min_teams=number('min_teams'),
        max_teams=number('max_teams'),
        min_players_in_team=None,
        max_players_in_team=None,
        point_table=(
            [int(x) for x in table.split(',') if x.strip()]
            if ffa and table
            else None
        ),
        group_size_min=number('group_size_min') if ffa else None,
        group_size_max=number('group_size_max') if ffa else None,
        advancement_count=number('advancement_count') if ffa else None,
        playoff_game_format=(
            (GameFormat.ONE_V_ONE if rr else GameFormat.FREE_FOR_ALL)
            if on
            else None
        ),
        playoff_elimination_mode=(
            enum(EliminationMode, 'playoff_elimination_mode') if on else None
        ),
        playoff_group_count=number('playoff_group_count') if on and rr else None,
        playoff_qualifiers_per_group=(
            number('playoff_qualifiers_per_group') if on and rr else None
        ),
        playoff_qualifier_count=(
            number('playoff_qualifier_count') if on and hs else None
        ),
        playoff_release_mode=(
            enum(PlayoffReleaseMode, 'playoff_release_mode') if on else None
        ),
    )


def _python_msgids(values: dict) -> dict[str, str]:
    match validate_tournament_settings(_settings(values), require_structure=True):
        case Err(errors):
            return {
                field: message.msgid
                for field, message in errors.items()
                if field in _SERVED_FIELDS
            }
    return {}


def _js_msgids(values: dict) -> dict[str, str]:
    js_values = {
        **values,
        'name': 'Cup',
        'playoff_enabled': bool(values.get('playoff_enabled')),
    }
    errors = _js(
        "r.validate(input, {limits: {countMax: 1024, pointTableMax: 64},"
        " validCombinations: {ONE_V_ONE: ['DOUBLE_ELIMINATION','ROUND_ROBIN',"
        "'SINGLE_ELIMINATION'], FREE_FOR_ALL: ['DOUBLE_ELIMINATION',"
        "'SINGLE_ELIMINATION'], HIGHSCORE: ['NONE']}, requireStructure: true}).errors",
        js_values,
    )
    return {
        field: message['msgid']
        for field, message in errors.items()
        if field in _SERVED_FIELDS
    }


_RR = {
    **_SOLO,
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'ROUND_ROBIN',
    'playoff_enabled': True,
    'playoff_group_count': '3',
    'playoff_qualifiers_per_group': '2',
    'playoff_elimination_mode': 'SINGLE_ELIMINATION',
    'playoff_release_mode': 'MANUAL',
}
_HS = {
    **_SOLO,
    'game_format': 'HIGHSCORE',
    'elimination_mode': 'NONE',
    'score_ordering': 'HIGHER_IS_BETTER',
    'max_players': '48',
    'playoff_enabled': True,
    'playoff_qualifier_count': '16',
    'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
    'playoff_release_mode': 'AUTOMATIC',
    'point_table': '10, 7, 5, 3',
    'group_size_min': '3',
    'group_size_max': '4',
    'advancement_count': '2',
}

# fmt: off
_PARITY_CASES = {
    'rr valid': _RR,
    'rr off': {**_RR, 'playoff_enabled': False, 'playoff_group_count': '1'},
    'rr one group': {**_RR, 'playoff_group_count': '1'},
    'rr no groups': {**_RR, 'playoff_group_count': ''},
    'rr no qualifiers': {**_RR, 'playoff_qualifiers_per_group': ''},
    'rr no mode': {**_RR, 'playoff_elimination_mode': None},
    'rr no release': {**_RR, 'playoff_release_mode': None},
    'rr minimum too small': {**_RR, 'min_players': '5'},
    'rr qualifiers reach the group': {**_RR, 'playoff_qualifiers_per_group': '4'},
    'rr double 2x1': {**_RR, 'min_players': '', 'playoff_group_count': '2',
                      'playoff_qualifiers_per_group': '1',
                      'playoff_elimination_mode': 'DOUBLE_ELIMINATION'},
    'rr double 2x2': {**_RR, 'min_players': '', 'playoff_group_count': '2',
                      'playoff_elimination_mode': 'DOUBLE_ELIMINATION'},
    'rr team minimum': {**_RR, 'contestant_type': 'TEAM', 'min_teams': '6',
                        'min_players': '', 'max_players': '',
                        'playoff_qualifiers_per_group': '2'},
    'rr stale on single knockout': {**_RR, 'elimination_mode': 'SINGLE_ELIMINATION',
                                    'playoff_group_count': '1'},
    'hs valid': _HS,
    'hs off': {**_HS, 'playoff_enabled': False, 'point_table': ''},
    'hs no qualifiers': {**_HS, 'playoff_qualifier_count': ''},
    'hs one qualifier': {**_HS, 'playoff_qualifier_count': '1', 'group_size_min': ''},
    'hs below the lobby minimum': {**_HS, 'playoff_qualifier_count': '2'},
    'hs qualifiers do not split': {**_HS, 'playoff_qualifier_count': '5'},
    'hs no points': {**_HS, 'point_table': ''},
    'hs no lobby maximum': {**_HS, 'group_size_max': ''},
    'hs lobby minimum above maximum': {**_HS, 'group_size_min': '5', 'group_size_max': '4'},
    'hs advancing too many': {**_HS, 'advancement_count': '4'},
    'hs two lobbies without cut': {**_HS, 'playoff_elimination_mode': 'SINGLE_ELIMINATION',
                                   'playoff_qualifier_count': '8', 'advancement_count': ''},
    'hs one lobby without cut': {**_HS, 'playoff_elimination_mode': 'SINGLE_ELIMINATION',
                                 'playoff_qualifier_count': '4', 'advancement_count': ''},
    'hs no mode': {**_HS, 'playoff_elimination_mode': None},
    'hs no release': {**_HS, 'playoff_release_mode': None},
}
# fmt: on


@pytest.mark.skipif(_NODE is None, reason='Node.js is not installed')
@pytest.mark.parametrize('case', sorted(_PARITY_CASES))
def test_js_playoff_rules_match_the_server_rules(case):
    values = _PARITY_CASES[case]

    assert _js_msgids(values) == _python_msgids(values)


@pytest.mark.skipif(_NODE is None, reason='Node.js is not installed')
def test_the_parity_table_reaches_every_playoff_rule():
    seen = set()
    for values in _PARITY_CASES.values():
        seen |= set(_python_msgids(values).values())

    assert {
        'Please choose a playoff elimination mode.',
        'Please choose how the playoffs are released.',
        'Please enter the number of groups.',
        'At least two groups are needed.',
        'Please enter how many advance from each group.',
        'The minimum number of contestants is too small for this many groups.',
        'Fewer must advance from each group than the smallest group holds.',
        'Double elimination playoffs need at least 4 qualifiers in total.',
        'Please enter the number of qualifiers.',
        'Please enter how many advance per lobby.',
        'At least two qualifiers are needed.',
        'Qualifiers must be at least the minimum group size.',
        (
            'The qualifiers cannot be split into lobbies between the minimum '
            'and maximum group size.'
        ),
    } <= seen


# Every msgid the wizard can show for the Playoffs step, including the ones
# the static scan of `t('...')` literals cannot see (ternaries, label maps).
_DYNAMIC_PLAYOFF_MSGIDS = (
    'Pools',
    'Playoff mode',
    'Min. group size',
    'Min. lobby size',
    'Max. group size',
    'Max. lobby size',
    'Advancing per group',
    'Advancing per lobby',
    'Group size',
    'Lobby size',
    'automatic',
    'manual',
)


@pytest.mark.skipif(_NODE is None, reason='Node.js is not installed')
def test_every_playoff_msgid_the_rules_emit_is_served_with_german(app):
    emitted = set(_DYNAMIC_PLAYOFF_MSGIDS)
    cases = [
        {**_RR, **over}
        for over in (
            {},
            {'playoff_qualifiers_per_group': '1'},
            {'playoff_enabled': False},
            {'playoff_group_count': ''},
            {'max_players': '16', 'playoff_group_count': '4'},
            {
                'max_players': '14',
                'playoff_group_count': '7',
                'playoff_qualifiers_per_group': '1',
            },
            {'max_players': '13'},
            {'max_players': ''},
            {'contestant_type': 'TEAM', 'max_teams': '12'},
            {'elimination_mode': 'SINGLE_ELIMINATION'},
        )
    ] + [
        {**_HS, **over}
        for over in (
            {},
            {'playoff_qualifier_count': '10'},
            {'group_size_max': '', 'advancement_count': ''},
            {'max_players': ''},
            {'contestant_type': 'TEAM', 'max_teams': '48'},
            {'playoff_qualifier_count': 'x'},
        )
    ]
    for values in cases:
        emitted |= set(
            _js(
                '[].concat(r.playoffPreview(input) || [], r.playoffWhat(input),'
                ' r.stepSummary(4, input)).map((p) => p.msgid)',
                values,
            )
        )
    for field in ('game_format', 'elimination_mode'):
        emitted |= set(
            _js(
                f"r.dependentChange(input, {field!r}, 'FREE_FOR_ALL',"
                " {validCombinations: {FREE_FOR_ALL: ['SINGLE_ELIMINATION']}})"
                '.notices.map((n) => n.msgid)',
                _RR,
            )
        )
    emitted |= set(
        _js(
            "r.dependentChange(input, 'elimination_mode', 'SINGLE_ELIMINATION',"
            ' {validCombinations: {}}).notices.map((n) => n.msgid)',
            _RR,
        )
    )
    emitted |= {
        'Playoffs: %(what)s → %(mode)s, release %(release)s.',
        'Summary',
        'No playoffs. One phase, as before.',
        'Preview',
        (
            'Playoffs exist only for 1v1 with "Everyone plays everyone" and '
            'for Highscore. This tournament (%(what)s) has one phase and '
            'behaves as before.'
        ),
    }

    with app.app_context(), app.test_request_context('/'):
        served = build_create_wizard_strings()
        assert emitted <= set(served), sorted(emitted - set(served))

    from babel.messages.pofile import read_po

    with pathlib.Path('byceps/translations/de/LC_MESSAGES/messages.po').open(
        'rb'
    ) as f:
        catalog = read_po(f)
    untranslated = [
        msgid
        for msgid in sorted(emitted)
        if not (message := catalog.get(msgid)) or not message.string
    ]
    assert untranslated == []
    placeholder = re.compile(r'%\((\w+)\)[sd]')
    for msgid in emitted:
        assert set(placeholder.findall(msgid)) == set(
            placeholder.findall(catalog.get(msgid).string)
        ), msgid


# --------------------------------------------------------------------- #
# wizard markup and script agree on the step count
# --------------------------------------------------------------------- #

_WIZARD_JS = pathlib.Path(
    'byceps/static/behavior/lan_tournament_create_wizard.js'
)
_CREATE_FORM = pathlib.Path(
    'byceps/services/lan_tournament/blueprints/admin/templates'
    '/admin/lan_tournament/create_form.html'
)


def test_wizard_script_counts_the_steps_of_the_step_table():
    source = _WIZARD_JS.read_text(encoding='utf-8')
    count = len(CREATE_WIZARD_STEP_FIELDS)

    assert re.search(rf'\bvar STEP_COUNT = {count};', source)
    assert re.search(rf'\bvar REVIEW_STEP = {count - 1};', source)
    assert re.search(rf'\bvar PLAYOFF_STEP = {count - 2};', source)
    titles = re.search(r'var STEP_TITLES = \[(.*?)\];', source, re.DOTALL)
    assert titles is not None
    assert len(re.findall(r"'[^']+'", titles.group(1))) == count
    assert "'Playoffs', 'Review and create'" in titles.group(1).replace(
        '\n', ' '
    ).replace('  ', ' ')


def test_create_form_has_one_step_element_per_step():
    source = _CREATE_FORM.read_text(encoding='utf-8')
    steps = re.findall(r'data-wiz-step="(\d+)"', source)

    assert steps == [str(i) for i in range(len(CREATE_WIZARD_STEP_FIELDS))]
    assert source.count('<fieldset class="lt-wiz-step box" data-wiz-step=') == 5
    assert '<section class="lt-wiz-step box" data-wiz-step="5">' in source


def test_create_form_keeps_the_slice_anchors_of_the_render_tests():
    source = _CREATE_FORM.read_text(encoding='utf-8')

    assert source.count('{# Provenance banner') == 1
    assert source.count('{# /Provenance banner #}') == 1
    assert source.count('class="lt-wiz-backlink"') == 2


def test_playoff_pieces_have_a_home_and_a_moved_slot():
    source = _CREATE_FORM.read_text(encoding='utf-8')
    pieces = re.findall(r'data-wiz-piece="([a-z-]+)"', source) + re.findall(
        r"piece='([a-z-]+)'", source
    )
    slots = set(re.findall(r'data-wiz-slot="([a-z-]+)"', source))

    assert sorted(pieces) == ['ffa-carry', 'ffa-points', 'playoff-mode']
    for piece in pieces:
        assert piece in slots
        assert f'{piece}-moved' in slots
