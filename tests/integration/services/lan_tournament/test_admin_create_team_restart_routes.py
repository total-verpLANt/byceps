"""
tests.integration.services.lan_tournament.test_admin_create_team_restart_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The admin create-team form restarts a running team tournament.
"""

from datetime import datetime, UTC
from itertools import count

from babel import Locale
import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service as participant_service,
    tournament_repository,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
CLOSED = TournamentStatus.REGISTRATION_CLOSED

REASON = 'A late team joins'

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand,
        PartyID(f'create-team-restart-routes-{generate_uuid7()}'),
        'Create Team Restart Routes',
    )


@pytest.fixture(scope='module')
def players(make_user):
    suffix = generate_uuid7().hex[-10:]
    return [make_user(f'RoutesP{i}{suffix}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {
            'admin.access',
            'lan_tournament.administrate',
            'lan_tournament.create',
            'lan_tournament.view',
        },
        screen_name=f'RoutesAdmin{generate_uuid7().hex[-10:]}',
        locale=Locale('en'),
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def create_only_admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.create', 'lan_tournament.view'},
        screen_name=f'RoutesCreate{generate_uuid7().hex[-10:]}',
        locale=Locale('en'),
    )
    log_in_user(user.id)
    return user


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def build(party, players, admin):
    """Build a team tournament that is generated and then started.

    The first four players each captain a team. The others are not
    registered.
    """

    def _build(status=PAUSED):
        result = tournament_service.create_tournament(
            party.id,
            f'Create team restart routes {next(_counter)} {generate_uuid7()}',
            contestant_type=ContestantType.TEAM,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()

        now = datetime.now(UTC).replace(tzinfo=None)
        for player in players[:4]:
            team_id = TournamentTeamID(generate_uuid7())
            tournament_repository.create_team(
                TournamentTeam(
                    id=team_id,
                    tournament_id=tournament.id,
                    name=f'Existing team {generate_uuid7()}',
                    tag=None,
                    description=None,
                    image_url=None,
                    captain_user_id=player.id,
                    join_code=None,
                    created_at=now,
                )
            )
            db.session.flush()
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=player.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=team_id,
                    created_at=now,
                )
            )
        db.session.commit()

        board = seeding.get_board(tournament.id, initiator_id=admin.id)
        generated = seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.unwrap().version,
            initiator_id=admin.id,
        )
        assert generated.is_ok(), generated.unwrap_err()

        _change_status(tournament, ONGOING, admin)
        if status is PAUSED:
            _change_status(tournament, PAUSED, admin)
        return tournament

    return _build


def _change_status(tournament, status, admin):
    changed = tournament_service.change_status(tournament.id, status, admin.id)
    assert changed.is_ok(), changed.unwrap_err()


def _state(tournament):
    """Read what a restart touches, from the database."""
    db.session.rollback()
    db.session.expire_all()
    return {
        'status': tournament_repository.get_tournament(
            tournament.id, fresh=True
        ).tournament_status,
        'matches': [
            m.id
            for m in tournament_match_service.get_matches_for_tournament_ordered(
                tournament.id
            )
        ],
        'teams': sorted(
            t.name
            for t in tournament_repository.get_teams_for_tournament(
                tournament.id, include_removed=True
            )
        ),
        'participants': sorted(
            str(p.user_id)
            for p in tournament_repository.get_participants_for_tournament(
                tournament.id, include_removed=True
            )
        ),
    }


def _form_data(captain, **overrides):
    data = {
        'captain': captain.screen_name,
        'name': f'Late team {generate_uuid7()}',
        'reason': REASON,
        'confirm_restart': 'y',
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _post(admin_app, user, tournament, data):
    with http_client(admin_app, user_id=user.id) as client:
        response = client.post(
            f'{ADMIN_URL}/tournaments/{tournament.id}/teams', data=data
        )
        with client.session_transaction() as session:
            flashes = list(session.get('_flashes', []))
    return response, flashes


def _assert_restarted(response, flashes, tournament, data, before, admin):
    assert response.status_code == 302
    assert response.headers['Location'].endswith(
        f'/lan-tournaments/tournaments/{tournament.id}/seeding'
    )
    state = _state(tournament)
    assert state['status'] is CLOSED
    assert state['matches'] == []
    assert data['name'] in state['teams']
    assert len(state['teams']) == len(before['teams']) + 1
    assert len(state['participants']) == len(before['participants']) + 1
    assert [(f['category'], f['text']) for _, f in flashes] == [
        (
            'success',
            (
                f'Tournament restarted. Team "{data["name"]}" has been'
                ' created. Re-seed and start the tournament again.'
            ),
        )
    ]
    entries = [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'tournament-restarted'
    ]
    assert len(entries) == 1
    assert entries[0].initiator_id == admin.id
    assert entries[0].data['reason'] == REASON


def test_admin_create_team_restarts_a_paused_tournament(
    admin_app, build, players, admin
):
    tournament = build(PAUSED)
    before = _state(tournament)
    assert before['status'] is PAUSED
    assert before['matches']
    data = _form_data(players[4])

    response, flashes = _post(admin_app, admin, tournament, data)

    _assert_restarted(response, flashes, tournament, data, before, admin)


def test_admin_create_team_restarts_an_ongoing_tournament(
    admin_app, build, players, admin
):
    tournament = build(ONGOING)
    before = _state(tournament)
    assert before['status'] is ONGOING
    assert before['matches']
    data = _form_data(players[4])

    response, flashes = _post(admin_app, admin, tournament, data)

    _assert_restarted(response, flashes, tournament, data, before, admin)


def test_admin_create_team_form_shows_the_restart_warning(
    admin_app, build, admin
):
    tournament = build(PAUSED)
    match_count = len(_state(tournament)['matches'])

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.get(
            f'{ADMIN_URL}/tournaments/{tournament.id}/teams/create'
        )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'notification color-danger' in body
    assert 'Creating this team restarts the tournament.' in body
    assert f'{match_count} matches will be deleted.' in body
    assert '0 confirmed results will be lost.' in body
    assert 'The playoffs have been released.' not in body
    assert 'name="confirm_restart"' in body
    assert 'name="reason"' in body
    assert 'lt-correct__consent' in body
    assert 'data-confirm="' in body
    assert 'onsubmit="return confirm(this.dataset.confirm)"' in body
    assert 'Restart tournament and create team' in body
    assert 'lan_tournament_match_admin.css' in body


def test_admin_create_team_refuses_without_confirmation_and_keeps_the_bracket(
    admin_app, build, players, admin
):
    tournament = build(PAUSED)
    before = _state(tournament)
    data = _form_data(players[4], confirm_restart=None)

    response, flashes = _post(admin_app, admin, tournament, data)

    assert response.status_code == 200
    assert _state(tournament) == before
    body = response.get_data(as_text=True)
    assert 'name="confirm_restart"' in body
    assert 'Team creation failed' not in body
    assert body.count('class="form-errors"') == 1


@pytest.mark.parametrize(
    ('reason', 'refused_by_form'),
    [(None, True), ('   ', False)],
    ids=['missing', 'blank'],
)
def test_admin_create_team_refuses_without_reason_and_keeps_the_bracket(
    admin_app, build, players, admin, reason, refused_by_form
):
    tournament = build(PAUSED)
    before = _state(tournament)
    data = _form_data(players[4], reason=reason)

    response, flashes = _post(admin_app, admin, tournament, data)

    assert response.status_code == 200
    assert _state(tournament) == before
    body = response.get_data(as_text=True)
    assert 'name="reason"' in body
    assert ('Team creation failed' not in body) is refused_by_form
    assert 'Please give a reason for the restart.' in body or refused_by_form


def test_admin_create_team_flashes_a_refusal_of_the_service_and_keeps_the_bracket(
    admin_app, build, players, admin
):
    tournament = build(PAUSED)
    before = _state(tournament)
    taken = before['teams'][0]
    data = _form_data(players[4], name=taken)

    response, flashes = _post(admin_app, admin, tournament, data)

    assert response.status_code == 200
    assert _state(tournament) == before
    body = response.get_data(as_text=True)
    assert 'Team creation failed' in body
    assert 'A team with this name already exists in this tournament.' in body


def test_admin_create_team_refuses_without_administrate_permission_and_keeps_the_bracket(
    admin_app, build, players, create_only_admin
):
    tournament = build(PAUSED)
    before = _state(tournament)
    teams_url = f'{ADMIN_URL}/tournaments/{tournament.id}/teams'

    with http_client(admin_app, user_id=create_only_admin.id) as client:
        form_response = client.get(f'{teams_url}/create')
        post_response = client.post(teams_url, data=_form_data(players[4]))

    assert form_response.status_code == 403
    assert post_response.status_code == 403
    assert _state(tournament) == before


def test_admin_create_team_in_registration_is_unchanged(
    admin_app, party, players, admin
):
    result = tournament_service.create_tournament(
        party.id,
        f'Create team routes registration {generate_uuid7()}',
        contestant_type=ContestantType.TEAM,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
        max_players=8,
        max_players_in_team=2,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    added = participant_service.admin_add_participant(
        tournament.id, players[5].id
    )
    assert added.is_ok(), added.unwrap_err()
    teams_url = f'{ADMIN_URL}/tournaments/{tournament.id}/teams'
    name = f'Registration team {generate_uuid7()}'

    with http_client(admin_app, user_id=admin.id) as client:
        form_response = client.get(f'{teams_url}/create')
        post_response = client.post(
            teams_url, data={'captain': players[5].screen_name, 'name': name}
        )

    assert form_response.status_code == 200
    body = form_response.get_data(as_text=True)
    assert 'notification color-danger' not in body
    assert 'name="confirm_restart"' not in body
    assert 'name="reason"' not in body
    assert 'data-confirm' not in body
    assert 'Restart tournament and create team' not in body
    assert post_response.status_code == 302
    assert post_response.headers['Location'].endswith(
        f'/lan-tournaments/tournaments/{tournament.id}/teams'
    )
    db.session.expire_all()
    teams = tournament_repository.get_teams_for_tournament(tournament.id)
    assert [t.name for t in teams] == [name]
    assert (
        tournament_repository.get_tournament(
            tournament.id, fresh=True
        ).tournament_status
        is TournamentStatus.REGISTRATION_OPEN
    )
    assert not [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'tournament-restarted'
    ]
