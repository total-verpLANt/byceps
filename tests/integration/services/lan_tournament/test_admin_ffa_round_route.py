"""
tests.integration.services.lan_tournament.test_admin_ffa_round_route
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The legacy `generate_ffa_round` route never builds a round: once round 0
exists it still sends the orga to the seeding board.
"""

from datetime import datetime, UTC

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
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


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'AdminFfaRoundRoutePlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture
def ffa_with_round_zero(party, players, admin):
    result = tournament_service.create_tournament(
        party.id,
        'Admin FFA Round Route',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        max_players=16,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[3, 2, 1, 0],
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
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
    yield tournament
    db.session.rollback()
    if tournament_repository.find_tournament(tournament.id) is not None:
        tournament_service.delete_tournament(tournament.id)


def test_post_with_round_zero_redirects_and_builds_nothing(
    admin_app, admin, ffa_with_round_zero
):
    tournament = ffa_with_round_zero
    before = len(
        tournament_match_service.get_matches_for_tournament(tournament.id)
    )
    assert before > 0

    with http_client(admin_app, user_id=admin.id) as client:
        response = client.post(
            f'{ADMIN_URL}/tournaments/{tournament.id}/generate_ffa_round'
        )

    assert response.status_code == 302
    assert response.location.endswith(
        f'/lan-tournaments/tournaments/{tournament.id}/seeding'
    )
    db.session.expire_all()
    after = tournament_match_service.get_matches_for_tournament(tournament.id)
    assert len(after) == before
    assert {m.round for m in after} == {0}
