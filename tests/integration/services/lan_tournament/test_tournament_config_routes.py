"""
tests.integration.services.lan_tournament.test_tournament_config_routes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives the configuration import and export routes through a real admin
app, the way a browser would.
"""

import base64
import functools
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from babel.messages.pofile import read_po
from markupsafe import escape
from PIL import Image
import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_config_document,
    tournament_config_service,
    tournament_image_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err

from tests.helpers import log_in_user


BASE_URL = 'http://admin.acmecon.test/lan-tournaments'

PARTY_ID = PartyID('lan-party-config-routes')

_S = 'byceps.services.lan_tournament.tournament_image_service'
_C = 'byceps.services.lan_tournament.tournament_config_service'

_PO_PATH = (
    Path(__file__).resolve().parents[4]
    / 'byceps/translations/de/LC_MESSAGES/messages.po'
)

_MAX_BYTES = tournament_config_document.MAX_DOCUMENT_BYTES


@pytest.fixture(scope='module')
def config_party(make_party, brand):
    return make_party(brand, PARTY_ID, 'Config Routes Party')


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.create'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def viewer(make_admin):
    user = make_admin({'admin.access', 'lan_tournament.view'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def full_admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.create', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def plain_admin(make_admin):
    user = make_admin({'admin.access'})
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, admin):
    return make_client(admin_app, user_id=admin.id)


@pytest.fixture(scope='module')
def viewer_client(make_client, admin_app, viewer):
    return make_client(admin_app, user_id=viewer.id)


@pytest.fixture(scope='module')
def full_client(make_client, admin_app, full_admin):
    return make_client(admin_app, user_id=full_admin.id)


@pytest.fixture(scope='module')
def plain_client(make_client, admin_app, plain_admin):
    return make_client(admin_app, user_id=plain_admin.id)


@pytest.fixture(scope='module')
def tournament(config_party):
    result = tournament_service.create_tournament(
        config_party.id,
        'Größe Äpfel Cup',
        tournament_status=TournamentStatus.DRAFT,
    )
    tournament, _event = result.unwrap()
    return tournament


@pytest.fixture(autouse=True)
def data_dir(tmp_path, config_party):
    app = SimpleNamespace(byceps_config=SimpleNamespace(data_path=tmp_path))
    with patch(f'{_S}.get_current_byceps_app', return_value=app):
        yield tmp_path


@functools.cache
def _catalog():
    with _PO_PATH.open('rb') as f:
        return read_po(f, locale='de')


def _occurrences(html: str, msgid: str, **params) -> int:
    """Count how often the page shows the message, in English or German."""
    message = _catalog().get(msgid)
    texts = [msgid, message.string] if message is not None else [msgid]
    return sum(
        html.count(str(escape(text % params if params else text)))
        for text in texts
    )


def _shown(html: str, msgid: str, **params) -> bool:
    return _occurrences(html, msgid, **params) > 0


def _image_too_large_shown(html: str) -> bool:
    """Tell if the page shows the image size message, whatever the size."""
    msgid = 'The file is %(size)s. The maximum is 5 MB.'
    message = _catalog().get(msgid)
    texts = [msgid, message.string] if message is not None else [msgid]
    return any(str(escape(t.split('%(size)s', 1)[1])) in html for t in texts)


def _form_actions(html: str, button: str) -> list[str]:
    """Return the action of each form on the page that holds the button."""
    return [
        action
        for action, body in re.findall(
            r'<form action="([^"]*)"[^>]*>(.*?)</form>',
            html,
            flags=re.DOTALL,
        )
        if button in body
    ]


def _import_url(party_id=PARTY_ID) -> str:
    return f'{BASE_URL}/for_party/{party_id}/import'


def _export_url(tournament_id) -> str:
    return f'{BASE_URL}/tournaments/{tournament_id}/export'


def _document(name: str = 'Imported Cup', **overrides) -> bytes:
    tournament = {
        'name': name,
        'category': 'MAIN',
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'max_players': 16,
        **overrides,
    }
    return _envelope(tournament)


def _envelope(tournament) -> bytes:
    return json.dumps(
        {
            'format': tournament_config_document.FORMAT,
            'version': tournament_config_document.VERSION,
            'tournament': tournament,
        }
    ).encode()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


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


def _check(client, raw: bytes, **extra):
    return client.post(
        _import_url(),
        data={
            'action': 'check',
            'config_file': (BytesIO(raw), 'config.json'),
            **extra,
        },
        content_type='multipart/form-data',
    )


def _commit(client, raw: bytes, token: str | None = None, **extra):
    return client.post(
        _import_url(),
        data={
            'action': 'import',
            'config_document': _b64(raw),
            'submission_token': token or str(uuid4()),
            **extra,
        },
        content_type='multipart/form-data',
    )


def _committed(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _count(sql: str) -> int:
    return _committed(sql)[0][0]


def _image_files(data_dir) -> list:
    return sorted(path for path in data_dir.rglob('*') if path.is_file())


def _state(data_dir) -> tuple:
    """Return what the import may write: tournaments, logs and images."""
    return (
        _count('SELECT count(*) FROM lan_tournaments'),
        _count('SELECT count(*) FROM lan_tournament_log_entries'),
        _count('SELECT count(*) FROM lan_tournament_images'),
        _image_files(data_dir),
    )


def _tournament_by_token(token: str):
    return tournament_service.find_tournament_by_creation_token(UUID(token))


def _log_events(tournament_id, event_type: str) -> list[tuple]:
    return _committed(
        'SELECT initiator_id, data FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id AND event_type = :event_type',
        id=tournament_id,
        event_type=event_type,
    )


# -- form --


def test_import_form_requires_create_permission(viewer_client, config_party):
    assert viewer_client.get(_import_url()).status_code == 403
    response = _check(viewer_client, _document())
    assert response.status_code == 403


def test_import_form_renders(client, config_party):
    response = client.get(_import_url())

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '%(' not in html
    assert 'enctype="multipart/form-data"' in html
    assert 'name="config_file"' in html
    assert 'name="action" value="check"' in html
    assert _shown(html, 'JSON file, at most %(size)s KiB.', size=256)
    assert 'name="config_document"' not in html
    assert 'name="action" value="import"' not in html


def test_import_unknown_party_is_404(client, config_party):
    assert client.get(_import_url('no-such-party')).status_code == 404

    response = client.post(
        _import_url('no-such-party'),
        data={'action': 'check', 'config_file': (BytesIO(b'{}'), 'x.json')},
        content_type='multipart/form-data',
    )
    assert response.status_code == 404


# -- check --


def test_import_check_reports_problems_and_writes_nothing(
    client, config_party, data_dir
):
    before = _state(data_dir)

    codec_problem = _check(
        client,
        _envelope({'name': 'Bad Cup', 'category': 'MAIN', 'bogus': 1}),
    )
    rule_problem = _check(client, _document('Bad Cup', max_players=0))

    assert codec_problem.status_code == 200
    html = codec_problem.get_data(as_text=True)
    assert 'notification color-danger' in html
    assert _shown(html, 'Unknown key "%(key)s".', key='bogus')
    assert rule_problem.status_code == 200
    html = rule_problem.get_data(as_text=True)
    assert 'notification color-danger' in html
    assert _shown(html, 'Max. players')
    assert _shown(html, 'Whole numbers from 1 only.')
    for response in (codec_problem, rule_problem):
        html = response.get_data(as_text=True)
        assert 'name="config_document"' not in html
        assert 'name="action" value="import"' not in html
        assert not _shown(
            html, 'Only the first %(count)s problems are shown.', count=20
        )
    assert _state(data_dir) == before


def test_import_check_valid_shows_summary_and_carries_document(
    client, config_party, data_dir
):
    raw = _document('Summary Cup', description='Some text')
    before = _state(data_dir)

    response = _check(client, raw)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _shown(
        html,
        'The file is valid. Check the settings, then create the tournament.',
    )
    assert 'notification color-danger' not in html
    assert '<dd>Summary Cup</dd>' in html
    assert _shown(html, 'Solo')
    assert '<dd>1v1</dd>' in html
    assert '<dd>16</dd>' in html
    assert '%(' not in html
    assert f'name="config_document" value="{_b64(raw)}"' in html
    assert re.search(
        r'name="submission_token"[^>]*value="[0-9a-f-]{36}"', html
    ) or re.search(r'value="[0-9a-f-]{36}"[^>]*name="submission_token"', html)
    assert 'name="action" value="import"' in html
    assert 'name="image"' in html
    assert 'name="image_alt_text"' in html
    assert _state(data_dir) == before


def test_import_summary_form_posts_to_step_import(client, config_party):
    path = f'/lan-tournaments/for_party/{PARTY_ID}/import'
    check = 'name="action" value="check"'
    create = 'name="action" value="import"'

    first = client.get(_import_url()).get_data(as_text=True)
    summary = _check(client, _document('Step Cup')).get_data(as_text=True)

    assert _form_actions(first, check) == [path]
    assert _form_actions(summary, create) == [f'{path}?step=import']
    assert _form_actions(summary, check) == []


def test_import_check_shows_only_the_first_20_problems(
    client, config_party, data_dir
):
    extra_keys = {f'extra_{number:02}': 1 for number in range(30)}
    raw = _envelope({'name': 'Many Cup', 'category': 'MAIN', **extra_keys})
    before = _state(data_dir)

    response = _check(client, raw)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    problems = html.split('notification color-danger', 1)[1].split('</ul>')[0]
    assert '%(' not in html
    assert problems.count('<li>') == 20
    assert 'extra_19' in problems
    assert 'extra_20' not in html
    assert _shown(
        html, 'Only the first %(count)s problems are shown.', count=20
    )
    assert _state(data_dir) == before


# -- commit --


def test_import_commit_from_carried_document_creates_draft_and_redirects(
    full_client, full_admin, config_party
):
    raw = _document('Committed Cup', description='Some text')
    token = str(uuid4())

    response = _commit(full_client, raw, token)

    assert response.status_code == 302
    created = _tournament_by_token(token)
    assert created is not None
    assert response.location.endswith(
        f'/lan-tournaments/tournaments/{created.id}'
    )
    assert created.party_id == config_party.id
    assert created.name == 'Committed Cup'
    assert created.description == 'Some text'
    assert created.tournament_status == TournamentStatus.DRAFT
    events = _log_events(created.id, 'tournament-config-imported')
    assert len(events) == 1
    initiator_id, data = events[0]
    assert str(initiator_id) == str(full_admin.id)
    assert data['document_sha256'] == sha256(raw).hexdigest()

    followed = full_client.get(response.location)
    assert followed.status_code == 200
    assert _shown(
        followed.get_data(as_text=True),
        'Tournament "%(name)s" has been imported as a draft.',
        name='Committed Cup',
    )


def test_import_commit_twice_with_the_same_token_creates_one(
    full_client, config_party, data_dir
):
    raw = _document('Twice Cup')
    token = str(uuid4())

    first = _commit(full_client, raw, token)
    after_first = _state(data_dir)
    second = _commit(full_client, raw, token)

    assert first.status_code == 302
    assert second.status_code == 302
    assert second.location == first.location
    assert _state(data_dir) == after_first
    assert (
        _count("SELECT count(*) FROM lan_tournaments WHERE name = 'Twice Cup'")
        == 1
    )

    followed = full_client.get(second.location)
    assert _shown(
        followed.get_data(as_text=True),
        'This tournament has already been created.',
    )


def test_import_tampered_carried_document_is_rechecked(
    client, config_party, data_dir
):
    tampered = _envelope({'category': 'MAIN', 'max_players': 0})
    token = str(uuid4())
    before = _state(data_dir)

    response = _commit(client, tampered, token)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _shown(html, 'Name')
    assert _shown(html, 'This key is missing.')
    assert 'name="config_document"' not in html
    assert _tournament_by_token(token) is None
    assert _state(data_dir) == before


def test_import_oversize_file_is_refused(client, config_party, data_dir):
    before = _state(data_dir)
    base = _document('Big Cup')
    just_over = base + b' ' * (_MAX_BYTES + 1 - len(base))
    far_over = base + b' ' * _MAX_BYTES

    responses = [
        _check(client, just_over),
        _check(client, far_over),
        _commit(client, just_over),
    ]
    carry_over_limit = client.post(
        _import_url(),
        data={
            'action': 'import',
            'config_document': 'A' * (4 * ((_MAX_BYTES + 3) // 3) + 4),
        },
        content_type='multipart/form-data',
    )

    assert len(just_over) == _MAX_BYTES + 1
    for response in responses:
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert _shown(html, 'The file is larger than %(max)s KiB.', max=256)
        assert 'name="config_document"' not in html
    assert carry_over_limit.status_code == 200
    assert _shown(
        carry_over_limit.get_data(as_text=True),
        'Please choose a configuration file.',
    )
    assert _state(data_dir) == before


def test_import_missing_file_or_bad_base64_asks_for_a_file(
    client, config_party, data_dir
):
    before = _state(data_dir)

    no_file = client.post(
        _import_url(),
        data={'action': 'check'},
        content_type='multipart/form-data',
    )
    bad_base64 = client.post(
        _import_url(),
        data={'action': 'import', 'config_document': 'not base64!'},
        content_type='multipart/form-data',
    )

    for response in (no_file, bad_base64):
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert _shown(html, 'Please choose a configuration file.')
        assert 'name="action" value="check"' in html
    assert _state(data_dir) == before


# -- image --


def test_import_commit_with_image_attaches_it(
    full_client, config_party, data_dir
):
    token = str(uuid4())

    response = _commit(
        full_client,
        _document('Image Cup'),
        token,
        image=(BytesIO(_png()), 'cover.png'),
        image_alt_text='  A blue cover  ',
    )

    assert response.status_code == 302
    created = _tournament_by_token(token)
    assert created is not None
    assert created.image_id is not None
    assert created.image_alt_text == 'A blue cover'
    assert list(data_dir.rglob(f'{created.image_id}.png'))


def test_import_unstorable_characters_are_readable_errors(
    client, config_party, data_dir
):
    raw = _document('\x00x', description='\ud800')
    before = _state(data_dir)

    for response in (_check(client, raw), _commit(client, raw)):
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert (
            _occurrences(
                html, tournament_config_document.UNSTORABLE_CHARACTER_ERROR
            )
            == 2
        )
        assert '\x00' not in html
        assert 'name="config_document"' not in html
    assert _state(data_dir) == before


def test_import_alt_text_over_200_is_refused_without_writes(
    full_client, config_party, data_dir
):
    token = str(uuid4())
    before = _state(data_dir)

    with patch.object(
        tournament_image_service, 'store_uploaded_image'
    ) as store:
        response = _commit(
            full_client,
            _document('Alt Cup'),
            token,
            image=(BytesIO(_png()), 'cover.png'),
            image_alt_text='a' * 201,
        )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('<ol class="form-errors">') == 1
    assert 'name="config_document"' in html
    store.assert_not_called()
    assert _tournament_by_token(token) is None
    assert _state(data_dir) == before


def test_import_alt_text_with_nul_is_refused(
    full_client, config_party, data_dir
):
    token = str(uuid4())
    before = _state(data_dir)

    with patch.object(
        tournament_image_service, 'store_uploaded_image'
    ) as store:
        response = _commit(
            full_client,
            _document('Nul Cup'),
            token,
            image=(BytesIO(_png()), 'cover.png'),
            image_alt_text='a\x00b',
        )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _shown(html, tournament_config_document.UNSTORABLE_CHARACTER_ERROR)
    store.assert_not_called()
    assert _tournament_by_token(token) is None
    assert _state(data_dir) == before


@pytest.mark.parametrize('extra', [{}, {'action': 'delete'}])
def test_import_missing_or_unknown_action_is_400(
    extra, full_client, config_party, data_dir
):
    token = str(uuid4())
    before = _state(data_dir)

    response = full_client.post(
        _import_url(),
        data={
            'config_document': _b64(_document('Action Cup')),
            'submission_token': token,
            **extra,
        },
        content_type='multipart/form-data',
    )

    assert response.status_code == 400
    assert _tournament_by_token(token) is None
    assert _state(data_dir) == before


@pytest.mark.parametrize('outcome', ['error', 'duplicate', 'raises'])
def test_import_cleans_up_image_when_create_fails(
    outcome, full_client, config_party, data_dir
):
    before = _state(data_dir)
    if outcome == 'error':
        failing = patch(
            f'{_C}.import_tournament_config',
            return_value=Err('Something went wrong.'),
        )
    elif outcome == 'duplicate':
        failing = patch(
            f'{_C}.import_tournament_config',
            return_value=Err(tournament_service.DUPLICATE_SUBMISSION_ERROR),
        )
    else:
        failing = patch(
            f'{_C}.import_tournament_config', side_effect=RuntimeError('boom')
        )
    store_spy = patch.object(
        tournament_image_service,
        'store_uploaded_image',
        wraps=tournament_image_service.store_uploaded_image,
    )

    with failing as import_mock, store_spy as store:
        if outcome == 'raises':
            with pytest.raises(RuntimeError, match='boom'):
                _commit(
                    full_client,
                    _document('Cleanup Cup'),
                    image=(BytesIO(_png()), 'cover.png'),
                )
        else:
            response = _commit(
                full_client,
                _document('Cleanup Cup'),
                image=(BytesIO(_png()), 'cover.png'),
            )
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert '%(' not in html
            assert 'name="config_document"' in html

    store.assert_called_once()
    import_mock.assert_called_once()
    assert import_mock.call_args.kwargs['image_id'] is not None
    assert _state(data_dir) == before


# -- request budget --


@pytest.mark.parametrize('action', ['check', 'import'])
def test_import_max_document_and_max_image_fit_the_budget(
    action, full_client, config_party, data_dir
):
    base = _document('Budget Cup')
    raw = base + b' ' * (_MAX_BYTES - len(base))
    image = _noise_png((1450, 1200))
    token = str(uuid4())
    assert len(raw) == _MAX_BYTES
    assert 5_000_000 < len(image) < tournament_image_service.MAX_UPLOAD_BYTES
    assert (
        len(_b64(raw)) + len(image) > tournament_image_service.MAX_REQUEST_BYTES
    )

    response = full_client.post(
        _import_url(),
        data={
            'action': action,
            'config_document': _b64(raw),
            'submission_token': token,
            'image': (BytesIO(image), 'big.png'),
        },
        content_type='multipart/form-data',
    )

    if action == 'check':
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert f'name="config_document" value="{_b64(raw)}"' in html
        assert '<dd>Budget Cup</dd>' in html
        assert _tournament_by_token(token) is None
    else:
        assert response.status_code == 302
        created = _tournament_by_token(token)
        assert created is not None
        assert created.image_id is not None


def test_import_over_budget_is_413_with_readable_page(
    full_client, config_party, data_dir
):
    body = b'\x00' * (tournament_config_service.IMPORT_MAX_REQUEST_BYTES + 1024)
    before = _state(data_dir)

    response = full_client.post(
        _import_url() + '?step=import',
        data={
            'action': 'import',
            'config_document': _b64(_document('Over Cup')),
            'image': (BytesIO(body), 'x.png'),
        },
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    html = response.get_data(as_text=True)
    problems = html.split('notification color-danger', 1)[1].split('</ul>')[0]
    assert problems.count('<li>') == 1
    assert _shown(problems, 'Tournament image')
    assert _image_too_large_shown(problems)
    assert not _shown(html, 'The file is larger than %(max)s KiB.', max=256)
    assert 'Request Entity Too Large' not in html
    assert _state(data_dir) == before


@pytest.mark.parametrize('query', ['', '?step=check', '?step=bogus'])
def test_import_over_budget_without_step_import_blames_the_document(
    query, full_client, config_party, data_dir
):
    body = b' ' * (tournament_config_service.IMPORT_MAX_REQUEST_BYTES + 1024)
    before = _state(data_dir)

    response = full_client.post(
        _import_url() + query,
        data={'action': 'check', 'config_file': (BytesIO(body), 'big.json')},
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    html = response.get_data(as_text=True)
    assert 'notification color-danger' in html
    assert _shown(html, 'The file is larger than %(max)s KiB.', max=256)
    assert not _image_too_large_shown(html)
    assert 'Request Entity Too Large' not in html
    assert _state(data_dir) == before


def test_check_and_refused_imports_write_no_image(
    full_client, config_party, data_dir
):
    def images():
        return _count('SELECT count(*) FROM lan_tournament_images'), (
            _image_files(data_dir)
        )

    def cover():
        return (BytesIO(_png()), 'cover.png')

    before = images()

    _check(
        full_client,
        _envelope({'name': 'No Image Cup', 'category': 'MAIN', 'bogus': 1}),
        image=cover(),
    )
    _check(full_client, _document('No Image Cup'), image=cover())
    refused = _commit(
        full_client,
        _document('No Image Cup'),
        image=cover(),
        image_alt_text='a' * 201,
    )
    assert refused.status_code == 200
    assert images() == before

    token = str(uuid4())
    first = _commit(full_client, _document('No Image Cup'), token)
    after_first = images()
    with patch.object(
        tournament_image_service, 'store_uploaded_image'
    ) as store:
        repeated = _commit(
            full_client, _document('No Image Cup'), token, image=cover()
        )

    assert first.status_code == 302
    assert repeated.status_code == 302
    store.assert_not_called()
    assert images() == after_first == before


# -- export --


def test_export_requires_view_permission(plain_client, tournament):
    logs = _count('SELECT count(*) FROM lan_tournament_log_entries')

    response = plain_client.post(_export_url(tournament.id))

    assert response.status_code == 403
    assert _count('SELECT count(*) FROM lan_tournament_log_entries') == logs


def test_export_get_is_405(viewer_client, tournament):
    assert viewer_client.get(_export_url(tournament.id)).status_code == 405


def test_export_unknown_tournament_is_404(viewer_client, config_party):
    assert viewer_client.post(_export_url(uuid4())).status_code == 404


def test_export_returns_json_attachment(viewer_client, viewer, tournament):
    logged = _log_events(tournament.id, 'tournament-config-exported')

    response = viewer_client.post(_export_url(tournament.id))

    assert response.status_code == 200
    match = re.fullmatch(
        r'attachment; filename="?(lan-tournament-[a-z0-9-]+-\d{8}\.json)"?',
        response.headers['Content-Disposition'],
    )
    assert match is not None
    assert match.group(1).isascii()
    assert response.mimetype == 'application/json'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    document = json.loads(response.get_data())
    assert document['format'] == tournament_config_document.FORMAT
    assert document['tournament']['name'] == tournament.name
    events = _log_events(tournament.id, 'tournament-config-exported')
    assert len(events) == len(logged) + 1
    assert str(events[-1][0]) == str(viewer.id)
    assert (
        events[-1][1]['document_sha256']
        == sha256(response.get_data()).hexdigest()
    )


# -- route table --


def test_config_routes_exist_only_in_admin(admin_app, site_app):
    def config_rules(app):
        return sorted(
            (rule.rule, sorted(rule.methods - {'HEAD', 'OPTIONS'}))
            for rule in app.url_map.iter_rules()
            if rule.endpoint.startswith('lan_tournament')
            and ('/import' in rule.rule or '/export' in rule.rule)
        )

    assert config_rules(admin_app) == [
        ('/lan-tournaments/for_party/<party_id>/import', ['GET']),
        ('/lan-tournaments/for_party/<party_id>/import', ['POST']),
        ('/lan-tournaments/tournaments/<tournament_id>/export', ['POST']),
    ]
    assert any(
        rule.endpoint.startswith('lan_tournament')
        for rule in site_app.url_map.iter_rules()
    )
    assert config_rules(site_app) == []


@pytest.mark.parametrize(
    ('client_name', 'route', 'status'),
    [
        ('plain_client', 'import_form', 403),
        ('plain_client', 'import_config', 403),
        ('plain_client', 'export_config', 403),
        ('viewer_client', 'import_form', 403),
        ('viewer_client', 'import_config', 403),
        ('viewer_client', 'export_config', 200),
        ('client', 'import_form', 200),
        ('client', 'import_config', 200),
        ('client', 'export_config', 403),
        ('full_client', 'import_form', 200),
        ('full_client', 'import_config', 200),
        ('full_client', 'export_config', 200),
    ],
)
def test_config_routes_enforce_their_permission(
    client_name, route, status, request, config_party, tournament
):
    client = request.getfixturevalue(client_name)

    if route == 'import_form':
        response = client.get(_import_url())
    elif route == 'import_config':
        response = client.post(
            _import_url(),
            data={'action': 'check'},
            content_type='multipart/form-data',
        )
    else:
        response = client.post(_export_url(tournament.id))

    assert response.status_code == status


# -- entry buttons --


def test_index_shows_import_button_only_with_create(
    full_client, viewer_client, config_party
):
    index_url = f'{BASE_URL}/for_party/{PARTY_ID}'
    import_href = f'href="/lan-tournaments/for_party/{PARTY_ID}/import"'

    with_create = full_client.get(index_url)
    without_create = viewer_client.get(index_url)

    assert with_create.status_code == 200
    html = with_create.get_data(as_text=True)
    assert import_href in html
    assert _shown(html, 'Import tournament')
    assert without_create.status_code == 200
    html = without_create.get_data(as_text=True)
    assert import_href not in html
    assert not _shown(html, 'Import tournament')


def _export_forms(html: str) -> list[tuple[str, str]]:
    """Return the opening tag and body of each export form on the page."""
    return [
        (f'<form{attributes}>', body)
        for attributes, body in re.findall(
            r'<form\b([^>]*)>(.*?)</form>', html, flags=re.DOTALL
        )
        if re.search(r'action="[^"]*/tournaments/[^"/]+/export"', attributes)
    ]


def test_view_shows_export_button(viewer_client, plain_client, tournament):
    view_url = f'{BASE_URL}/tournaments/{tournament.id}'
    export_path = f'/lan-tournaments/tournaments/{tournament.id}/export'

    response = viewer_client.get(view_url)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    forms = _export_forms(html)
    assert len(forms) == 1
    tag, body = forms[0]
    assert f'action="{export_path}"' in tag
    assert 'method="post"' in tag
    assert 'type="submit"' in body
    assert _shown(html, 'Export configuration')

    assert plain_client.get(view_url).status_code == 403


# -- text fields sent as file parts --


def _post_with_file_part(client, field: str, action: str, name: str):
    """Post a valid import in which `field` arrives as an uploaded file."""
    raw = _document(name)
    data: dict = {'action': action}
    if field != 'config_document':
        if action == 'check':
            data['config_file'] = (BytesIO(raw), 'config.json')
        else:
            data['config_document'] = _b64(raw)
            data['submission_token'] = str(uuid4())
    data[field] = (BytesIO(b'xx'), 'x.txt')
    return client.post(
        _import_url(), data=data, content_type='multipart/form-data'
    )


# fmt: off
@pytest.mark.parametrize(
    ('field', 'action', 'outcome'),
    [
        ('config_document', 'check',  'asks-for-file'),
        ('config_document', 'import', 'asks-for-file'),
        ('image_alt_text',  'check',  'summary'),
        ('image_alt_text',  'import', 'created'),
        ('submission_token', 'check',  'summary'),
        ('submission_token', 'import', 'created'),
    ],
)
# fmt: on
def test_import_text_field_sent_as_file_part_is_ignored(
    field, action, outcome, full_client, config_party, data_dir
):
    name = f'File Part {field} {action} Cup'
    before = _state(data_dir)

    response = _post_with_file_part(full_client, field, action, name)

    after = _state(data_dir)
    if outcome == 'created':
        assert response.status_code == 302
        assert after == (before[0] + 1, before[1] + 1, before[2], before[3])
        assert _committed(
            'SELECT image_id, image_alt_text FROM lan_tournaments'
            ' WHERE name = :name',
            name=name,
        ) == [(None, None)]
        return

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    if outcome == 'asks-for-file':
        assert _shown(html, 'Please choose a configuration file.')
    else:
        assert f'<dd>{name}</dd>' in html
        assert 'name="config_document"' in html
    assert after == before


@pytest.mark.parametrize('carried', [False, True])
def test_import_action_sent_as_file_part_is_400(
    carried, full_client, config_party, data_dir
):
    raw = _document('Action File Cup')
    data: dict = {'action': (BytesIO(b'import'), 'x.txt')}
    if carried:
        data['config_document'] = _b64(raw)
        data['submission_token'] = str(uuid4())
    else:
        data['config_file'] = (BytesIO(raw), 'config.json')
    before = _state(data_dir)

    response = full_client.post(
        _import_url(), data=data, content_type='multipart/form-data'
    )

    assert response.status_code == 400
    assert _state(data_dir) == before


def test_import_plain_text_in_a_file_field_is_ignored(
    full_client, config_party, data_dir
):
    before = _state(data_dir)

    no_file = full_client.post(
        _import_url(),
        data={'action': 'check', 'config_file': 'config.json'},
        content_type='multipart/form-data',
    )

    assert no_file.status_code == 200
    assert _shown(
        no_file.get_data(as_text=True), 'Please choose a configuration file.'
    )
    assert _state(data_dir) == before

    token = str(uuid4())
    created = _commit(
        full_client,
        _document('Text Fields Cup'),
        token,
        config_file='config.json',
        image='cover.png',
    )

    assert created.status_code == 302
    tournament = _tournament_by_token(token)
    assert tournament is not None
    assert tournament.name == 'Text Fields Cup'
    assert tournament.image_id is None
    assert _state(data_dir) == (
        before[0] + 1,
        before[1] + 1,
        before[2],
        before[3],
    )


def test_import_real_file_fields_are_still_read_from_the_files(
    full_client, config_party, data_dir
):
    token = str(uuid4())

    response = _commit(
        full_client,
        _document('Carried Cup'),
        token,
        config_file=(BytesIO(_document('Uploaded Cup')), 'config.json'),
        image=(BytesIO(_png()), 'cover.png'),
    )

    assert response.status_code == 302
    tournament = _tournament_by_token(token)
    assert tournament is not None
    assert tournament.name == 'Uploaded Cup'
    assert tournament.image_id is not None
    assert list(data_dir.rglob(f'{tournament.image_id}.png'))
