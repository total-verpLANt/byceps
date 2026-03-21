"""
tests.unit.services.lan_tournament.test_tournament_mode
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for the GameFormat + EliminationMode enum properties,
is_valid_combination logic, and ContestantStatus enum values.

These replace the former TournamentMode tests after the mode
composition refactoring.
"""

import pytest

from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
    is_valid_combination,
    VALID_COMBINATIONS,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.contestant_status import (
    ContestantStatus,
)


# -------------------------------------------------------------------- #
# GameFormat.has_match_structure
# -------------------------------------------------------------------- #


def test_one_v_one_has_match_structure():
    assert GameFormat.ONE_V_ONE.has_match_structure is True


def test_free_for_all_has_match_structure():
    assert GameFormat.FREE_FOR_ALL.has_match_structure is True


def test_highscore_has_no_match_structure():
    assert GameFormat.HIGHSCORE.has_match_structure is False


# -------------------------------------------------------------------- #
# GameFormat.uses_placements
# -------------------------------------------------------------------- #


def test_one_v_one_does_not_use_placements():
    assert GameFormat.ONE_V_ONE.uses_placements is False


def test_free_for_all_uses_placements():
    assert GameFormat.FREE_FOR_ALL.uses_placements is True


def test_highscore_does_not_use_placements():
    assert GameFormat.HIGHSCORE.uses_placements is False


# -------------------------------------------------------------------- #
# GameFormat.requires_bracket_generation
# -------------------------------------------------------------------- #


def test_one_v_one_requires_bracket_generation():
    assert GameFormat.ONE_V_ONE.requires_bracket_generation is True


def test_free_for_all_does_not_require_bracket_generation():
    assert GameFormat.FREE_FOR_ALL.requires_bracket_generation is False


def test_highscore_does_not_require_bracket_generation():
    assert GameFormat.HIGHSCORE.requires_bracket_generation is False


# -------------------------------------------------------------------- #
# All GameFormat members expose properties without error
# -------------------------------------------------------------------- #


def test_all_game_formats_have_has_match_structure():
    for fmt in GameFormat:
        assert isinstance(fmt.has_match_structure, bool)


def test_all_game_formats_have_uses_placements():
    for fmt in GameFormat:
        assert isinstance(fmt.uses_placements, bool)


def test_all_game_formats_have_requires_bracket_generation():
    for fmt in GameFormat:
        assert isinstance(fmt.requires_bracket_generation, bool)


# -------------------------------------------------------------------- #
# EliminationMode.has_pools
# -------------------------------------------------------------------- #


def test_single_elimination_has_no_pools():
    assert EliminationMode.SINGLE_ELIMINATION.has_pools is False


def test_double_elimination_has_pools():
    assert EliminationMode.DOUBLE_ELIMINATION.has_pools is True


def test_round_robin_has_no_pools():
    assert EliminationMode.ROUND_ROBIN.has_pools is False


def test_none_has_no_pools():
    assert EliminationMode.NONE.has_pools is False


def test_all_elimination_modes_have_has_pools():
    for mode in EliminationMode:
        assert isinstance(mode.has_pools, bool)


# -------------------------------------------------------------------- #
# is_valid_combination — valid pairs
# -------------------------------------------------------------------- #


def test_one_v_one_single_elimination_is_valid():
    assert is_valid_combination(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION) is True


def test_one_v_one_double_elimination_is_valid():
    assert is_valid_combination(GameFormat.ONE_V_ONE, EliminationMode.DOUBLE_ELIMINATION) is True


def test_one_v_one_round_robin_is_valid():
    assert is_valid_combination(GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN) is True


def test_ffa_single_elimination_is_valid():
    assert is_valid_combination(GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION) is True


def test_ffa_double_elimination_is_valid():
    assert is_valid_combination(GameFormat.FREE_FOR_ALL, EliminationMode.DOUBLE_ELIMINATION) is True


def test_highscore_none_is_valid():
    assert is_valid_combination(GameFormat.HIGHSCORE, EliminationMode.NONE) is True


# -------------------------------------------------------------------- #
# is_valid_combination — invalid pairs
# -------------------------------------------------------------------- #


def test_one_v_one_none_is_invalid():
    assert is_valid_combination(GameFormat.ONE_V_ONE, EliminationMode.NONE) is False


def test_highscore_single_elimination_is_invalid():
    assert is_valid_combination(GameFormat.HIGHSCORE, EliminationMode.SINGLE_ELIMINATION) is False


def test_highscore_double_elimination_is_invalid():
    assert is_valid_combination(GameFormat.HIGHSCORE, EliminationMode.DOUBLE_ELIMINATION) is False


def test_highscore_round_robin_is_invalid():
    assert is_valid_combination(GameFormat.HIGHSCORE, EliminationMode.ROUND_ROBIN) is False


def test_ffa_round_robin_is_invalid():
    assert is_valid_combination(GameFormat.FREE_FOR_ALL, EliminationMode.ROUND_ROBIN) is False


def test_ffa_none_is_invalid():
    assert is_valid_combination(GameFormat.FREE_FOR_ALL, EliminationMode.NONE) is False


# -------------------------------------------------------------------- #
# VALID_COMBINATIONS exhaustive check
# -------------------------------------------------------------------- #


def test_valid_combinations_count():
    """There are exactly 6 valid (GameFormat, EliminationMode) pairs."""
    assert len(VALID_COMBINATIONS) == 6


def test_every_valid_pair_passes_is_valid_combination():
    for game_format, elimination_mode in VALID_COMBINATIONS:
        assert is_valid_combination(game_format, elimination_mode) is True


def test_all_invalid_pairs_fail_is_valid_combination():
    """Every (GameFormat, EliminationMode) pair NOT in the set is invalid."""
    for game_format in GameFormat:
        for elimination_mode in EliminationMode:
            if (game_format, elimination_mode) not in VALID_COMBINATIONS:
                assert is_valid_combination(game_format, elimination_mode) is False, (
                    f'Expected ({game_format}, {elimination_mode}) to be invalid'
                )


# -------------------------------------------------------------------- #
# ContestantStatus enum values
# -------------------------------------------------------------------- #


def test_contestant_status_has_expected_members():
    assert set(ContestantStatus.__members__) == {
        'FINISHED',
        'DNS',
        'DNF',
        'DQ',
    }


def test_contestant_status_values():
    assert ContestantStatus.FINISHED.value == 'FINISHED'
    assert ContestantStatus.DNS.value == 'DNS'
    assert ContestantStatus.DNF.value == 'DNF'
    assert ContestantStatus.DQ.value == 'DQ'
