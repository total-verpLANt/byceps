"""
tests.integration.services.lan_tournament.test_tournament_request_timezones
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session as SqlaSession

from byceps.database import db
from byceps.services.lan_tournament import tournament_request_service
from byceps.services.lan_tournament.dbmodels.tournament_request import (
    DbTournamentRequest,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.party.models import PartyID


PARTY_ID = PartyID('lan-party-2026-request-tz')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Request TZ')


@pytest.fixture(scope='module')
def proposer(make_user):
    return make_user('RequestTzProposer')


def _submit_kwargs(**overrides):
    kwargs = dict(
        party_capacity=None,
        name='Timezone Cup',
        game='Rocket League',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=3,
        participant_limit=8,
        # Naive, of the same shape `flask_babel.to_utc` hands the
        # views: by contract, this instant is 11:00 UTC.
        preferred_start_time=datetime(2026, 1, 15, 11, 0),
        preferred_end_time=datetime(2026, 1, 15, 15, 0),
        description='Naive input, submitted under a Europe/Berlin session.',
    )
    kwargs.update(overrides)
    return kwargs


def _read_request_via_fresh_connection(request_id):
    """Read the committed request through a separate session.

    A fresh `Session` bound to `db.engine`, rather than the shared
    `db.session`, sidesteps `db.session`'s identity map.
    """
    with SqlaSession(bind=db.engine) as fresh:
        return fresh.execute(
            select(DbTournamentRequest).filter_by(id=request_id)
        ).scalar_one_or_none()


def test_submit_request_under_berlin_session_timezone_persists_utc_instant(
    party, proposer
):
    """A naive datetime submitted under a non-UTC session `TIME ZONE`
    must still persist, and read back, as the same UTC instant.

    `flask_babel.to_utc` hands the views a naive datetime that is, by
    contract, already UTC. `submit_request` must normalize it to aware
    UTC *before* the insert, so the `TIMESTAMPTZ` bind carries its own
    offset and does not depend on the session's `TIME ZONE`. The bug
    this guards against: binding the naive value as-is would have
    Postgres interpret 11:00 as *Europe/Berlin* local time and store
    10:00 UTC instead (CET is UTC+1 in January, no DST).

    `SET LOCAL TIME ZONE` (not the session-level `SET TIME ZONE`) is
    scoped to the current transaction: Postgres reverts it itself at
    COMMIT or ROLLBACK, before the connection goes back to the pool.
    That transaction is `db.session`'s: this call's `execute` is what
    triggers the ORM `Session`'s autobegin, and `submit_request` never
    commits or rolls back before its own final `db.session.commit()`
    (its repository calls -- `count_open_requests_for_proposer`,
    `get_next_number_for_party`, `create_request` -- all read/write
    through that same `db.session`, never a separate `Session` or
    `Connection`), so the insert runs inside the very transaction this
    `SET LOCAL` opened. There is nothing to restore afterwards, and
    nothing this test can leak into another connection.
    """
    db.session.execute(text("SET LOCAL TIME ZONE 'Europe/Berlin'"))
    result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs()
    )

    assert result.is_ok()
    request, _event = result.unwrap()
    assert request.preferred_start_time == datetime(
        2026, 1, 15, 11, 0, tzinfo=UTC
    )
    assert request.preferred_start_time.tzinfo is UTC
    assert request.preferred_end_time == datetime(
        2026, 1, 15, 15, 0, tzinfo=UTC
    )
    assert request.preferred_end_time.tzinfo is UTC

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.preferred_start_time == datetime(
        2026, 1, 15, 11, 0, tzinfo=UTC
    )
    assert db_request.preferred_end_time == datetime(
        2026, 1, 15, 15, 0, tzinfo=UTC
    )
