"""
tests.integration.services.lan_tournament.test_tournament_request_site_nav_and_draft
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

View-level regression tests for workspace-vxrc.3 (brief C):

* issue h -- `my_requests` must never link a DRAFT tournament: admin
  `create` always makes a tournament DRAFT, and site `view` 404s DRAFT
  for everyone (proposer included), so a live link there is dead on
  arrival. This drives the real `my_requests` route through a real
  site app's Flask test client for both a DRAFT-status and a
  REGISTRATION_OPEN-status linked tournament.
* issue l -- the lan_tournament site index page must link
  "Propose a tournament" / "My tournament requests" for a logged-in
  user, and show neither to an anonymous visitor.

Runs against a site with no `sites/<id>/template_overrides` directory
on disk, so it falls through to the blueprint's own `my_requests.html`
/ `index.html` -- the same templates the totalverplant-36 override
would otherwise exercise, just via the base Jinja loader path. This
file deliberately does NOT also stand up a `totalverplant-36`-id site:
that site ID is a real on-disk directory
(`sites/totalverplant-36/template_overrides/`) and its row is already
created, module-scoped, by the sibling
`test_tournament_request_site_update_view.py` -- site IDs are a
primary key, so a second creation attempt in the same pytest session
would 500 with a duplicate-key `IntegrityError` (confirmed while
writing this file). The override-specific markup for both fixes here
(the `.st st-draft` tag, the nav links) is covered instead by the
mutation-tested `bote_env` render tests in
`test_tournament_request_render_bote.py` /
`test_tournament_request_index_nav.py`, which render the real override
template files directly without needing a second site row.
"""

from datetime import datetime, timedelta, UTC

import pytest

from byceps.byceps_app import BycepsApp
from byceps.services.lan_tournament import (
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.site.models import Site, SiteID

from tests.helpers import create_site, http_client, log_in_user


PARTY_ID = PartyID('lan-party-2026-site-nav-draft')

_BASE_SITE_ID = SiteID('lt-test-nav-draft-base')

_INDEX_PATH = '/lan-tournaments/'
_MY_REQUESTS_PATH = '/lan-tournaments/requests'


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Site Nav Draft')


@pytest.fixture(scope='module')
def base_site(party) -> Site:
    """A site with no `template_overrides` directory on disk."""
    return create_site(
        _BASE_SITE_ID,
        party.brand_id,
        server_name='lt-test-nav-draft-base.test',
        party_id=party.id,
    )


@pytest.fixture(scope='module')
def base_site_app(database, make_site_app, base_site: Site) -> BycepsApp:
    app = make_site_app(base_site.server_name, base_site.id)
    with app.app_context():
        return app


def _submit_and_link(party, proposer, decider, *, name, tournament_status):
    now = datetime.now(UTC)
    result = tournament_request_service.submit_request(
        party.id,
        proposer.id,
        party_capacity=None,
        name=name,
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=now + timedelta(days=1),
        preferred_end_time=now + timedelta(days=1, hours=4),
        description='A friendly cup.',
    )
    assert result.is_ok(), result.unwrap_err()
    tournament_request, _event = result.unwrap()

    accept_result = tournament_request_service.accept_request(
        tournament_request.id, decider.id
    )
    assert accept_result.is_ok(), accept_result.unwrap_err()

    create_result = tournament_service.create_tournament(
        party.id,
        name,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=tournament_status,
        created_from_request_id=tournament_request.id,
        initiator_id=decider.id,
    )
    assert create_result.is_ok(), create_result.unwrap_err()
    tournament, _event = create_result.unwrap()

    return tournament_request, tournament


# ------------------------------------------------------------------ #
# issue h -- my_requests never links a DRAFT tournament
# ------------------------------------------------------------------ #


def test_my_requests_hides_link_and_shows_label_for_draft_tournament(
    base_site_app, party, make_user
):
    proposer = make_user()
    decider = make_user()

    _tournament_request, tournament = _submit_and_link(
        party,
        proposer,
        decider,
        name='Draft Nav Cup',
        tournament_status=TournamentStatus.DRAFT,
    )

    log_in_user(proposer.id)
    with http_client(base_site_app, user_id=proposer.id) as client:
        response = client.get(_MY_REQUESTS_PATH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'/lan-tournaments/{tournament.id}' not in html
    assert 'In preparation' in html
    assert 'Draft Nav Cup' in html

    # The DRAFT tournament itself still 404s, confirming the link
    # would have been dead had it been rendered.
    with http_client(base_site_app, user_id=proposer.id) as client:
        view_response = client.get(f'/lan-tournaments/{tournament.id}')
    assert view_response.status_code == 404


def test_my_requests_keeps_link_for_registration_open_tournament(
    base_site_app, party, make_user
):
    proposer = make_user()
    decider = make_user()

    _tournament_request, tournament = _submit_and_link(
        party,
        proposer,
        decider,
        name='Live Nav Cup',
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    log_in_user(proposer.id)
    with http_client(base_site_app, user_id=proposer.id) as client:
        response = client.get(_MY_REQUESTS_PATH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'/lan-tournaments/{tournament.id}' in html
    assert 'In preparation' not in html


# ------------------------------------------------------------------ #
# issue l -- index page nav links
# ------------------------------------------------------------------ #


def test_index_shows_request_nav_links_for_logged_in_user(
    base_site_app, make_user
):
    user = make_user()

    log_in_user(user.id)
    with http_client(base_site_app, user_id=user.id) as client:
        response = client.get(_INDEX_PATH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '/lan-tournaments/requests/propose' in html
    assert '/lan-tournaments/requests' in html


def test_index_hides_request_nav_links_for_anonymous_visitor(base_site_app):
    with http_client(base_site_app) as client:
        response = client.get(_INDEX_PATH)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '/lan-tournaments/requests/propose' not in html
    assert 'requests/propose' not in html
