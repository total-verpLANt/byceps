"""
tests.integration.services.lan_tournament.test_tournament_request_input_bounds
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two defects, both driven through the real site app's Flask test
client (`test_tournament_request_site_update_view.py`'s pattern) so a
regression 500 is actually caught, plus a `submit_request` service
call for the NUL case:

* an out-of-range `preferred_start_time`/`preferred_end_time` year
  (a `DateTimeLocalField` on its own accepts any year a browser lets
  through) used to reach `flask_babel.to_utc`, whose `pytz`
  localization overflows `datetime`'s range in a zone with a
  pre-1893 LMT offset (Europe/Berlin) -- an unhandled 500. Bounded by
  `tournament_request_domain_service.year_in_range_validator`.
* a NUL byte (or other disallowed `Cc` char) in `description` (also
  `special_rules`, `notes`, `desired_template`) used to reach the
  flush unchecked, and PostgreSQL text columns cannot store NUL --
  an unhandled 500 (`DataError`). Bounded by
  `tournament_request_domain_service.contains_disallowed_control_char`.

This file creates its own site row (a fresh, unique site ID) rather
than reusing `totalverplant-36`, per the sibling
`test_tournament_request_site_nav_and_draft.py`'s note: that site ID
is a real on-disk `template_overrides` directory whose row is already
created, module-scoped, by `test_tournament_request_site_update_view.py`,
and site IDs are a primary key.
"""

from datetime import datetime, timedelta, UTC

import pytest

from byceps.byceps_app import BycepsApp
from byceps.services.lan_tournament import tournament_request_service
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID
from byceps.services.site.models import Site, SiteID
from byceps.services.ticketing import ticket_creation_service

from tests.helpers import create_site, http_client, log_in_user


PARTY_ID = PartyID('lan-party-2026-request-input-bounds')

_SITE_ID = SiteID('lt-test-input-bounds-base')

_PROPOSE_PATH = '/lan-tournaments/requests/propose'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Input Bounds')


@pytest.fixture(scope='module')
def site(party) -> Site:
    """A site with no `template_overrides` directory on disk."""
    return create_site(
        _SITE_ID,
        party.brand_id,
        server_name='lt-test-input-bounds-base.test',
        party_id=party.id,
    )


@pytest.fixture(scope='module')
def site_app(database, make_site_app, site: Site) -> BycepsApp:
    app = make_site_app(site.server_name, site.id)
    with app.app_context():
        return app


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Input Bounds Entry')


@pytest.fixture()
def ticketed_user(make_user, ticket_category):
    """A user with a ticket for `party` -- `propose` requires one."""
    user = make_user()
    ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return user


def _propose_form_data(**overrides):
    now = datetime.now(UTC)
    data = {
        'name': 'Input Bounds Cup',
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


# ------------------------------------------------------------------ #
# 1. out-of-range preferred_start_time -> no 500, re-render 200
# ------------------------------------------------------------------ #


def test_propose_with_year_1_preferred_start_time_does_not_500(
    site_app, party, ticketed_user
):
    data = _propose_form_data(preferred_start_time='0001-01-01T00:00')

    log_in_user(ticketed_user.id)
    with http_client(site_app, user_id=ticketed_user.id) as client:
        response = client.post(_PROPOSE_PATH, data=data)

    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    html = response.get_data(as_text=True)
    assert 'Please enter a date between the years' in html


def test_propose_with_year_9999_preferred_end_time_does_not_500(
    site_app, party, ticketed_user
):
    data = _propose_form_data(preferred_end_time='9999-12-31T23:59')

    log_in_user(ticketed_user.id)
    with http_client(site_app, user_id=ticketed_user.id) as client:
        response = client.post(_PROPOSE_PATH, data=data)

    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    html = response.get_data(as_text=True)
    assert 'Please enter a date between the years' in html


# ------------------------------------------------------------------ #
# 2. NUL byte in description -> no 500, re-render 200 (view path);
#    submit_request directly -> Err, no DataError (service path)
# ------------------------------------------------------------------ #


def test_propose_with_nul_byte_in_description_does_not_500(
    site_app, party, ticketed_user
):
    data = _propose_form_data(description='Line one\x00line two')

    log_in_user(ticketed_user.id)
    with http_client(site_app, user_id=ticketed_user.id) as client:
        response = client.post(_PROPOSE_PATH, data=data)

    assert response.status_code == 200
    assert response.mimetype == 'text/html'


def test_submit_request_with_nul_byte_in_description_is_rejected(
    party, make_user
):
    proposer = make_user()
    now = datetime.now(UTC)

    result = tournament_request_service.submit_request(
        party.id,
        proposer.id,
        party_capacity=None,
        name='Input Bounds Service Cup',
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=now + timedelta(days=1),
        preferred_end_time=now + timedelta(days=1, hours=4),
        description='Line one\x00line two',
    )

    assert result.is_err()
    assert (
        result.unwrap_err()
        == 'Description must not contain control characters.'
    )
