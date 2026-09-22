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
    """Confirm every match of a generated SE bracket, round by round.

    Confirm the third-place match before the final, so the final's
    winner is the one recorded on the tournament.
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


def test_classify_no_downstream(
    party, user1, user2, admin_user, grant_ticket
):
    """A final match with no downstream classifies as NO_DOWNSTREAM."""
    tournament = _create_tournament(
        'Correction No Downstream', max_players=2
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
    assert case is CorrectionCase.NO_DOWNSTREAM
    assert affected == []


def test_classify_unconfirmed_downstream(
    party, user1, user2, user3, user4, grant_ticket
):
    """An upstream match of an unconfirmed match is UNCONFIRMED_DOWNSTREAM."""
    tournament = _create_tournament(
        'Correction Unconfirmed Downstream', max_players=4
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
    assert case is CorrectionCase.UNCONFIRMED_DOWNSTREAM
    assert final.id in affected


def test_classify_confirmed_downstream(
    party, user1, user2, user3, user4, admin_user, grant_ticket
):
    """An upstream match of a confirmed match is CONFIRMED_DOWNSTREAM."""
    tournament = _create_tournament(
        'Correction Confirmed Downstream', max_players=4
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
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert final.id in affected


def test_classify_confirmed_downstream_returns_full_transitive_chain(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    admin_user, grant_ticket,
):
    """Return the whole confirmed downstream chain, not just one hop."""
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
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert downstream_semi_id in affected
    assert final.id in affected
    assert p3.id in affected
    # Breadth-first: the direct feeder before the matches behind it.
    assert affected.index(downstream_semi_id) < affected.index(final.id)
    assert affected.index(downstream_semi_id) < affected.index(p3.id)


def test_classify_stops_traversal_at_unconfirmed_downstream(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    grant_ticket,
):
    """Include the direct downstream match, but stop there if unconfirmed."""
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

    target = quarters[0]
    downstream_semi_id = target.next_match_id
    assert downstream_semi_id in {s.id for s in semis}

    result = tournament_match_service.classify_result_correction(
        target.id
    )
    assert result.is_ok()
    case, affected = result.unwrap()
    assert case is CorrectionCase.UNCONFIRMED_DOWNSTREAM
    assert affected == [downstream_semi_id]
    assert final.id not in affected


def test_correct_match_result_confirmed_downstream_unwinds_full_chain(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    admin_user, grant_ticket,
):
    """Correcting a quarterfinal retracts the whole confirmed chain."""
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
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
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

    # The other semifinal and its feeders are untouched.
    untouched_semi = tournament_match_service.get_match(other_semi.id)
    assert untouched_semi.confirmed_by is not None

    reverted_tournament = tournament_service.get_tournament(tournament.id)
    assert reverted_tournament.tournament_status == TournamentStatus.ONGOING
    assert reverted_tournament.winner_participant_id is None
    assert reverted_tournament.winner_team_id is None


def test_correct_match_result_persists_log_entry_with_reason(
    party, user1, user2, admin_user, grant_ticket
):
    """A correction with re-entered scores logs `match-result-corrected`."""
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
    assert case is CorrectionCase.NO_DOWNSTREAM
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
    assert entry.data['case'] == 'no_downstream'
    assert entry.data['reason'] == 'Score entered incorrectly'
    assert entry.data['scores_applied'] is True


def test_correct_match_result_writes_retraction_entry(
    party, user1, user2, admin_user, grant_ticket
):
    """A correction without scores logs only the retraction entry."""
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
    assert case is CorrectionCase.NO_DOWNSTREAM
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


def test_retraction_entry_records_cascaded_downstream_scores(
    party,
    user1, user2, user3, user4, user5, user6, user7, user8,
    admin_user, grant_ticket,
):
    """Record the scores of every confirmed match the cascade clears."""
    tournament = _create_tournament(
        'Correction Cascade Score Log', max_players=8
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

    target = quarters[0]
    downstream_semi = tournament_match_service.get_match(
        target.next_match_id
    )

    # Read the numbers the cascade is about to destroy.
    expected_by_match = {}
    for match_id in (downstream_semi.id, final.id, p3.id):
        contestants = tournament_match_service.get_contestants_for_match(
            match_id
        )
        expected_by_match[str(match_id)] = {
            str(c.team_id or c.participant_id): c.score
            for c in contestants
            if (c.team_id or c.participant_id) is not None
        }
        assert any(
            score is not None
            for score in expected_by_match[str(match_id)].values()
        ), f'match {match_id} was expected to hold a confirmed result'

    result = tournament_match_service.correct_match_result(
        target.id,
        admin_user.id,
        reason='Quarterfinal result was wrong',
        ack_critical=True,
    )
    assert result.is_ok()
    assert result.unwrap()[0] is CorrectionCase.CONFIRMED_DOWNSTREAM

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    retractions = [
        e for e in entries if e.event_type == 'match-result-retracted'
    ]
    assert len(retractions) == 1

    cascaded = retractions[0].data['cascaded_retracted_scores']
    for match_id_str, expected_scores in expected_by_match.items():
        assert match_id_str in cascaded, (
            f'match {match_id_str} was cleared by the cascade but its '
            'scores were not recorded'
        )
        assert cascaded[match_id_str] == expected_scores

    # The subject match keeps its own separate snapshot.
    assert retractions[0].data['retracted_scores']

    # And the numbers really are gone from the database.
    for match_id in (downstream_semi.id, final.id, p3.id):
        contestants = tournament_match_service.get_contestants_for_match(
            match_id
        )
        assert all(c.score is None for c in contestants)


def test_retraction_entry_cascade_scores_empty_without_downstream(
    party, user1, user2, admin_user, grant_ticket
):
    """Without confirmed downstream matches, record an empty snapshot."""
    tournament = _create_tournament(
        'Correction Cascade Score Log Empty', max_players=2
    )
    matches, participants = _setup_se_bracket(
        tournament, [user1, user2], grant_ticket
    )
    match = matches[0]
    _confirm_match_with_scores(match, participants, admin_user)

    result = tournament_match_service.correct_match_result(
        match.id, admin_user.id, reason='Score entered incorrectly'
    )
    assert result.is_ok()

    entries = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    retraction = next(
        e for e in entries if e.event_type == 'match-result-retracted'
    )
    assert retraction.data['cascaded_retracted_scores'] == {}


def test_correct_match_result_writes_both_entries_when_scores_reentered(
    party, user1, user2, admin_user, grant_ticket
):
    """Log the retraction entry, then the correction entry."""
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
    assert case is CorrectionCase.NO_DOWNSTREAM
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
    """Assert the match is untouched and no log entry exists."""
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
    """Reject a negative score before the retraction runs."""
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
    """Reject a score above `MAX_MATCH_SCORE` before any write."""
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
    """Reject scores missing a contestant before any write."""
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
    """Read a match, its contestants and log entries via a new connection.

    It sees only committed data, unlike the shared session.
    """
    # The fixtures also write status-change entries; ignore those.
    correction_event_types = frozenset(
        {
            'match-result-retracted',
            'match-result-corrected',
        }
    )

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
            [
                e.event_type
                for e in fresh_entries
                if e.event_type in correction_event_types
            ],
        )


def test_correct_match_result_leaves_nothing_committed_when_retraction_log_write_fails(
    party, user1, user2, admin_user, grant_ticket, monkeypatch
):
    """A raising retraction log write leaves nothing committed."""
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

    # The raise triggered the rollback.
    assert len(rollback_calls) == 1

    fresh_match, fresh_scores, fresh_event_types = (
        _read_via_fresh_connection(tournament, match.id)
    )
    assert fresh_match.confirmed_by == admin_user.id
    assert fresh_scores == original_contestants
    assert fresh_event_types == []


def test_correct_match_result_leaves_nothing_committed_when_correction_log_write_fails(
    party, user1, user2, admin_user, grant_ticket, monkeypatch
):
    """A raising correction log write leaves nothing committed."""
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
    original_contestants = {
        contestant.participant_id: contestant.score
        for contestant in contestants
    }
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

    # The raise triggered the rollback.
    assert len(rollback_calls) == 1

    # A separate connection sees the pre-correction state in full.
    fresh_match, fresh_scores, fresh_event_types = (
        _read_via_fresh_connection(tournament, match.id)
    )
    assert fresh_match.confirmed_by == admin_user.id
    assert fresh_scores == original_contestants
    assert all(score is not None for score in fresh_scores.values())
    assert fresh_event_types == []


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


def test_correct_match_result_confirmed_downstream_requires_ack(
    party, user1, user2, user3, user4, admin_user, grant_ticket
):
    """CONFIRMED_DOWNSTREAM needs an acknowledgement, then applies."""
    tournament = _create_tournament(
        'Correction Confirmed Downstream Ack', max_players=4
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
    assert case is CorrectionCase.CONFIRMED_DOWNSTREAM
    assert scores_applied is True

    # The target match is re-confirmed with the corrected scores.
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
    assert corrections[0].data['case'] == 'confirmed_downstream'
    assert corrections[0].data['scores_applied'] is True


def test_unconfirm_without_reason_back_compatible(
    party, user1, user2, admin_user, grant_ticket
):
    """`unconfirm_match` without a reason writes no log entry."""
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
    """`unconfirm_match` with a reason writes one retraction entry."""
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
