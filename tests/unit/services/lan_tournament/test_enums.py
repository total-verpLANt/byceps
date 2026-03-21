"""
tests.unit.services.lan_tournament.test_enums
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


def test_contestant_type_has_expected_members():
    assert set(ContestantType.__members__) == {'SOLO', 'TEAM'}


def test_contestant_type_values():
    assert ContestantType.SOLO.value == 1
    assert ContestantType.TEAM.value == 2


def test_contestant_type_has_no_not_set():
    assert 'NOT_SET' not in ContestantType.__members__


def test_game_format_has_expected_members():
    assert set(GameFormat.__members__) == {
        'ONE_V_ONE',
        'FREE_FOR_ALL',
        'HIGHSCORE',
    }


def test_game_format_values():
    assert GameFormat.ONE_V_ONE.value == 'ONE_V_ONE'
    assert GameFormat.FREE_FOR_ALL.value == 'FREE_FOR_ALL'
    assert GameFormat.HIGHSCORE.value == 'HIGHSCORE'


def test_elimination_mode_has_expected_members():
    assert set(EliminationMode.__members__) == {
        'SINGLE_ELIMINATION',
        'DOUBLE_ELIMINATION',
        'ROUND_ROBIN',
        'NONE',
    }


def test_elimination_mode_values():
    assert EliminationMode.SINGLE_ELIMINATION.value == 'SINGLE_ELIMINATION'
    assert EliminationMode.DOUBLE_ELIMINATION.value == 'DOUBLE_ELIMINATION'
    assert EliminationMode.ROUND_ROBIN.value == 'ROUND_ROBIN'
    assert EliminationMode.NONE.value == 'NONE'


def test_tournament_status_has_expected_members():
    assert set(TournamentStatus.__members__) == {
        'DRAFT',
        'REGISTRATION_OPEN',
        'REGISTRATION_CLOSED',
        'ONGOING',
        'PAUSED',
        'COMPLETED',
        'CANCELLED',
    }


def test_tournament_status_values():
    assert TournamentStatus.DRAFT.value == 1
    assert TournamentStatus.REGISTRATION_OPEN.value == 2
    assert TournamentStatus.REGISTRATION_CLOSED.value == 3
    assert TournamentStatus.ONGOING.value == 4
    assert TournamentStatus.PAUSED.value == 5
    assert TournamentStatus.COMPLETED.value == 6
    assert TournamentStatus.CANCELLED.value == 7


def test_tournament_status_has_no_not_set():
    assert 'NOT_SET' not in TournamentStatus.__members__
