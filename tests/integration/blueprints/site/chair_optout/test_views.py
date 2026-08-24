"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import force_locale, gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.ticketing import ticket_creation_service

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
