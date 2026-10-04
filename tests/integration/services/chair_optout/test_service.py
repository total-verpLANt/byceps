"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.chair_optout import chair_optout_service
from byceps.services.chair_optout.chair_access_service import (
    lock_participant_ticket,
)
from byceps.services.party.dbmodels import DbParty
from byceps.services.seating import seat_service, seating_area_service
from byceps.services.ticketing import (
    ticket_creation_service,
    ticket_seat_management_service,
    ticket_service,
    ticket_user_management_service,
)
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.log import (
    ticket_log_domain_service,
    ticket_log_service,
)
from byceps.services.ticketing.models.ticket import ChairSource

from tests.helpers import generate_token, http_client, log_in_user


@pytest.fixture
def ticket(admin_app, party, user, make_ticket_category):
    category = make_ticket_category(party.id, generate_token())
    return ticket_creation_service.create_ticket(category, user, user=user)


def _set_source(party_id, ticket_id, user, source):
    assert lock_participant_ticket(party_id, ticket_id, user.id) is not None
    ticket_seat_management_service.set_chair_source(
        ticket_id, source, user
    ).unwrap()


@pytest.mark.parametrize('source', list(ChairSource))
def test_report_and_compact_map_use_core_source_without_seat(
    ticket, party, user, source
):
    _set_source(party.id, ticket.id, user, source)
    entries = chair_optout_service.get_report_entries_for_party(party.id)
    entry = next(entry for entry in entries if entry.ticket_id == ticket.id)
    assert entry.chair_source is source
    assert entry.has_seat is False
    assert (
        chair_optout_service.get_chair_sources_for_party(party.id)[ticket.id]
        is source
    )


def test_pending_prompt_returns_after_core_reset(ticket, party, user):
    get_pending = chair_optout_service.get_pending_chair_ticket_ids_for_user
    assert ticket.id in get_pending(party.id, user.id)
    _set_source(party.id, ticket.id, user, ChairSource.venue)
    assert ticket.id not in get_pending(party.id, user.id)
    _set_source(party.id, ticket.id, user, ChairSource.unknown)
    assert ticket.id in get_pending(party.id, user.id)


def test_report_excludes_revoked_and_unassigned_tickets(
    admin_app, party, user, make_ticket_category
):
    category = make_ticket_category(party.id, generate_token())
    revoked = ticket_creation_service.create_ticket(category, user, user=user)
    unassigned = ticket_creation_service.create_ticket(category, user)
    revoked.revoked = True
    db.session.commit()
    sources = chair_optout_service.get_chair_sources_for_party(party.id)
    entries = chair_optout_service.get_report_entries_for_party(party.id)
    assert revoked.id not in sources
    assert unassigned.id not in sources
    assert not {revoked.id, unassigned.id} & {
        entry.ticket_id for entry in entries
    }


def test_seat_change_preserves_source(ticket, party, user):
    area = seating_area_service.create_area(
        party.id, generate_token(), generate_token()
    )
    seats = [
        seat_service.create_seat(area.id, x, 2, ticket.category_id, label=label)
        for x, label in [(1, 'A-1'), (3, 'B-2')]
    ]
    _set_source(party.id, ticket.id, user, ChairSource.user)
    for seat in seats:
        ticket_seat_management_service.occupy_seat(
            ticket.id, seat.id, user
        ).unwrap()
    assert ticket_service.get_ticket(ticket.id).chair_source is ChairSource.user
    entry = next(
        entry
        for entry in chair_optout_service.get_report_entries_for_party(party.id)
        if entry.ticket_id == ticket.id
    )
    assert entry.seat_label == 'B-2'


@pytest.mark.parametrize('withdraw', [False, True])
def test_changed_and_returning_participant_must_answer_again(
    ticket, party, user, make_user, withdraw
):
    _set_source(party.id, ticket.id, user, ChairSource.user)
    if withdraw:
        ticket_user_management_service.withdraw_user(ticket.id, user).unwrap()
    else:
        new_user = make_user(generate_token())
        ticket_user_management_service.appoint_user(
            ticket.id, new_user, user
        ).unwrap()
    assert (
        ticket_service.get_ticket(ticket.id).chair_source is ChairSource.unknown
    )
    ticket_user_management_service.appoint_user(ticket.id, user, user).unwrap()
    assert (
        ticket_service.get_ticket(ticket.id).chair_source is ChairSource.unknown
    )
    _set_source(party.id, ticket.id, user, ChairSource.venue)
    assert (
        ticket_service.get_ticket(ticket.id).chair_source is ChairSource.venue
    )


def test_reappointing_same_participant_preserves_source(ticket, party, user):
    _set_source(party.id, ticket.id, user, ChairSource.user)
    ticket_user_management_service.appoint_user(ticket.id, user, user).unwrap()
    assert ticket_service.get_ticket(ticket.id).chair_source is ChairSource.user


def test_rollback_of_participant_change_restores_source(
    ticket, party, user, make_user
):
    new_user = make_user(generate_token())
    _set_source(party.id, ticket.id, user, ChairSource.user)
    ticket.used_by_id = new_user.id
    db.session.flush()
    assert ticket.chair_source is ChairSource.unknown
    db.session.rollback()
    assert ticket.used_by_id == user.id
    assert ticket.chair_source is ChairSource.user
    with Session(db.engine) as independent:
        assert (
            independent.scalar(
                select(DbTicket)
                .where(DbTicket.id == ticket.id)
                .with_for_update(nowait=True)
            )
            is not None
        )


def test_access_guard_holds_lock_through_core_commit(ticket, party, user):
    assert lock_participant_ticket(party.id, ticket.id, user.id) is ticket
    with Session(db.engine) as independent:
        with pytest.raises(OperationalError) as error:
            independent.execute(
                select(DbTicket)
                .where(DbTicket.id == ticket.id)
                .with_for_update(nowait=True)
            )
        assert error.value.orig.sqlstate == '55P03'
    ticket_seat_management_service.set_chair_source(
        ticket.id, ChairSource.user, user
    ).unwrap()
    with Session(db.engine) as independent:
        assert (
            independent.scalar(
                select(DbTicket)
                .where(DbTicket.id == ticket.id)
                .with_for_update(nowait=True)
            ).chair_source
            is ChairSource.user
        )


def test_access_guard_refreshes_cached_source(ticket, party, user):
    assert ticket.chair_source is ChairSource.unknown
    with Session(db.engine) as independent:
        independent.execute(
            update(DbTicket)
            .where(DbTicket.id == ticket.id)
            .values(_chair_source='venue')
        )
        independent.commit()
    assert lock_participant_ticket(party.id, ticket.id, user.id) is ticket
    assert ticket.chair_source is ChairSource.venue
    db.session.rollback()


@pytest.mark.parametrize(
    'change', ['participant', 'revocation', 'check_in', 'manager']
)
def test_access_guard_rechecks_cached_eligibility(
    ticket, party, user, make_user, change
):
    new_user = make_user(generate_token())
    if change == 'manager':
        ticket.used_by_id = new_user.id
        db.session.commit()
    changes = {
        'participant': {
            'used_by_id': new_user.id,
            'user_managed_by_id': new_user.id,
        },
        'manager': {'user_managed_by_id': new_user.id},
        'revocation': {'revoked': True},
        'check_in': {'user_checked_in': True},
    }
    ticket_id = ticket.id
    with Session(db.engine) as independent:
        independent.execute(
            update(DbTicket)
            .where(DbTicket.id == ticket_id)
            .values(**changes[change])
        )
        independent.commit()
    assert lock_participant_ticket(party.id, ticket_id, user.id) is None
    db.session.rollback()


def test_access_guard_enforces_party_boundary(
    ticket, party, brand, user, make_party
):
    other_party = make_party(brand, title=generate_token())
    assert lock_participant_ticket(other_party.id, ticket.id, user.id) is None
    db.session.rollback()


def test_parallel_answers_are_serialized_and_logged(
    admin_app, ticket, party, user
):
    ticket_id, party_id = ticket.id, party.id
    barrier = Barrier(2)

    def save(source):
        with admin_app.app_context():
            db.session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            _set_source(party_id, ticket_id, user, source)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(save, source)
            for source in [ChairSource.user, ChairSource.venue]
        ]
        for future in futures:
            future.result(timeout=10)
    db.session.expire_all()
    assert ticket_service.get_ticket(ticket_id).chair_source in {
        ChairSource.user,
        ChairSource.venue,
    }
    entries = ticket_log_service.get_entries_for_ticket(ticket_id)
    assert len(entries) == 2
    assert {entry.data['chair_source'] for entry in entries} == {
        'user',
        'venue',
    }


def test_reassignment_resets_answer_absent_from_stale_orm_instance(
    admin_app, ticket, party, user, make_user, monkeypatch
):
    new_user = make_user(generate_token())
    ticket_id, party_id = ticket.id, party.id
    prepared, allow_commit = Event(), Event()
    build_entry = ticket_log_domain_service.build_user_appointed_entry

    def pause(*args):
        entry = build_entry(*args)
        prepared.set()
        assert allow_commit.wait(timeout=10)
        return entry

    def reassign():
        with admin_app.app_context():
            db.session.execute(text("SET LOCAL lock_timeout = '5s'"))
            ticket_user_management_service.appoint_user(
                ticket_id, new_user, user
            ).unwrap()

    monkeypatch.setattr(
        ticket_log_domain_service, 'build_user_appointed_entry', pause
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reassign)
        try:
            assert prepared.wait(timeout=5)
            _set_source(party_id, ticket_id, user, ChairSource.user)
        finally:
            allow_commit.set()
        future.result(timeout=10)
    db.session.expire_all()
    assert (
        ticket_service.get_ticket(ticket_id).chair_source is ChairSource.unknown
    )


def test_delayed_duplicate_appointment_preserves_new_participants_answer(
    admin_app, ticket, party, user, make_user, monkeypatch
):
    new_user = make_user(generate_token())
    ticket_id, party_id = ticket.id, party.id
    prepared, allow_commit = Event(), Event()
    build_entry = ticket_log_domain_service.build_user_appointed_entry

    def pause_first(*args):
        entry = build_entry(*args)
        if not prepared.is_set():
            prepared.set()
            assert allow_commit.wait(timeout=10)
        return entry

    def appoint():
        with admin_app.app_context():
            db.session.execute(text("SET LOCAL lock_timeout = '5s'"))
            ticket_user_management_service.appoint_user(
                ticket_id, new_user, user
            ).unwrap()

    monkeypatch.setattr(
        ticket_log_domain_service, 'build_user_appointed_entry', pause_first
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(appoint)
        try:
            assert prepared.wait(timeout=5)
            ticket_user_management_service.appoint_user(
                ticket_id, new_user, user
            ).unwrap()
            _set_source(party_id, ticket_id, new_user, ChairSource.user)
        finally:
            allow_commit.set()
        future.result(timeout=10)
    db.session.expire_all()
    assert ticket_service.get_ticket(ticket_id).chair_source is ChairSource.user


def test_failed_http_commit_preserves_answer_and_releases_lock(
    site_app, site, ticket, party, user, monkeypatch
):
    ticket_id = ticket.id
    _set_source(party.id, ticket_id, user, ChairSource.user)
    db_party = db.session.get(DbParty, party.id)
    previous_enabled = db_party.ticket_management_enabled
    db_party.ticket_management_enabled = True
    db.session.commit()
    log_in_user(user.id)

    def fail_commit():
        raise RuntimeError('Simulated commit failure')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(db.session, 'commit', fail_commit)
            with http_client(site_app, user_id=user.id) as client:
                with pytest.raises(
                    RuntimeError, match='Simulated commit failure'
                ):
                    client.post(
                        f'http://www.acmecon.test/tickets/tickets/{ticket_id}'
                        '/chair_source/venue'
                    )
        with Session(db.engine) as independent:
            assert (
                independent.scalar(
                    select(DbTicket)
                    .where(DbTicket.id == ticket_id)
                    .with_for_update(nowait=True)
                ).chair_source
                is ChairSource.user
            )
        db.session.expire_all()
        assert len(ticket_log_service.get_entries_for_ticket(ticket_id)) == 1
    finally:
        db.session.rollback()
        db.session.get(
            DbParty, party.id
        ).ticket_management_enabled = previous_enabled
        db.session.commit()


def test_deleting_ticket_needs_no_dependent_chair_table(ticket, party, user):
    ticket_id = ticket.id
    _set_source(party.id, ticket_id, user, ChairSource.user)
    ticket_service.delete_ticket(ticket_id)
    with Session(db.engine) as independent:
        assert independent.get(DbTicket, ticket_id) is None
