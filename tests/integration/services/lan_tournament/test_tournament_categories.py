from datetime import datetime, timedelta, UTC
import re
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import update

from byceps.database import db

from byceps.services.lan_tournament import (
    tournament_orga_service,
    tournament_repository,
    tournament_request_repository,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_category import (
    TournamentCategory,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament

from tests.helpers import log_in_user


pytestmark = pytest.mark.usefixtures('admin_app')
BASE_URL = 'http://admin.acmecon.test/lan-tournaments'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, title='Category Party')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {
            'admin.access',
            'lan_tournament.create',
            'lan_tournament.update',
            'lan_tournament.view',
            'lan_tournament.request_view',
            'lan_tournament.request_decide',
        }
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture
def accepted_request(party, admin, make_user):
    proposer = make_user()
    now = datetime.now(UTC)
    request, _ = tournament_request_service.submit_request(
        party.id,
        proposer.id,
        party_capacity=None,
        name='Requested Cup',
        game='CS2',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=16,
        preferred_start_time=now,
        preferred_end_time=now + timedelta(hours=1),
        description='A cup.',
    ).unwrap()
    tournament_request_service.accept_request(request.id, admin.id).unwrap()
    return request


def _create_data(**extra):
    return {
        'name': 'Category Cup',
        'category': 'MAIN',
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'submission_token': str(uuid4()),
        **extra,
    }


@pytest.mark.parametrize('category', list(TournamentCategory))
def test_repository_round_trip_and_update_preserve_other_fields(
    party, category
):
    token = uuid4()
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Category round trip',
        category=category,
        position=42,
        creation_token=token,
        image_alt_text='Cover',
    ).unwrap()
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.category is category
    next_category = list(TournamentCategory)[
        (list(TournamentCategory).index(category) + 1) % 4
    ]
    tournament_service.update_tournament(
        stored.id,
        name=stored.name,
        category=next_category,
    ).unwrap()
    updated = tournament_service.get_tournament(stored.id)
    assert updated.category is next_category
    assert updated.position == 42
    assert updated.creation_token == token
    assert updated.image_alt_text == 'Cover'
    tournament_service.update_tournament(updated.id, name=updated.name).unwrap()
    assert (
        tournament_service.get_tournament(updated.id).category is next_category
    )


def test_regular_create_defaults_to_main(party):
    tournament, _ = tournament_service.create_tournament(
        party.id, 'Default'
    ).unwrap()
    assert (
        tournament_service.get_tournament(tournament.id).category
        is TournamentCategory.MAIN
    )


@pytest.mark.parametrize('category', [None, *TournamentCategory])
def test_request_service_default_and_admin_override(
    party, admin, accepted_request, category
):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Request category',
        category=category,
        created_from_request_id=accepted_request.id,
        initiator_id=admin.id,
    ).unwrap()
    expected = category or TournamentCategory.USER_ORGANIZED
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.category is expected
    assert stored.created_from_request_id == accepted_request.id
    linked = tournament_request_repository.find_request(accepted_request.id)
    assert linked.created_tournament_id == stored.id
    assert linked.status is TournamentRequestStatus.tournament_created
    tournament_service.update_tournament(
        stored.id, name=stored.name, category=TournamentCategory.MAIN
    ).unwrap()
    updated = tournament_service.get_tournament(stored.id)
    assert updated.created_from_request_id == accepted_request.id
    assert updated.position == stored.position
    assert not updated.is_user_organized


@pytest.mark.parametrize('category', list(TournamentCategory))
def test_nojs_request_create_appoints_orga_for_any_category(
    client, party, accepted_request, category
):
    data = _create_data(
        category=category.value, from_request_id=str(accepted_request.id)
    )
    response = client.post(f'{BASE_URL}/for_party/{party.id}', data=data)
    assert response.status_code == 302
    linked = tournament_request_repository.find_request(accepted_request.id)
    tournament = tournament_service.get_tournament(linked.created_tournament_id)
    assert tournament.category is category
    assert tournament.created_from_request_id == accepted_request.id
    assert tournament_orga_service.is_orga_for_tournament(
        accepted_request.proposer_id, tournament.id
    )
    assert tournament.tournament_status is TournamentStatus.DRAFT
    # A changed category in a token retry never rewrites the first creation.
    response = client.post(
        f'{BASE_URL}/for_party/{party.id}',
        data={
            **data,
            'category': 'FUN'
            if category is not TournamentCategory.FUN
            else 'MAIN',
        },
    )
    assert response.status_code == 302
    assert tournament_service.get_tournament(tournament.id).category is category


def test_request_prefill_and_failed_post_keep_the_admin_category(
    client, party, accepted_request
):
    response = client.get(
        f'{BASE_URL}/for_party/{party.id}/create?from_request={accepted_request.id}'
    )
    html = response.get_data(as_text=True)
    assert '<option selected value="USER_ORGANIZED">' in html
    for category in TournamentCategory:
        assert f'value="{category.value}"' in html
    data = _create_data(
        name='', category='STAGE', from_request_id=str(accepted_request.id)
    )
    response = client.post(f'{BASE_URL}/for_party/{party.id}', data=data)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<option selected value="STAGE">' in html
    assert not re.search(r'data-wiz-src-field="category"', html)
    assert (
        tournament_request_repository.find_request(
            accepted_request.id
        ).created_tournament_id
        is None
    )


@pytest.mark.parametrize(
    'category', ['', 'UNKNOWN', None, *[c.value for c in TournamentCategory]]
)
def test_precheck_validates_category_without_writing(client, party, category):
    data = _create_data(category=category)
    if category is None:
        del data['category']
    before = tournament_service.get_tournaments_for_party(party.id)
    response = client.post(
        f'{BASE_URL}/for_party/{party.id}/create/validate', data=data
    )
    body = response.get_json()
    assert body['ok'] is (category in {c.value for c in TournamentCategory})
    if not body['ok']:
        assert 'category' in body['errors']
        assert body['first_error_step'] == 0
        final = client.post(f'{BASE_URL}/for_party/{party.id}', data=data)
        assert final.status_code == 200
        assert 'data-first-error-step="0"' in final.get_data(as_text=True)
    assert tournament_service.get_tournaments_for_party(party.id) == before


def test_update_get_post_category_and_provenance(
    client, party, admin, accepted_request
):
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Edit category',
        created_from_request_id=accepted_request.id,
        initiator_id=admin.id,
    ).unwrap()
    response = client.get(f'{BASE_URL}/tournaments/{tournament.id}/update')
    assert '<option selected value="USER_ORGANIZED">' in response.get_data(
        as_text=True
    )
    response = client.post(
        f'{BASE_URL}/tournaments/{tournament.id}',
        data={'name': tournament.name, 'category': 'MAIN'},
    )
    assert response.status_code == 302
    stored = tournament_service.get_tournament(tournament.id)
    assert stored.category is TournamentCategory.MAIN
    assert stored.created_from_request_id == accepted_request.id


@pytest.mark.parametrize(
    'payload', [[], ['tournament_ids'], 'tournament_ids', 1, None]
)
def test_reorder_requires_a_json_object(client, party, payload):
    response = client.post(
        f'{BASE_URL}/for_party/{party.id}/sort', json=payload
    )
    assert response.status_code == 400


def test_reorder_full_payload_preserves_categories(
    client, party, make_party, brand
):
    own_party = make_party(brand, title='Reorder categories')
    tournaments = [
        tournament_service.create_tournament(
            own_party.id,
            c.value,
            category=c,
            tournament_status=TournamentStatus.DRAFT,
        ).unwrap()[0]
        for c in TournamentCategory
    ]
    ids = [str(t.id) for t in reversed(tournaments)]
    url = f'{BASE_URL}/for_party/{own_party.id}/sort'
    for invalid in [ids[:-1], [*ids, ids[0]], [*ids[:-1], str(uuid4())]]:
        assert (
            client.post(url, json={'tournament_ids': invalid}).status_code
            == 400
        )
    foreign, _ = tournament_service.create_tournament(
        party.id, 'Foreign'
    ).unwrap()
    assert (
        client.post(
            url, json={'tournament_ids': [*ids[:-1], str(foreign.id)]}
        ).status_code
        == 400
    )
    assert client.post(url, json={'tournament_ids': ids}).status_code == 204
    stored = tournament_service.get_tournaments_for_party(own_party.id)
    assert [str(t.id) for t in stored] == ids
    assert [t.position for t in stored] == list(range(4))
    assert {t.id: t.category for t in stored} == {
        t.id: t.category for t in tournaments
    }


@pytest.mark.parametrize('suffix', ['', '/overview'])
def test_admin_grouping_uses_position_and_includes_drafts(
    client, make_party, brand, suffix
):
    party = make_party(brand, title=f'Grouped dashboard {suffix}')
    for name, category, position in [
        ('A late', TournamentCategory.MAIN, 9),
        ('Fun first', TournamentCategory.FUN, 0),
        ('Z early draft', TournamentCategory.MAIN, 2),
        ('Stage', TournamentCategory.STAGE, 0),
        ('User', TournamentCategory.USER_ORGANIZED, 0),
    ]:
        tournament_service.create_tournament(
            party.id,
            name,
            category=category,
            position=position,
            tournament_status=TournamentStatus.DRAFT,
        ).unwrap()
    response = client.get(f'{BASE_URL}/for_party/{party.id}{suffix}')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert re.findall(r'data-tournament-category="([A-Z_]+)"', html) == [
        c.value for c in TournamentCategory
    ]
    assert (
        html.index('Z early draft')
        < html.index('A late')
        < html.index('Fun first')
    )
    if not suffix:
        assert html.count('data-tournament-id=') == 5
        assert html.count('data-tournament-group=') == 4
        assert 'lan_tournament_sort.js' in html
    else:
        assert 'lan_tournament_sort.js' not in html


def test_viewer_sees_grouped_drafts_but_cannot_edit_or_reorder(
    make_admin, make_client, admin_app, party
):
    viewer = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(viewer.id)
    client = make_client(admin_app, user_id=viewer.id)
    response = client.get(f'{BASE_URL}/for_party/{party.id}')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-tournament-category="MAIN"' in html
    assert 'class="drag-handle"' not in html
    assert 'lan_tournament_sort.js' not in html
    assert (
        client.post(
            f'{BASE_URL}/for_party/{party.id}/sort', json={'tournament_ids': []}
        ).status_code
        == 403
    )
    tournament = tournament_service.get_tournaments_for_party(party.id)[0]
    assert (
        client.post(
            f'{BASE_URL}/tournaments/{tournament.id}',
            data={'name': 'Forged', 'category': 'FUN'},
        ).status_code
        == 403
    )


def test_request_commit_failure_rolls_back_category_and_link(
    party, admin, accepted_request
):
    before = tournament_service.get_tournaments_for_party(party.id)
    with patch.object(
        tournament_repository,
        'commit_session',
        side_effect=RuntimeError('commit failed'),
    ):
        with pytest.raises(RuntimeError, match='commit failed'):
            tournament_service.create_tournament(
                party.id,
                'Rolled back',
                category=TournamentCategory.MAIN,
                created_from_request_id=accepted_request.id,
                initiator_id=admin.id,
            )
    assert tournament_service.get_tournaments_for_party(party.id) == before
    request = tournament_request_repository.find_request(accepted_request.id)
    assert request.status is TournamentRequestStatus.accepted
    assert request.created_tournament_id is None


def test_reorder_partial_write_failure_preserves_all_positions(party):
    before = tournament_service.get_tournaments_for_party(party.id)

    def fail_after_first_write(ids):
        db.session.execute(
            update(DbTournament)
            .where(DbTournament.id == ids[0])
            .values(position=999)
        )
        raise RuntimeError('partial write')

    with patch.object(
        tournament_repository,
        'reorder_tournaments',
        side_effect=fail_after_first_write,
    ):
        with pytest.raises(RuntimeError, match='partial write'):
            tournament_service.reorder_tournaments(
                party.id, [str(t.id) for t in before]
            )
    assert tournament_service.get_tournaments_for_party(party.id) == before
