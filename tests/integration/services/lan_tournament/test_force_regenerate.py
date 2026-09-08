"""
tests.integration.services.lan_tournament.test_force_regenerate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A refused force-regenerate keeps the existing bracket.
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


PARTY_ID = PartyID('lan-party-2026-force-regenerate')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Force Regenerate')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Force Regenerate Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'ForceRegen{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('ForceRegenAdmin')


def test_refused_force_regenerate_keeps_the_bracket(party, ticketed, admin):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Force regenerate below minimum',
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
    participants = []
    for user in ticketed:
        join = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join.is_ok(), join.unwrap_err()
        participants.append(join.unwrap()[0])
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()
    assert tournament_match_service.generate_double_elimination_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()

    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    played = next(
        m
        for m in matches
        if m.bracket == Bracket.WINNERS
        and len(tournament_match_service.get_contestants_for_match(m.id)) == 2
    )
    cs = tournament_match_service.get_contestants_for_match(played.id)
    assert tournament_match_service.admin_set_and_confirm_match(
        played.id, admin.id, {cs[0].participant_id: 3, cs[1].participant_id: 1}
    ).is_ok()

    # Three contestants remain, below the double-elimination minimum.
    leaver = next(
        p for p in participants if p.id not in {c.participant_id for c in cs}
    )
    assert tournament_participant_service.admin_remove_participant(
        tournament.id, leaver.id, initiator=admin
    ).is_ok()
    match_ids_before = {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    }

    result = tournament_match_service.generate_double_elimination_bracket(
        tournament.id, force_regenerate=True, initiator_id=admin.id
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Need at least 4 contestants for double-elimination bracket.'
    )
    assert {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    } == match_ids_before
    assert tournament_match_service.get_match(played.id).confirmed_by
    assert not [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'bracket-cleared'
    ]
