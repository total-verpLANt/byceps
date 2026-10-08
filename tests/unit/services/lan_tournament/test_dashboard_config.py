"""
tests.unit.services.lan_tournament.test_dashboard_config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from types import FunctionType, ModuleType

from flask import Flask
import pytest

from byceps.services.lan_tournament import dashboard_config
from byceps.services.lan_tournament.dashboard_config import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_POLL_SECONDS,
    DEFAULT_RED_MINUTES,
    DEFAULT_YELLOW_MINUTES,
    get_dashboard_settings,
    MAX_PAGE_SIZE,
    MAX_POLL_SECONDS,
    MAX_THRESHOLD_MINUTES,
    MIN_PAGE_SIZE,
    MIN_POLL_SECONDS,
    MIN_THRESHOLD_MINUTES,
    PAGE_SIZE_KEY,
    POLL_SECONDS_KEY,
    RED_MINUTES_KEY,
    validate_thresholds,
    YELLOW_MINUTES_KEY,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardSettings,
)
from byceps.util.result import Err, Ok


ALL_KEYS = (
    YELLOW_MINUTES_KEY,
    RED_MINUTES_KEY,
    POLL_SECONDS_KEY,
    PAGE_SIZE_KEY,
)

DEFAULT_SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

# Larger than the limit CPython puts on integer string conversion.
GIANT_DIGITS = '9' * 5000


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


def make_app(**config) -> Flask:
    app = Flask(__name__)
    app.config.update(config)
    return app


def load(app: Flask):
    with app.app_context():
        return get_dashboard_settings()


def error_of(result) -> str:
    assert result.is_err(), result
    return result.unwrap_err()


# -------------------------------------------------------------------- #
# precedence


def test_settings_defaults_and_precedence(monkeypatch):
    assert (
        DEFAULT_YELLOW_MINUTES,
        DEFAULT_RED_MINUTES,
        DEFAULT_POLL_SECONDS,
        DEFAULT_PAGE_SIZE,
    ) == (15, 45, 30, 50)
    assert load(make_app()) == Ok(DEFAULT_SETTINGS)

    # fmt: off
    cases = [
        (YELLOW_MINUTES_KEY, 'yellow_minutes', 10, '20'),
        (RED_MINUTES_KEY,    'red_minutes',    90, '60'),
        (POLL_SECONDS_KEY,   'poll_seconds',   60, '120'),
        (PAGE_SIZE_KEY,      'page_size',      25, '75'),
    ]
    # fmt: on
    for key, field, app_value, env_value in cases:
        default = getattr(DEFAULT_SETTINGS, field)

        # Environment beats the default.
        monkeypatch.setenv(key, env_value)
        settings = load(make_app()).unwrap()
        assert getattr(settings, field) == int(env_value) != default
        assert (
            dataclasses.replace(settings, **{field: default})
            == DEFAULT_SETTINGS
        )

        # The app key beats the environment.
        settings = load(make_app(**{key: app_value})).unwrap()
        assert getattr(settings, field) == app_value != int(env_value)
        assert (
            dataclasses.replace(settings, **{field: default})
            == DEFAULT_SETTINGS
        )

        monkeypatch.delenv(key)

        # The app key beats the default.
        settings = load(make_app(**{key: app_value})).unwrap()
        assert getattr(settings, field) == app_value != default


def test_settings_precedence_is_resolved_per_key(monkeypatch):
    monkeypatch.setenv(RED_MINUTES_KEY, '60')
    monkeypatch.setenv(POLL_SECONDS_KEY, '10')
    app = make_app(**{YELLOW_MINUTES_KEY: 5, POLL_SECONDS_KEY: 90})

    assert load(app) == Ok(
        DashboardSettings(
            yellow_minutes=5,  # app
            red_minutes=60,  # environment
            poll_seconds=90,  # app beats environment
            page_size=50,  # default
            threshold_source='deployment',
        )
    )


def test_unset_app_key_and_unset_environment_fall_through(monkeypatch):
    monkeypatch.setenv(YELLOW_MINUTES_KEY, '20')

    # `None` is "not set", like in the environment parser, so the next
    # source applies.
    app = make_app(**{YELLOW_MINUTES_KEY: None, RED_MINUTES_KEY: None})
    settings = load(app).unwrap()
    assert (settings.yellow_minutes, settings.red_minutes) == (20, 45)

    monkeypatch.setenv(YELLOW_MINUTES_KEY, 'null')
    assert load(make_app()) == Ok(DEFAULT_SETTINGS)


def test_valid_app_value_wins_over_a_garbage_environment(monkeypatch):
    monkeypatch.setenv(PAGE_SIZE_KEY, 'not-a-number')

    settings = load(make_app(**{PAGE_SIZE_KEY: 20})).unwrap()

    assert settings.page_size == 20


def test_environment_is_read_when_called_not_on_import(monkeypatch):
    app = make_app()
    assert load(app) == Ok(DEFAULT_SETTINGS)

    monkeypatch.setenv(PAGE_SIZE_KEY, '10')
    assert load(app).unwrap().page_size == 10

    monkeypatch.delenv(PAGE_SIZE_KEY)
    assert load(app) == Ok(DEFAULT_SETTINGS)


def test_settings_carry_the_deployment_source_and_are_frozen():
    settings = load(make_app()).unwrap()

    assert settings.threshold_source == 'deployment'
    for field in (
        'yellow_minutes',
        'red_minutes',
        'poll_seconds',
        'page_size',
    ):
        assert type(getattr(settings, field)) is int
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.yellow_minutes = 1  # type: ignore[misc]


# -------------------------------------------------------------------- #
# per app


def test_settings_are_per_app(monkeypatch):
    app_a = make_app(
        **{YELLOW_MINUTES_KEY: 5, RED_MINUTES_KEY: 10, PAGE_SIZE_KEY: 20}
    )
    app_b = make_app(**{POLL_SECONDS_KEY: 120})
    app_default = make_app()

    expected_a = DashboardSettings(
        yellow_minutes=5,
        red_minutes=10,
        poll_seconds=30,
        page_size=20,
        threshold_source='deployment',
    )
    expected_b = dataclasses.replace(DEFAULT_SETTINGS, poll_seconds=120)

    # Interleaved calls: no app's values leak into another one.
    for _ in range(2):
        assert load(app_a) == Ok(expected_a)
        assert load(app_b) == Ok(expected_b)
        assert load(app_default) == Ok(DEFAULT_SETTINGS)

    # Nested contexts resolve against the innermost app.
    with app_a.app_context():
        with app_b.app_context():
            assert get_dashboard_settings() == Ok(expected_b)
        assert get_dashboard_settings() == Ok(expected_a)

    # A config change of one app is seen at once and touches only it.
    app_a.config[YELLOW_MINUTES_KEY] = 7
    assert load(app_a).unwrap().yellow_minutes == 7
    assert load(app_b) == Ok(expected_b)
    assert load(app_default) == Ok(DEFAULT_SETTINGS)

    # An app with an invalid value does not poison its neighbours.
    app_bad = make_app(**{YELLOW_MINUTES_KEY: True})
    assert error_of(load(app_bad)) == 'invalid_dashboard_yellow_minutes'
    assert load(app_default) == Ok(DEFAULT_SETTINGS)
    assert load(app_b) == Ok(expected_b)


def test_settings_provider_has_no_import_time_mutable_state():
    mutable_types = (dict, list, set, bytearray)
    leaked = [
        name
        for name, value in vars(dashboard_config).items()
        if not name.startswith('__')
        and not isinstance(value, (ModuleType, FunctionType, type))
        and isinstance(value, mutable_types)
    ]

    assert leaked == []

    for name in dir(dashboard_config):
        value = getattr(dashboard_config, name)
        if name.isupper():
            assert isinstance(value, (int, str)), name

    for function in (
        dashboard_config.get_dashboard_settings,
        dashboard_config._resolve,
    ):
        assert function.__closure__ is None
        assert function.__defaults__ is None


def test_no_app_context_is_an_error_not_a_default():
    with pytest.raises(RuntimeError):
        get_dashboard_settings()


# -------------------------------------------------------------------- #
# invalid values


# fmt: off
INVALID_VALUE_CASES = [
    # key,                value,   expected error
    (YELLOW_MINUTES_KEY, True,    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, False,   'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 15.0,    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 14.5,    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '15',    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '900s',  'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '',      'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, [15],    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, {'m': 15}, 'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, float('nan'), 'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 0,       'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, -15,     'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 1441,    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 10 ** 30, 'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 45,      'invalid_dashboard_threshold_order'),
    (YELLOW_MINUTES_KEY, 46,      'invalid_dashboard_threshold_order'),
    (RED_MINUTES_KEY,    True,    'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    45.0,    'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    '45',    'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    [45],    'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    0,       'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    -1,      'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    1441,    'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    10 ** 30, 'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    15,      'invalid_dashboard_threshold_order'),
    (RED_MINUTES_KEY,    14,      'invalid_dashboard_threshold_order'),
    (POLL_SECONDS_KEY,   True,    'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   False,   'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   30.0,    'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   2.5,     'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   '30',    'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   {},      'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   4,       'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   0,       'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   -30,     'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   301,     'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   10 ** 30, 'invalid_dashboard_poll_seconds'),
    (PAGE_SIZE_KEY,      True,    'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      False,   'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      50.0,    'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      '50',    'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      [50],    'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      0,       'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      -1,      'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      101,     'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      10 ** 30, 'invalid_dashboard_page_size'),
]
# fmt: on


@pytest.mark.parametrize('key, value, expected_error', INVALID_VALUE_CASES)
def test_invalid_settings_fail_explicitly(key, value, expected_error):
    app = make_app(**{key: value})

    assert load(app) == Err(expected_error)


@pytest.mark.parametrize(
    'key, value, expected_error',
    [
        case
        for case in INVALID_VALUE_CASES
        # The order depends on the other threshold, which the environment
        # sets here.
        if case[2] != 'invalid_dashboard_threshold_order'
    ],
)
def test_invalid_app_value_is_not_rescued_by_a_valid_environment(
    monkeypatch, key, value, expected_error
):
    # The environment holds a valid value for every key, the invalid app
    # value must not fall through to it (or to the default).
    monkeypatch.setenv(YELLOW_MINUTES_KEY, '10')
    monkeypatch.setenv(RED_MINUTES_KEY, '50')
    monkeypatch.setenv(POLL_SECONDS_KEY, '60')
    monkeypatch.setenv(PAGE_SIZE_KEY, '20')
    app = make_app(**{key: value})

    assert load(app) == Err(expected_error)


# fmt: off
INVALID_ENVIRONMENT_CASES = [
    # key,                value,          expected error
    (YELLOW_MINUTES_KEY, 'true',         'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 'false',        'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '15.0',         'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '14.5',         'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '1e1',          'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, 'NaN',          'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '15s',          'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '"15"',         'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '[15]',         'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '{"m": 15}',    'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '',             'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, ' ',            'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '0',            'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '-5',           'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '1441',         'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, GIANT_DIGITS,   'invalid_dashboard_yellow_minutes'),
    (YELLOW_MINUTES_KEY, '45',           'invalid_dashboard_threshold_order'),
    (RED_MINUTES_KEY,    'true',         'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    '45.5',         'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    '1441',         'invalid_dashboard_red_minutes'),
    (RED_MINUTES_KEY,    '15',           'invalid_dashboard_threshold_order'),
    (POLL_SECONDS_KEY,   'true',         'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   '30.0',         'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   '4',            'invalid_dashboard_poll_seconds'),
    (POLL_SECONDS_KEY,   '301',          'invalid_dashboard_poll_seconds'),
    (PAGE_SIZE_KEY,      'true',         'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      'fifty',        'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      '0',            'invalid_dashboard_page_size'),
    (PAGE_SIZE_KEY,      '101',          'invalid_dashboard_page_size'),
]
# fmt: on


@pytest.mark.parametrize(
    'key, value, expected_error', INVALID_ENVIRONMENT_CASES
)
def test_invalid_environment_fails_explicitly(
    monkeypatch, key, value, expected_error
):
    monkeypatch.setenv(key, value)

    assert load(make_app()) == Err(expected_error)


def test_first_failing_setting_is_reported_in_a_fixed_order():
    app = make_app(
        **{
            YELLOW_MINUTES_KEY: 0,
            RED_MINUTES_KEY: 0,
            POLL_SECONDS_KEY: 0,
            PAGE_SIZE_KEY: 0,
        }
    )
    assert load(app) == Err('invalid_dashboard_yellow_minutes')

    app.config[YELLOW_MINUTES_KEY] = 15
    assert load(app) == Err('invalid_dashboard_red_minutes')

    app.config[RED_MINUTES_KEY] = 45
    assert load(app) == Err('invalid_dashboard_poll_seconds')

    app.config[POLL_SECONDS_KEY] = 30
    assert load(app) == Err('invalid_dashboard_page_size')

    app.config[PAGE_SIZE_KEY] = 50
    assert load(app) == Ok(DEFAULT_SETTINGS)


# -------------------------------------------------------------------- #
# whole-minute thresholds


def test_bounds_are_the_documented_ones():
    assert (MIN_THRESHOLD_MINUTES, MAX_THRESHOLD_MINUTES) == (1, 1440)
    assert (MIN_POLL_SECONDS, MAX_POLL_SECONDS) == (5, 300)
    assert (MIN_PAGE_SIZE, MAX_PAGE_SIZE) == (1, 100)


# fmt: off
THRESHOLD_CASES = [
    # yellow,  red,       expected error (`None`: accepted)
    (15,       45,        None),
    (1,        2,         None),
    (14,       15,        None),
    (1,        1440,      None),
    (1439,     1440,      None),
    (720,      721,       None),
    (60,       120,       None),
    # Seconds are refused, whether fractional or merely too large.
    (901 / 60, 45,        'invalid_dashboard_yellow_minutes'),
    (15,       902 / 60,  'invalid_dashboard_red_minutes'),
    (14.983,   45,        'invalid_dashboard_yellow_minutes'),
    (15.0,     45,        'invalid_dashboard_yellow_minutes'),
    (15,       45.0,      'invalid_dashboard_red_minutes'),
    ('900s',   45,        'invalid_dashboard_yellow_minutes'),
    (15,       '2700s',   'invalid_dashboard_red_minutes'),
    (900,      2700,      'invalid_dashboard_red_minutes'),
    # `bool` is not a whole number of minutes.
    (True,     45,        'invalid_dashboard_yellow_minutes'),
    (1,        True,      'invalid_dashboard_red_minutes'),
    # Bounds: 1 <= yellow < red <= 1440.
    (0,        45,        'invalid_dashboard_yellow_minutes'),
    (-1,       45,        'invalid_dashboard_yellow_minutes'),
    (15,       0,         'invalid_dashboard_red_minutes'),
    (15,       -45,       'invalid_dashboard_red_minutes'),
    (1441,     1442,      'invalid_dashboard_yellow_minutes'),
    (15,       1441,      'invalid_dashboard_red_minutes'),
    (1440,     1441,      'invalid_dashboard_red_minutes'),
    # Order: yellow must stay below red.
    (15,       15,        'invalid_dashboard_threshold_order'),
    (1,        1,         'invalid_dashboard_threshold_order'),
    (1440,     1440,      'invalid_dashboard_threshold_order'),
    (45,       15,        'invalid_dashboard_threshold_order'),
    (1440,     1,         'invalid_dashboard_threshold_order'),
]
# fmt: on


@pytest.mark.parametrize('yellow, red, expected_error', THRESHOLD_CASES)
def test_thresholds_are_whole_minutes_within_bounds(
    yellow, red, expected_error
):
    config = {YELLOW_MINUTES_KEY: yellow, RED_MINUTES_KEY: red}
    result = load(make_app(**config))

    if expected_error is not None:
        assert validate_thresholds(yellow, red) == Err(expected_error)
        assert result == Err(expected_error)
        return

    assert validate_thresholds(yellow, red) == Ok((yellow, red))
    settings = result.unwrap()
    assert (settings.yellow_minutes, settings.red_minutes) == (yellow, red)
    assert type(settings.yellow_minutes) is int
    assert type(settings.red_minutes) is int
    assert 1 <= settings.yellow_minutes < settings.red_minutes <= 1440


def test_validator_refuses_unset_thresholds():
    assert validate_thresholds(None, 45) == Err(
        'invalid_dashboard_yellow_minutes'
    )
    assert validate_thresholds(15, None) == Err('invalid_dashboard_red_minutes')
