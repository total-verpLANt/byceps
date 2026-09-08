"""
tests.integration.services.lan_tournament.test_match_result_correction
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-correction')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Correction')


@pytest.fixture(scope='module')
def user1(make_user):
    return make_user('CorrectionUser1')


@pytest.fixture(scope='module')
def user2(make_user):
    return make_user('CorrectionUser2')


@pytest.fixture(scope='module')
def user3(make_user):
    return make_user('CorrectionUser3')


@pytest.fixture(scope='module')
def user4(make_user):
    return make_user('CorrectionUser4')


@pytest.fixture(scope='module')
def user5(make_user):
    return make_user('CorrectionUser5')


@pytest.fixture(scope='module')
def user6(make_user):
    return make_user('CorrectionUser6')


@pytest.fixture(scope='module')
def user7(make_user):
    return make_user('CorrectionUser7')


@pytest.fixture(scope='module')
def user8(make_user):
    return make_user('CorrectionUser8')


@pytest.fixture(scope='module')
def admin_user(make_user):
    return make_user('CorrectionAdmin')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Tournament Entry Correction')


@pytest.fixture(scope='module')
def grant_ticket(ticket_category):
    """Give a user a valid (used) ticket for the party."""

    def _grant(user):
        return ticket_creation_service.create_ticket(
            ticket_category, user, user=user
        )

    return _grant


def _create_tournament(name, *, max_players):
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=max_players,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()

    open_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    assert open_result.is_ok()

    return tournament


def _setup_se_bracket(tournament, users, grant_ticket):
    """Join all users, close registration, generate the SE bracket."""
    participants = {}
    for user in users:
        grant_ticket(user)
        result = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert result.is_ok()
        participant, _ = result.unwrap()
        participants[user.id] = participant

    close_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    )
    assert close_result.is_ok()

    generate_result = (
        tournament_match_service.generate_single_elimination_bracket(
            tournament.id
        )
    )
    assert generate_result.is_ok()

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    return matches, participants


def _find_final(matches):
    """Return the grand final (terminal match that is not the P3)."""
    finals = [
        m for m in matches
        if m.next_match_id is None and m.loser_next_match_id is None
        and m.bracket is not Bracket.THIRD_PLACE
    ]
    assert len(finals) == 1
    return finals[0]


def _confirm_match_with_scores(match, participants, admin_user):
    """Set distinct scores for both contestants and confirm."""
    contestants = tournament_match_service.get_contestants_for_match(
        match.id
    )
    assert len(contestants) == 2
    for i, contestant in enumerate(contestants):
        score_result = tournament_match_service.set_score(
            match.id, contestant.participant_id, 3 - i
        )
        assert score_result.is_ok()

    confirm_result = tournament_match_service.confirm_match(
        match.id, admin_user.id
    )
    assert confirm_result.is_ok()


def _confirm_full_bracket(matches, participants, admin_user):
    """Confirm EVERY match in a generated SE bracket -- every round,
    in dependency order, plus the third-place match -- so the whole
    tree ends up confirmed and the tournament COMPLETED.

    Round-by-round order matters: a later round's contestants are
    only populated once the round before it is confirmed and its
    winners (and, for the semifinal round, losers) advance. The
    third-place match is confirmed before the final: BOTH have
    ``next_match_id is None``, so confirming either one triggers
    ``_try_auto_complete_tournament``; confirming the final LAST
    ensures its (correct) winner is the one left recorded on the
    tournament.
    """
    final = _find_final(matches)
    p3 = next(
        (m for m in matches if m.bracket is Bracket.THIRD_PLACE), None
    )
    excluded_ids = {final.id} | ({p3.id} if p3 is not None else set())

    by_round: dict[int, list] = {}
    for m in matches:
        if m.id in excluded_ids:
            continue
        by_round.setdefault(m.round, []).append(m)

    for round_num in sorted(by_round):
        for m in by_round[round_num]:
            current = tournament_match_service.get_match(m.id)
            _confirm_match_with_scores(current, participants, admin_user)

    if p3 is not None:
        current_p3 = tournament_match_service.get_match(p3.id)
        _confirm_match_with_scores(current_p3, participants, admin_user)

    current_final = tournament_match_service.get_match(final.id)
    _confirm_match_with_scores(current_final, participants, admin_user)

    return final, p3


def test_classify_case_a_no_downstream(
    party, user1, user2, admin_user, grant_ticket
):
    """A final match with no downstream classifies as Case A."""
    tournament = _create_tournament(
        'Correction Case A', max_players=2
    )

    matches, _participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    assert len(matches) == 1
    match = matches[0]
    assert match.next_match_id is None
    assert match.loser_next_match_id is None

    result = tournament_match_service.classify_result_correction(
        match.id
    )
    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CASE_A
    assert affected == []


def test_classify_case_b_downstream_unconfirmed(
    party, user1, user2, user3, user4, grant_ticket
):
    """An upstream match feeding an unconfirmed match is Case B."""
    tournament = _create_tournament(
        'Correction Case B', max_players=4
    )

    matches, _participants = _setup_se_bracket(
        tournament, [user1, user2, user3, user4], grant_ticket
    )
    final = _find_final(matches)

    feeders = [
        m for m in matches if m.next_match_id == final.id
    ]
    assert len(feeders) >= 1
    feeder = feeders[0]
    assert feeder.confirmed_by is None
    assert final.confirmed_by is None

    result = tournament_match_service.classify_result_correction(
        feeder.id
    )
    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CASE_B
    assert final.id in affected


def test_classify_case_c_downstream_confirmed(
    party, user1, user2, user3, user4, admin_user, grant_ticket
):
    """An upstream match whose downstream match is confirmed is Case C."""
    tournament = _create_tournament(
        'Correction Case C', max_players=4
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2, user3, user4], grant_ticket
    )
    final = _find_final(matches)

    feeders = [
        m for m in matches if m.next_match_id == final.id
    ]
    assert len(feeders) >= 1

    # Play out both semifinals so the final can be played and confirmed.
    for feeder in feeders:
        _confirm_match_with_scores(feeder, participants, admin_user)

    _confirm_match_with_scores(final, participants, admin_user)

    result = tournament_match_service.classify_result_correction(
        feeders[0].id
    )
    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CASE_C
    assert final.id in affected


def test_classify_case_c_returns_full_transitive_chain(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    admin_user, grant_ticket,
):
    """classify_result_correction must return the WHOLE confirmed
    downstream chain -- not just the direct (depth-1) feeder -- for a
    bracket at least 3 rounds deep (quarterfinal -> semifinal ->
    final). This is the exact scenario the false Case C warning text
    denied ("no automatic chain correction... admin handles
    downstream manually"): the real cascade (_unconfirm_match_impl)
    walks the whole confirmed chain, and classification must report
    that same chain, not just one hop of it.
    """
    tournament = _create_tournament(
        'Correction Deep Classify', max_players=8
    )
    matches, participants = _setup_se_bracket(
        tournament,
        [user1, user2, user3, user4, user5, user6, user7, user8],
        grant_ticket,
    )
    assert {
        m.round for m in matches if m.bracket is not Bracket.THIRD_PLACE
    } == {0, 1, 2}  # quarterfinal, semifinal, final -- 3 rounds deep

    final, p3 = _confirm_full_bracket(matches, participants, admin_user)
    semis = [m for m in matches if m.next_match_id == final.id]
    quarters = [
        m for m in matches if m.next_match_id in {s.id for s in semis}
    ]
    assert len(semis) == 2
    assert len(quarters) == 4

    target = quarters[0]
    downstream_semi_id = target.next_match_id

    result = tournament_match_service.classify_result_correction(
        target.id
    )
    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CASE_C
    assert downstream_semi_id in affected
    assert final.id in affected
    assert p3.id in affected
    # BFS order: the direct feeder (depth 1) before the transitive
    # (depth 2) matches reached through it.
    assert affected.index(downstream_semi_id) < affected.index(final.id)
    assert affected.index(downstream_semi_id) < affected.index(p3.id)


def test_classify_stops_traversal_at_unconfirmed_downstream(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    grant_ticket,
):
    """A match's own direct downstream neighbor is always included in
    the affected list -- the cascade always retracts an advanced
    contestant from it regardless of its confirmation state -- but
    classification must NOT traverse past an unconfirmed one: an
    unconfirmed match is never itself unconfirmed-and-cascaded
    further by _unconfirm_match_impl, so anything beyond it (here,
    the final, two rounds downstream) must not appear.
    """
    tournament = _create_tournament(
        'Correction Deep Stops At Unconfirmed', max_players=8
    )
    matches, _participants = _setup_se_bracket(
        tournament,
        [user1, user2, user3, user4, user5, user6, user7, user8],
        grant_ticket,
    )
    final = _find_final(matches)
    semis = [m for m in matches if m.next_match_id == final.id]
    quarters = [
        m for m in matches if m.next_match_id in {s.id for s in semis}
    ]

    # Nothing is confirmed -- a fresh bracket is enough to prove the
    # direct downstream neighbor stops the walk.
    target = quarters[0]
    downstream_semi_id = target.next_match_id
    assert downstream_semi_id in {s.id for s in semis}

    result = tournament_match_service.classify_result_correction(
        target.id
    )
    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.CASE_B
    assert affected == [downstream_semi_id]
    assert final.id not in affected


def test_correct_match_result_case_c_unwinds_full_transitive_chain(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    admin_user, grant_ticket,
):
    """Correcting a quarterfinal match in a fully-confirmed, 3-round-
    deep SE bracket (the tournament COMPLETED, every match including
    the third-place match confirmed) must unconfirm and clear scores
    for EVERY confirmed downstream match in the chain -- the direct
    semifinal, the third-place match reached via its loser routing,
    and the final two rounds away -- and revert the tournament to
    ONGOING with no winner recorded. Matches outside the chain (the
    other semifinal and its own feeders) must be left untouched.
    """
    tournament = _create_tournament(
        'Correction Deep Cascade', max_players=8
    )
    matches, participants = _setup_se_bracket(
        tournament,
        [user1, user2, user3, user4, user5, user6, user7, user8],
        grant_ticket,
    )
    final, p3 = _confirm_full_bracket(matches, participants, admin_user)
    semis = [m for m in matches if m.next_match_id == final.id]
    quarters = [
        m for m in matches if m.next_match_id in {s.id for s in semis}
    ]

    completed = tournament_service.get_tournament(tournament.id)
    assert completed.tournament_status == TournamentStatus.COMPLETED
    assert (
        completed.winner_participant_id is not None
        or completed.winner_team_id is not None
    )

    target = quarters[0]
    downstream_semi = tournament_match_service.get_match(
        target.next_match_id
    )
    other_semi = next(s for s in semis if s.id != downstream_semi.id)
    assert downstream_semi.confirmed_by is not None
    assert tournament_match_service.get_match(final.id).confirmed_by \
        is not None
    assert tournament_match_service.get_match(p3.id).confirmed_by \
        is not None

    result = tournament_match_service.correct_match_result(
        target.id,
        admin_user.id,
        reason='Quarterfinal result was wrong',
        ack_critical=True,
    )
    assert result.is_ok()
    case, scores_applied = result.unwrap()
    assert case is CorrectionCase.CASE_C
    assert scores_applied is False

    for match_id in (target.id, downstream_semi.id, final.id, p3.id):
        reverted = tournament_match_service.get_match(match_id)
        assert reverted.confirmed_by is None, (
            f'match {match_id} should have been unconfirmed'
        )
        contestants = tournament_match_service.get_contestants_for_match(
            match_id
        )
        assert all(c.score is None for c in contestants), (
            f'match {match_id} should have had its scores cleared'
        )

    # The OTHER semifinal (and its own feeders) is outside this
    # chain and must be untouched.
    untouched_semi = tournament_match_service.get_match(other_semi.id)
    assert untouched_semi.confirmed_by is not None

    reverted_tournament = tournament_service.get_tournament(tournament.id)
    assert reverted_tournament.tournament_status == TournamentStatus.ONGOING
    assert reverted_tournament.winner_participant_id is None
    assert reverted_tournament.winner_team_id is None


def test_correct_match_result_persists_log_entry_with_reason(
    party, user1, user2, admin_user, grant_ticket
):
    """A correction with reentered scores logs 'match-result-corrected'
    with reason + initiator.
    """
    tournament = _create_tournament(
        'Correction Log Entry', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    contestants = tournament_match_service.get_contestants_for_match(
        match.id
    )
    corrected_scores = {
        contestant.participant_id: 5 if i == 0 else 1
        for i, contestant in enumerate(contestants)
    }

    result = tournament_match_service.correct_match_result(
        match.id,
        admin_user.id,
        reason='Score entered incorrectly',
        corrected_scores=corrected_scores,
    )
    assert result.is_ok()
    case, scores_applied = result.unwrap()
    assert case is CorrectionCase.CASE_A
    assert scores_applied is True

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    corrections = [
        e for e in entries
        if e.event_type == 'match-result-corrected'
    ]
    assert len(corrections) == 1

    entry = corrections[0]
    assert entry.initiator_id == admin_user.id
    assert entry.data['match_id'] == str(match.id)
    assert entry.data['case'] == 'case_a'
    assert entry.data['reason'] == 'Score entered incorrectly'
    assert entry.data['scores_applied'] is True


def test_correct_match_result_writes_retraction_entry(
    party, user1, user2, admin_user, grant_ticket
):
    """A correction with no reentered scores logs only the retraction
    entry, carrying the reason.
    """
    tournament = _create_tournament(
        'Correction Retraction Entry', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    result = tournament_match_service.correct_match_result(
        match.id,
        admin_user.id,
        reason='Score entered incorrectly',
    )
    assert result.is_ok()
    case, scores_applied = result.unwrap()
    assert case is CorrectionCase.CASE_A
    assert scores_applied is False

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    retractions = [
        e for e in entries
        if e.event_type == 'match-result-retracted'
    ]
    assert len(retractions) == 1

    entry = retractions[0]
    assert entry.initiator_id == admin_user.id
    assert entry.data['match_id'] == str(match.id)
    assert entry.data['reason'] == 'Score entered incorrectly'

    corrections = [
        e for e in entries
        if e.event_type == 'match-result-corrected'
    ]
    assert len(corrections) == 0


def test_correct_match_result_writes_both_entries_when_scores_reentered(
    party, user1, user2, admin_user, grant_ticket
):
    """Reentering scores logs the retraction entry, then the
    correction entry, in that order.
    """
    tournament = _create_tournament(
        'Correction Both Entries', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    contestants = tournament_match_service.get_contestants_for_match(
        match.id
    )
    corrected_scores = {
        contestant.participant_id: 7 if i == 0 else 4
        for i, contestant in enumerate(contestants)
    }

    result = tournament_match_service.correct_match_result(
        match.id,
        admin_user.id,
        reason='Rescored after review',
        corrected_scores=corrected_scores,
    )
    assert result.is_ok()
    case, scores_applied = result.unwrap()
    assert case is CorrectionCase.CASE_A
    assert scores_applied is True

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    retractions = [
        e for e in entries
        if e.event_type == 'match-result-retracted'
    ]
    corrections = [
        e for e in entries
        if e.event_type == 'match-result-corrected'
    ]
    assert len(retractions) == 1
    assert len(corrections) == 1
    assert retractions[0].occurred_at <= corrections[0].occurred_at


def _assert_correction_rejected_before_any_write(
    tournament, match, original_contestants, result
):
    """Shared assertions for the "invalid corrected score" tests
    below: the match must be untouched -- still confirmed, with its
    original scores -- and NEITHER a retraction NOR a correction
    entry may exist. ``_validate_match_scores`` runs BEFORE
    ``unconfirm_match`` inside ``correct_match_result``, so a bad
    score must never trigger the retraction cascade at all.
    """
    assert result.is_err()

    still_confirmed = tournament_match_service.get_match(match.id)
    assert still_confirmed.confirmed_by is not None

    contestants_after = {
        contestant.participant_id: contestant.score
        for contestant in (
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }
    assert contestants_after == original_contestants

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert not any(
        e.event_type in (
            'match-result-retracted', 'match-result-corrected'
        )
        for e in entries
    )


def test_correct_match_result_rejects_negative_score_before_any_write(
    party, user1, user2, admin_user, grant_ticket
):
    """A rejected re-entered score (negative) is caught before the
    retraction cascade runs at all: the match stays confirmed with
    its original scores, and NEITHER a retraction NOR a correction
    entry is written.

    Previously, an invalid score was only discovered AFTER
    unconfirm_match's cascade had already committed -- retracting a
    (possibly downstream-affecting) result for a correction that was
    never actually going to apply. _validate_match_scores now runs
    before unconfirm_match, so nothing destructive happens on a
    rejected score.
    """
    tournament = _create_tournament(
        'Correction Rejected Negative Score', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    original_contestants = {
        contestant.participant_id: contestant.score
        for contestant in (
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }
    rejected_scores = {
        contestant.participant_id: -1 if i == 0 else 4
        for i, contestant in enumerate(
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }

    result = tournament_match_service.correct_match_result(
        match.id,
        admin_user.id,
        reason='Typo while re-scoring',
        corrected_scores=rejected_scores,
    )
    assert 'negative' in result.unwrap_err().lower()
    _assert_correction_rejected_before_any_write(
        tournament, match, original_contestants, result
    )


def test_correct_match_result_rejects_score_above_max_before_any_write(
    party, user1, user2, admin_user, grant_ticket
):
    """A re-entered score above MAX_MATCH_SCORE is rejected with the
    same no-write contract as a negative score."""
    tournament = _create_tournament(
        'Correction Rejected Score Too High', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    original_contestants = {
        contestant.participant_id: contestant.score
        for contestant in (
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }
    too_high_scores = {
        contestant.participant_id: (
            tournament_match_service.MAX_MATCH_SCORE + 1
            if i == 0
            else 4
        )
        for i, contestant in enumerate(
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }

    result = tournament_match_service.correct_match_result(
        match.id,
        admin_user.id,
        reason='Fat-fingered the score',
        corrected_scores=too_high_scores,
    )
    assert 'exceed' in result.unwrap_err().lower()
    _assert_correction_rejected_before_any_write(
        tournament, match, original_contestants, result
    )


def test_correct_match_result_rejects_short_score_dict_before_any_write(
    party, user1, user2, admin_user, grant_ticket
):
    """A corrected_scores dict missing an entry for one contestant is
    rejected with the same no-write contract as a negative score."""
    tournament = _create_tournament(
        'Correction Rejected Missing Score', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    original_contestants = {
        contestant.participant_id: contestant.score
        for contestant in (
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }
    contestants = tournament_match_service.get_contestants_for_match(
        match.id
    )
    short_scores = {contestants[0].participant_id: 5}  # missing the 2nd

    result = tournament_match_service.correct_match_result(
        match.id,
        admin_user.id,
        reason='Forgot to enter both scores',
        corrected_scores=short_scores,
    )
    assert 'scores' in result.unwrap_err().lower()
    _assert_correction_rejected_before_any_write(
        tournament, match, original_contestants, result
    )


def _read_via_fresh_connection(tournament, match_id):
    """Read a match, its contestants, and its tournament's log entries
    through a BRAND NEW SQLAlchemy session/connection, bypassing the
    shared app-context session (``db.session``) entirely.

    This is deliberately NOT ``db.session.expire_all()`` on the
    existing session: that would only clear the Python-level identity-
    map cache, then re-SELECT INSIDE THE SAME, still-open transaction
    -- which sees its own uncommitted writes ("read your own writes"
    is normal transaction semantics), so it would show whatever the
    cascade already flushed regardless of whether it was ever
    committed. A genuinely separate connection, under Postgres'
    default READ COMMITTED isolation, sees only what other
    transactions have actually COMMITTED -- proving what is truly
    durable, independent of the shared session's own state.
    """
    with SqlaSession(bind=db.engine) as fresh:
        fresh_match = fresh.get(DbTournamentMatch, match_id)
        fresh_contestants = fresh.scalars(
            select(DbTournamentMatchToContestant).filter_by(
                tournament_match_id=match_id
            )
        ).all()
        fresh_entries = fresh.scalars(
            select(DbTournamentLogEntry).filter_by(
                tournament_id=tournament.id
            )
        ).all()
        return (
            fresh_match,
            {c.participant_id: c.score for c in fresh_contestants},
            [e.event_type for e in fresh_entries],
        )


def test_correct_match_result_leaves_nothing_committed_when_retraction_log_write_fails(
    party, user1, user2, admin_user, grant_ticket, monkeypatch
):
    """A raised (non-Result) exception from unconfirm_match's OWN
    'match-result-retracted' log write must leave the DATABASE
    untouched: the flush-only unconfirm + score-clear that already
    ran before the raise are never committed.

    unconfirm_match now wraps this create_log_entry call in its own
    ``try/except Exception: tournament_repository.rollback_session();
    raise``, so this test asserts BOTH halves of that guarantee:

    - rollback_session is actually invoked as a direct result of the
      raise. This is checked with a SPY on
      tournament_repository.rollback_session, not by inspecting
      session.dirty/.new/.deleted -- verified empirically (a throwaway
      SQLAlchemy script against SQLite) that those collections are
      already empty immediately after ANY flush, regardless of
      whether a rollback ever follows: _unconfirm_match_impl's own
      flush() calls clear an object's dirty-tracking before this
      raise even fires, so that check would not actually detect a
      regression here.
    - nothing was actually persisted, checked through a genuinely
      separate connection (see _read_via_fresh_connection) so this
      cannot pass "by construction" the way an earlier version of
      this test did (it called db.session.rollback() itself and then
      asserted state was unchanged -- trivially true regardless of
      what unconfirm_match/correct_match_result actually do).
    """
    tournament = _create_tournament(
        'Correction Retraction Log Failure', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    original_contestants = {
        contestant.participant_id: contestant.score
        for contestant in (
            tournament_match_service.get_contestants_for_match(match.id)
        )
    }

    real_create_log_entry = tournament_match_service.create_log_entry

    def _explode_on_retraction(event_type, *args, **kwargs):
        if event_type == 'match-result-retracted':
            raise RuntimeError('audit log backend down')
        return real_create_log_entry(event_type, *args, **kwargs)

    monkeypatch.setattr(
        tournament_match_service,
        'create_log_entry',
        _explode_on_retraction,
    )

    rollback_calls: list[None] = []
    real_rollback_session = tournament_repository.rollback_session

    def _spy_rollback_session() -> None:
        rollback_calls.append(None)
        real_rollback_session()

    monkeypatch.setattr(
        tournament_repository, 'rollback_session', _spy_rollback_session
    )

    with pytest.raises(RuntimeError):
        tournament_match_service.correct_match_result(
            match.id,
            admin_user.id,
            reason='Log write will fail',
        )

    # Production actually rolled back as a direct result of the raise
    # -- this fails if the try/except around create_log_entry is ever
    # removed.
    assert len(rollback_calls) == 1

    fresh_match, fresh_scores, fresh_event_types = (
        _read_via_fresh_connection(tournament, match.id)
    )
    assert fresh_match.confirmed_by == admin_user.id
    assert fresh_scores == original_contestants
    assert fresh_event_types == []


def test_correct_match_result_leaves_retraction_committed_when_correction_log_write_fails(
    party, user1, user2, admin_user, grant_ticket, monkeypatch
):
    """A raised (non-Result) exception from correct_match_result's OWN
    staged 'match-result-corrected' log write happens AFTER
    unconfirm_match's retraction has already committed --
    unconfirm_match is documented as its own transaction boundary and
    "the single writer" of the retraction entry. So the match is left
    unconfirmed with cleared scores and exactly the retraction entry
    recorded; the corrected scores were never applied and no
    correction entry (real or orphaned) exists.

    Unlike the retraction-side test above, nothing is left flushed-
    but-uncommitted here: the raise happens inside the replacement
    for create_log_entry itself, before it ever touches the session,
    so rollback_session has nothing to actually discard on this path.
    This test still spies on it to prove BOTH create_log_entry call
    sites in this module are wrapped identically, even though this
    one is a no-op in terms of what state it reverts.
    """
    tournament = _create_tournament(
        'Correction Correction Log Failure', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    contestants = tournament_match_service.get_contestants_for_match(
        match.id
    )
    corrected_scores = {
        contestant.participant_id: 5 if i == 0 else 1
        for i, contestant in enumerate(contestants)
    }

    real_create_log_entry = tournament_match_service.create_log_entry

    def _explode_on_correction(event_type, *args, **kwargs):
        if event_type == 'match-result-corrected':
            raise RuntimeError('audit log backend down')
        return real_create_log_entry(event_type, *args, **kwargs)

    monkeypatch.setattr(
        tournament_match_service,
        'create_log_entry',
        _explode_on_correction,
    )

    rollback_calls: list[None] = []
    real_rollback_session = tournament_repository.rollback_session

    def _spy_rollback_session() -> None:
        rollback_calls.append(None)
        real_rollback_session()

    monkeypatch.setattr(
        tournament_repository, 'rollback_session', _spy_rollback_session
    )

    with pytest.raises(RuntimeError):
        tournament_match_service.correct_match_result(
            match.id,
            admin_user.id,
            reason='Rescored after review',
            corrected_scores=corrected_scores,
        )

    # Production rolled back on this path too -- proves both
    # create_log_entry call sites are wrapped identically.
    assert len(rollback_calls) == 1

    # The retraction is unconfirm_match's OWN, already-committed
    # transaction -- checked via a fresh connection anyway so this
    # test does not depend on that distinction holding forever.
    fresh_match, fresh_scores, fresh_event_types = (
        _read_via_fresh_connection(tournament, match.id)
    )
    assert fresh_match.confirmed_by is None
    assert all(score is None for score in fresh_scores.values())
    assert fresh_event_types == ['match-result-retracted']


def test_correct_match_result_rejects_blank_reason(
    party, user1, user2, admin_user, grant_ticket
):
    """Blank reasons are rejected and leave the match confirmed."""
    tournament = _create_tournament(
        'Correction Blank Reason', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    entries_before = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )

    for blank_reason in ('', '   '):
        result = tournament_match_service.correct_match_result(
            match.id,
            admin_user.id,
            reason=blank_reason,
        )
        assert result.is_err()
        assert 'reason' in result.unwrap_err().lower()

    still_there = tournament_match_service.get_match(match.id)
    assert still_there.confirmed_by == admin_user.id

    entries_after = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    corrections = [
        e for e in entries_after
        if e.event_type == 'match-result-corrected'
    ]
    assert len(corrections) == len([
        e for e in entries_before
        if e.event_type == 'match-result-corrected'
    ])


def test_correct_match_result_case_c_requires_ack(
    party, user1, user2, user3, user4, admin_user, grant_ticket
):
    """Case C refuses without ack; succeeds with ack and applies scores."""
    tournament = _create_tournament(
        'Correction Case C Ack', max_players=4
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2, user3, user4], grant_ticket
    )
    final = _find_final(matches)
    feeders = [m for m in matches if m.next_match_id == final.id]
    assert len(feeders) >= 1

    for feeder in feeders:
        _confirm_match_with_scores(feeder, participants, admin_user)
    _confirm_match_with_scores(final, participants, admin_user)

    target = feeders[0]
    target_participants = {
        c.participant_id
        for c in tournament_match_service.get_contestants_for_match(
            target.id
        )
    }

    # Without acknowledgement: refused, nothing changes.
    result = tournament_match_service.correct_match_result(
        target.id,
        admin_user.id,
        reason='Semifinal score wrong',
    )
    assert result.is_err()
    assert 'acknowledgement' in result.unwrap_err()

    still_confirmed = tournament_match_service.get_match(target.id)
    assert still_confirmed.confirmed_by is not None

    # With acknowledgement: proceeds, applies corrected scores.
    corrected_scores = {
        participant_id: 13 if i == 0 else 9
        for i, participant_id in enumerate(sorted(target_participants))
    }
    result_ack = tournament_match_service.correct_match_result(
        target.id,
        admin_user.id,
        reason='Semifinal score wrong',
        corrected_scores=corrected_scores,
        ack_critical=True,
    )
    assert result_ack.is_ok()
    case, scores_applied = result_ack.unwrap()
    assert case is CorrectionCase.CASE_C
    assert scores_applied is True

    # The target match is re-confirmed with the corrected scores;
    # no automatic chain correction happens beyond the existing
    # cascade — the admin handles downstream manually.
    corrected = tournament_match_service.get_match(target.id)
    assert corrected.confirmed_by == admin_user.id
    contestants_after = (
        tournament_match_service.get_contestants_for_match(target.id)
    )
    for contestant in contestants_after:
        expected = corrected_scores[contestant.participant_id]
        assert contestant.score == expected

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    corrections = [
        e for e in entries
        if e.event_type == 'match-result-corrected'
    ]
    assert len(corrections) == 1
    assert corrections[0].data['case'] == 'case_c'
    assert corrections[0].data['scores_applied'] is True


def test_unconfirm_without_reason_back_compatible(
    party, user1, user2, admin_user, grant_ticket
):
    """Legacy unconfirm_match call sites keep working unchanged, and
    write no audit log entry.
    """
    tournament = _create_tournament(
        'Correction Legacy Unconfirm', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    result = tournament_match_service.unconfirm_match(
        match.id, admin_user.id
    )
    assert result.is_ok()

    unconfirmed = tournament_match_service.get_match(match.id)
    assert unconfirmed.confirmed_by is None

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert not any(
        e.event_type == 'match-result-retracted' for e in entries
    )


def test_unconfirm_with_reason_writes_retraction_entry(
    party, user1, user2, admin_user, grant_ticket
):
    """A direct unconfirm_match call with a reason writes exactly
    one retraction entry.
    """
    tournament = _create_tournament(
        'Unconfirm With Reason', max_players=2
    )

    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    result = tournament_match_service.unconfirm_match(
        match.id, admin_user.id, reason='Manual retraction for testing'
    )
    assert result.is_ok()

    unconfirmed = tournament_match_service.get_match(match.id)
    assert unconfirmed.confirmed_by is None

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    retractions = [
        e for e in entries
        if e.event_type == 'match-result-retracted'
    ]
    assert len(retractions) == 1

    entry = retractions[0]
    assert entry.initiator_id == admin_user.id
    assert entry.data['match_id'] == str(match.id)
    assert entry.data['reason'] == 'Manual retraction for testing'

    corrections = [
        e for e in entries
        if e.event_type == 'match-result-corrected'
    ]
    assert len(corrections) == 0
