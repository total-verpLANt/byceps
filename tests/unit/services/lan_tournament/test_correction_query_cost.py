"""
tests.unit.services.lan_tournament.test_correction_query_cost
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pin the number of full-bracket reads a correction and a score
submission cost.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from byceps.services.lan_tournament.models.match_readiness import (
    MatchReadiness,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.readiness_change import ReadinessChange
from byceps.services.lan_tournament.models.tournament_match import (
    MatchInvitationID,
    CorrectionCase,
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
from byceps.util.result import Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'

MATCH_ID = TournamentMatchID(generate_uuid())
TOURNAMENT_ID = generate_uuid()
USER_ID = generate_uuid()
PARTICIPANT_A = TournamentParticipantID(generate_uuid())


@pytest.fixture(autouse=True)
def readiness_reset():
    pending_ids: tuple[MatchInvitationID, ...] = (MatchInvitationID(generate_uuid()),)
    def reset(match_id, *, occurred_at):
        assert occurred_at.tzinfo is None
        dispatch.assert_not_called()
        match = TournamentMatch(
            id=match_id, tournament_id=TOURNAMENT_ID, group_order=None,
            match_order=0, round=None, next_match_id=None, confirmed_by=None,
            created_at=occurred_at,
        )
        return Ok(ReadinessChange(
            match=match, actor_role=None,
            readiness=MatchReadiness(
                status=ReadinessDisplayStatus.NOT_YET_OCCUPIED,
                ready_sides=(), match_id=match_id,
            ),
        ))

    with (
        patch('byceps.services.lan_tournament.tournament_readiness_service'
              '.reset_readiness_flush', side_effect=reset),
        patch('byceps.services.lan_tournament.tournament_readiness_service'
              '.reconcile_invitations_flush', return_value=Ok(pending_ids)),
        patch('byceps.services.lan_tournament.tournament_readiness_service'
              '.dispatch_pending_invitations', return_value=Ok(None)) as dispatch,
    ):
        yield


def _make_match(*, confirmed: bool) -> TournamentMatch:
    return TournamentMatch(
        id=MATCH_ID, tournament_id=TOURNAMENT_ID, group_order=None,
        match_order=0, round=None, next_match_id=None,
        confirmed_by=USER_ID if confirmed else None, created_at=datetime.now(UTC),
    )


def _played_pair() -> list[TournamentMatchToContestant]:
    """A played match; fewer real contestants is a walkover."""
    return [
        TournamentMatchToContestant(
            id=TournamentMatchToContestantID(generate_uuid()),
            tournament_match_id=MATCH_ID, created_at=datetime.now(UTC),
            participant_id=TournamentParticipantID(generate_uuid()),
            team_id=None,
            score=score,
        )
        for score in (3, 1)
    ]


def _counting_repo(*, confirmed: bool) -> tuple[MagicMock, list[str]]:
    """A repository mock that records every full-bracket read."""
    reads: list[str] = []
    mock_repo = MagicMock()

    def _fresh(_tid):
        reads.append('bracket_read')
        return [_make_match(confirmed=confirmed)]

    mock_repo.get_matches_for_tournament_ordered_fresh.side_effect = _fresh
    mock_repo.get_matches_for_tournament_ordered.side_effect = _fresh

    match = _make_match(confirmed=confirmed)
    mock_repo.find_match.return_value = match
    mock_repo.find_match_fresh.return_value = match
    mock_repo.get_match.return_value = match
    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = _played_pair()
    mock_repo.get_matches_by_ids.return_value = []
    mock_repo.get_contestants_for_matches.return_value = {}
    return mock_repo, reads


# ------------------------------------------------------------------ #
# correct_match_result
# ------------------------------------------------------------------ #


def test_correction_locks_the_reachable_set_exactly_once():
    """One ordered acquisition for the whole correction."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo, _reads = _counting_repo(confirmed=True)

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._snapshot_contestant_scores', return_value={}),
        patch(f'{_S}.create_log_entry'),
        patch(f'{_S}._unconfirm_match_flush') as mock_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply,
        patch(f'{_S}.match_unconfirmed'),
        patch(f'{_S}.match_deleted'),
        patch(f'{_S}.match_confirmed'),
        patch(f'{_S}.contestant_advanced'),
        patch(f'{_S}.match_created'),
        patch(f'{_S}.match_ready'),
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Ok({'row': 3})
        mock_flush.return_value = Ok(([], [], False, TOURNAMENT_ID))
        mock_apply.return_value = Ok(
            (MagicMock(), None, [], [], [])
        )

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='typo in the score',
            corrected_scores={PARTICIPANT_A: 3},
        )

    assert result.is_ok()
    mock_lock.assert_called_once_with(MATCH_ID)
    # And the apply step is told so, rather than locking again.
    assert mock_apply.call_args.kwargs['_locks_held'] is True


def test_correction_classifies_exactly_once():
    """Reuse the gate's classification for the cascade score snapshot."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo, _reads = _counting_repo(confirmed=True)
    classification = (CorrectionCase.NO_DOWNSTREAM, [])

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._lock_reachable_matches'),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._snapshot_contestant_scores', return_value={}),
        patch(f'{_S}.create_log_entry'),
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply,
        patch(f'{_S}.match_unconfirmed'),
        patch(f'{_S}.match_deleted'),
        patch(f'{_S}.match_confirmed'),
        patch(f'{_S}.contestant_advanced'),
        patch(f'{_S}.match_created'),
        patch(f'{_S}.match_ready'),
    ):
        mock_classify.return_value = Ok(classification)
        mock_validate.return_value = Ok({'row': 3})
        mock_apply.return_value = Ok((MagicMock(), None, [], [], []))

        tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='typo in the score',
            corrected_scores={PARTICIPANT_A: 3},
        )

    # Once, for the gate.
    mock_classify.assert_called_once_with(MATCH_ID)


def test_snapshot_reuses_a_supplied_classification():
    """_snapshot_cascade_scores does not re-walk when handed one."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo, _reads = _counting_repo(confirmed=True)

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
    ):
        result = tournament_match_service._snapshot_cascade_scores(
            MATCH_ID,
            classification=(CorrectionCase.NO_DOWNSTREAM, []),
        )

    assert result == {}
    mock_classify.assert_not_called()


def test_snapshot_still_classifies_when_given_nothing():
    """Classify in the snapshot when no classification is passed."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo, _reads = _counting_repo(confirmed=True)

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))

        tournament_match_service._snapshot_cascade_scores(MATCH_ID)

    mock_classify.assert_called_once_with(MATCH_ID)


# ------------------------------------------------------------------ #
# end-to-end read counts, with the real lock helper in play
# ------------------------------------------------------------------ #


def test_score_submission_reads_the_bracket_twice_not_four_times():
    """`set_match_scores` locks once and tells `confirm_match` so."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo, reads = _counting_repo(confirmed=False)
    # A real dataclass: `set_match_scores` uses `dataclasses.replace()`.
    contestant = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=PARTICIPANT_A,
        score=None,
        created_at=datetime.now(UTC),
    )
    mock_repo.get_contestants_for_match.return_value = [contestant]

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._resolve_initiator_contestant', return_value=contestant),
        patch(f'{_S}.determine_match_winner') as mock_winner,
    ):
        # A draw skips the loser-only check and reaches `confirm_match`.
        mock_winner.return_value = Ok(None)

        tournament_match_service.set_match_scores(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    assert reads.count('bracket_read') == 2
