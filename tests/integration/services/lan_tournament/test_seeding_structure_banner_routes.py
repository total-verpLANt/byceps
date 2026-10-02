"""
tests.integration.services.lan_tournament.test_seeding_structure_banner_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A structure change after the first Setzliste open shows the structure banner
on the real admin and site Setzliste, not the roster sentence.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_orga_service,
    tournament_repository,
    tournament_seeding_service as svc,
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
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'
SITE_URL = 'http://www.acmecon.test/lan-tournaments'

STRUCTURE_BANNER = (
    'The tournament structure changed since the seeding was drawn'
)
ROSTER_SENTENCE = 'The roster changed since the seed code was made'

_counter = count(1)


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'StructureBannerPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('StructureBannerOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.administrate'})
    log_in_user(user.id)
    return user


@pytest.fixture
def tournament(party, players, orga):
    result = tournament_service.create_tournament(
        party.id,
        f'Structure Banner Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    assert result.is_ok(), result.unwrap_err()
    created, _ = result.unwrap()
    for user in players:
        tournament_repository.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=created.id,
                substitute_player=False,
                team_id=None,
                created_at=datetime.now(UTC),
            )
        )
    db.session.commit()
    tournament_orga_service.assign_orga(created.id, orga.id, orga.id).unwrap()
    yield created
    db.session.rollback()
    if tournament_repository.find_tournament(created.id) is not None:
        tournament_service.delete_tournament(created.id)


def _change_structure(tournament):
    current = tournament_repository.get_tournament(tournament.id)
    result = tournament_service.update_tournament(
        tournament.id,
        name=current.name,
        contestant_type=current.contestant_type,
        game_format=current.game_format,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        max_players=current.max_players,
    )
    assert result.is_ok(), result.unwrap_err()
    db.session.commit()


def _admin_page(admin_app, admin, tournament):
    with http_client(admin_app, user_id=admin.id) as client:
        return client.get(
            f'{ADMIN_URL}/tournaments/{tournament.id}/seeding'
        ).get_data(as_text=True)


def _site_page(site_app, orga, tournament):
    with http_client(site_app, user_id=orga.id) as client:
        return client.get(
            f'{SITE_URL}/orga/tournaments/{tournament.id}/seeding'
        ).get_data(as_text=True)


def test_admin_setzliste_shows_the_structure_banner(
    admin_app, admin, tournament
):
    svc.get_board(tournament.id).unwrap()
    assert STRUCTURE_BANNER not in _admin_page(admin_app, admin, tournament)

    _change_structure(tournament)

    html = _admin_page(admin_app, admin, tournament)
    assert STRUCTURE_BANNER in html
    assert ROSTER_SENTENCE not in html


def test_site_setzliste_shows_the_structure_banner(site_app, orga, tournament):
    svc.get_board(tournament.id).unwrap()
    assert STRUCTURE_BANNER not in _site_page(site_app, orga, tournament)

    _change_structure(tournament)

    html = _site_page(site_app, orga, tournament)
    assert STRUCTURE_BANNER in html
    assert ROSTER_SENTENCE not in html
