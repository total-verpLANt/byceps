"""
tests.integration.services.lan_tournament.test_tournament_request_site_update_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

View-level regression test for the site `update_request_form` route
(bead workspace-cg0k.1): a bare `@templated` there derived the
non-existent template name `site/lan_tournament/update_request_form.html`
(byceps/util/framework/templating.py `_derive_template_name`), 500ing
every request-card link from `my_requests` and the failed-update
re-render path. Explicit naming
(`@templated('site/lan_tournament/propose_form')`) fixes it.

Unlike `test_tournament_request_views_site.py` (which only ever calls
`view.__wrapped__`, unwrapping both `@login_required` and `@templated`
and so never actually invokes `render_template`) and
`test_tournament_request_render_site.py` / `_render_bote.py` (which
render `propose_form.html` directly from a hand-built Jinja
`Environment`, never touching the view at all), this file drives the
real, fully decorated view -- `@templated` included -- through a real
site app's Flask test client, so a missing/misnamed template is
actually caught.

Runs against two site apps sharing one party: one whose site ID has
no `sites/<id>/template_overrides` directory on disk (Jinja falls
through to the blueprint's own `propose_form.html`) and one bound to
`totalverplant-36` (the real override directory on disk), to prove
the fix covers both surfaces.
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

from tests.helpers import create_site, http_client, log_in_user


PARTY_ID = PartyID('lan-party-2026-site-update-view')

_BASE_SITE_ID = SiteID('lt-test-update-form-base')
_BOTE_SITE_ID = SiteID('totalverplant-36')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Site Update View')


@pytest.fixture(scope='module')
def base_site(party) -> Site:
    """A site with no `template_overrides` directory on disk."""
    return create_site(
        _BASE_SITE_ID,
        party.brand_id,
        server_name='lt-test-update-form-base.test',
        party_id=party.id,
    )


@pytest.fixture(scope='module')
def bote_site(party) -> Site:
    """Bound to the real `totalverplant-36` override directory."""
    return create_site(
        _BOTE_SITE_ID,
        party.brand_id,
        server_name='lt-test-update-form-bote.test',
        party_id=party.id,
    )


@pytest.fixture(scope='module')
def base_site_app(database, make_site_app, base_site: Site) -> BycepsApp:
    app = make_site_app(base_site.server_name, base_site.id)
    with app.app_context():
        return app


@pytest.fixture(scope='module')
def bote_site_app(database, make_site_app, bote_site: Site) -> BycepsApp:
    app = make_site_app(bote_site.server_name, bote_site.id)
    with app.app_context():
        return app


def _submit(party, proposer, **overrides):
    now = datetime.now(UTC)
    kwargs = dict(
        party_capacity=None,
        name='Site Update View Cup',
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=now + timedelta(days=1),
        preferred_end_time=now + timedelta(days=1, hours=4),
        description='A friendly cup.',
    )
    kwargs.update(overrides)
    result = tournament_request_service.submit_request(
        party.id, proposer.id, **kwargs
    )
    assert result.is_ok(), result.unwrap_err()
    tournament_request, _event = result.unwrap()
    return tournament_request


def _update_form_path(request_id) -> str:
    return f'/lan-tournaments/requests/{request_id}/update'


# ------------------------------------------------------------------ #
# 1. editable (submitted) own request -> 200, edit mode
# ------------------------------------------------------------------ #


@pytest.mark.parametrize('app_name', ['base_site_app', 'bote_site_app'])
def test_update_request_form_renders_editable_request(
    app_name, request, party, make_user
):
    app = request.getfixturevalue(app_name)
    proposer = make_user()
    tournament_request = _submit(party, proposer)

    log_in_user(proposer.id)
    with http_client(app, user_id=proposer.id) as client:
        response = client.get(_update_form_path(tournament_request.id))

    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    html = response.get_data(as_text=True)
    assert f'/requests/{tournament_request.id}/withdraw' in html


# ------------------------------------------------------------------ #
# 2. frozen (rejected, with a reason) own request -> 200, frozen mode
# ------------------------------------------------------------------ #


@pytest.mark.parametrize('app_name', ['base_site_app', 'bote_site_app'])
def test_update_request_form_renders_frozen_rejected_request(
    app_name, request, party, make_user
):
    app = request.getfixturevalue(app_name)
    proposer = make_user()
    decider = make_user()
    tournament_request = _submit(party, proposer)

    reason = 'Not enough interest, integration test marker.'
    reject_result = tournament_request_service.reject_request(
        tournament_request.id, decider.id, reason
    )
    assert reject_result.is_ok(), reject_result.unwrap_err()

    log_in_user(proposer.id)
    with http_client(app, user_id=proposer.id) as client:
        response = client.get(_update_form_path(tournament_request.id))

    assert response.status_code == 200
    assert response.mimetype == 'text/html'
    html = response.get_data(as_text=True)
    assert reason in html
    assert f'/requests/{tournament_request.id}/withdraw' not in html


# ------------------------------------------------------------------ #
# 3. erroneous-form re-render path: update POST with invalid data
#    re-renders (200), it does not 500
# ------------------------------------------------------------------ #


@pytest.mark.parametrize('app_name', ['base_site_app', 'bote_site_app'])
def test_update_request_erroneous_form_rerenders_instead_of_500(
    app_name, request, party, make_user
):
    app = request.getfixturevalue(app_name)
    proposer = make_user()
    tournament_request = _submit(party, proposer)

    url = f'/lan-tournaments/requests/{tournament_request.id}/update'

    log_in_user(proposer.id)
    with http_client(app, user_id=proposer.id) as client:
        response = client.post(url, data={})

    assert response.status_code == 200
    assert response.mimetype == 'text/html'
