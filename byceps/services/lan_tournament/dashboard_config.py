"""
byceps.services.lan_tournament.dashboard_config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from typing import TypeGuard

from flask import current_app

from byceps.config.integration import parse_value_from_environment
from byceps.util.result import Err, Ok, Result

from .models.tournament_dashboard import DashboardSettings


YELLOW_MINUTES_KEY = 'LAN_TOURNAMENT_DASHBOARD_YELLOW_MINUTES'
RED_MINUTES_KEY = 'LAN_TOURNAMENT_DASHBOARD_RED_MINUTES'
POLL_SECONDS_KEY = 'LAN_TOURNAMENT_DASHBOARD_POLL_SECONDS'
PAGE_SIZE_KEY = 'LAN_TOURNAMENT_DASHBOARD_PAGE_SIZE'

DEFAULT_YELLOW_MINUTES = 15
DEFAULT_RED_MINUTES = 45
DEFAULT_POLL_SECONDS = 30
DEFAULT_PAGE_SIZE = 50

MIN_THRESHOLD_MINUTES = 1
MAX_THRESHOLD_MINUTES = 1440
MIN_POLL_SECONDS = 5
MAX_POLL_SECONDS = 300
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 100

INVALID_YELLOW_MINUTES_ERROR = 'invalid_dashboard_yellow_minutes'
INVALID_RED_MINUTES_ERROR = 'invalid_dashboard_red_minutes'
INVALID_THRESHOLD_ORDER_ERROR = 'invalid_dashboard_threshold_order'
INVALID_POLL_SECONDS_ERROR = 'invalid_dashboard_poll_seconds'
INVALID_PAGE_SIZE_ERROR = 'invalid_dashboard_page_size'


def get_dashboard_settings() -> Result[DashboardSettings, str]:
    """Return the deployment settings of the current app.

    Each value comes from the app config key, else from the environment
    variable of the same name, else from the default. A value that is set
    but invalid is an error, never a fallback to the next source.
    """
    thresholds_result = validate_thresholds(
        _resolve(YELLOW_MINUTES_KEY, DEFAULT_YELLOW_MINUTES),
        _resolve(RED_MINUTES_KEY, DEFAULT_RED_MINUTES),
    )
    if thresholds_result.is_err():
        return Err(thresholds_result.unwrap_err())
    yellow_minutes, red_minutes = thresholds_result.unwrap()

    poll_seconds = _resolve(POLL_SECONDS_KEY, DEFAULT_POLL_SECONDS)
    if not _is_whole_number_within(
        poll_seconds, MIN_POLL_SECONDS, MAX_POLL_SECONDS
    ):
        return Err(INVALID_POLL_SECONDS_ERROR)

    page_size = _resolve(PAGE_SIZE_KEY, DEFAULT_PAGE_SIZE)
    if not _is_whole_number_within(page_size, MIN_PAGE_SIZE, MAX_PAGE_SIZE):
        return Err(INVALID_PAGE_SIZE_ERROR)

    return Ok(
        DashboardSettings(
            yellow_minutes=yellow_minutes,
            red_minutes=red_minutes,
            poll_seconds=poll_seconds,
            page_size=page_size,
            threshold_source='deployment',
        )
    )


def validate_thresholds(
    yellow_minutes: object, red_minutes: object
) -> Result[tuple[int, int], str]:
    """Return the thresholds if they are ordered whole minutes in bounds."""
    if not _is_whole_number_within(
        yellow_minutes, MIN_THRESHOLD_MINUTES, MAX_THRESHOLD_MINUTES
    ):
        return Err(INVALID_YELLOW_MINUTES_ERROR)

    if not _is_whole_number_within(
        red_minutes, MIN_THRESHOLD_MINUTES, MAX_THRESHOLD_MINUTES
    ):
        return Err(INVALID_RED_MINUTES_ERROR)

    if yellow_minutes >= red_minutes:
        return Err(INVALID_THRESHOLD_ORDER_ERROR)

    return Ok((yellow_minutes, red_minutes))


def _resolve(key: str, default: int) -> object:
    """Return the app config value, else the environment value, else the default."""
    value = current_app.config.get(key)
    if value is not None:
        return value

    value = parse_value_from_environment(key)
    if value is not None:
        return value

    return default


def _is_whole_number_within(
    value: object, minimum: int, maximum: int
) -> TypeGuard[int]:
    """Tell if the value is an `int` (not a `bool`) within the bounds."""
    return type(value) is int and minimum <= value <= maximum
