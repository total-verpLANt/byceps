"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from types import SimpleNamespace

from flask_babel import gettext
from markupsafe import escape
import pytest

from byceps.services.chair_optout import chair_setting_service
from byceps.services.chair_optout.blueprints.admin import views
from byceps.services.party import party_setting_service
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.site import site_service
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_revocation_service,
    ticket_seat_management_service,
    ticket_user_management_service,
)
from byceps.services.ticketing.models.ticket import ChairSource

from tests.helpers import generate_token, log_in_user


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


def test_rental_selection_requires_seating_view(
    unauthorized_chair_admin_client, party
):
    response = unauthorized_chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/rental_selection'
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


def test_graphical_plan_uses_primary_site_seating_stylesheet(
    chair_admin_client, party
):
    party_setting_service.create_or_update_setting(
        party.id, 'primary_party_site_id', 'totalverplant'
    )

    try:
        response = chair_admin_client.get(
            f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
        )
        html = response.get_data(as_text=True)
        site_stylesheet = '/static_sites/totalverplant/style/seating.css'

        assert response.status_code == 200
        assert (
            html.index('/static/style/seating.css')
            < html.index(site_stylesheet)
            < html.index('style/chair_optout.css')
        )
    finally:
        party_setting_service.remove_setting(party.id, 'primary_party_site_id')


def test_graphical_plan_uses_unique_party_site_seating_stylesheet(
    chair_admin_client, party, monkeypatch, tmp_path
):
    site_id = 'custom-site'
    stylesheet_path = tmp_path / site_id / 'static/style/seating.css'
    stylesheet_path.parent.mkdir(parents=True)
    stylesheet_path.touch()
    monkeypatch.setattr(views, 'SITES_PATH', tmp_path)
    monkeypatch.setattr(
        site_service,
        'get_all_sites',
        lambda: [SimpleNamespace(id=site_id, party_id=party.id)],
    )

    response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )

    assert response.status_code == 200
    assert f'/static_sites/{site_id}/style/seating.css' in response.get_data(
        as_text=True
    )


def test_graphical_plan_does_not_guess_between_site_stylesheets(
    chair_admin_client, party, monkeypatch, tmp_path
):
    site_ids = ['custom-site-1', 'custom-site-2']
    for site_id in site_ids:
        stylesheet_path = tmp_path / site_id / 'static/style/seating.css'
        stylesheet_path.parent.mkdir(parents=True)
        stylesheet_path.touch()
    monkeypatch.setattr(views, 'SITES_PATH', tmp_path)
    monkeypatch.setattr(
        site_service,
        'get_all_sites',
        lambda: [
            SimpleNamespace(id=site_id, party_id=party.id)
            for site_id in site_ids
        ],
    )

    response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert all(f'/static_sites/{site_id}/' not in html for site_id in site_ids)


def test_graphical_plan_ignores_noncanonical_site_stylesheet_path(
    chair_admin_client, party
):
    party_setting_service.create_or_update_setting(
        party.id, 'primary_party_site_id', '../sites/totalverplant'
    )

    try:
        response = chair_admin_client.get(
            f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
        )

        assert response.status_code == 200
        assert '/static_sites/' not in response.get_data(as_text=True)
    finally:
        party_setting_service.remove_setting(party.id, 'primary_party_site_id')


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
    rental_seat = seat_service.create_seat(
        second_area.id, 41, 42, category.id, label='B-4'
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
    rental_ticket = ticket_creation_service.create_ticket(
        category, second_user, user=second_user
    )
    revoked_ticket = ticket_creation_service.create_ticket(
        category, first_user, user=first_user
    )
    ticket_revocation_service.revoke_ticket(revoked_ticket.id, first_user)
    ticket_seat_management_service.occupy_seat(
        own_ticket.id, first_seat.id, first_user
    ).unwrap()
    ticket_seat_management_service.occupy_seat(
        provided_ticket.id, second_seat.id, second_user
    ).unwrap()
    ticket_seat_management_service.occupy_seat(
        unanswered_ticket.id, third_seat.id, third_user
    ).unwrap()
    ticket_seat_management_service.occupy_seat(
        rental_ticket.id, rental_seat.id, second_user
    ).unwrap()
    ticket_seat_management_service.set_chair_source(
        own_ticket.id, ChairSource.user, first_user
    ).unwrap()
    ticket_seat_management_service.set_chair_source(
        provided_ticket.id, ChairSource.venue, second_user
    ).unwrap()
    ticket_seat_management_service.set_chair_source(
        own_no_seat_ticket.id, ChairSource.user, first_user
    ).unwrap()
    ticket_seat_management_service.set_chair_source(
        rental_ticket.id, ChairSource.rental, second_user
    ).unwrap()

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
    assert revoked_ticket.code not in text
    assert rental_ticket.code in text
    assert 'filter=rented_chair' in text
    assert _translate(chair_admin_client, 'rented') in text
    assert _translate(chair_admin_client, 'Brings own chair') in text
    assert _translate(chair_admin_client, 'Needs a provided chair') in text
    assert _translate(chair_admin_client, 'Not specified yet') in text
    assert _translate(chair_admin_client, 'no seat') in text
    assert 'First area' not in text
    assert text.count(f'href="/users/{first_user.id}"') == 2
    assert text.count(f'href="/users/{second_user.id}"') == 2
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
    assert 'chair_information/seating_plan?filter=all' in filtered_text
    assert 'chair_information/seating_plan?filter=no_seat' not in filtered_text

    rental_response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information?filter=rented_chair'
    )
    rental_text = rental_response.get_data(as_text=True)
    assert rental_response.status_code == 200
    assert rental_ticket.code in rental_text
    assert provided_ticket.code not in rental_text
    assert own_ticket.code not in rental_text

    seating_response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )
    seating_text = seating_response.get_data(as_text=True)
    assert seating_response.status_code == 200
    assert 'First area' in seating_text
    assert 'Second area' in seating_text
    assert 'left: 11px; top: 12px;' in seating_text
    assert 'rotate(45deg)' in seating_text
    seat_markups = {
        seat.id: seating_text.split(f'id="seat-{seat.id}"', 1)[1].split(
            '</div>', 1
        )[0]
        for seat in (first_seat, second_seat, third_seat, rental_seat)
    }
    assert 'seat--own-chair' in seat_markups[first_seat.id]
    assert 'seat--own-chair' not in seat_markups[second_seat.id]
    assert 'seat--own-chair' not in seat_markups[third_seat.id]
    assert 'seat--chair-venue' in seat_markups[second_seat.id]
    assert 'seat--chair-unknown' in seat_markups[third_seat.id]
    assert 'seat--chair-rental' in seat_markups[rental_seat.id]
    assert 'seat--filter-dimmed' not in seat_markups[first_seat.id]
    assert f'data-seat-id="{first_seat.id}"' in seating_text
    assert 'data-occupier-name=' in seating_text
    for chair_information in [
        'Brings own chair',
        'Needs a provided chair',
        'Not specified yet',
        'Rental chair',
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

    assert 'behavior/chair_optout.js' in seating_text
    assert 'behavior/seating.js' not in seating_text
    assert 'filter=no_seat' not in seating_text
    legacy_plan = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan?filter=no_seat'
    ).get_data(as_text=True)
    assert 'seat--filter-dimmed' not in legacy_plan
    assert 'filter=no_seat' not in legacy_plan
    provided_plan = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan?filter=provided_chair'
    ).get_data(as_text=True)
    for seat in (first_seat, second_seat, third_seat, rental_seat):
        markup = provided_plan.split(f'id="seat-{seat.id}"', 1)[1].split(
            '</div>', 1
        )[0]
        assert ('seat--filter-dimmed' in markup) is (seat.id != second_seat.id)
    assert 'left: 11px; top: 12px;' in provided_plan
    assert 'rotate(45deg)' in provided_plan

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
    assert revoked_ticket.code not in csv_text
    assert rental_ticket.code in csv_text
    assert _translate(chair_admin_client, 'rented') in csv_text


def test_reassigned_ticket_source_does_not_highlight_seat(
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
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.user, previous_user
    ).unwrap()
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


def test_graphical_plan_keeps_unlabeled_and_escaped_seats_intact(
    chair_admin_client,
    party,
    make_user,
    make_ticket_category,
):
    user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    area = seating_area_service.create_area(
        party.id,
        generate_token(),
        'Unlabeled seats',
        image_filename='unlabeled.png',
        image_width=320,
        image_height=240,
    )
    seat = seat_service.create_seat(area.id, 10, 10, category.id, label=None)
    special_label = '<img src=x onerror="alert(1)">'
    special_seat = seat_service.create_seat(
        area.id, 40, 40, category.id, label=special_label
    )
    ticket = ticket_creation_service.create_ticket(category, user, user=user)
    ticket_seat_management_service.occupy_seat(
        ticket.id, seat.id, user
    ).unwrap()
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.user, user
    ).unwrap()

    response = chair_admin_client.get(
        f'/chair_optout/for_party/{party.id}/chair_information/seating_plan'
    )
    html = response.get_data(as_text=True)
    seat_tag = html.split(f'<div id="seat-{seat.id}"', 1)[1].split('>', 1)[0]
    special_seat_tag = html.split(f'<div id="seat-{special_seat.id}"', 1)[
        1
    ].split('>', 1)[0]

    assert response.status_code == 200
    assert (
        f'data-label="{_translate(chair_admin_client, "unnamed")}"' in seat_tag
    )
    assert f'data-ticket-id="{ticket.id}"' in seat_tag
    assert 'data-tooltip-note=' in seat_tag
    assert 'seat--own-chair' in html.split(seat_tag, 1)[1].split('</div>', 1)[0]
    assert f'data-label="{escape(special_label)}"' in special_seat_tag
    assert special_label not in special_seat_tag


def test_rental_switch_requires_write_permission(chair_admin_client, party):
    url = (
        f'/chair_optout/for_party/{party.id}/chair_information/rental_selection'
    )
    response = chair_admin_client.post(url, data={'enabled': 'true'})
    assert response.status_code == 403
    assert not chair_setting_service.is_rental_selection_enabled(party.id)
    response = chair_admin_client.get(url)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _translate(chair_admin_client, 'Rental chair selection') in html
    assert 'name="enabled"' not in html
    assert 'data-rental-selection-form' not in html
    assert _translate(chair_admin_client, 'OFF') in html


@pytest.mark.parametrize('invalid', ['invalid', '', None])
def test_rental_switch_validation_preserves_setting(
    admin_app, make_admin, make_client, party, invalid
):
    admin = make_admin({'admin.access', 'seating.view', 'party.update'})
    log_in_user(admin.id)
    client = make_client(admin_app, user_id=admin.id)
    response = client.post(
        f'/chair_optout/for_party/{party.id}/chair_information/rental_selection',
        data={'enabled': invalid} if invalid is not None else {},
    )
    assert response.status_code == 400
    assert not chair_setting_service.is_rental_selection_enabled(party.id)


def test_rental_switch_is_party_local_and_keeps_reports(
    admin_app,
    make_admin,
    make_client,
    party,
    brand,
    make_party,
    make_user,
    make_ticket_category,
):
    admin = make_admin({'admin.access', 'seating.view', 'party.update'})
    log_in_user(admin.id)
    client = make_client(admin_app, user_id=admin.id)
    other_party = make_party(brand)
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    url = (
        f'/chair_optout/for_party/{party.id}/chair_information/rental_selection'
    )
    try:
        list_html = client.get(
            f'/chair_optout/for_party/{party.id}/chair_information'
        ).get_data(as_text=True)
        assert 'name="enabled"' not in list_html
        html = client.get(url).get_data(as_text=True)
        assert 'name="enabled"' in html
        assert 'data-enabled="false"' in html
        response = client.post(url, data={'enabled': 'true'})
        assert response.status_code == 302
        assert response.location.endswith(url)
        assert chair_setting_service.is_rental_selection_enabled(party.id)
        assert not chair_setting_service.is_rental_selection_enabled(
            other_party.id
        )
        ticket_seat_management_service.set_chair_source(
            ticket.id, ChairSource.rental, participant
        ).unwrap()
        assert client.post(url, data={'enabled': 'false'}).status_code == 302
        assert not chair_setting_service.is_rental_selection_enabled(party.id)
        for suffix in ['chair_information?filter=rented_chair', 'export.csv']:
            response = client.get(
                f'/chair_optout/for_party/{party.id}/{suffix}'
            )
            assert response.status_code == 200
            assert ticket.code in response.get_data(as_text=True)
            assert _translate(client, 'rented') in response.get_data(
                as_text=True
            )
    finally:
        chair_setting_service.set_rental_selection_enabled(party.id, False)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('has_rental', [False, True])
def test_rental_visibility_matrix(
    chair_admin_client,
    brand,
    make_party,
    make_user,
    make_ticket_category,
    enabled,
    has_rental,
):
    party = make_party(brand)
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    area = seating_area_service.create_area(
        party.id,
        generate_token(),
        'Rental matrix',
        image_filename='matrix.png',
        image_width=320,
        image_height=240,
    )
    seat = seat_service.create_seat(area.id, 10, 20, category.id, rotation=45)
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    ticket_seat_management_service.occupy_seat(
        ticket.id, seat.id, participant
    ).unwrap()
    if has_rental:
        ticket_seat_management_service.set_chair_source(
            ticket.id, ChairSource.rental, participant
        ).unwrap()
    chair_setting_service.set_rental_selection_enabled(party.id, enabled)
    visible = enabled or has_rental
    base = f'/chair_optout/for_party/{party.id}/chair_information'
    for suffix in ['', '/seating_plan']:
        response = chair_admin_client.get(base + suffix)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert ('filter=rented_chair' in html) is visible
        assert 'name="enabled"' not in html
        for tab in [
            'Participant list',
            'Graphical seating plan',
            'Rental chair selection',
        ]:
            assert _translate(chair_admin_client, tab) in html
        filtered = chair_admin_client.get(
            base + suffix + '?filter=rented_chair'
        ).get_data(as_text=True)
        if suffix:
            assert ('seat--chair-rental" aria-hidden="true"' in html) is visible
            markup = html.split(f'id="seat-{seat.id}"', 1)[1].split(
                '</div>', 1
            )[0]
            assert ('seat--chair-rental' in markup) is has_rental
            filtered_markup = filtered.split(f'id="seat-{seat.id}"', 1)[
                1
            ].split('</div>', 1)[0]
            assert ('seat--filter-dimmed' in filtered_markup) is not has_rental
            assert 'rotate(45deg)' in markup
        else:
            assert (ticket.code in filtered) is has_rental
        assert ticket.code in chair_admin_client.get(base).get_data(
            as_text=True
        )


def test_rental_selection_unknown_party(chair_admin_client):
    response = chair_admin_client.get(
        '/chair_optout/for_party/does-not-exist/chair_information/rental_selection'
    )
    assert response.status_code == 404


def _translate(client, message: str) -> str:
    with client.application.test_request_context():
        return gettext(message)
