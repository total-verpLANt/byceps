"""Verify targeted winner writes and transaction boundaries of B2 completion."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_match_service as service
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Err, Ok
from byceps.util.uuid import generate_uuid7


@pytest.fixture
def completion(monkeypatch):
    cid = str(generate_uuid7())
    tournament = SimpleNamespace(
        id=generate_uuid7(),
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.ONGOING,
        game_format=GameFormat.FREE_FOR_ALL,
        has_playoffs=False,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    plan = service.FfaAdvancePlan(None, 2, (cid,), {}, False)
    repo = Mock()
    repo.get_participants_for_tournament.return_value = [
        SimpleNamespace(id=cid)
    ]
    repo.get_teams_for_tournament.return_value = [SimpleNamespace(id=cid)]
    repo.set_tournament_winner.return_value = Ok(None)
    repo.set_tournament_status_flush.return_value = Ok(None)
    audit = Mock()
    monkeypatch.setattr(service, 'tournament_repository', repo)
    monkeypatch.setattr(service, 'create_log_entry', audit)
    return tournament, plan, repo, audit


@pytest.mark.parametrize('team', [False, True])
def test_completion_targets_the_correct_winner_and_stages_audit(
    completion, team
):
    tournament, plan, repo, audit = completion
    if team:
        tournament.contestant_type = ContestantType.TEAM
    result = service.complete_ffa_single_survivor(tournament, plan, None)
    assert result.is_ok(), result.unwrap_err()
    event = result.unwrap()
    assert (
        str(event.winner_team_id if team else event.winner_participant_id)
        == plan.survivors[0]
    )
    assert (
        event.winner_participant_id if team else event.winner_team_id
    ) is None
    repo.set_tournament_winner.assert_called_once_with(
        tournament.id,
        winner_team_id=event.winner_team_id,
        winner_participant_id=event.winner_participant_id,
    )
    repo.set_tournament_status_flush.assert_called_once_with(
        tournament.id, TournamentStatus.COMPLETED
    )
    audit.assert_called_once_with(
        'bracket-single-survivor',
        tournament.id,
        None,
        data={'pool': 'SE', 'round': 2, 'contestant': plan.survivors[0]},
        commit=False,
    )
    repo.commit_session.assert_not_called()


@pytest.mark.parametrize(
    'writer', ['set_tournament_winner', 'set_tournament_status_flush']
)
def test_writer_failure_is_returned_without_audit_or_commit(completion, writer):
    tournament, plan, repo, audit = completion
    getattr(repo, writer).return_value = Err('write_failed')
    result = service.complete_ffa_single_survivor(tournament, plan, None)
    assert result.is_err() and result.unwrap_err() == 'write_failed'
    audit.assert_not_called()
    repo.commit_session.assert_not_called()
    if writer == 'set_tournament_winner':
        repo.set_tournament_status_flush.assert_not_called()


@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    ],
)
def test_completion_refuses_nonrunning_status(completion, status):
    tournament, plan, repo, audit = completion
    tournament.tournament_status = status
    assert service.complete_ffa_single_survivor(tournament, plan, None).is_err()
    repo.set_tournament_winner.assert_not_called()
    audit.assert_not_called()


def test_completion_refuses_a_removed_or_foreign_survivor(completion):
    tournament, plan, repo, audit = completion
    repo.get_participants_for_tournament.return_value = []
    repo.get_teams_for_tournament.return_value = []
    result = service.complete_ffa_single_survivor(tournament, plan, None)
    assert (
        result.is_err() and result.unwrap_err() == service.LAYOUT_ROSTER_ERROR
    )
    repo.set_tournament_winner.assert_not_called()
    audit.assert_not_called()


@pytest.mark.parametrize('pool', [Bracket.WINNERS, Bracket.LOSERS])
def test_de_lone_pools_never_complete(completion, pool):
    tournament, plan, repo, audit = completion
    tournament.elimination_mode = EliminationMode.DOUBLE_ELIMINATION
    plan = service.FfaAdvancePlan(pool, 2, plan.survivors, {}, False)
    assert service.complete_ffa_single_survivor(tournament, plan, None).is_err()
    repo.set_tournament_winner.assert_not_called()
    audit.assert_not_called()


@pytest.mark.parametrize(
    'status',
    [
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    ],
)
def test_planning_refuses_nonrunning_status_before_reading_matches(
    completion, status
):
    tournament, _plan, repo, _audit = completion
    tournament.tournament_status = status
    result = service.plan_ffa_advance(tournament, None)
    assert result.is_err()
    repo.get_matches_for_tournament_ordered.assert_not_called()
