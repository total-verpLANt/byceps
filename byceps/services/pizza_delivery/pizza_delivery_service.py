"""
byceps.services.pizza_delivery.pizza_delivery_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok, Result

from . import pizza_delivery_domain_service, signals
from .dbmodels import DbPizzaDeliveryEntry
from .errors import (
    DuplicateDeliveryNumberError,
    PizzaDeliveryEntryAlreadyClaimedError,
    PizzaDeliveryEntryAlreadyDeliveredError,
    PizzaDeliveryEntryNotDeliveredError,
    PizzaDeliveryEntryNotFoundError,
    PizzaDeliveryNumberNotFoundError,
)
from .events import (
    PizzaDeliveryEntryClaimedEvent,
    PizzaDeliveryEntryCreatedEvent,
    PizzaDeliveryEntryDeletedEvent,
    PizzaDeliveryEntryDeliveredEvent,
    PizzaDeliveryEntryUndeliveredEvent,
    PizzaDeliveryEntryUpdatedEvent,
)
from .models import PizzaDeliveryEntry, PizzaDeliveryEntryID, PizzaDeliveryStatus


def create_entry(
    party_id: PartyID,
    number: str,
    user_id: UserID | None,
    created_by_id: UserID,
    *,
    initiator: User | None = None,
) -> Result[PizzaDeliveryEntry, DuplicateDeliveryNumberError]:
    """Create a pizza delivery entry."""
    number = number.upper()

    # Check uniqueness.
    existing = (
        db.session.execute(
            select(DbPizzaDeliveryEntry).filter_by(
                party_id=party_id, number=number
            )
        )
        .scalars()
        .one_or_none()
    )
    if existing is not None:
        return Err(DuplicateDeliveryNumberError(party_id=party_id, number=number))

    entry = pizza_delivery_domain_service.create_entry(
        party_id, number, user_id, created_by_id
    )

    db_entry = DbPizzaDeliveryEntry(
        entry.id,
        entry.party_id,
        entry.number,
        entry.user_id,
        entry.created_at,
        entry.updated_at,
        entry.created_by_id,
        entry.status,
    )
    db.session.add(db_entry)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return Err(DuplicateDeliveryNumberError(party_id=party_id, number=number))

    event = PizzaDeliveryEntryCreatedEvent(
        occurred_at=datetime.now(timezone.utc),
        initiator=initiator,
        party_id=party_id,
        entry_id=entry.id,
        number=number,
        user_id=user_id,
    )
    signals.entry_created.send(None, event=event)

    return Ok(entry)


def delete_entry(
    entry_id: PizzaDeliveryEntryID,
    *,
    initiator: User | None = None,
) -> Result[None, PizzaDeliveryEntryNotFoundError]:
    """Delete a pizza delivery entry."""
    db_entry = db.session.get(DbPizzaDeliveryEntry, entry_id)
    if db_entry is None:
        return Err(PizzaDeliveryEntryNotFoundError(entry_id=entry_id))

    party_id = db_entry.party_id
    number = db_entry.number

    db.session.delete(db_entry)
    db.session.commit()

    event = PizzaDeliveryEntryDeletedEvent(
        occurred_at=datetime.now(timezone.utc),
        initiator=initiator,
        party_id=party_id,
        entry_id=entry_id,
        number=number,
    )
    signals.entry_deleted.send(None, event=event)

    return Ok(None)


def get_entries_for_party(
    party_id: PartyID,
    *,
    status: str | None = None,
) -> list[PizzaDeliveryEntry]:
    """Return all pizza delivery entries for the party, ordered by
    created_at descending.
    """
    stmt = select(DbPizzaDeliveryEntry).filter_by(party_id=party_id)

    if status is not None:
        stmt = stmt.filter(DbPizzaDeliveryEntry.status == status)

    stmt = stmt.order_by(DbPizzaDeliveryEntry.created_at.desc()).limit(200)

    db_entries = db.session.execute(stmt).scalars().all()

    return [_db_entity_to_entry(db_entry) for db_entry in db_entries]


def get_claimed_entries_for_user(
    user_id: UserID,
    party_id: PartyID,
) -> list[PizzaDeliveryEntry]:
    """Return all pizza delivery entries claimed by a user at a
    specific party, ordered by created_at descending.
    """
    db_entries = (
        db.session.execute(
            select(DbPizzaDeliveryEntry)
            .filter_by(party_id=party_id, user_id=user_id)
            .order_by(DbPizzaDeliveryEntry.created_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )

    return [_db_entity_to_entry(db_entry) for db_entry in db_entries]


def find_entry(
    entry_id: PizzaDeliveryEntryID,
) -> PizzaDeliveryEntry | None:
    """Return the pizza delivery entry, if found."""
    db_entry = db.session.get(DbPizzaDeliveryEntry, entry_id)
    if db_entry is None:
        return None

    return _db_entity_to_entry(db_entry)


def deliver_entry(
    entry_id: PizzaDeliveryEntryID,
    *,
    initiator: User | None = None,
) -> Result[PizzaDeliveryEntry, PizzaDeliveryEntryNotFoundError | PizzaDeliveryEntryAlreadyDeliveredError]:
    """Mark a pizza delivery entry as delivered."""
    db_entry = db.session.get(DbPizzaDeliveryEntry, entry_id)
    if db_entry is None:
        return Err(PizzaDeliveryEntryNotFoundError(entry_id=entry_id))

    if db_entry.status == PizzaDeliveryStatus.DELIVERED:
        return Err(PizzaDeliveryEntryAlreadyDeliveredError(entry_id=entry_id))

    db_entry.status = PizzaDeliveryStatus.DELIVERED
    db_entry.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    entry = _db_entity_to_entry(db_entry)

    event = PizzaDeliveryEntryDeliveredEvent(
        occurred_at=datetime.now(timezone.utc),
        initiator=initiator,
        party_id=entry.party_id,
        entry_id=entry.id,
        number=entry.number,
        user_id=entry.user_id,
    )
    signals.entry_delivered.send(None, event=event)

    return Ok(entry)


def undeliver_entry(
    entry_id: PizzaDeliveryEntryID,
    *,
    initiator: User | None = None,
) -> Result[
    PizzaDeliveryEntry,
    PizzaDeliveryEntryNotFoundError | PizzaDeliveryEntryNotDeliveredError,
]:
    """Revert a pizza delivery entry from delivered back to pending."""
    db_entry = db.session.get(DbPizzaDeliveryEntry, entry_id)
    if db_entry is None:
        return Err(PizzaDeliveryEntryNotFoundError(entry_id=entry_id))

    if db_entry.status != PizzaDeliveryStatus.DELIVERED:
        return Err(PizzaDeliveryEntryNotDeliveredError(entry_id=entry_id))

    db_entry.status = PizzaDeliveryStatus.PENDING
    db_entry.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    entry = _db_entity_to_entry(db_entry)

    event = PizzaDeliveryEntryUndeliveredEvent(
        occurred_at=datetime.now(timezone.utc),
        initiator=initiator,
        party_id=entry.party_id,
        entry_id=entry.id,
        number=entry.number,
        user_id=entry.user_id,
    )
    signals.entry_undelivered.send(None, event=event)

    return Ok(entry)


def update_entry_user(
    entry_id: PizzaDeliveryEntryID,
    user_id: UserID | None,
    *,
    initiator: User | None = None,
) -> Result[PizzaDeliveryEntry, PizzaDeliveryEntryNotFoundError]:
    """Update the linked user of a pizza delivery entry."""
    db_entry = db.session.get(DbPizzaDeliveryEntry, entry_id)
    if db_entry is None:
        return Err(PizzaDeliveryEntryNotFoundError(entry_id=entry_id))

    db_entry.user_id = user_id
    db_entry.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    entry = _db_entity_to_entry(db_entry)

    event = PizzaDeliveryEntryUpdatedEvent(
        occurred_at=datetime.now(timezone.utc),
        initiator=initiator,
        party_id=entry.party_id,
        entry_id=entry.id,
        number=entry.number,
        user_id=user_id,
    )
    signals.entry_updated.send(None, event=event)

    return Ok(entry)


def claim_entry_by_number(
    party_id: PartyID,
    number: str,
    user_id: UserID,
    *,
    initiator: User | None = None,
) -> Result[PizzaDeliveryEntry, PizzaDeliveryNumberNotFoundError | PizzaDeliveryEntryAlreadyClaimedError]:
    """Claim a pizza delivery entry by its number."""
    number = number.upper()

    db_entry = (
        db.session.execute(
            select(DbPizzaDeliveryEntry)
            .filter_by(party_id=party_id, number=number)
            .with_for_update()
        )
        .scalars()
        .one_or_none()
    )
    if db_entry is None:
        return Err(PizzaDeliveryNumberNotFoundError(party_id=party_id, number=number))

    if db_entry.user_id is not None:
        return Err(
            PizzaDeliveryEntryAlreadyClaimedError(
                entry_id=db_entry.id, claimed_by_id=db_entry.user_id
            )
        )

    db_entry.user_id = user_id
    db_entry.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    entry = _db_entity_to_entry(db_entry)

    event = PizzaDeliveryEntryClaimedEvent(
        occurred_at=datetime.now(timezone.utc),
        initiator=initiator,
        party_id=entry.party_id,
        entry_id=entry.id,
        number=entry.number,
        user_id=user_id,
    )
    signals.entry_claimed.send(None, event=event)

    return Ok(entry)


def _db_entity_to_entry(
    db_entry: DbPizzaDeliveryEntry,
) -> PizzaDeliveryEntry:
    return PizzaDeliveryEntry(
        id=db_entry.id,
        party_id=db_entry.party_id,
        number=db_entry.number,
        user_id=db_entry.user_id,
        status=db_entry.status,
        created_at=db_entry.created_at,
        updated_at=db_entry.updated_at,
        created_by_id=db_entry.created_by_id,
    )
