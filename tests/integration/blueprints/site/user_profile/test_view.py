"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from flask_babel import force_locale, gettext

from byceps.database import db
from byceps.services.party.dbmodels import DbParty
from byceps.services.site.models import SiteID
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
)
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.util.templating import create_site_template_loader

from tests.helpers import generate_token, http_client, log_in_user


def test_view_profile_of_existing_user(site_app, site, user):
    response = request_profile(site_app, user.id)

    assert response.status_code == 200
    assert response.mimetype == 'text/html'


def test_view_profile_of_uninitialized_user(site_app, site, uninitialized_user):
    response = request_profile(site_app, uninitialized_user.id)

    assert response.status_code == 404


def test_view_profile_of_suspended_user(site_app, site, suspended_user):
    response = request_profile(site_app, suspended_user.id)

    assert response.status_code == 404


def test_view_profile_of_deleted_user(site_app, site, deleted_user):
    response = request_profile(site_app, deleted_user.id)

    assert response.status_code == 404


def test_view_profile_of_unknown_user(site_app, site):
    unknown_user_id = '00000000-0000-0000-0000-000000000000'

    response = request_profile(site_app, unknown_user_id)

    assert response.status_code == 404


def test_default_own_profile_has_no_local_chair_display(
    site_app, site, party, make_user, make_ticket_category
):
    profile_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, profile_user, user=profile_user
    )
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.venue, profile_user
    ).unwrap()
    log_in_user(profile_user.id)

    response = request_profile(
        site_app, profile_user.id, current_user_id=profile_user.id
    )
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert translate(site_app, 'Chair information') not in text
    assert '/tickets/mine#ticket-' not in text


def test_other_profile_does_not_show_chair_edit_action(
    site_app, site, party, make_user, make_ticket_category
):
    current_user = make_user(generate_token())
    other_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket_creation_service.create_ticket(category, other_user, user=other_user)
    log_in_user(current_user.id)

    response = request_profile(
        site_app, other_user.id, current_user_id=current_user.id
    )
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert translate(site_app, 'Make selection') not in text
    assert translate(site_app, 'Change selection') not in text
    assert '/chair_optout/' not in text


def test_gv36_theme_shows_chair_action_only_on_own_profile(
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
        visitor = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        log_in_user(participant.id)
        log_in_user(visitor.id)

        own_response = request_profile(
            app, participant.id, current_user_id=participant.id
        )
        ticket_seat_management_service.set_chair_source(
            ticket.id, ChairSource.user, participant
        ).unwrap()
        answered_response = request_profile(
            app, participant.id, current_user_id=participant.id
        )
        other_response = request_profile(
            app, participant.id, current_user_id=visitor.id
        )

    own_html = own_response.get_data(as_text=True)
    answered_html = answered_response.get_data(as_text=True)
    other_html = other_response.get_data(as_text=True)
    assert own_response.status_code == 200
    assert 'class="chair-callout"' not in own_html
    assert ticket.code in own_html
    assert f'/tickets/mine#ticket-{ticket.id}' in own_html
    assert 'class="btn-news chair-action"' in own_html
    assert translate(app, 'Not specified yet') in own_html
    assert answered_response.status_code == 200
    assert 'class="chair-callout"' not in answered_html
    assert translate(app, 'Brings own chair') in answered_html
    assert translate(app, 'Change selection') in answered_html
    assert other_response.status_code == 200
    assert 'class="chair-callout"' not in other_html
    assert '/tickets/mine#ticket-' not in other_html
    assert 'class="chair-entry"' not in other_html


@pytest.fixture
def enable_ticket_management(admin_app, party):
    db_party = db.session.get(DbParty, party.id)
    previous_value = db_party.ticket_management_enabled
    db_party.ticket_management_enabled = True
    db.session.commit()
    yield
    db_party.ticket_management_enabled = previous_value
    db.session.commit()


@pytest.mark.parametrize('source', list(ChairSource))
@pytest.mark.parametrize('state', ['checked_in', 'disabled'])
def test_gv36_own_profile_displays_inactive_state_without_action(
    make_site_app,
    site,
    party,
    make_user,
    make_ticket_category,
    enable_ticket_management,
    source,
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
        ticket_seat_management_service.set_chair_source(
            ticket.id, source, participant
        ).unwrap()
        if state == 'checked_in':
            ticket.user_checked_in = True
        else:
            db.session.get(DbParty, party.id).ticket_management_enabled = False
        db.session.commit()
        log_in_user(participant.id)
        response = request_profile(
            app, participant.id, current_user_id=participant.id
        )

    label = {
        ChairSource.user: 'Brings own chair',
        ChairSource.venue: 'Needs a provided chair',
        ChairSource.rental: 'rented',
        ChairSource.unknown: 'Not specified yet',
    }[source]
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert translate(app, label) in html
    assert ticket.code in html
    assert 'class="btn-news chair-action"' not in html
    assert '/tickets/mine#ticket-' not in html


# helpers


def request_profile(app, user_id, *, current_user_id=None):
    url = f'http://www.acmecon.test/users/{user_id}'

    with http_client(app, user_id=current_user_id) as client:
        return client.get(url)


def translate(app, message: str) -> str:
    with app.test_request_context():
        with force_locale(app.config['LOCALE']):
            return gettext(message)
