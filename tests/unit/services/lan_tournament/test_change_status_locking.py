"""
tests.unit.services.lan_tournament.test_change_status_locking
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import ast
from datetime import datetime
import inspect
from types import SimpleNamespace
from unittest.mock import call, Mock, patch

import pytest

from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    OperationalClock,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_service'


def _create_tournament(status: TournamentStatus) -> Tournament:
    return Tournament(
        id=TournamentID(generate_uuid()),
        party_id=PartyID('test-party'),
        name='Test Tournament',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=datetime(2025, 6, 15, 14, 0, 0),
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=None,
        tournament_status=status,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )


@pytest.fixture
def repository():
    with (
        patch(f'{_S}.tournament_repository') as repository,
        patch(f'{_S}.signals'),
        patch(f'{_S}.create_log_entry'),
    ):
        # workspace-pv3b.24: `change_status` now writes the status
        # through this targeted, `Result`-returning setter instead of
        # the full-row `update_tournament`.
        repository.set_tournament_status_flush.return_value = Ok(None)
        yield repository


def _call_names(repository) -> list[str]:
    return [name for name, _, _ in repository.mock_calls]


def test_tournament_is_locked_before_it_is_read(repository):
    tournament = _create_tournament(TournamentStatus.ONGOING)
    repository.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    )

    assert result.is_ok()
    repository.lock_tournament_for_update.assert_called_once_with(tournament.id)
    names = _call_names(repository)
    assert names.index('lock_tournament_for_update') < names.index(
        'get_tournament'
    )


def test_invalid_transition_releases_the_lock(repository):
    tournament = _create_tournament(TournamentStatus.COMPLETED)
    repository.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    )

    assert result.is_err()
    repository.rollback_session.assert_called_once_with()
    repository.update_tournament.assert_not_called()


def test_refused_start_releases_the_lock(repository):
    tournament = _create_tournament(TournamentStatus.REGISTRATION_CLOSED)
    repository.get_tournament.return_value = tournament

    with patch(
        f'{_S}.tournament_match_service.validate_bracket_for_start',
        return_value=['no matches generated'],
    ):
        result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING
        )

    assert result.is_err()
    repository.rollback_session.assert_called_once_with()
    repository.update_tournament.assert_not_called()


# -- the operational clock edge --

_AT = datetime(2026, 10, 7, 18, 0, 0)
_CLOCK = OperationalClock(
    elapsed_us=7_000_000, running_since=_AT, activated_at=datetime(2026, 10, 7)
)


@pytest.fixture
def operational():
    with patch(f'{_S}.tournament_operational_service') as operational:
        operational.reconcile_due_matches_flush.return_value = Ok(None)
        yield operational


def _edge(clock=_CLOCK):
    return tournament_repository.StatusClockEdge(at=_AT, clock=clock)


def _sequence(repository, operational):
    calls = Mock()
    for mock, name in (
        (repository.lock_tournament_for_update, 'lock'),
        (repository.get_tournament, 'read'),
        (repository.set_tournament_status_flush, 'status'),
        (operational.reconcile_due_matches_flush, 'reconcile'),
        (repository.rollback_session, 'rollback'),
        (repository.commit_session, 'commit'),
    ):
        calls.attach_mock(mock, name)
    return calls


@pytest.mark.parametrize(
    ('old', 'new'),
    # fmt: off
    [
        (TournamentStatus.PAUSED, TournamentStatus.ONGOING),
        (TournamentStatus.COMPLETED, TournamentStatus.ONGOING),
        (TournamentStatus.ONGOING, TournamentStatus.COMPLETED),
        (TournamentStatus.PAUSED, TournamentStatus.CANCELLED),
    ],
    # fmt: on
)
def test_demand_is_reconciled_after_the_status_write_at_the_edge_time(
    repository, operational, old, new
):
    tournament = _create_tournament(old)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_winner.return_value = Ok(None)
    repository.set_tournament_status_flush.return_value = Ok(_edge())
    calls = _sequence(repository, operational)

    result = tournament_service.change_status(
        tournament.id, new, allow_completed_reopen=True
    )

    assert result.is_ok()
    assert calls.mock_calls == [
        call.lock(tournament.id),
        call.read(tournament.id, fresh=True),
        call.status(tournament.id, new),
        call.reconcile(tournament.id, occurred_at=_AT),
        call.commit(),
    ]


def test_a_pause_keeps_every_episode_as_it_is(repository, operational):
    tournament = _create_tournament(TournamentStatus.ONGOING)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Ok(_edge())

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED
    )

    assert result.is_ok()
    operational.reconcile_due_matches_flush.assert_not_called()
    repository.commit_session.assert_called_once_with()


def test_a_tournament_without_clock_history_is_not_reconciled(
    repository, operational
):
    tournament = _create_tournament(TournamentStatus.PAUSED)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()
    operational.reconcile_due_matches_flush.assert_not_called()


def test_a_failed_reconcile_rolls_back_and_commits_nothing(
    repository, operational
):
    tournament = _create_tournament(TournamentStatus.PAUSED)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Ok(_edge())
    operational.reconcile_due_matches_flush.return_value = Err('failed')

    with patch(f'{_S}.signals') as signals:
        result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING
        )

    assert result.is_err()
    assert result.unwrap_err() == 'failed'
    repository.rollback_session.assert_called_once_with()
    repository.commit_session.assert_not_called()
    signals.tournament_status_changed.send.assert_not_called()


def test_the_status_write_failing_skips_the_reconcile(repository, operational):
    tournament = _create_tournament(TournamentStatus.PAUSED)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Err('unknown')

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    operational.reconcile_due_matches_flush.assert_not_called()
    repository.rollback_session.assert_called_once_with()


def test_the_returned_tournament_carries_the_persisted_clock(
    repository, operational
):
    tournament = _create_tournament(TournamentStatus.PAUSED)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Ok(_edge())

    updated, _ = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()

    assert updated.tournament_status is TournamentStatus.ONGOING
    assert updated.operational_clock_elapsed_us == _CLOCK.elapsed_us
    assert updated.operational_clock_running_since == _CLOCK.running_since
    assert updated.operational_clock_activated_at == _CLOCK.activated_at


def test_the_returned_tournament_keeps_a_clock_without_history(
    repository, operational
):
    tournament = _create_tournament(TournamentStatus.PAUSED)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Ok(None)

    updated, _ = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()

    assert updated.operational_clock_elapsed_us == 0
    assert updated.operational_clock_running_since is None
    assert updated.operational_clock_activated_at is None


def test_pending_invitations_are_dispatched_once_after_the_commit(
    repository, operational
):
    tournament = _create_tournament(TournamentStatus.ONGOING)
    repository.get_tournament.return_value = tournament
    repository.set_tournament_status_flush.return_value = Ok(_edge())
    pending = (generate_uuid(), generate_uuid())
    with (
        patch(
            f'{_S}._reconcile_lifecycle_invitations_flush',
            return_value=Ok(pending),
        ),
        patch(
            'byceps.services.lan_tournament.tournament_readiness_service'
            '.dispatch_pending_invitations'
        ) as dispatch,
    ):
        calls = _sequence(repository, operational)
        calls.attach_mock(dispatch, 'dispatch')
        result = tournament_service.change_status(
            tournament.id, TournamentStatus.PAUSED
        )

    assert result.is_ok()
    assert [c for c in calls.mock_calls if c[0] in ('commit', 'dispatch')] == [
        call.commit(),
        call.dispatch(pending),
    ]


# -- the central status setter, without a database --


def _row(status, *, elapsed=0, running_since=None, activated_at=None):
    return SimpleNamespace(
        tournament_status=status,
        operational_clock_elapsed_us=elapsed,
        operational_clock_running_since=running_since,
        operational_clock_activated_at=activated_at,
    )


@pytest.fixture
def session():
    with patch.object(tournament_repository, 'db') as db:
        yield db.session


def test_the_setter_reads_the_row_from_the_database_not_the_cache(session):
    row = _row('PAUSED')
    session.get.return_value = row

    result = tournament_repository.set_tournament_status_flush(
        TournamentID(generate_uuid()),
        TournamentStatus.ONGOING,
        changed_at=_AT,
    )

    assert result.is_ok()
    ((args, kwargs),) = [(c.args, c.kwargs) for c in session.get.call_args_list]
    assert kwargs == {'populate_existing': True}
    assert row.tournament_status == 'ONGOING'
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_the_setter_samples_the_server_only_without_a_given_time(session):
    session.get.return_value = _row('REGISTRATION_CLOSED')
    tournament_id = TournamentID(generate_uuid())
    with patch.object(
        tournament_repository, 'get_operation_time', return_value=_AT
    ) as sample:
        given = tournament_repository.set_tournament_status_flush(
            tournament_id, TournamentStatus.ONGOING, changed_at=_AT
        )
        sample.assert_not_called()
        session.get.return_value = _row('REGISTRATION_CLOSED')
        sampled = tournament_repository.set_tournament_status_flush(
            tournament_id, TournamentStatus.ONGOING
        )
        sample.assert_called_once_with()

    assert given.unwrap().at == sampled.unwrap().at == _AT


def test_the_setter_reports_an_unknown_tournament_and_writes_nothing(session):
    session.get.return_value = None

    result = tournament_repository.set_tournament_status_flush(
        TournamentID(generate_uuid()), TournamentStatus.ONGOING
    )

    assert result.is_err()
    session.flush.assert_not_called()


# -- no status writer bypasses the clock edge --

_CLOCK_COLUMNS = {
    'tournament_status',
    'operational_clock_elapsed_us',
    'operational_clock_running_since',
    'operational_clock_activated_at',
}


def _repository_functions():
    tree = ast.parse(inspect.getsource(tournament_repository))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _writers_of(columns):
    """Name the functions that store a column after the row exists."""
    writers = set()
    for name, function in _repository_functions().items():
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and node.attr in columns
            ):
                writers.add(name)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'values'
                and any(keyword.arg in columns for keyword in node.keywords)
            ):
                writers.add(name)
    return writers


def test_only_the_status_setter_writes_the_status_and_the_clock():
    # A writer that assigns the status itself, or stores a clock column,
    # would skip the edge: the automatic completion and retraction paths
    # call the setter, never `change_status`. Only a new row is stored
    # as it is given.
    assert _writers_of(_CLOCK_COLUMNS) == {
        'create_tournament',
        'set_tournament_status_flush',
    }


def test_the_status_setter_is_flush_only():
    setter = _repository_functions()['set_tournament_status_flush']
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(setter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute | ast.Name)
    }
    assert {
        'commit',
        'rollback',
        'commit_session',
        'rollback_session',
    }.isdisjoint(called)
    assert 'flush' in called
