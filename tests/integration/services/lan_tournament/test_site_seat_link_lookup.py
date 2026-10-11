from datetime import datetime, UTC

import pytest

from byceps.database import db
from byceps.services.lan_tournament import tournament_participant_service
from byceps.services.lan_tournament.tournament_personal_repository import (
    ContactSeat,
)
from byceps.services.seating.dbmodels.area import DbSeatingArea
from byceps.services.seating.dbmodels.seat import DbSeat
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.util.uuid import generate_uuid7


pytestmark = pytest.mark.usefixtures('admin_app')


def make_area(party_id):
    slug = str(generate_uuid7())
    area = DbSeatingArea(generate_uuid7(), party_id, slug, f'Hall {slug}')
    db.session.add(area)
    return area


def make_seat(category, label, *, area=None):
    if area is None:
        area = make_area(category.party_id)
    seat = DbSeat(generate_uuid7(), area.id, category.id, label=label)
    seat.area = area
    db.session.add(seat)
    return seat


def make_ticket(category, owner, user, seat=None, *, revoked=False):
    ticket = DbTicket(
        generate_uuid7(),
        datetime.now(UTC),
        category,
        str(generate_uuid7()),
        owner.id,
        used_by_id=user.id if user else None,
        revoked=revoked,
    )
    ticket.occupied_seat_id = seat.id if seat else None
    db.session.add(ticket)
    return ticket


def get_seat_links(user_ids, party):
    return tournament_participant_service.get_seat_links_for_users(
        user_ids, party.id
    )


def test_lookup_returns_seat_objects_with_area_slug(
    make_party, brand, make_user, make_ticket_category
):
    party = make_party(brand)
    category = make_ticket_category(party.id, 'Seat links')
    owner, user = make_user(), make_user()
    seat_b = make_seat(category, 'B12')
    seat_a = make_seat(category, 'A17', area=seat_b.area)
    make_ticket(category, owner, user, seat_b)
    make_ticket(category, owner, user, seat_a)
    db.session.commit()

    seat_links = get_seat_links({user.id}, party)

    assert set(seat_links) == {user.id}
    assert [s.label for s in seat_links[user.id]] == ['A17', 'B12']
    for seat in seat_links[user.id]:
        assert isinstance(seat, ContactSeat)
        assert seat.area.slug == seat_b.area.slug
    assert {s.id for s in seat_links[user.id]} == {seat_a.id, seat_b.id}


def test_lookup_omits_user_without_seat(
    make_party, brand, make_user, make_ticket_category
):
    party = make_party(brand)
    category = make_ticket_category(party.id, 'Seat links')
    owner = make_user()
    seated, unseated, ticketless = make_user(), make_user(), make_user()
    make_ticket(category, owner, seated, make_seat(category, 'A01'))
    make_ticket(category, owner, unseated)
    db.session.commit()

    seat_links = get_seat_links({seated.id, unseated.id, ticketless.id}, party)

    assert set(seat_links) == {seated.id}
    assert get_seat_links(set(), party) == {}


def test_lookup_ignores_revoked_ticket(
    make_party, brand, make_user, make_ticket_category
):
    party = make_party(brand)
    category = make_ticket_category(party.id, 'Seat links')
    owner, only_revoked, mixed = make_user(), make_user(), make_user()
    area = make_area(party.id)
    make_ticket(
        category,
        owner,
        only_revoked,
        make_seat(category, 'R01', area=area),
        revoked=True,
    )
    make_ticket(
        category,
        owner,
        mixed,
        make_seat(category, 'R02', area=area),
        revoked=True,
    )
    make_ticket(category, owner, mixed, make_seat(category, 'L01', area=area))
    db.session.commit()

    seat_links = get_seat_links({only_revoked.id, mixed.id}, party)

    assert set(seat_links) == {mixed.id}
    assert [s.label for s in seat_links[mixed.id]] == ['L01']


def test_lookup_is_scoped_to_the_party(
    make_party, brand, make_user, make_ticket_category
):
    party, foreign = make_party(brand), make_party(brand)
    category = make_ticket_category(party.id, 'Seat links')
    foreign_category = make_ticket_category(foreign.id, 'Foreign seat links')
    owner, user, foreign_user = make_user(), make_user(), make_user()
    make_ticket(category, owner, user, make_seat(category, 'A01'))
    make_ticket(
        foreign_category,
        owner,
        user,
        make_seat(foreign_category, 'F01'),
    )
    make_ticket(
        foreign_category,
        owner,
        foreign_user,
        make_seat(foreign_category, 'F02'),
    )
    db.session.commit()

    seat_links = get_seat_links({user.id, foreign_user.id}, party)
    foreign_seat_links = get_seat_links({user.id, foreign_user.id}, foreign)

    assert set(seat_links) == {user.id}
    assert [s.label for s in seat_links[user.id]] == ['A01']
    assert set(foreign_seat_links) == {user.id, foreign_user.id}
    assert [s.label for s in foreign_seat_links[user.id]] == ['F01']
