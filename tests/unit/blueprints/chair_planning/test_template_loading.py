"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from pathlib import Path

from flask import Blueprint, Flask
from jinja2 import StrictUndefined
import pytest

from byceps.application import _init_site_app
from byceps.blueprints import site as site_registration
from byceps.services.site.models import SiteID
from byceps.util import templatefilters, templating
from byceps.util.framework import blueprint as blueprint_util


ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_NAME = 'site/ticketing/index_mine.html'
CHAIR_PACKAGE = 'services.chair_planning.blueprints.site'
TICKETING_PACKAGE = 'services.ticketing.blueprints.site'
CHAIR_FILE = (
    ROOT
    / 'byceps'
    / CHAIR_PACKAGE.replace('.', '/')
    / 'templates'
    / TEMPLATE_NAME
)
STOCK_FILE = (
    ROOT
    / 'byceps'
    / TICKETING_PACKAGE.replace('.', '/')
    / 'templates'
    / TEMPLATE_NAME
)
GV36_FILE = ROOT / 'sites/totalverplant-36/template_overrides' / TEMPLATE_NAME


def _blueprint_for(package_path):
    """Stub view imports only; retain the real template folders and loader."""
    name = {
        CHAIR_PACKAGE: 'chair_planning',
        TICKETING_PACKAGE: 'ticketing',
    }.get(package_path, package_path.replace('.', '_'))
    folder = ROOT / 'byceps' / package_path.replace('.', '/') / 'templates'
    return Blueprint(name, __name__, template_folder=str(folder))


@pytest.fixture
def make_loader_app(monkeypatch):
    monkeypatch.setattr(blueprint_util, 'get_blueprint', _blueprint_for)
    monkeypatch.setattr(templating, 'SITES_PATH', ROOT / 'sites')
    register_blueprints = site_registration.register_blueprints

    def make_app(
        *,
        site_id='chair-loader-no-overrides',
        chair_enabled=True,
        chair_late=False,
    ):
        # Initialize a distinct app completely before the first Jinja lookup.
        app = Flask(__name__)
        app.jinja_options = {**app.jinja_options, 'undefined': StrictUndefined}

        def register(app, blueprints):
            if not chair_enabled or chair_late:
                blueprints = [
                    entry for entry in blueprints if entry[0] != CHAIR_PACKAGE
                ]
            register_blueprints(app, blueprints)
            if chair_late:
                register_blueprints(app, [(CHAIR_PACKAGE, '/chair_planning')])

        with monkeypatch.context() as patch:
            patch.setattr(site_registration, 'register_blueprints', register)
            site_registration.register_site_blueprints(app)
        _init_site_app(app, SiteID(site_id))
        templatefilters.register(app)
        return app

    return make_app


def _assert_template_source(app, expected_file):
    source, filename, _ = app.jinja_env.loader.get_source(
        app.jinja_env, TEMPLATE_NAME
    )
    assert Path(filename).resolve() == expected_file
    assert source == expected_file.read_text()
    assert (
        Path(app.jinja_env.get_template(TEMPLATE_NAME).filename).resolve()
        == expected_file
    )
    return source


def test_standard_ticket_template_resolves_to_chair_module(make_loader_app):
    app = make_loader_app()

    source = _assert_template_source(app, CHAIR_FILE)

    assert 'can_edit_chair_information(ticket)' in source
    assert 'is_chair_rental_selection_enabled(ticket.party_id)' in source
    assert 'id="ticket-{{ ticket.id }}"' in source
    assert "chair_source='unknown'" in source


def test_gv36_ticket_template_override_has_priority_over_chair_module(
    make_loader_app,
):
    app = make_loader_app(site_id='totalverplant-36')

    source = _assert_template_source(app, GV36_FILE)

    assert "'site/ticketing/_chair_information.html'" in source


def test_chair_site_blueprint_is_registered_before_ticketing(make_loader_app):
    app = make_loader_app()

    names = list(app.blueprints)
    assert names.index('chair_planning') < names.index('ticketing')
    _assert_template_source(app, CHAIR_FILE)


def test_stock_ticket_template_resolves_when_chair_blueprint_is_absent(
    make_loader_app,
):
    app = make_loader_app(chair_enabled=False)

    source = _assert_template_source(app, STOCK_FILE)

    assert 'chair_planning' not in app.blueprints
    assert 'can_edit_chair_information' not in source
    assert 'is_chair_rental_selection_enabled' not in source
    assert 'ticket.is_user_managed_by(g.user.id)' in source


def test_late_chair_registration_negative_control_selects_stock(
    make_loader_app,
):
    app = make_loader_app(chair_late=True)

    names = list(app.blueprints)
    assert names.index('chair_planning') > names.index('ticketing')
    _assert_template_source(app, STOCK_FILE)
