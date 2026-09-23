"""
tests.integration.services.lan_tournament.test_correction_acknowledgement
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An acknowledgement covers only the matches the admin was shown.
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


PARTY_ID = PartyID('lan-party-2026-correction-ack')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Correction Ack')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Correction Ack Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'CorrectionAck{i:02d}') for i in range(4)]


@pytest.fixture(scope='module')
def ticketed(users, ticket_category):
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('CorrectionAckAdmin')


def _play(match, admin):
    cs = tournament_match_service.get_contestants_for_match(match.id)
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, {cs[0].participant_id: 7, cs[1].participant_id: 2}
    )
    assert result.is_ok(), result.unwrap_err()


def _shown_acknowledgement(match_id):
    case, affected = tournament_match_service.classify_result_correction(
        match_id
    ).unwrap()
    return tournament_match_service.acknowledgement_match_ids(
        case, tournament_match_service.get_matches_by_ids(affected)
    )


def test_match_confirmed_after_the_page_loaded_needs_a_new_acknowledgement(
    party, ticketed, admin
):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Correction acknowledgement',
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
        assert tournament_participant_service.join_tournament(
            tournament.id, user.id
        ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()
    assert tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    main = [m for m in matches if m.bracket in (None, Bracket.WINNERS)]
    semi, other_semi = (m for m in main if m.round == 0)
    (final,) = (m for m in main if m.round == 1)
    (p3,) = (m for m in matches if m.bracket == Bracket.THIRD_PLACE)
    _play(semi, admin)
    _play(other_semi, admin)
    _play(final, admin)

    shown = _shown_acknowledgement(semi.id)
    assert shown == [final.id]

    _play(p3, admin)

    contestants = tournament_match_service.get_contestants_for_match(semi.id)
    corrected = {
        contestants[0].participant_id: 1,
        contestants[1].participant_id: 5,
    }

    stale = tournament_match_service.correct_match_result(
        semi.id,
        admin.id,
        reason='wrong winner entered',
        corrected_scores=corrected,
        ack_critical=True,
        acknowledged_match_ids=shown,
    )

    assert stale.is_err()
    assert 'acknowledge it again' in stale.unwrap_err()
    assert tournament_match_service.get_match(p3.id).confirmed_by is not None
    assert tournament_match_service.get_match(final.id).confirmed_by is not None

    fresh = tournament_match_service.correct_match_result(
        semi.id,
        admin.id,
        reason='wrong winner entered',
        corrected_scores=corrected,
        ack_critical=True,
        acknowledged_match_ids=_shown_acknowledgement(semi.id),
    )

    assert fresh.is_ok(), fresh.unwrap_err()
