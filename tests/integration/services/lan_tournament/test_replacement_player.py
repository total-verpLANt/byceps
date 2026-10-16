"""
tests.integration.services.lan_tournament.test_replacement_player
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An admin replaces a removed team member while the tournament runs.
"""

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_participant_service as participant_service,
    tournament_repository,
    tournament_service,
    tournament_team_service as team_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'ReplacementPlayer{i}') for i in range(7)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


def _running_team_tournament(party, users, status):
    """Two full teams of two in a tournament with no free slot."""
    result = tournament_service.create_tournament(
        party.id,
        f'Replacement {generate_uuid7()}',
        contestant_type=ContestantType.TEAM,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        max_players=4,
        max_players_in_team=2,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    teams = []
    for captain, member in [(users[0], users[1]), (users[2], users[3])]:
        added = participant_service.admin_add_participant(
            tournament.id, captain.id
        )
        assert added.is_ok(), added.unwrap_err()
        created = team_service.create_team(
            tournament.id, f'Team {generate_uuid7()}', captain.id
        )
        assert created.is_ok(), created.unwrap_err()
        team, _ = created.unwrap()
        added = participant_service.admin_add_participant(
            tournament.id, member.id, team_id=team.id
        )
        assert added.is_ok(), added.unwrap_err()
        teams.append(team)

    db.session.get(DbTournament, tournament.id).tournament_status = status.name
    db.session.commit()
    return tournament, teams


def _team_user_ids(team_id):
    return {m.user_id for m in team_service.get_team_members(team_id)}


def _entries(tournament_id):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament_id
        )
        if e.event_type == 'participant-replacement-added'
    ]


@pytest.mark.parametrize(
    'status', [TournamentStatus.ONGOING, TournamentStatus.PAUSED]
)
def test_replacement_joins_the_team_of_a_running_tournament(
    party, users, admin, status
):
    tournament, teams = _running_team_tournament(party, users, status)
    team = teams[0]
    removed = team_service.remove_team_member(team.id, users[1].id)
    assert removed.is_ok(), removed.unwrap_err()

    result = participant_service.admin_add_participant(
        tournament.id, users[4].id, team_id=team.id, initiator=admin
    )

    assert result.is_ok(), result.unwrap_err()
    participant, _ = result.unwrap()
    assert participant.team_id == team.id
    assert _team_user_ids(team.id) == {users[0].id, users[4].id}
    entries = _entries(tournament.id)
    assert len(entries) == 1
    assert entries[0].initiator_id == admin.id
    assert entries[0].data['user_id'] == str(users[4].id)
    assert entries[0].data['team_id'] == str(team.id)
    assert entries[0].data['roster_before'] == 4
    assert entries[0].data['roster_after'] == 5


def test_running_tournament_refuses_a_participant_without_a_team(
    party, users, admin
):
    tournament, _ = _running_team_tournament(
        party, users, TournamentStatus.ONGOING
    )

    result = participant_service.admin_add_participant(
        tournament.id, users[4].id, initiator=admin
    )

    assert result.unwrap_err() == (
        'Participants can only be added during registration.'
    )
    assert (
        tournament_repository.find_participant_by_user(
            tournament.id, users[4].id
        )
        is None
    )


def test_running_tournament_refuses_a_replacement_on_a_full_team(
    party, users, admin
):
    tournament, teams = _running_team_tournament(
        party, users, TournamentStatus.ONGOING
    )

    result = participant_service.admin_add_participant(
        tournament.id, users[4].id, team_id=teams[0].id, initiator=admin
    )

    assert result.unwrap_err() == 'Team is full.'
    assert (
        tournament_repository.find_participant_by_user(
            tournament.id, users[4].id
        )
        is None
    )
    assert _entries(tournament.id) == []


def test_completed_tournament_refuses_a_replacement(party, users, admin):
    tournament, teams = _running_team_tournament(
        party, users, TournamentStatus.COMPLETED
    )
    removed = team_service.remove_team_member(teams[0].id, users[1].id)
    assert removed.is_ok(), removed.unwrap_err()

    result = participant_service.admin_add_participant(
        tournament.id, users[4].id, team_id=teams[0].id, initiator=admin
    )

    assert result.unwrap_err() == (
        'Participants can only be added during registration.'
    )


def test_admin_add_member_route_registers_an_unregistered_replacement(
    admin_app, party, users, admin
):
    tournament, teams = _running_team_tournament(
        party, users, TournamentStatus.ONGOING
    )
    team = teams[0]
    removed = team_service.remove_team_member(team.id, users[1].id)
    assert removed.is_ok(), removed.unwrap_err()

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.post(
            f'{ADMIN_URL}/teams/{team.id}/add_member',
            data={'screen_name': users[5].screen_name},
        )

    assert response.status_code == 302
    db.session.expire_all()
    assert _team_user_ids(team.id) == {users[0].id, users[5].id}
    assert len(_entries(tournament.id)) == 1


def test_admin_add_member_route_moves_a_registered_free_player(
    admin_app, party, users, admin
):
    tournament, teams = _running_team_tournament(
        party, users, TournamentStatus.ONGOING
    )
    team = teams[0]
    removed = team_service.remove_team_member(team.id, users[1].id)
    assert removed.is_ok(), removed.unwrap_err()

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.post(
            f'{ADMIN_URL}/teams/{team.id}/add_member',
            data={'screen_name': users[1].screen_name},
        )

    assert response.status_code == 302
    db.session.expire_all()
    assert _team_user_ids(team.id) == {users[0].id, users[1].id}
    assert _entries(tournament.id) == []


def test_running_solo_tournament_refuses_a_participant_via_a_team(
    party, users, admin, make_user
):
    result = tournament_service.create_tournament(
        party.id,
        f'Solo replacement {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        max_players=2,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    for user in users[:2]:
        added = participant_service.admin_add_participant(
            tournament.id, user.id
        )
        assert added.is_ok(), added.unwrap_err()
    created = team_service.create_team(
        tournament.id, f'Team {generate_uuid7()}', users[0].id
    )
    assert created.is_ok(), created.unwrap_err()
    team, _ = created.unwrap()
    status = TournamentStatus.ONGOING
    db.session.get(DbTournament, tournament.id).tournament_status = status.name
    db.session.commit()
    outsider = make_user(f'SoloReplacementOutsider{generate_uuid7()}')

    result = participant_service.admin_add_participant(
        tournament.id, outsider.id, team_id=team.id, initiator=admin
    )

    assert result.unwrap_err() == (
        'Participants can only be added during registration.'
    )
    assert (
        tournament_repository.find_participant_by_user(
            tournament.id, outsider.id
        )
        is None
    )
    assert _entries(tournament.id) == []
