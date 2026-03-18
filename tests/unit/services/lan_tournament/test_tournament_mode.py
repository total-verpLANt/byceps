import pytest

from byceps.services.lan_tournament.models.tournament_mode import TournamentMode


def test_single_elimination_requires_bracket():
    assert TournamentMode.SINGLE_ELIMINATION.requires_bracket is True


def test_double_elimination_requires_bracket():
    assert TournamentMode.DOUBLE_ELIMINATION.requires_bracket is True


def test_round_robin_requires_bracket():
    assert TournamentMode.ROUND_ROBIN.requires_bracket is True


def test_highscore_does_not_require_bracket():
    assert TournamentMode.HIGHSCORE.requires_bracket is False


def test_all_modes_have_requires_bracket():
    """Every enum member must expose the property without error."""
    for mode in TournamentMode:
        assert isinstance(mode.requires_bracket, bool)
