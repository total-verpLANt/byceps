"""
tests.unit.services.lan_tournament.test_correction_classification_gaps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the two cascade effects ``classify_result_correction``
used to miss, and for the lock discipline both entry points into the
retraction cascade now share.

The classifier drives an acknowledgement gate, so anything the cascade
does that the classifier does not report is a destructive act an admin
was never warned about:

* correcting DE grand final M1 DELETES the bracket-reset match GF M2,
  it does not retract it -- and that is the normal post-reset state;
* the structural-DEFWIN undo strips the loser's auto-advanced row from
  an unconfirmed LB match's own next match, which the confirmed-only
  recursion never visits.
"""

from unittest.mock import MagicMock, patch

from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.util.result import Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'

TOURNAMENT_ID = generate_uuid()
USER_ID = generate_uuid()


def _match(
    *,
    confirmed=False,
    bracket=None,
    match_order=0,
    next_match_id=None,
    loser_next_match_id=None,
) -> MagicMock:
    m = MagicMock()
    m.id = TournamentMatchID(generate_uuid())
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = USER_ID if confirmed else None
    m.bracket = bracket
    m.match_order = match_order
    m.next_match_id = next_match_id
    m.loser_next_match_id = loser_next_match_id
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


def _repo_for(subject, matches) -> MagicMock:
    mock_repo = MagicMock()
    # find_match / get_matches_for_tournament_ordered back
    # _lock_reachable_matches's pre-lock subject check and (for
    # callers who never reach a second round) its first refresh
    # round. classify_result_correction and every later refresh round
    # go through the _fresh (populate_existing) variants instead --
    # see workspace-ubjc. Both are wired to the same fixture data so
    # tests exercising either call site see consistent state.
    mock_repo.find_match.return_value = subject
    mock_repo.find_match_fresh.return_value = subject
    mock_repo.get_matches_for_tournament_ordered.return_value = matches
    mock_repo.get_matches_for_tournament_ordered_fresh.return_value = matches
    mock_repo.get_contestants_for_match.return_value = _played_pair()
    return mock_repo


def _classify(subject, matches):
    from byceps.services.lan_tournament import tournament_match_service

    with patch(f'{_S}.tournament_repository', _repo_for(subject, matches)):
        return tournament_match_service.classify_result_correction(subject.id)


# ------------------------------------------------------------------ #
# DE bracket reset: GF M2 is deleted, not retracted
# ------------------------------------------------------------------ #


def test_gf_m1_with_pending_gf_m2_is_reported_as_a_deletion():
    """The normal post-reset state.

    GF M1 confirmed, GF M2 created and pending. The cascade deletes
    GF M2 outright; reporting UNCONFIRMED_DOWNSTREAM would tell the
    admin it merely loses a contestant, and would let the deletion
    through with no acknowledgement at all.
    """
    gf_m2 = _match(bracket=Bracket.GRAND_FINAL, match_order=1)
    gf_m1 = _match(
        confirmed=True,
        bracket=Bracket.GRAND_FINAL,
        match_order=0,
        next_match_id=gf_m2.id,
    )

    result = _classify(gf_m1, [gf_m1, gf_m2])

    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.BRACKET_RESET_DELETION
    assert affected == [gf_m2.id]


def test_gf_m1_with_confirmed_gf_m2_is_still_reported_as_a_deletion():
    """Deletion outranks retraction.

    A confirmed GF M2 is both unconfirmed AND deleted by the cascade.
    CONFIRMED_DOWNSTREAM would describe only the first half.
    """
    gf_m2 = _match(confirmed=True, bracket=Bracket.GRAND_FINAL, match_order=1)
    gf_m1 = _match(
        confirmed=True,
        bracket=Bracket.GRAND_FINAL,
        match_order=0,
        next_match_id=gf_m2.id,
    )

    case, affected = _classify(gf_m1, [gf_m1, gf_m2]).unwrap()

    assert case is CorrectionCase.BRACKET_RESET_DELETION
    assert affected == [gf_m2.id]


def test_gf_m1_pointing_at_a_non_reset_match_is_unchanged():
    """Only the GF M1 -> GF M2 shape triggers the deletion cleanup.

    Guards against classifying every grand final as a deletion.
    """
    downstream = _match(bracket=Bracket.WINNERS, match_order=1)
    gf_m1 = _match(
        confirmed=True,
        bracket=Bracket.GRAND_FINAL,
        match_order=0,
        next_match_id=downstream.id,
    )

    case, affected = _classify(gf_m1, [gf_m1, downstream]).unwrap()

    assert case is CorrectionCase.UNCONFIRMED_DOWNSTREAM
    assert affected == [downstream.id]


def test_ordinary_match_classification_is_unchanged():
    """Regression guard for the non-grand-final path."""
    downstream = _match(bracket=Bracket.WINNERS)
    subject = _match(
        confirmed=True, bracket=Bracket.WINNERS, next_match_id=downstream.id
    )

    case, affected = _classify(subject, [subject, downstream]).unwrap()

    assert case is CorrectionCase.UNCONFIRMED_DOWNSTREAM
    assert affected == [downstream.id]


# ------------------------------------------------------------------ #
# structural-DEFWIN undo reaches a grandchild
# ------------------------------------------------------------------ #


def test_confirmed_grandchild_of_unconfirmed_lb_match_is_reported():
    """_unconfirm_match_impl deletes the loser's auto-advanced row from
    ``loser_next_match.next_match_id`` regardless of whether the LB
    match itself is confirmed. If that grandchild is confirmed, its
    contestant set is mutated without being retracted -- so the gate
    must demand an acknowledgement."""
    grandchild = _match(confirmed=True, bracket=Bracket.LOSERS)
    lb_match = _match(bracket=Bracket.LOSERS, next_match_id=grandchild.id)
    subject = _match(
        confirmed=True,
        bracket=Bracket.WINNERS,
        loser_next_match_id=lb_match.id,
    )

    case, affected = _classify(
        subject, [subject, lb_match, grandchild]
    ).unwrap()

    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert affected == [lb_match.id, grandchild.id]


def test_unconfirmed_grandchild_does_not_escalate_the_case():
    """The grandchild is reported, but an unconfirmed one is no more
    critical than the LB match feeding it."""
    grandchild = _match(bracket=Bracket.LOSERS)
    lb_match = _match(bracket=Bracket.LOSERS, next_match_id=grandchild.id)
    subject = _match(
        confirmed=True,
        bracket=Bracket.WINNERS,
        loser_next_match_id=lb_match.id,
    )

    case, affected = _classify(
        subject, [subject, lb_match, grandchild]
    ).unwrap()

    assert case is CorrectionCase.UNCONFIRMED_DOWNSTREAM
    assert affected == [lb_match.id, grandchild.id]


def test_grandchild_is_not_collected_through_the_winner_edge():
    """Only the loser edge carries the structural-DEFWIN undo. Walking
    grandchildren of an unconfirmed winner-edge match would over-report
    and demand acknowledgements the cascade never earns."""
    grandchild = _match(confirmed=True, bracket=Bracket.WINNERS)
    downstream = _match(bracket=Bracket.WINNERS, next_match_id=grandchild.id)
    subject = _match(
        confirmed=True, bracket=Bracket.WINNERS, next_match_id=downstream.id
    )

    case, affected = _classify(
        subject, [subject, downstream, grandchild]
    ).unwrap()

    assert case is CorrectionCase.UNCONFIRMED_DOWNSTREAM
    assert affected == [downstream.id]


# ------------------------------------------------------------------ #
# acknowledgement gate
# ------------------------------------------------------------------ #


def test_bracket_reset_deletion_requires_acknowledgement():
    from byceps.services.lan_tournament import tournament_match_service

    subject = _match(confirmed=True, bracket=Bracket.GRAND_FINAL)
    mock_repo = _repo_for(subject, [subject])

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}.unconfirm_match') as mock_unconfirm,
    ):
        mock_classify.return_value = Ok(
            (CorrectionCase.BRACKET_RESET_DELETION, [])
        )

        result = tournament_match_service.correct_match_result(
            subject.id, USER_ID, reason='wrong winner'
        )

    assert result.is_err()
    assert 'bracket-reset match' in result.unwrap_err()
    mock_unconfirm.assert_not_called()
    mock_repo.rollback_session.assert_called_once()


def test_bracket_reset_deletion_proceeds_once_acknowledged():
    from byceps.services.lan_tournament import tournament_match_service

    subject = _match(confirmed=True, bracket=Bracket.GRAND_FINAL)
    mock_repo = _repo_for(subject, [subject])

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        # correct_match_result retracts via the flush-only core
        # directly (workspace-ukkj) -- not the committing
        # unconfirm_match -- so that is the seam to mock here.
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
    ):
        mock_classify.return_value = Ok(
            (CorrectionCase.BRACKET_RESET_DELETION, [])
        )
        mock_unconfirm_flush.return_value = Ok(
            ([], [], False, TOURNAMENT_ID)
        )

        result = tournament_match_service.correct_match_result(
            subject.id, USER_ID, reason='wrong winner', ack_critical=True
        )

    assert result.is_ok()
    assert result.unwrap() == (CorrectionCase.BRACKET_RESET_DELETION, False)
    mock_unconfirm_flush.assert_called_once()


# ------------------------------------------------------------------ #
# shared lock discipline
# ------------------------------------------------------------------ #


def test_lock_set_is_recomputed_after_locking():
    """The reachable set comes from an unlocked read. A concurrently
    committed edge -- a bracket reset wiring GF M1 to a fresh GF M2 --
    can make a match reachable that the first pass did not lock, so the
    helper re-reads and re-locks until the set stops growing."""
    from byceps.services.lan_tournament import tournament_match_service

    late = _match(bracket=Bracket.GRAND_FINAL, match_order=1)
    before = _match(confirmed=True, bracket=Bracket.GRAND_FINAL)
    after = _match(confirmed=True, bracket=Bracket.GRAND_FINAL)
    after.id = before.id
    after.next_match_id = late.id

    mock_repo = MagicMock()
    mock_repo.find_match.return_value = before
    # The loop's re-read is the _fresh (populate_existing) variant --
    # see workspace-ubjc. A plain get_matches_for_tournament_ordered
    # would never observe "late" becoming reachable: production code
    # no longer calls it here at all.
    mock_repo.get_matches_for_tournament_ordered_fresh.side_effect = [
        [before],
        [after, late],
        [after, late],
    ]
    locked = []
    mock_repo.lock_matches_for_update.side_effect = lambda ids: locked.append(
        list(ids)
    )

    with patch(f'{_S}.tournament_repository', mock_repo):
        tournament_match_service._lock_reachable_matches(before.id)

    assert len(locked) == 2
    assert locked[0] == [before.id]
    assert set(locked[1]) == {before.id, late.id}
    # Re-locking passes the whole set, not just the new IDs, so the
    # acquisition order stays stable across both statements.
    assert locked[1] == sorted(locked[1])


def test_lock_helper_tolerates_an_unknown_match():
    """It locks nothing and stays silent; both callers keep their own
    not-found contract (unconfirm_match raises, correct_match_result
    returns Err)."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = MagicMock()
    mock_repo.find_match.return_value = None

    with patch(f'{_S}.tournament_repository', mock_repo):
        tournament_match_service._lock_reachable_matches(
            TournamentMatchID(generate_uuid())
        )

    mock_repo.lock_matches_for_update.assert_not_called()


def test_unconfirm_match_locks_the_reachable_set_first():
    """The cascade takes per-node locks in traversal order. Unless this
    path takes the whole set in id order first, it and
    correct_match_result can acquire the same rows in opposing orders
    and deadlock."""
    from byceps.services.lan_tournament import tournament_match_service

    subject = _match(confirmed=True, bracket=Bracket.WINNERS)
    calls = []

    mock_repo = _repo_for(subject, [subject])
    mock_repo.lock_matches_for_update.side_effect = lambda ids: calls.append(
        'bulk_lock'
    )
    mock_repo.get_match_for_update.side_effect = lambda _mid: (
        calls.append('row_lock') or subject
    )

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._unconfirm_match_impl') as mock_impl,
    ):
        mock_impl.return_value = Ok(([], [], False))

        result = tournament_match_service.unconfirm_match(
            subject.id, USER_ID
        )

    assert result.is_ok()
    assert calls == ['bulk_lock', 'row_lock']


def test_correction_uses_a_single_lock_acquisition_and_commit():
    """Fixed workspace-ukkj: correct_match_result no longer spans two
    committed transactions. Previously unconfirm_match's own commit
    released every lock taken before it, forcing a second lock
    acquisition before the re-confirm; now the whole correction --
    retraction and score re-application -- is one transaction, so
    exactly one lock acquisition and one commit cover it."""
    from byceps.services.lan_tournament import tournament_match_service

    subject = _match(confirmed=True, bracket=Bracket.WINNERS)
    calls = []

    mock_repo = _repo_for(subject, [subject])
    mock_repo.lock_matches_for_update.side_effect = lambda ids: calls.append(
        'lock'
    )
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._snapshot_contestant_scores') as mock_snapshot,
        patch(f'{_S}.create_log_entry'),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply_impl,
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Ok({'row': 3})
        mock_snapshot.return_value = {}
        mock_unconfirm_flush.return_value = Ok(
            ([], [], False, TOURNAMENT_ID)
        )
        mock_apply_impl.return_value = Ok((MagicMock(), None, [], [], []))

        result = tournament_match_service.correct_match_result(
            subject.id,
            USER_ID,
            reason='swapped scores',
            corrected_scores={generate_uuid(): 3},
        )

    assert result.is_ok()
    assert calls == ['lock', 'commit']
    mock_unconfirm_flush.assert_called_once()
    mock_apply_impl.assert_called_once()
