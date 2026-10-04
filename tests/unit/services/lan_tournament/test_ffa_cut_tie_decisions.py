"""Read-only report guards."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_qualification_service as service
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus


def test_non_ffa_tournament_has_no_ffa_decision_report(monkeypatch):
    repository = Mock()
    repository.find_tournament.return_value = SimpleNamespace(
        game_format=GameFormat.ONE_V_ONE, playoff_game_format=None, has_playoffs=False
    )
    repository.get_matches_for_tournament_ordered.return_value = [SimpleNamespace(round=1)]
    monkeypatch.setattr(service, 'tournament_repository', repository)
    assert service.get_ffa_decision_report('t') == service.FfaDecisionReport((), ())
    repository.get_matches_for_tournament_ordered.assert_not_called()


@pytest.mark.parametrize(('bracket', 'companion', 'expected_ties'), [
    (None, Bracket.GRAND_FINAL, 1),
    (Bracket.WINNERS, None, 1),
    (Bracket.WINNERS, Bracket.WINNERS, 0),
])
def test_report_uses_full_lobby_counts_without_grand_final_scope_aliases(
    monkeypatch, bracket, companion, expected_ties
):
    repository = Mock()
    tournament = SimpleNamespace(
        id='t', game_format=GameFormat.FREE_FOR_ALL, has_playoffs=False,
        advancement_count=1 if bracket is None else 2,
        tournament_status=TournamentStatus.ONGOING,
    )
    lobby = SimpleNamespace(
        id='m', tournament_id='t', bracket=bracket, round=0,
        group_order=0, confirmed_by='orga', phase=1, seeding_target=None,
    )
    matches = [lobby]
    if companion is not None:
        matches.append(SimpleNamespace(
            id='other', tournament_id='t', bracket=companion, round=0,
            group_order=0 if companion is Bracket.GRAND_FINAL else 1,
            confirmed_by=None, phase=1, seeding_target=None,
        ))
    entries = [SimpleNamespace(participant_id=str(i), team_id=None,
                               points=1, placement=i + 1) for i in range(2)]
    repository.find_tournament.return_value = tournament
    repository.get_matches_for_tournament_ordered.return_value = matches
    repository.get_contestants_for_matches.return_value = {
        match.id: entries if match is lobby else [] for match in matches
    }
    qualification_repository = Mock()
    qualification_repository.get_decisions_for_tournament.return_value = {}
    monkeypatch.setattr(service, 'tournament_repository', repository)
    monkeypatch.setattr(service, 'tournament_qualification_repository', qualification_repository)
    monkeypatch.setattr(service.tournament_match_service, 'active_contestant_ids', lambda tid: {'0', '1'})
    report = service.get_ffa_decision_report('t')
    assert len(report.ties) == expected_ties
    repository.get_contestants_for_matches.assert_called_once()
    repository.get_matches_for_round.assert_not_called()
    repository.get_contestants_for_match.assert_not_called()


@pytest.mark.parametrize('cut', [None, 0, -1])
def test_invalid_cut_has_no_ffa_decision_report(monkeypatch, cut):
    repository = Mock()
    repository.find_tournament.return_value = SimpleNamespace(
        game_format=GameFormat.FREE_FOR_ALL, playoff_game_format=None,
        has_playoffs=False, advancement_count=cut,
    )
    monkeypatch.setattr(service, 'tournament_repository', repository)
    assert service.get_ffa_decision_report('t') == service.FfaDecisionReport((), ())
    repository.get_matches_for_tournament_ordered.assert_not_called()
