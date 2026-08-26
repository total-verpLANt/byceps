"""Admin removal of team members, captain handover and the acting-captain checks.

Real PostgreSQL, the real services and the real admin and site apps.
"""

from datetime import datetime, timedelta, UTC
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

from flask import current_app
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_invitation_service as invitations,
    tournament_participant_service as participants,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service as lifecycle,
    tournament_team_service as teams,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.util.result import Err
from byceps.util.uuid import uuid7

from tests.helpers import http_client, log_in_user


ADMIN_ROOT = 'http://admin.acmecon.test/lan-tournaments'
SITE_ROOT = 'http://www.acmecon.test/lan-tournaments'
ENGLISH = {'Accept-Language': 'en'}
CAPTAIN_LEAVE_ERROR = (
    'Team captain cannot leave while team has other members. '
    'Transfer captain role first or have other members leave.'
)
NOT_CAPTAIN_ERROR = 'Only the team captain can update this team.'


@pytest.fixture(scope='module')
def users(make_user):
    created = [make_user(f'Fc3A{i}-{uuid7().hex[-8:]}') for i in range(6)]
    for user in created:
        log_in_user(user.id)
    return created


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


def _purge(tournament_id):
    db.session.rollback()
    if repo.find_tournament(tournament_id) is not None:
        lifecycle.delete_tournament(tournament_id)
    for model in (DbMatchInvitation, DbMatchPairing, DbTournamentLogEntry):
        db.session.execute(
            delete(model).where(model.tournament_id == tournament_id)
        )
    db.session.commit()


@pytest.fixture
def build_world(party, users, monkeypatch):
    """Two teams of three; members join in index order, index 0 captains."""
    created = []
    sends = {}
    for name in (
        'team_member_joined',
        'team_member_left',
        'team_deleted',
        'captain_transferred',
        'participant_joined',
        'participant_left',
        'contestant_advanced',
        'match_confirmed',
        'tournament_completed',
    ):
        sends[name] = Mock()
        monkeypatch.setattr(getattr(signals, name), 'send', sends[name])
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', Mock())

    def build(status='ONGOING'):
        now = datetime.now(UTC).replace(tzinfo=None)
        tournament = DbTournament(
            uuid7(),
            party.id,
            f'Fc3 roster {uuid7()}',
            now,
            contestant_type='TEAM',
            game_format='ONE_V_ONE',
            elimination_mode='SINGLE_ELIMINATION',
            tournament_status=status,
            max_players_in_team=8,
        )
        tournament_id = tournament.id
        created.append(tournament_id)
        db.session.add(tournament)
        db.session.flush()
        team_rows = [
            DbTournamentTeam(
                uuid7(), tournament_id, f'Fc3 team {i}', users[i * 3].id, now
            )
            for i in range(2)
        ]
        db.session.add_all(team_rows)
        db.session.flush()
        roster = [
            DbTournamentParticipant(
                uuid7(),
                user.id,
                tournament_id,
                now + timedelta(seconds=i),
                team_id=team_rows[i // 3].id,
            )
            for i, user in enumerate(users)
        ]
        db.session.add_all(roster)
        db.session.flush()
        match_id = None
        if status == 'ONGOING':
            match = DbTournamentMatch(
                uuid7(), tournament_id, now, match_order=0, round=0
            )
            match_id = match.id
            db.session.add(match)
            db.session.flush()
            for team in team_rows:
                db.session.add(
                    DbTournamentMatchToContestant(
                        uuid7(), match_id, now, team_id=team.id
                    )
                )
            db.session.flush()
            repo.get_tournament_for_update(tournament_id)
            repo.get_match_for_update(match_id)
            assert repo.refresh_match_pairing_flush(
                match_id, occurred_at=now
            ).is_ok()
        db.session.commit()
        return SimpleNamespace(
            tournament_id=tournament_id,
            party_id=party.id,
            match_id=match_id,
            team_ids=[t.id for t in team_rows],
            participant_ids=[p.id for p in roster],
            users=users,
            sends=sends,
        )

    yield build
    db.session.rollback()
    for tournament_id in created:
        _purge(tournament_id)


# -- facts, read through a connection of their own --


def _is_removed(world, index):
    with Session(db.engine) as session:
        row = session.get(DbTournamentParticipant, world.participant_ids[index])
        return row is None or row.removed_at is not None


def _captain_user_id(world, team_index):
    with Session(db.engine) as session:
        return session.get(
            DbTournamentTeam, world.team_ids[team_index]
        ).captain_user_id


def _active_user_ids(world, team_index):
    with Session(db.engine) as session:
        return set(
            session.scalars(
                select(DbTournamentParticipant.user_id).where(
                    DbTournamentParticipant.team_id
                    == world.team_ids[team_index],
                    DbTournamentParticipant.removed_at.is_(None),
                )
            )
        )


def _hollow_out_captain_row(world):
    """Soft-delete the captain's row but leave the captaincy where it is."""
    with Session(db.engine) as session:
        session.execute(
            DbTournamentParticipant.__table__.update()
            .where(DbTournamentParticipant.id == world.participant_ids[0])
            .values(removed_at=datetime.now(UTC))
        )
        session.commit()


def _claim_ready(world, user, team_index):
    db.session.rollback()
    pairing = repo.get_match_pairing(world.match_id)
    side = (
        MatchSide.A
        if pairing.side_a.id == world.team_ids[team_index]
        else MatchSide.B
    )
    with Session(db.engine) as session:
        match = session.get(DbTournamentMatch, world.match_id)
        generation, revision = (
            match.pairing_generation,
            match.readiness_revision,
        )
    result = readiness.claim_ready_flush(
        world.match_id,
        side,
        user.id,
        expected_pairing_generation=generation,
        expected_readiness_revision=revision,
    )
    repo.rollback_session()
    return result


# -- A1: admin removal of a team member with an id from the URL --


def test_admin_route_removes_a_team_member_during_ongoing(
    admin_app, admin, build_world, users, monkeypatch
):
    world = build_world()
    refreshed = []
    real_refresh = readiness.refresh_pairing_and_invitations_flush

    def spy(match_id, *, occurred_at):
        refreshed.append(match_id)
        return real_refresh(match_id, occurred_at=occurred_at)

    monkeypatch.setattr(readiness, 'refresh_pairing_and_invitations_flush', spy)

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.post(
            f'{ADMIN_ROOT}/tournaments/{world.tournament_id}'
            f'/participants/{world.participant_ids[2]}/remove',
            headers=ENGLISH,
        )
        assert response.status_code == 302
        page = client.get(response.location, headers=ENGLISH).get_data(
            as_text=True
        )

    assert 'Participant has been removed.' in page
    assert _is_removed(world, 2)
    assert _active_user_ids(world, 0) == {users[0].id, users[1].id}
    assert refreshed == [world.match_id]
    (call,) = world.sends['team_member_left'].call_args_list
    assert call.kwargs['event'].participant_id == world.participant_ids[2]
    assert call.kwargs['event'].team_id == world.team_ids[0]
    assert world.sends['participant_left'].call_count == 1


def test_service_accepts_a_str_participant_id(admin, build_world, users):
    world = build_world()

    result = participants.admin_remove_participant(
        world.tournament_id, str(world.participant_ids[1]), initiator=admin
    )

    assert result.is_ok(), result
    assert result.unwrap().participant_id == world.participant_ids[1]
    assert _is_removed(world, 1)
    assert _active_user_ids(world, 0) == {users[0].id, users[2].id}


def test_admin_route_rejects_a_malformed_participant_id(
    admin_app, admin, build_world
):
    world = build_world()
    with http_client(admin_app, user_id=admin.id) as client:
        response = client.post(
            f'{ADMIN_ROOT}/tournaments/{world.tournament_id}'
            '/participants/not-a-uuid/remove'
        )

    assert response.status_code == 404


def test_site_join_team_hands_uuids_to_the_service(
    site_app, build_world, users
):
    world = build_world(status='REGISTRATION_OPEN')
    with Session(db.engine) as session:
        session.execute(
            DbTournamentParticipant.__table__.update()
            .where(DbTournamentParticipant.id == world.participant_ids[2])
            .values(team_id=None)
        )
        session.commit()

    with http_client(site_app, user_id=users[2].id) as client:
        response = client.post(
            f'{SITE_ROOT}/teams/{world.team_ids[0]}/join', headers=ENGLISH
        )

    assert response.status_code == 302
    assert _active_user_ids(world, 0) == {
        users[0].id,
        users[1].id,
        users[2].id,
    }
    (call,) = world.sends['team_member_joined'].call_args_list
    assert call.kwargs['event'].team_id == world.team_ids[0]


# -- A2: captain handover and the acting-captain checks --


def test_admin_removal_of_the_captain_hands_the_captaincy_over(
    admin, build_world, users
):
    world = build_world()

    result = participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=admin
    )

    assert result.is_ok(), result
    assert _is_removed(world, 0)
    # The longest-standing remaining member inherits, as in the ticketless path.
    assert _captain_user_id(world, 0) == users[1].id
    claimed = _claim_ready(world, users[1], 0)
    assert claimed.is_ok(), claimed


def test_admin_removal_of_a_captain_leaves_the_other_team_alone(
    admin, build_world, users
):
    world = build_world()

    participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=admin
    ).unwrap()

    assert _captain_user_id(world, 1) == users[3].id


def test_admin_removal_of_a_member_keeps_the_captain(admin, build_world, users):
    world = build_world()

    participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[1], initiator=admin
    ).unwrap()

    assert _captain_user_id(world, 0) == users[0].id


def test_admin_removal_of_the_last_member_deletes_the_team_as_before(
    admin, build_world, users
):
    world = build_world()
    for index in (1, 2, 0):
        participants.admin_remove_participant(
            world.tournament_id, world.participant_ids[index], initiator=admin
        ).unwrap()

    assert _active_user_ids(world, 0) == set()
    assert world.sends['team_deleted'].call_count == 1


def test_removed_ex_captain_cannot_kick_members_on_the_site(
    site_app, admin, build_world, users
):
    world = build_world()
    participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=admin
    ).unwrap()

    with http_client(site_app, user_id=users[0].id) as client:
        response = client.post(
            f'{SITE_ROOT}/{world.tournament_id}/teams/{world.team_ids[0]}'
            '/remove_member',
            data={'user_id': str(users[2].id)},
            headers=ENGLISH,
        )

    assert response.status_code == 403
    assert _active_user_ids(world, 0) == {users[1].id, users[2].id}


def test_captain_with_a_removed_row_cannot_kick_members_on_the_site(
    site_app, build_world, users
):
    world = build_world()
    _hollow_out_captain_row(world)
    assert _captain_user_id(world, 0) == users[0].id

    with http_client(site_app, user_id=users[0].id) as client:
        response = client.post(
            f'{SITE_ROOT}/{world.tournament_id}/teams/{world.team_ids[0]}'
            '/remove_member',
            data={'user_id': str(users[2].id)},
            headers=ENGLISH,
        )

    assert response.status_code == 403
    assert _active_user_ids(world, 0) == {users[1].id, users[2].id}


def test_service_refuses_a_removed_ex_captain_as_acting_captain(
    admin, build_world, users
):
    world = build_world()
    participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=admin
    ).unwrap()

    result = teams.remove_team_member(
        world.team_ids[0], users[2].id, acting_captain_id=users[0].id
    )

    assert result.is_err()
    assert result.unwrap_err() == NOT_CAPTAIN_ERROR
    assert _active_user_ids(world, 0) == {users[1].id, users[2].id}


def test_service_refuses_a_captain_with_a_removed_row_as_acting_captain(
    build_world, users
):
    world = build_world()
    _hollow_out_captain_row(world)

    removed = teams.remove_team_member(
        world.team_ids[0], users[2].id, acting_captain_id=users[0].id
    )
    transferred = teams.transfer_captain(
        world.team_ids[0], users[1].id, acting_captain_id=users[0].id
    )

    assert removed.unwrap_err() == NOT_CAPTAIN_ERROR
    assert transferred.unwrap_err() == NOT_CAPTAIN_ERROR
    assert _captain_user_id(world, 0) == users[0].id


def test_demoted_captain_cannot_remove_members_or_transfer_again(
    build_world, users
):
    world = build_world()
    assert teams.transfer_captain(
        world.team_ids[0], users[1].id, acting_captain_id=users[0].id
    ).is_ok()

    removed = teams.remove_team_member(
        world.team_ids[0], users[2].id, acting_captain_id=users[0].id
    )
    transferred = teams.transfer_captain(
        world.team_ids[0], users[0].id, acting_captain_id=users[0].id
    )

    assert removed.unwrap_err() == NOT_CAPTAIN_ERROR
    assert transferred.unwrap_err() == NOT_CAPTAIN_ERROR
    assert _captain_user_id(world, 0) == users[1].id
    assert _active_user_ids(world, 0) == {
        users[0].id,
        users[1].id,
        users[2].id,
    }


def test_current_captain_acts_through_the_service(build_world, users):
    world = build_world()

    removed = teams.remove_team_member(
        world.team_ids[0], users[2].id, acting_captain_id=users[0].id
    )
    transferred = teams.transfer_captain(
        world.team_ids[0], users[1].id, acting_captain_id=users[0].id
    )

    assert removed.is_ok(), removed
    assert transferred.is_ok(), transferred
    assert _captain_user_id(world, 0) == users[1].id


def test_captain_leaving_the_tournament_with_members_is_refused(
    build_world, users
):
    world = build_world(status='REGISTRATION_OPEN')

    result = participants.leave_tournament(
        world.tournament_id, world.participant_ids[0]
    )

    assert result.is_err()
    assert result.unwrap_err() == CAPTAIN_LEAVE_ERROR
    assert not _is_removed(world, 0)
    assert _captain_user_id(world, 0) == users[0].id


def test_member_and_lone_captain_may_still_leave_the_tournament(
    build_world, users
):
    world = build_world(status='REGISTRATION_OPEN')

    for index in (1, 2, 0):
        result = participants.leave_tournament(
            world.tournament_id, world.participant_ids[index]
        )
        assert result.is_ok(), (index, result)

    assert _active_user_ids(world, 0) == set()


# -- the captaincy handover is announced after the commit, like transfer_captain --


def _record_captain_at_send(world):
    """Record the committed captain as another connection sees it at each send."""
    seen = []
    world.sends['captain_transferred'].side_effect = lambda *args, **kwargs: (
        seen.append(_captain_user_id(world, 0))
    )
    return seen


def test_admin_removal_of_the_captain_announces_the_handover_after_commit(
    admin, build_world, users
):
    world = build_world()
    seen = _record_captain_at_send(world)

    participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=admin
    ).unwrap()

    (call,) = world.sends['captain_transferred'].call_args_list
    event = call.kwargs['event']
    assert event.tournament_id == world.tournament_id
    assert event.team_id == world.team_ids[0]
    assert event.old_captain_user_id == users[0].id
    assert event.new_captain_user_id == users[1].id
    assert event.initiator == admin
    assert seen == [users[1].id]


def test_admin_removal_of_a_member_announces_no_handover(admin, build_world):
    world = build_world()

    participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[1], initiator=admin
    ).unwrap()

    world.sends['captain_transferred'].assert_not_called()


def test_a_rolled_back_removal_announces_no_handover(
    admin, build_world, users, monkeypatch
):
    world = build_world()
    monkeypatch.setattr(
        participants,
        '_refresh_roster_matches_flush',
        lambda match_ids, *, occurred_at: Err('injected_refresh_failure'),
    )

    result = participants.admin_remove_participant(
        world.tournament_id, world.participant_ids[0], initiator=admin
    )

    assert result.is_err()
    assert _captain_user_id(world, 0) == users[0].id
    assert not _is_removed(world, 0)
    world.sends['captain_transferred'].assert_not_called()


def test_ticketless_removal_of_the_captain_announces_the_handover(
    build_world, users, monkeypatch
):
    world = build_world()
    seen = _record_captain_at_send(world)
    monkeypatch.setattr(
        participants.ticket_service,
        'select_ticket_users_for_party',
        lambda user_ids, party_id: set(user_ids) - {users[0].id},
    )

    result = participants.remove_participants_without_tickets(
        world.tournament_id, world.party_id
    )

    assert result.unwrap() == 1
    (call,) = world.sends['captain_transferred'].call_args_list
    event = call.kwargs['event']
    assert event.team_id == world.team_ids[0]
    assert event.old_captain_user_id == users[0].id
    assert event.new_captain_user_id == users[1].id
    assert event.initiator is None
    assert seen == [users[1].id]


# -- update_team re-checks the captain after the tournament lock --


def test_update_team_refuses_a_captain_demoted_while_it_waited_for_the_lock(
    build_world, users
):
    world = build_world()
    app = current_app._get_current_object()
    started = Event()
    outcomes = []
    pids = []

    def worker():
        with app.app_context():
            try:
                pids.append(
                    db.session.execute(
                        text('SELECT pg_backend_pid()')
                    ).scalar_one()
                )
                started.set()
                outcomes.append(
                    teams.update_team(
                        world.team_ids[0],
                        name='Renamed by a demoted captain',
                        tag=None,
                        description=None,
                        image_url=None,
                        join_code=None,
                        current_user_id=users[0].id,
                    )
                )
            except BaseException as exc:
                outcomes.append(exc)
            finally:
                db.session.remove()

    with Session(db.engine) as holder:
        holder.execute(
            select(DbTournament)
            .where(DbTournament.id == world.tournament_id)
            .with_for_update()
        )
        holder_pid = holder.execute(
            text('SELECT pg_backend_pid()')
        ).scalar_one()
        thread = Thread(target=worker, daemon=True)
        thread.start()
        assert started.wait(5)
        deadline = monotonic() + 5
        blocked = False
        while monotonic() < deadline:
            with Session(db.engine) as probe:
                blockers = probe.execute(
                    text('SELECT pg_blocking_pids(:pid)'), {'pid': pids[0]}
                ).scalar_one()
            if holder_pid in blockers:
                blocked = True
                break
            sleep(0.02)
        assert blocked, 'the update did not wait for the tournament lock'
        holder.execute(
            DbTournamentTeam.__table__.update()
            .where(DbTournamentTeam.id == world.team_ids[0])
            .values(captain_user_id=users[1].id)
        )
        holder.commit()
        thread.join(10)

    (outcome,) = outcomes
    assert isinstance(outcome, Err), outcome
    assert outcome.unwrap_err() == NOT_CAPTAIN_ERROR
    with Session(db.engine) as session:
        assert (
            session.get(DbTournamentTeam, world.team_ids[0]).name
            == 'Fc3 team 0'
        )


def test_update_team_accepts_the_sitting_captain_and_the_admin_bypass(
    build_world, users
):
    world = build_world()

    own = teams.update_team(
        world.team_ids[0],
        name='Renamed by the captain',
        tag=None,
        description=None,
        image_url=None,
        join_code=None,
        current_user_id=users[0].id,
    )
    bypass = teams.update_team(
        world.team_ids[0],
        name='Renamed by an admin',
        tag=None,
        description=None,
        image_url=None,
        join_code=None,
    )
    refused = teams.update_team(
        world.team_ids[0],
        name='Renamed by a member',
        tag=None,
        description=None,
        image_url=None,
        join_code=None,
        current_user_id=users[1].id,
    )

    assert own.is_ok() and bypass.is_ok()
    assert refused.unwrap_err() == NOT_CAPTAIN_ERROR
