"""
tests.integration.services.lan_tournament.test_audit_log_coverage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Admin writes that set or destroy results leave an audit entry.
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


PARTY_ID = PartyID('lan-party-2026-audit-coverage')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Audit Coverage')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Audit Coverage Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'AuditCoverage{i:02d}') for i in range(4)]


@pytest.fixture(scope='module')
def ticketed(users, ticket_category):
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('AuditCoverageAdmin')


def _started(name, ticketed, **kwargs):
    result = tournament_service.create_tournament(
        PARTY_ID, name, contestant_type=ContestantType.SOLO, **kwargs
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
    return tournament


def _entries(tournament_id, event_type):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament_id
        )
        if e.event_type == event_type
    ]


def _started_se(name, ticketed, admin):
    tournament = _started(
        name,
        ticketed,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        max_players=8,
    )
    assert tournament_match_service.generate_single_elimination_bracket(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()
    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    semi = next(m for m in matches if m.round == 0 and m.bracket is None)
    return tournament, semi


def test_admin_result_entry_is_logged(party, ticketed, admin):
    tournament, semi = _started_se('Audit admin entry', ticketed, admin)
    a, b = tournament_match_service.get_contestants_for_match(semi.id)

    result = tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, {a.participant_id: 7, b.participant_id: 2}
    )

    assert result.is_ok(), result.unwrap_err()
    (entry,) = _entries(tournament.id, 'match-result-entered')
    assert entry.initiator_id == admin.id
    assert entry.data == {
        'match_id': str(semi.id),
        'scores': {str(a.participant_id): 7, str(b.participant_id): 2},
    }


def test_force_regenerate_logs_the_results_it_deletes(party, ticketed, admin):
    tournament, semi = _started_se('Audit regenerate', ticketed, admin)
    a, b = tournament_match_service.get_contestants_for_match(semi.id)
    assert tournament_match_service.admin_set_and_confirm_match(
        semi.id, admin.id, {a.participant_id: 7, b.participant_id: 2}
    ).is_ok()
    match_count = len(
        tournament_match_service.get_matches_for_tournament(tournament.id)
    )

    regenerated = tournament_match_service.generate_single_elimination_bracket(
        tournament.id, force_regenerate=True, initiator_id=admin.id
    )

    assert regenerated.is_ok(), regenerated.unwrap_err()
    (entry,) = _entries(tournament.id, 'bracket-cleared')
    assert entry.initiator_id == admin.id
    assert entry.data['match_count'] == match_count
    assert entry.data['confirmed_results'] == {
        str(semi.id): {str(a.participant_id): 7, str(b.participant_id): 2}
    }


def _ffa_match(name, ticketed, admin):
    tournament = _started(
        name,
        ticketed,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
    )
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()
    assert tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    (match,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    placements = {
        str(c.participant_id): i + 1 for i, c in enumerate(contestants)
    }
    assert tournament_match_service.set_ffa_placements(
        match.id, placements
    ).is_ok()
    return tournament, match, placements


def test_ffa_confirmation_is_logged(party, ticketed, admin):
    tournament, match, placements = _ffa_match(
        'Audit FFA confirm', ticketed, admin
    )

    result = tournament_match_service.confirm_ffa_match(match.id, admin.id)

    assert result.is_ok(), result.unwrap_err()
    (entry,) = _entries(tournament.id, 'ffa-match-confirmed')
    assert entry.initiator_id == admin.id
    assert entry.data['match_id'] == str(match.id)
    assert {
        key: value['placement']
        for key, value in entry.data['placements'].items()
    } == placements


def test_ffa_retraction_records_the_placements(party, ticketed, admin):
    tournament, match, placements = _ffa_match(
        'Audit FFA retract', ticketed, admin
    )
    assert tournament_match_service.confirm_ffa_match(
        match.id, admin.id
    ).is_ok()

    result = tournament_match_service.unconfirm_match(
        match.id, admin.id, reason='wrong order'
    )

    assert result.is_ok(), result.unwrap_err()
    (entry,) = _entries(tournament.id, 'match-result-retracted')
    assert {
        key: value['placement']
        for key, value in entry.data['retracted_placements'].items()
    } == placements
