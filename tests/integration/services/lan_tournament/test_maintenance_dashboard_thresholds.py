from datetime import datetime, timedelta
import html as htmllib
import inspect
from io import BytesIO
import logging
from pathlib import Path
import re
import secrets
from types import SimpleNamespace
from uuid import uuid4

from babel.messages.extract import extract
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations
import flask_babel
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session as SqlaSession

from byceps.database import db
from byceps.services.lan_tournament import (
    dashboard_config,
    tournament_dashboard_settings_service as service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.blueprints.admin import (
    views as admin_views,
)
from byceps.services.lan_tournament.blueprints.dashboard_csrf import (
    CSRF_SESSION_KEY,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.uuid import uuid7

from tests.helpers import log_in_user


SUFFIX = uuid4().hex[:8]
PARTY_ID = PartyID(f'f03thr-{SUFFIX}')
OTHER_PARTY_ID = PartyID(f'f03thr-other-{SUFFIX}')
UNKNOWN_PARTY_ID = PartyID(f'f03thr-unknown-{SUFFIX}')

BASE = 'http://admin.acmecon.test/lan-tournaments'
TAB_URL = f'{BASE}/for_party/{PARTY_ID}/maintenance'
POST_URL = f'{TAB_URL}/dashboard-thresholds'


def _form_tag(party_id: PartyID) -> str:
    path = f'/lan-tournaments/for_party/{party_id}/maintenance'
    return f'<form method="post" action="{path}/dashboard-thresholds">'


LOGGER = 'byceps.services.lan_tournament.tournament_dashboard_settings_service'

# 22:30 UTC is 00:30 of the next day in the party's time zone.
NOW = datetime(2026, 10, 8, 22, 30, 15, 123456)
NOW_ISO = '2026-10-08T22:30:15.123456'
NOW_GERMAN_DATE = '09.10.2026'

DEFAULT_YELLOW = dashboard_config.DEFAULT_YELLOW_MINUTES
DEFAULT_RED = dashboard_config.DEFAULT_RED_MINUTES

TITLE = 'Orga-Dashboard: Warnschwellen'
SCOPE = 'Gilt für alle Turniere dieser Party.'
YELLOW_LABEL = 'Gelb ab (Minuten)'
RED_LABEL = 'Rot ab (Minuten)'
SAVE = 'Speichern'
RESET = 'Auf Standard zurücksetzen'
YELLOW_MIN = 'Gelb muss mindestens 1 Minute sein.'
RED_ORDER = 'Rot muss größer als Gelb sein.'
AT_MOST = 'Höchstens 1440 Minuten.'
STALE = 'Die Schwellen wurden inzwischen geändert. Bitte neu laden.'
SAVED = 'Die Warnschwellen wurden gespeichert.'
WAS_RESET = 'Die Warnschwellen wurden auf den Standard zurückgesetzt.'
CSRF_NOTICE = (
    'Die Formularprüfung ist abgelaufen. Bitte die Seite neu laden;'
    ' dein Entwurf bleibt sichtbar.'
)
WHOLE_YELLOW = (
    'Die Gelb-Schwelle muss eine ganze Zahl von Minuten zwischen 1 und 1440'
    ' sein.'
)
WHOLE_RED = (
    'Die Rot-Schwelle muss eine ganze Zahl von Minuten zwischen 1 und 1440'
    ' sein.'
)
INVALID_FORM = 'Ungültige Formulardaten.'


# -- fixtures --


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand(f'f03thr-brand-{SUFFIX}', 'F03 thresholds brand')
    return make_party(brand, PARTY_ID, 'F03 thresholds party')


@pytest.fixture(scope='module')
def other_party(make_party, make_brand):
    brand = make_brand(f'f03thr-other-brand-{SUFFIX}', 'F03 other brand')
    return make_party(brand, OTHER_PARTY_ID, 'F03 other thresholds party')


@pytest.fixture(scope='module')
def maintainer(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.maintain'},
        screen_name=f'F03thrMaint{SUFFIX}',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def second_maintainer(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.maintain'},
        screen_name=f'F03thrSecond{SUFFIX}',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def administrator(make_admin):
    """Hold the dashboard permission, but not the Wartung one."""
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate'},
        screen_name=f'F03thrAdmin{SUFFIX}',
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def client(make_client, admin_app, maintainer):
    return make_client(admin_app, user_id=maintainer.id)


@pytest.fixture(scope='module')
def second_client(make_client, admin_app, second_maintainer):
    return make_client(admin_app, user_id=second_maintainer.id)


@pytest.fixture(scope='module')
def administrator_client(make_client, admin_app, administrator):
    return make_client(admin_app, user_id=administrator.id)


@pytest.fixture
def anonymous_client(make_client, admin_app):
    return make_client(admin_app)


@pytest.fixture(scope='module')
def german_translations():
    po_path = (
        Path(__file__).parents[4]
        / 'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        catalog = read_po(f, locale='de')
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return Translations(fp=buffer)


@pytest.fixture(autouse=True)
def german(monkeypatch, german_translations):
    """Read the catalogue as it stands, not the compiled one on disk."""
    monkeypatch.setattr(
        flask_babel.Domain,
        'get_translations',
        lambda self: german_translations,
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Make `clock.now` the server time of every write."""
    state = SimpleNamespace(now=NOW)

    def tick():
        state.now += timedelta(seconds=1)

    state.tick = tick
    monkeypatch.setattr(repo, 'get_operation_time', lambda: state.now)
    return state


@pytest.fixture(autouse=True)
def _clean(admin_app, party, other_party):
    """Provide the app context, and leave no override behind."""
    _delete_overrides()
    yield
    db.session.rollback()
    _delete_overrides()


def _delete_overrides() -> None:
    db.session.execute(
        text(
            'DELETE FROM lan_tournament_dashboard_party_thresholds'
            ' WHERE party_id = ANY(:ids)'
        ),
        {'ids': [PARTY_ID, OTHER_PARTY_ID, UNKNOWN_PARTY_ID]},
    )
    db.session.commit()


@pytest.fixture
def spy(monkeypatch):
    """Record every call of the service writers, then run them."""
    calls = []

    def wrap(name):
        original = getattr(admin_views, name)

        def wrapper(*args, **kwargs):
            calls.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(admin_views, name, wrapper)

    wrap('set_party_thresholds')
    wrap('reset_party_thresholds')
    return calls


# -- helpers --


def _row(party_id=PARTY_ID) -> dict | None:
    """Read the stored override through a session of its own."""
    with SqlaSession(bind=db.engine) as session:
        row = (
            session.execute(
                text(
                    'SELECT yellow_minutes, red_minutes, revision,'
                    ' updated_at, updated_by'
                    ' FROM lan_tournament_dashboard_party_thresholds'
                    ' WHERE party_id = :party_id'
                ),
                {'party_id': party_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _seed(
    clock, user, *, yellow=20, red=60, party_id=PARTY_ID, expected=(0, None)
):
    """Store an override the way an earlier orga did."""
    clock.tick()
    result = service.set_party_thresholds(
        party_id,
        yellow_minutes=yellow,
        red_minutes=red,
        expected_revision=expected[0],
        expected_updated_at=expected[1],
        initiator_id=user.id,
    )
    assert result.is_ok(), result
    return result.unwrap()


def _hidden(page: str, name: str) -> str:
    match = re.search(
        rf'<input type="hidden" name="{name}" value="([^"]*)">', page
    )
    assert match, f'no hidden field {name}'
    return htmllib.unescape(match.group(1))


def _entered(page: str, name: str) -> str:
    match = re.search(
        rf'<input class="form-control"[^>]* name="{name}" value="([^"]*)"', page
    )
    assert match, f'no input {name}'
    return htmllib.unescape(match.group(1))


def _load(client, party_id=PARTY_ID) -> tuple[str, dict]:
    """Open the tab and read the card as a browser would submit it."""
    response = client.get(f'{BASE}/for_party/{party_id}/maintenance')
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    return page, _fields(page)


def _fields(page: str) -> dict:
    return {
        'csrf_token': _hidden(page, 'csrf_token'),
        'expected_revision': _hidden(page, 'expected_revision'),
        'expected_updated_at': _hidden(page, 'expected_updated_at'),
        'yellow_minutes': _entered(page, 'yellow_minutes'),
        'red_minutes': _entered(page, 'red_minutes'),
    }


def _submit(client, fields: dict, *, url=POST_URL, **overrides):
    """Post the card; a value of `None` leaves the field out."""
    data = {**fields, **overrides}
    return client.post(
        url, data={k: v for k, v in data.items() if v is not None}
    )


def _page(response) -> str:
    return response.get_data(as_text=True)


def _error_text(page: str, field: str) -> str | None:
    match = re.search(
        rf'<ol class="form-errors" id="lt-threshold-{field}-error">'
        r'<li><strong>[^<]*</strong> <span>([^<]*)</span>',
        page,
    )
    return htmllib.unescape(match.group(1)) if match else None


def _notice(page: str) -> tuple[str, str] | None:
    match = re.search(
        r'data-error-code="([^"]*)">\s*<ol class="form-errors"><li>'
        r'<strong>[^<]*</strong> <span>([^<]*)</span>',
        page,
    )
    return (match.group(1), htmllib.unescape(match.group(2))) if match else None


def _source(page: str) -> tuple[str, str]:
    match = re.search(
        r'<p class="finding" data-threshold-source="([^"]*)">([^<]*)</p>', page
    )
    assert match, 'no source line'
    return match.group(1), htmllib.unescape(match.group(2))


def _logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def _stored(party_id=PARTY_ID) -> tuple | None:
    row = _row(party_id)
    if row is None:
        return None
    return (row['yellow_minutes'], row['red_minutes'], row['revision'])


def _token_in_session(client, user) -> str:
    """Put a token of this user into the client's session and return it."""
    token = secrets.token_urlsafe(32)
    with client.session_transaction() as session:
        session[CSRF_SESSION_KEY] = {'user_id': str(user.id), 'token': token}
    return token


WRITERS = {'save': 'set_party_thresholds', 'reset': 'reset_party_thresholds'}


# -- the plan's tests --


@pytest.mark.parametrize('action', ['save', 'reset'])
def test_threshold_card_requires_maintain_and_csrf(
    action,
    clock,
    spy,
    client,
    second_client,
    administrator_client,
    administrator,
    anonymous_client,
    maintainer,
    second_maintainer,
):
    _seed(clock, maintainer, yellow=20, red=60)
    before = _row()
    page, fields = _load(client)
    fields = {
        **fields,
        'yellow_minutes': '30',
        'red_minutes': '90',
        'action': action,
    }

    # A valid form of someone without `lan_tournament.maintain`.
    own_token = _token_in_session(administrator_client, administrator)
    refused = _submit(administrator_client, fields, csrf_token=own_token)
    assert refused.status_code == 403
    assert _row() == before

    # An anonymous request.
    refused = _submit(anonymous_client, fields)
    assert refused.status_code == 403
    assert _row() == before

    # Neither got as far as the service.
    assert spy == []

    _, second_fields = _load(second_client)
    # fmt: off
    tokens = {
        'missing': None,
        'empty': '',
        'wrong': secrets.token_urlsafe(32),
        'short': 'x',
        'too long': secrets.token_urlsafe(200),
        'foreign': second_fields['csrf_token'],
        'twice': [fields['csrf_token'], fields['csrf_token']],
    }
    # fmt: on
    for label, token in tokens.items():
        refused = _submit(client, fields, csrf_token=token)

        assert refused.status_code == 403, label
        answer = _page(refused)
        assert _notice(answer) == ('csrf_invalid', CSRF_NOTICE), label
        assert 'Verwaiste Bilddateien' in answer, label
        assert _entered(answer, 'yellow_minutes') == '30', label
        assert _entered(answer, 'red_minutes') == '90', label
        assert _row() == before, label
        assert spy == [], label

    # The refused card carries a token that works, so the draft is not lost.
    fresh = _fields(_page(refused))
    assert fresh['csrf_token'] == fields['csrf_token']
    accepted = _submit(client, fresh, action=action)
    assert accepted.status_code == 303
    assert spy == [WRITERS[action]]
    assert _row() != before


def test_threshold_card_shows_effective_values_and_source(
    admin_app, monkeypatch, clock, client, maintainer, second_maintainer
):
    # No override: the deployment default, said as such.
    page, fields = _load(client)

    assert TITLE in page
    assert SCOPE in page
    assert _form_tag(PARTY_ID) in page
    assert YELLOW_LABEL in page
    assert RED_LABEL in page
    assert (fields['yellow_minutes'], fields['red_minutes']) == (
        str(DEFAULT_YELLOW),
        str(DEFAULT_RED),
    )
    assert _source(page) == (
        'deployment',
        f'Standard der Installation: {DEFAULT_YELLOW}/{DEFAULT_RED} min',
    )
    assert (fields['expected_revision'], fields['expected_updated_at']) == (
        '0',
        '',
    )
    assert (
        f'name="action" value="save" class="button color-primary">{SAVE}</button>'
        in page
    )
    assert 'value="reset"' not in page
    assert RESET not in page
    effective = service.get_effective_dashboard_settings(PARTY_ID).unwrap()
    assert (effective.yellow_minutes, effective.red_minutes) == (
        DEFAULT_YELLOW,
        DEFAULT_RED,
    )

    # The default is the installation's, not the module's constant.
    monkeypatch.setitem(
        admin_app.config, dashboard_config.YELLOW_MINUTES_KEY, 20
    )
    monkeypatch.setitem(admin_app.config, dashboard_config.RED_MINUTES_KEY, 90)
    page, fields = _load(client)
    assert (fields['yellow_minutes'], fields['red_minutes']) == ('20', '90')
    assert _source(page) == (
        'deployment',
        'Standard der Installation: 20/90 min',
    )

    # An override: its values, its orga and its date in the party's zone.
    _seed(clock, maintainer, yellow=25, red=70)
    page, fields = _load(client)

    assert (fields['yellow_minutes'], fields['red_minutes']) == ('25', '70')
    assert _source(page) == (
        'party',
        (
            f'Für diese Party gesetzt von {maintainer.screen_name}'
            f' am {NOW_GERMAN_DATE}'
        ),
    )
    assert fields['expected_revision'] == '1'
    assert fields['expected_updated_at'] == (
        (NOW + timedelta(seconds=1)).isoformat(timespec='microseconds')
    )
    assert f'>{RESET}</button>' in page
    assert page.index('name="action" value="save"') < page.index(
        'name="action" value="reset"'
    )
    effective = service.get_effective_dashboard_settings(PARTY_ID).unwrap()
    assert (effective.yellow_minutes, effective.red_minutes) == (25, 70)
    assert effective.threshold_source == 'party'

    # Another orga's later change shows that orga.
    _seed(
        clock,
        second_maintainer,
        yellow=30,
        red=80,
        expected=(1, NOW + timedelta(seconds=1)),
    )
    page, fields = _load(client)
    assert _source(page)[1].startswith(
        f'Für diese Party gesetzt von {second_maintainer.screen_name} am '
    )
    assert fields['expected_revision'] == '2'

    # The card belongs to the party of the URL.
    other_page, other_fields = _load(client, OTHER_PARTY_ID)
    assert _source(other_page)[0] == 'deployment'
    assert _form_tag(OTHER_PARTY_ID) in other_page
    assert other_fields['expected_revision'] == '0'

    # The cleanup actions stay where they were.
    assert 'Verwaiste Bilddateien' in page
    assert page.count('<article class="lt-maint-action') == 3


def test_threshold_save_and_reset_round_trip(
    caplog, clock, client, maintainer, second_maintainer
):
    _seed(clock, second_maintainer, yellow=11, red=22, party_id=OTHER_PARTY_ID)
    other = _row(OTHER_PARTY_ID)
    caplog.set_level(logging.INFO, logger=LOGGER)
    caplog.clear()

    page, fields = _load(client)
    assert _stored() is None

    # Save, with whitespace and leading zeros as a person types them.
    clock.tick()
    saved = _submit(
        client,
        fields,
        yellow_minutes=' 030 ',
        red_minutes='0090',
        action='save',
    )

    assert saved.status_code == 303
    assert (
        saved.location == f'/lan-tournaments/for_party/{PARTY_ID}/maintenance'
    )
    assert _stored() == (30, 90, 1)
    row = _row()
    assert row['updated_by'] == maintainer.id
    assert row['updated_at'] == clock.now
    (message,) = _logged(caplog)
    assert f'{PARTY_ID!r} set by {maintainer.id}' in message

    page, fields = _load(client)
    assert SAVED in page
    assert (fields['yellow_minutes'], fields['red_minutes']) == ('30', '90')
    assert _source(page)[0] == 'party'
    effective = service.get_effective_dashboard_settings(PARTY_ID).unwrap()
    assert (effective.yellow_minutes, effective.red_minutes) == (30, 90)

    # Save again from the rendered form.
    clock.tick()
    saved = _submit(
        client, fields, yellow_minutes='5', red_minutes='10', action='save'
    )

    assert saved.status_code == 303
    assert _stored() == (5, 10, 2)

    # Reset from the rendered form.
    page, fields = _load(client)
    clock.tick()
    reset = _submit(client, fields, action='reset')

    assert reset.status_code == 303
    assert reset.location == saved.location
    assert _stored() is None
    assert f'{PARTY_ID!r} reset by {maintainer.id}' in _logged(caplog)[-1]
    page, fields = _load(client)
    assert WAS_RESET in page
    assert (fields['yellow_minutes'], fields['red_minutes']) == (
        str(DEFAULT_YELLOW),
        str(DEFAULT_RED),
    )
    assert _source(page)[0] == 'deployment'
    assert RESET not in page
    effective = service.get_effective_dashboard_settings(PARTY_ID).unwrap()
    assert (effective.yellow_minutes, effective.red_minutes) == (
        DEFAULT_YELLOW,
        DEFAULT_RED,
    )
    assert effective.threshold_source == 'deployment'

    # Another party's override was never touched.
    assert _row(OTHER_PARTY_ID) == other


def test_threshold_card_rejects_invalid_and_stale_without_change(
    clock, client, second_client, maintainer, second_maintainer
):
    _seed(clock, maintainer, yellow=20, red=60)
    before = _row()
    page, fields = _load(client)

    # A field error keeps the draft and the version, and changes nothing.
    refused = _submit(
        client, fields, yellow_minutes='0', red_minutes='60', action='save'
    )

    assert refused.status_code == 422
    answer = _page(refused)
    assert _error_text(answer, 'yellow') == YELLOW_MIN
    assert _error_text(answer, 'red') is None
    assert 'aria-invalid="true"' in answer
    assert _entered(answer, 'yellow_minutes') == '0'
    assert _entered(answer, 'red_minutes') == '60'
    assert _hidden(answer, 'expected_revision') == fields['expected_revision']
    assert (
        _hidden(answer, 'expected_updated_at') == fields['expected_updated_at']
    )
    assert _row() == before

    # The refused form can be corrected and sent again.
    clock.tick()
    corrected = _submit(
        client,
        _fields(answer),
        yellow_minutes='25',
        red_minutes='60',
        action='save',
    )
    assert corrected.status_code == 303
    assert _stored() == (25, 60, 2)

    # Stale: another orga saved after this form was loaded.
    page, fields = _load(client)
    clock.tick()
    _, theirs = _load(second_client)
    saved = _submit(
        second_client,
        theirs,
        yellow_minutes='35',
        red_minutes='80',
        action='save',
    )
    assert saved.status_code == 303
    after_theirs = _row()
    assert (after_theirs['yellow_minutes'], after_theirs['red_minutes']) == (
        35,
        80,
    )

    stale = _submit(
        client, fields, yellow_minutes='40', red_minutes='90', action='save'
    )

    assert stale.status_code == 409
    answer = _page(stale)
    assert _notice(answer) == ('stale', STALE)
    assert _entered(answer, 'yellow_minutes') == '40'
    assert _entered(answer, 'red_minutes') == '90'
    assert _error_text(answer, 'yellow') is None
    assert _source(answer)[1].startswith(
        f'Für diese Party gesetzt von {second_maintainer.screen_name} am '
    )
    assert _row() == after_theirs

    # Sending the refused form again stays refused until the page is
    # loaded again: nobody overwrites what they have not seen.
    again = _submit(client, _fields(answer), action='save')
    assert again.status_code == 409
    assert _row() == after_theirs

    page, fresh = _load(client)
    assert (fresh['yellow_minutes'], fresh['red_minutes']) == ('35', '80')
    assert _submit(client, fresh, action='save').status_code == 303
    assert _stored() == (35, 80, 4)


# -- more: what the card refuses --


# fmt: off
FIELD_ERRORS = [
    ('0', '45', {'yellow': YELLOW_MIN}),
    ('0', '0', {'yellow': YELLOW_MIN}),
    ('15', '0', {'red': RED_ORDER}),
    ('45', '45', {'red': RED_ORDER}),
    ('60', '30', {'red': RED_ORDER}),
    ('1440', '1440', {'red': RED_ORDER}),
    ('1441', '1442', {'yellow': AT_MOST}),
    ('15', '1441', {'red': AT_MOST}),
    ('15', '99999', {'red': AT_MOST}),
    ('abc', '45', {'yellow': WHOLE_YELLOW}),
    ('15', '', {'red': WHOLE_RED}),
    ('   ', '45', {'yellow': WHOLE_YELLOW}),
    ('x', 'y', {'yellow': WHOLE_YELLOW, 'red': WHOLE_RED}),
    ('-5', '45', {'yellow': WHOLE_YELLOW}),
    ('+5', '45', {'yellow': WHOLE_YELLOW}),
    ('1.5', '45', {'yellow': WHOLE_YELLOW}),
    ('1e2', '45', {'yellow': WHOLE_YELLOW}),
    ('15 min', '45', {'yellow': WHOLE_YELLOW}),
    ('１５', '45', {'yellow': WHOLE_YELLOW}),
    ('15', '٤٥', {'red': WHOLE_RED}),
    ('15', '1' * 10, {'red': WHOLE_RED}),
    ('15', '9' * 5000, {'red': WHOLE_RED}),
]
# fmt: on


@pytest.mark.parametrize(('yellow', 'red', 'expected'), FIELD_ERRORS)
def test_threshold_card_field_errors_use_the_design_copy(
    yellow, red, expected, client
):
    page, fields = _load(client)

    refused = _submit(
        client, fields, yellow_minutes=yellow, red_minutes=red, action='save'
    )

    assert refused.status_code == 422
    answer = _page(refused)
    assert {
        field: _error_text(answer, field)
        for field in ('yellow', 'red')
        if _error_text(answer, field)
    } == expected
    assert answer.count('aria-invalid="true"') == len(expected)
    assert _entered(answer, 'yellow_minutes') == yellow[:64]
    assert _entered(answer, 'red_minutes') == red[:64]
    assert _notice(answer) is None
    assert _stored() is None
    assert '9' * 100 not in answer


FULLWIDTH = {ord(d): ord(d) + 0xFEE0 for d in '0123456789'}

# fmt: off
UNUSABLE_VERSIONS = [
    ('revision missing', {'expected_revision': None}),
    ('revision empty', {'expected_revision': ''}),
    ('revision letters', {'expected_revision': 'abc'}),
    ('revision negative', {'expected_revision': '-1'}),
    ('revision float', {'expected_revision': '1.0'}),
    ('revision signed', {'expected_revision': '+1'}),
    ('revision beyond the column', {'expected_revision': '2147483648'}),
    ('revision giant', {'expected_revision': '9' * 500}),
    ('revision other digits', {'expected_revision': '١'}),
    ('revision twice', {'expected_revision': ['1', '1']}),
    ('revision of a new row', {'expected_revision': '0'}),
    ('revision ahead', {'expected_revision': '2'}),
    ('time missing', {'expected_updated_at': None}),
    ('time empty', {'expected_updated_at': ''}),
    ('time garbage', {'expected_updated_at': 'garbage'}),
    ('time date only', {'expected_updated_at': '2026-10-08'}),
    ('time without fraction', {'expected_updated_at': '2026-10-08T22:30:16'}),
    ('time short fraction', {'expected_updated_at': '2026-10-08T22:30:16.12345'}),
    ('time month 13', {'expected_updated_at': '2026-13-08T22:30:16.123456'}),
    ('time zulu', {'expected_updated_at': '2026-10-08T22:30:16.123456Z'}),
    ('time aware', {'expected_updated_at': '2026-10-08T22:30:16.123456+00:00'}),
    ('time extreme offset', {'expected_updated_at': '0001-01-01T00:00:00.000000+05:00'}),
    ('time giant', {'expected_updated_at': 'x' * 10_000}),
    ('time twice', {'expected_updated_at': [NOW_ISO, NOW_ISO]}),
    ('time of another moment', {'expected_updated_at': '2026-10-08T22:30:16.000000'}),
    ('time unpadded', {'expected_updated_at': '2026-10-8T22:30:16.123456'}),
    ('time other digits', {'expected_updated_at': '2026-10-08T22:30:16.123456'.translate(FULLWIDTH)}),
]
# fmt: on


@pytest.mark.parametrize('action', ['save', 'reset'])
@pytest.mark.parametrize(('label', 'change'), UNUSABLE_VERSIONS)
def test_threshold_card_treats_unusable_versions_as_stale(
    label, change, action, clock, client, maintainer
):
    _seed(clock, maintainer, yellow=20, red=60)
    before = _row()
    page, fields = _load(client)
    assert fields['expected_updated_at'] == '2026-10-08T22:30:16.123456'

    stale = _submit(
        client,
        fields,
        yellow_minutes='30',
        red_minutes='90',
        action=action,
        **change,
    )

    assert stale.status_code == 409
    answer = _page(stale)
    assert _notice(answer) == ('stale', STALE)
    assert 'x' * 100 not in answer
    assert _row() == before


# fmt: off
UNUSABLE_NEW_VERSIONS = [
    ('revision missing', {'expected_revision': None}),
    ('revision letters', {'expected_revision': 'abc'}),
    ('revision negative', {'expected_revision': '-1'}),
    ('revision giant', {'expected_revision': '9' * 500}),
    ('revision twice', {'expected_revision': ['0', '0']}),
    ('revision of a row', {'expected_revision': '1'}),
    ('time missing', {'expected_updated_at': None}),
    ('time garbage', {'expected_updated_at': 'garbage'}),
    ('time of a row', {'expected_updated_at': NOW_ISO}),
    ('time twice', {'expected_updated_at': ['', '']}),
]
# fmt: on


@pytest.mark.parametrize(('label', 'change'), UNUSABLE_NEW_VERSIONS)
def test_threshold_card_creates_no_row_from_an_unusable_version(
    label, change, client
):
    page, fields = _load(client)
    assert (fields['expected_revision'], fields['expected_updated_at']) == (
        '0',
        '',
    )

    stale = _submit(
        client,
        fields,
        yellow_minutes='30',
        red_minutes='90',
        action='save',
        **change,
    )

    assert stale.status_code == 409
    assert _notice(_page(stale)) == ('stale', STALE)
    assert _row() is None


def test_threshold_card_round_trips_a_time_without_microseconds(
    clock, client, maintainer
):
    clock.now = datetime(2026, 10, 8, 22, 30, 15)
    result = service.set_party_thresholds(
        PARTY_ID,
        yellow_minutes=20,
        red_minutes=60,
        expected_revision=0,
        expected_updated_at=None,
        initiator_id=maintainer.id,
    )
    assert result.is_ok()
    page, fields = _load(client)
    assert fields['expected_updated_at'] == '2026-10-08T22:30:15.000000'

    clock.tick()
    saved = _submit(
        client, fields, yellow_minutes='25', red_minutes='70', action='save'
    )

    assert saved.status_code == 303
    assert _stored() == (25, 70, 2)


def test_threshold_card_refuses_a_stale_reset_and_a_reset_and_resave(
    clock, client, second_client, maintainer, second_maintainer
):
    _seed(clock, maintainer, yellow=20, red=60)
    page, old = _load(client)
    _, theirs = _load(second_client)

    # Another orga resets, and a third save follows: the row is revision 1
    # again, but it is not the row the old form was rendered from.
    clock.tick()
    assert _submit(second_client, theirs, action='reset').status_code == 303
    assert _stored() is None
    clock.tick()
    _, fresh = _load(second_client)
    assert (
        _submit(
            second_client,
            fresh,
            yellow_minutes='45',
            red_minutes='100',
            action='save',
        ).status_code
        == 303
    )
    assert _stored() == (45, 100, 1)
    before = _row()
    # The ABA set-up: the same revision, another row.
    assert before['revision'] == int(old['expected_revision']) == 1
    assert before['updated_at'] != datetime.fromisoformat(
        old['expected_updated_at']
    )

    for action in ('save', 'reset'):
        refused = _submit(
            client, old, yellow_minutes='25', red_minutes='70', action=action
        )
        assert refused.status_code == 409, action
        assert _notice(_page(refused)) == ('stale', STALE)
        assert _row() == before

    # Two forms of a party without an override: only the first one creates.
    _delete_overrides()
    _, first = _load(client)
    _, second = _load(second_client)
    assert first['expected_revision'] == '0'
    assert (
        _submit(
            client, first, yellow_minutes='10', red_minutes='20', action='save'
        ).status_code
        == 303
    )
    created = _row()
    refused = _submit(
        second_client,
        second,
        yellow_minutes='30',
        red_minutes='40',
        action='save',
    )
    assert refused.status_code == 409
    assert _row() == created

    # A reset of a row that is gone is stale, too.
    _delete_overrides()
    refused = _submit(client, old, action='reset')
    assert refused.status_code == 409
    assert _notice(_page(refused)) == ('stale', STALE)
    assert _row() is None


# fmt: off
UNKNOWN_ACTIONS = [
    ('missing', None),
    ('empty', ''),
    ('other word', 'delete'),
    ('upper case', 'SAVE'),
    ('twice', ['save', 'reset']),
]
# fmt: on


@pytest.mark.parametrize(('label', 'action'), UNKNOWN_ACTIONS)
def test_threshold_card_refuses_an_unknown_action(
    label, action, clock, client, maintainer, spy
):
    _seed(clock, maintainer, yellow=20, red=60)
    before = _row()
    page, fields = _load(client)

    refused = _submit(
        client, fields, yellow_minutes='30', red_minutes='90', action=action
    )

    assert refused.status_code == 422
    answer = _page(refused)
    assert _notice(answer) == ('invalid', INVALID_FORM)
    assert _entered(answer, 'yellow_minutes') == '30'
    assert _row() == before
    assert spy == []


def test_threshold_card_names_a_deleted_orga(clock, client):
    gone = SimpleNamespace(id=UserID(uuid7()))
    _seed(clock, gone)

    page, _ = _load(client)

    assert _source(page)[1] == (
        f'Für diese Party gesetzt von Gelöschte Orga am {NOW_GERMAN_DATE}'
    )


def test_threshold_card_is_unavailable_with_a_broken_deployment_configuration(
    admin_app, monkeypatch, clock, client, maintainer
):
    _seed(clock, maintainer)
    page, fields = _load(client)
    monkeypatch.setitem(admin_app.config, dashboard_config.RED_MINUTES_KEY, 'x')

    broken = client.get(TAB_URL)

    assert broken.status_code == 200
    answer = _page(broken)
    assert TITLE in answer
    assert WHOLE_RED in answer
    assert '/maintenance/dashboard-thresholds' not in answer
    assert 'name="yellow_minutes"' not in answer
    assert 'Verwaiste Bilddateien' in answer

    refused = _submit(client, fields, csrf_token='wrong', action='save')
    assert refused.status_code == 403
    assert WHOLE_RED in _page(refused)
    assert 'Verwaiste Bilddateien' in _page(refused)


def test_threshold_card_binds_the_party_of_the_url(
    clock, client, maintainer, spy
):
    page, fields = _load(client)

    unknown = _submit(
        client,
        fields,
        url=f'{BASE}/for_party/{UNKNOWN_PARTY_ID}/maintenance/dashboard-thresholds',
        yellow_minutes='30',
        red_minutes='90',
        action='save',
    )

    assert unknown.status_code == 404
    assert _row(UNKNOWN_PARTY_ID) is None
    assert spy == []

    # The same form posted for another party stores it for that party only.
    clock.tick()
    other = _submit(
        client,
        fields,
        url=f'{BASE}/for_party/{OTHER_PARTY_ID}/maintenance/dashboard-thresholds',
        yellow_minutes='30',
        red_minutes='90',
        action='save',
    )
    assert other.status_code == 303
    assert other.location.endswith(f'/for_party/{OTHER_PARTY_ID}/maintenance')
    assert _stored(OTHER_PARTY_ID) == (30, 90, 1)
    assert _stored(PARTY_ID) is None

    # A party in the form is not read: the URL's party is the one.
    clock.tick()
    forged = _submit(
        client,
        fields,
        yellow_minutes='40',
        red_minutes='95',
        action='save',
        party_id=OTHER_PARTY_ID,
    )
    assert forged.status_code == 303
    assert _stored(PARTY_ID) == (40, 95, 1)
    assert _stored(OTHER_PARTY_ID) == (30, 90, 1)


def test_the_threshold_route_is_registered_once(admin_app):
    rules = [
        (rule.rule, tuple(sorted(rule.methods - {'HEAD', 'OPTIONS'})))
        for rule in admin_app.url_map.iter_rules()
        if rule.endpoint == 'lan_tournament_admin.update_dashboard_thresholds'
    ]

    path = '/lan-tournaments/for_party/<party_id>/maintenance'
    assert rules == [(f'{path}/dashboard-thresholds', ('POST',))]


# -- the German copy --


def _catalog():
    po_path = (
        Path(__file__).parents[4]
        / 'byceps/translations/de/LC_MESSAGES/messages.po'
    )
    with po_path.open('rb') as f:
        return read_po(f, locale='de')


# fmt: off
DESIGN_COPY = {
    'Orga dashboard: warning thresholds': TITLE,
    'Applies to all tournaments of this party.': SCOPE,
    'Yellow from (minutes)': YELLOW_LABEL,
    'Red from (minutes)': RED_LABEL,
    'Installation default: %(yellow)d/%(red)d min': (
        'Standard der Installation: %(yellow)d/%(red)d min'
    ),
    'Set for this party by %(actor)s on %(date)s': (
        'Für diese Party gesetzt von %(actor)s am %(date)s'
    ),
    'Save': SAVE,
    'Reset to default': RESET,
    'Yellow must be at least 1 minute.': YELLOW_MIN,
    'Red must be greater than yellow.': RED_ORDER,
    'At most 1440 minutes.': AT_MOST,
    'The thresholds were changed in the meantime. Please reload.': STALE,
}
# fmt: on


def test_threshold_card_copy_is_in_the_catalogue_verbatim():
    catalog = _catalog()

    assert {
        msgid: catalog.get(msgid).string for msgid in DESIGN_COPY
    } == DESIGN_COPY


def test_threshold_card_strings_have_german():
    catalog = _catalog()

    def german(msgid: str) -> bool:
        message = catalog.get(msgid)
        return (
            message is not None
            and bool(message.string)
            and not message.fuzzy
            and message.string != msgid
        )

    functions = (
        admin_views.update_dashboard_thresholds,
        admin_views._thresholds_stale_page,
        admin_views._thresholds_refusal,
        admin_views._threshold_field_errors,
        admin_views._dashboard_thresholds_card,
    )
    msgids = set()
    for function in functions:
        source = inspect.getsource(inspect.unwrap(function))
        msgids |= {
            message if isinstance(message, str) else message[0]
            for _, message, _, _ in extract(
                'python', BytesIO(source.encode('utf-8'))
            )
        }

    assert 'The dashboard thresholds were saved.' in msgids
    assert 'Set for this party by %(actor)s on %(date)s' in msgids
    assert [msgid for msgid in sorted(msgids) if not german(msgid)] == []
    codes = [
        dashboard_config.INVALID_YELLOW_MINUTES_ERROR,
        dashboard_config.INVALID_RED_MINUTES_ERROR,
        dashboard_config.INVALID_THRESHOLD_ORDER_ERROR,
        service.THRESHOLDS_STALE_ERROR,
    ]
    assert [code for code in codes if not german(code)] == []
