"""
tests.unit.services.lan_tournament.test_log_service_cleanups
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the tournament log service's purge path and the
purge CLI command's output convention.
"""

import pathlib
from datetime import UTC, datetime
from unittest.mock import patch

from byceps.util.result import Ok


_L = 'byceps.services.lan_tournament.tournament_log_service'


def test_purge_rolls_back_on_repository_error():
    """A failed purge must not leave a poisoned session behind.

    ``delete_log_entries_older_than`` commits internally. If the DELETE
    or that commit raises, the session is left in a failed state where
    every later statement aborts, so the Err must be accompanied by a
    rollback.
    """
    from byceps.services.lan_tournament import tournament_log_service

    cutoff = datetime(2025, 1, 1, tzinfo=UTC)

    with patch(f'{_L}.tournament_repository') as mock_repo:
        mock_repo.delete_log_entries_older_than.side_effect = RuntimeError(
            'deadlock detected'
        )

        result = tournament_log_service.purge_entries_older_than(cutoff)

        mock_repo.rollback_session.assert_called_once()

    assert result.is_err()
    assert 'deadlock detected' in result.unwrap_err()


def test_purge_returns_deleted_count_on_success():
    """A clean purge reports the row count and does not roll back."""
    from byceps.services.lan_tournament import tournament_log_service

    cutoff = datetime(2025, 1, 1, tzinfo=UTC)

    with patch(f'{_L}.tournament_repository') as mock_repo:
        mock_repo.delete_log_entries_older_than.return_value = 42

        result = tournament_log_service.purge_entries_older_than(cutoff)

        mock_repo.rollback_session.assert_not_called()

    assert result == Ok(42)


def test_purge_cli_uses_click_echo():
    """The CLI reports through click.echo, not bare print."""
    module = pathlib.Path(
        'byceps/cli/commands/purge_lan_tournament_log_entries.py'
    )
    source = module.read_text()

    assert 'click.echo(' in source
    assert 'print(' not in source


def test_log_entry_dbmodel_has_no_unused_uuid_import():
    """The dbmodel types its id via TournamentLogEntryID, not UUID."""
    module = pathlib.Path(
        'byceps/services/lan_tournament/dbmodels/tournament_log_entry.py'
    )
    source = module.read_text()

    assert 'from uuid import UUID' not in source
