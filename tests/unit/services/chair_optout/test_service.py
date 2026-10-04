"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    create_engine,
    MetaData,
    String,
    Table,
    Uuid,
)
from sqlalchemy.orm import joinedload, Session

from byceps.services.chair_optout import chair_optout_service as service
from byceps.services.ticketing.models.ticket import ChairSource

from tests.helpers import generate_uuid


@pytest.fixture
def ticket_rows(monkeypatch):
    """Exercise compact queries on an isolated, in-memory ticket table."""
    engine = create_engine('sqlite://')
    metadata = MetaData()
    tickets = Table(
        'tickets',
        metadata,
        Column('id', Uuid, primary_key=True),
        Column('party_id', String),
        Column('code', String),
        Column('used_by_id', Uuid),
        Column('owned_by_id', Uuid),
        Column('user_managed_by_id', Uuid),
        Column('revoked', Boolean),
        Column('user_checked_in', Boolean),
        Column('chair_source', String),
    )
    metadata.create_all(engine)
    with Session(engine) as session:
        monkeypatch.setattr(service, 'db', SimpleNamespace(session=session))

        def insert(*, source=None, **overrides):
            row = dict(
                id=generate_uuid(),
                party_id='party-1',
                code='T-1',
                used_by_id=user_id,
                owned_by_id=generate_uuid(),
                user_managed_by_id=None,
                revoked=False,
                user_checked_in=False,
                chair_source=source.name if source else None,
            )
            row.update(overrides)
            session.execute(tickets.insert().values(**row))
            return row['id']

        user_id = generate_uuid()
        yield insert, user_id
    engine.dispose()


def test_compact_sources_include_all_states_and_only_eligible_tickets(
    ticket_rows,
):
    insert, _ = ticket_rows
    own = insert(source=ChairSource.user)
    venue = insert(source=ChairSource.venue)
    rental = insert(source=ChairSource.rental)
    pending = insert()
    unknown = insert(source=ChairSource.unknown)
    invalid = insert(chair_source='invalid')
    checked_in = insert(source=ChairSource.user, user_checked_in=True)
    insert(source=ChairSource.user, revoked=True)
    insert(source=ChairSource.rental, used_by_id=None)
    insert(source=ChairSource.venue, party_id='other-party')

    assert service.get_chair_sources_for_party('party-1') == {
        own: ChairSource.user,
        venue: ChairSource.venue,
        rental: ChairSource.rental,
        pending: ChairSource.unknown,
        unknown: ChairSource.unknown,
        invalid: ChairSource.unknown,
        checked_in: ChairSource.user,
    }


def test_pending_tickets_include_unknown_null_invalid_and_current_permissions(
    ticket_rows,
):
    insert, user_id = ticket_rows
    second = insert(code='T-2')
    first = insert(code='T-1')
    insert(revoked=True)
    insert(user_checked_in=True)
    insert(used_by_id=None)
    insert(used_by_id=None, owned_by_id=user_id)
    insert(used_by_id=generate_uuid())
    insert(party_id='other-party')
    for source in [ChairSource.user, ChairSource.venue, ChairSource.rental]:
        insert(source=source)
    unknown = insert(source=ChairSource.unknown, code='T-3')
    invalid = insert(chair_source='unrecognized-source', code='T-4')
    managed = insert(
        used_by_id=generate_uuid(), user_managed_by_id=user_id, code='T-5'
    )
    owned = insert(used_by_id=generate_uuid(), owned_by_id=user_id, code='T-6')
    insert(
        used_by_id=generate_uuid(),
        owned_by_id=user_id,
        user_managed_by_id=generate_uuid(),
    )

    assert service.get_pending_chair_ticket_ids_for_user(
        'party-1', user_id
    ) == [
        first,
        second,
        unknown,
        invalid,
        managed,
        owned,
    ]


def test_report_uses_current_user_seat_and_core_source(monkeypatch):
    user = SimpleNamespace(
        id=generate_uuid(),
        screen_name='alice',
        detail=SimpleNamespace(full_name='Alice Example'),
    )
    seat = SimpleNamespace(
        id=generate_uuid(), label='A-1', area=SimpleNamespace(slug='main')
    )
    tickets = [
        SimpleNamespace(
            id=generate_uuid(),
            code=f'T-{index}',
            used_by=user,
            occupied_seat=seat if index < 3 else None,
            chair_source=source,
        )
        for index, source in enumerate(
            [
                ChairSource.user,
                ChairSource.venue,
                ChairSource.rental,
                ChairSource.unknown,
                ChairSource.rental,
            ]
        )
    ]
    result = SimpleNamespace(
        unique=lambda: SimpleNamespace(all=lambda: tickets)
    )
    monkeypatch.setattr(
        service,
        'db',
        SimpleNamespace(
            session=SimpleNamespace(scalars=lambda _: result),
            joinedload=joinedload,
        ),
    )

    entries = service.get_report_entries_for_party('party-1')
    summary = service.summarize_report_entries(entries)

    assert [entry.chair_source for entry in entries] == [
        ticket.chair_source for ticket in tickets
    ]
    assert entries[0].seat_id == seat.id
    assert entries[0].seat_area_slug == 'main'
    assert entries[0].full_name == 'Alice Example'
    assert entries[0].user_id == user.id
    assert entries[3].seat_id is None
    assert entries[3].seat_label is None
    assert summary.brings_own_chair == 1
    assert summary.needs_provided_chair == 1
    assert summary.rented_chair == 2
    assert summary.not_specified == 1
    assert summary.no_seat == 2

    tickets[0].occupied_seat = SimpleNamespace(
        id=generate_uuid(), label='B-2', area=SimpleNamespace(slug='second')
    )
    user.detail = None
    changed_entry = service.get_report_entries_for_party('party-1')[0]
    assert changed_entry.seat_label == 'B-2'
    assert changed_entry.seat_area_slug == 'second'
    assert changed_entry.full_name is None
    assert changed_entry.chair_source is ChairSource.user


def test_empty_report_summary():
    summary = service.summarize_report_entries([])
    assert summary.brings_own_chair == 0
    assert summary.needs_provided_chair == 0
    assert summary.rented_chair == 0
    assert summary.not_specified == 0
    assert summary.no_seat == 0
