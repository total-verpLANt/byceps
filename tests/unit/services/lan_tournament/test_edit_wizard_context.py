import dataclasses
from datetime import datetime, UTC
import json
from pathlib import Path
from uuid import uuid4

from babel.messages.pofile import read_po
from flask import Flask
from flask_babel import Babel, to_user_timezone
import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)
from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentCreateForm,
    TournamentUpdateForm,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_create_wizard_context,
    build_create_wizard_strings,
    edit_locked_fields,
    tournament_form_data,
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


_PO = (
    Path(__file__).resolve().parents[4]
    / 'byceps/translations/de/LC_MESSAGES/messages.po'
)

_URLS = {'update': '/update', 'validate': '/update/validate'}

_STATUS_LOCKED = frozenset({
    'name', 'game', 'category', 'start_time', 'contestant_type',
    'game_format', 'elimination_mode', 'score_ordering', 'min_players',
    'max_players', 'min_teams', 'max_teams', 'min_players_in_team',
    'max_players_in_team',
})  # fmt: skip
_FFA_LOCKED = frozenset({
    'point_table', 'group_size_min', 'group_size_max', 'advancement_count',
    'points_carry_to_losers',
})  # fmt: skip
_STARTED_PLAYOFF_LOCKED = frozenset({'playoff_enabled', 'playoff_group_count'})
_ALL_PLAYOFF_LOCKED = frozenset({
    'playoff_enabled', 'playoff_elimination_mode', 'playoff_group_count',
    'playoff_qualifiers_per_group', 'playoff_qualifier_count',
    'playoff_release_mode',
})  # fmt: skip

_CREATE_CONFIG_KEYS = {
    'sessionKey',
    'capacity',
    'timezoneLabel',
    'timezoneName',
    'timezoneDetail',
    'locale',
    'limits',
    'validCombinations',
    'urls',
    'request',
    'refusal',
    'sourceValues',
    'stagedImage',
    'firstErrorStep',
    'serverErrors',
    'strings',
}
_CREATE_CONTEXT_KEYS = {
    'config',
    'timezone_label',
    'timezone_name',
    'timezone_detail',
    'capacity',
    'party_dates',
    'staged_image',
    'first_error_step',
    'server_banner',
    'refusal',
    'source_values',
}


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['TIMEZONE'] = 'Europe/Berlin'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'Europe/Berlin'
    Babel(a)
    return a


@pytest.fixture
def ctx(app):
    with app.app_context(), app.test_request_context('/'):
        yield


class _Party:
    id = 'p1'
    starts_at = datetime(2026, 10, 1, 12, 0)
    ends_at = datetime(2026, 10, 4, 12, 0)
    max_ticket_quantity = 120


def _tournament(**overrides) -> Tournament:
    values = {
        'id': TournamentID(uuid4()),
        'party_id': 'p1',
        'name': 'Rocket League Cup',
        'game': 'Rocket League',
        'description': 'Fast cars.',
        'image_url': None,
        'ruleset': 'Best of 3.',
        'start_time': datetime(2026, 10, 3, 12, 0),
        'created_at': datetime(2026, 9, 1, 10, 0),
        'min_players': 2,
        'max_players': 16,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.DRAFT,
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    return Tournament(**(values | overrides))


def _legacy_prefill(tournament: Tournament) -> dict:
    """Return the prefill as `update_form` built it before the move."""
    start_time_local = (
        to_user_timezone(tournament.start_time)
        if tournament.start_time
        else None
    )

    data = dataclasses.asdict(tournament)
    data['category'] = tournament.category.value
    data['start_time'] = start_time_local
    data['contestant_type'] = (
        tournament.contestant_type.name if tournament.contestant_type else ''
    )
    data['game_format'] = (
        tournament.game_format.name if tournament.game_format else ''
    )
    data['elimination_mode'] = (
        tournament.elimination_mode.name if tournament.elimination_mode else ''
    )
    data['score_ordering'] = (
        tournament.score_ordering.name if tournament.score_ordering else ''
    )
    data['point_table'] = (
        ', '.join(str(v) for v in tournament.point_table)
        if tournament.point_table is not None
        else ''
    )
    data['points_carry_to_losers'] = tournament.points_carry_to_losers or False
    data['playoff_enabled'] = tournament.has_playoffs
    data['playoff_elimination_mode'] = (
        tournament.playoff_elimination_mode.name
        if tournament.playoff_elimination_mode
        else ''
    )
    data['playoff_release_mode'] = (
        tournament.playoff_release_mode.name
        if tournament.playoff_release_mode
        else ''
    )
    return data


_IMAGE_KEYS = {'image_id', 'image_url', 'image_alt_text'}
_IMAGE_ID = uuid4()
_SERVED_PATH = '/data/parties/p1/lan_tournament/images/x.png'


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        pytest.param(
            {},
            {
                'category': 'MAIN',
                'contestant_type': 'SOLO',
                'game_format': 'ONE_V_ONE',
                'elimination_mode': 'SINGLE_ELIMINATION',
                'score_ordering': '',
                'point_table': '',
                'points_carry_to_losers': False,
                'playoff_enabled': False,
                'playoff_elimination_mode': '',
                'playoff_release_mode': '',
                'image_id': '',
                'image_url': None,
                'image_alt_text': '',
            },
            id='solo-single-elimination',
        ),
        pytest.param(
            {
                'category': TournamentCategory.USER_ORGANIZED,
                'contestant_type': ContestantType.TEAM,
                'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
            },
            {
                'category': 'USER_ORGANIZED',
                'contestant_type': 'TEAM',
                'elimination_mode': 'DOUBLE_ELIMINATION',
            },
            id='team-double-elimination-user-organized',
        ),
        pytest.param(
            {
                'contestant_type': None,
                'game_format': None,
                'elimination_mode': None,
                'start_time': None,
            },
            {
                'contestant_type': '',
                'game_format': '',
                'elimination_mode': '',
                'start_time': None,
            },
            id='legacy-row-without-enums-or-start',
        ),
        pytest.param(
            {
                'game_format': GameFormat.FREE_FOR_ALL,
                'point_table': [10, 7, 5, 3],
                'points_carry_to_losers': True,
                'group_size_min': 3,
                'group_size_max': 4,
                'advancement_count': 2,
            },
            {
                'game_format': 'FREE_FOR_ALL',
                'point_table': '10, 7, 5, 3',
                'points_carry_to_losers': True,
                'group_size_min': 3,
                'group_size_max': 4,
                'advancement_count': 2,
            },
            id='free-for-all-point-table',
        ),
        pytest.param(
            {
                'game_format': GameFormat.HIGHSCORE,
                'elimination_mode': EliminationMode.NONE,
                'score_ordering': ScoreOrdering.LOWER_IS_BETTER,
                'point_table': [],
            },
            {
                'game_format': 'HIGHSCORE',
                'elimination_mode': 'NONE',
                'score_ordering': 'LOWER_IS_BETTER',
                'point_table': '',
            },
            id='highscore-empty-point-table',
        ),
        pytest.param(
            {
                'playoff_game_format': GameFormat.ONE_V_ONE,
                'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'playoff_group_count': 2,
                'playoff_qualifiers_per_group': 2,
                'playoff_release_mode': PlayoffReleaseMode.MANUAL,
            },
            {
                'playoff_enabled': True,
                'playoff_elimination_mode': 'DOUBLE_ELIMINATION',
                'playoff_group_count': 2,
                'playoff_qualifiers_per_group': 2,
                'playoff_release_mode': 'MANUAL',
            },
            id='playoffs',
        ),
        pytest.param(
            {
                'image_id': _IMAGE_ID,
                'image_url': _SERVED_PATH,
                'image_alt_text': 'The arena',
            },
            {
                'image_id': str(_IMAGE_ID),
                'image_url': '',
                'image_alt_text': 'The arena',
            },
            id='uploaded-image-hides-its-served-path',
        ),
        pytest.param(
            {
                'image_id': None,
                'image_url': 'https://example.org/a.png',
                'image_alt_text': None,
            },
            {
                'image_id': '',
                'image_url': 'https://example.org/a.png',
                'image_alt_text': '',
            },
            id='external-image-url',
        ),
    ],
)
# fmt: on
def test_tournament_form_data_matches_the_old_prefill(ctx, overrides, expected):
    tournament = _tournament(**overrides)

    data = tournament_form_data(tournament)
    legacy = _legacy_prefill(tournament)

    assert data.keys() == legacy.keys()
    assert {k: v for k, v in data.items() if k not in _IMAGE_KEYS} == {
        k: v for k, v in legacy.items() if k not in _IMAGE_KEYS
    }
    assert {name: data[name] for name in expected} == expected


def test_tournament_form_data_shows_the_start_time_in_the_user_timezone(ctx):
    data = tournament_form_data(
        _tournament(start_time=datetime(2026, 10, 3, 12, 0, tzinfo=UTC))
    )

    assert data['start_time'].replace(tzinfo=None) == datetime(
        2026, 10, 3, 14, 0
    )


def test_tournament_form_data_fills_the_update_form(ctx):
    tournament = _tournament(
        image_id=_IMAGE_ID, image_url=_SERVED_PATH, image_alt_text='Arena'
    )

    form = TournamentUpdateForm(data=tournament_form_data(tournament))

    assert form.name.data == 'Rocket League Cup'
    assert form.image_id.data == str(_IMAGE_ID)
    assert form.image_url.data == ''
    assert form.image_alt_text.data == 'Arena'


def test_update_form_has_the_image_fields():
    assert all(
        hasattr(TournamentUpdateForm, name)
        for name in ('image', 'image_id', 'image_alt_text')
    )
    assert not hasattr(TournamentUpdateForm, 'validate_image_url')
    assert hasattr(TournamentCreateForm, 'validate_image_url')


_REVIEW_PLAYOFFS = {
    'playoff_game_format': GameFormat.ONE_V_ONE,
    'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    'playoff_group_count': 2,
    'playoff_qualifiers_per_group': 2,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}
_HIGHSCORE = {
    'game_format': GameFormat.HIGHSCORE,
    'elimination_mode': EliminationMode.NONE,
    'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
    'point_table': [10, 5],
}
_RELEASED_AT = datetime(2026, 10, 3, 18, 0)

# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        pytest.param(
            {'tournament_status': TournamentStatus.DRAFT},
            frozenset(),
            id='draft',
        ),
        pytest.param(
            {'tournament_status': TournamentStatus.REGISTRATION_OPEN},
            frozenset(),
            id='registration-open',
        ),
        pytest.param(
            {'tournament_status': TournamentStatus.CANCELLED},
            frozenset(),
            id='cancelled',
        ),
        pytest.param(
            {'tournament_status': TournamentStatus.ONGOING},
            _STATUS_LOCKED | _FFA_LOCKED | _STARTED_PLAYOFF_LOCKED,
            id='ongoing',
        ),
        pytest.param(
            {'tournament_status': TournamentStatus.PAUSED},
            _STATUS_LOCKED | _FFA_LOCKED | _STARTED_PLAYOFF_LOCKED,
            id='paused',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.COMPLETED,
                **_REVIEW_PLAYOFFS,
            },
            _STARTED_PLAYOFF_LOCKED,
            id='completed-before-the-release',
        ),
        pytest.param(
            {'tournament_status': TournamentStatus.COMPLETED},
            _STARTED_PLAYOFF_LOCKED,
            id='completed-without-playoffs',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.ONGOING,
                **_REVIEW_PLAYOFFS,
                'playoff_released_at': _RELEASED_AT,
            },
            _STATUS_LOCKED | _FFA_LOCKED | _ALL_PLAYOFF_LOCKED,
            id='ongoing-released',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.COMPLETED,
                **_REVIEW_PLAYOFFS,
                'playoff_released_at': _RELEASED_AT,
            },
            _ALL_PLAYOFF_LOCKED,
            id='completed-released',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.ONGOING,
                **_HIGHSCORE,
            },
            _STATUS_LOCKED | _FFA_LOCKED | _STARTED_PLAYOFF_LOCKED,
            id='highscore-without-playoffs-ongoing',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.ONGOING,
                **_HIGHSCORE,
                **_REVIEW_PLAYOFFS,
            },
            _STATUS_LOCKED | _STARTED_PLAYOFF_LOCKED,
            id='highscore-playoffs-ongoing-ffa-fields-follow-the-playoff-lock',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.COMPLETED,
                **_HIGHSCORE,
                **_REVIEW_PLAYOFFS,
            },
            _STARTED_PLAYOFF_LOCKED,
            id='highscore-playoffs-completed-before-the-release',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.ONGOING,
                **_HIGHSCORE,
                **_REVIEW_PLAYOFFS,
                'playoff_released_at': _RELEASED_AT,
            },
            _STATUS_LOCKED | _FFA_LOCKED | _ALL_PLAYOFF_LOCKED,
            id='highscore-playoffs-released',
        ),
        pytest.param(
            {
                'tournament_status': TournamentStatus.REGISTRATION_OPEN,
                **_HIGHSCORE,
                **_REVIEW_PLAYOFFS,
                'playoff_released_at': _RELEASED_AT,
            },
            _FFA_LOCKED | _ALL_PLAYOFF_LOCKED,
            id='highscore-playoffs-released-in-any-status',
        ),
    ],
)
# fmt: on
def test_edit_locked_fields_by_status_and_release(overrides, expected):
    assert edit_locked_fields(_tournament(**overrides)) == expected


def test_edit_locked_fields_are_form_field_names(ctx):
    form_fields = set(TournamentUpdateForm()._fields)
    everything = edit_locked_fields(
        _tournament(
            tournament_status=TournamentStatus.ONGOING,
            **_HIGHSCORE,
            **_REVIEW_PLAYOFFS,
            playoff_released_at=_RELEASED_AT,
        )
    )

    assert everything == _STATUS_LOCKED | _FFA_LOCKED | _ALL_PLAYOFF_LOCKED
    assert everything <= form_fields
    assert 'playoff_game_format' not in everything


def _edit_wizard(tournament, *, pass_tournament=True, **overrides):
    form = TournamentUpdateForm(data=tournament_form_data(tournament))
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()

    kwargs = {
        'mode': 'edit',
        'tournament': tournament if pass_tournament else None,
        'locked_fields': edit_locked_fields(tournament),
        'stored_values': {'min_players': '2', 'max_players': '16'},
    } | overrides
    return build_create_wizard_context(
        _Party(),
        form,
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls=_URLS,
        **kwargs,
    )


def test_edit_context_has_no_session_key_and_no_structure_requirement(ctx):
    tournament = _tournament(tournament_status=TournamentStatus.ONGOING)

    wizard = _edit_wizard(tournament)

    config = wizard['config']
    assert config['mode'] == 'edit'
    assert config['sessionKey'] is None
    assert config['requireStructure'] is False
    assert config['lockedFields'] == sorted(edit_locked_fields(tournament))
    assert config['storedValues'] == {'min_players': '2', 'max_players': '16'}
    assert config['editExemptFields'] == [
        'min_players',
        'max_players',
        'min_teams',
        'max_teams',
        'min_players_in_team',
        'max_players_in_team',
        'group_size_min',
        'group_size_max',
        'advancement_count',
        'point_table',
        'start_time',
    ]
    assert config['minMaxPairs'] == [
        ['min_players', 'max_players'],
        ['min_teams', 'max_teams'],
        ['min_players_in_team', 'max_players_in_team'],
    ]
    assert config['tournamentStatusLabel'] == 'Ongoing'
    assert config['urls'] == _URLS
    assert config['request'] is None
    assert config['strings'] == build_create_wizard_strings()
    json.dumps(config)


def test_edit_context_keeps_the_create_context_keys_and_adds_its_own(ctx):
    wizard = _edit_wizard(_tournament())

    assert set(wizard['config']) == _CREATE_CONFIG_KEYS | {
        'mode',
        'requireStructure',
        'lockedFields',
        'storedValues',
        'editExemptFields',
        'minMaxPairs',
        'tournamentStatusLabel',
    }
    assert set(wizard) == _CREATE_CONTEXT_KEYS | {
        'mode',
        'is_edit',
        'locked_fields',
        'tournament_status_label',
        'lock_banner',
        'playoff_lock_note',
    }
    assert wizard['mode'] == 'edit'
    assert wizard['is_edit'] is True


# fmt: off
@pytest.mark.parametrize(
    ('overrides', 'lock_banner', 'playoff_lock_note', 'status_label'),
    [
        ({}, False, False, 'Draft'),
        (
            {'tournament_status': TournamentStatus.REGISTRATION_OPEN},
            False, False, 'Registration open',
        ),
        (
            {'tournament_status': TournamentStatus.ONGOING},
            True, False, 'Ongoing',
        ),
        (
            {
                'tournament_status': TournamentStatus.PAUSED,
                **_REVIEW_PLAYOFFS,
            },
            True, True, 'Paused',
        ),
        (
            {
                'tournament_status': TournamentStatus.ONGOING,
                **_REVIEW_PLAYOFFS,
                'playoff_released_at': _RELEASED_AT,
            },
            True, False, 'Ongoing',
        ),
        (
            {
                'tournament_status': TournamentStatus.COMPLETED,
                **_REVIEW_PLAYOFFS,
            },
            False, True, 'Completed',
        ),
    ],
)
# fmt: on
def test_edit_context_banner_flags(
    ctx, overrides, lock_banner, playoff_lock_note, status_label
):
    wizard = _edit_wizard(_tournament(**overrides))

    assert wizard['lock_banner'] is lock_banner
    assert wizard['playoff_lock_note'] is playoff_lock_note
    assert wizard['tournament_status_label'] == status_label


def test_edit_mode_needs_the_tournament(ctx):
    with pytest.raises(ValueError):
        _edit_wizard(_tournament(), pass_tournament=False)


def _create_wizard(**kwargs):
    form = TournamentCreateForm()
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    return build_create_wizard_context(
        _Party(),
        form,
        source_request=None,
        source_proposer_name=None,
        staged_image=None,
        urls={'create': '/create'},
        **kwargs,
    )


def test_create_context_is_unchanged_by_the_edit_parameters(ctx):
    wizard = _create_wizard()

    assert set(wizard) == _CREATE_CONTEXT_KEYS
    assert set(wizard['config']) == _CREATE_CONFIG_KEYS
    assert wizard['config']['sessionKey'] == 'lt-create:p1:new'
    assert 'requireStructure' not in wizard['config']
    assert 'mode' not in wizard and 'mode' not in wizard['config']

    with_parameters = _create_wizard(
        mode='create',
        tournament=_tournament(tournament_status=TournamentStatus.ONGOING),
        locked_fields=frozenset({'name'}),
        stored_values={'min_players': '2'},
    )

    assert with_parameters == wizard


def test_views_reuse_the_moved_edit_tolerance_tuples():
    assert views._UNCHANGED_COUNT_FIELDS is helpers._UNCHANGED_COUNT_FIELDS
    assert views._MIN_MAX_PAIRS is helpers._MIN_MAX_PAIRS


_EDIT_STRINGS = {
    'Edit tournament': 'Turnier bearbeiten',
    'Save changes': 'Änderungen speichern',
    'Saving …': 'Speichert …',
    'Review and save': 'Prüfen & speichern',
    'The server rejected the changes.': 'Der Server hat die Änderungen abgelehnt.',
    'You can still save: the server checks everything again. Your entries are kept.': (
        'Du kannst trotzdem speichern: Der Server prüft beim Speichern '
        'alles erneut. Deine Eingaben bleiben erhalten.'
    ),
    'Changes are saved only when you press Save changes.': (
        'Änderungen werden erst gespeichert, wenn du '
        '„Änderungen speichern“ drückst.'
    ),
}


def test_edit_strings_have_german(ctx):
    strings = build_create_wizard_strings()
    with _PO.open('rb') as f:
        catalog = read_po(f)

    for msgid, german in _EDIT_STRINGS.items():
        assert strings[msgid] == msgid
        message = catalog.get(msgid)
        assert message is not None, msgid
        assert message.string == german
