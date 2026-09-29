"""
tests.integration.services.lan_tournament.test_create_wizard_http
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the admin create form, the create POST and the pre-check through
a real admin app, the way a browser without JavaScript would.
"""

from datetime import datetime, UTC
from io import BytesIO
import os
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from PIL import Image
import pytest
from sqlalchemy import update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_image_service,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_request import (
    DbTournamentRequest,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
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

PARTY_ID = PartyID('lan-party-create-wizard-http')

_S = 'byceps.services.lan_tournament.tournament_image_service'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Create Wizard HTTP Party')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.create'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


def _create_url() -> str:
    return f'{BASE_URL}/for_party/{PARTY_ID}'


def _form_url() -> str:
    return f'{BASE_URL}/for_party/{PARTY_ID}/create'


def _validate_url() -> str:
    return f'{BASE_URL}/for_party/{PARTY_ID}/create/validate'


def _solo_data(name: str, **extra) -> dict:
    return {
        'name': name,
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'max_players': '16',
        **extra,
    }


def _tournament_by_token(token: str):
    return tournament_service.find_tournament_by_creation_token(UUID(token))


def _png(size=(1000, 600)) -> bytes:
    buf = BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buf, format='PNG')
    return buf.getvalue()


def _noise_png(size) -> bytes:
    buf = BytesIO()
    Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3)).save(
        buf, format='PNG'
    )
    return buf.getvalue()


def test_create_form_get_renders_200(client, party):
    response = client.get(_form_url())

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-lt-wizard' in html
    assert 'id="lt-create-wizard-config"' in html
    match = re.search(
        r'name="submission_token"[^>]*value="([0-9a-f-]{36})"', html
    ) or re.search(r'value="([0-9a-f-]{36})"[^>]*name="submission_token"', html)
    assert match is not None


def test_nojs_team_ffa_de_create_via_form_post(client, party):
    token = str(uuid4())

    response = client.post(
        _create_url(),
        data={
            'submission_token': token,
            'name': 'Team FFA DE via form',
            'contestant_type': 'TEAM',
            'game_format': 'FREE_FOR_ALL',
            'elimination_mode': 'DOUBLE_ELIMINATION',
            'max_teams': '8',
            'min_players_in_team': '2',
            'max_players_in_team': '2',
            'point_table': '10, 6, 3',
            'group_size_min': '3',
            'group_size_max': '4',
            'advancement_count': '2',
            'points_carry_to_losers': 'y',
        },
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    tournament = _tournament_by_token(token)
    assert tournament is not None
    assert response.headers['Location'].endswith(
        f'/tournaments/{tournament.id}'
    )
    assert tournament.party_id == PARTY_ID
    assert tournament.tournament_status is TournamentStatus.DRAFT
    assert tournament.contestant_type is ContestantType.TEAM
    assert tournament.game_format is GameFormat.FREE_FOR_ALL
    assert tournament.elimination_mode is EliminationMode.DOUBLE_ELIMINATION
    assert tournament.point_table == [10, 6, 3]
    assert (tournament.group_size_min, tournament.group_size_max) == (3, 4)
    assert tournament.advancement_count == 2
    assert tournament.points_carry_to_losers is True
    assert tournament.max_teams == 8
    assert tournament.max_players is None
    assert tournament.image_id is None


def test_invalid_post_rerenders_form_with_field_errors(client, party):
    token = str(uuid4())

    response = client.post(
        _create_url(),
        data={
            'submission_token': token,
            'name': 'Missing structure',
            'game_format': 'FREE_FOR_ALL',
            'elimination_mode': 'SINGLE_ELIMINATION',
        },
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Please choose whether individuals or teams compete.' in html
    assert 'Add points for at least place 1.' in html
    assert _tournament_by_token(token) is None
    # The token survives the failed attempt.
    assert token in html


def test_nojs_create_with_uploaded_file_attaches_image(
    client, party, admin, data_dir
):
    token = str(uuid4())

    response = client.post(
        _create_url(),
        data=_solo_data(
            'Tournament with file',
            submission_token=token,
            image_alt_text='A blue field',
            image=(BytesIO(_png((1920, 1080))), 'cover.png'),
        ),
        content_type='multipart/form-data',
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    tournament = _tournament_by_token(token)
    assert tournament is not None
    assert tournament.image_id is not None
    assert tournament.image_alt_text == 'A blue field'
    assert tournament.image_url == (
        f'/data/parties/{PARTY_ID}/lan_tournament/images/'
        f'{tournament.image_id}.png'
    )
    assert list(data_dir.rglob(f'{tournament.image_id}.png'))


@pytest.mark.parametrize(
    ('field', 'length', 'limit'),
    [
        ('name', 81, 80),
        ('game', 81, 80),
        ('description', 10_001, 10_000),
        ('ruleset', 10_001, 10_000),
    ],
)
def test_nojs_over_length_text_is_rejected_by_the_server(
    client, party, field, length, limit
):
    token = str(uuid4())
    data = _solo_data('Over length', submission_token=token)
    data[field] = 'x' * length

    response = client.post(_create_url(), data=data)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'At most {limit} characters' in html
    assert f'currently {length}.' in html
    assert _tournament_by_token(token) is None
    assert 'maxlength' not in re.search(
        rf'<[^>]*id="{field}"[^>]*>', html
    ).group(0)


def test_repost_same_token_creates_one_tournament(client, party):
    token = str(uuid4())
    data = _solo_data('Double submit', submission_token=token)
    before = len(tournament_service.get_tournaments_for_party(PARTY_ID))

    first = client.post(_create_url(), data=data)
    second = client.post(_create_url(), data=data)

    assert first.status_code == 302
    assert second.status_code == 302
    assert second.headers['Location'] == first.headers['Location']
    after = tournament_service.get_tournaments_for_party(PARTY_ID)
    assert len(after) == before + 1
    assert [t.name for t in after].count('Double submit') == 1


def test_other_party_token_is_ignored_and_creates_in_own_party(
    client, party, make_party, brand
):
    other = make_party(brand, PartyID('lan-party-create-wizard-http-b'), 'B')
    token = str(uuid4())
    first = client.post(
        _create_url(), data=_solo_data('Token owner', submission_token=token)
    )
    assert first.status_code == 302
    owner = _tournament_by_token(token)
    assert owner is not None and owner.party_id == PARTY_ID

    url_b = f'{BASE_URL}/for_party/{other.id}'
    data = _solo_data('Token thief', submission_token=token)
    second = client.post(url_b, data=data)

    assert second.status_code == 302
    assert str(owner.id) not in second.headers['Location']
    in_b = tournament_service.get_tournaments_for_party(other.id)
    assert [t.name for t in in_b] == ['Token thief']
    assert _tournament_by_token(token).id == owner.id
    assert len(tournament_service.get_tournaments_for_party(PARTY_ID)) >= 1


def test_forged_foreign_token_double_post_creates_one_tournament(
    client, party, make_party, brand
):
    other = make_party(brand, PartyID('lan-party-create-wizard-http-c'), 'C')
    token = str(uuid4())
    first = client.post(
        _create_url(), data=_solo_data('Token owner C', submission_token=token)
    )
    assert first.status_code == 302
    url_c = f'{BASE_URL}/for_party/{other.id}'
    data = _solo_data('Forged twice', submission_token=token)

    second = client.post(url_c, data=data)
    third = client.post(url_c, data=data)

    assert second.status_code == 302
    assert third.status_code == 302
    assert third.headers['Location'] == second.headers['Location']
    in_c = tournament_service.get_tournaments_for_party(other.id)
    assert [t.name for t in in_c] == ['Forged twice']
    assert in_c[0].creation_token == uuid5(UUID(token), str(other.id))


def test_unavailable_image_id_is_blanked_so_resubmit_creates(client, party):
    data = _solo_data(
        'Stale image',
        submission_token=str(uuid4()),
        image_id=str(uuid4()),
    )

    first = client.post(_create_url(), data=data)

    assert first.status_code == 200
    html = first.get_data(as_text=True)
    assert 'This image is no longer available' in html
    hidden = re.search(r'<input[^>]*name="image_id"[^>]*>', html).group(0)
    value = re.search(r'value="([^"]*)"', hidden)
    assert value is None or value.group(1) == ''

    second = client.post(
        _create_url(),
        data={**data, 'image_id': value.group(1) if value else ''},
    )
    assert second.status_code == 302


def test_create_accepts_body_above_core_limit(client, party):
    data = _noise_png((1200, 1200))  # ~4.3 MB, core cap is 4,000,000
    assert 4_000_000 < len(data) < tournament_image_service.MAX_UPLOAD_BYTES
    token = str(uuid4())

    response = client.post(
        _create_url(),
        data=_solo_data(
            'Big upload',
            submission_token=token,
            image=(BytesIO(data), 'big.png'),
        ),
        content_type='multipart/form-data',
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    tournament = _tournament_by_token(token)
    assert tournament is not None
    assert tournament.image_id is not None


def test_create_over_body_limit_rerenders_form_with_image_error(client, party):
    body = b'\x89PNG\r\n\x1a\n' + b'\x00' * (
        tournament_image_service.MAX_REQUEST_BYTES + 1024
    )
    token = str(uuid4())
    before = len(tournament_service.get_tournaments_for_party(PARTY_ID))

    response = client.post(
        f'{_create_url()}?from_request={uuid4()}',
        data=_solo_data(
            'Too big', submission_token=token, image=(BytesIO(body), 'x.png')
        ),
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    html = response.get_data(as_text=True)
    assert 'data-lt-wizard' in html
    assert 'The maximum is 5 MB' in html
    assert 'Request Entity Too Large' not in html
    assert _tournament_by_token(token) is None
    assert len(tournament_service.get_tournaments_for_party(PARTY_ID)) == before


def test_validate_create_over_body_limit_is_json_413(client, party):
    body = b'\x00' * (tournament_image_service.MAX_REQUEST_BYTES + 1024)

    response = client.post(
        _validate_url(),
        data=_solo_data('Too big', image=(BytesIO(body), 'x.png')),
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    assert 'The maximum is 5 MB' in response.get_json()['error']


def test_validate_create_returns_json_and_writes_nothing(client, party):
    token = str(uuid4())
    before = len(tournament_service.get_tournaments_for_party(PARTY_ID))

    invalid = client.post(
        _validate_url(),
        data={'submission_token': token, 'name': 'Only a name'},
    )
    valid = client.post(
        _validate_url(), data=_solo_data('Fine', submission_token=token)
    )

    assert invalid.status_code == 200
    body = invalid.get_json()
    assert body['ok'] is False
    assert 'contestant_type' in body['errors']
    assert body['first_error_step'] == 1
    assert valid.status_code == 200
    assert valid.get_json()['ok'] is True
    assert len(tournament_service.get_tournaments_for_party(PARTY_ID)) == before
    assert _tournament_by_token(token) is None


@pytest.mark.parametrize(
    ('game_format', 'mode', 'extra'),
    [
        (
            'FREE_FOR_ALL',
            'ROUND_ROBIN',
            {'point_table': '3, 2, 1', 'group_size_max': '4'},
        ),
        ('ONE_V_ONE', 'NONE', {}),
    ],
)
def test_tampered_post_with_an_impossible_mode_is_rejected(
    client, party, game_format, mode, extra
):
    token = str(uuid4())

    response = client.post(
        _create_url(),
        data={
            'submission_token': token,
            'name': 'Tampered mode',
            'contestant_type': 'SOLO',
            'game_format': game_format,
            'elimination_mode': mode,
            'max_players': '16',
            **extra,
        },
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert (
        'This combination of game format and elimination mode is not supported.'
    ) in html
    assert _tournament_by_token(token) is None


def test_highscore_post_with_a_bracket_mode_is_stored_without_a_bracket(
    client, party
):
    token = str(uuid4())

    response = client.post(
        _create_url(),
        data={
            'submission_token': token,
            'name': 'Tampered highscore',
            'contestant_type': 'SOLO',
            'game_format': 'HIGHSCORE',
            'elimination_mode': 'SINGLE_ELIMINATION',
            'score_ordering': 'HIGHER_IS_BETTER',
            'max_players': '16',
        },
    )

    assert response.status_code == 302, response.get_data(as_text=True)
    tournament = _tournament_by_token(token)
    assert tournament.game_format is GameFormat.HIGHSCORE
    assert tournament.elimination_mode is EliminationMode.NONE


# --- A request link the server refuses ---

_REQUEST_ADMIN_PERMISSIONS = {
    'admin.access',
    'lan_tournament.create',
    'lan_tournament.request_view',
    'lan_tournament.request_decide',
}


@pytest.fixture(scope='module')
def request_admin(make_admin):
    user = make_admin(_REQUEST_ADMIN_PERMISSIONS, screen_name='RefusalAdmin')
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def request_client(make_client, admin_app, request_admin):
    return make_client(admin_app, user_id=request_admin.id)


@pytest.fixture(scope='module')
def proposer(make_user):
    return make_user(screen_name='Oma_Gerda')


def _accepted_request(proposer, decider):
    result = tournament_request_service.submit_request(
        PARTY_ID,
        proposer.id,
        party_capacity=None,
        name='Rollator-Rallye 2026',
        game='Mario Kart 8 Deluxe',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=16,
        preferred_start_time=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 3, 18, 0, tzinfo=UTC),
        description='Drei Cups.',
        special_rules=None,
    )
    assert result.is_ok(), result.unwrap_err()
    request, _event = result.unwrap()
    accepted = tournament_request_service.accept_request(request.id, decider.id)
    assert accepted.is_ok(), accepted.unwrap_err()
    return request


def _withdraw(request_id, at: datetime) -> None:
    """Withdraw an accepted request, which the service refuses to do."""
    db.session.execute(
        update(DbTournamentRequest)
        .where(DbTournamentRequest.id == request_id)
        .values(status='withdrawn', updated_at=at)
    )
    db.session.commit()


def _visible(html: str) -> str:
    """Return the page without the JSON island the script reads."""
    return re.sub(
        r'<script type="application/json".*?</script>', '', html, flags=re.S
    )


def _post_linked_create(client, request_id, **extra):
    return client.post(
        _create_url(),
        data=_solo_data(
            'Refused link',
            from_request_id=str(request_id),
            submission_token=str(uuid4()),
            **extra,
        ),
    )


def test_refusal_is_one_box_with_who_when_and_two_actions(
    request_client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)
    _withdraw(request.id, datetime(2026, 10, 3, 8, 24))
    number = f'{request.number:04d}'

    response = _post_linked_create(request_client, request.id)

    assert response.status_code == 200
    page = _visible(response.get_data(as_text=True))
    assert page.count('data-wiz-refusal') == 1
    box = page[page.index('data-wiz-refusal') :]
    box = box[: box.index('</div>\n          </div>')]
    assert f'<strong>Request #{number} is no longer accepted.</strong>' in box
    assert 'Oma_Gerda withdrew it at 10:24.' in box
    assert 'No tournament was created; your entries are kept.' in box
    assert 'data-wiz-unlink' in box
    assert f'/lan-tournaments/requests/{request.id}"' in box
    assert 'View request' in box
    # Shown once: not repeated in a summary.
    assert page.count('is no longer accepted') == 1
    assert 'data-wiz-errsum' not in page
    # Unlink-on-refusal: the hidden field is empty for the next submit.
    assert re.search(r'<input[^>]*name="from_request_id"[^>]*value=""', page)
    assert 'Refused link' in page
    token = re.search(
        r'<input[^>]*name="submission_token"[^>]*value="([0-9a-f-]{36})"', page
    )
    assert _tournament_by_token(token.group(1)) is None


def test_refusal_of_a_rejected_request_names_the_decider(
    request_client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)
    rejected = tournament_request_service.reject_request(
        request.id, request_admin.id, 'No room.'
    )
    assert rejected.is_ok(), rejected.unwrap_err()

    response = _post_linked_create(request_client, request.id)

    page = _visible(response.get_data(as_text=True))
    assert page.count('data-wiz-refusal') == 1
    assert re.search(r'RefusalAdmin rejected it at \d\d:\d\d\.', page)


def test_refusal_stays_hidden_from_an_admin_without_request_rights(
    client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)
    _withdraw(request.id, datetime(2026, 10, 3, 8, 24))

    response = _post_linked_create(client, request.id)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-wiz-refusal' not in html
    assert 'Oma_Gerda' not in html


def test_precheck_reports_the_refusal_for_a_stale_request(
    request_client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)
    _withdraw(request.id, datetime(2026, 10, 3, 8, 24))

    response = request_client.post(
        _validate_url(),
        data=_solo_data('Refused link', from_request_id=str(request.id)),
    )

    body = response.get_json()
    assert body['ok'] is False
    refusal = body['refusal']
    assert (
        refusal['lead']
        == f'Request #{request.number:04d} is no longer accepted.'
    )
    assert refusal['detail'] == 'Oma_Gerda withdrew it at 10:24.'
    assert refusal['viewUrl'].endswith(f'/requests/{request.id}')


def test_precheck_has_no_refusal_for_a_usable_request(
    request_client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)

    response = request_client.post(
        _validate_url(),
        data=_solo_data('Usable link', from_request_id=str(request.id)),
    )

    assert response.get_json()['refusal'] is None


@pytest.fixture(scope='module')
def user_viewer_client(make_admin, make_client, admin_app):
    user = make_admin(
        _REQUEST_ADMIN_PERMISSIONS | {'user.view'}, screen_name='UserViewer'
    )
    log_in_user(user.id)
    return make_client(admin_app, user_id=user.id)


def _prefilled_page(client, request) -> str:
    response = client.get(f'{_form_url()}?from_request={request.id}')

    assert response.status_code == 200
    return _visible(response.get_data(as_text=True))


def test_prefill_links_the_proposer_for_an_admin_who_may_view_users(
    user_viewer_client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)

    page = _prefilled_page(user_viewer_client, request)

    link = f'<a href="/users/{proposer.id}">Oma_Gerda</a>'
    assert page.count(link) == 2


def test_prefill_shows_the_proposer_as_text_without_the_user_view_right(
    request_client, request_admin, proposer, party
):
    request = _accepted_request(proposer, request_admin)

    page = _prefilled_page(request_client, request)

    assert f'/users/{proposer.id}' not in page
    assert page.count('Oma_Gerda') >= 2
