"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime
from types import SimpleNamespace

from byceps.services.chair_optout import chair_optout_service
from byceps.services.chair_optout.dbmodels import DbPartyTicketChairOptout
from byceps.services.chair_optout.models import ChairOptoutID
from byceps.services.party.models import PartyID
from byceps.services.ticketing.models.ticket import TicketID
from byceps.services.user.models import UserID

from tests.helpers import generate_token, generate_uuid


class DummySession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = False

    def add(self, obj: object) -> None:
        if getattr(obj, 'id', None) is None:
            obj.id = ChairOptoutID(generate_uuid())
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True


def _make_ids() -> tuple[PartyID, TicketID, UserID]:
    return (
        PartyID(generate_token()),
        TicketID(generate_uuid()),
        UserID(generate_uuid()),
    )


def _make_db_optout(
    party_id: PartyID,
    ticket_id: TicketID,
    user_id: UserID,
    updated_at: datetime,
    brings_own_chair: bool,
) -> DbPartyTicketChairOptout:
    db_optout = DbPartyTicketChairOptout(
        party_id,
        ticket_id,
        user_id,
        updated_at,
        brings_own_chair=brings_own_chair,
    )
    db_optout.id = ChairOptoutID(generate_uuid())
    return db_optout


def test_set_optout_creates_and_gets_optout(monkeypatch):
    party_id, ticket_id, user_id = _make_ids()
    fixed_now = datetime(2026, 1, 15, 12, 0, 0)

    class FixedDateTime:
        @staticmethod
        def utcnow() -> datetime:
            return fixed_now

    session = DummySession()
    dummy_db = SimpleNamespace(session=session)

    monkeypatch.setattr(chair_optout_service, 'db', dummy_db)
    monkeypatch.setattr(chair_optout_service, 'datetime', FixedDateTime)
    monkeypatch.setattr(
        chair_optout_service, '_get_db_optout', lambda *_: None
    )

    optout = chair_optout_service.set_optout(
        party_id, ticket_id, user_id, True
    )

    assert session.committed is True
    assert len(session.added) == 1
    assert optout.party_id == party_id
    assert optout.ticket_id == ticket_id
    assert optout.user_id == user_id
    assert optout.brings_own_chair is True
    assert optout.updated_at == fixed_now

    created_db_optout = session.added[0]
    monkeypatch.setattr(
        chair_optout_service,
        '_get_db_optout',
        lambda *_: created_db_optout,
    )
    fetched = chair_optout_service.get_optout(party_id, ticket_id)

    assert fetched == optout

    ticket = SimpleNamespace(occupied_seat=SimpleNamespace(label='B-13'))
    assert (
        chair_optout_service.resolve_seat_label_for_ticket(ticket) == 'B-13'
    )


def test_get_optout_returns_none_when_missing(monkeypatch):
    party_id, ticket_id, _ = _make_ids()

    monkeypatch.setattr(
        chair_optout_service, '_get_db_optout', lambda *_: None
    )

    assert chair_optout_service.get_optout(party_id, ticket_id) is None


def test_set_optout_updates_existing(monkeypatch):
    party_id, ticket_id, user_id = _make_ids()
    previous_user_id = UserID(generate_uuid())
    initial_updated_at = datetime(2026, 1, 10, 9, 0, 0)
    fixed_now = datetime(2026, 1, 16, 8, 30, 0)

    db_optout = _make_db_optout(
        party_id,
        ticket_id,
        previous_user_id,
        initial_updated_at,
        False,
    )

    class FixedDateTime:
        @staticmethod
        def utcnow() -> datetime:
            return fixed_now

    session = DummySession()
    dummy_db = SimpleNamespace(session=session)

    monkeypatch.setattr(chair_optout_service, 'db', dummy_db)
    monkeypatch.setattr(chair_optout_service, 'datetime', FixedDateTime)
    monkeypatch.setattr(
        chair_optout_service, '_get_db_optout', lambda *_: db_optout
    )

    optout = chair_optout_service.set_optout(
        party_id, ticket_id, user_id, True
    )

    assert session.committed is True
    assert session.added == []
    assert db_optout.user_id == user_id
    assert db_optout.brings_own_chair is True
    assert db_optout.updated_at == fixed_now
    assert optout.user_id == user_id
    assert optout.brings_own_chair is True
    assert optout.updated_at == fixed_now


def test_list_optouts_for_party_filters_false_entries(monkeypatch):
    party_id, ticket_id_true, user_id = _make_ids()
    _, ticket_id_false, _ = _make_ids()
    updated_at = datetime(2026, 1, 15, 12, 30, 0)

    db_optout_true = _make_db_optout(
        party_id, ticket_id_true, user_id, updated_at, True
    )
    db_optout_false = _make_db_optout(
        party_id, ticket_id_false, user_id, updated_at, False
    )

    monkeypatch.setattr(
        chair_optout_service,
        '_get_db_optouts_for_party',
        lambda *_: [db_optout_true, db_optout_false],
    )

    optouts = chair_optout_service.list_optouts_for_party(
        party_id, only_true=True
    )

    assert [optout.ticket_id for optout in optouts] == [ticket_id_true]


def test_list_optouts_for_user_returns_all_entries(monkeypatch):
    party_id, ticket_id_first, user_id = _make_ids()
    _, ticket_id_second, _ = _make_ids()
    updated_at = datetime(2026, 1, 15, 13, 0, 0)

    db_optouts = [
        _make_db_optout(
            party_id, ticket_id_first, user_id, updated_at, True
        ),
        _make_db_optout(
            party_id, ticket_id_second, user_id, updated_at, False
        ),
    ]

    monkeypatch.setattr(
        chair_optout_service,
        '_get_db_optouts_for_user',
        lambda *_: db_optouts,
    )

    optouts = chair_optout_service.list_optouts_for_user(party_id, user_id)

    assert [optout.ticket_id for optout in optouts] == [
        ticket_id_first,
        ticket_id_second,
    ]


def test_resolve_seat_label_for_ticket_handles_missing_seat():
    assert chair_optout_service.resolve_seat_label_for_ticket(None) is None

    ticket = SimpleNamespace(occupied_seat=None)
    assert chair_optout_service.resolve_seat_label_for_ticket(ticket) is None
