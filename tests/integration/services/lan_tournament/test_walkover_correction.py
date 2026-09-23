"""
tests.integration.services.lan_tournament.test_walkover_correction
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A walkover has no result to correct.
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_log_service,
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


PARTY_ID = PartyID('lan-party-2026-walkover-correction')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Walkover Correction')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Walkover Correction Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'WalkoverFix{i:02d}') for i in range(3)]


@pytest.fixture(scope='module')
def ticketed(users, ticket_category):
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('WalkoverFixAdmin')


def _completed_three_player_bracket(ticketed, admin):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Walkover correction',
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
        tournament.id, initiator_id=admin.id
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
    (final,) = (m for m in main if m.round == 1)
    by_size = {
        len(tournament_match_service.get_contestants_for_match(m.id)): m
        for m in semis
    }
    walkover, played_semi = by_size[1], by_size[2]
    assert walkover.confirmed_by is not None

    for match in (played_semi, final):
        cs = tournament_match_service.get_contestants_for_match(match.id)
        confirmed = tournament_match_service.admin_set_and_confirm_match(
            match.id,
            admin.id,
            {cs[0].participant_id: 7, cs[1].participant_id: 2},
        )
        assert confirmed.is_ok(), confirmed.unwrap_err()

    return tournament, walkover, final


def test_retract_only_correction_of_a_walkover_changes_nothing(
    party, ticketed, admin
):
    tournament, walkover, final = _completed_three_player_bracket(
        ticketed, admin
    )
    entries_before = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )

    result = tournament_match_service.correct_match_result(
        walkover.id,
        admin.id,
        reason='the walkover was wrong',
        corrected_scores=None,
        ack_critical=True,
    )

    assert result.is_err()
    assert 'walkover' in result.unwrap_err()

    assert tournament_match_service.get_match(walkover.id).confirmed_by
    assert tournament_match_service.get_match(final.id).confirmed_by
    after = tournament_service.get_tournament(tournament.id)
    assert after.tournament_status == TournamentStatus.COMPLETED
    entries_after = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )
    assert [e.id for e in entries_after] == [e.id for e in entries_before]
