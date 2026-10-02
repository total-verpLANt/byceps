"""
tests.unit.services.lan_tournament.test_phase_format_helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The view helpers that decide by phase whether matches are placement-based.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    ffa_elimination_mode,
    ffa_phase,
    match_uses_placements,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import Tournament


FFA = GameFormat.FREE_FOR_ALL
HS = GameFormat.HIGHSCORE
RR = GameFormat.ONE_V_ONE
SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION
NO = EliminationMode.NONE


def _tournament(
    game_format,
    elimination_mode=NO,
    *,
    has_playoffs=False,
    playoff_game_format=None,
    playoff_elimination_mode=None,
):
    return SimpleNamespace(
        game_format=game_format,
        elimination_mode=elimination_mode,
        has_playoffs=has_playoffs,
        playoff_game_format=playoff_game_format,
        playoff_elimination_mode=playoff_elimination_mode,
    )


# fmt: off
@pytest.mark.parametrize(
    ('tournament', 'phase', 'expected'),
    [
        (_tournament(FFA, SE),                                                                  1, True),
        (_tournament(RR, SE),                                                                   1, False),
        (_tournament(HS, has_playoffs=True, playoff_game_format=FFA, playoff_elimination_mode=SE), 2, True),
        (_tournament(HS, has_playoffs=True, playoff_game_format=FFA, playoff_elimination_mode=SE), 1, False),
        (_tournament(RR, has_playoffs=True, playoff_game_format=RR, playoff_elimination_mode=SE),  1, False),
        (_tournament(RR, has_playoffs=True, playoff_game_format=RR, playoff_elimination_mode=SE),  2, False),
    ],
)
# fmt: on
def test_match_uses_placements(tournament, phase, expected):
    match = SimpleNamespace(phase=phase)

    assert match_uses_placements(tournament, match) is expected


# fmt: off
@pytest.mark.parametrize(
    ('tournament', 'expected_phase', 'expected_mode'),
    [
        (_tournament(FFA, DE),                                                                      1,    DE),
        (_tournament(HS, has_playoffs=True, playoff_game_format=FFA, playoff_elimination_mode=DE),  2,    DE),
        (_tournament(HS),                                                                           None, None),
        (_tournament(RR, SE),                                                                       None, None),
    ],
)
# fmt: on
def test_ffa_phase_and_mode(tournament, expected_phase, expected_mode):
    assert ffa_phase(tournament) == expected_phase
    assert ffa_elimination_mode(tournament) == expected_mode


def test_mock_tournament_stays_on_the_phase_less_path():
    tournament = MagicMock(spec=Tournament)
    tournament.game_format = FFA
    match = SimpleNamespace(phase=2)

    assert match_uses_placements(tournament, match) is True
