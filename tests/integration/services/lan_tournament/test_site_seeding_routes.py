"""
tests.integration.services.lan_tournament.test_site_seeding_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the seeding routes of scoped orgas through a real site app.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_orga_service,
    tournament_repository,
    tournament_seeding_repository,
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
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7

from tests.helpers import http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/lan-tournaments'
JSON = {'Accept': 'application/json'}

OTHER_PARTY_ID = PartyID('lan-party-site-seeding-other')

_counter = count(1)


@pytest.fixture(scope='module')
def other_party(make_party, make_brand):
    brand = make_brand('siteseedingotherbrand', 'Site Seeding Other Brand')
    return make_party(brand, OTHER_PARTY_ID, 'LAN Party Site Seeding Other')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'SiteSeedingPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('SiteSeedingOrga')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def bystander(make_user):
    user = make_user('SiteSeedingBystander')
    log_in_user(user.id)
    return user


@pytest.fixture
def make_tournament(players, orga):
    created = []

    def _make(party_id, *, orga_user=orga):
        result = tournament_service.create_tournament(
            party_id,
            f'Site Seeding Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        tournament, _ = result.unwrap()
        created.append(tournament)
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
        if orga_user is not None:
            tournament_orga_service.assign_orga(
                tournament.id, orga_user.id, orga_user.id
            ).unwrap()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _url(tournament, suffix=''):
    return f'{BASE_URL}/orga/tournaments/{tournament.id}/seeding{suffix}'


def _stored(tournament):
    db.session.rollback()
    return tournament_seeding_repository.find_seeding(tournament.id, 'initial')


def _swap(version, p=0, q=1):
    return {
        'target': 'initial',
        'version': str(version),
        'action': 'swap',
        'p': str(p),
        'q': str(q),
    }


def test_scoped_orga_can_open_seeding(site_app, party, orga, make_tournament):
    tournament = make_tournament(party.id)

    with http_client(site_app, user_id=orga.id) as client:
        response = client.get(_url(tournament))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    board = svc.get_board(tournament.id).unwrap()
    assert board.code in html
    assert '/seeding/actions' in html


def test_scoped_orga_action_json_and_conflict(
    site_app, party, orga, make_tournament
):
    tournament = make_tournament(party.id)
    board = svc.get_board(tournament.id).unwrap()

    with http_client(site_app, user_id=orga.id) as client:
        ok = client.post(
            _url(tournament, '/actions'),
            data=_swap(board.version),
            headers=JSON,
        )
        stale = client.post(
            _url(tournament, '/actions'),
            data=_swap(board.version, p=2, q=3),
            headers=JSON,
        )
        malformed = client.post(
            _url(tournament, '/actions'),
            data={'version': '1', 'action': 'nope'},
            headers=JSON,
        )
        plain = client.post(
            _url(tournament, '/actions'), data=_swap(board.version + 1)
        )

    assert ok.status_code == 200
    assert ok.get_json()['board']['version'] == board.version + 1
    assert stale.status_code == 409
    assert malformed.status_code == 422
    assert plain.status_code == 302
    assert _stored(tournament).version == board.version + 2


def test_non_orga_forbidden(site_app, party, bystander, make_tournament):
    tournament = make_tournament(party.id)
    board = svc.get_board(tournament.id).unwrap()

    with http_client(site_app, user_id=bystander.id) as client:
        get = client.get(_url(tournament))
        action = client.post(
            _url(tournament, '/actions'), data=_swap(board.version)
        )
        generate = client.post(
            _url(tournament, '/generate'),
            data={'version': str(board.version)},
        )

    assert get.status_code == 403
    assert action.status_code == 403
    assert generate.status_code == 403
    stored = _stored(tournament)
    assert stored.version == board.version
    assert stored.generated_seed_code is None


def test_anonymous_is_not_served(site_app, party, make_tournament):
    tournament = make_tournament(party.id)

    with http_client(site_app) as client:
        response = client.get(_url(tournament))

    assert response.status_code in (302, 401, 403)
    assert b'seeding/actions' not in response.data


def test_other_party_tournament_404(
    site_app, other_party, orga, make_tournament
):
    tournament = make_tournament(other_party.id)

    with http_client(site_app, user_id=orga.id) as client:
        get = client.get(_url(tournament))
        action = client.post(
            _url(tournament, '/actions'), data=_swap(1), headers=JSON
        )
        generate = client.post(
            _url(tournament, '/generate'), data={'version': '1'}
        )

    assert get.status_code == 404
    assert action.status_code == 404
    assert generate.status_code == 404
    assert _stored(tournament) is None


def test_orga_generate_from_seeding(site_app, party, orga, make_tournament):
    tournament = make_tournament(party.id)
    board = svc.get_board(tournament.id).unwrap()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/generate'),
            data={'target': 'initial', 'version': str(board.version)},
        )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/seeding?target=initial')
    assert tournament_match_service.get_matches_for_tournament(tournament.id)
    stored = _stored(tournament)
    assert stored.generated_seed_code == stored.seed_code


def test_orga_generate_with_stale_version_generates_nothing(
    site_app, party, orga, make_tournament
):
    tournament = make_tournament(party.id)
    board = svc.get_board(tournament.id).unwrap()

    with http_client(site_app, user_id=orga.id) as client:
        response = client.post(
            _url(tournament, '/generate'),
            data={'version': str(board.version + 5)},
        )

    assert response.status_code == 302
    assert _stored(tournament).generated_seed_code is None
    assert not tournament_match_service.get_matches_for_tournament(
        tournament.id
    )


def _flashes(client):
    with client.session_transaction() as session:
        flashes = session.pop('_flashes', None) or []
    return ' | '.join(str(message) for _, message in flashes)


def _match_ids(tournament):
    db.session.rollback()
    return {
        m.id
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    }


def test_site_double_posted_generate_is_refused(
    site_app, party, orga, make_tournament
):
    tournament = make_tournament(party.id)
    version = svc.get_board(tournament.id).unwrap().version
    data = {'target': 'initial', 'version': str(version)}

    with http_client(site_app, user_id=orga.id) as client:
        first = client.post(_url(tournament, '/generate'), data=data)
        placed = _match_ids(tournament)
        _flashes(client)
        second = client.post(_url(tournament, '/generate'), data=data)
        flashed = _flashes(client)

    assert first.status_code == 302
    assert placed
    assert second.status_code == 302
    assert svc.ERR_CONFLICT in flashed
    assert _match_ids(tournament) == placed


def test_site_generate_post_with_unchanged_code_flashes_notice(
    site_app, party, orga, make_tournament
):
    tournament = make_tournament(party.id)
    version = svc.get_board(tournament.id).unwrap().version

    with http_client(site_app, user_id=orga.id) as client:
        client.post(
            _url(tournament, '/generate'),
            data={'target': 'initial', 'version': str(version)},
        )
        placed = _match_ids(tournament)
        current = _stored(tournament).version
        _flashes(client)
        response = client.post(
            _url(tournament, '/generate'),
            data={'target': 'initial', 'version': str(current)},
        )
        flashed = _flashes(client)

    assert current == version + 1
    assert response.status_code == 302
    assert svc.MSG_UNCHANGED in flashed
    assert _match_ids(tournament) == placed
    assert _stored(tournament).version == current
