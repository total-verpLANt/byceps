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
import re

import pytest

from byceps.byceps_app import BycepsApp
from byceps.services.lan_tournament import tournament_request_service
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID
from byceps.services.site import site_service
from byceps.services.site.models import Site, SiteID

from tests.helpers import create_site, http_client, log_in_user


PARTY_ID = PartyID('lan-party-2026-site-update-view')

_BASE_SITE_ID = SiteID('lt-test-update-form-base')
_BOTE_SITE_ID = SiteID('totalverplant-36')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand,
        PARTY_ID,
        'LAN Party 2026 Site Update View',
        max_ticket_quantity=240,
    )


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
    site = site_service.find_site(_BOTE_SITE_ID)
    if site is not None:
        return site

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


def _flat(html: str) -> str:
    return ' '.join(html.split())


def _markup(response) -> str:
    """Return the page's HTML without inline `<style>` blocks."""
    return re.sub(
        r'<style.*?</style>',
        '',
        response.get_data(as_text=True),
        flags=re.DOTALL,
    )


def _edit(tournament_request, editor, **changes):
    fields = dict(
        party_capacity=None,
        name=tournament_request.name,
        game=tournament_request.game,
        game_format=tournament_request.game_format,
        elimination_mode=tournament_request.elimination_mode,
        team_size=tournament_request.team_size,
        participant_limit=tournament_request.participant_limit,
        preferred_start_time=tournament_request.preferred_start_time,
        preferred_end_time=tournament_request.preferred_end_time,
        description=tournament_request.description,
        special_rules=tournament_request.special_rules,
        notes=tournament_request.notes,
        desired_template=tournament_request.desired_template,
    )
    fields.update(changes)
    result = tournament_request_service.update_request(
        tournament_request.id, editor.id, **fields
    )
    assert result.is_ok(), result.unwrap_err()


def test_bote_edit_page_marks_the_latest_edit_and_lists_it_in_the_history(
    bote_site_app, party, make_user
):
    proposer = make_user()
    tournament_request = _submit(party, proposer, participant_limit=8)
    _edit(
        tournament_request,
        proposer,
        name='Renamed Cup',
        participant_limit=12,
    )

    log_in_user(proposer.id)
    with http_client(bote_site_app, user_id=proposer.id) as client:
        response = client.get(_update_form_path(tournament_request.id))

    assert response.status_code == 200
    html = _markup(response)
    flat = _flat(html)

    assert html.count('form-control-block chg') == 2
    assert html.count('class="opt-chip chg-t"') == 2
    assert '<div class="lbl-row"><label class="form-label"' in html
    assert re.search(r'data-limit-caption data-before="[^"]*8[^"]*"', html)
    assert re.search(
        r'<div class="form-caption">[^<]*Site Update View Cup</div>', html
    )
    assert '<aside class="desk">' in html
    assert '<div class="desk-h">' in html
    assert re.search(r'Name, [^<]*8 → 12', flat), flat
    assert '<ol class="tl">' in html


def test_bote_edit_page_without_an_edit_shows_no_markers(
    bote_site_app, party, make_user
):
    proposer = make_user()
    tournament_request = _submit(party, proposer)

    log_in_user(proposer.id)
    with http_client(bote_site_app, user_id=proposer.id) as client:
        response = client.get(_update_form_path(tournament_request.id))

    html = _markup(response)
    assert response.status_code == 200
    assert 'chg-t' not in html
    assert 'form-control-block chg' not in html
    assert 'data-before' not in html
    assert '<div class="desk-h">' in html


def test_bote_frozen_page_shows_the_six_row_summary_with_short_dates(
    bote_site_app, party, make_user
):
    proposer = make_user()
    decider = make_user()
    starts_at = datetime(2026, 10, 3, 19, 0, tzinfo=UTC)
    tournament_request = _submit(
        party,
        proposer,
        preferred_start_time=starts_at,
        preferred_end_time=starts_at + timedelta(hours=2),
        notes='Only for the orga.',
    )
    accept_result = tournament_request_service.accept_request(
        tournament_request.id, decider.id
    )
    assert accept_result.is_ok(), accept_result.unwrap_err()

    log_in_user(proposer.id)
    with http_client(bote_site_app, user_id=proposer.id) as client:
        response = client.get(_update_form_path(tournament_request.id))

    assert response.status_code == 200
    html = _markup(response)

    assert html.count('<dl class="sup">') == 1
    rows = re.findall(r'<div><dt>([^<]*)</dt><dd>([^<]*)</dd></div>', html)
    assert len(rows) == 6
    values = dict(rows)
    assert 'Some Game' in values.values()
    times = [value for _label, value in rows[-2:]]
    assert all(re.fullmatch(r'03\.10\. \d{2}:\d{2}', t) for t in times), times
    assert '2026' not in ''.join(times)
    assert 'desk-h' not in html
    assert 'class="desk"' not in html
    assert 'Only for the orga.' not in html
    lock_note = re.search(
        r'class="note-box lock">.*?<p>(.*?)</p>', html, flags=re.DOTALL
    )
    assert lock_note, html
    assert re.search(r'\b\d{2}\.\d{2}\.(?!\d)', lock_note.group(1))
    assert '2026' not in lock_note.group(1)
    assert '%(' not in html
    assert 'class="btns" style="margin-top:22px"' in html
