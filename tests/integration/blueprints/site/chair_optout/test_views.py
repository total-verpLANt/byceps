"""
:License: Revised BSD (see `LICENSE` file for details)
"""

import re

import pytest
from flask import url_for
from flask_babel import force_locale, gettext
from sqlalchemy import select
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.chair_optout import chair_setting_service
from byceps.services.party.dbmodels import DbParty
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.seating.blueprints.site import views as seating_views
from byceps.services.site.models import SiteID
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
    ticket_user_management_service,
)
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.log import ticket_log_service
from byceps.services.ticketing.log.dbmodels import DbTicketLogEntry
from byceps.services.ticketing.models.ticket import ChairSource
from byceps.util.templating import create_site_template_loader

from tests.helpers import (
    generate_token,
    generate_uuid,
    http_client,
    log_in_user,
)
from tests.helpers.shop import place_order


BASE_URL = 'http://www.acmecon.test'


@pytest.fixture(autouse=True)
def enable_ticket_management(admin_app, party):
    db_party = db.session.get(DbParty, party.id)
    previous_value = db_party.ticket_management_enabled
    db_party.ticket_management_enabled = True
    db.session.commit()
    yield
    db_party.ticket_management_enabled = previous_value
    db.session.commit()


@pytest.fixture
def theme_app(make_site_app, site):
    app = make_site_app('www.acmecon.test', site.id)
    app.jinja_loader = create_site_template_loader(SiteID('totalverplant-36'))
    with app.app_context():
        yield app


@pytest.fixture
def make_chair_ticket(
    make_brand,
    make_shop,
    make_order_number_sequence,
    make_storefront,
    make_product,
    make_orderer,
):
    """Provide the order association expected by the standard ticket page."""
    shop = make_shop(make_brand())
    sequence = make_order_number_sequence(shop.id)
    storefront = make_storefront(shop.id, sequence.id)
    product = make_product(shop.id)

    def create(category, owner, participant):
        order = place_order(
            shop, storefront, make_orderer(owner), [(product, 1)]
        )
        return ticket_creation_service.create_ticket(
            category, owner, user=participant, order_number=order.order_number
        )

    return create


def test_legacy_index_requires_login(site_app, site):
    with http_client(site_app) as client:
        response = client.get(f'{BASE_URL}/chair_optout/')

    assert response.status_code == 302


@pytest.mark.parametrize('target', ['own', 'foreign', 'revoked', 'invalid'])
def test_legacy_redirect_targets_unique_rendered_standard_ticket_anchor(
    site_app,
    site,
    party,
    make_user,
    make_ticket_category,
    make_chair_ticket,
    target,
):
    participant = make_user(generate_token())
    other_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    tickets = [
        make_chair_ticket(category, participant, participant) for _ in range(2)
    ]
    foreign = make_chair_ticket(category, other_user, other_user)
    revoked = make_chair_ticket(category, participant, participant)
    revoked.revoked = True
    db.session.commit()
    log_in_user(participant.id)
    target_id = {
        'own': tickets[1].id,
        'foreign': foreign.id,
        'revoked': revoked.id,
        'invalid': 'invalid',
    }[target]
    with http_client(site_app, user_id=participant.id) as client:
        redirect = client.get(
            f'{BASE_URL}/chair_optout/',
            query_string={'ticket_id': str(target_id)},
        )
        response = client.get(redirect.location)

    assert redirect.status_code == 302
    assert response.status_code == 200
    assert redirect.location.endswith(
        f'/tickets/mine#ticket-{tickets[1].id}'
        if target == 'own'
        else '/tickets/mine'
    )
    html = response.get_data(as_text=True)
    ids = re.findall(r'<li id="(ticket-[^"]+)">', html)
    assert set(ids) == {f'ticket-{ticket.id}' for ticket in tickets}
    assert len(ids) == len(set(ids)) == 2
    if target == 'own':
        anchor = redirect.location.split('#', 1)[1]
        assert html.count(f'id="{anchor}"') == 1
        assert re.search(
            rf'<li id="{anchor}">.*?<span class="ticket-code">{tickets[1].code}</span>',
            html,
            re.DOTALL,
        )


@pytest.mark.parametrize('ticket_id_arg', ['own', 'foreign', 'invalid', None])
def test_legacy_index_redirects_with_only_valid_participant_anchor(
    site_app, site, party, make_user, make_ticket_category, ticket_id_arg
):
    participant = make_user(generate_token())
    other_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    foreign_ticket = ticket_creation_service.create_ticket(
        category, other_user, user=other_user
    )
    log_in_user(participant.id)
    query = {
        'own': str(ticket.id),
        'foreign': str(foreign_ticket.id),
        'invalid': 'invalid',
        None: None,
    }[ticket_id_arg]

    with http_client(site_app, user_id=participant.id) as client:
        response = client.get(
            f'{BASE_URL}/chair_optout/',
            query_string={'ticket_id': query} if query else None,
        )
        obsolete_response = client.post(f'{BASE_URL}/chair_optout/{ticket.id}')
        obsolete_index_response = client.post(f'{BASE_URL}/chair_optout/')

    assert response.status_code == 302
    assert response.location.endswith(
        f'/tickets/mine#ticket-{ticket.id}'
        if ticket_id_arg == 'own'
        else '/tickets/mine'
    )
    assert obsolete_response.status_code == 405
    assert obsolete_index_response.status_code == 405


def test_gv36_ticket_controls_offer_only_two_choices(
    theme_app, party, make_user, make_ticket_category
):
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.rental, participant
    ).unwrap()
    log_in_user(participant.id)

    with http_client(theme_app, user_id=participant.id) as client:
        response = client.get(f'{BASE_URL}/tickets/mine')

    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f'id="ticket-{ticket.id}"' in text
    assert ticket.code in text
    assert 'class="bote-page tickets-page"' in text
    assert _translate(theme_app, 'rented') in text
    for source in ['user', 'venue']:
        assert f' href="{_chair_url(theme_app, ticket.id, source)}"' in text
    assert f' href="{_chair_url(theme_app, ticket.id, "rental")}"' not in text
    assert f' href="{_chair_url(theme_app, ticket.id, "unknown")}"' not in text
    assert text.count(' data-action="set-chair-source"') == 2
    assert 'behavior/chair-information.js' in text
    assert 'chair-submit' not in text


def test_current_participant_stores_both_choices_in_core(
    site_app, site, party, make_user, make_ticket_category
):
    participant = make_user(generate_token())
    owner = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, owner, user=participant
    )
    log_in_user(participant.id)

    with http_client(site_app, user_id=participant.id) as client:
        for source, expected in [
            ('user', ChairSource.user),
            ('venue', ChairSource.venue),
        ]:
            response = client.post(_chair_url(site_app, ticket.id, source))
            assert response.status_code == 204
            assert _get_persisted_chair_source(ticket.id) is expected

    with Session(db.engine) as session:
        entries = session.scalars(
            select(DbTicketLogEntry)
            .filter_by(ticket_id=ticket.id, event_type='chair-source-set')
            .order_by(DbTicketLogEntry.occurred_at, DbTicketLogEntry.id)
        ).all()
        assert [entry.data for entry in entries] == [
            {'chair_source': source, 'initiator_id': str(participant.id)}
            for source in ['user', 'venue']
        ]


@pytest.mark.parametrize('source', ['invalid', 'own', 'provided', 'rental'])
def test_invalid_source_preserves_core_answer(
    site_app, site, party, make_user, make_ticket_category, source
):
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.user, participant
    ).unwrap()
    entries_before = ticket_log_service.get_entries_for_ticket(ticket.id)
    log_in_user(participant.id)

    with http_client(site_app, user_id=participant.id) as client:
        response = client.post(_chair_url(site_app, ticket.id, source))

    assert response.status_code == 400
    assert _get_persisted_chair_source(ticket.id) is ChairSource.user
    assert (
        ticket_log_service.get_entries_for_ticket(ticket.id) == entries_before
    )


@pytest.mark.parametrize(
    'scenario',
    ['foreign_user', 'foreign_party', 'revoked', 'checked_in', 'disabled'],
)
def test_ineligible_update_is_denied_without_writes(
    theme_app,
    party,
    brand,
    make_party,
    make_user,
    make_ticket_category,
    scenario,
):
    participant = make_user(generate_token())
    other_user = make_user(generate_token())
    ticket_party = make_party(brand) if scenario == 'foreign_party' else party
    category = make_ticket_category(ticket_party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category,
        participant,
        user=other_user if scenario == 'foreign_user' else participant,
    )
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.venue, participant
    ).unwrap()
    if scenario == 'revoked':
        ticket.revoked = True
    elif scenario == 'foreign_user':
        ticket.user_managed_by_id = other_user.id
    elif scenario == 'checked_in':
        ticket.user_checked_in = True
    elif scenario == 'disabled':
        db.session.get(DbParty, party.id).ticket_management_enabled = False
    db.session.commit()
    entries_before = ticket_log_service.get_entries_for_ticket(ticket.id)
    log_in_user(participant.id)

    with http_client(theme_app, user_id=participant.id) as client:
        response = client.post(_chair_url(theme_app, ticket.id, 'user'))
        tickets_response = client.get(f'{BASE_URL}/tickets/mine')

    assert response.status_code in {403, 404}
    assert _get_persisted_chair_source(ticket.id) is ChairSource.venue
    assert (
        ticket_log_service.get_entries_for_ticket(ticket.id) == entries_before
    )
    assert tickets_response.status_code == 200
    assert ' data-action="set-chair-source"' not in tickets_response.get_data(
        as_text=True
    )


def test_unknown_ticket_is_not_found(site_app, site, make_user):
    participant = make_user(generate_token())
    log_in_user(participant.id)
    with http_client(site_app, user_id=participant.id) as client:
        response = client.post(_chair_url(site_app, generate_uuid(), 'user'))
    assert response.status_code == 404


def test_core_chair_update_requires_login(
    site_app, site, party, make_user, make_ticket_category
):
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )
    with http_client(site_app) as client:
        response = client.post(_chair_url(site_app, ticket.id, 'user'))
    assert response.status_code == 302
    assert _get_persisted_chair_source(ticket.id) is ChairSource.unknown
    assert not ticket_log_service.get_entries_for_ticket(ticket.id)


def test_gv36_seating_reminder_tracks_own_ticket_while_managing_others(
    theme_app, party, make_user, make_ticket_category, monkeypatch
):
    participant = make_user(generate_token())
    other_user = make_user(generate_token())
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
    own_ticket = ticket_creation_service.create_ticket(
        category, other_user, user=participant
    )
    managed_ticket = ticket_creation_service.create_ticket(
        category, participant, user=other_user
    )
    ticket_user_management_service.appoint_user_manager(
        managed_ticket.id, other_user, participant
    ).unwrap()
    log_in_user(participant.id)
    monkeypatch.setattr(
        seating_views, '_is_seat_management_enabled', lambda: True
    )
    url = f'{BASE_URL}/seating/areas/{area.slug}/manage_seats'

    with http_client(theme_app, user_id=participant.id) as client:
        pending_response = client.get(url)
        assert (
            client.post(
                _chair_url(theme_app, own_ticket.id, 'venue')
            ).status_code
            == 204
        )
        answered_response = client.get(url)
        ticket_user_management_service.appoint_user(
            own_ticket.id, other_user, other_user
        ).unwrap()
        ticket_user_management_service.appoint_user(
            own_ticket.id, participant, other_user
        ).unwrap()
        reset_response = client.get(url)

    for response in [pending_response, reset_response]:
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert managed_ticket.code in html
        assert 'class="block chair-prompt"' in html
        assert f'/tickets/mine#ticket-{own_ticket.id}' in html
        assert f'/tickets/mine#ticket-{managed_ticket.id}' not in html
    assert answered_response.status_code == 200
    assert 'chair-information-link"' not in answered_response.get_data(
        as_text=True
    )


def test_seat_manager_without_used_ticket_has_no_chair_reminder(
    theme_app, party, make_user, make_ticket_category, monkeypatch
):
    owner = make_user(generate_token())
    participant = make_user(generate_token())
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
    ticket_user_management_service.appoint_user_manager(
        ticket.id, participant, owner
    ).unwrap()
    log_in_user(owner.id)
    monkeypatch.setattr(
        seating_views, '_is_seat_management_enabled', lambda: True
    )
    with http_client(theme_app, user_id=owner.id) as client:
        response = client.get(
            f'{BASE_URL}/seating/areas/{area.slug}/manage_seats'
        )
    assert response.status_code == 200
    assert ticket.code in response.get_data(as_text=True)
    assert 'chair-information-link"' not in response.get_data(as_text=True)


@pytest.mark.parametrize('theme', [False, True], ids=['standard', 'gv36'])
@pytest.mark.parametrize('rental_enabled', [False, True])
def test_rental_switch_controls_choices_posts_and_preserves_existing_answer(
    site_app,
    theme_app,
    party,
    make_user,
    make_ticket_category,
    make_chair_ticket,
    theme,
    rental_enabled,
):
    app = theme_app if theme else site_app
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = make_chair_ticket(category, participant, participant)
    log_in_user(participant.id)
    chair_setting_service.set_rental_selection_enabled(party.id, rental_enabled)
    try:
        with http_client(app, user_id=participant.id) as client:
            html = client.get(f'{BASE_URL}/tickets/mine').get_data(as_text=True)
            rental_url = _chair_url(app, ticket.id, 'rental')
            assert (f'href="{rental_url}"' in html) is rental_enabled
            assert (
                f'href="{_chair_url(app, ticket.id, "unknown")}"' in html
            ) is (not theme)
            response = client.post(rental_url)
            assert response.status_code == (204 if rental_enabled else 400)
            assert _get_persisted_chair_source(ticket.id) is (
                ChairSource.rental if rental_enabled else ChairSource.unknown
            )
            if rental_enabled:
                chair_setting_service.set_rental_selection_enabled(
                    party.id, False
                )
                html = client.get(f'{BASE_URL}/tickets/mine').get_data(
                    as_text=True
                )
                assert _translate(app, 'rented') in html
                assert f'href="{rental_url}"' not in html
                assert client.post(rental_url).status_code == 400
                assert (
                    _get_persisted_chair_source(ticket.id) is ChairSource.rental
                )
            # The Core reset remains available, including when rental is OFF.
            assert (
                client.post(_chair_url(app, ticket.id, 'unknown')).status_code
                == 204
            )
            assert _get_persisted_chair_source(ticket.id) is ChairSource.unknown
        entries = [
            entry
            for entry in ticket_log_service.get_entries_for_ticket(ticket.id)
            if entry.event_type == 'chair-source-set'
        ]
        assert len(entries) == (2 if rental_enabled else 1)
        assert all(
            entry.data['initiator_id'] == str(participant.id)
            for entry in entries
        )
    finally:
        chair_setting_service.set_rental_selection_enabled(party.id, False)


@pytest.mark.parametrize('theme', [False, True], ids=['standard', 'gv36'])
@pytest.mark.parametrize(
    ('role', 'allowed'),
    [
        ('participant', True),
        ('user_manager', True),
        ('owner', True),
        ('delegated_owner', False),
        ('seat_manager', False),
        ('stranger', False),
    ],
)
def test_chair_permissions_match_core_in_both_interfaces(
    site_app,
    theme_app,
    party,
    make_user,
    make_ticket_category,
    make_chair_ticket,
    theme,
    role,
    allowed,
):
    app = theme_app if theme else site_app
    owner, participant, manager, seat_manager, stranger = [
        make_user(generate_token()) for _ in range(5)
    ]
    category = make_ticket_category(party.id, generate_token())
    ticket = make_chair_ticket(category, owner, participant)
    ticket.seat_managed_by_id = seat_manager.id
    if role != 'owner':
        ticket.user_managed_by_id = manager.id
    db.session.commit()
    actor = {
        'participant': participant,
        'user_manager': manager,
        'owner': owner,
        'delegated_owner': owner,
        'seat_manager': seat_manager,
        'stranger': stranger,
    }[role]
    log_in_user(actor.id)
    with http_client(app, user_id=actor.id) as client:
        html = client.get(f'{BASE_URL}/tickets/mine').get_data(as_text=True)
        source_url = _chair_url(app, ticket.id, 'venue')
        assert (f'href="{source_url}"' in html) is allowed
        response = client.post(source_url)
    assert response.status_code == (204 if allowed else 403)
    assert _get_persisted_chair_source(ticket.id) is (
        ChairSource.venue if allowed else ChairSource.unknown
    )
    entries = ticket_log_service.get_entries_for_ticket(ticket.id)
    assert len(entries) == (1 if allowed else 0)
    if allowed:
        assert entries[0].data == {
            'chair_source': 'venue',
            'initiator_id': str(actor.id),
        }


def _get_persisted_chair_source(ticket_id):
    # Requests commit in a separate app-scoped session from these fixtures.
    with Session(db.engine) as session:
        return session.get(DbTicket, ticket_id).chair_source


def _chair_url(app, ticket_id, source):
    with app.test_request_context():
        return url_for(
            'ticketing.set_chair_source',
            ticket_id=ticket_id,
            chair_source=source,
        )


def _translate(app, message: str) -> str:
    with app.test_request_context():
        with force_locale(app.config['LOCALE']):
            return gettext(message)
