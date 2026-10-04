"""Effective cuts for lone small winners pools and historical removals."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_match_service as service
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.game_format import GameFormat


def entrants(count):
    return [
        SimpleNamespace(
            participant_id=str(i),
            team_id=None,
            points=count - i,
            placement=i + 1,
        )
        for i in range(count)
    ]


def test_ffa_lobby_cut_takes_the_lobby_count(monkeypatch):
    repository = Mock()
    monkeypatch.setattr(service, 'tournament_repository', repository)
    match = SimpleNamespace(tournament_id='t', bracket=Bracket.WINNERS, round=3)
    assert service.ffa_lobby_cut(
        match, entrants(2), 2, {'0', '1'}, lobbies_in_round=1
    ) == 1
    repository.get_matches_for_round.assert_not_called()


# fmt: off
@pytest.mark.parametrize(('bracket', 'active', 'cut', 'lobbies', 'expected'), [
    (Bracket.WINNERS, 2, 2, 1, 1),
    (Bracket.WINNERS, 3, 2, 1, 2),
    (Bracket.WINNERS, 2, 2, 2, 2),
    (Bracket.LOSERS, 2, 2, 1, 2),
    (None, 2, 2, 1, 2),
    (Bracket.WINNERS, 1, 2, 1, 1),
])
# fmt: on
def test_ffa_lobby_cut(monkeypatch, bracket, active, cut, lobbies, expected):
    repository = Mock()
    repository.get_matches_for_round.return_value = [object()] * lobbies
    monkeypatch.setattr(service, 'tournament_repository', repository)
    monkeypatch.setattr(service, 'active_contestant_ids', lambda tid: {str(i) for i in range(active)})
    match = SimpleNamespace(tournament_id='t', bracket=bracket, round=3)
    assert service.ffa_lobby_cut(match, entrants(active + 1), cut) == expected


@pytest.mark.parametrize('count', [2, 3])
def test_removed_in_race_uses_the_historical_lobby_cut(monkeypatch, count):
    repository = Mock()
    monkeypatch.setattr(service, 'tournament_repository', repository)
    tournament = SimpleNamespace(id='t', game_format=GameFormat.FREE_FOR_ALL,
                                 contestant_type=None, advancement_count=2)
    match = SimpleNamespace(id='m', tournament_id='t', bracket=Bracket.WINNERS,
                            round=3, group_order=0, phase=1, confirmed_by='orga')
    entries = entrants(count)
    removed = str(count - 1)
    repository.get_contestant_ids_removed_since_phase_start.return_value = {removed}
    repository.get_matches_for_tournament.return_value = [match]
    repository.get_matches_for_round.return_value = [match]
    repository.get_contestants_for_matches.return_value = {'m': entries}
    monkeypatch.setattr(service, 'ffa_decisions', lambda tid: {})
    monkeypatch.setattr(service, 'active_contestant_ids', lambda tid: {str(i) for i in range(count - 1)})
    helper = getattr(service, 'ffa_lobby_cut', None)
    spy = Mock(wraps=helper)
    monkeypatch.setattr(service, 'ffa_lobby_cut', spy, raising=False)
    ranking = Mock(wraps=service.rank_ffa_lobby)
    monkeypatch.setattr(service, 'rank_ffa_lobby', ranking)

    result = service.removed_in_race(tournament)

    assert removed not in result.winners
    assert removed in result.racing
    historical_ids = {str(i) for i in range(count)}
    spy.assert_called_once_with(match, entries, 2, active_ids=historical_ids)
    assert ranking.call_args.kwargs['active_ids'] == historical_ids
    assert ranking.call_args.args[2] == (1 if count == 2 else 2)
