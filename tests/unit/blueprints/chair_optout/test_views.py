"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import csv
from io import StringIO
from types import SimpleNamespace

import pytest
from flask import g
from werkzeug.exceptions import Forbidden

from byceps.services.chair_optout.blueprints.admin import views as admin_views
from byceps.services.chair_optout.blueprints.site import views as site_views
from byceps.config.models import AppMode
from byceps.services.chair_optout.models import ChairOptoutReportEntry
from byceps.util import views as util_views

from tests.helpers import generate_uuid


def _make_ticket(ticket_id, code, *, has_seat=True):
    occupied_seat = SimpleNamespace(label='A-1') if has_seat else None
    return SimpleNamespace(
        id=ticket_id,
        code=code,
        occupied_seat=occupied_seat,
    )


def test_site_index_requires_login(app, monkeypatch):
    with app.test_request_context('/'):
        g.user = SimpleNamespace(authenticated=False)
        g.app_mode = AppMode.site

        monkeypatch.setattr(
            util_views, 'flash_notice', lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            util_views, 'redirect_to', lambda *args, **kwargs: 'redirected'
        )

        assert site_views.index() == 'redirected'


def test_admin_index_requires_permission(app):
    with app.test_request_context('/'):
        g.user = SimpleNamespace(permissions=frozenset())

        with pytest.raises(Forbidden):
            admin_views.index('party-1')


def test_admin_export_requires_permission(app):
    with app.test_request_context('/'):
        g.user = SimpleNamespace(permissions=frozenset())

        with pytest.raises(Forbidden):
            admin_views.export_as_csv('party-1')


def test_admin_export_csv_smoke(app, monkeypatch):
    with app.test_request_context('/'):
        g.user = SimpleNamespace(
            permissions=frozenset(['chair_optout.view'])
        )

        party = SimpleNamespace(id='party-1')
        report_entries = [
            ChairOptoutReportEntry(
                full_name='Alice Example',
                screen_name='alice',
                ticket_code='T-100',
                seat_label='A-1',
                has_seat=True,
            ),
            ChairOptoutReportEntry(
                full_name=None,
                screen_name=None,
                ticket_code='T-101',
                seat_label=None,
                has_seat=False,
            ),
        ]

        monkeypatch.setattr(
            admin_views.party_service, 'find_party', lambda *_: party
        )
        monkeypatch.setattr(
            admin_views.chair_optout_service,
            'get_report_entries_for_party',
            lambda *_: report_entries,
        )

        response = admin_views.export_as_csv('party-1')

        csv_rows = list(csv.reader(StringIO(response.get_data(as_text=True))))

        assert csv_rows[0] == [
            'Name',
            'Nickname',
            'Ticketnummer',
            'Sitzplatz-Label',
        ]
        assert csv_rows[1] == ['Alice Example', 'alice', 'T-100', 'A-1']
        assert csv_rows[2] == ['', '', 'T-101', '']


def test_site_update_rejects_malformed_uuid(app, monkeypatch):
    app.config['LOCALE'] = 'de'
    bad_ticket_id = 'not-a-uuid'
    ticket = _make_ticket(bad_ticket_id, 'T-1')
    user_id = generate_uuid()

    captured = {}

    def fake_index(erroneous_form=None):
        captured['form'] = erroneous_form
        return 'index-result'

    with app.test_request_context(
        '/', method='POST', data={'ticket_ids': [bad_ticket_id]}
    ):
        g.user = SimpleNamespace(id=user_id, authenticated=True)
        g.party = SimpleNamespace(id='party-1')

        monkeypatch.setattr(site_views, 'index', fake_index)
        monkeypatch.setattr(
            site_views.ticket_service,
            'get_tickets_used_by_user',
            lambda *_: [ticket],
        )
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'list_optouts_for_party',
            lambda *args, **kwargs: [],
        )
        set_optout_calls = []
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'set_optout',
            lambda *args, **kwargs: set_optout_calls.append((args, kwargs)),
        )

        result = site_views.update()

    assert result == 'index-result'
    assert set_optout_calls == []
    assert 'Ungültige Ticket-ID.' in captured['form'].ticket_ids.errors


def test_site_update_rejects_unowned_ticket_id(app, monkeypatch):
    app.config['LOCALE'] = 'de'
    allowed_ticket_id = generate_uuid()
    unowned_ticket_id = generate_uuid()
    ticket = _make_ticket(allowed_ticket_id, 'T-1')
    user_id = generate_uuid()

    captured = {}

    def fake_index(erroneous_form=None):
        captured['form'] = erroneous_form
        return 'index-result'

    def fake_ticket_choices(_tickets):
        return [
            (str(allowed_ticket_id), 'T-1'),
            (str(unowned_ticket_id), 'T-999'),
        ]

    with app.test_request_context(
        '/',
        method='POST',
        data={
            'ticket_ids': [
                str(allowed_ticket_id),
                str(unowned_ticket_id),
            ]
        },
    ):
        g.user = SimpleNamespace(id=user_id, authenticated=True)
        g.party = SimpleNamespace(id='party-1')

        monkeypatch.setattr(site_views, 'index', fake_index)
        monkeypatch.setattr(
            site_views, '_get_ticket_id_choices', fake_ticket_choices
        )
        monkeypatch.setattr(
            site_views.ticket_service,
            'get_tickets_used_by_user',
            lambda *_: [ticket],
        )
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'list_optouts_for_party',
            lambda *args, **kwargs: [],
        )
        set_optout_calls = []
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'set_optout',
            lambda *args, **kwargs: set_optout_calls.append((args, kwargs)),
        )

        result = site_views.update()

    assert result == 'index-result'
    assert set_optout_calls == []
    assert 'Ungültige Ticket-ID.' in captured['form'].ticket_ids.errors


def test_site_update_ignores_ticket_without_seat(app, monkeypatch):
    app.config['LOCALE'] = 'de'
    ticket_id = generate_uuid()
    ticket = _make_ticket(ticket_id, 'T-1', has_seat=False)
    user_id = generate_uuid()

    with app.test_request_context(
        '/', method='POST', data={'ticket_ids': [str(ticket_id)]}
    ):
        g.user = SimpleNamespace(id=user_id, authenticated=True)
        g.party = SimpleNamespace(id='party-1')

        monkeypatch.setattr(
            site_views.ticket_service,
            'get_tickets_used_by_user',
            lambda *_: [ticket],
        )
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'list_optouts_for_party',
            lambda *args, **kwargs: [],
        )
        set_optout_calls = []
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'set_optout',
            lambda *args, **kwargs: set_optout_calls.append((args, kwargs)),
        )
        notices = []
        monkeypatch.setattr(
            site_views,
            'flash_notice',
            lambda message, **kwargs: notices.append(message),
        )
        monkeypatch.setattr(
            site_views, 'flash_success', lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            site_views, 'redirect_to', lambda *args, **kwargs: 'redirected'
        )

        result = site_views.update()

    assert result == 'redirected'
    assert set_optout_calls == []
    assert notices == ['Keine Sitzplätze vorhanden - nichts zu speichern.']


def test_site_update_happy_path(app, monkeypatch):
    app.config['LOCALE'] = 'de'
    ticket_id = generate_uuid()
    ticket = _make_ticket(ticket_id, 'T-1')
    user_id = generate_uuid()

    with app.test_request_context(
        '/', method='POST', data={'ticket_ids': [str(ticket_id)]}
    ):
        g.user = SimpleNamespace(id=user_id, authenticated=True)
        g.party = SimpleNamespace(id='party-1')

        monkeypatch.setattr(
            site_views.ticket_service,
            'get_tickets_used_by_user',
            lambda *_: [ticket],
        )
        monkeypatch.setattr(
            site_views.chair_optout_service,
            'list_optouts_for_party',
            lambda *args, **kwargs: [],
        )
        set_optout_calls = []

        def fake_set_optout(party_id, ticket_id, user_id, brings_own_chair):
            set_optout_calls.append(
                (party_id, ticket_id, user_id, brings_own_chair)
            )

        monkeypatch.setattr(
            site_views.chair_optout_service, 'set_optout', fake_set_optout
        )
        flashes = []
        monkeypatch.setattr(
            site_views,
            'flash_success',
            lambda message, **kwargs: flashes.append(message),
        )
        monkeypatch.setattr(
            site_views, 'flash_notice', lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            site_views, 'redirect_to', lambda *args, **kwargs: 'redirected'
        )

        result = site_views.update()

    assert result == 'redirected'
    assert set_optout_calls == [
        ('party-1', ticket_id, user_id, True)
    ]
    assert flashes == ['Änderungen gespeichert.']
