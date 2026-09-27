"""
tests.integration.services.lan_tournament.test_tournament_request_huge_int_forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

I1: a 309+ digit `team_size`/`participant_limit` used to reach stock
`wtforms.validators.NumberRange.__call__`, which calls
`math.isnan(data)` unconditionally -- for an `int` too large to
convert to a `float` (roughly >= 2**1024, ~309 decimal digits) that
raises `OverflowError` instead of returning a bool, an unhandled 500.
`IntegerField` itself parses up to 4300 digits. `SafeNumberRange`
(`form_validators.py`) fixes it; this drives the real site app's
Flask test client (`test_tournament_request_input_bounds.py`'s
pattern) through the actual `propose` POST route, so the fix is
proven at the HTTP layer, not just against the domain service.

This file creates its own site row (a fresh, unique site ID), per
`test_tournament_request_site_nav_and_draft.py`'s note: `SiteID` is a
primary key and other files' site rows are module-scoped.
"""

from datetime import datetime, timedelta, UTC

import pytest

from byceps.byceps_app import BycepsApp
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID
from byceps.services.site.models import Site, SiteID
from byceps.services.ticketing import ticket_creation_service

from tests.helpers import create_site, http_client, log_in_user


PARTY_ID = PartyID('lan-party-2026-huge-int-forms')

_SITE_ID = SiteID('lt-test-huge-int-base')

_PROPOSE_PATH = '/lan-tournaments/requests/propose'

_309_DIGITS = '9' * 309
_4300_DIGITS = '9' * 4300


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Huge Int Forms')


@pytest.fixture(scope='module')
def site(party) -> Site:
    """A site with no `template_overrides` directory on disk."""
    return create_site(
        _SITE_ID,
        party.brand_id,
        server_name='lt-test-huge-int-base.test',
        party_id=party.id,
    )


@pytest.fixture(scope='module')
def site_app(database, make_site_app, site: Site) -> BycepsApp:
    app = make_site_app(site.server_name, site.id)
    with app.app_context():
        return app


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Huge Int Forms Entry')


@pytest.fixture()
def ticketed_user(make_user, ticket_category):
    """A user with a ticket for `party` -- `propose` requires one."""
    user = make_user()
    ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return user


def _propose_form_data(**overrides):
    now = datetime.now(UTC)
    data = {
        'name': 'Huge Int Cup',
        'game': 'Some Game',
        'game_format': GameFormat.ONE_V_ONE.value,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
        'team_size': '1',
        'participant_limit': '8',
        'preferred_start_time': (now + timedelta(days=1)).strftime(
            '%Y-%m-%dT%H:%M'
        ),
        'preferred_end_time': (now + timedelta(days=1, hours=4)).strftime(
            '%Y-%m-%dT%H:%M'
        ),
        'description': 'A friendly cup.',
    }
    data.update(overrides)
    return data


# fmt: off
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('participant_limit', _309_DIGITS, id='participant_limit_309'),
        pytest.param('participant_limit', _4300_DIGITS, id='participant_limit_4300'),
        pytest.param('team_size', _309_DIGITS, id='team_size_309'),
        pytest.param('team_size', _4300_DIGITS, id='team_size_4300'),
    ],
)
# fmt: on
def test_propose_with_huge_int_does_not_500(
    field, value, site_app, party, ticketed_user
):
    data = _propose_form_data(**{field: value})

    log_in_user(ticketed_user.id)
    with http_client(site_app, user_id=ticketed_user.id) as client:
        response = client.post(_PROPOSE_PATH, data=data)

    assert response.status_code == 200
    assert response.mimetype == 'text/html'
