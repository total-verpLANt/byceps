"""
tests.unit.services.lan_tournament.test_ffa_round_consumed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import tournament_match_service as service
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.game_format import GameFormat

WB = Bracket.WINNERS
LB = Bracket.LOSERS


def _match(bracket, round, seeding_target=None):
    return SimpleNamespace(
        bracket=bracket, round=round, seeding_target=seeding_target
    )


def _tournament():
    return SimpleNamespace(
        id='t',
        game_format=GameFormat.FREE_FOR_ALL,
        playoff_game_format=None,
        has_playoffs=False,
    )


# fmt: off
@pytest.mark.parametrize(
    ('source', 'others', 'expected'),
    [
        # the merged losers round of a waiting advance carries the next target
        (_match(WB, 1), [_match(LB, 1, 'ffa:WB:2')], True),
        # a tag for another winners round does not consume this one
        (_match(WB, 1), [_match(LB, 1, 'ffa:WB:3')], False),
        # the tag on a winners match is a normal next round, not a merge
        (_match(WB, 1), [_match(WB, 1, 'ffa:WB:2')], False),
        # an untagged losers round does not consume the winners round
        (_match(WB, 1), [_match(LB, 1, 'ffa:LB:1')], False),
        # a losers source is consumed by a later losers round
        (_match(LB, 0), [_match(LB, 1, 'ffa:LB:1')], True),
        (_match(LB, 1), [_match(LB, 1, 'ffa:LB:1')], False),
    ],
)
# fmt: on
def test_merged_losers_round_consumes_its_winners_source(
    source, others, expected
):
    tournament = _tournament()
    with patch.object(service, 'tournament_repository') as repo:
        repo.get_matches_for_tournament.return_value = [source, *others]
        assert service.ffa_round_already_advanced(source, tournament) is expected
