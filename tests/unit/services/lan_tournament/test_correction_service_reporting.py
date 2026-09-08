"""
tests.unit.services.lan_tournament.test_correction_service_reporting
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for ``correct_match_result``'s failure reporting and for
the single-fetch contract between ``_validate_match_scores`` and
``admin_set_and_confirm_match``.

``correct_match_result`` is one transaction (workspace-ukkj): the
retraction and the corrected-score application share a single commit,
via the flush-only ``_unconfirm_match_flush`` /
``_admin_set_and_confirm_match_impl`` cores rather than the committing
``unconfirm_match`` / ``admin_set_and_confirm_match`` themselves. When
the apply step fails, the retraction is rolled back with it -- so the
error must not claim the result was retracted. It is returned as the
bare inner msgid; the "nothing was changed" reassurance belongs to the
view's translatable wrapper, since it holds for every ``Err`` this
function returns.
"""

from dataclasses import replace
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
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'

MATCH_ID = TournamentMatchID(generate_uuid())
TOURNAMENT_ID = generate_uuid()
USER_ID = generate_uuid()
PARTICIPANT_A = TournamentParticipantID(generate_uuid())
PARTICIPANT_B = TournamentParticipantID(generate_uuid())


def _make_contestant(participant_id) -> TournamentMatchToContestant:
    """A real dataclass -- _validate_match_scores calls replace() on it."""
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=participant_id,
        score=None,
        created_at=datetime.now(UTC),
    )


def _played_pair() -> list[TournamentMatchToContestant]:
    """A played match; fewer real contestants is a walkover."""
    return [
        replace(_make_contestant(PARTICIPANT_A), score=3),
        replace(_make_contestant(PARTICIPANT_B), score=1),
    ]


def _make_match() -> MagicMock:
    m = MagicMock()
    m.id = MATCH_ID
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = USER_ID
    m.next_match_id = None
    m.loser_next_match_id = None
    return m


# ------------------------------------------------------------------ #
# half-applied correction is reported honestly
# ------------------------------------------------------------------ #


def test_correct_match_result_error_states_nothing_changed_on_apply_failure():
    """Fixed workspace-ukkj: correct_match_result is one transaction.

    A failure applying the corrected scores rolls back the retraction
    too -- there is no intermediate commit left to make it durable --
    so the error must no longer claim the result was retracted; the
    original, confirmed result is untouched.
    """
    from byceps.services.lan_tournament import tournament_match_service

    scores = {PARTICIPANT_A: 3, PARTICIPANT_B: 1}

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm_flush,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply_impl,
        patch(f'{_S}.create_log_entry'),
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Ok({})
        mock_repo.get_match.return_value = _make_match()
        mock_repo.get_contestants_for_match.return_value = _played_pair()
        mock_unconfirm_flush.return_value = Ok(
            ([], [], False, TOURNAMENT_ID)
        )
        mock_apply_impl.return_value = Err('Match is already confirmed.')

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='scorekeeper error',
            corrected_scores=scores,
        )

        # One rollback undoes the whole transaction -- retraction
        # included, since it was never committed separately.
        mock_repo.rollback_session.assert_called_once()
        mock_repo.commit_session.assert_not_called()

    assert result.is_err()
    message = result.unwrap_err()
    assert 'retracted' not in message
    # Returned VERBATIM, not wrapped: the view translates this string
    # with gettext(), so it has to stay a catalogue msgid. Wrapping it
    # in an f-string ("The correction could not be applied: ...")
    # matched no msgid and produced a half-German flash. The
    # "nothing was changed" reassurance now lives in the view's own
    # translatable wrapper -- it holds for every Err this function
    # returns, not only this one.
    assert message == 'Match is already confirmed.'


def test_correct_match_result_validates_before_retracting():
    """Invalid scores must fail before anything destructive runs."""
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_validate.return_value = Err('Score cannot be negative.')
        mock_repo.get_match.return_value = _make_match()
        mock_repo.get_contestants_for_match.return_value = _played_pair()

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='typo',
            corrected_scores={PARTICIPANT_A: -1, PARTICIPANT_B: 1},
        )

    assert result.is_err()
    assert result.unwrap_err() == 'Score cannot be negative.'
    mock_unconfirm.assert_not_called()


def test_correct_match_result_confirmed_downstream_requires_ack():
    """A confirmed downstream match blocks until explicitly accepted."""
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        mock_repo.get_contestants_for_match.return_value = _played_pair()
        mock_classify.return_value = Ok(
            (CorrectionCase.CONFIRMED_DOWNSTREAM, [TournamentMatchID(generate_uuid())])
        )

        result = tournament_match_service.correct_match_result(
            MATCH_ID, USER_ID, reason='disputed'
        )

    assert result.is_err()
    assert 'acknowledgement' in result.unwrap_err()
    mock_unconfirm.assert_not_called()


def test_correct_match_result_rejects_blank_reason():
    """A whitespace-only reason is not a reason."""
    from byceps.services.lan_tournament import tournament_match_service

    with patch(f'{_S}.classify_result_correction') as mock_classify:
        result = tournament_match_service.correct_match_result(
            MATCH_ID, USER_ID, reason='   '
        )

    assert result.is_err()
    assert 'reason is required' in result.unwrap_err()
    mock_classify.assert_not_called()


# ------------------------------------------------------------------ #
# contestants are resolved once, not twice
# ------------------------------------------------------------------ #


def test_admin_set_and_confirm_fetches_contestants_once():
    """The write path reuses the map _validate_match_scores resolved."""
    from byceps.services.lan_tournament import tournament_match_service

    match = _make_match()
    match.confirmed_by = None

    contestant_a = _make_contestant(PARTICIPANT_A)
    contestant_b = _make_contestant(PARTICIPANT_B)

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        # admin_set_and_confirm_match's write path delegates to the
        # flush-only _confirm_match_impl (not the committing
        # confirm_match) -- see workspace-ukkj -- so that is the seam
        # to mock to keep this test isolated to the score-write step.
        patch(f'{_S}._confirm_match_impl') as mock_confirm_impl,
        patch(f'{_S}.create_log_entry'),
    ):
        mock_repo.get_match_for_update.return_value = match
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_match.return_value = match
        mock_repo.get_contestants_for_match.return_value = [
            contestant_a,
            contestant_b,
        ]
        tournament = MagicMock()
        tournament.elimination_mode = None
        mock_repo.get_tournament.return_value = tournament
        mock_confirm_impl.return_value = Ok((MagicMock(), None, [], [], []))

        result = tournament_match_service.admin_set_and_confirm_match(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3, PARTICIPANT_B: 1}
        )

        assert mock_repo.get_contestants_for_match.call_count == 1

        # The score map handed to the write came from that one fetch.
        mock_repo.update_contestant_scores.assert_called_once_with(
            {contestant_a.id: 3, contestant_b.id: 1}
        )

    assert result.is_ok()


def test_validate_match_scores_returns_resolved_id_map():
    """Validation hands back contestant-row-ID -> score on success."""
    from byceps.services.lan_tournament import tournament_match_service

    contestant_a = _make_contestant(PARTICIPANT_A)
    contestant_b = _make_contestant(PARTICIPANT_B)

    with patch(f'{_S}.tournament_repository') as mock_repo:
        mock_repo.get_contestants_for_match.return_value = [
            contestant_a,
            contestant_b,
        ]
        mock_repo.get_match.return_value = _make_match()
        tournament = MagicMock()
        tournament.elimination_mode = None
        mock_repo.get_tournament.return_value = tournament

        result = tournament_match_service._validate_match_scores(
            MATCH_ID, {PARTICIPANT_A: 7, PARTICIPANT_B: 2}
        )

    assert result.is_ok()
    assert result.unwrap() == {contestant_a.id: 7, contestant_b.id: 2}


# ------------------------------------------------------------------ #
# classification walks in memory, not query-per-node
# ------------------------------------------------------------------ #


def test_classify_result_correction_uses_single_batched_fetch():
    """The BFS must not issue one find_match per bracket node."""
    from byceps.services.lan_tournament import tournament_match_service

    subject = _make_match()
    downstream_1 = _make_match()
    downstream_1.id = TournamentMatchID(generate_uuid())
    downstream_1.confirmed_by = USER_ID
    downstream_2 = _make_match()
    downstream_2.id = TournamentMatchID(generate_uuid())
    downstream_2.confirmed_by = None

    subject.next_match_id = downstream_1.id
    downstream_1.next_match_id = downstream_2.id

    with patch(f'{_S}.tournament_repository') as mock_repo:
        # classify_result_correction reads via the _fresh
        # (populate_existing) variants -- see workspace-ubjc.
        mock_repo.find_match_fresh.return_value = subject
        mock_repo.get_matches_for_tournament_ordered_fresh.return_value = [
            subject,
            downstream_1,
            downstream_2,
        ]

        result = tournament_match_service.classify_result_correction(
            MATCH_ID
        )

        # One lookup for the subject, one batched fetch. No per-node
        # find_match calls beyond the subject resolution.
        assert mock_repo.find_match_fresh.call_count == 1
        assert (
            mock_repo.get_matches_for_tournament_ordered_fresh.call_count
            == 1
        )

    assert result.is_ok()
    case, affected = result.unwrap()
    # A confirmed downstream match makes this CONFIRMED_DOWNSTREAM,
    # and the walk
    # continues past it in breadth-first order.
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert affected == [downstream_1.id, downstream_2.id]


def test_classify_result_correction_stops_at_cycle():
    """A routing cycle stops the walk instead of raising."""
    from byceps.services.lan_tournament import tournament_match_service

    subject = _make_match()
    other = _make_match()
    other.id = TournamentMatchID(generate_uuid())
    other.confirmed_by = USER_ID

    subject.next_match_id = other.id
    other.next_match_id = subject.id  # back-edge

    with patch(f'{_S}.tournament_repository') as mock_repo:
        mock_repo.find_match_fresh.return_value = subject
        mock_repo.get_matches_for_tournament_ordered_fresh.return_value = [
            subject,
            other,
        ]

        result = tournament_match_service.classify_result_correction(
            MATCH_ID
        )

    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert affected == [other.id]


def test_classify_result_correction_no_downstream():
    """A terminal match corrects freely."""
    from byceps.services.lan_tournament import tournament_match_service

    subject = _make_match()

    with patch(f'{_S}.tournament_repository') as mock_repo:
        mock_repo.find_match_fresh.return_value = subject
        mock_repo.get_matches_for_tournament_ordered_fresh.return_value = [
            subject
        ]

        result = tournament_match_service.classify_result_correction(
            MATCH_ID
        )

    assert result.is_ok()
    assert result.unwrap() == (CorrectionCase.NO_DOWNSTREAM, [])


# ------------------------------------------------------------------ #
# guards that must fire BEFORE the retraction cascade
# ------------------------------------------------------------------ #


def _patched_correction(contestants, *, elimination_mode=None):
    """Patch the repo so correct_match_result reaches its guards."""
    from byceps.services.lan_tournament.models.elimination_mode import (
        EliminationMode,
    )

    mock_repo = MagicMock()
    match = _make_match()
    mock_repo.find_match.return_value = match
    # correct_match_result's own subject fetch and
    # _lock_reachable_matches's pre-lock check use the plain reads
    # above; classify_result_correction's post-lock read uses the
    # _fresh (populate_existing) variants instead -- see
    # workspace-ubjc. Both are wired to the same match/list so either
    # call site sees consistent data.
    mock_repo.find_match_fresh.return_value = match
    mock_repo.get_match.return_value = match
    mock_repo.get_matches_for_tournament_ordered.return_value = [match]
    mock_repo.get_matches_for_tournament_ordered_fresh.return_value = [
        match
    ]
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.get_tournament.return_value.elimination_mode = (
        elimination_mode or EliminationMode.SINGLE_ELIMINATION
    )
    return mock_repo


def test_correction_rejects_match_with_one_contestant_before_retracting():
    """A DEFWIN match can never be re-confirmed, so it must not be
    retracted. Previously determine_match_winner returned Err here,
    the tie check skipped it, and the cascade committed first."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = _patched_correction([_make_contestant(PARTICIPANT_A)])

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='scorekeeper error',
            corrected_scores={PARTICIPANT_A: 3},
        )

    assert result.is_err()
    assert 'walkover' in result.unwrap_err()
    mock_unconfirm.assert_not_called()


def test_retract_only_correction_rejects_a_walkover_before_retracting():
    """Blank scores skip score validation; the guard must still fire."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = _patched_correction([_make_contestant(PARTICIPANT_A)])

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='the walkover was wrong',
            corrected_scores=None,
            ack_critical=True,
        )

    assert result.is_err()
    assert 'walkover' in result.unwrap_err()
    mock_unconfirm.assert_not_called()
    mock_repo.commit_session.assert_not_called()
    mock_repo.rollback_session.assert_called()


def test_correction_rejects_unchanged_scores_before_retracting():
    """Submitting the pre-filled scores untouched is not harmless:
    the cascade would still clear confirmed downstream results."""
    from byceps.services.lan_tournament import tournament_match_service

    c_a = _make_contestant(PARTICIPANT_A)
    c_b = _make_contestant(PARTICIPANT_B)
    scored = [
        type(c_a)(**{**c_a.__dict__, 'score': 3}),
        type(c_b)(**{**c_b.__dict__, 'score': 1}),
    ]
    mock_repo = _patched_correction(scored)

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='no actual change',
            corrected_scores={PARTICIPANT_A: 3, PARTICIPANT_B: 1},
        )

    assert result.is_err()
    assert 'identical' in result.unwrap_err()
    mock_unconfirm.assert_not_called()


def test_correction_allows_a_genuinely_changed_score():
    """The no-op guard must not block a real correction."""
    from byceps.services.lan_tournament import tournament_match_service

    c_a = _make_contestant(PARTICIPANT_A)
    c_b = _make_contestant(PARTICIPANT_B)
    scored = [
        type(c_a)(**{**c_a.__dict__, 'score': 3}),
        type(c_b)(**{**c_b.__dict__, 'score': 1}),
    ]
    mock_repo = _patched_correction(scored)

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
        patch(f'{_S}._admin_set_and_confirm_match_impl') as mock_apply,
        patch(f'{_S}.create_log_entry') as mock_log,
    ):
        mock_classify.return_value = Ok((CorrectionCase.NO_DOWNSTREAM, []))
        mock_unconfirm.return_value = Ok(([], [], False, TOURNAMENT_ID))
        mock_apply.return_value = Ok((MagicMock(), None, [], [], []))

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='swapped scores',
            corrected_scores={PARTICIPANT_A: 1, PARTICIPANT_B: 3},
        )

    assert result.is_ok()
    assert result.unwrap() == (CorrectionCase.NO_DOWNSTREAM, True)
    mock_unconfirm.assert_called_once()

    # The audit entry must carry what was replaced, and with what.
    data = mock_log.call_args.kwargs['data']
    assert data['previous_scores'] == {
        str(PARTICIPANT_A): 3,
        str(PARTICIPANT_B): 1,
    }
    assert data['new_scores'] == {
        str(PARTICIPANT_A): 1,
        str(PARTICIPANT_B): 3,
    }


def test_correction_locks_reachable_matches_before_classifying():
    """The ack gate must be decided on locked rows, or a concurrent
    downstream confirm slips past it."""
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = _patched_correction(_played_pair())
    calls = []
    mock_repo.lock_matches_for_update.side_effect = lambda ids: calls.append(
        ('lock', list(ids))
    )

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        mock_classify.side_effect = lambda _mid: (
            calls.append(('classify', None))
            or Ok((CorrectionCase.NO_DOWNSTREAM, []))
        )
        mock_unconfirm.return_value = Ok(([], [], False, TOURNAMENT_ID))

        result = tournament_match_service.correct_match_result(
            MATCH_ID, USER_ID, reason='retract only'
        )

    assert result.is_ok()
    assert [c[0] for c in calls] == ['lock', 'classify']
    assert calls[0][1] == [MATCH_ID]


def test_unconfirm_match_logs_the_scores_it_retracted():
    """The cascade clears the scores, so the snapshot must be read
    before it runs -- otherwise the audit entry records only nulls."""
    from byceps.services.lan_tournament import tournament_match_service

    c_a = _make_contestant(PARTICIPANT_A)
    scored = [type(c_a)(**{**c_a.__dict__, 'score': 7})]

    mock_repo = MagicMock()
    mock_repo.get_match_for_update.return_value = _make_match()
    mock_repo.find_match_fresh.return_value = _make_match()
    mock_repo.get_contestants_for_match.return_value = scored

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}._unconfirm_match_impl') as mock_impl,
        patch(f'{_S}.create_log_entry') as mock_log,
    ):
        mock_impl.return_value = Ok(([], [], False))

        result = tournament_match_service.unconfirm_match(
            MATCH_ID, USER_ID, reason='wrong winner'
        )

    assert result.is_ok()
    data = mock_log.call_args.kwargs['data']
    assert data['retracted_scores'] == {str(PARTICIPANT_A): 7}
    assert data['reason'] == 'wrong winner'


def test_correction_releases_locks_on_every_early_return():
    """The reachable bracket is locked before classifying, so an
    early Err must roll back -- outside a request nothing else does."""
    from byceps.services.lan_tournament import tournament_match_service

    for kwargs, contestants in (
        ({'corrected_scores': {PARTICIPANT_A: 3}}, [_make_contestant(PARTICIPANT_A)]),
        ({}, []),
    ):
        mock_repo = _patched_correction(contestants)
        with (
            patch(f'{_S}.tournament_repository', mock_repo),
            patch(f'{_S}.classify_result_correction') as mock_classify,
            patch(f'{_S}._unconfirm_match_flush'),
        ):
            mock_classify.return_value = Ok(
                (CorrectionCase.CONFIRMED_DOWNSTREAM, [MATCH_ID])
            )
            result = tournament_match_service.correct_match_result(
                MATCH_ID, USER_ID, reason='x', **kwargs
            )

        assert result.is_err()
        mock_repo.rollback_session.assert_called_once()


# ------------------------------------------------------------------ #
# the acknowledgement covers the matches the admin was shown
# ------------------------------------------------------------------ #


def _confirmed_match(match_id=None) -> MagicMock:
    m = MagicMock()
    m.id = match_id or TournamentMatchID(generate_uuid())
    m.confirmed_by = USER_ID
    return m


def _acknowledged_correction(case, downstream, acknowledged):
    from byceps.services.lan_tournament import tournament_match_service

    mock_repo = _patched_correction(_played_pair())
    mock_repo.get_matches_by_ids.return_value = downstream

    with (
        patch(f'{_S}.tournament_repository', mock_repo),
        patch(f'{_S}.classify_result_correction') as mock_classify,
        patch(f'{_S}._unconfirm_match_flush') as mock_unconfirm,
    ):
        mock_classify.return_value = Ok(
            (case, [m.id for m in downstream])
        )
        mock_unconfirm.return_value = Ok(([], [], False, TOURNAMENT_ID))

        result = tournament_match_service.correct_match_result(
            MATCH_ID,
            USER_ID,
            reason='disputed',
            ack_critical=True,
            acknowledged_match_ids=acknowledged,
        )

    return result, mock_repo, mock_unconfirm


def test_acknowledgement_of_fewer_matches_than_now_confirmed_is_refused():
    """A match confirmed after the page rendered was never acknowledged."""
    shown = _confirmed_match()
    confirmed_since = _confirmed_match()

    result, mock_repo, mock_unconfirm = _acknowledged_correction(
        CorrectionCase.CONFIRMED_DOWNSTREAM,
        [shown, confirmed_since],
        [shown.id],
    )

    assert result.is_err()
    assert 'acknowledge it again' in result.unwrap_err()
    mock_unconfirm.assert_not_called()
    mock_repo.rollback_session.assert_called()
    mock_repo.commit_session.assert_not_called()


def test_acknowledgement_covering_every_confirmed_match_proceeds():
    shown = _confirmed_match()
    unconfirmed = MagicMock(id=TournamentMatchID(generate_uuid()))
    unconfirmed.confirmed_by = None

    result, _mock_repo, mock_unconfirm = _acknowledged_correction(
        CorrectionCase.CONFIRMED_DOWNSTREAM,
        [shown, unconfirmed],
        [str(shown.id)],
    )

    assert result.is_ok(), result.unwrap_err()
    mock_unconfirm.assert_called_once()


def test_bracket_reset_acknowledgement_must_name_the_reset_match():
    gf_m2 = MagicMock(id=TournamentMatchID(generate_uuid()))
    gf_m2.confirmed_by = None

    result, _mock_repo, mock_unconfirm = _acknowledged_correction(
        CorrectionCase.BRACKET_RESET_DELETION, [gf_m2], []
    )

    assert result.is_err()
    assert 'acknowledge it again' in result.unwrap_err()
    mock_unconfirm.assert_not_called()
