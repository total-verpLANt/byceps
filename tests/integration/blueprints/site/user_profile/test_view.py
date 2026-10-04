"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.ticketing import ticket_creation_service

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


def test_own_profile_shows_current_chair_state_and_edit_action(
    site_app, site, party, user, make_ticket_category
):
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(category, user, user=user)
    unanswered_ticket = ticket_creation_service.create_ticket(
        category, user, user=user
    )
    chair_optout_service.set_optout(party.id, ticket.id, user.id, False)
    log_in_user(user.id)

    response = request_profile(site_app, user.id, current_user_id=user.id)
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert ticket.code in text
    assert unanswered_ticket.code in text
    assert translate(site_app, 'no seat') in text
    assert translate(site_app, 'Needs a provided chair') in text
    assert translate(site_app, 'Make selection') in text
    assert translate(site_app, 'Change selection') in text
    assert '/chair_optout/#ticket-' in text


def test_other_profile_does_not_show_chair_edit_action(
    site_app, site, party, user, make_user, make_ticket_category
):
    other_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket_creation_service.create_ticket(category, other_user, user=other_user)
    log_in_user(user.id)

    response = request_profile(site_app, other_user.id, current_user_id=user.id)
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert translate(site_app, 'Make selection') not in text
    assert translate(site_app, 'Change selection') not in text
    assert '/chair_optout/' not in text


# helpers


def request_profile(app, user_id, *, current_user_id=None):
    url = f'http://www.acmecon.test/users/{user_id}'

    with http_client(app, user_id=current_user_id) as client:
        return client.get(url)


def translate(app, message: str) -> str:
    with app.test_request_context():
        return gettext(message)
