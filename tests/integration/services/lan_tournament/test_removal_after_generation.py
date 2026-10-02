"""
tests.integration.services.lan_tournament.test_removal_after_generation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Removing a participant after the bracket was generated, before the start.
"""

import dataclasses
from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_participant_service as participant_service,
    tournament_repository,
    tournament_seeding_service as svc,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-removal-after-generation')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('removalaftergenbrand', 'Removal After Generation')
    return make_party(brand, PARTY_ID, 'LAN Party Removal After Generation')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'RemovalAfterGenUser{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        contestant_type=ContestantType.SOLO,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    ):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Removal After Generation {next(_counter)}',
            contestant_type=contestant_type,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=elimination_mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users:
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _generate(tournament, admin):
    board = svc.get_board(tournament.id).unwrap()
    generated = svc.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()


def _entry_count(participant_id):
    db.session.rollback()
    return db.session.execute(
        text(
            'select count(*) from lan_tournament_match_contestants'
            ' where participant_id = :p'
        ),
        {'p': str(participant_id)},
    ).scalar()


def _team_entry_count(team_id):
    db.session.rollback()
    return db.session.execute(
        text(
            'select count(*) from lan_tournament_match_contestants'
            ' where team_id = :t'
        ),
        {'t': str(team_id)},
    ).scalar()


def _participants(tournament):
    db.session.rollback()
    return tournament_repository.get_participants_for_tournament(tournament.id)


def _status(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(tournament.id).tournament_status


def test_removal_after_generation_deletes_the_unplayed_entries(
    admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    victim = _participants(tournament)[0]
    assert _entry_count(victim.id) == 1

    result = participant_service.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    )

    assert result.is_ok(), result
    assert _entry_count(victim.id) == 0
    assert victim.id not in {p.id for p in _participants(tournament)}


def test_removal_after_generation_does_not_decide_a_match(
    admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    victim = _participants(tournament)[0]

    participant_service.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    ).unwrap()

    db.session.rollback()
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    assert all(m.confirmed_by is None for m in matches)


def test_removal_after_generation_stales_the_board_and_blocks_the_start(
    admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    victim = _participants(tournament)[0]

    participant_service.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    ).unwrap()

    db.session.rollback()
    board = svc.get_board(tournament.id).unwrap()
    assert board.stale
    assert str(victim.id) in board.stale_leaver_ids
    started = tournament_service.start_tournament(tournament.id, admin.id)
    assert started.is_err()
    assert started.unwrap_err() == svc.ERR_ROSTER_CHANGED
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED


def test_regenerating_after_the_removal_allows_the_start(
    admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    victim = _participants(tournament)[0]
    participant_service.admin_remove_participant(
        tournament.id, victim.id, initiator=admin
    ).unwrap()

    board = svc.get_board(tournament.id).unwrap()
    reseeded = svc.apply_action(
        tournament.id,
        svc.INITIAL_TARGET,
        svc.ReseedKeepTiers(),
        expected_version=board.version,
        initiator_id=admin.id,
    )
    assert reseeded.is_ok(), reseeded.unwrap_err()
    _generate(tournament, admin)

    started = tournament_service.start_tournament(tournament.id, admin.id)

    assert started.is_ok(), started
    assert _status(tournament) is TournamentStatus.ONGOING


def test_ticketless_removal_after_generation_deletes_the_entries(
    admin, make_tournament, party
):
    tournament = make_tournament()
    _generate(tournament, admin)
    everyone = _participants(tournament)

    result = participant_service.remove_participants_without_tickets(
        tournament.id, party.id, initiator_id=admin.id
    )

    assert result.is_ok(), result
    assert result.unwrap() == len(everyone)
    assert all(_entry_count(p.id) == 0 for p in everyone)


def test_admin_removal_view_redirects_with_a_flash(
    client, admin, make_tournament
):
    tournament = make_tournament()
    _generate(tournament, admin)
    victim = _participants(tournament)[0]

    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}'
        f'/participants/{victim.id}/remove'
    )

    assert response.status_code == 302
    assert response.location.endswith(
        f'/tournaments/{tournament.id}/participants'
    )
    assert _entry_count(victim.id) == 0
    page = client.get(response.location).get_data(as_text=True)
    assert 'Participant has been removed.' in page


def _generated_team_tournament(admin, make_tournament):
    tournament = make_tournament(contestant_type=ContestantType.TEAM)
    members = _participants(tournament)
    teams = []
    for i in range(0, 8, 2):
        team, _ = tournament_team_service.create_team(
            tournament.id, f'RemovalTeam{next(_counter)}', members[i].user_id
        ).unwrap()
        teams.append(team)
        tournament_repository.update_participant(
            dataclasses.replace(members[i + 1], team_id=team.id)
        )
    _generate(tournament, admin)
    return tournament, teams


def test_the_last_leaver_of_a_generated_team_deletes_the_team_entries(
    admin, make_tournament
):
    tournament, teams = _generated_team_tournament(admin, make_tournament)
    team = teams[0]
    mine = [p for p in _participants(tournament) if p.team_id == team.id]
    assert _team_entry_count(team.id) == 1
    captain = next(p for p in mine if p.user_id == team.captain_user_id)
    other = next(p for p in mine if p is not captain)

    assert tournament_team_service.leave_team(other.id).is_ok()
    left = tournament_team_service.leave_team(captain.id)

    assert left.is_ok(), left
    assert tournament_repository.find_team(team.id) is None
    assert _team_entry_count(team.id) == 0


def test_ticketless_removal_of_generated_teams_deletes_the_team_entries(
    admin, make_tournament, party
):
    tournament, teams = _generated_team_tournament(admin, make_tournament)
    assert all(_team_entry_count(t.id) == 1 for t in teams)

    result = participant_service.remove_participants_without_tickets(
        tournament.id, party.id, initiator_id=admin.id
    )

    assert result.is_ok(), result
    assert all(_team_entry_count(t.id) == 0 for t in teams)
    assert _participants(tournament) == []
