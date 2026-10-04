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
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus

from tests.unit.services.lan_tournament import test_ffa_single_survivor_correction as b2

# Register the reused fixture without shadowing the parametrized source match.
b2_source = b2.source

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


# fmt: off
@pytest.mark.parametrize(('bracket', 'others', 'expected'), [
    (WB, [_match(Bracket.GRAND_FINAL, 0)], True),
    (LB, [_match(Bracket.GRAND_FINAL, 0)], True),
    (None, [_match(Bracket.GRAND_FINAL, 0)], False),
    (WB, [_match(WB, 2)], True),
    (LB, [_match(LB, 2)], True),
    (None, [_match(None, 2)], True),
    (WB, [_match(LB, 1, 'ffa:WB:2')], True),
    (WB, [_match(LB, 1, 'ffa:WB:3')], False),
    (Bracket.GRAND_FINAL, [_match(Bracket.GRAND_FINAL, 2)], False),
])
# fmt: on
def test_cached_structural_consumption_is_query_free(bracket, others, expected):
    tournament = _tournament()
    tournament.tournament_status = TournamentStatus.ONGOING
    match = _match(bracket, 1)
    with patch.object(service, 'tournament_repository') as repository:
        assert service.ffa_round_consumed(match, tournament, matches=[match, *others]) is expected
        repository.assert_not_called()
        assert repository.mock_calls == []


# fmt: off
@pytest.mark.parametrize('boundary', [
    'eligible', 'partial', 'tie', 'multiple', 'zero', 'wrong-phase',
    'de', 'non-ffa', 'bracket', 'missing-cut', 'negative-cut', 'unreleased',
    'ongoing', 'cancelled', 'other-phase', 'single-lobby',
])
# fmt: on
def test_cached_b2_consumption_matches_uncached_semantics(b2_source, boundary):
    tournament, lobbies, entries, repository, ids = b2_source
    if boundary == 'partial':
        lobbies[1].confirmed_by = None
    elif boundary == 'tie':
        entries[0][1].points = entries[0][0].points
    elif boundary == 'multiple':
        repository.get_participants_for_tournament.return_value = [SimpleNamespace(id=cid) for cid in ids]
    elif boundary == 'zero':
        repository.get_participants_for_tournament.return_value = []
    elif boundary == 'wrong-phase':
        lobbies[0].phase = 2
    elif boundary == 'de':
        from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
        tournament.elimination_mode = EliminationMode.DOUBLE_ELIMINATION
    elif boundary == 'non-ffa':
        tournament.game_format = GameFormat.ONE_V_ONE
    elif boundary == 'bracket':
        lobbies[0].bracket = WB
    elif boundary == 'missing-cut':
        tournament.advancement_count = None
    elif boundary == 'negative-cut':
        tournament.advancement_count = -1
    elif boundary == 'unreleased':
        from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
        tournament.game_format = GameFormat.HIGHSCORE
        tournament.has_playoffs = True
        tournament.playoff_game_format = GameFormat.FREE_FOR_ALL
        tournament.playoff_elimination_mode = EliminationMode.SINGLE_ELIMINATION
        tournament.playoff_released_at = None
        for lobby in lobbies:
            lobby.phase = 2
    elif boundary in ('ongoing', 'cancelled'):
        tournament.tournament_status = (TournamentStatus.ONGOING if boundary == 'ongoing' else TournamentStatus.CANCELLED)
    elif boundary == 'other-phase':
        repository.get_matches_for_tournament.return_value = [
            *lobbies, SimpleNamespace(phase=2, bracket=None, round=9, seeding_target=None),
        ]
    elif boundary == 'single-lobby':
        repository.get_matches_for_tournament.return_value = lobbies[:1]
    # Capture all data before disabling repository access. Compare both source
    # lobbies, including the empty lobby and phase/release/mode refusals.
    matches = repository.get_matches_for_tournament.return_value
    contestants = {lobby.id: members for lobby, members in zip(lobbies, entries, strict=True)}
    active = {str(p.id) for p in repository.get_participants_for_tournament.return_value}
    expected = [service.ffa_round_consumed(lobby, tournament) for lobby in lobbies]
    repository.reset_mock()
    actual = [service.ffa_round_consumed(
        lobby, tournament, matches=matches, contestants_by_match=contestants,
        decisions={}, active_ids=active,
    ) for lobby in lobbies]
    assert actual == expected
    assert repository.mock_calls == []
