"""
tests.unit.services.lan_tournament.test_correction_query_cost
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pin the number of full-bracket reads a correction and a score
submission cost.

Both ``_lock_reachable_matches`` and ``classify_result_correction``
read every match of the tournament. ``_lock_reachable_matches`` reads
it twice per call -- once to walk the reachable set, once to re-verify
the walk under the lock it just took. Those reads, not the row locks,
are the expensive part: a 256-entrant double-elimination bracket is
511 rows, materialised into dataclasses each time.

Before the fix these stacked up. ``correct_match_result`` took the
ordered lock itself, then ``_admin_set_and_confirm_match_impl`` took
it again, then ``_confirm_match_impl`` took it a third time; and the
classification ran once for the acknowledgement gate and a second
time inside ``_snapshot_cascade_scores``. Eight full-bracket reads for
one button press, and four for a player submitting a score.

The fix threads ``_locks_held`` and ``_classification`` down the
in-module call chain. Both default to the safe value, so a caller
that forgets pays for the work again rather than losing the lock.

These tests pin the resulting counts. They are deliberately exact: a
regression here is invisible in behaviour and shows up only as a
slower admin page under tournament load.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
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


def _make_match(*, confirmed: bool) -> MagicMock:
    m = MagicMock()
    m.id = MATCH_ID
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = USER_ID if confirmed else None
    m.next_match_id = None
    m.loser_next_match_id = None
    m.bracket = None
    return m


def _played_pair() -> list[MagicMock]:
    """A played match; fewer real contestants is a walkover."""
    return [
        MagicMock(
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
    """One ordered acquisition for the whole correction.

    ``correct_match_result`` holds the lock from before the
    classification until the single commit at the end, so neither
    ``_admin_set_and_confirm_match_impl`` nor ``_confirm_match_impl``
    beneath it has anything to re-acquire.
    """
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
    """The acknowledgement gate's classification is reused for the
    cascade score snapshot.

    Nothing commits between the two, so a second walk could only
    return the same answer -- and reusing it makes the audit entry
    and the admin's warning describe the same match set by
    construction rather than by two walks agreeing.
    """
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

    # Once, for the gate. _unconfirm_match_flush ran for real here and
    # handed the same tuple to _snapshot_cascade_scores instead of
    # asking again.
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
    """The unconfirm_match path passes no classification and must
    still get one -- the reuse is an optimisation for the caller that
    has one, not a new requirement."""
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
    """set_match_scores locks once and tells confirm_match so.

    Two reads is the floor for one ``_lock_reachable_matches`` call:
    walk, then re-verify under the lock. Four means the confirm path
    recomputed a set the caller already holds.
    """
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo, reads = _counting_repo(confirmed=False)
    # A real dataclass, not a MagicMock: set_match_scores builds its
    # proposed contestants with dataclasses.replace().
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
        # A draw: set_match_scores's own loser-only check is skipped,
        # so it proceeds into confirm_match, which is the second
        # lock site we care about.
        mock_winner.return_value = Ok(None)

        tournament_match_service.set_match_scores(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    assert reads.count('bracket_read') == 2
