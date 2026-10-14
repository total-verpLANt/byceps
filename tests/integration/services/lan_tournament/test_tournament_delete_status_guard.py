"""
tests.integration.services.lan_tournament.test_tournament_delete_status_guard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Refuses to delete an ongoing, paused or completed tournament, in the
service and through the admin route, not only by hiding the button.
"""

from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-delete-status-guard')

DELETE_BUTTON = 'data-action="delete-tournament"'

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('deleteguardbrand', 'Delete Guard Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Delete Guard')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {
            'admin.access',
            'lan_tournament.administrate',
            'lan_tournament.delete',
            'lan_tournament.view',
        }
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture
def make_tournament(party):
    created = []

    def _make(status):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Delete Guard Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        if status is not TournamentStatus.REGISTRATION_CLOSED:
            tournament_repository.lock_tournament_for_update(tournament.id)
            set_status = tournament_repository.set_tournament_status_flush(
                tournament.id, status
            )
            assert set_status.is_ok(), set_status.unwrap_err()
        db.session.commit()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _exists(tournament):
    db.session.rollback()
    return tournament_repository.find_tournament(tournament.id) is not None


# fmt: off
LOCKED = [
    TournamentStatus.ONGOING,
    TournamentStatus.PAUSED,
    TournamentStatus.COMPLETED,
]
DELETABLE = [
    TournamentStatus.DRAFT,
    TournamentStatus.REGISTRATION_OPEN,
    TournamentStatus.REGISTRATION_CLOSED,
    TournamentStatus.CANCELLED,
]
# fmt: on


@pytest.mark.parametrize('status', LOCKED)
def test_service_refuses_to_delete_a_started_tournament(
    make_tournament, admin, status
):
    tournament = make_tournament(status)

    result = tournament_service.delete_tournament_if_allowed(
        tournament.id, admin.id
    )

    assert result.is_err()
    assert result.unwrap_err() == tournament_service.DELETE_LOCKED_ERROR
    assert _exists(tournament)


@pytest.mark.parametrize('status', DELETABLE)
def test_service_deletes_a_tournament_that_has_not_started(
    make_tournament, admin, status
):
    tournament = make_tournament(status)

    result = tournament_service.delete_tournament_if_allowed(
        tournament.id, admin.id
    )

    assert result.is_ok(), result.unwrap_err()
    assert not _exists(tournament)


@pytest.mark.parametrize('status', LOCKED)
def test_route_refuses_a_direct_delete_post(client, make_tournament, status):
    tournament = make_tournament(status)
    url = f'{BASE_URL}/tournaments/{tournament.id}'

    response = client.post(f'{url}/delete')

    assert response.status_code == 302
    assert response.location.endswith(f'/tournaments/{tournament.id}')
    assert _exists(tournament)
    assert DELETE_BUTTON not in client.get(url).get_data(as_text=True)


def test_route_deletes_a_tournament_that_has_not_started(
    client, make_tournament
):
    tournament = make_tournament(TournamentStatus.REGISTRATION_CLOSED)
    url = f'{BASE_URL}/tournaments/{tournament.id}'

    assert DELETE_BUTTON in client.get(url).get_data(as_text=True)

    response = client.post(f'{url}/delete')

    assert response.status_code == 302
    assert not _exists(tournament)
