"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from flask import url_for
from flask_babel import force_locale, gettext

from byceps.database import db
from byceps.services.party.dbmodels import DbParty
from byceps.services.site.models import SiteID
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
    ticket_user_management_service,
)
from byceps.services.ticketing.models.ticket import ChairSource
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


@pytest.fixture
def enable_ticket_management(admin_app, party):
    db_party = db.session.get(DbParty, party.id)
    previous_value = db_party.ticket_management_enabled
    db_party.ticket_management_enabled = True
    db.session.commit()
    yield
    db_party.ticket_management_enabled = previous_value
    db.session.commit()


def test_gv36_chair_coupon_tracks_multiple_unanswered_tickets_and_reset(
    make_site_app,
    site,
    party,
    make_user,
    make_ticket_category,
    enable_ticket_management,
):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))

    with app.app_context():
        participant = make_user(generate_token())
        borrowed_ticket_user = make_user(generate_token())
        no_ticket_user = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        other_ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        borrowed_ticket = ticket_creation_service.create_ticket(
            category, participant, user=borrowed_ticket_user
        )
        ticket_user_management_service.appoint_user_manager(
            borrowed_ticket.id, borrowed_ticket_user, participant
        ).unwrap()
        log_in_user(participant.id)
        log_in_user(no_ticket_user.id)
        log_in_user(borrowed_ticket_user.id)

        pending_response = send_request(app, user_id=participant.id)
        ticket_seat_management_service.set_chair_source(
            ticket.id, ChairSource.venue, participant
        ).unwrap()
        partially_answered_response = send_request(app, user_id=participant.id)
        ticket_seat_management_service.set_chair_source(
            other_ticket.id, ChairSource.user, participant
        ).unwrap()
        answered_response = send_request(app, user_id=participant.id)
        ticket_user_management_service.appoint_user(
            ticket.id, borrowed_ticket_user, participant
        ).unwrap()
        ticket_user_management_service.appoint_user(
            ticket.id, participant, participant
        ).unwrap()
        reset_response = send_request(app, user_id=participant.id)
        # This participant owns none of their tickets; reminders follow use.
        other_participant_response = send_request(
            app, user_id=borrowed_ticket_user.id
        )
        no_ticket_response = send_request(app, user_id=no_ticket_user.id)

    pending_html = pending_response.get_data(as_text=True)
    assert pending_response.status_code == 200
    assert 'class="dashboard-chair-coupon"' in pending_html
    first_ticket = min([ticket, other_ticket], key=lambda t: t.code)
    assert f'/tickets/mine#ticket-{first_ticket.id}' in pending_html
    with app.test_request_context(), force_locale(app.config['LOCALE']):
        assert f'2 {gettext("tickets")}' in pending_html
    assert partially_answered_response.status_code == 200
    assert (
        f'/tickets/mine#ticket-{other_ticket.id}'
        in partially_answered_response.get_data(as_text=True)
    )
    assert answered_response.status_code == 200
    assert 'class="dashboard-chair-coupon"' not in answered_response.get_data(
        as_text=True
    )
    assert reset_response.status_code == 200
    assert f'/tickets/mine#ticket-{ticket.id}' in reset_response.get_data(
        as_text=True
    )
    assert other_participant_response.status_code == 200
    assert (
        'class="dashboard-chair-coupon"'
        in other_participant_response.get_data(as_text=True)
    )
    assert no_ticket_response.status_code == 200
    assert 'class="dashboard-chair-coupon"' not in no_ticket_response.get_data(
        as_text=True
    )


@pytest.mark.parametrize('state', ['checked_in', 'revoked', 'disabled'])
def test_gv36_chair_coupon_excludes_ineligible_unanswered_tickets(
    make_site_app,
    site,
    party,
    make_user,
    make_ticket_category,
    enable_ticket_management,
    state,
):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))
    with app.app_context():
        participant = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        if state == 'checked_in':
            ticket.user_checked_in = True
        elif state == 'revoked':
            ticket.revoked = True
        else:
            db.session.get(DbParty, party.id).ticket_management_enabled = False
        db.session.commit()
        log_in_user(participant.id)
        response = send_request(app, user_id=participant.id)
    assert response.status_code == 200
    assert 'class="dashboard-chair-coupon"' not in response.get_data(
        as_text=True
    )


def test_gv36_chair_coupon_returns_after_automatic_participant_reset(
    make_site_app,
    site,
    party,
    make_user,
    make_ticket_category,
    enable_ticket_management,
):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))
    with app.app_context():
        participant = make_user(generate_token())
        next_participant = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        log_in_user(participant.id)
        with app.test_request_context():
            answer_url = url_for(
                'ticketing.set_chair_source',
                ticket_id=ticket.id,
                chair_source='user',
            )
        with http_client(app, user_id=participant.id) as client:
            assert client.post(answer_url).status_code == 204
            answered = client.get('http://www.acmecon.test/dashboard')
            ticket_user_management_service.appoint_user(
                ticket.id, next_participant, participant
            ).unwrap()
            ticket_user_management_service.appoint_user(
                ticket.id, participant, participant
            ).unwrap()
            reset = client.get('http://www.acmecon.test/dashboard')
    assert 'class="dashboard-chair-coupon"' not in answered.get_data(
        as_text=True
    )
    assert 'class="dashboard-chair-coupon"' in reset.get_data(as_text=True)


# helpers


def send_request(app, user_id=None):
    url = 'http://www.acmecon.test/dashboard'
    with http_client(app, user_id=user_id) as client:
        return client.get(url)
