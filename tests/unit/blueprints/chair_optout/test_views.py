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
            permissions=frozenset(['chair_optout.export_report'])
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
