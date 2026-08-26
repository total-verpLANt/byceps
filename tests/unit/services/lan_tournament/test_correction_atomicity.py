"""
tests.unit.services.lan_tournament.test_correction_atomicity
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

`correct_match_result` retracts and re-applies scores in one
transaction; `unconfirm_match` and `admin_set_and_confirm_match` keep
their own commit and dispatch.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    MatchInvitationID,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'
_R = 'byceps.services.lan_tournament.tournament_readiness_service'

MATCH_ID = TournamentMatchID(generate_uuid())
READY_MATCH_ID = TournamentMatchID(generate_uuid())
DELETED_MATCH_ID = TournamentMatchID(generate_uuid())
TOURNAMENT_ID = generate_uuid()
USER_ID = generate_uuid()
PARTICIPANT_A = TournamentParticipantID(generate_uuid())
PENDING_INVITATION_IDS = (
    MatchInvitationID(generate_uuid()),
    MatchInvitationID(generate_uuid()),
)


def _make_match() -> MagicMock:
    m = MagicMock()
    m.id = MATCH_ID
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = USER_ID
    m.next_match_id = None
    m.loser_next_match_id = None
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


def _patched_correction() -> MagicMock:
    """Return a repository mock that lets a correction reach its apply step."""
    mock_repo = MagicMock()
    match = _make_match()
    mock_repo.find_match.return_value = match
    mock_repo.find_match_fresh.return_value = match
    mock_repo.get_match.return_value = match
    mock_repo.get_matches_for_tournament_ordered.return_value = [match]
    mock_repo.get_matches_for_tournament_ordered_fresh.return_value = [match]
    mock_repo.get_contestants_for_match.return_value = _played_pair()
    return mock_repo


def _record(calls: list[str], tag: str):
    """Return a signal `send` stand-in that logs `tag` on the timeline."""

    def send(*args, **kwargs):
        calls.append(tag)

    return send


@dataclass
class _InvitationBoundary:
    reconciled: list[set[TournamentMatchID]] = field(default_factory=list)
    dispatched: list[tuple[MatchInvitationID, ...]] = field(
        default_factory=list
    )


@contextmanager
def _invitation_boundary(calls: list[str]) -> Iterator[_InvitationBoundary]:
    """Isolate the readiness service's invitation reconcile and dispatch.

    Reconcile returns the typed ID tuple of the real function. Both
    calls land on the caller's timeline, so a test can pin that
    reconcile runs before the commit and dispatch after it.
    """
    boundary = _InvitationBoundary()

    def reconcile(match_ids, *, occurred_at):
        calls.append('reconcile_invitations')
        boundary.reconciled.append(set(match_ids))
        return Ok(PENDING_INVITATION_IDS)

    def dispatch(invitation_ids):
        calls.append('dispatch_invitations')
        boundary.dispatched.append(tuple(invitation_ids))
        return Ok(None)

    with (
        patch(
            f'{_R}.reconcile_invitations_flush',
            autospec=True,
            side_effect=reconcile,
        ),
        patch(
            f'{_R}.dispatch_pending_invitations',
            autospec=True,
            side_effect=dispatch,
        ),
    ):
        yield boundary


# ------------------------------------------------------------------ #
# success: one commit, every event dispatched only after it
# ------------------------------------------------------------------ #


def test_success_path_commits_once_and_dispatches_every_event_after_it():
    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []
    mock_repo = _patched_correction()
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')

    retract_event = MagicMock(name='retract_event', match_id=MATCH_ID)
    deleted_event = MagicMock(name='deleted_event', match_id=DELETED_MATCH_ID)
    confirmed_event = MagicMock(name='confirmed_event')
    completed_event = MagicMock(name='completed_event')
    adv_event = MagicMock(name='adv_event')
    created_event = MagicMock(name='created_event')
    ready_event = MagicMock(name='ready_event', match_id=READY_MATCH_ID)

    with (
        _invitation_boundary(calls) as invitations,
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._snapshot_contestant_scores') as mock_snapshot,
        patch(f'{_S}.create_log_entry'),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply_impl,
        patch(f'{_S}.match_unconfirmed') as sig_unconfirmed,
        patch(f'{_S}.match_deleted') as sig_deleted,
        patch(f'{_S}.tournament_uncompleted') as sig_uncompleted,
        patch(f'{_S}.match_confirmed') as sig_confirmed,
        patch(f'{_S}.tournament_completed') as sig_completed,
        patch(f'{_S}.contestant_advanced') as sig_advanced,
        patch(f'{_S}.match_created') as sig_created,
        patch(f'{_S}.match_ready') as sig_ready,
    ):
        for sig, tag in (
            (sig_unconfirmed, 'unconfirmed'),
            (sig_deleted, 'deleted'),
            (sig_uncompleted, 'uncompleted'),
            (sig_confirmed, 'confirmed'),
            (sig_completed, 'completed'),
            (sig_advanced, 'advanced'),
            (sig_created, 'created'),
            (sig_ready, 'ready'),
        ):
            sig.send.side_effect = _record(calls, tag)

        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Ok({'row': 3})
        mock_snapshot.return_value = {}
        # The correction both reverts and re-completes the tournament.
        mock_unconfirm_flush.return_value = Ok(
            ([retract_event], [deleted_event], True, TOURNAMENT_ID)
        )
        mock_apply_impl.return_value = Ok(
            (
                confirmed_event,
                completed_event,
                [adv_event],
                [created_event],
                [ready_event],
            )
        )

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='swapped scores',
            corrected_scores={PARTICIPANT_A: 3},
        )

    assert result.is_ok()
    # Invitations are reconciled in the transaction, then the one commit.
    assert calls[:2] == ['reconcile_invitations', 'commit']
    assert calls.count('commit') == 1
    # Every collected event dispatched, all strictly after the commit.
    assert set(calls[2:-1]) == {
        'unconfirmed',
        'deleted',
        'uncompleted',
        'confirmed',
        'completed',
        'advanced',
        'created',
        'ready',
    }
    # The reconciled invitations are dispatched last, after the events.
    assert calls[-1] == 'dispatch_invitations'
    # Retracted and newly ready matches, minus the deleted one.
    assert invitations.reconciled == [{MATCH_ID, READY_MATCH_ID}]
    assert invitations.dispatched == [PENDING_INVITATION_IDS]
    sig_unconfirmed.send.assert_called_once_with(None, event=retract_event)
    sig_confirmed.send.assert_called_once_with(None, event=confirmed_event)


def test_success_path_without_corrected_scores_dispatches_only_retraction_events():
    """A retract-only correction fires no confirm-side events."""
    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []
    mock_repo = _patched_correction()
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')

    retract_event = MagicMock(name='retract_event', match_id=MATCH_ID)

    with (
        _invitation_boundary(calls) as invitations,
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply_impl,
        patch(f'{_S}.match_unconfirmed') as sig_unconfirmed,
        patch(f'{_S}.match_confirmed') as sig_confirmed,
    ):
        sig_unconfirmed.send.side_effect = lambda *a, **kw: calls.append(
            'unconfirmed'
        )
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_unconfirm_flush.return_value = Ok(
            ([retract_event], [], False, TOURNAMENT_ID)
        )

        result = tournament_match_service.correct_match_result(
            MATCH_ID, USER_ID, reason='retract only'
        )

    assert result.is_ok()
    assert result.unwrap() == (CorrectionCase.NO_DOWNSTREAM, False)
    assert calls == [
        'reconcile_invitations',
        'commit',
        'unconfirmed',
        'dispatch_invitations',
    ]
    assert invitations.reconciled == [{MATCH_ID}]
    assert invitations.dispatched == [PENDING_INVITATION_IDS]
    mock_apply_impl.assert_not_called()
    sig_confirmed.send.assert_not_called()


# ------------------------------------------------------------------ #
# apply failure: the retraction is undone with it, nothing dispatched
# ------------------------------------------------------------------ #


def test_apply_failure_rolls_back_and_dispatches_nothing():
    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []
    mock_repo = _patched_correction()
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')
    mock_repo.rollback_session.side_effect = lambda: calls.append(
        'rollback'
    )

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._snapshot_contestant_scores') as mock_snapshot,
        patch(f'{_S}.create_log_entry'),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply_impl,
        patch(f'{_S}.match_unconfirmed') as sig_unconfirmed,
        patch(f'{_S}.match_confirmed') as sig_confirmed,
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Ok({'row': 3})
        mock_snapshot.return_value = {}
        mock_unconfirm_flush.return_value = Ok(
            ([MagicMock()], [], False, TOURNAMENT_ID)
        )
        mock_apply_impl.return_value = Err('Match is already confirmed.')

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='swapped scores',
            corrected_scores={PARTICIPANT_A: 3},
        )

    assert result.is_err()
    # Returned verbatim, so the view can translate it.
    assert result.unwrap_err() == 'Match is already confirmed.'
    # No commit ever happened -- only the rollback.
    assert calls == ['rollback']
    sig_unconfirmed.send.assert_not_called()
    sig_confirmed.send.assert_not_called()


def test_apply_step_raising_rolls_back_before_the_exception_escapes():
    """An exception from the apply step also undoes the retraction."""
    import pytest

    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []
    mock_repo = _patched_correction()
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')
    mock_repo.rollback_session.side_effect = lambda: calls.append(
        'rollback'
    )

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._snapshot_contestant_scores') as mock_snapshot,
        patch(f'{_S}.create_log_entry'),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply_impl,
        patch(f'{_S}.match_unconfirmed') as sig_unconfirmed,
        patch(f'{_S}.match_confirmed') as sig_confirmed,
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Ok({'row': 3})
        mock_snapshot.return_value = {}
        mock_unconfirm_flush.return_value = Ok(
            ([MagicMock()], [], False, TOURNAMENT_ID)
        )
        mock_apply_impl.side_effect = RuntimeError('deadlock detected')

        with pytest.raises(RuntimeError, match='deadlock detected'):
            tournament_match_service.correct_match_result(
                MATCH_ID,
                USER_ID,
                reason='swapped scores',
                corrected_scores={PARTICIPANT_A: 3},
            )

    assert calls == ['rollback']
    sig_unconfirmed.send.assert_not_called()
    sig_confirmed.send.assert_not_called()


# ------------------------------------------------------------------ #
# unconfirm_match / admin_set_and_confirm_match own their commit
# ------------------------------------------------------------------ #


def test_unconfirm_match_still_owns_a_single_commit_and_dispatch():
    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []
    mock_repo = MagicMock()
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')

    unconfirmed_event = MagicMock(match_id=MATCH_ID)

    with (
        _invitation_boundary(calls) as invitations,
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._lock_reachable_matches'),
        patch(f'{_S}._unconfirm_match_flush') as mock_flush,
        patch(f'{_S}.match_unconfirmed') as sig_unconfirmed,
    ):
        sig_unconfirmed.send.side_effect = lambda *a, **kw: calls.append(
            'dispatch'
        )
        mock_flush.return_value = Ok(
            ([unconfirmed_event], [], False, TOURNAMENT_ID)
        )

        result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    assert calls == [
        'reconcile_invitations',
        'commit',
        'dispatch',
        'dispatch_invitations',
    ]
    assert invitations.reconciled == [{MATCH_ID}]
    assert invitations.dispatched == [PENDING_INVITATION_IDS]
    sig_unconfirmed.send.assert_called_once_with(
        None, event=unconfirmed_event
    )


def test_unconfirm_match_rolls_back_on_any_flush_failure():
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = MagicMock()

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._lock_reachable_matches'),
        patch(f'{_S}._unconfirm_match_flush') as mock_flush,
    ):
        mock_flush.return_value = Err('Match is not confirmed.')

        result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    mock_repo.rollback_session.assert_called_once()
    mock_repo.commit_session.assert_not_called()


def test_admin_set_and_confirm_match_still_owns_a_single_commit_and_dispatch():
    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []
    mock_repo = MagicMock()
    mock_repo.commit_session.side_effect = lambda: calls.append('commit')

    confirmed_event = MagicMock()

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_impl,
        patch(f'{_S}.match_confirmed') as sig_confirmed,
        patch(f'{_S}.create_log_entry'),
    ):
        sig_confirmed.send.side_effect = lambda *a, **kw: calls.append(
            'dispatch'
        )
        mock_impl.return_value = Ok((confirmed_event, None, [], [], []))

        result = tournament_match_service.admin_set_and_confirm_match(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    assert result.is_ok()
    assert calls == ['commit', 'dispatch']
    sig_confirmed.send.assert_called_once_with(None, event=confirmed_event)


def test_admin_set_and_confirm_match_rolls_back_on_any_impl_failure():
    """Roll back on any `Err` from the flush-only core."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = MagicMock()

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_impl,
    ):
        mock_impl.return_value = Err('Match is already confirmed.')

        result = tournament_match_service.admin_set_and_confirm_match(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    assert result.is_err()
    mock_repo.rollback_session.assert_called_once()
    mock_repo.commit_session.assert_not_called()
