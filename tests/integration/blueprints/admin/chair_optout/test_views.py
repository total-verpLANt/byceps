"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from flask_babel import gettext

from byceps.services.chair_optout import chair_optout_service
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
    ticket_user_management_service,
)

from tests.helpers import generate_token


def test_seat_management_requires_seating_view(
    unauthorized_chair_admin_client, party
):
    response = unauthorized_chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}'
    )
    assert response.status_code == 403

    response = unauthorized_chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )
    assert response.status_code == 403


def test_seat_management_landing_page(chair_admin_client, party):
    response = chair_admin_client.get(f'/chair_optout/for_party/{party.id}')
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert _translate(chair_admin_client, 'Seat management') in text
    assert (
        _translate(chair_admin_client, 'Participant chair information') in text
    )


def test_overview_and_csv_include_all_states_and_multiple_areas(
    chair_admin_client,
    party,
    site,
    make_user,
    make_ticket_category,
):
    first_user = make_user(generate_token())
    second_user = make_user(generate_token())
    third_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    first_area = seating_area_service.create_area(
        party.id,
        generate_token(),
        'First area',
        image_filename='first.png',
        image_width=800,
        image_height=600,
    )
    second_area = seating_area_service.create_area(
        party.id,
        generate_token(),
        'Second area',
        image_filename='second.png',
        image_width=640,
        image_height=480,
    )
    first_seat = seat_service.create_seat(
        first_area.id, 11, 12, category.id, rotation=45, label='A-1'
    )
    second_seat = seat_service.create_seat(
        second_area.id, 21, 22, category.id, label='B-2'
    )
    third_seat = seat_service.create_seat(
        first_area.id, 31, 32, category.id, label='A-3'
    )
    own_ticket = ticket_creation_service.create_ticket(
        category, first_user, user=first_user
    )
    provided_ticket = ticket_creation_service.create_ticket(
        category, second_user, user=second_user
    )
    unanswered_ticket = ticket_creation_service.create_ticket(
        category, third_user, user=third_user
    )
    own_no_seat_ticket = ticket_creation_service.create_ticket(
        category, first_user, user=first_user
    )
    unassigned_ticket = ticket_creation_service.create_ticket(
        category, first_user
    )
    ticket_seat_management_service.occupy_seat(
        own_ticket.id, first_seat.id, first_user
    ).unwrap()
    ticket_seat_management_service.occupy_seat(
        provided_ticket.id, second_seat.id, second_user
    ).unwrap()
    ticket_seat_management_service.occupy_seat(
        unanswered_ticket.id, third_seat.id, third_user
    ).unwrap()
    chair_optout_service.set_optout(
        party.id, own_ticket.id, first_user.id, True
    )
    chair_optout_service.set_optout(
        party.id, provided_ticket.id, second_user.id, False
    )
    chair_optout_service.set_optout(
        party.id, own_no_seat_ticket.id, first_user.id, True
    )

    response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information'
    )
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert own_ticket.code in text
    assert provided_ticket.code in text
    assert unanswered_ticket.code in text
    assert own_no_seat_ticket.code in text
    assert unassigned_ticket.code not in text
    assert _translate(chair_admin_client, 'Brings own chair') in text
    assert _translate(chair_admin_client, 'Needs a provided chair') in text
    assert _translate(chair_admin_client, 'Not specified yet') in text
    assert _translate(chair_admin_client, 'no seat') in text
    assert 'First area' not in text
    assert text.count(f'href="/users/{first_user.id}"') == 2
    assert text.count(f'href="/users/{second_user.id}"') == 1
    assert '<td>John Joseph Doe</td>' in text
    assert f'href="/ticketing/tickets/{own_ticket.id}"' in text
    assert (
        f'href="https://{site.server_name}/seating/areas/{first_area.slug}'
        f'#seat-{first_seat.id}"' in text
    )
    no_seat_row = text.split(own_no_seat_ticket.code, 1)[1].split('</tr>', 1)[0]
    assert '/seating/areas/' not in no_seat_row

    filtered_response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information?filter=no_seat'
    )
    filtered_text = filtered_response.get_data(as_text=True)
    assert filtered_response.status_code == 200
    assert own_no_seat_ticket.code in filtered_text
    assert unanswered_ticket.code not in filtered_text
    assert own_ticket.code not in filtered_text
    assert 'filter=no_seat' in filtered_text
    assert 'tabs-tab--current' in filtered_text

    seating_response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )
    seating_text = seating_response.get_data(as_text=True)
    assert seating_response.status_code == 200
    assert 'First area' in seating_text
    assert 'Second area' in seating_text
    assert 'left: 11px; top: 12px;' in seating_text
    assert 'rotate(45deg)' in seating_text
    assert seating_text.count('seat--own-chair') == 2
    assert f'data-seat-id="{first_seat.id}"' in seating_text
    assert 'data-occupier-name=' in seating_text
    for chair_information in [
        'Brings own chair',
        'Needs a provided chair',
        'Not specified yet',
    ]:
        assert (
            f'data-tooltip-note="{_translate(chair_admin_client, chair_information)}"'
            in seating_text
        )
    assert (
        _translate(
            chair_admin_client,
            'Green outline and dot: Brings own chair',
        )
        in seating_text
    )

    csv_response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/export.csv'
    )
    csv_text = csv_response.get_data(as_text=True)
    assert csv_response.status_code == 200
    assert _translate(chair_admin_client, 'Brings own chair') in csv_text
    assert _translate(chair_admin_client, 'Needs a provided chair') in csv_text
    assert _translate(chair_admin_client, 'Not specified yet') in csv_text
    assert _translate(chair_admin_client, 'no seat') in csv_text
    assert unassigned_ticket.code not in csv_text


def test_stale_answer_does_not_highlight_seat(
    chair_admin_client,
    party,
    make_user,
    make_ticket_category,
):
    previous_user = make_user(generate_token())
    current_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    area = seating_area_service.create_area(
        party.id,
        generate_token(),
        'Reassigned area',
        image_filename='reassigned.png',
        image_width=320,
        image_height=240,
    )
    seat = seat_service.create_seat(area.id, 31, 32, category.id, label='C-3')
    ticket = ticket_creation_service.create_ticket(
        category, previous_user, user=previous_user
    )
    ticket_seat_management_service.occupy_seat(
        ticket.id, seat.id, previous_user
    ).unwrap()
    chair_optout_service.set_optout(party.id, ticket.id, previous_user.id, True)
    ticket_user_management_service.appoint_user(
        ticket.id, current_user, previous_user
    ).unwrap()

    response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )
    text = response.get_data(as_text=True)
    seat_markup = text.split(f'id="seat-{seat.id}"', 1)[1].split('</div>', 1)[0]

    assert response.status_code == 200
    assert 'seat--occupied' in seat_markup
    assert 'seat--own-chair' not in seat_markup


def _translate(client, message: str) -> str:
    with client.application.test_request_context():
        return gettext(message)
