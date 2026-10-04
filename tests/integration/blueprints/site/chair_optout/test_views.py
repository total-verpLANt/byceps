"""
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from flask_babel import force_locale, gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.seating.blueprints.site import views as seating_views
from byceps.services.site.models import SiteID
from byceps.services.ticketing import ticket_creation_service
from byceps.util.templating import create_site_template_loader

from tests.helpers import generate_token, http_client, log_in_user


BASE_URL = 'http://www.acmecon.test/chair_optout/'


def test_index_requires_login(site_app, site):
    with http_client(site_app) as client:
        response = client.get(BASE_URL)

    assert response.status_code == 302


def test_index_shows_only_currently_used_tickets(
    site_app, site, party, make_user, make_ticket_category
):
    participant = make_user(generate_token())
    other_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    current_ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    foreign_ticket = ticket_creation_service.create_ticket(
        category, participant, user=other_user
    )
    log_in_user(participant.id)

    with http_client(site_app, user_id=participant.id) as client:
        response = client.get(BASE_URL)

    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert current_ticket.code in text
    assert foreign_ticket.code not in text
    assert _translate(site_app, 'no seat') in text
    assert _translate(site_app, 'Not specified yet') in text


def test_gv36_theme_renders_chair_form(
    make_site_app, site, party, make_user, make_ticket_category
):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))

    with app.app_context():
        participant = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        log_in_user(participant.id)

        with http_client(app, user_id=participant.id) as client:
            response = client.get(BASE_URL)

    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f'id="ticket-{ticket.id}"' in text
    assert ticket.code in text
    assert 'class="bote-page chair-page"' in text
    assert 'class="button chair-submit"' in text
    assert f'name="{ticket.id}-choice"' in text


def test_gv36_seat_management_prompts_until_chair_answered(
    make_site_app, site, party, make_user, make_ticket_category, monkeypatch
):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))

    with app.app_context():
        participant = make_user(generate_token())
        category = make_ticket_category(party.id, generate_token())
        area = seating_area_service.create_area(
            party.id,
            generate_token(),
            'Hall',
            image_filename='hall.png',
            image_width=600,
            image_height=400,
        )
        seat_service.create_seat(area.id, 20, 20, category.id)
        ticket = ticket_creation_service.create_ticket(
            category, participant, user=participant
        )
        log_in_user(participant.id)
        monkeypatch.setattr(
            seating_views, '_is_seat_management_enabled', lambda: True
        )

        url = f'http://www.acmecon.test/seating/areas/{area.slug}/manage_seats'
        with http_client(app, user_id=participant.id) as client:
            pending_response = client.get(url)
            chair_optout_service.set_optout(
                party.id, ticket.id, participant.id, False
            )
            answered_response = client.get(url)

    pending_html = pending_response.get_data(as_text=True)
    answered_html = answered_response.get_data(as_text=True)
    assert pending_response.status_code == 200
    assert 'class="block chair-prompt"' in pending_html
    assert f'/chair_optout/#ticket-{ticket.id}' in pending_html
    assert answered_response.status_code == 200
    assert 'class="block chair-prompt"' not in answered_html
    assert 'class="button chair-information-link"' in answered_html


@pytest.mark.parametrize(
    ('gv36_theme', 'admin_selected'), [(False, False), (True, True)]
)
def test_seat_manager_without_used_ticket_has_no_chair_link(
    make_site_app,
    site,
    party,
    make_user,
    make_ticket_category,
    monkeypatch,
    gv36_theme,
    admin_selected,
):
    app = make_site_app('www.acmecon.test', site.id)
    if gv36_theme:
        app.jinja_loader = create_site_template_loader(
            SiteID('totalverplant-36')
        )

    with app.app_context():
        owner = make_user(generate_token())
        participant = make_user(generate_token())
        viewer = make_user(generate_token()) if admin_selected else owner
        category = make_ticket_category(party.id, generate_token())
        area = seating_area_service.create_area(
            party.id,
            generate_token(),
            generate_token(),
            image_filename='hall.png',
            image_width=600,
            image_height=400,
        )
        seat_service.create_seat(area.id, 20, 20, category.id)
        ticket = ticket_creation_service.create_ticket(
            category, owner, user=participant
        )
        log_in_user(viewer.id)
        monkeypatch.setattr(
            seating_views, '_is_seat_management_enabled', lambda: True
        )
        if admin_selected:
            monkeypatch.setattr(
                seating_views, '_is_current_user_seating_admin', lambda: True
            )

        url = f'http://www.acmecon.test/seating/areas/{area.slug}/manage_seats'
        if admin_selected:
            url += f'?ticket_id={ticket.id}'
        with http_client(app, user_id=viewer.id) as client:
            response = client.get(url)

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert ticket.code in html
    assert 'class="button chair-information-link"' not in html


def test_current_user_can_store_both_answers_without_seat(
    site_app, site, party, make_user, make_ticket_category
):
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    log_in_user(participant.id)
    url = f'{BASE_URL}{ticket.id}'

    with http_client(site_app, user_id=participant.id) as client:
        own_response = client.post(url, data={f'{ticket.id}-choice': 'own'})
        provided_response = client.post(
            url, data={f'{ticket.id}-choice': 'provided'}
        )

    assert own_response.status_code == 302
    assert provided_response.status_code == 302
    answer = chair_optout_service.get_optout(party.id, ticket.id)
    assert answer is not None
    assert answer.brings_own_chair is False


def test_ticket_owner_cannot_submit_for_current_participant(
    site_app, site, party, make_user, make_ticket_category
):
    owner = make_user(generate_token())
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, owner, user=participant
    )
    log_in_user(owner.id)
    url = f'{BASE_URL}{ticket.id}'

    with http_client(site_app, user_id=owner.id) as client:
        response = client.post(url, data={f'{ticket.id}-choice': 'own'})

    assert response.status_code == 403
    assert chair_optout_service.get_optout(party.id, ticket.id) is None


def _translate(app, message: str) -> str:
    with app.test_request_context():
        with force_locale(app.config['LOCALE']):
            return gettext(message)
