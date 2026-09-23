"""
tests.integration.services.lan_tournament.test_ffa_confirmed_placements
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A confirmed FFA result changes only through the audited unconfirm.
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
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-ffa-confirmed-placements')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 FFA Placements')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'FFA Placements Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'FfaPlacements{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaPlacementsAdmin')


def _placements(match_id):
    return {
        str(c.participant_id): c.placement
        for c in tournament_match_service.get_contestants_for_match(match_id)
    }


def test_confirmed_ffa_placements_cannot_be_overwritten(party, ticketed, admin):
    result = tournament_service.create_tournament(
        PARTY_ID,
        'FFA confirmed placements',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
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
    for status in (
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    ):
        assert tournament_service.change_status(tournament.id, status).is_ok()
    assert tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    (group,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )

    contestants = tournament_match_service.get_contestants_for_match(group.id)
    assert tournament_match_service.set_ffa_placements(
        group.id,
        {str(c.participant_id): i + 1 for i, c in enumerate(contestants)},
    ).is_ok()
    assert tournament_match_service.confirm_ffa_match(
        group.id, admin.id
    ).is_ok()
    confirmed = _placements(group.id)
    entries_before = tournament_log_service.get_entries_for_tournament(
        tournament.id
    )

    result = tournament_match_service.set_ffa_placements(
        group.id,
        {cid: len(confirmed) + 1 - p for cid, p in confirmed.items()},
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Cannot modify placements of a confirmed match.'
    )
    assert _placements(group.id) == confirmed
    assert tournament_match_service.get_match(group.id).confirmed_by
    assert (
        tournament_log_service.get_entries_for_tournament(tournament.id)
        == entries_before
    )
