"""
tests.integration.services.lan_tournament.test_highscore_playoffs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from itertools import count
from unittest.mock import Mock

import pytest
from flask_babel import force_locale
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from byceps.database import db
from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as view_helpers,
    tournament_log_service,
    tournament_match_service,
    tournament_qualification_domain_service as qualification_domain,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
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


PARTY_ID = PartyID('lan-party-2024-highscore-playoffs')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('highscoreplayoffsbrand', 'Highscore Playoffs Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Highscore Playoffs')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'HighscorePlayoffsUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        *,
        participants=5,
        qualifiers=4,
        group_size_min=3,
        group_size_max=4,
        advancement_count=2,
        point_table=(5, 3, 2, 1),
        release_mode=PlayoffReleaseMode.MANUAL,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        playoffs=True,
        playoff_mode=EliminationMode.SINGLE_ELIMINATION,
        status=TournamentStatus.ONGOING,
    ):
        playoff_args = (
            dict(
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=playoff_mode,
                playoff_qualifier_count=qualifiers,
                playoff_release_mode=release_mode,
                point_table=list(point_table),
                group_size_min=group_size_min,
                group_size_max=group_size_max,
                advancement_count=advancement_count,
            )
            if playoffs
            else {}
        )
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Highscore Playoffs Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            score_ordering=score_ordering,
            tournament_status=status,
            **playoff_args,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:participants]:
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _participants(tournament, users):
    """Return the participant IDs as strings, in the order of `users`."""
    by_user = {
        p.user_id: str(p.id)
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }
    return [by_user[u.id] for u in users if u.id in by_user]


def _submit(tournament, participant_id, score):
    result = tournament_score_service.submit_score(
        tournament.id,
        score,
        participant_id=TournamentParticipantID(participant_id),
    )
    assert result.is_ok(), result.unwrap_err()


def _fill(tournament, users, scores):
    """Submit `scores` in order, one per participant; return their IDs."""
    ids = _participants(tournament, users)
    for participant_id, score in zip(ids, scores, strict=False):
        _submit(tournament, participant_id, score)
    return ids


def _qualification(tournament):
    result = tournament_qualification_service.get_qualification(tournament.id)
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def _close(tournament, user):
    result = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=user.id
    )
    assert result.is_ok(), result.unwrap_err()


def _release(tournament, user):
    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    assert board.is_ok(), board.unwrap_err()
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=board.unwrap().version,
        initiator_id=user.id,
    )
    assert released.is_ok(), released.unwrap_err()
    return released.unwrap()


def _phase_two(tournament):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _contestant_ids(match):
    return {
        str(c.participant_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    }


def _log_types(tournament):
    return list(
        db.session.scalars(
            select(DbTournamentLogEntry.event_type)
            .filter_by(tournament_id=tournament.id)
            .order_by(DbTournamentLogEntry.occurred_at)
        )
    )


def _play(match, ranked_ids, user):
    """Place the contestants in the given order and confirm the lobby."""
    placements = {cid: place for place, cid in enumerate(ranked_ids, start=1)}
    set_result = tournament_match_service.set_ffa_placements(
        match.id, placements
    )
    assert set_result.is_ok(), set_result.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(match.id, user.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()


def test_highscore_cut_tie_blocks_without_time_tiebreak(make_tournament, users):
    tournament = make_tournament(
        participants=5, qualifiers=2, group_size_min=2, advancement_count=1
    )
    # Three contestants share the value across the cut of two; the first
    # to submit must not win the tie.
    a, b, c, d, e = _fill(tournament, users, [100, 90, 90, 90, 50])
    _close(tournament, users[0])

    state = _qualification(tournament)

    assert not state.ready
    assert state.qualifiers is None
    (tie,) = state.blockers
    assert tie.kind is qualification_domain.TieKind.CUT
    assert set(tie.contestant_ids) == {b, c, d}
    board = tournament_score_service.get_leaderboard(tournament.id).unwrap()
    assert [str(s.participant_id) for s in board[1:4]] == [b, c, d]

    decided = tournament_qualification_service.save_decision(
        tournament.id,
        'leaderboard',
        [d, c, b],
        reason='Sudden death',
        initiator_id=users[0].id,
    )
    assert decided.is_ok(), decided.unwrap_err()

    state = _qualification(tournament)
    assert state.ready
    assert [q.contestant_id for q in state.qualifiers] == [a, d]
    assert e not in {q.contestant_id for q in state.qualifiers}


def test_qualification_needs_the_leaderboard_closed(make_tournament, users):
    tournament = make_tournament()
    _fill(tournament, users, [100, 90, 80, 70, 60])

    before = _qualification(tournament)
    _close(tournament, users[0])
    after = _qualification(tournament)

    assert not before.ready
    assert before.qualifiers is None
    assert not before.blockers
    assert after.ready
    assert len(after.qualifiers) == 4


def test_qualification_values_keep_the_best_per_contestant(
    make_tournament, users
):
    higher = make_tournament()
    ids = _fill(higher, users, [10, 20, 30])
    _submit(higher, ids[0], 5)
    _submit(higher, ids[1], 25)
    lower = make_tournament(score_ordering=ScoreOrdering.LOWER_IS_BETTER)
    low_ids = _fill(lower, users, [10, 20, 30])
    _submit(lower, low_ids[0], 5)
    _submit(lower, low_ids[1], 25)

    high_values = tournament_score_service.get_qualification_values(
        higher.id
    ).unwrap()
    low_values = tournament_score_service.get_qualification_values(
        lower.id
    ).unwrap()

    assert high_values == {ids[0]: 10, ids[1]: 25, ids[2]: 30}
    assert low_values == {low_ids[0]: 5, low_ids[1]: 20, low_ids[2]: 30}


def test_qualification_ranks_a_lower_is_better_leaderboard(
    make_tournament, users
):
    tournament = make_tournament(
        qualifiers=2,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=(3, 1),
        score_ordering=ScoreOrdering.LOWER_IS_BETTER,
    )
    a, b, c, d, e = _fill(tournament, users, [50, 20, 30, 40, 10])
    _close(tournament, users[0])

    state = _qualification(tournament)

    assert [q.contestant_id for q in state.qualifiers] == [e, b]
    assert [entry.contestant_id for entry in state.rankings[0].entries] == [
        e,
        b,
        c,
        d,
        a,
    ]


def test_qualification_values_need_a_highscore_tournament(
    party, users, make_tournament
):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Highscore Playoffs Plain FFA {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=[3, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    try:
        values = tournament_score_service.get_qualification_values(
            tournament.id
        )

        assert values.is_err()
    finally:
        tournament_service.delete_tournament(tournament.id)


def test_close_leaderboard_locks_scores_and_is_audited(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [10, 20])

    _close(tournament, users[1])

    found = tournament_repository.get_tournament(tournament.id)
    assert isinstance(found.leaderboard_closed_at, datetime)
    rejected = tournament_score_service.submit_score(
        tournament.id, 99, participant_id=TournamentParticipantID(ids[0])
    )
    assert rejected.unwrap_err() == tournament_score_service.SCORES_LOCKED_ERROR
    entries = (
        db.session.execute(
            select(DbTournamentLogEntry).filter_by(
                tournament_id=tournament.id,
                event_type='qualification-leaderboard-closed',
            )
        )
        .scalars()
        .all()
    )
    assert [e.initiator_id for e in entries] == [users[1].id]


def test_reopen_unlocks_scores_and_is_audited(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])

    result = tournament_score_service.reopen_leaderboard(
        tournament.id,
        reason='  Correct scores\r\nnow  ',
        initiator_id=users[1].id,
    )

    assert result.is_ok(), result.unwrap_err()
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is None
    assert not _qualification(tournament).ready
    _submit(tournament, ids[4], 110)
    _close(tournament, users[0])
    state = _qualification(tournament)
    assert state.ready
    assert str(state.qualifiers[0].contestant_id) == ids[4]
    entries = (
        db.session.execute(
            select(DbTournamentLogEntry).filter_by(
                tournament_id=tournament.id,
                event_type='qualification-leaderboard-reopened',
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].initiator_id == users[1].id
    assert entries[0].data == {'reason': 'Correct scores\nnow'}
    with force_locale('en'):
        assert (
            view_helpers.seeding_event_label(entries[0].event_type)
            == 'Qualification reopened'
        )
        details = view_helpers._qualification_event_details(
            entries[0].event_type, entries[0].data, {}
        )
        assert 'Correct scores' in details


# fmt: off
@pytest.mark.parametrize('stage', [
    'lock_tournament_for_update',
    'get_tournament',
    'set_leaderboard_closed',
    'create_log_entry',
    'commit_session',
    'commit_database_error',
])
# fmt: on
def test_reopen_failure_preserves_closed_audit_and_session(
    make_tournament, users, monkeypatch, stage
):
    tournament = make_tournament()
    _close(tournament, users[0])
    closed_at = tournament_repository.get_tournament(
        tournament.id
    ).leaderboard_closed_at
    target = (
        tournament_log_service
        if stage == 'create_log_entry'
        else tournament_repository
    )
    method = 'commit_session' if stage == 'commit_database_error' else stage
    original = getattr(target, method)
    failure: Exception = RuntimeError(f'injected {stage}')

    def fail(*args, **kwargs):
        nonlocal failure
        if stage == 'commit_database_error':
            # PostgreSQL aborts the transaction, making rollback mandatory.
            try:
                db.session.execute(text('SELECT 1 / 0'))
            except DBAPIError as exc:
                failure = exc
                raise
        # Fail after real locking/reading/flushing, but before any commit.
        if stage != 'commit_session':
            original(*args, **kwargs)
        raise failure

    rollback = Mock(wraps=tournament_repository.rollback_session)
    with monkeypatch.context() as patch:
        patch.setattr(target, method, fail)
        patch.setattr(tournament_repository, 'rollback_session', rollback)
        expected_error = (
            DBAPIError if stage == 'commit_database_error' else RuntimeError
        )
        with pytest.raises(expected_error) as caught:
            tournament_score_service.reopen_leaderboard(
                tournament.id, reason='Correction', initiator_id=users[0].id
            )

    assert caught.value is failure
    rollback.assert_called_once_with()
    # Inspect committed state independently, without resetting the caller session.
    with db.engine.connect() as connection:
        persisted = connection.execute(
            select(DbTournament.leaderboard_closed_at).filter_by(id=tournament.id)
        ).scalar_one()
        audits = connection.execute(
            select(DbTournamentLogEntry.id).filter_by(
                tournament_id=tournament.id,
                event_type='qualification-leaderboard-reopened',
            )
        ).all()
    assert persisted == closed_at
    assert audits == []
    # Read and then successfully retry on the same session without manual rollback.
    assert tournament_repository.get_tournament(
        tournament.id
    ).leaderboard_closed_at == closed_at
    reopened = tournament_score_service.reopen_leaderboard(
        tournament.id, reason='Retry correction', initiator_id=users[0].id
    )
    assert reopened.is_ok(), reopened.unwrap_err()
    assert tournament_repository.get_tournament(
        tournament.id
    ).leaderboard_closed_at is None
    assert _log_types(tournament).count('qualification-leaderboard-reopened') == 1


def test_reopen_is_refused_while_released(make_tournament, users):
    tournament = make_tournament()
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    result = tournament_score_service.reopen_leaderboard(
        tournament.id, reason='Correct scores', initiator_id=users[0].id
    )
    assert result.unwrap_err() == (
        'The playoffs are released. Take the release back first.'
    )
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is not None
    assert 'qualification-leaderboard-reopened' not in _log_types(tournament)
    undone = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='Correct scores', initiator_id=users[0].id
    )
    assert undone.is_ok(), undone.unwrap_err()
    assert tournament_score_service.reopen_leaderboard(
        tournament.id, reason='Correct scores', initiator_id=users[0].id
    ).is_ok()


# fmt: off
@pytest.mark.parametrize('reason', ['', '  \n ', '\u200b'])
# fmt: on
def test_reopen_needs_a_visible_reason(make_tournament, users, reason):
    tournament = make_tournament()
    _close(tournament, users[0])
    result = tournament_score_service.reopen_leaderboard(
        tournament.id, reason=reason, initiator_id=users[0].id
    )
    assert result.is_err()
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is not None
    assert 'qualification-leaderboard-reopened' not in _log_types(tournament)


def test_reopen_is_refused_when_not_closed(make_tournament, users):
    tournament = make_tournament()
    result = tournament_score_service.reopen_leaderboard(
        tournament.id, reason='Correct scores', initiator_id=users[0].id
    )
    assert result.unwrap_err() == 'The qualification is not closed.'
    assert 'qualification-leaderboard-reopened' not in _log_types(tournament)


# fmt: off
@pytest.mark.parametrize('status', [TournamentStatus.COMPLETED, TournamentStatus.CANCELLED])
# fmt: on
def test_reopen_is_refused_when_completed_or_cancelled(
    make_tournament, users, status
):
    tournament = make_tournament(status=status)
    tournament_repository.set_leaderboard_closed(
        tournament.id, datetime.now(UTC).replace(tzinfo=None)
    )
    db.session.commit()
    result = tournament_score_service.reopen_leaderboard(
        tournament.id, reason='Correct scores', initiator_id=users[0].id
    )
    assert result.unwrap_err() == (
        'The qualification can only be reopened while the tournament is ongoing or paused.'
    )
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is not None
    assert 'qualification-leaderboard-reopened' not in _log_types(tournament)


def test_close_leaderboard_refuses_a_second_close(make_tournament, users):
    tournament = make_tournament()
    _fill(tournament, users, [10, 20])
    _close(tournament, users[0])
    closed_at = tournament_repository.get_tournament(
        tournament.id
    ).leaderboard_closed_at

    again = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=users[0].id
    )

    assert again.unwrap_err() == tournament_score_service.SCORES_LOCKED_ERROR
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).leaderboard_closed_at
        == closed_at
    )
    assert _log_types(tournament).count('qualification-leaderboard-closed') == 1


def test_close_leaderboard_refuses_after_the_release(make_tournament, users):
    tournament = make_tournament()
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    tournament_repository.set_leaderboard_closed(tournament.id, None)
    db.session.commit()

    result = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=users[0].id
    )

    assert result.unwrap_err() == tournament_score_service.SCORES_LOCKED_ERROR
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).leaderboard_closed_at
        is None
    )


def test_close_leaderboard_needs_playoffs(make_tournament, users):
    tournament = make_tournament(playoffs=False)
    _fill(tournament, users, [10, 20])

    result = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=users[0].id
    )

    assert result.unwrap_err() == 'This tournament has no playoff phase.'
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is None
    assert 'qualification-leaderboard-closed' not in _log_types(tournament)
    again = tournament_score_service.submit_score(
        tournament.id,
        30,
        participant_id=TournamentParticipantID(
            _participants(tournament, users)[0]
        ),
    )
    assert again.is_ok()
    assert tournament_match_service._ffa_phase(found) is None


def test_release_needs_the_leaderboard_closed(make_tournament, users):
    tournament = make_tournament()
    _fill(tournament, users, [100, 90, 80, 70, 60])

    board = tournament_seeding_service.ensure_playoff_draft(tournament.id)

    assert (
        board.unwrap_err() == tournament_seeding_service.ERR_PLAYOFF_NOT_READY
    )
    assert not _phase_two(tournament)


def test_highscore_release_starts_ffa_phase_two(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])

    count_ = _release(tournament, users[0])

    lobbies = _phase_two(tournament)
    assert count_ == len(lobbies) == 1
    (lobby,) = lobbies
    assert _contestant_ids(lobby) == set(ids[:4])
    assert lobby.confirmed_by is None
    assert tournament_repository.get_matches_for_tournament(tournament.id) == (
        lobbies
    )
    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is not None
    assert found.playoff_released_by == users[0].id
    types = _log_types(tournament)
    assert types.index('qualification-leaderboard-closed') < types.index(
        'playoffs-released'
    )
    state = _qualification(tournament)
    assert state.released_at is not None
    assert state.can_unrelease


def test_automatic_release_follows_the_close(make_tournament, users):
    tournament = make_tournament(release_mode=PlayoffReleaseMode.AUTOMATIC)
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    assert not _phase_two(tournament)

    _close(tournament, users[0])

    (lobby,) = _phase_two(tournament)
    assert _contestant_ids(lobby) == set(ids[:4])
    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is not None
    assert found.playoff_released_by is None


def test_automatic_release_waits_for_a_tie_decision(make_tournament, users):
    tournament = make_tournament(
        release_mode=PlayoffReleaseMode.AUTOMATIC,
        qualifiers=2,
        group_size_min=2,
        advancement_count=1,
    )
    a, b, c, *_ = _fill(tournament, users, [100, 90, 90, 50, 40])

    _close(tournament, users[0])

    assert not _phase_two(tournament)
    decided = tournament_qualification_service.save_decision(
        tournament.id,
        'leaderboard',
        [c, b],
        reason='Rematch',
        initiator_id=users[0].id,
    )
    assert decided.is_ok(), decided.unwrap_err()
    (lobby,) = _phase_two(tournament)
    assert _contestant_ids(lobby) == {a, c}


def test_phase_two_ffa_completes_tournament(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    (lobby,) = _phase_two(tournament)

    _play(lobby, [ids[2], ids[0], ids[3], ids[1]], users[0])

    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) == ids[2]


def test_phase_two_rounds_advance_and_the_final_completes(
    make_tournament, users
):
    tournament = make_tournament(
        qualifiers=4,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=(3, 1),
    )
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    first, second = sorted(_phase_two(tournament), key=lambda m: m.group_order)

    _play(first, sorted(_contestant_ids(first)), users[0])
    _play(second, sorted(_contestant_ids(second)), users[0])

    still_running = tournament_repository.get_tournament(tournament.id)
    assert still_running.tournament_status is TournamentStatus.ONGOING
    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=users[0].id
    )
    assert advanced.unwrap() == 1
    assert tournament_match_service.ffa_round_already_advanced(
        first, still_running
    )
    (final,) = [m for m in _phase_two(tournament) if m.round == 1]
    assert len(_phase_two(tournament)) == 3
    assert len(_contestant_ids(final)) == 2
    assert _contestant_ids(final) <= set(ids[:4])

    _play(final, sorted(_contestant_ids(final)), users[0])

    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) in _contestant_ids(final)


def test_phase_two_cut_tie_takes_a_decision(make_tournament, users):
    tournament = make_tournament(
        qualifiers=4,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=(1, 1),
    )
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    lobbies = _phase_two(tournament)
    for lobby in lobbies:
        _play(lobby, sorted(_contestant_ids(lobby)), users[0])

    blocked = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=users[0].id
    )

    assert (
        blocked.unwrap_err() == tournament_match_service.QUALIFICATION_TIE_ERROR
    )
    for lobby in lobbies:
        decided = tournament_qualification_service.save_decision(
            tournament.id,
            tournament_match_service.ffa_lobby_scope(lobby),
            sorted(_contestant_ids(lobby)),
            reason='Coin toss',
            initiator_id=users[0].id,
        )
        assert decided.is_ok(), decided.unwrap_err()
    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=users[0].id
    )
    assert advanced.unwrap() == 1


def test_phase_two_lobbies_are_not_bracket_matches(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    (lobby,) = _phase_two(tournament)
    lobby_ids = sorted(_contestant_ids(lobby))

    by_admin = tournament_match_service.admin_set_and_confirm_match(
        lobby.id,
        users[0].id,
        {TournamentParticipantID(cid): 1 for cid in lobby_ids},
    )
    tournament_repository.update_contestant_scores(
        {
            c.id: 1
            for c in tournament_repository.get_contestants_for_match(lobby.id)
        }
    )
    db.session.commit()
    confirmed = tournament_match_service.confirm_match(lobby.id, users[0].id)

    placement_error = tournament_match_service.PLACEMENT_FORMAT_CONFIRM_ERROR
    assert by_admin.unwrap_err() == placement_error
    assert confirmed.unwrap_err() == placement_error
    assert ids[0] in lobby_ids

    _play(lobby, lobby_ids, users[0])
    corrected = tournament_match_service.correct_match_result(
        lobby.id, users[0].id, reason='typo'
    )
    assert (
        corrected.unwrap_err()
        == tournament_match_service.PLACEMENT_FORMAT_CORRECTION_ERROR
    )


def test_a_phase_one_match_is_not_a_free_for_all_lobby(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    # Highscore has no phase-1 matches; build one the engine must refuse.
    created = tournament_match_service._generate_ffa_round_impl(
        tournament.id, 0, ids[:3], initiator_id=users[0].id
    )
    assert created.is_ok(), created.unwrap_err()
    (stray,) = tournament_repository.get_matches_for_tournament(tournament.id)
    assert stray.phase == 2
    db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id == stray.id)
        .values(phase=1)
    )
    db.session.commit()
    assert tournament_repository.get_match(stray.id).phase == 1
    placements = {cid: i for i, cid in enumerate(ids[:3], start=1)}

    placed = tournament_match_service.set_ffa_placements(stray.id, placements)
    tournament_repository.update_contestant_placement_and_points(
        {
            c.id: (i, 0)
            for i, c in enumerate(
                tournament_repository.get_contestants_for_match(stray.id),
                start=1,
            )
        }
    )
    db.session.commit()
    confirmed = tournament_match_service.confirm_ffa_match(
        stray.id, users[0].id
    )

    not_ffa = 'Placements apply only to free-for-all matches.'
    assert placed.unwrap_err() == not_ffa
    assert confirmed.unwrap_err() == not_ffa
    assert tournament_repository.get_match(stray.id).confirmed_by is None
    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.ONGOING


def test_generate_ffa_round_is_closed_for_a_highscore_tournament(
    make_tournament, users
):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])

    refused = tournament_match_service.generate_ffa_round(
        tournament.id, contestant_ids=ids
    )
    direct = tournament_match_service._generate_ffa_round_impl(tournament.id)

    assert (
        refused.unwrap_err()
        == tournament_match_service.PLAYOFF_ROUNDS_FROM_DRAFT_ERROR
    )
    assert (
        direct.unwrap_err()
        == tournament_match_service.PLAYOFF_ROUNDS_FROM_DRAFT_ERROR
    )
    assert not _phase_two(tournament)


def test_unrelease_clears_the_ffa_lobbies(make_tournament, users):
    tournament = make_tournament()
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])

    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='Wrong cut', initiator_id=users[0].id
    )

    assert result.is_ok(), result.unwrap_err()
    assert not _phase_two(tournament)
    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is None
    assert isinstance(found.leaderboard_closed_at, datetime)


def test_ensure_playoff_draft_prefills_the_leaderboard_order(
    make_tournament, users
):
    tournament = make_tournament()
    ids = _fill(tournament, users, [60, 100, 70, 90, 80])
    _close(tournament, users[0])

    first = tournament_seeding_service.ensure_playoff_draft(tournament.id)
    second = tournament_seeding_service.ensure_playoff_draft(tournament.id)

    board = first.unwrap()
    assert board.target == tournament_seeding_service.PLAYOFF_TARGET
    assert board.state.seed_list == (ids[1], ids[3], ids[4], ids[2])
    assert set(board.state.roster) == set(board.state.seed_list)
    assert not board.stale
    assert second.unwrap().version == board.version
    assert not _phase_two(tournament)


def test_confirm_ffa_match_hands_over_to_the_auto_release(
    make_tournament, users, monkeypatch
):
    tournament = make_tournament()
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    (lobby,) = _phase_two(tournament)
    calls = []
    monkeypatch.setattr(
        tournament_match_service,
        '_try_auto_release',
        lambda tournament_id, triggered_by: calls.append(
            (tournament_id, triggered_by)
        ),
    )

    _play(lobby, sorted(_contestant_ids(lobby)), users[1])

    assert calls == [(tournament.id, users[1].id)]


def test_phase_two_round_goes_through_a_draft(make_tournament, users):
    tournament = make_tournament(
        qualifiers=4,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=(3, 1),
    )
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    for lobby in _phase_two(tournament):
        _play(lobby, sorted(_contestant_ids(lobby)), users[0])

    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, initiator_id=users[0].id
    ).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    made = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=users[0].id,
    )

    assert made.unwrap() == 1
    (final,) = [m for m in _phase_two(tournament) if m.round == 1]
    assert len(_phase_two(tournament)) == 3
    assert len(_contestant_ids(final)) == 2


def test_regenerating_the_playoffs_replaces_the_lobbies(make_tournament, users):
    tournament = make_tournament()
    ids = _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    (old,) = _phase_two(tournament)
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    unchanged = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        tournament_seeding_service.PLAYOFF_TARGET,
        expected_version=board.version,
        initiator_id=users[0].id,
    )
    assert unchanged.unwrap() == tournament_seeding_service.GENERATION_UNCHANGED
    edited = tournament_seeding_service.apply_action(
        tournament.id,
        tournament_seeding_service.PLAYOFF_TARGET,
        tournament_seeding_service.Swap(0, 1),
        expected_version=board.version,
        initiator_id=users[0].id,
    ).unwrap()

    made = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        tournament_seeding_service.PLAYOFF_TARGET,
        expected_version=edited.version,
        initiator_id=users[0].id,
    )

    assert made.unwrap() == 1
    (new,) = tournament_repository.get_matches_for_tournament(tournament.id)
    assert new.id != old.id
    assert new.phase == 2
    assert _contestant_ids(new) == set(ids[:4])


def test_phase_two_double_elimination_starts_in_the_winners_bracket(
    make_tournament, users
):
    tournament = make_tournament(
        playoff_mode=EliminationMode.DOUBLE_ELIMINATION,
        qualifiers=4,
        group_size_min=2,
        group_size_max=2,
        advancement_count=1,
        point_table=(3, 1),
    )
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])
    _release(tournament, users[0])
    lobbies = _phase_two(tournament)
    for lobby in lobbies:
        _play(lobby, sorted(_contestant_ids(lobby)), users[0])

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=users[0].id
    )

    assert len(lobbies) == 2
    assert {m.bracket for m in lobbies} == {Bracket.WINNERS}
    assert 'pool' in advanced.unwrap_err()
    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.ONGOING


@pytest.fixture
def plain_gettext(monkeypatch):
    monkeypatch.setattr(
        view_helpers,
        'gettext',
        lambda message, **params: message % params if params else message,
    )


def test_start_gate_notices_fewer_scorers_than_qualifying_places(
    make_tournament, plain_gettext
):
    tournament = make_tournament(
        participants=5,
        qualifiers=8,
        status=TournamentStatus.REGISTRATION_CLOSED,
    )

    gate = view_helpers.start_gate(tournament)

    assert gate.notices == (
        'Only 5 contestants qualify instead of the configured 8.',
    )
    assert gate.state == 'open'


def test_start_gate_has_no_notice_when_enough_scorers_can_qualify(
    make_tournament, plain_gettext
):
    tournament = make_tournament(
        participants=5,
        qualifiers=4,
        status=TournamentStatus.REGISTRATION_CLOSED,
    )

    assert view_helpers.start_gate(tournament).notices == ()


@pytest.mark.parametrize(
    'playoff_mode',
    [
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ],
    ids=['SE', 'DE'],
)
def test_a_short_release_plays_undersized_round_zero_lobbies(
    make_tournament, users, playoff_mode
):
    tournament = make_tournament(
        participants=8,
        qualifiers=8,
        playoff_mode=playoff_mode,
        release_mode=PlayoffReleaseMode.AUTOMATIC,
    )
    _fill(tournament, users, [100, 90, 80, 70, 60])

    _close(tournament, users[0])

    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is not None
    lobbies = _phase_two(tournament)
    sizes = sorted(len(_contestant_ids(m)) for m in lobbies)
    assert sizes == [2, 3]
    assert all(m.phase == 2 for m in lobbies)
    assert tournament_repository.get_matches_for_tournament(tournament.id) == (
        lobbies
    )


def test_ffa_release_permits_an_undersized_round_zero(make_tournament, users):
    tournament = make_tournament(
        participants=8,
        qualifiers=8,
        release_mode=PlayoffReleaseMode.AUTOMATIC,
    )
    _fill(tournament, users, [100, 90, 80, 70, 60])

    _close(tournament, users[0])

    assert (
        tournament_repository.get_tournament(tournament.id).playoff_released_at
        is not None
    )
    sizes = sorted(len(_contestant_ids(m)) for m in _phase_two(tournament))
    assert sizes == [2, 3]
    entries = db.session.scalars(
        select(DbTournamentLogEntry).filter_by(
            tournament_id=tournament.id, event_type='playoffs-shortfall'
        )
    ).all()
    assert [e.data['configured'] for e in entries] == [8]
    assert [e.data['qualified'] for e in entries] == [5]
    assert not db.session.scalars(
        select(DbTournamentLogEntry).filter_by(
            tournament_id=tournament.id, event_type='playoffs-de-fallback'
        )
    ).all()


def _initial_phase_two(tournament, groups):
    result = tournament_match_service._generate_ffa_initial_impl(
        tournament.id,
        roster=[cid for group in groups for cid in group],
        groups=groups,
        seeding_target='playoff',
    )
    db.session.rollback()
    return result


def test_a_full_release_keeps_the_lobby_minimum(make_tournament, users):
    tournament = make_tournament(participants=6, qualifiers=6)
    a, b, c, d, e, f = _participants(tournament, users)

    result = _initial_phase_two(tournament, [[a, b], [c, d, e, f]])

    assert result.is_err()
    assert result.unwrap_err() == (
        tournament_match_service.FFA_LOBBY_BELOW_MINIMUM_ERROR
    )


def test_a_short_release_refuses_a_lone_lobby(make_tournament, users):
    tournament = make_tournament(participants=5, qualifiers=8)
    a, b, c, *_ = _participants(tournament, users)

    result = _initial_phase_two(tournament, [[a], [b, c]])

    assert result.is_err()
    assert result.unwrap_err() == (
        tournament_match_service.FFA_LOBBY_BELOW_MINIMUM_ERROR
    )


def test_release_with_too_few_qualifiers_for_full_lobbies(
    make_tournament, users
):
    tournament = make_tournament(
        participants=8,
        qualifiers=8,
        release_mode=PlayoffReleaseMode.AUTOMATIC,
    )
    _fill(tournament, users, [100, 90, 80, 70, 60])

    _close(tournament, users[0])

    board = tournament_seeding_service.get_board(
        tournament.id, 'playoff'
    ).unwrap()
    assert board.problems == ()
    assert tournament_seeding_service.NOTICE_SMALL_PLAYOFF_LOBBIES in (
        board.notices
    )
    params = board.notice_params[
        board.notices.index(
            tournament_seeding_service.NOTICE_SMALL_PLAYOFF_LOBBIES
        )
    ]
    assert params == {'sizes': '3, 2', 'minimum': 3}
    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is not None
    sizes = sorted(len(_contestant_ids(m)) for m in _phase_two(tournament))
    assert sizes == [2, 3]


def test_a_short_playoff_board_keeps_undersized_lobbies(
    make_tournament, users
):
    tournament = make_tournament(participants=8, qualifiers=8)
    _fill(tournament, users, [100, 90, 80, 70, 60])
    _close(tournament, users[0])

    board = tournament_seeding_service.get_board(
        tournament.id, 'playoff'
    ).unwrap()

    assert (
        tournament_seeding_service.PROBLEM_LOBBY_BELOW_MIN
        not in board.problems
    )
    assert board.problems == ()
    assert tournament_seeding_service.NOTICE_SMALL_PLAYOFF_LOBBIES in (
        board.notices
    )
    _release(tournament, users[0])
    sizes = sorted(len(_contestant_ids(m)) for m in _phase_two(tournament))
    assert sizes == [2, 3]
