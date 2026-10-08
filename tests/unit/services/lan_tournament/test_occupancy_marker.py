import ast
from datetime import datetime, timedelta, timezone, UTC
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)

from tests.helpers import generate_uuid


BASE = datetime(2026, 10, 7, 12, 0, 0)
MINUTE = timedelta(minutes=1)
SERVER_CLOCK = datetime(2026, 10, 7, 12, 30, 0)
PLUS_TWO = timezone(timedelta(hours=2))
MINUS_FIVE = timezone(timedelta(hours=-5))

MATCH_ID = TournamentMatchID(generate_uuid())

ONE_V_ONE = 'ONE_V_ONE'
FREE_FOR_ALL = 'FREE_FOR_ALL'
HIGHSCORE = 'HIGHSCORE'


class FakeSession:
    """Answer the statements the occupancy writers send, from one match."""

    def __init__(self, *, phase=1, game_format=ONE_V_ONE, playoff=None):
        self.match = SimpleNamespace(occupied_since=None)
        self.format_row = SimpleNamespace(
            phase=phase, game_format=game_format, playoff_game_format=playoff
        )
        self.known = True
        self.contestants = 0
        self.statements = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.flush = MagicMock()

    def add(self, entity):
        self.contestants += 1

    def get(self, model, key, **kwargs):
        return self.match if self.known and key == MATCH_ID else None

    def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        result = MagicMock()
        if isinstance(statement, Select):
            if 'clock_timestamp' in sql:
                result.scalar_one.return_value = SERVER_CLOCK
            elif 'count(' in sql:
                result.scalar_one.return_value = self.contestants
            elif 'game_format' in sql:
                result.one_or_none.return_value = (
                    self.format_row if self.known else None
                )
        return result

    @property
    def clock_samples(self) -> int:
        return sum(
            1
            for statement in self.statements
            if isinstance(statement, Select)
            and 'clock_timestamp'
            in str(statement.compile(dialect=postgresql.dialect()))
        )

    @property
    def stamps(self) -> list:
        return [
            statement
            for statement in self.statements
            if isinstance(statement, Update)
            and 'last_changed_at'
            in str(statement.compile(dialect=postgresql.dialect()))
        ]


@pytest.fixture
def world(request):
    session = FakeSession(**getattr(request, 'param', {}))
    with patch.object(repo.db, 'session', session):
        yield session


def _contestant() -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=TournamentParticipantID(generate_uuid()),
        score=None,
        created_at=BASE,
    )


def _insert_and_observe(session, count, **kwargs):
    """Insert contestants one by one and return the occupancy after each."""
    seen = []
    for i in range(count):
        repo.create_match_contestant(
            _contestant(), changed_at=BASE + i * MINUTE, **kwargs
        )
        seen.append(session.match.occupied_since)
    return seen


# -------------------------------------------------------------------- #
# the two-row shortcut
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'phase, game_format, playoff, occupied',
    [
        (1, ONE_V_ONE, None, True),
        (2, ONE_V_ONE, ONE_V_ONE, True),
        (2, FREE_FOR_ALL, ONE_V_ONE, True),
        (1, ONE_V_ONE, FREE_FOR_ALL, True),
        (1, FREE_FOR_ALL, None, False),
        (1, FREE_FOR_ALL, ONE_V_ONE, False),
        (2, ONE_V_ONE, FREE_FOR_ALL, False),
        (2, HIGHSCORE, FREE_FOR_ALL, False),
        (2, ONE_V_ONE, None, False),
        (1, HIGHSCORE, None, False),
        (1, None, None, False),
        (3, ONE_V_ONE, ONE_V_ONE, False),
    ],
)
# fmt: on
def test_two_row_occupancy_only_for_effective_1v1(
    phase, game_format, playoff, occupied
):
    session = FakeSession(phase=phase, game_format=game_format, playoff=playoff)
    with patch.object(repo.db, 'session', session):
        seen = _insert_and_observe(session, 3)

    # The second row fixes both sides of a 1v1 match, and a third row
    # (a legacy oddity) never moves the original occupancy.
    expected = [None, BASE + MINUTE, BASE + MINUTE] if occupied else [None] * 3
    assert seen == expected


def test_two_row_occupancy_asks_the_database_for_the_phase_format_once(world):
    _insert_and_observe(world, 1)
    format_queries = [
        s
        for s in world.statements
        if 'playoff_game_format'
        in str(s.compile(dialect=postgresql.dialect()))
    ]
    assert format_queries == []

    _insert_and_observe(world, 3)
    format_queries = [
        s
        for s in world.statements
        if 'playoff_game_format'
        in str(s.compile(dialect=postgresql.dialect()))
    ]
    assert len(format_queries) == 1


def test_an_unknown_phase_format_never_makes_a_lobby_occupied():
    session = FakeSession(phase=1, game_format=None)
    with patch.object(repo.db, 'session', session):
        _insert_and_observe(session, 4)
        with pytest.raises(ValueError):
            repo.set_ffa_lobby_occupied_since_if_unset_flush(MATCH_ID, BASE)

    assert session.match.occupied_since is None


# -------------------------------------------------------------------- #
# the completed-lobby marker writer
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'world',
    [
        {'phase': 1, 'game_format': FREE_FOR_ALL, 'playoff': None},
        {'phase': 2, 'game_format': HIGHSCORE, 'playoff': FREE_FOR_ALL},
    ],
    indirect=True,
    ids=['phase-1-lobby', 'phase-2-playoff-lobby'],
)
# fmt: on
def test_ffa_lobby_occupancy_set_by_completed_lobby_marker(world):
    # Assembling the roster, however large, records no occupancy.
    assert _insert_and_observe(world, 4) == [None] * 4

    # The marker records it once, with the operation time it is given.
    assert repo.set_ffa_lobby_occupied_since_if_unset_flush(MATCH_ID, BASE)
    assert world.match.occupied_since == BASE

    # A repeated call is a no-op: unset-guarded, the first value stays.
    assert not repo.set_ffa_lobby_occupied_since_if_unset_flush(
        MATCH_ID, BASE + 5 * MINUTE
    )
    assert world.match.occupied_since == BASE

    # The marker never writes through a commit: the owner commits.
    world.commit.assert_not_called()
    world.rollback.assert_not_called()


def test_the_marker_never_resets_an_original_occupancy(world):
    world.format_row.game_format = FREE_FOR_ALL
    original = BASE - timedelta(hours=1)
    world.match.occupied_since = original

    assert not repo.set_ffa_lobby_occupied_since_if_unset_flush(
        MATCH_ID, BASE
    )

    assert world.match.occupied_since == original


# fmt: off
@pytest.mark.parametrize(
    'world',
    [
        {'phase': 1, 'game_format': ONE_V_ONE, 'playoff': None},
        {'phase': 2, 'game_format': ONE_V_ONE, 'playoff': ONE_V_ONE},
        {'phase': 1, 'game_format': HIGHSCORE, 'playoff': FREE_FOR_ALL},
        {'phase': 2, 'game_format': FREE_FOR_ALL, 'playoff': ONE_V_ONE},
        {'phase': 2, 'game_format': FREE_FOR_ALL, 'playoff': None},
    ],
    indirect=True,
    ids=['1v1', '1v1-playoff', 'highscore-phase-1', 'ffa-main-1v1-phase-2',
         'no-playoff-format'],
)
# fmt: on
def test_the_marker_refuses_a_match_that_is_not_an_ffa_lobby(world):
    with pytest.raises(ValueError, match='not a free-for-all lobby'):
        repo.set_ffa_lobby_occupied_since_if_unset_flush(MATCH_ID, BASE)

    assert world.match.occupied_since is None


def test_the_marker_refuses_an_unknown_match(world):
    world.known = False

    with pytest.raises(ValueError, match='Unknown match ID'):
        repo.set_ffa_lobby_occupied_since_if_unset_flush(MATCH_ID, BASE)


def _repository_function(name) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(repo))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_only_the_two_format_aware_adapters_call_the_low_level_writer():
    tree = ast.parse(inspect.getsource(repo))
    callers = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and ast.unparse(call.func) == 'set_occupied_since_if_unset_flush'
            for call in ast.walk(node)
        )
    }

    assert callers == {
        '_mark_occupied_if_fully_occupied',
        'set_ffa_lobby_occupied_since_if_unset_flush',
    }
    # Both ask for the phase's format before they write.
    for name in callers:
        assert '_phase_game_format_name' in {
            ast.unparse(call.func)
            for call in ast.walk(_repository_function(name))
            if isinstance(call, ast.Call)
        }


# -------------------------------------------------------------------- #
# the time
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'changed_at, expected',
    [
        (BASE, BASE),
        (BASE.replace(tzinfo=UTC), BASE),
        (BASE.replace(tzinfo=PLUS_TWO), BASE - timedelta(hours=2)),
        (BASE.replace(tzinfo=MINUS_FIVE), BASE + timedelta(hours=5)),
        (None, SERVER_CLOCK),
    ],
    ids=['naive', 'aware-utc', 'aware-plus-two', 'aware-minus-five',
         'server-clock'],
)
# fmt: on
def test_occupancy_time_is_naive_utc_operation_time(world, changed_at, expected):
    repo.create_match_contestant(_contestant(), changed_at=changed_at)
    repo.create_match_contestant(_contestant(), changed_at=changed_at)

    stored = world.match.occupied_since
    assert stored == expected
    assert stored.tzinfo is None


def test_the_server_clock_is_sampled_once_for_the_whole_operation(world):
    repo.create_match_contestant(_contestant())
    assert world.clock_samples == 1

    repo.create_match_contestant(_contestant())
    # One sample per call, shared by the stamp and the occupancy.
    assert world.clock_samples == 2
    assert world.match.occupied_since == SERVER_CLOCK
    assert world.stamps and all(
        stamp.compile(dialect=postgresql.dialect()).params['greatest_1']
        == SERVER_CLOCK
        for stamp in world.stamps
    )


def test_a_given_operation_time_leaves_the_server_clock_alone(world):
    repo.create_match_contestant(_contestant(), changed_at=BASE)
    repo.create_match_contestant(_contestant(), changed_at=BASE)

    assert world.clock_samples == 0
    assert world.match.occupied_since == BASE


@pytest.mark.parametrize(
    'world',
    [{'phase': 1, 'game_format': FREE_FOR_ALL, 'playoff': None}],
    indirect=True,
)
def test_the_marker_stores_naive_utc_for_an_aware_operation_time(world):
    aware = BASE.replace(tzinfo=PLUS_TWO)

    assert repo.set_ffa_lobby_occupied_since_if_unset_flush(MATCH_ID, aware)

    assert world.match.occupied_since == BASE - timedelta(hours=2)
    assert world.match.occupied_since.tzinfo is None


def test_the_low_level_writer_stores_naive_utc_for_an_aware_time(world):
    aware = BASE.replace(tzinfo=PLUS_TWO)

    assert repo.set_occupied_since_if_unset_flush(MATCH_ID, aware)

    assert world.match.occupied_since == BASE - timedelta(hours=2)
    assert world.match.occupied_since.tzinfo is None


def test_no_occupancy_writer_samples_the_python_clock():
    source = inspect.getsource(repo)
    tree = ast.parse(source)
    names = {
        '_mark_occupied_if_fully_occupied',
        'set_occupied_since_if_unset_flush',
        'set_ffa_lobby_occupied_since_if_unset_flush',
        '_phase_game_format_name',
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            assert 'datetime.now' not in ast.unparse(node), node.name
