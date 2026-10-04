"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

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
        self.commit_count = 0

    def add(self, obj: object) -> None:
        if getattr(obj, 'id', None) is None:
            obj.id = ChairOptoutID(generate_uuid())
        self.added.append(obj)

    def commit(self) -> None:
        self.commit_count += 1


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
    brings_own_chair: bool,
) -> DbPartyTicketChairOptout:
    db_optout = DbPartyTicketChairOptout(
        party_id,
        ticket_id,
        user_id,
        datetime(2026, 1, 15, 12, 0, 0),
        brings_own_chair=brings_own_chair,
    )
    db_optout.id = ChairOptoutID(generate_uuid())
    return db_optout


def _prepare_set_optout(
    monkeypatch, existing_optout=None
) -> tuple[DummySession, PartyID, TicketID, UserID]:
    party_id, ticket_id, user_id = _make_ids()
    session = DummySession()
    monkeypatch.setattr(
        chair_optout_service, 'db', SimpleNamespace(session=session)
    )
    monkeypatch.setattr(
        chair_optout_service,
        '_find_eligible_ticket',
        lambda *_: SimpleNamespace(id=ticket_id),
    )
    monkeypatch.setattr(
        chair_optout_service,
        '_get_db_optout',
        lambda *_: existing_optout,
    )
    return session, party_id, ticket_id, user_id


@pytest.mark.parametrize('brings_own_chair', [True, False])
def test_set_optout_creates_answer(monkeypatch, brings_own_chair):
    session, party_id, ticket_id, user_id = _prepare_set_optout(monkeypatch)

    optout = chair_optout_service.set_optout(
        party_id, ticket_id, user_id, brings_own_chair
    )

    assert session.commit_count == 1
    assert len(session.added) == 1
    assert optout.party_id == party_id
    assert optout.ticket_id == ticket_id
    assert optout.user_id == user_id
    assert optout.brings_own_chair is brings_own_chair


@pytest.mark.parametrize(
    ('initial_value', 'new_value'), [(True, False), (False, True)]
)
def test_set_optout_changes_answer(monkeypatch, initial_value, new_value):
    party_id, ticket_id, user_id = _make_ids()
    db_optout = _make_db_optout(party_id, ticket_id, user_id, initial_value)
    session, _, _, _ = _prepare_set_optout(monkeypatch, db_optout)

    optout = chair_optout_service.set_optout(
        party_id, ticket_id, user_id, new_value
    )

    assert session.commit_count == 1
    assert session.added == []
    assert optout.brings_own_chair is new_value


def test_set_optout_updates_user_after_reassignment(monkeypatch):
    party_id, ticket_id, old_user_id = _make_ids()
    new_user_id = UserID(generate_uuid())
    db_optout = _make_db_optout(party_id, ticket_id, old_user_id, True)
    session, _, _, _ = _prepare_set_optout(monkeypatch, db_optout)

    optout = chair_optout_service.set_optout(
        party_id, ticket_id, new_user_id, False
    )

    assert session.commit_count == 1
    assert optout.user_id == new_user_id
    assert optout.brings_own_chair is False


def test_set_optout_rejects_ineligible_ticket(monkeypatch):
    party_id, ticket_id, user_id = _make_ids()
    monkeypatch.setattr(
        chair_optout_service, '_find_eligible_ticket', lambda *_: None
    )

    with pytest.raises(ValueError):
        chair_optout_service.set_optout(party_id, ticket_id, user_id, True)


def test_current_optouts_ignore_stale_participant_and_party(monkeypatch):
    party_id, current_ticket_id, current_user_id = _make_ids()
    stale_ticket_id = TicketID(generate_uuid())
    foreign_party_ticket_id = TicketID(generate_uuid())
    tickets = [
        SimpleNamespace(
            id=current_ticket_id,
            party_id=party_id,
            used_by_id=current_user_id,
            revoked=False,
        ),
        SimpleNamespace(
            id=stale_ticket_id,
            party_id=party_id,
            used_by_id=current_user_id,
            revoked=False,
        ),
        SimpleNamespace(
            id=foreign_party_ticket_id,
            party_id=party_id,
            used_by_id=current_user_id,
            revoked=False,
        ),
    ]
    optouts = [
        _make_db_optout(party_id, current_ticket_id, current_user_id, False),
        _make_db_optout(
            party_id, stale_ticket_id, UserID(generate_uuid()), True
        ),
        _make_db_optout(
            PartyID(generate_token()),
            foreign_party_ticket_id,
            current_user_id,
            True,
        ),
    ]
    monkeypatch.setattr(
        chair_optout_service,
        '_get_db_optouts_for_tickets',
        lambda *_: optouts,
    )

    current = chair_optout_service.get_current_optouts_for_tickets(tickets)

    assert set(current) == {current_ticket_id}
    assert current[current_ticket_id].brings_own_chair is False


def test_current_optouts_ignore_revoked_and_unassigned_tickets(monkeypatch):
    party_id, ticket_id, user_id = _make_ids()
    tickets = [
        SimpleNamespace(
            id=ticket_id,
            party_id=party_id,
            used_by_id=user_id,
            revoked=True,
        ),
        SimpleNamespace(
            id=TicketID(generate_uuid()),
            party_id=party_id,
            used_by_id=None,
            revoked=False,
        ),
    ]
    called = False

    def get_optouts(*_):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        chair_optout_service, '_get_db_optouts_for_tickets', get_optouts
    )

    assert chair_optout_service.get_current_optouts_for_tickets(tickets) == {}
    assert called is False


def test_report_includes_all_answer_states_and_missing_seat(monkeypatch):
    party_id, first_ticket_id, first_user_id = _make_ids()
    second_ticket_id = TicketID(generate_uuid())
    third_ticket_id = TicketID(generate_uuid())
    user = SimpleNamespace(
        id=first_user_id,
        screen_name='alice',
        detail=SimpleNamespace(full_name='Alice Example'),
    )
    first_seat = SimpleNamespace(
        id=generate_uuid(),
        label='A-1',
        area=SimpleNamespace(slug='first-area'),
    )
    second_seat = SimpleNamespace(
        id=generate_uuid(),
        label='A-2',
        area=SimpleNamespace(slug='second-area'),
    )
    tickets = [
        SimpleNamespace(
            id=first_ticket_id,
            code='T-1',
            used_by=user,
            occupied_seat=first_seat,
        ),
        SimpleNamespace(
            id=second_ticket_id,
            code='T-2',
            used_by=user,
            occupied_seat=second_seat,
        ),
        SimpleNamespace(
            id=third_ticket_id,
            code='T-3',
            used_by=user,
            occupied_seat=None,
        ),
    ]
    own = SimpleNamespace(brings_own_chair=True)
    provided = SimpleNamespace(brings_own_chair=False)
    monkeypatch.setattr(
        chair_optout_service,
        '_get_eligible_tickets_for_party',
        lambda *_: tickets,
    )
    monkeypatch.setattr(
        chair_optout_service,
        'get_current_optouts_for_tickets',
        lambda *_: {first_ticket_id: own, second_ticket_id: provided},
    )

    entries = chair_optout_service.get_report_entries_for_party(party_id)
    summary = chair_optout_service.summarize_report_entries(entries)

    assert [entry.brings_own_chair for entry in entries] == [True, False, None]
    assert entries[2].has_seat is False
    assert entries[0].user_id == first_user_id
    assert entries[0].seat_id == first_seat.id
    assert entries[0].seat_area_slug == 'first-area'
    assert summary.brings_own_chair == 1
    assert summary.needs_provided_chair == 1
    assert summary.not_specified == 1
    assert summary.no_seat == 1


def test_seat_label_changes_without_changing_answer():
    ticket = SimpleNamespace(occupied_seat=SimpleNamespace(label='A-1'))
    assert chair_optout_service.resolve_seat_label_for_ticket(ticket) == 'A-1'

    ticket.occupied_seat = SimpleNamespace(label='B-2')
    assert chair_optout_service.resolve_seat_label_for_ticket(ticket) == 'B-2'

    ticket.occupied_seat = None
    assert chair_optout_service.resolve_seat_label_for_ticket(ticket) is None
