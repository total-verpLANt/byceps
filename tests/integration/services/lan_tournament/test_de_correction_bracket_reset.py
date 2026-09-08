"""
tests.integration.services.lan_tournament.test_de_correction_bracket_reset
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Correct a double-elimination grand final after a bracket reset.

Why this exists: ``test_match_result_correction.py`` is
single-elimination throughout, so ``BRACKET_RESET_DELETION`` -- the one
correction case that DELETES a match row together with its contestants
and comments rather than retracting it -- had no coverage against a
real database. Its unit coverage patches the repository, which is
exactly where a foreign-key ordering mistake or a missed re-creation
would hide.

The path exercised end to end: LB champion wins GF M1, the bracket
resets and GF M2 appears; an admin then corrects GF M1 so the WB
champion wins after all. The cascade must delete GF M2, re-confirm
GF M1 with the corrected scores in the same transaction, and leave the
tournament completed with the corrected winner -- no reset match left
behind, no half-applied correction.
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
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-de-reset')

# Guards the drain loop below against an unwired bracket turning a
# failure into a hang.
MAX_CONFIRM_ROUNDS = 40


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 DE Reset')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'DE Reset Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'DeReset{i:02d}') for i in range(4)]


@pytest.fixture(scope='module')
def admin_user(make_user):
    return make_user('DeResetAdmin')


@pytest.fixture(scope='module')
def grant_ticket(ticket_category):
    def _grant(user):
        return ticket_creation_service.create_ticket(
            ticket_category, user, user=user
        )

    return _grant


def _real_contestants(match_id):
    return [
        c
        for c in tournament_match_service.get_contestants_for_match(match_id)
        if c.participant_id is not None or c.team_id is not None
    ]


def _contestant_key(contestant):
    return contestant.team_id or contestant.participant_id


def _build_started_de_tournament(users, grant_ticket):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'DE bracket reset correction',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()

    for user in users:
        grant_ticket(user)
        join_result = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join_result.is_ok(), join_result.unwrap_err()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()

    generate_result = (
        tournament_match_service.generate_double_elimination_bracket(
            tournament.id
        )
    )
    assert generate_result.is_ok(), generate_result.unwrap_err()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()

    return tournament


def _play_bracket_up_to_grand_final(tournament, admin_user):
    """Confirm every match except GF M1; return GF M1.

    Plays whatever is playable rather than assuming a fixed bracket
    shape: a match becomes playable once both its contestants have
    advanced into it, so repeating the sweep walks the WB and LB in
    dependency order without this test hard-coding the wiring.
    """
    grand_final = None

    for _ in range(MAX_CONFIRM_ROUNDS):
        matches = tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
        grand_final = next(
            (
                m
                for m in matches
                if m.bracket is Bracket.GRAND_FINAL and m.match_order == 0
            ),
            None,
        )
        playable = [
            m
            for m in matches
            if m.confirmed_by is None
            and (grand_final is None or m.id != grand_final.id)
            and len(_real_contestants(m.id)) == 2
        ]
        if not playable:
            break

        match = playable[0]
        contestants = _real_contestants(match.id)
        confirm_result = (
            tournament_match_service.admin_set_and_confirm_match(
                match.id,
                admin_user.id,
                {
                    _contestant_key(contestants[0]): 10,
                    _contestant_key(contestants[1]): 1,
                },
            )
        )
        assert confirm_result.is_ok(), confirm_result.unwrap_err()

    assert grand_final is not None, 'no grand final in the generated bracket'
    return grand_final


def test_correcting_grand_final_deletes_bracket_reset_match(
    party, users, admin_user, grant_ticket
):
    tournament = _build_started_de_tournament(users, grant_ticket)
    grand_final = _play_bracket_up_to_grand_final(tournament, admin_user)

    gf_contestants = _real_contestants(grand_final.id)
    assert len(gf_contestants) == 2

    # The LB champion is the winner of the losers-bracket match that
    # routes into the grand final.
    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    lb_final = next(
        m
        for m in matches
        if m.bracket is Bracket.LOSERS and m.next_match_id == grand_final.id
    )
    lb_champion_key = _contestant_key(
        max(_real_contestants(lb_final.id), key=lambda c: c.score or 0)
    )
    wb_champion_key = next(
        _contestant_key(c)
        for c in gf_contestants
        if _contestant_key(c) != lb_champion_key
    )

    # LB champion takes GF M1 -> the bracket resets and GF M2 appears.
    confirm_result = tournament_match_service.admin_set_and_confirm_match(
        grand_final.id,
        admin_user.id,
        {lb_champion_key: 10, wb_champion_key: 1},
    )
    assert confirm_result.is_ok(), confirm_result.unwrap_err()

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert any(
        m.bracket is Bracket.GRAND_FINAL and m.match_order == 1
        for m in matches
    ), 'bracket reset did not create GF M2'

    classification = tournament_match_service.classify_result_correction(
        grand_final.id
    )
    assert classification.is_ok(), classification.unwrap_err()
    case, _affected = classification.unwrap()
    assert case is CorrectionCase.BRACKET_RESET_DELETION

    # The result was recorded the wrong way round: correct it so the
    # WB champion wins and no reset is warranted.
    correction_result = tournament_match_service.correct_match_result(
        grand_final.id,
        admin_user.id,
        reason='scores were recorded for the wrong side',
        corrected_scores={wb_champion_key: 10, lb_champion_key: 2},
        ack_critical=True,
    )
    assert correction_result.is_ok(), correction_result.unwrap_err()
    corrected_case, scores_applied = correction_result.unwrap()
    assert corrected_case is CorrectionCase.BRACKET_RESET_DELETION
    assert scores_applied is True

    matches = tournament_match_service.get_matches_for_tournament(
        tournament.id
    )
    assert not [
        m
        for m in matches
        if m.bracket is Bracket.GRAND_FINAL and m.match_order == 1
    ], 'GF M2 survived the correction'

    reconfirmed = tournament_match_service.get_match(grand_final.id)
    assert reconfirmed.confirmed_by is not None
    assert reconfirmed.next_match_id is None

    tournament = tournament_service.get_tournament(tournament.id)
    assert tournament.tournament_status is TournamentStatus.COMPLETED
    assert tournament.winner_participant_id == wb_champion_key
