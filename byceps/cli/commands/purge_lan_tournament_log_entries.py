"""
byceps.cli.command.purge_lan_tournament_log_entries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Purge old lan_tournament log entries.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import UTC, datetime, timedelta

import click
from flask.cli import with_appcontext
from sqlalchemy import select

from byceps.database import db
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.tournament_log_service import (
    purge_entries_older_than,
)


@click.command()
@click.option(
    '--older-than-days',
    type=int,
    default=365,
    show_default=True,
    help='Delete log entries older than this many days.',
)
@click.option(
    '--dry-run',
    is_flag=True,
    default=False,
    help='Report how many entries would be deleted without deleting.',
)
@with_appcontext
def purge_lan_tournament_log_entries(
    older_than_days: int, dry_run: bool
) -> None:
    """Purge old lan_tournament log entries."""
    if older_than_days <= 0:
        raise click.BadParameter('Must be a positive number of days.')

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=older_than_days)

    if dry_run:
        num_matching = db.session.scalar(
            select(db.func.count())
            .select_from(DbTournamentLogEntry)
            .where(DbTournamentLogEntry.occurred_at < cutoff)
        )
        print(f'{num_matching} log entries would be deleted')
        return

    result = purge_entries_older_than(cutoff)
    if result.is_err():
        raise click.ClickException(result.unwrap_err())

    print(f'{result.unwrap()} log entries deleted')
