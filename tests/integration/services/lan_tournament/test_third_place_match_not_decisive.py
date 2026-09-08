"""
tests.integration.services.lan_tournament.test_third_place_match_not_decisive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The third-place match must never decide the tournament.

``generate_single_elimination_bracket`` creates P3 with
``next_match_id=None``, the same shape the final has. Both the
auto-complete gate (``_try_auto_complete_tournament``) and the
retraction's tournament-state revert (``_unconfirm_match_impl``) used
to test terminality by ``next_match_id`` alone, so P3 satisfied it:
confirming P3 installed the bronze medalist as tournament winner, and
correcting P3 cleared the winner and then reinstalled the new bronze
medalist in the champion's place.

These run against real generator output rather than a hand-built
match graph, because the defect was precisely that the gate and the
generator disagreed about what "terminal" means. A mocked bracket
would have been built to whichever definition the test author had in
mind.
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-third-place')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Third Place')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Third Place Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'ThirdPlace{i:02d}') for i in range(4)]


@pytest.fixture(scope='module')
def ticketed(users, ticket_category):
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('ThirdPlaceAdmin')


def _started_se_bracket(name, ticketed):
    """Create, fill, generate and start a 4-player SE tournament.

    Four is the smallest size that produces semifinals, and so the
    smallest that produces a P3 match at all.
    """
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed:
        join = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join.is_ok(), join.unwrap_err()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()

    generated = tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    )
    assert generated.is_ok(), generated.unwrap_err()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    main = [m for m in matches if m.bracket in (None, Bracket.WINNERS)]
    semis = [m for m in main if m.round == 0]
    (final,) = [m for m in main if m.round == 1]
    (p3,) = [m for m in matches if m.bracket == Bracket.THIRD_PLACE]

    assert len(semis) == 2
    # The premise of this whole module: P3 looks terminal.
    assert p3.next_match_id is None
    assert final.next_match_id is None

    return tournament, semis, final, p3


def _play(match, admin, winner_score=10, loser_score=1):
    """Confirm a match, first listed real contestant winning."""
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    real = [c for c in contestants if c.participant_id is not None]
    assert len(real) == 2
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id,
        admin.id,
        {
            real[0].participant_id: winner_score,
            real[1].participant_id: loser_score,
        },
    )
    assert result.is_ok(), result.unwrap_err()
    return real[0].participant_id


def test_confirming_third_place_match_leaves_the_champion_alone(
    party, ticketed, admin
):
    """P3 confirmed after the final must not overwrite the winner."""
    tournament, semis, final, p3 = _started_se_bracket(
        'Third place after final', ticketed
    )

    for semi in semis:
        _play(semi, admin)

    champion = _play(final, admin)

    completed = tournament_service.get_tournament(tournament.id)
    assert completed.tournament_status == TournamentStatus.COMPLETED
    assert completed.winner_participant_id == champion

    bronze = _play(p3, admin)
    assert bronze != champion

    after = tournament_service.get_tournament(tournament.id)
    assert after.winner_participant_id == champion
    assert after.tournament_status == TournamentStatus.COMPLETED


def test_confirming_third_place_match_does_not_complete_tournament(
    party, ticketed, admin
):
    """P3 confirmed before the final must not complete anything.

    The reverse order of the test above: here the tournament has no
    champion yet, so the defect showed as P3 declaring one out of
    nothing and flipping the tournament to COMPLETED while the final
    was still unplayed.
    """
    tournament, semis, final, p3 = _started_se_bracket(
        'Third place before final', ticketed
    )

    for semi in semis:
        _play(semi, admin)

    _play(p3, admin)

    mid = tournament_service.get_tournament(tournament.id)
    assert mid.tournament_status == TournamentStatus.ONGOING
    assert mid.winner_participant_id is None

    champion = _play(final, admin)

    after = tournament_service.get_tournament(tournament.id)
    assert after.tournament_status == TournamentStatus.COMPLETED
    assert after.winner_participant_id == champion


def test_correcting_third_place_match_leaves_the_champion_alone(
    party, ticketed, admin
):
    """Correcting P3 must not touch the tournament winner or status.

    The correction path is the one that made this reachable in
    practice: view_match.html now routes every bracket retraction
    through the correction panel, and the panel's own
    ``correction_clears_winner`` banner used the same faulty
    terminality test.
    """
    tournament, semis, final, p3 = _started_se_bracket(
        'Third place corrected', ticketed
    )

    for semi in semis:
        _play(semi, admin)
    _play(p3, admin)
    champion = _play(final, admin)

    before = tournament_service.get_tournament(tournament.id)
    assert before.tournament_status == TournamentStatus.COMPLETED
    assert before.winner_participant_id == champion

    # Flip the P3 result the other way round.
    contestants = tournament_match_service.get_contestants_for_match(p3.id)
    real = [c for c in contestants if c.participant_id is not None]
    corrected = tournament_match_service.correct_match_result(
        p3.id,
        admin.id,
        reason='scorekeeper transposed the third-place scores',
        corrected_scores={
            real[0].participant_id: 2,
            real[1].participant_id: 9,
        },
    )
    assert corrected.is_ok(), corrected.unwrap_err()

    after = tournament_service.get_tournament(tournament.id)
    assert after.winner_participant_id == champion
    assert after.tournament_status == TournamentStatus.COMPLETED


def test_correcting_the_final_still_clears_the_champion(
    party, ticketed, admin
):
    """The P3 exclusion must not disarm the real terminal match.

    Counterpart to the tests above: the gates still have to fire for
    the match that genuinely decides the tournament, or the fix
    would have traded one wrong answer for another.
    """
    tournament, semis, final, p3 = _started_se_bracket(
        'Final corrected', ticketed
    )

    for semi in semis:
        _play(semi, admin)
    champion = _play(final, admin)

    before = tournament_service.get_tournament(tournament.id)
    assert before.tournament_status == TournamentStatus.COMPLETED
    assert before.winner_participant_id == champion

    contestants = tournament_match_service.get_contestants_for_match(final.id)
    real = [c for c in contestants if c.participant_id is not None]
    corrected = tournament_match_service.correct_match_result(
        final.id,
        admin.id,
        reason='final result entered for the wrong side',
        corrected_scores={
            real[0].participant_id: 3,
            real[1].participant_id: 11,
        },
    )
    assert corrected.is_ok(), corrected.unwrap_err()

    after = tournament_service.get_tournament(tournament.id)
    assert after.tournament_status == TournamentStatus.COMPLETED
    assert after.winner_participant_id == real[1].participant_id
    assert after.winner_participant_id != champion
