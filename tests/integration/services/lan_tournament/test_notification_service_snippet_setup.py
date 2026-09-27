"""
tests.integration.services.lan_tournament.test_notification_service_snippet_setup
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration coverage for the email-snippet setup race fix: proves the
real Postgres unique-constraint violation is an `IntegrityError` and
that `_create_missing_snippets`'s rollback actually leaves the session
usable afterwards -- neither of which a mocked `snippet_service` can
show.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_notification_service as notif,
)
from byceps.services.snippet import snippet_service
from byceps.services.snippet.models import SnippetScope


@pytest.fixture(scope='module')
def creator(make_user):
    return make_user('SnippetSetupRaceCreator')


def test_create_snippet_raises_integrity_error_on_duplicate(brand, creator):
    """A second `create_snippet` call for the same scope/name/language
    hits the real unique constraint and raises `IntegrityError` -- the
    exact exception type `_create_missing_snippets` catches.
    """
    scope = SnippetScope.for_brand(brand.id)

    snippet_service.create_snippet(
        scope, 'email_race_dup_test', 'en', creator, 'first'
    )

    try:
        with pytest.raises(IntegrityError):
            snippet_service.create_snippet(
                scope, 'email_race_dup_test', 'en', creator, 'second'
            )
    finally:
        # `create_snippet` itself never rolls back on failure (that is
        # exactly the gap the production fix closes) -- do it here so
        # this test doesn't leave the shared session broken for the
        # next one, as `_create_missing_snippets` now does for real.
        db.session.rollback()


def test_create_missing_snippets_recovers_from_real_race(brand, creator):
    """Simulate the TOCTOU gap a real concurrent setup produces: the
    pair already exists (another request won the race) but
    `_find_missing_snippets` is forced to still report it as missing,
    so `create_snippet` hits the real unique constraint.

    `_create_missing_snippets` must roll back and continue rather than
    leaving the session's transaction broken (a `PendingRollbackError`
    on the very next query would prove a missing `db.session.rollback()`
    -- a mock can't catch that, only a live session can).
    """
    scope = SnippetScope.for_brand(brand.id)
    pair = ('email_race_recovery_test', 'en')

    snippet_service.create_snippet(
        scope, pair[0], pair[1], creator, 'already there'
    )

    with patch.object(notif, '_find_missing_snippets', return_value=[pair]):
        created = notif._create_missing_snippets(
            brand, creator, {pair: 'a concurrent setup would have sent this'}
        )

    assert created is False

    # The session must still be usable -- this query would raise
    # `PendingRollbackError` if the failed INSERT's transaction had
    # not actually been rolled back.
    still_there = snippet_service.find_current_version_of_snippet_with_name(
        scope, pair[0], pair[1]
    )
    assert still_there is not None
    assert still_there.body.strip() == 'already there'


def test_create_missing_snippets_raises_on_fk_violation(brand, creator):
    """A real config error -- an unknown language code, which violates
    the FK to `languages.code` -- is not a concurrent-setup race. The
    recheck after rollback still finds the pair missing, so the
    original `IntegrityError` must propagate instead of being logged
    and swallowed as a truthy-looking `created_any=False` return.
    """
    pair = ('email_fk_violation_test', 'xx-unknown-language')

    with pytest.raises(IntegrityError):
        notif._create_missing_snippets(
            brand, creator, {pair: 'this should never be inserted'}
        )

    # The session must still be usable -- `_create_missing_snippets`
    # rolls back before re-raising, so this query must not raise
    # `PendingRollbackError`.
    still_missing = snippet_service.find_current_version_of_snippet_with_name(
        SnippetScope.for_brand(brand.id), pair[0], pair[1]
    )
    assert still_missing is None
