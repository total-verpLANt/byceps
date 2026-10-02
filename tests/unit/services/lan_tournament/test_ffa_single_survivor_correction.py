from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_match_service as service
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.util.uuid import generate_uuid7


@pytest.fixture
def source(monkeypatch):
    ids = [generate_uuid7() for _ in range(4)]
    tournament = SimpleNamespace(
        id=generate_uuid7(),
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.COMPLETED,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        has_playoffs=False,
        advancement_count=1,
    )
    lobbies = [
        SimpleNamespace(
            id=generate_uuid7(),
            tournament_id=tournament.id,
            phase=1,
            bracket=None,
            round=0,
            confirmed_by=generate_uuid7(),
            group_order=i,
            next_match_id=None,
        )
        for i in range(2)
    ]
    contestants = [
        [
            SimpleNamespace(
                participant_id=ids[2 * i + j],
                team_id=None,
                placement=j + 1,
                points=5 if j == 0 else 3,
            )
            for j in range(2)
        ]
        for i in range(2)
    ]
    repo = Mock()
    repo.get_matches_for_tournament.return_value = lobbies
    repo.get_matches_for_round.return_value = lobbies
    repo.get_contestants_for_match.side_effect = lambda mid: contestants[
        next(i for i, lobby in enumerate(lobbies) if lobby.id == mid)
    ]
    repo.get_participants_for_tournament.return_value = [
        SimpleNamespace(id=cid) for cid in ids[:2]
    ]
    repo.get_teams_for_tournament.return_value = []
    monkeypatch.setattr(service, 'tournament_repository', repo)
    qualification_repo = Mock()
    qualification_repo.get_decisions_for_tournament.return_value = {}
    monkeypatch.setattr(
        service, 'tournament_qualification_repository', qualification_repo
    )
    return tournament, lobbies, contestants, repo, ids


@pytest.mark.parametrize('empty', [False, True])
def test_both_source_lobbies_decide_single_survivor_completion(source, empty):
    tournament, lobbies, _, repo, ids = source
    match = lobbies[int(empty)]
    plan = service._single_survivor_source_plan(match, tournament)
    assert plan is not None
    assert plan.survivors == (str(ids[0]),)
    assert plan.round_number == 1
    assert service.retraction_reverts_completion(match, tournament)
    repo.commit_session.assert_not_called()
    repo.set_tournament_winner.assert_not_called()


# fmt: off
@pytest.mark.parametrize('boundary', [
    'partial', 'tie', 'multiple', 'zero', 'earlier', 'foreign-match',
    'wrong-phase', 'de', 'non-ffa', 'bracket', 'missing-cut', 'negative-cut',
    'unreleased',
])
# fmt: on
def test_only_fully_confirmed_eligible_se_source_is_recognized(source, boundary):
    tournament, lobbies, contestants, repo, ids = source
    match = lobbies[0]
    if boundary == 'partial':
        lobbies[1].confirmed_by = None
    elif boundary == 'tie':
        contestants[0][1].points = 5
    elif boundary == 'multiple':
        repo.get_participants_for_tournament.return_value = [
            SimpleNamespace(id=cid) for cid in ids
        ]
    elif boundary == 'zero':
        repo.get_participants_for_tournament.return_value = []
    elif boundary == 'earlier':
        repo.get_matches_for_tournament.return_value = [
            *lobbies, SimpleNamespace(id=generate_uuid7(), phase=1, bracket=None, round=1)
        ]
    elif boundary == 'foreign-match':
        match = SimpleNamespace(**{**vars(match), 'id': generate_uuid7()})
    elif boundary == 'wrong-phase':
        match.phase = 2
    elif boundary == 'de':
        tournament.elimination_mode = EliminationMode.DOUBLE_ELIMINATION
    elif boundary == 'non-ffa':
        tournament.game_format = GameFormat.ONE_V_ONE
    elif boundary == 'bracket':
        match.bracket = Bracket.WINNERS
    elif boundary == 'missing-cut':
        tournament.advancement_count = None
    elif boundary == 'negative-cut':
        tournament.advancement_count = -1
    elif boundary == 'unreleased':
        tournament.game_format = GameFormat.HIGHSCORE
        tournament.has_playoffs = True
        tournament.playoff_game_format = GameFormat.FREE_FOR_ALL
        tournament.playoff_elimination_mode = EliminationMode.SINGLE_ELIMINATION
        tournament.playoff_released_at = None
        for lobby in lobbies:
            lobby.phase = 2
    assert service._single_survivor_source_plan(match, tournament) is None
    repo.commit_session.assert_not_called()


def test_other_phase_rounds_do_not_contaminate_source_confirmation(source):
    tournament, lobbies, _, repo, _ = source
    repo.get_matches_for_tournament.return_value = [
        *lobbies, SimpleNamespace(id=generate_uuid7(), phase=2, bracket=None, round=9)
    ]
    assert service._single_survivor_source_plan(lobbies[0], tournament) is not None


@pytest.mark.parametrize('round_number', [0, 9])
def test_bracket_matches_do_not_contaminate_se_source(source, round_number):
    tournament, lobbies, _, repo, ids = source
    repo.get_matches_for_tournament.return_value = [
        *lobbies,
        SimpleNamespace(
            id=generate_uuid7(), phase=1, bracket=Bracket.WINNERS,
            round=round_number, confirmed_by=None,
        ),
    ]
    plan = service._single_survivor_source_plan(lobbies[0], tournament)
    assert plan is not None
    assert plan.survivors == (str(ids[0]),)


@pytest.mark.parametrize('status', [TournamentStatus.ONGOING, TournamentStatus.CANCELLED])
def test_retraction_never_reopens_other_statuses(source, status):
    tournament, lobbies, _, _, _ = source
    tournament.tournament_status = status
    assert not service.retraction_reverts_completion(lobbies[0], tournament)


def test_normal_se_final_and_de_grand_final_keep_deciding_behavior(source):
    tournament, lobbies, _, repo, _ = source
    repo.get_matches_for_round.return_value = [lobbies[0]]
    assert service.is_deciding_match(lobbies[0], tournament)
    tournament.elimination_mode = EliminationMode.DOUBLE_ELIMINATION
    lobbies[0].bracket = Bracket.GRAND_FINAL
    assert service.is_deciding_match(lobbies[0], tournament)
    lobbies[0].bracket = Bracket.WINNERS
    assert not service.is_deciding_match(lobbies[0], tournament)


def test_unrelated_highscore_phase_never_retracts_playoff_completion(source):
    tournament, lobbies, _, _, _ = source
    tournament.has_playoffs = True
    tournament.playoff_game_format = GameFormat.FREE_FOR_ALL
    tournament.playoff_elimination_mode = EliminationMode.SINGLE_ELIMINATION
    assert not service.retraction_reverts_completion(lobbies[0], tournament)
