"""
tests.integration.services.lan_tournament.test_phase_lock
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Group results are locked once the playoffs are released.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

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


PARTY_ID = PartyID('lan-party-2024-phase-lock')
LOCKED = tournament_match_service.PHASE1_LOCKED_ERROR

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('phaselockbrand', 'Phase Lock Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Phase Lock')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PhaseLockUser{i}') for i in range(8)]


@pytest.fixture
def created(party):
    tournaments = []
    yield tournaments
    db.session.rollback()
    for tournament in tournaments:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


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
def make_playoff_tournament(users, created):
    def _make(*, playoffs=True):
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
            f'Phase Lock Tournament {next(_counter)}',
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
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, users[0].id
        )
        assert started.is_ok(), started.unwrap_err()
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


def _confirm(match, ids, scores, admin):
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, dict(zip(ids, scores, strict=True))
    )
    assert result.is_ok(), result.unwrap_err()


def _play_groups(tournament, admin):
    """Play every group match; the lower ID wins by 3 + the group number."""
    played = _matches(tournament, 1)
    assert len(played) == 12
    for match, ids in played:
        low = min(ids, key=str)
        margin = 3 + match.group_order
        _confirm(
            match, ids, (margin, 0) if ids[0] == low else (0, margin), admin
        )


def _release(tournament, admin):
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    result = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert result.is_ok(), result.unwrap_err()


def _unrelease(tournament, admin):
    result = tournament_qualification_service.unrelease_playoffs(
        tournament.id, reason='re-seed by hand', initiator_id=admin.id
    )
    assert result.is_ok(), result.unwrap_err()


def _force_release(tournament, admin):
    """Flag the tournament released without playing the groups."""
    tournament_repository.set_playoff_release(
        tournament.id,
        released_at=datetime.now(UTC).replace(tzinfo=None),
        released_by=admin.id,
    )
    tournament_repository.commit_session()


def _scores(match):
    return {
        c.participant_id: c.score
        for c in tournament_repository.get_contestants_for_match(match.id)
    }


def _is_confirmed(match):
    return tournament_repository.get_match(match.id).confirmed_by is not None


def _corrected(ids):
    return dict(zip(ids, (0, 5), strict=True))


def test_phase_one_correction_refused_after_release(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    _play_groups(tournament, users[0])
    _release(tournament, users[0])
    match, ids = _matches(tournament, 1)[0]
    before = _scores(match)

    result = tournament_match_service.correct_match_result(
        match.id,
        users[0].id,
        reason='typo in the score',
        corrected_scores=_corrected(ids),
    )

    assert result.is_err()
    assert result.unwrap_err() == LOCKED
    tournament_repository.rollback_session()
    assert _is_confirmed(match)
    assert _scores(match) == before


def test_correction_is_refused_before_any_retraction(
    make_playoff_tournament, users, monkeypatch
):
    tournament = make_playoff_tournament()
    _play_groups(tournament, users[0])
    _release(tournament, users[0])
    match, ids = _matches(tournament, 1)[0]

    def retract(*args, **kwargs):
        raise AssertionError('the correction started a retraction')

    monkeypatch.setattr(
        tournament_match_service, '_unconfirm_match_flush', retract
    )

    result = tournament_match_service.correct_match_result(
        match.id,
        users[0].id,
        reason='typo in the score',
        corrected_scores=_corrected(ids),
    )

    assert result.unwrap_err() == LOCKED
    tournament_repository.rollback_session()
    assert _is_confirmed(match)


def test_phase_one_unconfirm_refused_after_release(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    _play_groups(tournament, users[0])
    _release(tournament, users[0])
    match, _ = _matches(tournament, 1)[0]

    result = tournament_match_service.unconfirm_match(
        match.id, users[0].id, reason='typo in the score'
    )

    assert result.is_err()
    assert result.unwrap_err() == LOCKED
    assert _is_confirmed(match)


def test_phase_two_is_still_editable_after_release(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    _play_groups(tournament, users[0])
    _release(tournament, users[0])
    semi, ids = next((m, i) for m, i in _matches(tournament, 2) if len(i) == 2)
    _confirm(semi, ids, (2, 1), users[0])

    corrected = tournament_match_service.correct_match_result(
        semi.id,
        users[0].id,
        reason='typo in the score',
        corrected_scores=dict(zip(ids, (1, 2), strict=True)),
    )
    assert corrected.is_ok(), corrected.unwrap_err()
    assert _scores(semi) == dict(zip(ids, (1, 2), strict=True))

    unconfirmed = tournament_match_service.unconfirm_match(
        semi.id, users[0].id, reason='played again'
    )
    assert unconfirmed.is_ok(), unconfirmed.unwrap_err()
    assert not _is_confirmed(semi)


def test_tournament_without_playoffs_is_unaffected(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament(playoffs=False)
    assert not tournament_repository.get_tournament(tournament.id).has_playoffs
    match, ids = _matches(tournament, 1)[0]
    _confirm(match, ids, (3, 0), users[0])

    corrected = tournament_match_service.correct_match_result(
        match.id,
        users[0].id,
        reason='typo in the score',
        corrected_scores=_corrected(ids),
    )
    assert corrected.is_ok(), corrected.unwrap_err()
    assert _scores(match) == _corrected(ids)

    unconfirmed = tournament_match_service.unconfirm_match(
        match.id, users[0].id, reason='played again'
    )
    assert unconfirmed.is_ok(), unconfirmed.unwrap_err()
    assert not _is_confirmed(match)


def test_phase_one_corrects_again_after_unrelease(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    _play_groups(tournament, users[0])
    _release(tournament, users[0])
    match, ids = _matches(tournament, 1)[0]
    refused = tournament_match_service.unconfirm_match(
        match.id, users[0].id, reason='typo in the score'
    )
    assert refused.unwrap_err() == LOCKED

    _unrelease(tournament, users[0])

    corrected = tournament_match_service.correct_match_result(
        match.id,
        users[0].id,
        reason='typo in the score',
        corrected_scores=_corrected(ids),
    )
    assert corrected.is_ok(), corrected.unwrap_err()
    assert _scores(match) == _corrected(ids)


def test_set_score_refused_for_a_phase_one_match_after_release(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    match, ids = _matches(tournament, 1)[0]
    before = _scores(match)
    _force_release(tournament, users[0])

    result = tournament_match_service.set_score(match.id, ids[0], 7)

    assert result.unwrap_err() == LOCKED
    assert _scores(match) == before


def test_participant_scores_refused_for_a_phase_one_match_after_release(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    match, ids = _matches(tournament, 1)[0]
    _force_release(tournament, users[0])
    user_of = {
        p.id: p.user_id
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }

    result = tournament_match_service.set_match_scores(
        match.id, user_of[ids[0]], dict(zip(ids, (0, 3), strict=True))
    )

    assert result.unwrap_err() == LOCKED
    assert not _is_confirmed(match)


def test_admin_confirm_refused_for_a_phase_one_match_after_release(
    make_playoff_tournament, users
):
    tournament = make_playoff_tournament()
    match, ids = _matches(tournament, 1)[0]
    _force_release(tournament, users[0])

    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, users[0].id, dict(zip(ids, (3, 0), strict=True))
    )

    assert result.unwrap_err() == LOCKED
    assert not _is_confirmed(match)


@pytest.fixture
def ffa_match(users, created):
    """An unconfirmed FFA match of a tournament flagged as released."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Phase Lock FFA {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    created.append(tournament)
    _join(tournament, users[:4])
    assert tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=users[0].id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, users[0].id
    ).is_ok()
    (match,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    return tournament, match


@pytest.fixture
def phase_one_released(monkeypatch):
    """Treat any tournament as a released playoff tournament."""
    monkeypatch.setattr(
        tournament_match_service, '_has_playoffs', lambda tournament: True
    )


def _placements(match):
    return {
        str(c.participant_id): i + 1
        for i, c in enumerate(
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }


def test_ffa_placements_refused_for_a_phase_one_match_after_release(
    ffa_match, users, phase_one_released
):
    tournament, match = ffa_match
    _force_release(tournament, users[0])

    result = tournament_match_service.set_ffa_placements(
        match.id, _placements(match)
    )

    assert result.unwrap_err() == LOCKED
    assert all(
        c.placement is None
        for c in tournament_match_service.get_contestants_for_match(match.id)
    )


def test_ffa_confirm_refused_for_a_phase_one_match_after_release(
    ffa_match, users, monkeypatch
):
    tournament, match = ffa_match
    placed = tournament_match_service.set_ffa_placements(
        match.id, _placements(match)
    )
    assert placed.is_ok(), placed.unwrap_err()
    _force_release(tournament, users[0])
    monkeypatch.setattr(
        tournament_match_service, '_has_playoffs', lambda tournament: True
    )

    result = tournament_match_service.confirm_ffa_match(match.id, users[0].id)

    assert result.unwrap_err() == LOCKED
    assert not _is_confirmed(match)


@pytest.fixture
def highscore_tournament(users, created):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Phase Lock Highscore {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    created.append(tournament)
    _join(tournament, users[:2])
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    return tournament, participants


def _submit(tournament, participant, score):
    return tournament_score_service.submit_score(
        tournament.id, score, participant_id=participant.id
    )


def _submission_count(tournament):
    return len(tournament_score_service.get_leaderboard(tournament.id).unwrap())


def test_score_submission_refused_after_close(highscore_tournament):
    tournament, (first, second) = highscore_tournament
    assert _submit(tournament, first, 10).is_ok()
    tournament_repository.set_leaderboard_closed(
        tournament.id, datetime.now(UTC).replace(tzinfo=None)
    )
    tournament_repository.commit_session()

    submitted = _submit(tournament, second, 20)
    by_participant = tournament_score_service.submit_score_by_participant(
        tournament.id, second.user_id, 20
    )
    deleted = tournament_score_service.delete_scores_for_tournament(
        tournament.id
    )

    error = tournament_score_service.SCORES_LOCKED_ERROR
    assert submitted.unwrap_err() == error
    assert by_participant.unwrap_err() == error
    assert deleted.unwrap_err() == error
    assert _submission_count(tournament) == 1


def test_score_submission_refused_after_release(highscore_tournament, users):
    tournament, (first, second) = highscore_tournament
    assert _submit(tournament, first, 10).is_ok()
    _force_release(tournament, users[0])

    submitted = _submit(tournament, second, 20)
    deleted = tournament_score_service.delete_scores_for_tournament(
        tournament.id
    )

    error = tournament_score_service.SCORES_LOCKED_ERROR
    assert submitted.unwrap_err() == error
    assert deleted.unwrap_err() == error
    assert _submission_count(tournament) == 1


def test_score_submission_and_reset_work_while_open(highscore_tournament):
    tournament, (first, second) = highscore_tournament
    assert _submit(tournament, first, 10).is_ok()
    assert _submit(tournament, second, 20).is_ok()
    assert _submission_count(tournament) == 2

    deleted = tournament_score_service.delete_scores_for_tournament(
        tournament.id
    )

    assert deleted.is_ok()
    assert _submission_count(tournament) == 0
