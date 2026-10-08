import ast
from dataclasses import fields, replace
from datetime import datetime
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import False_

from byceps.services.lan_tournament import (
    tournament_dashboard_coordination_service as coordination,
    tournament_dashboard_repository as dashboard_repository,
    tournament_dashboard_service as dashboard_service,
    tournament_match_service as match_service,
    tournament_operational_service as operational,
    tournament_readiness_service as service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    derive_match_readiness,
    MatchPairing,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchEscalationAcknowledgement,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardRow,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Ok


# Far from the wall clock the readiness service reads, so a stamp that took
# the Ready time instead of the operation time cannot equal it by accident.
OPERATION_TIME = datetime(2031, 9, 10, 18, 0, 0)
BASE = datetime(2026, 10, 8, 12, 0, 0)

LOCKS = ('get_tournament_for_update', 'get_match_for_update')
READY_WRITERS = frozenset(
    {
        'set_side_ready_flush',
        'clear_side_ready_flush',
        'set_side_invitation_hold_flush',
        'set_readiness_revision_flush',
    }
)
# The writers of the timing layer. A Ready change reaches none of them: it
# moves the last change of its match and nothing that starts or ends a wait.
TIMING_WRITERS = (
    'touch_matches_last_changed_flush',
    'open_due_episode_flush',
    'close_due_episodes_flush',
    'retire_dashboard_matches_flush',
    'advance_episode_ack_revision_flush',
    'create_escalation_ack_flush',
    'set_occupied_since_if_unset_flush',
    'set_ffa_lobby_occupied_since_if_unset_flush',
    'set_tournament_status_flush',
)
REVIEW_WRITERS = (
    'dispute_result',
    'resolve_dispute',
    'set_match_dispute_flush',
    'clear_match_dispute_flush',
)
ONGOING = TournamentStatus.ONGOING


class _Repo:
    """Serve the reads of a Ready operation and record every call.

    The two Ready setters run the real repository code against a mocked
    session, so the statements they issue are visible. Any other repository
    function raises: a Ready operation that reaches for a writer it should
    not know fails at once.
    """

    def __init__(
        self,
        session,
        *,
        game_format=GameFormat.ONE_V_ONE,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        phase=1,
        contestants=2,
        status=ONGOING,
        claimed=False,
    ):
        self.session = session
        self.calls: list[str] = []
        self.writes: list[tuple[str, tuple, dict]] = []
        self.users = [uuid4() for _ in range(contestants)]
        self.tournament = SimpleNamespace(
            id=uuid4(),
            tournament_status=status,
            game_format=game_format,
            playoff_game_format=playoff_game_format,
        )
        self.participants = {}
        for user in self.users:
            participant = SimpleNamespace(
                id=uuid4(),
                user_id=user,
                tournament_id=self.tournament.id,
                removed_at=None,
                team_id=None,
            )
            self.participants[participant.id] = participant
        match_id = uuid4()
        paired = contestants == 2 and game_format == GameFormat.ONE_V_ONE
        pairing_id = uuid4() if paired else None
        self.match = TournamentMatch(
            id=match_id,
            tournament_id=self.tournament.id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=BASE,
            phase=phase,
            pairing_id=pairing_id,
            pairing_generation=1 if paired else 0,
        )
        self.contestants = [
            TournamentMatchToContestant(
                id=uuid4(),
                tournament_match_id=match_id,
                participant_id=participant_id,
                team_id=None,
                score=None,
                created_at=BASE,
            )
            for participant_id in self.participants
        ]
        self.pairing = None
        if paired:
            sides = [
                ContestantIdentity(kind='participant', id=participant_id)
                for participant_id in self.participants
            ]
            self.pairing = MatchPairing(
                id=pairing_id,
                match_id=match_id,
                tournament_id=self.tournament.id,
                generation=1,
                side_a=sides[0],
                side_b=sides[1],
            )
        # The mocked row stands for the locked database row.
        self.row = SimpleNamespace(
            ready_at_a=None, ready_by_a=None, ready_at_b=None, ready_by_b=None
        )
        session.rows[match_id] = self.row
        if claimed:
            self.match = replace(
                self.match, ready_at_a=BASE, ready_by_a=self.users[0]
            )
            self.row.ready_at_a, self.row.ready_by_a = BASE, self.users[0]

    def __getattr__(self, name):
        raise AssertionError(f'the Ready path reached repository.{name}')

    def _note(self, name, *args, **kwargs):
        self.calls.append(name)
        if name in READY_WRITERS:
            self.writes.append((name, args, kwargs))

    def find_match(self, match_id):
        self._note('find_match')
        return self.match

    def get_tournament_for_update(self, tournament_id):
        self._note('get_tournament_for_update')
        return self.tournament

    def get_match_for_update(self, match_id):
        self._note('get_match_for_update')
        return self.match

    def get_contestants_for_match(self, match_id):
        self._note('get_contestants_for_match')
        return list(self.contestants)

    def get_match_pairing(self, match_id):
        self._note('get_match_pairing')
        return self.pairing

    def find_participant_fresh(self, participant_id):
        self._note('find_participant_fresh')
        return self.participants.get(participant_id)

    def set_side_ready_flush(self, match_id, side, ready_at, ready_by, **kw):
        self._note(
            'set_side_ready_flush', match_id, side, ready_at, ready_by, **kw
        )
        repo.set_side_ready_flush(match_id, side, ready_at, ready_by, **kw)
        self.match = replace(
            self.match,
            **{
                f'ready_at_{side.value}': ready_at,
                f'ready_by_{side.value}': ready_by,
            },
        )

    def clear_side_ready_flush(self, match_id, side, **kw):
        self._note('clear_side_ready_flush', match_id, side, **kw)
        repo.clear_side_ready_flush(match_id, side, **kw)
        self.match = replace(
            self.match,
            **{f'ready_at_{side.value}': None, f'ready_by_{side.value}': None},
        )

    def set_side_invitation_hold_flush(self, match_id, side, held):
        self._note('set_side_invitation_hold_flush', match_id, side, held)
        self.match = replace(
            self.match, **{f'invitation_hold_{side.value}': held}
        )

    def set_readiness_revision_flush(self, match_id, revision):
        self._note('set_readiness_revision_flush', match_id, revision)
        self.match = replace(self.match, readiness_revision=revision)


@pytest.fixture
def session():
    mock = MagicMock()
    mock.rows = {}
    mock.get.side_effect = lambda model, key, **kwargs: mock.rows.get(key)
    with patch.object(repo.db, 'session', mock):
        yield mock


@pytest.fixture
def make_world(monkeypatch, session):
    """Build a Ready world; the operation clock and the effects are fakes."""

    def build(**facts):
        stub = _Repo(session, **facts)

        def operation_time():
            stub.calls.append('get_operation_time')
            return OPERATION_TIME

        monkeypatch.setattr(service, 'repository', stub)
        monkeypatch.setattr(repo, 'get_operation_time', operation_time)
        monkeypatch.setattr(
            service.authority,
            'authorize_readiness_side',
            lambda tournament_id, pairing, side, user_id: Ok('player'),
        )
        stub.audit = Mock()
        monkeypatch.setattr(service, 'create_log_entry', stub.audit)
        stub.reconcile = Mock(return_value=Ok(()))
        monkeypatch.setattr(
            service, 'reconcile_invitations_flush', stub.reconcile
        )
        return stub

    return build


def _run(stub, operation, *, side=MatchSide.A, generation=None, revision=None):
    function = (
        service.claim_ready_flush
        if operation == 'claim'
        else service.revoke_ready_flush
    )
    return function(
        stub.match.id,
        side,
        stub.users[0],
        expected_pairing_generation=(
            stub.match.pairing_generation if generation is None else generation
        ),
        expected_readiness_revision=(
            stub.match.readiness_revision if revision is None else revision
        ),
    )


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _executed(session) -> list:
    return [call.args[0] for call in session.execute.call_args_list]


def _tree(module) -> ast.Module:
    # The source text, not a file: it follows an in-memory mutant too.
    return ast.parse(inspect.getsource(module))


def _function(module, name) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


# -- a Ready change is a domain change of its match, and nothing else --


# fmt: off
CHANGES = [
    pytest.param(
        'claim', False,
        ['set_side_ready_flush', 'set_side_invitation_hold_flush',
         'set_readiness_revision_flush'],
        id='claim',
    ),
    pytest.param(
        'revoke', True,
        ['clear_side_ready_flush', 'set_side_invitation_hold_flush',
         'set_readiness_revision_flush'],
        id='revoke',
    ),
]
# fmt: on


@pytest.mark.parametrize(('operation', 'claimed', 'writers'), CHANGES)
def test_ready_change_touches_last_change_without_resetting_alert(
    make_world, session, operation, claimed, writers
):
    stub = make_world(claimed=claimed)
    initial_columns = set(vars(stub.row))

    result = _run(stub, operation)

    assert result.is_ok(), result
    # Only the Ready writers ran. Any episode, acknowledgement, occupancy,
    # clock or status writer would have raised in the stub; the list
    # makes the same fact visible.
    assert [name for name, *_ in stub.writes] == writers
    for name in TIMING_WRITERS:
        assert hasattr(repo, name), name
        assert name not in stub.calls
    # The stamp is the one statement of the whole operation: the match's
    # last change at the operation time, sampled once. It is not the
    # Ready time, which the service takes from its own clock.
    statements = _executed(session)
    assert len(statements) == 1
    (stamp,) = statements
    assert isinstance(stamp, Update)
    sql = _sql(stamp)
    assert 'SET last_changed_at=' in sql
    assert 'greatest' in sql.lower()
    for column in ('occupied_since', 'opened_at', 'ack_revision', 'clock'):
        assert column not in sql
    assert (
        OPERATION_TIME
        in stamp.compile(dialect=postgresql.dialect()).params.values()
    )
    assert stub.calls.count('get_operation_time') == 1
    # The row got the claim and nothing else.
    assert set(vars(stub.row)) == initial_columns
    if operation == 'claim':
        assert stub.row.ready_by_a == stub.users[0]
        assert stub.row.ready_at_a != OPERATION_TIME
        assert stub.row.ready_at_a.tzinfo is None
    else:
        assert stub.row.ready_at_a is None and stub.row.ready_by_a is None
    assert stub.audit.call_count == 1
    assert stub.audit.call_args.kwargs['commit'] is False


# fmt: off
REFUSALS = [
    pytest.param('claim', {'revision': 9}, {}, 'readiness_conflict', id='claim-stale-revision'),
    pytest.param('claim', {'generation': 9}, {}, 'readiness_conflict', id='claim-stale-generation'),
    pytest.param('claim', {}, {'claimed': True}, 'readiness_conflict', id='claim-twice'),
    pytest.param('revoke', {}, {}, 'readiness_conflict', id='revoke-unclaimed'),
    pytest.param('claim', {}, {'status': TournamentStatus.PAUSED}, 'tournament_not_ongoing', id='claim-paused'),
    pytest.param('revoke', {}, {'status': TournamentStatus.COMPLETED, 'claimed': True}, 'tournament_not_ongoing', id='revoke-completed'),
    pytest.param('claim', {'side': 'C'}, {}, 'invalid_match_side', id='claim-bad-side'),
]
# fmt: on


@pytest.mark.parametrize(('operation', 'call', 'world', 'error'), REFUSALS)
def test_a_refused_ready_operation_writes_and_stamps_nothing(
    make_world, session, operation, call, world, error
):
    stub = make_world(**world)

    result = _run(stub, operation, **call)

    assert result.unwrap_err() == error
    assert stub.writes == []
    assert not [name for name in TIMING_WRITERS if name in stub.calls]
    # No stamp, and the server clock is not even read for a refusal.
    assert _executed(session) == []
    assert 'get_operation_time' not in stub.calls
    stub.audit.assert_not_called()
    stub.reconcile.assert_not_called()


def test_a_confirmed_match_takes_no_ready_change(make_world, session):
    stub = make_world()
    stub.match = replace(stub.match, confirmed_by=stub.users[0])

    result = _run(stub, 'claim')

    assert result.unwrap_err() == 'match_confirmed'
    assert stub.writes == []
    assert _executed(session) == []


def test_the_ready_service_never_passes_its_own_clock_as_the_change_time():
    """A Ready time is not an operation time: it comes from the app's clock.

    The setters of the repository sample the server clock after the locks
    and stamp the match with it. The service must not hand them its own
    `now`, must not stamp by itself, and knows no timing fact.
    """
    tree = _tree(service)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == 'changed_at':
            assert not (
                isinstance(node.value, ast.Name)
                and node.value.id in {'now', 'occurred_at'}
            ), 'the Ready time was passed as the change time'
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            assert 'tournament_operational_service' not in names
            assert 'tournament_operational_domain_service' not in names
    referenced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    for name in (
        'touch_matches_last_changed_flush',
        'last_changed_at',
        'occupied_since',
        'operational_clock_elapsed_us',
        'open_due_episode_flush',
        'close_due_episodes_flush',
    ):
        assert name not in referenced, name


# -- the Ready writers lock the tournament, then the match --


# fmt: off
LOCKED_RUNS = [
    pytest.param('claim', False, {}, True, id='claim'),
    pytest.param('revoke', True, {}, True, id='revoke'),
    pytest.param('claim', False, {'revision': 9}, False, id='claim-refused'),
    pytest.param('revoke', True, {'generation': 9}, False, id='revoke-refused'),
]
# fmt: on


@pytest.mark.parametrize(('operation', 'claimed', 'call', 'ok'), LOCKED_RUNS)
def test_ready_writer_takes_tournament_first(
    make_world, session, operation, claimed, call, ok
):
    stub = make_world(claimed=claimed)

    result = _run(stub, operation, **call)

    assert result.is_ok() is ok
    names = stub.calls
    # The unlocked identity read decides which tournament to lock.
    assert names[0] == 'find_match'
    locks = [name for name in names if name in LOCKS]
    # Every match lock follows its tournament lock; none comes first.
    assert locks and locks == list(LOCKS) * (len(locks) // 2)
    first_match_lock = names.index('get_match_for_update')
    assert names.index('get_tournament_for_update') < first_match_lock
    # Nothing is written, and the clock is not read, before both locks.
    for index, name in enumerate(names):
        if name in READY_WRITERS or name == 'get_operation_time':
            assert index > first_match_lock, name
    if ok:
        assert names.count('get_operation_time') == 1


def test_the_lock_helper_takes_the_tournament_before_the_match():
    helper = _function(service, '_locked_facts')
    called = [
        node.func.attr
        for node in ast.walk(helper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert called.index('get_tournament_for_update') < called.index(
        'get_match_for_update'
    )
    assert 'lock_matches_for_update' not in called


# -- review: no adapter is installed, and none is faked --


def test_installed_review_changes_are_atomic_and_independent_of_ack():
    """The absent-adapter contract.

    No dispute or review feature exists in this tree. Its writers, its
    stored facts and its read model are all absent, and the traffic
    acknowledgement knows nothing of a review. The day an adapter is
    installed this test fails: that adapter's writers must then be
    integrated (flush only, one operation time, one commit owner, never
    through the acknowledgement) and this contract replaced.
    """
    modules = (
        match_service,
        repo,
        service,
        coordination,
        operational,
        dashboard_service,
        dashboard_repository,
    )
    installed = [
        f'{module.__name__}.{name}'
        for module in modules
        for name in REVIEW_WRITERS
        if hasattr(module, name)
    ]
    assert installed == [], f'a review adapter is installed: {installed}'

    # No stored or modelled review fact on a match or an episode.
    def review_like(names):
        return sorted(
            n for n in names if 'dispute' in n.lower() or 'review' in n.lower()
        )

    assert (
        review_like(c.name for c in DbTournamentMatch.__table__.columns) == []
    )
    for model in (
        TournamentMatch,
        MatchDueEpisode,
        MatchEscalationAcknowledgement,
    ):
        assert review_like(f.name for f in fields(model)) == [], model

    # The read model carries two flags and both default to "no review".
    defaults = {f.name: f.default for f in fields(DashboardRow)}
    assert review_like(defaults) == ['review_available', 'review_open']
    assert defaults['review_available'] is False
    assert defaults['review_open'] is False

    # The review state selects nothing, whatever the data.
    predicate = dashboard_repository._STATE_PREDICATES['review-open'](None)
    assert isinstance(predicate, False_)

    # Acknowledging an escalation is independent of any review: neither
    # the acknowledgement path nor its writers mention one.
    for module, name in (
        (coordination, 'acknowledge_match'),
        (repo, 'advance_episode_ack_revision_flush'),
        (repo, 'create_escalation_ack_flush'),
    ):
        function = _function(module, name)
        mentioned = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(function)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        assert review_like(mentioned) == [], (name, review_like(mentioned))


# -- FFA has no Ready sides, and no review is invented --


def _pairs_nothing(readiness):
    assert readiness.assignment_complete is False
    assert readiness.status == ReadinessDisplayStatus.NOT_YET_OCCUPIED
    assert readiness.ready_sides == ()
    assert readiness.ready_at_a is None and readiness.ready_at_b is None
    assert readiness.ready_by_a is None and readiness.ready_by_b is None
    assert readiness.pairing_started_at is None


def test_absent_review_and_ffa_ready_are_truthfully_unavailable(make_world):
    # An FFA lobby has no sides, whatever its size: the projection says so,
    # even for a lobby whose row happens to hold stale claim columns, and
    # the writers refuse it before any write, lock-held stamp or audit entry
    # reaches storage.
    for size in (2, 4):
        stub = make_world(game_format=GameFormat.FREE_FOR_ALL, contestants=size)
        forged = replace(
            stub.match,
            ready_at_a=BASE,
            ready_by_a=stub.users[0],
            ready_at_b=BASE,
            ready_by_b=stub.users[1],
        )
        _pairs_nothing(
            derive_match_readiness(
                forged, stub.contestants, pairing=None, supports_readiness=False
            )
        )
        for operation, side in (
            ('claim', MatchSide.A),
            ('claim', MatchSide.B),
            ('revoke', MatchSide.A),
            ('revoke', MatchSide.B),
        ):
            result = _run(stub, operation, side=side, generation=0, revision=0)
            assert result.unwrap_err() == 'readiness_format_unsupported'
        assert stub.writes == []
        assert 'get_operation_time' not in stub.calls
        assert stub.session.execute.call_count == 0
        stub.audit.assert_not_called()
        stub.reconcile.assert_not_called()

    # The row model offers no review fact and no Ready time for such a
    # lobby by default, and nothing fills them in.
    defaults = {f.name: f.default for f in fields(DashboardRow)}
    assert defaults['readiness_available'] is False
    assert defaults['ready_at_a'] is None and defaults['ready_at_b'] is None
    assert defaults['review_available'] is False
    assert defaults['review_open'] is False
    for module in (dashboard_repository, dashboard_service):
        setters = {
            keyword.arg
            for node in ast.walk(_tree(module))
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg in {'review_available', 'review_open'}
        }
        assert setters == set(), (module.__name__, setters)


# fmt: off
UNSUPPORTED = [
    pytest.param(
        {'game_format': GameFormat.FREE_FOR_ALL, 'contestants': 4},
        id='free-for-all-tournament',
    ),
    pytest.param(
        {'game_format': GameFormat.ONE_V_ONE,
         'playoff_game_format': GameFormat.FREE_FOR_ALL, 'phase': 2},
        id='free-for-all-playoff-of-a-1v1-tournament',
    ),
]
# fmt: on


@pytest.mark.parametrize('world', UNSUPPORTED)
def test_a_phase_without_ready_sides_takes_no_claim(make_world, world):
    stub = make_world(**world)

    for operation in ('claim', 'revoke'):
        result = _run(stub, operation, generation=0, revision=0)
        assert result.unwrap_err() == 'readiness_format_unsupported'
    assert stub.writes == []
    assert stub.session.execute.call_count == 0


def test_the_same_tournament_supports_ready_in_its_first_phase(make_world):
    stub = make_world(
        game_format=GameFormat.ONE_V_ONE,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
    )

    result = _run(stub, 'claim')

    assert result.is_ok(), result
    assert [name for name, *_ in stub.writes][0] == 'set_side_ready_flush'
