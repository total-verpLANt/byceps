from datetime import datetime, UTC
from unittest.mock import Mock

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_participant_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
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
from byceps.util.result import Ok
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user
from tests.integration.services.lan_tournament.test_ffa_waiting_winner import (
    lobbies,
    members,
    play,
)


@pytest.fixture(scope='module')
def other_party(make_party, make_brand):
    brand = make_brand('closeoutroutesother', 'Closeout Routes Other')
    return make_party(brand, 'closeout-routes-other', 'Closeout Routes Other')


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('CloseoutRouteOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.administrate'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def bystander(make_user):
    user = make_user('CloseoutRouteBystander')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'CloseoutRoutePlayer{i}') for i in range(8)]


@pytest.fixture
def make_tournament(orga):
    def make(party_id):
        result = tournament_service.create_tournament(
            party_id,
            f'Closeout routes {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            group_size_min=2,
            group_size_max=4,
            advancement_count=1,
            point_table=[3, 2, 1, 0],
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        assigned = tournament_orga_service.assign_orga(
            tournament.id, orga.id, orga.id
        )
        assert assigned.is_ok(), assigned.unwrap_err()
        return tournament

    return make


@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize('operation', ['prepare', 'generate'])
def test_decorated_closeout_redirects(
    admin_app,
    site_app,
    party,
    admin,
    orga,
    make_tournament,
    monkeypatch,
    surface,
    operation,
):
    tournament = make_tournament(party.id)
    service = Mock(return_value=Ok('completed'))
    monkeypatch.setattr(
        tournament_seeding_service,
        'prepare_ffa_round_draft'
        if operation == 'prepare'
        else 'generate_from_seeding',
        service,
    )
    app, user = (admin_app, admin) if surface == 'admin' else (site_app, orga)
    prefix = (
        'http://admin.acmecon.test/lan-tournaments/tournaments'
        if surface == 'admin'
        else 'http://www.acmecon.test/lan-tournaments/orga/tournaments'
    )
    suffix = (
        'advance_ffa_round' if operation == 'prepare' else 'seeding/generate'
    )
    with http_client(app, user_id=user.id) as client:
        response = client.post(
            f'{prefix}/{tournament.id}/{suffix}',
            data={'target': 'ffa:SE:2', 'version': '3'},
        )
    assert response.status_code == 302
    assert response.location.endswith(f'/{tournament.id}/bracket')
    service.assert_called_once()


@pytest.mark.parametrize('operation', ['advance_ffa_round', 'seeding/generate'])
@pytest.mark.parametrize('denial', ['unassigned', 'other_party'])
def test_site_closeout_enforces_scope(
    site_app,
    party,
    other_party,
    orga,
    bystander,
    make_tournament,
    monkeypatch,
    operation,
    denial,
):
    tournament = make_tournament(
        other_party.id if denial == 'other_party' else party.id
    )
    user = orga if denial == 'other_party' else bystander
    prepare, generate = Mock(), Mock()
    monkeypatch.setattr(
        tournament_seeding_service, 'prepare_ffa_round_draft', prepare
    )
    monkeypatch.setattr(
        tournament_seeding_service, 'generate_from_seeding', generate
    )
    with http_client(site_app, user_id=user.id) as client:
        response = client.post(
            f'http://www.acmecon.test/lan-tournaments/orga/tournaments/{tournament.id}/{operation}',
            data={'target': 'ffa:SE:2', 'version': '3'},
        )
    assert response.status_code in {403, 404}
    prepare.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize('surface', ['admin', 'site'])
@pytest.mark.parametrize('operation', ['prepare', 'generate'])
def test_real_single_survivor_request_completes_and_redirects(
    admin_app,
    site_app,
    party,
    admin,
    orga,
    players,
    make_tournament,
    surface,
    operation,
):
    tournament = make_tournament(party.id)
    for user in players:
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
    generated = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()
    started = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )
    assert started.is_ok(), started.unwrap_err()
    first = lobbies(tournament, None, 0)
    for lobby in first:
        play(lobby, admin)
    winner = members(first[0])[0]
    data = {}
    if operation == 'generate':
        target = tournament_seeding_service.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        board = tournament_seeding_service.get_board(
            tournament.id, target
        ).unwrap()
        data = {'target': target, 'version': str(board.version)}
    for cid in members(first[1]):
        removed = tournament_participant_service.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=admin
        )
        assert removed.is_ok(), removed.unwrap_err()
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )
    app, user = (admin_app, admin) if surface == 'admin' else (site_app, orga)
    prefix = (
        'http://admin.acmecon.test/lan-tournaments/tournaments'
        if surface == 'admin'
        else 'http://www.acmecon.test/lan-tournaments/orga/tournaments'
    )
    suffix = (
        'advance_ffa_round' if operation == 'prepare' else 'seeding/generate'
    )
    with http_client(app, user_id=user.id) as client:
        response = client.post(f'{prefix}/{tournament.id}/{suffix}', data=data)
    assert response.status_code == 302
    assert response.location.endswith(f'/{tournament.id}/bracket')
    persisted = tournament_repository.get_tournament(tournament.id)
    assert persisted.tournament_status is TournamentStatus.COMPLETED
    assert str(persisted.winner_participant_id) == winner
    assert len(
        tournament_repository.get_matches_for_tournament(tournament.id)
    ) == len(first)


@pytest.mark.parametrize('operation', ['advance_ffa_round', 'seeding/generate'])
def test_admin_closeout_requires_administrate(
    admin_app,
    make_admin,
    party,
    make_tournament,
    monkeypatch,
    operation,
):
    viewer = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(viewer.id)
    tournament = make_tournament(party.id)
    prepare, generate = Mock(), Mock()
    monkeypatch.setattr(
        tournament_seeding_service, 'prepare_ffa_round_draft', prepare
    )
    monkeypatch.setattr(
        tournament_seeding_service, 'generate_from_seeding', generate
    )
    with http_client(admin_app, user_id=viewer.id) as client:
        response = client.post(
            f'http://admin.acmecon.test/lan-tournaments/tournaments/{tournament.id}/{operation}',
            data={'target': 'ffa:SE:2', 'version': '3'},
        )
    assert response.status_code == 403
    prepare.assert_not_called()
    generate.assert_not_called()
