"""
tests.integration.services.lan_tournament.test_admin_seeding_endpoints
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin seeding page, action and generate routes through a real
admin app.
"""

from datetime import datetime, UTC
from itertools import count
import re

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
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

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'
JSON = {'Accept': 'application/json'}

PARTY_ID = PartyID('lan-party-admin-seeding-endpoints')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('adminseedingbrand', 'Admin Seeding Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Admin Seeding')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'AdminSeedingUser{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.administrate'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture(scope='module')
def viewer_client(make_client, admin_app, viewer):
    return make_client(admin_app, user_id=viewer.id)


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(
        status=TournamentStatus.REGISTRATION_CLOSED,
        participants=8,
        mode=(GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION),
        **kwargs,
    ):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Admin Seeding Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=mode[0],
            elimination_mode=mode[1],
            tournament_status=status,
            **kwargs,
        )
        assert result.is_ok()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:participants]:
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _url(tournament, suffix=''):
    return f'{BASE_URL}/tournaments/{tournament.id}/seeding{suffix}'


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


def test_board_disables_regenerate_while_a_result_is_confirmed(
    client, make_tournament, admin
):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()
    assert svc.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).is_ok()
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_matches([m.id for m in matches])
    match = next(m for m in matches if m.round == 0 and len(contestants[m.id]) == 2)
    assert tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id,
        {c.participant_id: i for i, c in enumerate(contestants[match.id])},
    ).is_ok()
    board = svc.get_board(tournament.id).unwrap()
    response = client.post(
        _url(tournament, '/actions'), data=_swap(board.version, 0, 7), headers=JSON
    )
    assert response.status_code == 200
    payload = response.get_json()['board']
    reason = 'A match already has a confirmed result. Take it back before you regenerate.'
    assert payload.get('regenerate_refusal') == reason
    html = client.get(_url(tournament)).get_data(as_text=True)
    assert reason in html
    assert re.search(r'<button[^>]* disabled[^>]*>Regenerate from this code</button>', html)
    before = _match_ids(tournament)
    _flashes(client)
    response = client.post(
        _url(tournament, '/generate'),
        data={'target': 'initial', 'version': str(payload['version'])},
    )
    assert response.status_code == 302
    assert reason in _flashes(client)
    assert _match_ids(tournament) == before
    assert tournament_repository.get_match(match.id).confirmed_by == admin.id


def test_seeding_page_renders_for_admin(client, make_tournament):
    tournament = make_tournament()

    response = client.get(_url(tournament))

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    board = svc.get_board(tournament.id).unwrap()
    assert board.code in html
    assert 'name="p"' in html
    assert 'seeding-drawn' in html
    assert '/seeding/actions' in html


def test_seeding_page_before_registration_closes_renders_the_empty_state(
    client, make_tournament
):
    tournament = make_tournament(status=TournamentStatus.REGISTRATION_OPEN)

    response = client.get(_url(tournament))

    assert response.status_code == 200
    assert (
        'The seeding opens once registration is closed.'
        in response.get_data(as_text=True)
    )
    assert _stored(tournament) is None


def test_seeding_action_json_returns_board(client, make_tournament):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()
    before = board.state.layout

    response = client.post(
        _url(tournament, '/actions'), data=_swap(board.version), headers=JSON
    )

    assert response.status_code == 200
    payload = response.get_json()['board']
    assert payload['version'] == board.version + 1
    assert payload['fix_count'] == 1
    slots = [
        slot['contestant_id']
        for match in payload['layout']['matches']
        for slot in match['slots']
    ]
    assert slots[0] == before[1]
    assert slots[1] == before[0]
    assert _stored(tournament).version == board.version + 1


def test_seeding_action_without_json_redirects(client, make_tournament):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    response = client.post(
        _url(tournament, '/actions'), data=_swap(board.version)
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/seeding?target=initial')
    assert _stored(tournament).version == board.version + 1


def test_seeding_action_version_conflict_409(client, make_tournament):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()
    first = client.post(
        _url(tournament, '/actions'), data=_swap(board.version), headers=JSON
    )
    assert first.status_code == 200
    stored_code = _stored(tournament).seed_code

    response = client.post(
        _url(tournament, '/actions'),
        data=_swap(board.version, p=2, q=3),
        headers=JSON,
    )

    assert response.status_code == 409
    assert response.get_json()['error']
    stored = _stored(tournament)
    assert stored.version == board.version + 1
    assert stored.seed_code == stored_code


def test_seeding_action_version_conflict_without_json_flashes(
    client, make_tournament
):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()
    client.post(_url(tournament, '/actions'), data=_swap(board.version))

    response = client.post(
        _url(tournament, '/actions'), data=_swap(board.version, p=2, q=3)
    )

    assert response.status_code == 302
    assert _stored(tournament).version == board.version + 1


@pytest.mark.parametrize(
    'data',
    [
        {'version': 'x', 'action': 'redraw'},
        {'version': '1', 'action': 'nonsense'},
        {'version': '1', 'action': 'swap', 'p': 'a', 'q': '1'},
        {'version': '1', 'action': 'replay', 'code': '  '},
    ],
)
def test_seeding_action_malformed_422(client, make_tournament, data):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    response = client.post(
        _url(tournament, '/actions'), data=data, headers=JSON
    )

    assert response.status_code == 422
    assert response.get_json()['error']
    assert _stored(tournament).version == board.version


def test_seeding_action_rule_error_422(client, make_tournament):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    response = client.post(
        _url(tournament, '/actions'),
        data=_swap(board.version, p=0, q=99),
        headers=JSON,
    )

    assert response.status_code == 422
    assert _stored(tournament).version == board.version


def test_seeding_tier_action_json_for_ffa(client, make_tournament):
    tournament = make_tournament(
        mode=(GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION),
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
    )
    board = svc.get_board(tournament.id).unwrap()

    response = client.post(
        _url(tournament, '/actions'),
        data={
            'target': 'initial',
            'version': str(board.version),
            'action': 'set_tier_count',
            'n': '3',
        },
        headers=JSON,
    )

    assert response.status_code == 200
    payload = response.get_json()['board']
    assert payload['tier_count'] == 3
    assert [t['letter'] for t in payload['tiers']] == ['A', 'B', 'C']
    assert sum(len(t['contestants']) for t in payload['tiers']) == 8
    assert payload['layout']['kind'] == 'groups'
    assert payload['balance'] is not None


def test_seeding_generate_redirects(client, make_tournament):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    response = client.post(
        _url(tournament, '/generate'),
        data={'target': 'initial', 'version': str(board.version)},
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/seeding?target=initial')
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    assert len(matches) > 0
    assert (
        _stored(tournament).generated_seed_code == _stored(tournament).seed_code
    )


def test_seeding_generate_with_stale_version_generates_nothing(
    client, make_tournament
):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    response = client.post(
        _url(tournament, '/generate'),
        data={'target': 'initial', 'version': str(board.version + 5)},
    )

    assert response.status_code == 302
    assert _stored(tournament).generated_seed_code is None
    assert not tournament_match_service.get_matches_for_tournament(
        tournament.id
    )


def test_seeding_requires_administrate_permission(
    viewer_client, make_tournament
):
    tournament = make_tournament()
    board = svc.get_board(tournament.id).unwrap()

    assert viewer_client.get(_url(tournament)).status_code == 403
    assert (
        viewer_client.post(
            _url(tournament, '/actions'), data=_swap(board.version)
        ).status_code
        == 403
    )
    assert (
        viewer_client.post(
            _url(tournament, '/generate'), data={'version': '1'}
        ).status_code
        == 403
    )
    assert _stored(tournament).version == board.version


def test_seeding_unknown_tournament_404(client):
    response = client.get(f'{BASE_URL}/tournaments/not-a-uuid/seeding')

    assert response.status_code == 404


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


def test_double_posted_generate_flashes_conflict_not_regenerates(
    client, make_tournament
):
    tournament = make_tournament()
    version = svc.get_board(tournament.id).unwrap().version
    data = {'target': 'initial', 'version': str(version)}
    first = client.post(_url(tournament, '/generate'), data=data)
    assert first.status_code == 302
    placed = _match_ids(tournament)
    assert placed
    _flashes(client)

    response = client.post(_url(tournament, '/generate'), data=data)

    assert response.status_code == 302
    assert svc.ERR_CONFLICT in _flashes(client)
    assert _match_ids(tournament) == placed


def test_generate_post_with_unchanged_code_flashes_notice(
    client, make_tournament
):
    tournament = make_tournament()
    version = svc.get_board(tournament.id).unwrap().version
    client.post(
        _url(tournament, '/generate'),
        data={'target': 'initial', 'version': str(version)},
    )
    placed = _match_ids(tournament)
    current = _stored(tournament).version
    assert current == version + 1
    _flashes(client)

    response = client.post(
        _url(tournament, '/generate'),
        data={'target': 'initial', 'version': str(current)},
    )

    assert response.status_code == 302
    assert svc.MSG_UNCHANGED in _flashes(client)
    assert _match_ids(tournament) == placed
    assert _stored(tournament).version == current
