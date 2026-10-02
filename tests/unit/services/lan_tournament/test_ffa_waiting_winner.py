"""Keep waiting WB rosters separate from ordinary WB and LB-bye rosters."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_match_service as service
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.uuid import generate_uuid7


# fmt: off
@pytest.mark.parametrize(('pool', 'survivors', 'lb_pool', 'waiting', 'single', 'draft'), [
    (Bracket.WINNERS, ('w',), ('a', 'b'), True, False, ('a', 'b')),
    (Bracket.WINNERS, ('w', 'x'), ('a',), False, False, ('w', 'x')),
    (Bracket.LOSERS, ('a',), (), False, False, ('a',)),
    (None, ('a',), (), False, True, ('a',)),
    (None, ('a', 'b'), (), False, False, ('a', 'b')),
])
# fmt: on
def test_only_one_wb_survivor_seeds_the_merged_lb_roster(pool, survivors, lb_pool, waiting, single, draft):
    plan = service.FfaAdvancePlan(pool, 2, survivors, {}, False, lb_pool, 1)
    assert service.is_waiting_winners_round(plan) is waiting
    assert service.is_single_survivor(plan) is single
    assert service.ffa_draft_survivors(plan) == draft
    assert all(bye.pool is Bracket.LOSERS for bye in service.ffa_lobby_byes(plan))


def test_removing_every_latest_wb_drop_does_not_fake_an_already_waiting_winner(monkeypatch):
    ids = [str(generate_uuid7()) for _ in range(4)]
    tournament = SimpleNamespace(
        id=generate_uuid7(), game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        tournament_status=TournamentStatus.ONGOING,
        advancement_count=1, group_size_max=4, has_playoffs=False,
    )
    wb = SimpleNamespace(id=generate_uuid7(), bracket=Bracket.WINNERS, round=1,
                         tournament_id=tournament.id, group_order=0,
                         confirmed_by=generate_uuid7(), seeding_target=None)
    lb = [SimpleNamespace(id=generate_uuid7(), bracket=Bracket.LOSERS, round=0,
                          tournament_id=tournament.id, group_order=i,
                          confirmed_by=generate_uuid7(), seeding_target=None)
          for i in range(3)]
    all_matches = [wb, *lb]
    contestants = {
        match.id: [SimpleNamespace(participant_id=cid, team_id=None,
                                   placement=1, points=5)]
        for match, cid in zip(all_matches, ids, strict=True)
    }
    repo = Mock()
    repo.get_matches_for_tournament_ordered.return_value = all_matches
    repo.get_matches_for_round.side_effect = lambda tid, rnd, bracket=None: [
        m for m in all_matches if m.round == rnd and m.bracket is bracket
    ]
    repo.get_contestants_for_match.side_effect = contestants.__getitem__
    repo.get_participants_for_tournament.return_value = [SimpleNamespace(id=i) for i in ids]
    repo.get_teams_for_tournament.return_value = []
    monkeypatch.setattr(service, 'tournament_repository', repo)
    monkeypatch.setattr(service, 'ffa_decisions', lambda tid: {})
    result = service.plan_ffa_advance(tournament, Bracket.WINNERS)
    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap().grand_final_eligible
