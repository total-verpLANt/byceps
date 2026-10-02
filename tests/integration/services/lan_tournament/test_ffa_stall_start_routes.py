"""
tests.integration.services.lan_tournament.test_ffa_stall_start_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The start of a plain FFA tournament is refused when the cut or the minimum
lobby size changed after the generation and the generated first round now
runs into a dead end.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
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
from byceps.util.uuid import generate_uuid7

from tests.helpers import log_in_user


ADMIN_URL = 'http://admin.acmecon.test/lan-tournaments'
SITE_URL = 'http://www.acmecon.test/lan-tournaments'

_counter = count(1)


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaStallStartPlayer{i}') for i in range(9)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('FfaStallStartOrga')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_ffa(party, players, orga):
    """Return a generated plain FFA tournament at REGISTRATION_CLOSED."""
    created = []

    def _make(*, size=8, minimum=4, maximum=4, cut=2):
        tournament, _ = tournament_service.create_tournament(
            party.id,
            f'FFA Stall Start {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            max_players=16,
            point_table=[5, 3, 2, 1],
            group_size_min=minimum,
            group_size_max=maximum,
            advancement_count=cut,
        ).unwrap()
        created.append(tournament)
        for user in players[:size]:
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
        tournament_orga_service.assign_orga(
            tournament.id, orga.id, orga.id
        ).unwrap()
        board = tournament_seeding_service.get_board(tournament.id).unwrap()
        generated = tournament_seeding_service.generate_from_seeding(
            tournament.id,
            expected_version=board.version,
            initiator_id=orga.id,
        )
        assert generated.is_ok(), generated.unwrap_err()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _reconfigure(tournament, **changes):
    current = tournament_repository.get_tournament(tournament.id)
    settings = {
        'advancement_count': current.advancement_count,
        'group_size_min': current.group_size_min,
        'group_size_max': current.group_size_max,
    } | changes
    result = tournament_service.update_tournament(
        tournament.id,
        name=current.name,
        max_players=current.max_players,
        contestant_type=current.contestant_type,
        game_format=current.game_format,
        elimination_mode=current.elimination_mode,
        point_table=current.point_table,
        **settings,
    )
    assert result.is_ok(), result.unwrap_err()


def _status(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(tournament.id).tournament_status


def _start(tournament, initiator):
    return tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, initiator.id
    )


def _flashes(client):
    with client.session_transaction() as session:
        flashes = session.pop('_flashes', None) or []
    return ' | '.join(str(message) for _, message in flashes)


def test_a_cut_raised_after_generation_blocks_the_start(make_ffa, orga):
    # 8 -> [4, 4] -> 4 -> final; a cut of 3 gives 8 -> 6 -> [3, 3].
    tournament = make_ffa()
    _reconfigure(tournament, advancement_count=3)

    result = _start(tournament, orga)

    assert result.is_err()
    assert result.unwrap_err() == tournament_match_service.FFA_STALLS_ERROR
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED


def test_restoring_the_cut_allows_the_start(make_ffa, orga):
    tournament = make_ffa()
    _reconfigure(tournament, advancement_count=3)
    assert _start(tournament, orga).is_err()

    _reconfigure(tournament, advancement_count=2)
    result = _start(tournament, orga)

    assert result.is_ok(), result.unwrap_err()
    assert _status(tournament) is TournamentStatus.ONGOING


def test_a_raised_minimum_blocks_the_start(make_ffa, orga):
    # 9 -> [3, 3, 3] -> 6 -> [3, 3] -> 4: fine with a minimum of 3,
    # but round two falls below a minimum of 4.
    tournament = make_ffa(size=9, minimum=3, maximum=4, cut=2)
    _reconfigure(tournament, group_size_min=4)

    result = _start(tournament, orga)

    assert result.is_err()
    assert result.unwrap_err() == tournament_match_service.FFA_STALLS_ERROR
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED


def test_admin_start_route_refuses_a_stalling_setup(
    admin_app, make_client, admin, make_ffa
):
    tournament = make_ffa()
    _reconfigure(tournament, advancement_count=3)
    client = make_client(admin_app, user_id=admin.id)

    response = client.post(f'{ADMIN_URL}/tournaments/{tournament.id}/start')

    assert response.status_code == 302
    assert tournament_match_service.FFA_STALLS_ERROR in _flashes(client)
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED


def test_site_orga_start_route_refuses_a_stalling_setup(
    site_app, make_client, orga, make_ffa
):
    tournament = make_ffa()
    _reconfigure(tournament, advancement_count=3)
    client = make_client(site_app, user_id=orga.id)

    response = client.post(f'{SITE_URL}/orga/tournaments/{tournament.id}/start')

    assert response.status_code == 302
    assert tournament_match_service.FFA_STALLS_ERROR in _flashes(client)
    assert _status(tournament) is TournamentStatus.REGISTRATION_CLOSED
