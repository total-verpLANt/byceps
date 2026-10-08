"""
byceps.services.lan_tournament.tournament_dashboard_settings_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The per-party Wartung override of the dashboard thresholds.
"""

from dataclasses import replace
from datetime import datetime
import logging

from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result

from . import tournament_repository
from .dashboard_config import get_dashboard_settings, validate_thresholds
from .models.tournament_dashboard import (
    DashboardSettings,
    PartyDashboardThresholds,
)
from .tournament_operational_domain_service import normalize_utc


THRESHOLDS_STALE_ERROR = 'dashboard_thresholds_stale'

# The revision column is a 32-bit integer.
_MAX_REVISION = 2**31 - 1

_DEPLOYMENT_DEFAULT = 'deployment default'

logger = logging.getLogger(__name__)


def get_effective_dashboard_settings(
    party_id: PartyID,
) -> Result[DashboardSettings, str]:
    """Return the settings of the party.

    The party override replaces only the thresholds. Poll interval and
    page size, and an invalid deployment configuration, always come from
    the deployment.
    """
    settings_result = get_dashboard_settings()
    if settings_result.is_err():
        return settings_result
    settings = settings_result.unwrap()

    override = tournament_repository.find_party_thresholds(party_id)
    if override is None:
        return Ok(settings)

    return Ok(
        replace(
            settings,
            yellow_minutes=override.yellow_minutes,
            red_minutes=override.red_minutes,
            threshold_source='party',
        )
    )


def get_party_thresholds(party_id: PartyID) -> PartyDashboardThresholds | None:
    """Return the party override, or `None` if there is none."""
    return tournament_repository.find_party_thresholds(party_id)


def set_party_thresholds(
    party_id: PartyID,
    *,
    yellow_minutes: int,
    red_minutes: int,
    expected_revision: int,
    expected_updated_at: datetime | None,
    initiator_id: UserID,
) -> Result[PartyDashboardThresholds, str]:
    """Store the override of the party if it is still the one that was read.

    The override is identified by its revision and its `updated_at`, as
    a reset and a new save restart the revision. Both are 0 and `None`
    if the party has no override yet. Whole minutes in bounds are
    checked before anything is stored.
    """
    validated = validate_thresholds(yellow_minutes, red_minutes)
    if validated.is_err():
        return Err(validated.unwrap_err())

    expected_version = _usable_version(expected_revision, expected_updated_at)
    if expected_version is None:
        return Err(THRESHOLDS_STALE_ERROR)

    old = tournament_repository.find_party_thresholds(party_id)
    if _version_of(old) != expected_version:
        return Err(THRESHOLDS_STALE_ERROR)

    try:
        saved = tournament_repository.set_party_thresholds_flush(
            party_id,
            yellow_minutes=yellow_minutes,
            red_minutes=red_minutes,
            expected_revision=expected_revision,
            expected_updated_at=None if old is None else old.updated_at,
            updated_at=tournament_repository.get_operation_time(),
            updated_by=initiator_id,
        )
        if saved is None:
            tournament_repository.rollback_session()
            return Err(THRESHOLDS_STALE_ERROR)

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    logger.info(
        'Dashboard thresholds of party %r set by %s: old %s, new %s'
        ' (revision %d).',
        party_id,
        initiator_id,
        _describe_override(old),
        _describe(yellow_minutes, red_minutes),
        saved.revision,
    )

    return Ok(saved)


def reset_party_thresholds(
    party_id: PartyID,
    *,
    expected_revision: int,
    expected_updated_at: datetime | None,
    initiator_id: UserID,
) -> Result[None, str]:
    """Remove the override of the party if it is still the one that was read.

    The deployment default applies afterwards. A party without an
    override, with revision 0 and `updated_at` `None`, is already reset.
    """
    expected_version = _usable_version(expected_revision, expected_updated_at)
    if expected_version is None:
        return Err(THRESHOLDS_STALE_ERROR)

    old = tournament_repository.find_party_thresholds(party_id)
    if _version_of(old) != expected_version:
        return Err(THRESHOLDS_STALE_ERROR)

    if old is None:
        return Ok(None)

    try:
        deleted = tournament_repository.delete_party_thresholds_flush(
            party_id,
            expected_revision=expected_revision,
            expected_updated_at=old.updated_at,
        )
        if not deleted:
            tournament_repository.rollback_session()
            return Err(THRESHOLDS_STALE_ERROR)

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    logger.info(
        'Dashboard thresholds of party %r reset by %s: old %s, new %s.',
        party_id,
        initiator_id,
        _describe_override(old),
        _DEPLOYMENT_DEFAULT,
    )

    return Ok(None)


def _usable_version(
    revision: object, updated_at: object
) -> tuple[int, datetime | None] | None:
    """Return the version as naive UTC, or `None` if it cannot be stored.

    No override has revision 0 and no `updated_at`. An override has a
    revision of 1 or more and the `updated_at` it was stored with.
    """
    if type(revision) is not int or not 0 <= revision <= _MAX_REVISION:
        return None

    if revision == 0:
        return (0, None) if updated_at is None else None

    if not isinstance(updated_at, datetime):
        return None

    return (revision, normalize_utc(updated_at))


def _version_of(
    thresholds: PartyDashboardThresholds | None,
) -> tuple[int, datetime | None]:
    if thresholds is None:
        return (0, None)

    return (thresholds.revision, normalize_utc(thresholds.updated_at))


def _describe(yellow_minutes: int, red_minutes: int) -> str:
    return f'yellow={yellow_minutes} red={red_minutes}'


def _describe_override(thresholds: PartyDashboardThresholds | None) -> str:
    if thresholds is None:
        return _DEPLOYMENT_DEFAULT

    return _describe(thresholds.yellow_minutes, thresholds.red_minutes)
