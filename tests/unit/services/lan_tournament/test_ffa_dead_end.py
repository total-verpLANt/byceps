"""
tests.unit.services.lan_tournament.test_ffa_dead_end
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest

from byceps.services.lan_tournament import tournament_seeding_domain_service
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.tournament_domain_service import (
    _ffa_lobby_split,
    ffa_single_track_dead_end,
    FfaDeadEnd,
)


# fmt: off
@pytest.mark.parametrize(
    ('count', 'group_min', 'group_max', 'cut', 'expected'),
    [
        (8,  4, 4, 3, (1, 6, (3, 3))),
        (12, 4, 4, 2, (1, 6, (3, 3))),
        (18, 6, 8, 3, (1, 9, (5, 4))),
        (8,  4, 4, 2, None),
        (16, 4, 6, 2, None),
        (6,  2, 2, 1, (1, 3, (2, 1))),
        (4,  4, 8, 2, None),
    ],
)
# fmt: on
def test_dead_end_table(count, group_min, group_max, cut, expected):
    dead_end = ffa_single_track_dead_end(count, group_min, group_max, cut)

    if expected is None:
        assert dead_end is None
        return
    round_number, next_count, sizes = expected
    assert dead_end is not None
    assert dead_end.reason == 'below_minimum'
    assert dead_end.round_number == round_number
    assert dead_end.count == next_count
    assert dead_end.lobby_sizes == sizes


def test_lobby_split_matches_the_seeding_layout():
    for count in range(2, 65):
        for group_max in range(2, 11):
            assert sorted(_ffa_lobby_split(count, group_max)) == sorted(
                tournament_seeding_domain_service.group_sizes(
                    SeedingFormat.FREE_FOR_ALL, count, group_max
                )
            ), (count, group_max)


def test_a_field_that_never_shrinks_is_a_dead_end():
    dead_end = ffa_single_track_dead_end(8, None, 4, 3)

    assert dead_end == FfaDeadEnd(
        reason='no_progress',
        round_number=2,
        count=6,
        lobby_sizes=(3, 3),
        minimum=2,
    )


def test_a_single_lobby_final_is_valid():
    assert ffa_single_track_dead_end(6, 4, 8, 3) is None
