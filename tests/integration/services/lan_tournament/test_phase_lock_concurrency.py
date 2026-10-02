"""
tests.integration.services.lan_tournament.test_phase_lock_concurrency
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two sessions race a release against a phase-1 result or a score write.
"""

from datetime import datetime, UTC
from itertools import count
import threading
import time

from flask import current_app
import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-phase-lock-race')
LOCKED = tournament_match_service.PHASE1_LOCKED_ERROR
SCORES_LOCKED = tournament_score_service.SCORES_LOCKED_ERROR

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('phaselockracebrand', 'Phase Lock Race Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Phase Lock Race')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PhaseLockRaceUser{i}') for i in range(8)]


@pytest.fixture
def created(party):
    tournaments = []
    yield tournaments
    db.session.rollback()
    for tournament in tournaments:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


class _Worker:
    """Run a call in a thread with its own app context, so its own session."""

    def __init__(self, call):
        self.result = None
        self.error = None
        self._thread = threading.Thread(
            target=self._run,
            args=(current_app._get_current_object(), call),
            daemon=True,
        )
        self._thread.start()

    def _run(self, app, call):
        try:
            with app.app_context():
                self.result = call()
                db.session.rollback()
        except BaseException as exc:
            self.error = exc

    @property
    def alive(self):
        return self._thread.is_alive()

    def finish(self):
        self._thread.join(timeout=20)
        assert not self._thread.is_alive(), 'the worker never finished'
        if self.error is not None:
            raise self.error
        return self.result


def _wait_until_blocked(worker):
    """Wait until another backend waits on a lock."""
    with db.engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as connection:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text(
                    'SELECT count(*) FROM pg_stat_activity'
                    ' WHERE datname = current_database()'
                    " AND wait_event_type = 'Lock'"
                    ' AND pid <> pg_backend_pid()'
                )
            ).scalar()
            if waiting:
                assert worker.alive
                return
            assert worker.alive, 'the worker finished without waiting'
            time.sleep(0.05)
    pytest.fail('the worker never waited on a lock')


def _join(tournament, users):
    for user in users:
        tournament_repository.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=datetime.now(UTC),
            )
        )
    db.session.commit()


@pytest.fixture
def make_group_tournament(users, created):
    def _make(*, playoffs=True, started=True):
        extra = (
            {
                'playoff_game_format': GameFormat.ONE_V_ONE,
                'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
                'playoff_group_count': 2,
                'playoff_qualifiers_per_group': 2,
                'playoff_release_mode': PlayoffReleaseMode.MANUAL,
            }
            if playoffs
            else {}
        )
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Phase Lock Race Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **extra,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _join(tournament, users)
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        if started:
            begun = tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, users[0].id
            )
            assert begun.is_ok(), begun.unwrap_err()
        return tournament

    return _make


def _matches(tournament, phase):
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    return [
        (m, [c.participant_id for c in contestants.get(m.id, [])])
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == phase
    ]


def _play_groups(tournament, admin):
    """Play every group match; the lower ID wins by 3 + the group number."""
    for match, ids in _matches(tournament, 1):
        low = min(ids, key=str)
        margin = 3 + match.group_order
        scores = (margin, 0) if ids[0] == low else (0, margin)
        result = tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, dict(zip(ids, scores, strict=True))
        )
        assert result.is_ok(), result.unwrap_err()


def _scores(match):
    return {
        c.participant_id: c.score
        for c in tournament_repository.get_contestants_for_match(match.id)
    }


def _flag_released(tournament, admin):
    """Stage a release in the current transaction: lock the row, flag it."""
    tournament_repository.lock_tournament_for_update(tournament.id)
    tournament_repository.set_playoff_release(
        tournament.id,
        released_at=datetime.now(UTC).replace(tzinfo=None),
        released_by=admin.id,
    )


def _flag_closed(tournament):
    tournament_repository.lock_tournament_for_update(tournament.id)
    tournament_repository.set_leaderboard_closed(
        tournament.id, datetime.now(UTC).replace(tzinfo=None)
    )


def _edge_match(tournament):
    """Return the match between the last qualifier and the first runner-up."""
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    qualified = {q.contestant_id for q in state.qualifiers}
    ranking = state.rankings[0]
    last_in = next(
        e.contestant_id
        for e in reversed(ranking.entries)
        if e.contestant_id in qualified
    )
    first_out = next(
        e.contestant_id
        for e in ranking.entries
        if e.contestant_id not in qualified
    )
    for match, ids in _matches(tournament, 1):
        if {str(i) for i in ids} == {last_in, first_out}:
            wins = {str(i): 5 if str(i) == first_out else 0 for i in ids}
            return match, {i: wins[str(i)] for i in ids}, qualified
    raise AssertionError('no edge match')


# -------------------------------------------------------------------- #
# release vs phase-1 results


def test_correction_waits_for_a_release_then_is_refused(
    make_group_tournament, users
):
    tournament = make_group_tournament()
    _play_groups(tournament, users[0])
    match, ids = _matches(tournament, 1)[0]
    before = _scores(match)

    _flag_released(tournament, users[0])  # the release transaction is open

    worker = _Worker(
        lambda: tournament_match_service.correct_match_result(
            match.id,
            users[0].id,
            reason='typo in the score',
            corrected_scores=dict(zip(ids, (0, 5), strict=True)),
        )
    )
    _wait_until_blocked(worker)
    tournament_repository.commit_session()  # the release commits

    result = worker.finish()
    assert result.is_err()
    assert result.unwrap_err() == LOCKED
    assert _scores(match) == before


def test_release_waits_for_a_correction_then_uses_the_corrected_result(
    make_group_tournament, users
):
    tournament = make_group_tournament()
    _play_groups(tournament, users[0])
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    match, corrected, qualified_before = _edge_match(tournament)

    # The correction holds the tournament row; the release queues behind it.
    tournament_match_service._lock_reachable_matches(match.id)
    worker = _Worker(
        lambda: tournament_qualification_service.release_playoffs(
            tournament.id,
            expected_version=board.version,
            initiator_id=users[0].id,
        )
    )
    _wait_until_blocked(worker)

    corrected_result = tournament_match_service.correct_match_result(
        match.id,
        users[0].id,
        reason='the edge match was entered wrongly',
        corrected_scores=corrected,
    )
    assert corrected_result.is_ok(), corrected_result.unwrap_err()

    released = worker.finish()
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    qualified_after = {q.contestant_id for q in state.qualifiers}
    assert qualified_after != qualified_before
    if released.is_ok():
        assert state.released_at is not None
        phase_two = {
            str(i) for _, ids in _matches(tournament, 2) for i in ids if i
        }
        assert phase_two <= qualified_after
        assert not phase_two & (qualified_before - qualified_after)
    else:
        assert state.released_at is None


def test_set_score_waits_for_a_release_then_is_refused(
    make_group_tournament, users
):
    tournament = make_group_tournament()
    match, ids = _matches(tournament, 1)[0]
    before = _scores(match)

    _flag_released(tournament, users[0])

    worker = _Worker(
        lambda: tournament_match_service.set_score(match.id, ids[0], 7)
    )
    _wait_until_blocked(worker)
    tournament_repository.commit_session()

    result = worker.finish()
    assert result.is_err()
    assert result.unwrap_err() == LOCKED
    assert _scores(match) == before


def test_set_score_takes_no_tournament_lock_without_playoffs(
    make_group_tournament, users, monkeypatch
):
    tournament = make_group_tournament(playoffs=False)
    match, ids = _matches(tournament, 1)[0]
    locks = []
    monkeypatch.setattr(
        tournament_repository,
        'lock_tournament_for_update',
        locks.append,
    )

    result = tournament_match_service.set_score(match.id, ids[0], 7)

    assert result.is_ok(), result.unwrap_err()
    assert _scores(match)[ids[0]] == 7
    assert locks == []


# -------------------------------------------------------------------- #
# leaderboard close vs score writes


@pytest.fixture
def highscore_playoff_tournament(users, created):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Phase Lock Race Highscore {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=4,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
        point_table=[5, 3, 2, 1],
        group_size_min=3,
        group_size_max=4,
        advancement_count=2,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    created.append(tournament)
    _join(tournament, users[:5])
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    return tournament, participants


def _submission_count(tournament):
    return len(tournament_score_service.get_leaderboard(tournament.id).unwrap())


def test_submit_score_waits_for_a_close_then_is_refused(
    highscore_playoff_tournament,
):
    tournament, (first, second, *_) = highscore_playoff_tournament
    assert tournament_score_service.submit_score(
        tournament.id, 10, participant_id=first.id
    ).is_ok()

    _flag_closed(tournament)

    worker = _Worker(
        lambda: tournament_score_service.submit_score(
            tournament.id, 20, participant_id=second.id
        )
    )
    _wait_until_blocked(worker)
    tournament_repository.commit_session()

    result = worker.finish()
    assert result.is_err()
    assert result.unwrap_err() == SCORES_LOCKED
    assert _submission_count(tournament) == 1


def test_score_reset_waits_for_a_close_then_is_refused(
    highscore_playoff_tournament,
):
    tournament, (first, *_) = highscore_playoff_tournament
    assert tournament_score_service.submit_score(
        tournament.id, 10, participant_id=first.id
    ).is_ok()

    _flag_closed(tournament)

    worker = _Worker(
        lambda: tournament_score_service.delete_scores_for_tournament(
            tournament.id
        )
    )
    _wait_until_blocked(worker)
    tournament_repository.commit_session()

    result = worker.finish()
    assert result.unwrap_err() == SCORES_LOCKED
    assert _submission_count(tournament) == 1


def test_close_waits_for_a_submission_and_counts_it(
    highscore_playoff_tournament, users
):
    tournament, (first, second, *_) = highscore_playoff_tournament
    assert tournament_score_service.submit_score(
        tournament.id, 10, participant_id=first.id
    ).is_ok()

    # The submission holds the tournament row; the close queues behind it.
    tournament_repository.lock_tournament_for_update(tournament.id)
    worker = _Worker(
        lambda: tournament_score_service.close_leaderboard(
            tournament.id, initiator_id=users[0].id
        )
    )
    _wait_until_blocked(worker)

    submitted = tournament_score_service.submit_score(
        tournament.id, 20, participant_id=second.id
    )
    assert submitted.is_ok(), submitted.unwrap_err()

    assert worker.finish().is_ok()
    assert _submission_count(tournament) == 2
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).leaderboard_closed_at
        is not None
    )


def test_score_writes_take_no_tournament_lock_without_playoffs(
    users, created, monkeypatch
):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Phase Lock Race Plain Highscore {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
    )
    tournament, _ = result.unwrap()
    created.append(tournament)
    _join(tournament, users[:1])
    (participant,) = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    locks = []
    monkeypatch.setattr(
        tournament_repository,
        'lock_tournament_for_update',
        locks.append,
    )

    submitted = tournament_score_service.submit_score(
        tournament.id, 10, participant_id=participant.id
    )
    deleted = tournament_score_service.delete_scores_for_tournament(
        tournament.id
    )

    assert submitted.is_ok(), submitted.unwrap_err()
    assert deleted.is_ok(), deleted.unwrap_err()
    assert locks == []
