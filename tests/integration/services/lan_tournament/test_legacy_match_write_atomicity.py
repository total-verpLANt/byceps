from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_match_service as service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


PARTY_ID = PartyID('f03-legacy-match-write-atomicity')

BASE = datetime(2026, 10, 7, 12, 0, 0)

MAX_SCORE = service.MAX_MATCH_SCORE
CONFIRMED_ERROR = 'Cannot modify scores of a confirmed match.'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 legacy match write atomicity')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'LegacyAtomicity{i}') for i in range(2)]


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


# -------------------------------------------------------------------- #
# the world: one match under test, one control match
# -------------------------------------------------------------------- #


def _match(tournament_id) -> TournamentMatchID:
    """Create a match whose last change is `BASE`, written the plain way."""
    match_id = TournamentMatchID(uuid7())
    repo.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=BASE,
        ),
        changed_at=BASE,
    )
    return match_id


def _seat(match_id, participant_id, score):
    row = DbTournamentMatchToContestant(
        uuid7(), match_id, BASE, participant_id=participant_id, score=score
    )
    db.session.add(row)
    db.session.flush()
    return row.id


def _comment(match_id, user_id):
    repo.create_match_comment(
        TournamentMatchComment(
            id=TournamentMatchCommentID(uuid7()),
            tournament_match_id=match_id,
            created_by=user_id,
            comment='gg',
            created_at=BASE,
        )
    )


@pytest.fixture
def world(party, users):
    tournament = DbTournament(
        uuid7(),
        party.id,
        f'Legacy atomicity {uuid7()}',
        BASE,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(uuid7(), user.id, tournament.id, BASE)
        for user in users
    ]
    db.session.add_all(participants)
    db.session.commit()

    tournament_id = TournamentID(tournament.id)
    first, second = (participant.id for participant in participants)
    match_id = _match(tournament_id)
    seats = [_seat(match_id, first, 1), _seat(match_id, second, 2)]
    control_id = _match(tournament_id)
    _seat(control_id, first, 5)
    for target in (match_id, control_id):
        _comment(target, users[0].id)
        _comment(target, users[1].id)

    # A claimed side gives the deletion pairing work to audit and retire.
    row = db.session.get(DbTournamentMatch, match_id)
    row.ready_at_a = BASE
    row.ready_by_a = users[0].id
    # Operational facts of the match, as the dashboard keeps them.
    repo.open_due_episode_flush(
        MatchDueEpisode(
            id=MatchDueEpisodeID(uuid7()),
            tournament_id=tournament_id,
            match_id=match_id,
            pairing_key='pair',
            opened_at=BASE,
            opened_clock_us=0,
        )
    )
    repo.save_match_pin_flush(
        match_id,
        tournament_id,
        pinned_at=BASE,
        pinned_by=users[0].id,
        updated_at=BASE,
        updated_by=users[0].id,
        expected_revision=0,
    )
    db.session.commit()
    return SimpleNamespace(
        tournament_id=tournament_id,
        match_id=match_id,
        control_id=control_id,
        participant_ids=[first, second],
        seat_ids=seats,
        user_ids=[user.id for user in users],
    )


# -------------------------------------------------------------------- #
# observation: committed state, held locks, transactions
# -------------------------------------------------------------------- #


def _read(sql: str, **params):
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _state(world, match_id=None):
    """Return everything committed about a match and its dependents."""
    match_id = match_id or world.match_id
    return {
        'match': _read(
            'SELECT last_changed_at, confirmed_by, ready_at_a, ready_by_a,'
            ' readiness_revision FROM lan_tournament_matches WHERE id = :id',
            id=match_id,
        ),
        'contestants': _read(
            'SELECT id, participant_id, score'
            ' FROM lan_tournament_match_contestants'
            ' WHERE tournament_match_id = :id ORDER BY id',
            id=match_id,
        ),
        'comments': _read(
            'SELECT id FROM lan_tournament_match_comments'
            ' WHERE tournament_match_id = :id ORDER BY id',
            id=match_id,
        ),
        'episodes': _read(
            'SELECT id, closed_at, closed_clock_us'
            ' FROM lan_tournament_match_due_episodes WHERE match_id = :id',
            id=match_id,
        ),
        'pins': _read(
            'SELECT revision, pinned_at FROM'
            ' lan_tournament_match_dashboard_annotations'
            ' WHERE match_id = :id',
            id=match_id,
        ),
        'audit': [
            event_type
            for (event_type,) in _read(
                'SELECT event_type FROM lan_tournament_log_entries'
                ' WHERE tournament_id = :id ORDER BY id',
                id=world.tournament_id,
            )
        ],
    }


def _score_of(world, seat_id):
    [(score,)] = _read(
        'SELECT score FROM lan_tournament_match_contestants WHERE id = :id',
        id=seat_id,
    )
    return score


def _last_changed(match_id):
    [(value,)] = _read(
        'SELECT last_changed_at FROM lan_tournament_matches WHERE id = :id',
        id=match_id,
    )
    return value


def _is_locked(table: str, row_id) -> bool:
    """Tell if another transaction holds the row lock."""
    statements = {
        'tournament': 'SELECT id FROM lan_tournaments WHERE id = :id'
        ' FOR UPDATE NOWAIT',
        'match': 'SELECT id FROM lan_tournament_matches WHERE id = :id'
        ' FOR UPDATE NOWAIT',
    }
    with db.engine.connect() as connection:
        try:
            connection.execute(text(statements[table]), {'id': row_id})
        except OperationalError:
            return True
        finally:
            connection.rollback()
    return False


def _locks_are_free(world) -> bool:
    return not (
        _is_locked('tournament', world.tournament_id)
        or _is_locked('match', world.match_id)
    )


@pytest.fixture
def transactions():
    """Count the commits and rollbacks of this thread's session."""
    session = db.session()
    counts = SimpleNamespace(commits=0, rollbacks=0)

    def on_commit(_):
        counts.commits += 1

    def on_rollback(_):
        counts.rollbacks += 1

    event.listen(session, 'after_commit', on_commit)
    event.listen(session, 'after_rollback', on_rollback)
    yield counts
    event.remove(session, 'after_commit', on_commit)
    event.remove(session, 'after_rollback', on_rollback)


def _spy(monkeypatch, name, log, *, before=None):
    """Record calls to a repository function and run the real one."""
    real = getattr(repo, name)

    def spy(*args, **kwargs):
        log.append((name, kwargs))
        if before is not None:
            before()
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, name, spy)


# -------------------------------------------------------------------- #
# legacy score: tournament first, flush only, one commit
# -------------------------------------------------------------------- #


def test_legacy_score_takes_tournament_first_and_commits_once(
    world, transactions, monkeypatch
):
    log = []
    held = {}

    def observe():
        # At the moment of the write, both rows are locked by this writer.
        held['tournament'] = _is_locked('tournament', world.tournament_id)
        held['match'] = _is_locked('match', world.match_id)
        held['commits'] = transactions.commits

    for name in (
        'lock_tournament_for_update',
        'lock_matches_for_update',
        'commit_session',
    ):
        _spy(monkeypatch, name, log)
    _spy(monkeypatch, 'update_contestant_score', log, before=observe)

    result = service.set_score(world.match_id, world.participant_ids[0], 7)

    assert result.is_ok(), result.unwrap_err()
    assert [name for name, _ in log] == [
        'lock_tournament_for_update',
        'lock_matches_for_update',
        'update_contestant_score',
        'commit_session',
    ]
    assert log[2][1] == {'commit': False}
    assert held == {'tournament': True, 'match': True, 'commits': 0}
    assert transactions.commits == 1
    assert transactions.rollbacks == 0
    # The score and its last-change stamp became durable together.
    assert _score_of(world, world.seat_ids[0]) == 7
    assert _last_changed(world.match_id) > BASE
    assert _last_changed(world.control_id) == BASE
    assert _locks_are_free(world)


def test_legacy_score_without_a_change_commits_without_a_stamp(
    world, transactions
):
    result = service.set_score(world.match_id, world.participant_ids[0], 1)

    assert result.is_ok(), result.unwrap_err()
    assert transactions.commits == 1
    assert _last_changed(world.match_id) == BASE


# fmt: off
@pytest.mark.parametrize('score', [0, MAX_SCORE])
# fmt: on
def test_legacy_score_accepts_zero_and_the_ceiling(world, score):
    result = service.set_score(world.match_id, world.participant_ids[0], score)

    assert result.is_ok(), result.unwrap_err()
    assert _score_of(world, world.seat_ids[0]) == score


# fmt: off
@pytest.mark.parametrize(
    'score, error, confirmed',
    [
        pytest.param(-1, 'Score cannot be negative.', False, id='negative'),
        pytest.param(
            MAX_SCORE + 1, service.MAX_MATCH_SCORE_ERROR, False,
            id='over the ceiling',
        ),
        pytest.param(
            2**63, service.MAX_MATCH_SCORE_ERROR, False,
            id='far over the ceiling',
        ),
        pytest.param(9, CONFIRMED_ERROR, True, id='confirmed'),
    ],
)
# fmt: on
def test_legacy_score_rejects_confirmed_and_out_of_range(
    world, transactions, score, error, confirmed
):
    if confirmed:
        repo.confirm_match(world.match_id, world.user_ids[0], changed_at=BASE)
        repo.commit_session()
    before = _state(world)
    commits_before = transactions.commits

    result = service.set_score(world.match_id, world.participant_ids[0], score)

    assert result.is_err()
    assert result.unwrap_err() == error
    # Nothing changed, no commit happened, and no lock is left behind.
    assert _state(world) == before
    assert transactions.commits == commits_before
    assert _locks_are_free(world)


def test_legacy_score_for_an_unknown_contestant_releases_the_locks(
    world, transactions
):
    before = _state(world)
    stranger = uuid7()

    result = service.set_score(world.match_id, stranger, 3)

    assert result.is_err()
    assert result.unwrap_err() == (
        f'Contestant "{stranger}" not found in match "{world.match_id}"'
    )
    assert _state(world) == before
    assert transactions.commits == 0
    assert _locks_are_free(world)


def test_legacy_score_for_an_unknown_match_is_an_error_result(world):
    missing = TournamentMatchID(uuid7())

    result = service.set_score(missing, world.participant_ids[0], 3)

    assert result.is_err()
    assert str(missing) in result.unwrap_err()


# fmt: off
@pytest.mark.parametrize('step', ['update_contestant_score', 'commit_session'])
# fmt: on
def test_legacy_score_failure_leaves_no_partial_write(
    world, transactions, monkeypatch, step
):
    before = _state(world)
    real = getattr(repo, step)

    def failing(*args, **kwargs):
        if step == 'update_contestant_score':
            real(*args, **kwargs)  # the score and its stamp are staged
        raise RuntimeError('storage unavailable')

    monkeypatch.setattr(repo, step, failing)

    with pytest.raises(RuntimeError, match='storage unavailable'):
        service.set_score(world.match_id, world.participant_ids[0], 7)

    assert _state(world) == before
    assert transactions.commits == 0
    assert transactions.rollbacks >= 1
    assert _locks_are_free(world)


# -------------------------------------------------------------------- #
# match deletion: comments, contestants, match and timing as one operation
# -------------------------------------------------------------------- #


@pytest.fixture
def match_deleted(monkeypatch):
    """Replace the post-commit signal with a recorder."""
    signal = Mock()
    monkeypatch.setattr(signals, 'match_deleted', signal)
    return signal


# fmt: off
STEPS = [
    'delete_comments_for_match_flush',
    'delete_contestants_for_match_flush',
    'delete_match_flush',
    'commit_session',
]
# fmt: on


@pytest.mark.parametrize('step', STEPS)
def test_match_delete_cleanup_rolls_back_as_one_operation(
    world, transactions, monkeypatch, match_deleted, step
):
    before = _state(world)
    control_before = _state(world, world.control_id)
    # The state under test holds every kind of dependent.
    assert len(before['comments']) == 2
    assert len(before['contestants']) == 2
    assert before['episodes'] and before['pins'] and before['match']
    real = getattr(repo, step)

    def failing(*args, **kwargs):
        if step != 'commit_session':
            real(*args, **kwargs)  # this step is staged, then storage fails
        raise RuntimeError('storage unavailable')

    monkeypatch.setattr(repo, step, failing)

    with pytest.raises(RuntimeError, match='storage unavailable'):
        service.delete_match(world.match_id)

    # A fresh connection sees the starting state, the operational facts and
    # the last-change stamp included. The rollback is the service's own.
    assert _state(world) == before
    assert _state(world, world.control_id) == control_before
    assert transactions.commits == 0
    assert transactions.rollbacks >= 1
    assert _locks_are_free(world)
    match_deleted.send.assert_not_called()


def test_match_delete_commits_everything_once_before_the_signal(
    world, transactions, monkeypatch, match_deleted
):
    before = _state(world)
    control_before = _state(world, world.control_id)
    order = []
    log = []
    held = {}

    def at_commit():
        order.append('commit')
        held['tournament'] = _is_locked('tournament', world.tournament_id)
        held['match'] = _is_locked('match', world.match_id)
        held['commits'] = transactions.commits

    _spy(monkeypatch, 'lock_tournament_for_update', log)
    _spy(monkeypatch, 'delete_comments_for_match_flush', log)
    _spy(monkeypatch, 'commit_session', log, before=at_commit)
    match_deleted.send.side_effect = lambda *a, **k: order.append('signal')

    service.delete_match(world.match_id)

    assert order == ['commit', 'signal']
    # The lock comes first, before the first deletion. The repository
    # re-takes it later; the commit is one and the last step.
    names = [name for name, _ in log]
    assert names[:2] == [
        'lock_tournament_for_update',
        'delete_comments_for_match_flush',
    ]
    assert names[-1] == 'commit_session'
    assert names.count('commit_session') == 1
    # Until the one commit, the work is staged under both locks.
    assert held == {'tournament': True, 'match': True, 'commits': 0}
    assert transactions.commits == 1
    assert transactions.rollbacks == 0
    after = _state(world)
    assert after['match'] == []
    assert after['contestants'] == []
    assert after['comments'] == []
    # The pairing change was audited in the same transaction.
    assert after['audit'] == before['audit'] + ['match-pairing-invalidated']
    # The control match keeps every row; only the audit trail is shared.
    control_after = _state(world, world.control_id)
    assert control_after | {'audit': []} == control_before | {'audit': []}
    assert _locks_are_free(world)
    [call] = match_deleted.send.call_args_list
    assert call.kwargs['event'].match_id == world.match_id


def test_match_delete_of_an_unknown_match_changes_nothing(
    world, transactions, match_deleted
):
    before = _state(world)

    with pytest.raises(ValueError):
        service.delete_match(TournamentMatchID(uuid7()))

    assert _state(world) == before
    assert transactions.commits == 0
    match_deleted.send.assert_not_called()
