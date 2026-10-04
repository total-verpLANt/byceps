"""
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from sqlalchemy import func, select

from byceps.database import db
from byceps.services.chair_optout import chair_optout_service
from byceps.services.chair_optout.dbmodels import DbPartyTicketChairOptout
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
    ticket_user_management_service,
)

from tests.helpers import generate_token


def test_answer_without_seat_is_reported(
    admin_app, party, user, make_ticket_category
):
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(category, user, user=user)

    answer = chair_optout_service.set_optout(
        party.id, ticket.id, user.id, False
    )
    entries = chair_optout_service.get_report_entries_for_party(party.id)

    assert answer.brings_own_chair is False
    entry = next(entry for entry in entries if entry.ticket_id == ticket.id)
    assert entry.has_seat is False
    assert entry.brings_own_chair is False


def test_repeated_save_updates_same_row(
    admin_app, party, user, make_ticket_category
):
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(category, user, user=user)

    first = chair_optout_service.set_optout(party.id, ticket.id, user.id, True)
    second = chair_optout_service.set_optout(
        party.id, ticket.id, user.id, False
    )

    assert first.id == second.id
    assert second.brings_own_chair is False
    assert (
        db.session.scalar(
            select(func.count())
            .select_from(DbPartyTicketChairOptout)
            .filter_by(party_id=party.id, ticket_id=ticket.id)
        )
        == 1
    )


def test_unanswered_ticket_prompt_clears_after_answer(
    admin_app, party, make_user, make_ticket_category
):
    participant = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(
        category, participant, user=participant
    )

    assert (
        chair_optout_service.find_first_unanswered_ticket_id_for_user(
            party.id, participant.id
        )
        == ticket.id
    )

    chair_optout_service.set_optout(party.id, ticket.id, participant.id, False)

    assert (
        chair_optout_service.find_first_unanswered_ticket_id_for_user(
            party.id, participant.id
        )
        is None
    )


def test_seat_change_preserves_answer(
    admin_app, party, user, make_ticket_category
):
    category = make_ticket_category(party.id, generate_token())
    area = seating_area_service.create_area(
        party.id, generate_token(), generate_token()
    )
    first_seat = seat_service.create_seat(
        area.id, 1, 2, category.id, label='A-1'
    )
    second_seat = seat_service.create_seat(
        area.id, 3, 4, category.id, label='B-2'
    )
    ticket = ticket_creation_service.create_ticket(category, user, user=user)
    chair_optout_service.set_optout(party.id, ticket.id, user.id, True)

    ticket_seat_management_service.occupy_seat(
        ticket.id, first_seat.id, user
    ).unwrap()
    ticket_seat_management_service.occupy_seat(
        ticket.id, second_seat.id, user
    ).unwrap()

    answer = chair_optout_service.get_optout(party.id, ticket.id)
    assert answer is not None
    assert answer.brings_own_chair is True


def test_reassignment_invalidates_and_replaces_previous_answer(
    admin_app, party, user, make_user, make_ticket_category
):
    new_user = make_user(generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(category, user, user=user)
    chair_optout_service.set_optout(party.id, ticket.id, user.id, True)

    ticket_user_management_service.appoint_user(
        ticket.id, new_user, user
    ).unwrap()

    assert chair_optout_service.get_optout(party.id, ticket.id) is None
    entry = next(
        entry
        for entry in chair_optout_service.get_report_entries_for_party(party.id)
        if entry.ticket_id == ticket.id
    )
    assert entry.brings_own_chair is None

    answer = chair_optout_service.set_optout(
        party.id, ticket.id, new_user.id, False
    )
    assert answer.user_id == new_user.id
    assert answer.brings_own_chair is False


def test_set_answer_enforces_party_boundary(
    admin_app, party, brand, user, make_party, make_ticket_category
):
    other_party = make_party(brand, title=generate_token())
    category = make_ticket_category(party.id, generate_token())
    ticket = ticket_creation_service.create_ticket(category, user, user=user)

    with pytest.raises(ValueError):
        chair_optout_service.set_optout(
            other_party.id, ticket.id, user.id, True
        )
