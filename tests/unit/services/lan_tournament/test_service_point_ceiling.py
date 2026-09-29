"""
tests.unit.services.lan_tournament.test_service_point_ceiling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The service validation of an FFA config bounds the point table.
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType, GameFormat


CEILING = tournament_domain_service.MAX_POINTS_PER_PLACE
MAX_PLACES = tournament_domain_service.MAX_POINT_TABLE_PLACES


def _validate(point_table, game_format=GameFormat.FREE_FOR_ALL):
    return tournament_service._validate_ffa_config(
        game_format,
        point_table,
        4,
        ContestantType.SOLO,
        None,
        2,
    )


# fmt: off
@pytest.mark.parametrize(
    'point_table',
    [
        [CEILING + 1],
        [10, 6, 10**12],
        [2**31],
        [10**300],
    ],
)
# fmt: on
def test_a_place_value_above_the_ceiling_is_an_error(point_table):
    result = _validate(point_table)

    assert result.is_err()
    assert result.unwrap_err() == f'Points may be at most {CEILING}.'


# fmt: off
@pytest.mark.parametrize(
    'point_table',
    [
        [-(CEILING + 1)],
        [10, -(10**12), 3],
        [-(10**300)],
    ],
)
# fmt: on
def test_a_place_value_below_the_floor_is_an_error(point_table):
    result = _validate(point_table)

    assert result.is_err()
    assert result.unwrap_err() == f'Points may be at least {-CEILING}.'


# fmt: off
@pytest.mark.parametrize(
    'point_table',
    [
        [CEILING],
        [-CEILING],
        [CEILING, 0, -CEILING],
        [10, 6, 3, 1],
        [],
    ],
)
# fmt: on
def test_place_values_up_to_the_ceiling_are_valid(point_table):
    assert _validate(point_table).is_ok()


# fmt: off
@pytest.mark.parametrize(
    'point_table',
    [
        [1] * (MAX_PLACES + 1),
        [1] * 1000,
        list(range(MAX_PLACES + 1)),
    ],
)
# fmt: on
def test_more_places_than_the_maximum_is_an_error(point_table):
    result = _validate(point_table)

    assert result.is_err()
    assert result.unwrap_err() == f'At most {MAX_PLACES} places.'


def test_too_many_places_are_reported_before_too_high_values():
    result = _validate([10**12] * (MAX_PLACES + 1))

    assert result.unwrap_err() == f'At most {MAX_PLACES} places.'


# fmt: off
@pytest.mark.parametrize(
    'point_table',
    [
        [1] * MAX_PLACES,
        [CEILING] * MAX_PLACES,
    ],
)
# fmt: on
def test_the_maximum_number_of_places_is_valid(point_table):
    assert _validate(point_table).is_ok()


def test_the_table_limits_can_be_skipped_for_an_unchanged_table():
    result = tournament_service._validate_ffa_config(
        GameFormat.FREE_FOR_ALL,
        [CEILING + 1] * (MAX_PLACES + 1),
        4,
        ContestantType.SOLO,
        None,
        2,
        check_table_limits=False,
    )

    assert result.is_ok()


def test_the_table_limits_apply_by_default():
    assert _validate([1] * (MAX_PLACES + 1)).is_err()
