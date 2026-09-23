"""
tests.unit.services.lan_tournament.test_confirm_lock_ordering
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Regression tests for workspace-k9hm: ``_lock_reachable_matches``'s
id-ordered acquisition used to be taken only by the retraction path
(``unconfirm_match`` / ``correct_match_result``). Every writer that
advances a contestant downstream -- ``confirm_match``,
``admin_set_and_confirm_match``, ``set_match_scores`` -- instead took
just the subject row via ``get_match_for_update`` and then touched
downstream rows in traversal order, picking up an implicit
``FOR KEY SHARE`` on the downstream match row via the FK on each
inserted contestant row. Where id order and topological order
disagree, that is a lock cycle: a concurrent confirm and a concurrent
retraction can acquire the same two rows in opposing orders and
deadlock in PostgreSQL.

The fix routes every downstream-advancing writer through
``_lock_reachable_matches`` first (via ``_confirm_match_impl``, the
flush-only core all three now share), so they take the SAME
id-ordered set ``unconfirm_match`` / ``correct_match_result`` do.

The FFA writers (``confirm_ffa_match``, ``set_ffa_placements``) never
advance a contestant into another match -- FFA does not use
``next_match_id`` -- so they are correctly NOT routed through the
lock; this is asserted below too, so that claim stays checked rather
than assumed.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

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
from byceps.util.result import Ok

from tests.helpers import generate_uuid


_S = 'byceps.services.lan_tournament.tournament_match_service'

MATCH_ID = TournamentMatchID(generate_uuid())
TOURNAMENT_ID = generate_uuid()
USER_ID = generate_uuid()
PARTICIPANT_A = TournamentParticipantID(generate_uuid())


def _terminal_match() -> MagicMock:
    """A match with nothing downstream: no next_match_id, no
    loser_next_match_id, not a DE grand final."""
    m = MagicMock()
    m.confirmed_by = None
    m.tournament_id = TOURNAMENT_ID
    m.next_match_id = None
    m.loser_next_match_id = None
    m.bracket = None
    m.match_order = 0
    return m


# ------------------------------------------------------------------ #
# downstream-advancing writers share the ordered acquisition
# ------------------------------------------------------------------ #


def test_confirm_match_routes_through_the_shared_lock():
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        mock_repo.get_match_for_update.return_value = _terminal_match()
        mock_repo.find_match_fresh.return_value = _terminal_match()
        # Fewer than 2 contestants -> _validate_match_confirmable
        # fails fast. The result is irrelevant here; only whether the
        # lock was taken before that failure matters.
        mock_repo.get_contestants_for_match.return_value = []

        tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    mock_lock.assert_called_once_with(MATCH_ID)


def test_admin_set_and_confirm_match_routes_through_the_shared_lock():
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._validate_match_scores') as mock_validate,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        mock_repo.get_match_for_update.return_value = _terminal_match()
        mock_repo.find_match_fresh.return_value = _terminal_match()
        mock_validate.return_value = Ok({})
        mock_repo.get_contestants_for_match.return_value = []

        tournament_match_service.admin_set_and_confirm_match(
            MATCH_ID, USER_ID, {}
        )

    # Exactly once, at the top of _admin_set_and_confirm_match_impl.
    # _confirm_match_impl is then told the locks are already held
    # (_locks_held=True) and skips its own acquisition: re-taking the
    # row locks would be cheap, but recomputing the reachable set
    # costs two full-bracket SELECTs. The choke point inside that
    # core still stands for any caller that reaches it without
    # locking -- _locks_held defaults to False, and
    # test_confirm_match_routes_through_the_shared_lock covers that
    # entry. The ordering tests below pin the 'first' half.
    mock_lock.assert_called_once_with(MATCH_ID)


def test_set_match_scores_routes_through_the_shared_lock():
    """set_match_scores calls the public confirm_match, which reaches
    the same flush-only core -- prove the lock fires end-to-end
    through that chain, not just at confirm_match's own boundary."""
    from byceps.services.lan_tournament import tournament_match_service

    contestant_a = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=PARTICIPANT_A,
        score=None,
        created_at=datetime.now(UTC),
    )

    match = _terminal_match()

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._resolve_initiator_contestant') as mock_resolve,
        patch(f'{_S}.determine_match_winner') as mock_winner,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        mock_repo.get_match_for_update.return_value = match
        mock_repo.find_match.return_value = match
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_contestants_for_match.return_value = [contestant_a]
        mock_resolve.return_value = contestant_a
        # A draw: set_match_scores's own loser-only check is skipped
        # (winner is None), so it proceeds to confirm_match. What
        # confirm_match's OWN winner_result decides doesn't matter --
        # _lock_reachable_matches runs before any of that, at the top
        # of _confirm_match_impl.
        mock_winner.return_value = Ok(None)

        tournament_match_service.set_match_scores(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    # Exactly once, at the top of set_match_scores. confirm_match
    # carries _locks_held=True down to _confirm_match_impl from here
    # -- see the note on the admin test above.
    mock_lock.assert_called_once_with(MATCH_ID)


# ------------------------------------------------------------------ #
# FFA writers correctly do NOT advance downstream, so they are
# correctly NOT routed through the lock
# ------------------------------------------------------------------ #


def test_confirm_ffa_match_does_not_use_the_shared_lock():
    from byceps.services.lan_tournament import tournament_match_service

    match = _terminal_match()

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        mock_repo.get_match_for_update.return_value = match
        mock_repo.find_match_fresh.return_value = match
        # Missing placements -> fails fast, before any write.
        mock_repo.get_contestants_for_match.return_value = [
            MagicMock(placement=None)
        ]

        tournament_match_service.confirm_ffa_match(MATCH_ID, USER_ID)

    mock_lock.assert_not_called()


def test_set_ffa_placements_does_not_use_the_shared_lock():
    from byceps.services.lan_tournament import tournament_match_service

    match = _terminal_match()

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        mock_repo.get_match_for_update.return_value = match
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_contestants_for_match.return_value = []

        tournament_match_service.set_ffa_placements(MATCH_ID, {})

    mock_lock.assert_not_called()


# ------------------------------------------------------------------ #
# query-count baseline: the shared lock is not free
# ------------------------------------------------------------------ #


def test_confirm_match_lock_acquisition_query_count_for_a_terminal_match():
    """Confirming a match with nothing downstream costs a small,
    fixed number of extra repository round-trips for the lock -- one
    unlocked existence check plus two lock-refresh rounds (the second
    of which observes no growth and stops), per _lock_reachable_matches'
    own documented refresh loop. Pin the count so a future change
    cannot silently make every confirm more expensive without a test
    noticing."""
    from byceps.services.lan_tournament import tournament_match_service

    calls: list[str] = []

    with patch(f'{_S}.tournament_repository') as mock_repo:
        mock_repo.find_match.side_effect = lambda _mid: (
            calls.append('find_match') or _terminal_match()
        )
        mock_repo.get_matches_for_tournament_ordered_fresh.side_effect = (
            lambda _tid: (
                calls.append('fresh_fetch') or [_terminal_match()]
            )
        )
        mock_repo.lock_matches_for_update.side_effect = lambda ids: (
            calls.append('lock')
        )
        mock_repo.get_match_for_update.return_value = _terminal_match()
        mock_repo.find_match_fresh.return_value = _terminal_match()
        # Fewer than 2 contestants -> fails fast, right after the lock.
        mock_repo.get_contestants_for_match.return_value = []

        tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    # 1 unlocked subject check, then round 1 (fetch + lock) and round 2
    # (fetch only -- the reachable set did not grow, so the loop
    # returns without locking again).
    assert calls == ['find_match', 'fresh_fetch', 'lock', 'fresh_fetch']


# ------------------------------------------------------------------ #
# ordering invariant: the id-ordered reachable-set lock must be the
# FIRST match-row lock a writer takes
# ------------------------------------------------------------------ #
#
# Asserting that _lock_reachable_matches is merely *called* is not
# enough. Bracket ids are uuid7 and generation order is
# reverse-topological -- generate_single_elimination_bracket creates
# the third-place match first and then builds rounds final-first --
# so a match's own id is routinely the HIGHEST in its reachable set.
# A writer that takes get_match_for_update on the subject before the
# ordered set therefore holds the highest id and waits on lower ones,
# exactly opposite to a concurrent correct_match_result. That
# deadlocks. These tests pin the order, not just the call.


def _ordered_calls(run):
    """Run `run(mock_repo)` with repo + lock attached to one parent
    mock, and return the combined ordered call-name list."""
    from byceps.services.lan_tournament import tournament_match_service

    parent = MagicMock()
    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        parent.attach_mock(mock_repo, 'repo')
        parent.attach_mock(mock_lock, 'lock')
        run(tournament_match_service, mock_repo)

    return [c[0] for c in parent.mock_calls]


def _assert_lock_precedes_row_lock(names):
    assert 'lock' in names, (
        '_lock_reachable_matches was never called'
    )
    first_lock = names.index('lock')
    # Every repository call below takes a row lock Postgres will hold
    # to the end of the transaction: the explicit SELECT ... FOR
    # UPDATE on the match row, and the contestant-row writes a
    # concurrent retraction cascade also reaches for via
    # clear_contestant_scores. Any of them landing before the
    # id-ordered set inverts lock order against that cascade.
    lock_taking = {
        'repo.get_match_for_update',
        'repo.update_contestant_scores',
        'repo.clear_contestant_scores',
        'repo.create_match_contestant',
    }
    offenders = [
        (i, n)
        for i, n in enumerate(names)
        if n in lock_taking and i < first_lock
    ]
    assert not offenders, (
        f'{offenders} took row locks before the id-ordered '
        'reachable-set lock; that inverts lock order against a '
        f'concurrent retraction and deadlocks. call order: {names}'
    )


def test_admin_set_and_confirm_locks_reachable_set_before_any_row_lock():
    def run(svc, mock_repo):
        match = _terminal_match()
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_match_for_update.return_value = match
        mock_repo.get_contestants_for_match.return_value = []
        with patch(f'{_S}._validate_match_scores') as mock_validate:
            mock_validate.return_value = Ok({})
            svc.admin_set_and_confirm_match(MATCH_ID, USER_ID, {})

    _assert_lock_precedes_row_lock(_ordered_calls(run))


def test_set_match_scores_locks_reachable_set_before_any_row_lock():
    contestant_a = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=PARTICIPANT_A,
        score=None,
        created_at=datetime.now(UTC),
    )

    def run(svc, mock_repo):
        match = _terminal_match()
        mock_repo.find_match.return_value = match
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_match_for_update.return_value = match
        mock_repo.get_contestants_for_match.return_value = [contestant_a]
        with (
            patch(f'{_S}._resolve_initiator_contestant') as mock_resolve,
            patch(f'{_S}.determine_match_winner') as mock_winner,
        ):
            mock_resolve.return_value = contestant_a
            # A draw, so set_match_scores' own loser-only check is
            # skipped and it proceeds all the way into confirm_match.
            mock_winner.return_value = Ok(None)
            svc.set_match_scores(MATCH_ID, USER_ID, {PARTICIPANT_A: 3})

    _assert_lock_precedes_row_lock(_ordered_calls(run))


def test_confirm_match_locks_reachable_set_before_any_row_lock():
    def run(svc, mock_repo):
        match = _terminal_match()
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_match_for_update.return_value = match
        mock_repo.get_contestants_for_match.return_value = []
        svc.confirm_match(MATCH_ID, USER_ID)

    _assert_lock_precedes_row_lock(_ordered_calls(run))


# ------------------------------------------------------------------ #
# every rejection after the lock releases it
#
# _lock_reachable_matches covers the whole reachable bracket, not the
# single subject row set_match_scores used to lock. A rejection that
# returned Err while holding that set parked it until request
# teardown, serialising every other writer on that bracket behind a
# submission that was refused -- and set_match_scores is reachable
# from contexts with no teardown at all. Its own docstring already
# promises callers a rolled-back session on Err.
# ------------------------------------------------------------------ #


def _one_contestant():
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=PARTICIPANT_A,
        score=None,
        created_at=datetime.now(UTC),
    )


def _set_match_scores_rejection(match, *, resolve, winner, scores):
    """Run set_match_scores to a rejection; return (result, repo mock)."""
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._resolve_initiator_contestant') as mock_resolve,
        patch(f'{_S}.determine_match_winner') as mock_winner,
        patch(f'{_S}._lock_reachable_matches'),
    ):
        mock_repo.find_match.return_value = _terminal_match()
        mock_repo.find_match_fresh.return_value = match
        mock_repo.get_contestants_for_match.return_value = [
            _one_contestant()
        ]
        mock_resolve.return_value = resolve
        mock_winner.return_value = winner

        result = tournament_match_service.set_match_scores(
            MATCH_ID, USER_ID, scores
        )

    return result, mock_repo


def test_set_match_scores_releases_locks_when_match_already_confirmed():
    match = _terminal_match()
    match = match.__class__(
        **{
            **match.__dict__,
            'confirmed_by': USER_ID,
        }
    )

    result, mock_repo = _set_match_scores_rejection(
        match,
        resolve=_one_contestant(),
        winner=Ok(None),
        scores={PARTICIPANT_A: 3},
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Cannot modify scores of a confirmed match.'
    )
    mock_repo.rollback_session.assert_called_once()


def test_set_match_scores_releases_locks_for_a_non_participant():
    result, mock_repo = _set_match_scores_rejection(
        _terminal_match(),
        resolve=None,
        winner=Ok(None),
        scores={PARTICIPANT_A: 3},
    )

    assert result.is_err()
    assert result.unwrap_err() == 'You are not a participant in this match.'
    mock_repo.rollback_session.assert_called_once()


def test_set_match_scores_releases_locks_on_a_negative_score():
    result, mock_repo = _set_match_scores_rejection(
        _terminal_match(),
        resolve=_one_contestant(),
        winner=Ok(None),
        scores={PARTICIPANT_A: -1},
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Score cannot be negative.'
    mock_repo.rollback_session.assert_called_once()


def test_set_match_scores_takes_no_lock_for_a_non_participant():
    """Any logged-in user can post scores; a refusal must lock nothing."""
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._resolve_initiator_contestant') as mock_resolve,
        patch(f'{_S}._lock_reachable_matches') as mock_lock,
    ):
        mock_repo.find_match.return_value = _terminal_match()
        mock_repo.find_match_fresh.return_value = _terminal_match()
        mock_repo.get_contestants_for_match.return_value = [
            _one_contestant()
        ]
        mock_resolve.return_value = None

        result = tournament_match_service.set_match_scores(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    assert result.unwrap_err() == 'You are not a participant in this match.'
    mock_lock.assert_not_called()
    mock_repo.lock_matches_for_update.assert_not_called()
    mock_repo.get_match_for_update.assert_not_called()


def test_set_match_scores_releases_locks_when_the_winner_submits():
    contestant = _one_contestant()

    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._resolve_initiator_contestant') as mock_resolve,
        patch(f'{_S}.determine_match_winner') as mock_winner,
        patch(f'{_S}._lock_reachable_matches'),
    ):
        mock_repo.find_match.return_value = _terminal_match()
        mock_repo.find_match_fresh.return_value = _terminal_match()
        mock_repo.get_contestants_for_match.return_value = [contestant]
        mock_resolve.return_value = contestant
        # The initiator is the proposed winner -- loser-only submission.
        mock_winner.return_value = Ok(contestant)

        result = tournament_match_service.set_match_scores(
            MATCH_ID, USER_ID, {PARTICIPANT_A: 3}
        )

    assert result.is_err()
    assert result.unwrap_err() == 'Only the losing side may submit scores.'
    mock_repo.rollback_session.assert_called_once()


# ------------------------------------------------------------------ #
# the defwin removal handlers (workspace-ur9c)
# ------------------------------------------------------------------ #
#
# ``find_contestant_entries_for_*_in_tournament`` carries no
# ``ORDER BY``, so before the fix these handlers took their implicit
# UPDATE/DELETE row locks in whatever order Postgres returned the
# rows. They were the last writer family in this module not acquiring
# match rows in ascending id order, and the matches they touch --
# unconfirmed ones -- are exactly what a retraction cascade locks
# downstream of its subject, so the two could deadlock ABBA.


def _defwin_entry(match_id, next_match_id=None):
    contestant = MagicMock()
    match = MagicMock()
    match.id = match_id
    match.next_match_id = next_match_id
    return (contestant, match)


def _sorted_uuids(count):
    return sorted(generate_uuid() for _ in range(count))


def test_removed_participant_defwin_locks_matches_in_id_order():
    from byceps.services.lan_tournament import tournament_match_service

    low, high = _sorted_uuids(2)
    # Deliberately returned highest-first, the order that produced the
    # inversion.
    entries = [_defwin_entry(high), _defwin_entry(low)]

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._process_defwin_entries') as mock_process,
    ):
        mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = (
            entries
        )
        mock_process.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )

        tournament_match_service.handle_defwin_for_removed_participant(
            TOURNAMENT_ID, PARTICIPANT_A
        )

    mock_repo.lock_matches_for_update.assert_called_once_with([low, high])


def test_removed_team_defwin_locks_matches_in_id_order():
    from byceps.services.lan_tournament import tournament_match_service

    low, high = _sorted_uuids(2)
    entries = [_defwin_entry(high), _defwin_entry(low)]

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._process_defwin_entries') as mock_process,
    ):
        mock_repo.find_contestant_entries_for_team_in_tournament.return_value = (
            entries
        )
        mock_process.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )

        tournament_match_service.handle_defwin_for_removed_team(
            TOURNAMENT_ID, generate_uuid()
        )

    mock_repo.lock_matches_for_update.assert_called_once_with([low, high])


def test_defwin_lock_covers_the_advance_destinations():
    """The sole remaining opponent is advanced INTO ``next_match_id``.

    That row is in the set the correction path locks, so it has to be
    in this one too -- otherwise the ordering only holds for half the
    rows the pass touches.
    """
    from byceps.services.lan_tournament import tournament_match_service

    a, b, c = _sorted_uuids(3)
    # One entry match (c) routing into a match with a LOWER id (a).
    entries = [_defwin_entry(c, next_match_id=a), _defwin_entry(b)]

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._process_defwin_entries') as mock_process,
    ):
        mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = (
            entries
        )
        mock_process.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )

        tournament_match_service.handle_defwin_for_removed_participant(
            TOURNAMENT_ID, PARTICIPANT_A
        )

    mock_repo.lock_matches_for_update.assert_called_once_with([a, b, c])


def test_defwin_lock_is_taken_before_any_write():
    """A lock acquired after the first DELETE orders nothing."""
    from byceps.services.lan_tournament import tournament_match_service

    low, high = _sorted_uuids(2)
    entries = [_defwin_entry(high), _defwin_entry(low)]

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._process_defwin_entries') as mock_process,
    ):
        mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = (
            entries
        )
        mock_process.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )

        tournament_match_service.handle_defwin_for_removed_participant(
            TOURNAMENT_ID, PARTICIPANT_A
        )

    call_names = [c[0] for c in mock_repo.method_calls]
    assert call_names.index('lock_matches_for_update') < call_names.index(
        'delete_contestant_from_match'
    )


def test_defwin_with_no_entries_locks_nothing():
    """Removing a contestant who is in no unconfirmed match must not
    emit an empty ``FOR UPDATE``."""
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._process_defwin_entries') as mock_process,
    ):
        mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = (
            []
        )
        mock_process.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )

        tournament_match_service.handle_defwin_for_removed_participant(
            TOURNAMENT_ID, PARTICIPANT_A
        )

    mock_repo.lock_matches_for_update.assert_not_called()


# ------------------------------------------------------------------ #
# the tournament row is locked before any match row
# ------------------------------------------------------------------ #


def _names(mock_repo):
    return [c[0] for c in mock_repo.mock_calls]


def _assert_tournament_locked_first(names, first_match_lock):
    assert 'lock_tournament_for_update' in names, names
    assert names.index('lock_tournament_for_update') < names.index(
        first_match_lock
    ), names


def test_reachable_set_lock_takes_the_tournament_row_first():
    from byceps.services.lan_tournament import tournament_match_service

    match = _terminal_match()

    with patch(f'{_S}.tournament_repository') as mock_repo:
        mock_repo.find_match.return_value = match
        mock_repo.get_matches_for_tournament_ordered_fresh.return_value = [
            match
        ]

        tournament_match_service._lock_reachable_matches(MATCH_ID)

    mock_repo.lock_tournament_for_update.assert_called_once_with(
        match.tournament_id
    )
    _assert_tournament_locked_first(
        _names(mock_repo), 'lock_matches_for_update'
    )


def test_defwin_handler_takes_the_tournament_row_first():
    from byceps.services.lan_tournament import tournament_match_service

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}._process_defwin_entries') as mock_process,
    ):
        mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = (
            [_defwin_entry(generate_uuid())]
        )
        mock_process.return_value = tournament_match_service.DefwinResult(
            [], [], []
        )

        tournament_match_service.handle_defwin_for_removed_participant(
            TOURNAMENT_ID, PARTICIPANT_A
        )

    mock_repo.lock_tournament_for_update.assert_called_once_with(
        TOURNAMENT_ID
    )
    _assert_tournament_locked_first(
        _names(mock_repo), 'lock_matches_for_update'
    )


def test_ffa_confirmation_takes_the_tournament_row_first():
    from byceps.services.lan_tournament import tournament_match_service

    match = _terminal_match()

    with (
        patch(f'{_S}.tournament_repository') as mock_repo,
        patch(f'{_S}.create_log_entry'),
    ):
        mock_repo.find_match.return_value = match
        mock_repo.get_match_for_update.return_value = match
        mock_repo.get_contestants_for_match.return_value = []

        tournament_match_service.confirm_ffa_match(MATCH_ID, USER_ID)

    mock_repo.lock_tournament_for_update.assert_called_once_with(
        match.tournament_id
    )
    _assert_tournament_locked_first(
        _names(mock_repo), 'get_match_for_update'
    )
