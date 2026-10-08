import ast
from datetime import datetime, UTC
import inspect
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as service,
    tournament_repository,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())
CONTESTANT_ROW_ID = TournamentMatchToContestantID(generate_uuid())
PARTICIPANT_ID = TournamentParticipantID(generate_uuid())
USER_ID = UserID(generate_uuid())

MAX_SCORE = service.MAX_MATCH_SCORE
CONFIRMED_ERROR = 'Cannot modify scores of a confirmed match.'
NOT_FOUND = f'Contestant "{PARTICIPANT_ID}" not found in match "{MATCH_ID}"'

# Every repository call, in order, of an accepted score.
ACCEPTED_CALLS = [
    call.find_match(MATCH_ID),
    call.lock_tournament_for_update(TOURNAMENT_ID),
    call.lock_matches_for_update([MATCH_ID]),
    call.find_match_fresh(MATCH_ID),
    call.get_tournament(TOURNAMENT_ID, fresh=True),
    call.find_contestant_for_match(MATCH_ID, participant_id=PARTICIPANT_ID),
    call.update_contestant_score(CONTESTANT_ROW_ID, 7, commit=False),
    call.commit_session(),
]


def _match(*, confirmed_by=None, phase=1) -> TournamentMatch:
    return TournamentMatch(
        id=MATCH_ID,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=1,
        next_match_id=None,
        confirmed_by=confirmed_by,
        created_at=NOW,
        phase=phase,
    )


def _contestant(*, participant_id=PARTICIPANT_ID, team_id=None):
    return TournamentMatchToContestant(
        id=CONTESTANT_ROW_ID,
        tournament_match_id=MATCH_ID,
        team_id=team_id,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )


def _tournament(*, released=False):
    return SimpleNamespace(
        has_playoffs=released,
        playoff_released_at=NOW if released else None,
    )


@pytest.fixture
def repo():
    """Stand in for the repository, with an unconfirmed match to score."""
    with patch.object(service, 'tournament_repository') as mock:
        match = _match()
        mock.find_match.return_value = match
        mock.find_match_fresh.return_value = match
        mock.get_tournament.return_value = _tournament()
        mock.find_contestant_for_match.return_value = _contestant()
        yield mock


# -------------------------------------------------------------------- #
# legacy score: tournament first, flush only, one commit


def test_legacy_score_takes_tournament_first_and_commits_once(repo):
    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.is_ok()
    # Tournament row, then the match row, then the fresh read, the write
    # without its own commit, and the owner's single commit last.
    assert repo.method_calls == ACCEPTED_CALLS
    assert repo.commit_session.call_count == 1
    repo.rollback_session.assert_not_called()


def test_legacy_score_for_a_team_looks_the_team_up_second(repo):
    team_id = generate_uuid()
    repo.find_contestant_for_match.side_effect = [
        None,
        _contestant(participant_id=None, team_id=team_id),
    ]

    result = service.set_score(MATCH_ID, team_id, 7)

    assert result.is_ok()
    assert repo.find_contestant_for_match.call_args_list == [
        call(MATCH_ID, participant_id=team_id),
        call(MATCH_ID, team_id=team_id),
    ]
    repo.update_contestant_score.assert_called_once_with(
        CONTESTANT_ROW_ID, 7, commit=False
    )
    assert repo.commit_session.call_count == 1


def test_legacy_score_takes_the_lock_even_without_playoffs(repo):
    repo.get_tournament.return_value = _tournament(released=False)

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.is_ok()
    repo.lock_tournament_for_update.assert_called_once_with(TOURNAMENT_ID)


def test_legacy_score_never_writes_before_both_locks(repo):
    service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    names = [name for name, _, _ in repo.method_calls]
    assert names.index('lock_tournament_for_update') == 1
    assert names.index('lock_matches_for_update') == 2
    assert names.index('update_contestant_score') > names.index(
        'find_match_fresh'
    )


# fmt: off
@pytest.mark.parametrize('score', [0, 1, MAX_SCORE])
# fmt: on
def test_legacy_score_accepts_zero_and_the_ceiling(repo, score):
    result = service.set_score(MATCH_ID, PARTICIPANT_ID, score)

    assert result.is_ok()
    repo.update_contestant_score.assert_called_once_with(
        CONTESTANT_ROW_ID, score, commit=False
    )
    assert repo.commit_session.call_count == 1


# fmt: off
@pytest.mark.parametrize(
    'score, error',
    [
        pytest.param(-1, 'Score cannot be negative.', id='negative'),
        pytest.param(
            MAX_SCORE + 1, service.MAX_MATCH_SCORE_ERROR, id='over the ceiling'
        ),
        pytest.param(
            2**63, service.MAX_MATCH_SCORE_ERROR, id='far over the ceiling'
        ),
    ],
)
# fmt: on
def test_legacy_score_rejects_confirmed_and_out_of_range(repo, score, error):
    result = service.set_score(MATCH_ID, PARTICIPANT_ID, score)

    assert result.is_err()
    assert result.unwrap_err() == error
    # A value out of range is refused before any database access.
    assert repo.method_calls == []


def test_legacy_score_rejects_a_confirmed_match_and_releases_the_lock(repo):
    confirmed = _match(confirmed_by=USER_ID)
    repo.find_match_fresh.return_value = confirmed

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.is_err()
    assert result.unwrap_err() == CONFIRMED_ERROR
    repo.update_contestant_score.assert_not_called()
    repo.commit_session.assert_not_called()
    repo.rollback_session.assert_called_once_with()


def test_legacy_score_judges_the_fresh_match_not_the_first_read(repo):
    # Confirmed while this writer waited for the tournament lock.
    repo.find_match.return_value = _match(confirmed_by=None)
    repo.find_match_fresh.return_value = _match(confirmed_by=USER_ID)

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.unwrap_err() == CONFIRMED_ERROR
    repo.update_contestant_score.assert_not_called()


def test_legacy_score_refuses_a_released_phase_one_match(repo):
    repo.get_tournament.return_value = _tournament(released=True)

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.unwrap_err() == service.PHASE1_LOCKED_ERROR
    repo.update_contestant_score.assert_not_called()
    repo.commit_session.assert_not_called()
    repo.rollback_session.assert_called_once_with()


def test_legacy_score_for_an_unknown_contestant_rolls_back(repo):
    repo.find_contestant_for_match.return_value = None

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.unwrap_err() == NOT_FOUND
    repo.update_contestant_score.assert_not_called()
    repo.commit_session.assert_not_called()
    repo.rollback_session.assert_called_once_with()


def test_legacy_score_for_a_match_deleted_while_waiting_rolls_back(repo):
    repo.find_match_fresh.return_value = None

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.unwrap_err() == NOT_FOUND
    repo.update_contestant_score.assert_not_called()
    repo.rollback_session.assert_called_once_with()


def test_legacy_score_for_an_unknown_match_takes_no_lock(repo):
    repo.find_match.return_value = None

    result = service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    assert result.unwrap_err() == NOT_FOUND
    assert repo.method_calls == [call.find_match(MATCH_ID)]


# fmt: off
@pytest.mark.parametrize(
    'failure', ['update_contestant_score', 'commit_session']
)
# fmt: on
def test_legacy_score_failure_rolls_back_and_raises(repo, failure):
    getattr(repo, failure).side_effect = RuntimeError('storage unavailable')

    with pytest.raises(RuntimeError, match='storage unavailable'):
        service.set_score(MATCH_ID, PARTICIPANT_ID, 7)

    repo.rollback_session.assert_called_once_with()
    if failure == 'update_contestant_score':
        repo.commit_session.assert_not_called()


# -------------------------------------------------------------------- #
# the repository writer hands the commit to its caller on request


@pytest.fixture
def session():
    row = SimpleNamespace(score=1, tournament_match_id=MATCH_ID)
    with (
        patch.object(tournament_repository, 'db') as db,
        patch.object(
            tournament_repository, '_touch_changed_matches_flush'
        ) as touch,
    ):
        db.session.get.return_value = row
        db.session.touch = touch
        yield db.session


def test_legacy_score_writer_commits_unless_the_caller_owns_the_commit(
    session,
):
    tournament_repository.update_contestant_score(CONTESTANT_ROW_ID, 2)

    session.commit.assert_called_once_with()
    session.touch.assert_called_once_with([MATCH_ID], None)


def test_legacy_score_writer_leaves_the_commit_to_the_caller(session):
    tournament_repository.update_contestant_score(
        CONTESTANT_ROW_ID, 2, commit=False
    )

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.flush.assert_called_once_with()
    session.touch.assert_called_once_with([MATCH_ID], None)


def test_legacy_score_writer_stays_flush_only_for_an_unchanged_score(session):
    tournament_repository.update_contestant_score(
        CONTESTANT_ROW_ID, 1, commit=False
    )

    session.commit.assert_not_called()
    session.touch.assert_not_called()


# -------------------------------------------------------------------- #
# source contracts


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(service))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(function: ast.FunctionDef) -> list[ast.Call]:
    return sorted(
        (node for node in ast.walk(function) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )


def _named(function: ast.FunctionDef, name: str) -> list[ast.Call]:
    return [
        node
        for node in _calls(function)
        if ast.unparse(node.func).rpartition('.')[2] == name
    ]


def test_legacy_score_has_one_commit_after_its_flush_only_write():
    function = _function('set_score')

    [write] = _named(function, 'update_contestant_score')
    [commit] = _named(function, 'commit_session')

    assert {kw.arg: ast.literal_eval(kw.value) for kw in write.keywords} == {
        'commit': False
    }
    assert write.lineno < commit.lineno
    [lock] = _named(function, 'lock_tournament_for_update')
    assert lock.lineno < write.lineno


def test_match_delete_composes_only_flush_helpers_and_commits_once():
    function = _function('delete_match')
    called = {ast.unparse(node.func) for node in _calls(function)}

    assert not called & {
        'tournament_repository.delete_match',
        'tournament_repository.delete_comments_for_match',
        'tournament_repository.delete_contestants_for_match',
    }
    assert {
        'tournament_repository.delete_comments_for_match_flush',
        '_delete_contestants_for_match_flush',
        'tournament_repository.delete_match_flush',
    } <= called
    [commit] = _named(function, 'commit_session')
    [send] = _named(function, 'send')
    assert commit.lineno < send.lineno
    for step in _named(function, 'delete_match_flush'):
        assert step.lineno < commit.lineno


# -------------------------------------------------------------------- #
# match deletion: one operation


def _repository_for_deletion(repo):
    match = _match()
    repo.get_match.return_value = match
    repo.get_match_for_update.return_value = match
    return repo


# fmt: off
STEPS = [
    'delete_comments_for_match_flush',
    'delete_contestants_for_match_flush',
    'delete_match_flush',
    'commit_session',
]
# fmt: on


@pytest.fixture
def deletion():
    with (
        patch.object(service, 'tournament_repository') as repo,
        patch('byceps.services.lan_tournament.signals.match_deleted') as signal,
    ):
        yield _repository_for_deletion(repo), signal


def test_match_delete_commits_once_after_every_flush_and_before_the_signal(
    deletion,
):
    repo, signal = deletion
    order = []
    repo.commit_session.side_effect = lambda: order.append('commit')
    signal.send.side_effect = lambda *args, **kwargs: order.append('signal')

    service.delete_match(MATCH_ID)

    names = [name for name, _, _ in repo.method_calls]
    assert names[:3] == [
        'get_match',
        'lock_tournament_for_update',
        'get_match_for_update',
    ]
    assert names[-1] == 'commit_session'
    assert names.count('commit_session') == 1
    assert order == ['commit', 'signal']
    repo.rollback_session.assert_not_called()


@pytest.mark.parametrize('step', STEPS)
def test_match_delete_cleanup_rolls_back_as_one_operation(deletion, step):
    repo, signal = deletion
    getattr(repo, step).side_effect = RuntimeError('storage unavailable')

    with pytest.raises(RuntimeError, match='storage unavailable'):
        service.delete_match(MATCH_ID)

    # One rollback, no event, and no commit before the final step: comments,
    # contestants, the match and their timing stamp stand or fall together.
    repo.rollback_session.assert_called_once_with()
    signal.send.assert_not_called()
    names = [name for name, _, _ in repo.method_calls]
    assert names.index('rollback_session') > names.index(step)
    if step == 'commit_session':
        assert names.count('commit_session') == 1
    else:
        assert 'commit_session' not in names


def test_match_delete_of_an_unknown_match_touches_nothing(deletion):
    repo, signal = deletion
    repo.get_match.side_effect = ValueError('unknown')

    with pytest.raises(ValueError):
        service.delete_match(MATCH_ID)

    assert repo.method_calls == [call.get_match(MATCH_ID)]
    signal.send.assert_not_called()
