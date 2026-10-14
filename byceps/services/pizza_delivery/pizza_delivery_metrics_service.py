from collections.abc import Iterator

from sqlalchemy import func, select

from byceps.database import db
from byceps.services.metrics.models import Label, Metric
from byceps.services.party import party_service

from .dbmodels import DbPizzaDeliveryEntry
from .models import PizzaDeliveryStatus


_STATUSES = (PizzaDeliveryStatus.PENDING, PizzaDeliveryStatus.DELIVERED)


def collect_metrics() -> Iterator[Metric]:
    """Yield pizza delivery entry counts for active parties."""
    party_ids = [party.id for party in party_service.get_active_parties()]
    if not party_ids:
        return

    rows = db.session.execute(
        select(
            DbPizzaDeliveryEntry.party_id,
            DbPizzaDeliveryEntry.status,
            func.count(DbPizzaDeliveryEntry.id),
        )
        .where(DbPizzaDeliveryEntry.party_id.in_(party_ids))
        .group_by(DbPizzaDeliveryEntry.party_id, DbPizzaDeliveryEntry.status)
    ).all()

    counts = {(party_id, status): count for party_id, status, count in rows}

    for party_id in party_ids:
        for status in _STATUSES:
            yield Metric(
                'pizza_delivery_entries_count',
                counts.get((party_id, status), 0),
                labels=[Label('party', party_id), Label('status', status)],
            )
