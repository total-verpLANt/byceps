"""Transaction boundaries for reopening highscore qualification."""

from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_qualification_service,
    tournament_score_service as service,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


@pytest.fixture
def boundary(monkeypatch):
    repository = Mock()
    repository.get_tournament.return_value = SimpleNamespace(
        game_format=GameFormat.HIGHSCORE,
        has_playoffs=True,
        playoff_released_at=None,
        leaderboard_closed_at=datetime.now(UTC),
        tournament_status=TournamentStatus.ONGOING,
    )
    log = Mock()
    auto_release = Mock()
    monkeypatch.setattr(service, 'tournament_repository', repository)
    monkeypatch.setattr(service, 'tournament_log_service', log)
    monkeypatch.setattr(
        tournament_qualification_service, 'try_auto_release', auto_release
    )
    return repository, log, auto_release


# fmt: off
@pytest.mark.parametrize('stage', [
    'lock_tournament_for_update',
    'get_tournament',
    'set_leaderboard_closed',
    'create_log_entry',
    'commit_session',
])
# fmt: on
def test_reopen_rolls_back_and_reraises_operational_failure(boundary, stage):
    repository, log, auto_release = boundary
    target = log if stage == 'create_log_entry' else repository
    failure = RuntimeError(f'injected {stage}')
    getattr(target, stage).side_effect = failure

    with pytest.raises(RuntimeError) as caught:
        service.reopen_leaderboard(
            'tournament', reason='Correction', initiator_id='initiator'
        )

    assert caught.value is failure
    repository.rollback_session.assert_called_once_with()
    auto_release.assert_not_called()


def test_reopen_happy_path_commits_once_without_auto_release(boundary):
    repository, log, auto_release = boundary

    result = service.reopen_leaderboard(
        'tournament', reason='  Correction  ', initiator_id='initiator'
    )

    assert result.is_ok()
    repository.lock_tournament_for_update.assert_called_once_with('tournament')
    repository.get_tournament.assert_called_once_with('tournament')
    repository.set_leaderboard_closed.assert_called_once_with('tournament', None)
    log.create_log_entry.assert_called_once_with(
        'qualification-leaderboard-reopened',
        'tournament',
        'initiator',
        data={'reason': 'Correction'},
        commit=False,
    )
    repository.commit_session.assert_called_once_with()
    repository.rollback_session.assert_not_called()
    auto_release.assert_not_called()
