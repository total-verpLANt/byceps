"""
byceps.services.chair_optout.lifecycle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:License: Revised BSD (see `LICENSE` file for details)
"""

from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.attributes import flag_modified

from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.ticketing.models.ticket import ChairSource


def enable_chair_lifecycle() -> None:
    """Register process-local ticket lifecycle handling exactly once."""
    if not event.contains(DbTicket, 'before_update', _reset_after_user_change):
        event.listen(DbTicket, 'before_update', _reset_after_user_change)


def _reset_after_user_change(
    mapper: Mapper, connection: Connection, ticket: DbTicket
) -> None:
    """Reset the source atomically when the database participant changes."""
    if not inspect(ticket).attrs.used_by_id.history.has_changes():
        return

    table = DbTicket.__table__
    row = connection.execute(
        select(table.c.used_by_id)
        .where(table.c.id == ticket.id)
        .with_for_update()
    ).one_or_none()
    if row is None or row.used_by_id == ticket.used_by_id:
        return

    ticket.chair_source = ChairSource.unknown
    # A concurrently saved answer may not be present in this ORM instance.
    # Force the reset into the UPDATE even if its local value was already unknown.
    flag_modified(ticket, '_chair_source')
