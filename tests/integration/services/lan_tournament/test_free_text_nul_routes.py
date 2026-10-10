"""
tests.integration.services.lan_tournament.test_free_text_nul_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

PostgreSQL rejects NUL in text columns. A participant who posts one in a
free-text field must get the route's ordinary invalid-form response, not
a 500, and nothing may be written.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_repository,
    tournament_score_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'
ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'

NUL_VALUE = 'A\x00B'

_counter = count(1)


@pytest.fixture(scope='module')
def player(make_user):
    user = make_user(f'NulP{str(generate_uuid7())[:12]}')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user(f'NulG{str(generate_uuid7())[:12]}')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.update', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def opponent(make_user):
    user = make_user(f'NulO{str(generate_uuid7())[:12]}')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_tournament(party):
    created = []

    def _make(**kwargs):
        tournament, _ = tournament_service.create_tournament(
            party.id,
            f'Free Text NUL Tournament {next(_counter)}',
            **kwargs,
        ).unwrap()
        created.append(tournament)
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _join(tournament, user):
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


def _post(site_app, user, url, **data):
    with http_client(site_app, user_id=user.id) as client:
        return client.post(url, data=data)


def _team_tournament(make_tournament, player):
    tournament = make_tournament(
        contestant_type=ContestantType.TEAM,
        max_teams=8,
        min_players_in_team=1,
        max_players_in_team=5,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    _join(tournament, player)
    return tournament


def _teams(tournament):
    db.session.rollback()
    return tournament_team_service.get_teams_for_tournament(tournament.id)


_TEAM_FIELDS = ['name', 'tag', 'description', 'join_code']


@pytest.mark.parametrize('field_name', _TEAM_FIELDS)
def test_team_create_with_nul_is_an_invalid_form(
    site_app, player, make_tournament, field_name
):
    tournament = _team_tournament(make_tournament, player)
    url = f'{BASE_URL}/{tournament.id}/teams/create'
    data = {
        'name': 'Valid Team',
        'tag': 'VT',
        'description': 'A description',
        'join_code': 'secret',
    }
    control = _post(site_app, player, url, **{**data, 'name': ''})

    response = _post(site_app, player, url, **{**data, field_name: NUL_VALUE})

    assert response.status_code == control.status_code
    assert _teams(tournament) == []


def test_team_create_without_nul_creates_the_team(
    site_app, player, make_tournament
):
    tournament = _team_tournament(make_tournament, player)

    response = _post(
        site_app,
        player,
        f'{BASE_URL}/{tournament.id}/teams/create',
        name='Valid Team',
        tag='VT',
    )

    assert response.status_code == 302
    assert [team.name for team in _teams(tournament)] == ['Valid Team']


@pytest.fixture
def team(make_tournament, player):
    tournament = _team_tournament(make_tournament, player)
    team, _ = tournament_team_service.create_team(
        tournament.id,
        'Original Name',
        player.id,
        tag='ORG',
        description='Original description',
        join_code='original',
    ).unwrap()
    return team


def _team_state(team):
    db.session.rollback()
    current = tournament_team_service.get_team(team.id)
    return (
        current.name,
        current.tag,
        current.description,
        current.join_code,
    )


@pytest.mark.parametrize('field_name', _TEAM_FIELDS)
def test_team_update_with_nul_is_an_invalid_form(
    site_app, player, team, field_name
):
    url = f'{BASE_URL}/{team.tournament_id}/teams/{team.id}/update'
    data = {
        'name': 'Changed Name',
        'tag': 'CHG',
        'description': 'Changed description',
        'join_code': 'changed',
    }
    before = _team_state(team)
    control = _post(site_app, player, url, **{**data, 'name': ''})

    response = _post(site_app, player, url, **{**data, field_name: NUL_VALUE})

    assert response.status_code == control.status_code
    assert _team_state(team) == before


def test_team_update_without_nul_changes_the_team(site_app, player, team):
    response = _post(
        site_app,
        player,
        f'{BASE_URL}/{team.tournament_id}/teams/{team.id}/update',
        name='Changed Name',
        tag='CHG',
    )

    assert response.status_code == 302
    assert _team_state(team)[0] == 'Changed Name'


@pytest.fixture
def ongoing_match(make_tournament, player, opponent):
    tournament = make_tournament(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    _join(tournament, player)
    _join(tournament, opponent)
    tournament_match_service.generate_single_elimination_bracket(
        tournament.id, initiator_id=player.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).unwrap()
    db.session.rollback()
    return tournament_match_service.get_matches_for_tournament(tournament.id)[0]


@pytest.fixture
def paused_match(ongoing_match):
    tournament_service.change_status(
        ongoing_match.tournament_id, TournamentStatus.PAUSED
    ).unwrap()
    db.session.rollback()
    return ongoing_match


def _comments(match):
    db.session.rollback()
    return tournament_match_service.get_comments_from_match(match.id)


def test_match_comment_with_nul_is_an_invalid_form(
    site_app, player, ongoing_match
):
    url = f'{BASE_URL}/matches/{ongoing_match.id}/add_comment'
    control = _post(site_app, player, url, comment='')

    response = _post(site_app, player, url, comment=NUL_VALUE)

    assert response.status_code == control.status_code
    assert response.headers['Location'] == control.headers['Location']
    assert _comments(ongoing_match) == []


def test_match_comment_without_nul_is_stored(site_app, player, ongoing_match):
    response = _post(
        site_app,
        player,
        f'{BASE_URL}/matches/{ongoing_match.id}/add_comment',
        comment='Good luck',
    )

    assert response.status_code == 302
    assert [c.comment for c in _comments(ongoing_match)] == ['Good luck']


def _post_flashed(app, user, url, **data):
    with http_client(app, user_id=user.id) as client:
        response = client.post(url, data=data)
        with client.session_transaction() as session:
            flashes = session.get('_flashes', [])
    return response, [flash['category'] for _, flash in flashes]


def test_orga_comment_with_nul_is_refused_with_a_flash(
    site_app, orga, paused_match
):
    tournament_orga_service.assign_orga(
        paused_match.tournament_id, orga.id, orga.id
    ).unwrap()
    url = f'{BASE_URL}/matches/{paused_match.id}/add_comment'
    control, control_flashes = _post_flashed(site_app, orga, url, comment='')

    response, flashes = _post_flashed(site_app, orga, url, comment=NUL_VALUE)

    assert response.status_code == 302
    assert response.headers['Location'] == control.headers['Location']
    assert flashes == control_flashes == ['danger']
    assert _comments(paused_match) == []


def test_orga_comment_without_nul_is_stored(site_app, orga, paused_match):
    tournament_orga_service.assign_orga(
        paused_match.tournament_id, orga.id, orga.id
    ).unwrap()

    response, flashes = _post_flashed(
        site_app,
        orga,
        f'{BASE_URL}/matches/{paused_match.id}/add_comment',
        comment='Orga note',
    )

    assert response.status_code == 302
    assert flashes == ['success']
    assert [c.comment for c in _comments(paused_match)] == ['Orga note']


def test_admin_comment_with_nul_is_refused_with_a_flash(
    admin_app, admin, ongoing_match
):
    url = f'{ADMIN_URL}/matches/{ongoing_match.id}/add_comment'
    control, control_flashes = _post_flashed(admin_app, admin, url, comment='')

    response, flashes = _post_flashed(admin_app, admin, url, comment=NUL_VALUE)

    assert response.status_code == 302
    assert response.headers['Location'] == control.headers['Location']
    assert flashes == control_flashes == ['danger']
    assert _comments(ongoing_match) == []


def test_admin_comment_without_nul_is_stored(admin_app, admin, ongoing_match):
    response, flashes = _post_flashed(
        admin_app,
        admin,
        f'{ADMIN_URL}/matches/{ongoing_match.id}/add_comment',
        comment='Admin note',
    )

    assert response.status_code == 302
    assert flashes == ['success']
    assert [c.comment for c in _comments(ongoing_match)] == ['Admin note']


@pytest.fixture
def highscore(make_tournament, player):
    tournament = make_tournament(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    _join(tournament, player)
    return tournament


def _submissions(tournament):
    db.session.rollback()
    return tournament_repository.get_official_submissions_for_tournament(
        tournament.id
    )


def test_highscore_note_with_nul_is_an_invalid_form(
    site_app, player, highscore
):
    url = f'{BASE_URL}/{highscore.id}/highscore/submit'
    control = _post(site_app, player, url, score='abc', note='fine')

    response = _post(site_app, player, url, score='100', note=NUL_VALUE)

    assert response.status_code == control.status_code
    assert _submissions(highscore) == []
    assert tournament_score_service.get_leaderboard(highscore.id).unwrap() == []


def test_highscore_without_nul_is_stored(site_app, player, highscore):
    response = _post(
        site_app,
        player,
        f'{BASE_URL}/{highscore.id}/highscore/submit',
        score='100',
        note='personal best',
    )

    assert response.status_code == 302
    assert [(s.score, s.note) for s in _submissions(highscore)] == [
        (100, 'personal best')
    ]
