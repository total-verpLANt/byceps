from datetime import datetime, UTC
import re

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


SERVER_NAME = 'admin-for-lt-metrics.acmecon.test'
URL = f'http://{SERVER_NAME}/lan-tournaments/metrics/'

PARTY_ID = PartyID('lt-metrics-party')

LABEL_NAMES = re.compile(r'[{,] ?(\w+)="')


# To be overridden by test parametrization
@pytest.fixture()
def metrics_enabled():
    return False


@pytest.fixture()
def client(metrics_enabled, database, make_admin_app):
    app = make_admin_app(SERVER_NAME, metrics_enabled=metrics_enabled)
    with app.app_context():
        yield app.test_client()


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ltmetricsbrand', 'LT Metrics Brand')
    return make_party(brand, PARTY_ID, 'LT Metrics Party')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'LtMetricsUser{i}') for i in range(2)]


@pytest.fixture(scope='module')
def seeded_tournament(party, users):
    """Seed one open tournament: 1 + 1 removed participant and team, 2 matches."""
    result = tournament_service.create_tournament(
        party.id,
        'LT Metrics Tournament',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    now = datetime.now(UTC)

    participants = [
        TournamentParticipant(
            id=TournamentParticipantID(generate_uuid7()),
            user_id=user.id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=None,
            created_at=now,
        )
        for user in users
    ]
    for participant in participants:
        tournament_repository.create_participant(participant)

    teams = [
        TournamentTeam(
            id=TournamentTeamID(generate_uuid7()),
            tournament_id=tournament.id,
            name=name,
            tag=None,
            description=None,
            image_url=None,
            captain_user_id=users[0].id,
            join_code=None,
            created_at=now,
        )
        for name in ('LT Metrics Team A', 'LT Metrics Team B')
    ]
    for team in teams:
        tournament_repository.create_team(team)

    for confirmed_by in (users[0].id, None):
        tournament_repository.create_match(
            TournamentMatch(
                id=TournamentMatchID(generate_uuid7()),
                tournament_id=tournament.id,
                group_order=None,
                match_order=1,
                round=1,
                next_match_id=None,
                confirmed_by=confirmed_by,
                created_at=now,
            )
        )

    # The second participant and team left again: they must not be counted.
    db.session.get(DbTournamentParticipant, participants[1].id).removed_at = now
    db.session.get(DbTournamentTeam, teams[1].id).removed_at = now
    db.session.commit()

    return tournament


@pytest.mark.parametrize('metrics_enabled', [False])
def test_metrics_endpoint_disabled_returns_404(client):
    response = client.get(URL)

    assert response.status_code == 404


@pytest.mark.parametrize('metrics_enabled', [True])
def test_metrics_endpoint_returns_prometheus_text(client):
    response = client.get(URL)

    assert response.status_code == 200
    assert response.content_type == 'text/plain; version=0.0.4; charset=utf-8'


@pytest.mark.parametrize('metrics_enabled', [True])
def test_metrics_endpoint_counts_seeded_tournament(client, seeded_tournament):
    response = client.get(URL)

    assert response.status_code == 200

    lines = response.get_data(as_text=True).splitlines()
    party = f'party="{PARTY_ID}"'
    assert (
        f'lan_tournament_tournaments_count{{{party}, '
        'status="REGISTRATION_OPEN"} 1'
    ) in lines
    assert (
        f'lan_tournament_tournaments_count{{{party}, '
        'status="REGISTRATION_CLOSED"} 0'
    ) in lines
    assert f'lan_tournament_matches_confirmed_count{{{party}}} 1' in lines
    assert f'lan_tournament_matches_unconfirmed_count{{{party}}} 1' in lines
    assert f'lan_tournament_participants_count{{{party}}} 1' in lines
    assert f'lan_tournament_teams_count{{{party}}} 1' in lines


@pytest.mark.parametrize('metrics_enabled', [True])
def test_metrics_endpoint_is_reachable_without_login(client):
    response = client.get(URL)

    assert response.status_code == 200


@pytest.mark.parametrize('metrics_enabled', [True])
def test_metrics_endpoint_exposes_only_party_and_status_labels(
    client, seeded_tournament
):
    response = client.get(URL)

    assert response.status_code == 200

    label_names = set(LABEL_NAMES.findall(response.get_data(as_text=True)))
    assert label_names
    assert label_names <= {'party', 'status'}
