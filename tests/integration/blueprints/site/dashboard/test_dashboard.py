"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from byceps.services.chair_optout import chair_optout_service
from byceps.services.site.models import SiteID
from byceps.services.ticketing import ticket_creation_service
from byceps.util.templating import create_site_template_loader

from tests.helpers import generate_token, http_client, log_in_user


def test_when_logged_in(site_app, user):
    log_in_user(user.id)

    response = send_request(site_app, user_id=user.id)

    assert response.status_code == 200
    assert response.mimetype == 'text/html'


def test_when_not_logged_in(site_app):
    response = send_request(site_app)

    assert response.status_code == 302
    assert 'Location' in response.headers


def test_gv36_chair_coupon_only_for_ticket_with_unanswered_chair(
    make_site_app, site, party, make_user, make_ticket_category
):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))

    with app.app_context():
        participant = make_user(generate_token())
        no_ticket_user = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        log_in_user(participant.id)
        log_in_user(no_ticket_user.id)

        pending_response = send_request(app, user_id=participant.id)
        chair_optout_service.set_optout(
            party.id, ticket.id, participant.id, False
        )
        answered_response = send_request(app, user_id=participant.id)
        no_ticket_response = send_request(app, user_id=no_ticket_user.id)

    pending_html = pending_response.get_data(as_text=True)
    assert pending_response.status_code == 200
    assert 'class="dashboard-chair-coupon"' in pending_html
    assert f'/chair_optout/#ticket-{ticket.id}' in pending_html
    assert answered_response.status_code == 200
    assert 'class="dashboard-chair-coupon"' not in answered_response.get_data(
        as_text=True
    )
    assert no_ticket_response.status_code == 200
    assert 'class="dashboard-chair-coupon"' not in no_ticket_response.get_data(
        as_text=True
    )


# helpers


def send_request(app, user_id=None):
    url = 'http://www.acmecon.test/dashboard'
    with http_client(app, user_id=user_id) as client:
        return client.get(url)
