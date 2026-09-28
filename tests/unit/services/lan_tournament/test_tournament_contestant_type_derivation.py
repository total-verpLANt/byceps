"""
tests.unit.services.lan_tournament.test_tournament_contestant_type_derivation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from types import SimpleNamespace

import pytest
from werkzeug.exceptions import NotFound

from byceps.services.lan_tournament.blueprints.site.views import (
    _require_team_tournament,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    derive_contestant_type,
)


# fmt: off
@pytest.mark.parametrize(
    ('contestant_type', 'max_players_in_team', 'min_players_in_team', 'expected'),
    [
        # explicit type is kept unchanged, team size is irrelevant
        (ContestantType.SOLO, None, None, ContestantType.SOLO),
        (ContestantType.SOLO, 5,    None, ContestantType.SOLO),
        (ContestantType.TEAM, None, None, ContestantType.TEAM),
        (ContestantType.TEAM, 1,    None, ContestantType.TEAM),

        # missing type: derive from max_players_in_team
        (None,                1,    None, ContestantType.SOLO),
        (None,                2,    None, ContestantType.TEAM),
        (None,                5,    None, ContestantType.TEAM),

        # missing type and max: fall back to min_players_in_team
        (None,                None, 1,    ContestantType.SOLO),
        (None,                None, 2,    ContestantType.TEAM),
        (None,                None, 5,    ContestantType.TEAM),

        # max_players_in_team wins over min_players_in_team when both are set
        (None,                1,    5,    ContestantType.SOLO),
        (None,                5,    1,    ContestantType.TEAM),

        # no team size at all: SOLO
        (None,                None, None, ContestantType.SOLO),
    ],
)
# fmt: on
def test_derive_contestant_type(
    contestant_type, max_players_in_team, min_players_in_team, expected
):
    result = derive_contestant_type(
        contestant_type, max_players_in_team, min_players_in_team
    )

    assert result == expected


# -------------------------------------------------------------------- #
# _require_team_tournament (site blueprint's team-route guard)


def test_require_team_tournament_lets_derived_team_through():
    tournament = SimpleNamespace(
        contestant_type=None,
        max_players_in_team=2,
        min_players_in_team=None,
    )

    _require_team_tournament(tournament)  # must not raise/abort


def test_require_team_tournament_404s_derived_solo():
    tournament = SimpleNamespace(
        contestant_type=None,
        max_players_in_team=1,
        min_players_in_team=None,
    )

    with pytest.raises(NotFound):
        _require_team_tournament(tournament)
