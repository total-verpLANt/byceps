"""
tests.integration.services.lan_tournament.test_tournament_request_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from datetime import datetime, UTC
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session as SqlaSession
from sqlalchemy.schema import CreateIndex

from byceps.database import db
from byceps.services.lan_tournament import (
    signals as lan_tournament_signals,
    tournament_orga_repository,
    tournament_request_repository,
    tournament_request_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.dbmodels.tournament_request import (
    DbTournamentRequest,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequestStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.user import user_command_service
from byceps.util.result import Err


PARTY_ID = PartyID('lan-party-2026-requests')
OTHER_PARTY_ID = PartyID('lan-party-2026-requests-other')

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[4]
    / 'byceps'
    / 'services'
    / 'lan_tournament'
    / 'migrations'
)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Requests')


@pytest.fixture(scope='module')
def other_party(make_party, brand):
    return make_party(brand, OTHER_PARTY_ID, 'LAN Party 2026 Requests (Other)')


@pytest.fixture(scope='module')
def proposer(make_user):
    return make_user('RequestProposer')


@pytest.fixture(scope='module')
def decider(make_user):
    return make_user('RequestDecider')


def _submit_kwargs(**overrides):
    kwargs = dict(
        party_capacity=None,
        name='Sunday Cup',
        game='Rocket League',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=3,
        participant_limit=8,
        preferred_start_time=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
        description='A friendly Sunday cup.',
    )
    kwargs.update(overrides)
    return kwargs


def _read_request_via_fresh_connection(request_id):
    """Read the committed request through a separate session."""
    with SqlaSession(bind=db.engine) as fresh:
        return fresh.execute(
            select(DbTournamentRequest).filter_by(id=request_id)
        ).scalar_one_or_none()


def test_request_round_trips_through_database(party, proposer, decider):
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Round Trip Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.name == 'Round Trip Cup'
    assert db_request.status == TournamentRequestStatus.submitted.value
    assert db_request.proposer_id == proposer.id

    log_entries = tournament_request_service.get_request_history(request.id)
    assert any(
        entry.event_type == 'tournament-request-submitted'
        for entry in log_entries
    )

    update_result = tournament_request_service.update_request(
        request.id,
        proposer.id,
        **_submit_kwargs(name='Round Trip Cup Updated'),
    )
    assert update_result.is_ok()

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request.name == 'Round Trip Cup Updated'

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()
    accepted, _event = accept_result.unwrap()
    assert accepted.status is TournamentRequestStatus.accepted
    assert accepted.is_editable is False

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request.status == TournamentRequestStatus.accepted.value
    assert db_request.decided_by_id == decider.id
    assert db_request.decided_at is not None

    log_entries = tournament_request_service.get_request_history(request.id)
    event_types = {entry.event_type for entry in log_entries}
    assert 'tournament-request-submitted' in event_types
    assert 'tournament-request-edited' in event_types
    assert 'tournament-request-accepted' in event_types

    # An accepted request cannot be edited further -- editability froze.
    frozen_edit_result = tournament_request_service.update_request(
        request.id,
        proposer.id,
        **_submit_kwargs(name='Should Not Apply'),
    )
    assert frozen_edit_result.is_err()


def test_number_allocation_is_unique_under_concurrent_submits(
    party, proposer, monkeypatch
):
    """Force a stale-read number collision and prove the retry works.

    Two concurrent submits for the same party can both read the same
    `MAX(number) + 1` before either commits. Simulate that race by
    seeding a request that holds a given number, then forcing a
    second submit's first number lookup to return that same, already
    taken, number -- so its insert must hit the real UNIQUE
    constraint (`uq_lan_tournament_requests_party_number`) against
    real PostgreSQL, and the retry-once logic must recover a fresh
    number rather than surfacing the IntegrityError.
    """
    seed_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Race Seed Cup')
    )
    assert seed_result.is_ok()
    seed_request, _event = seed_result.unwrap()

    real_get_next_number = tournament_request_service.tournament_request_repository.get_next_number_for_party
    call_count = {'n': 0}

    def _stale_then_real(party_id):
        call_count['n'] += 1
        if call_count['n'] == 1:
            return seed_request.number
        return real_get_next_number(party_id)

    monkeypatch.setattr(
        tournament_request_service.tournament_request_repository,
        'get_next_number_for_party',
        _stale_then_real,
    )

    racing_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Race Second Cup')
    )

    monkeypatch.undo()

    assert racing_result.is_ok()
    racing_request, _event = racing_result.unwrap()
    assert racing_request.number != seed_request.number
    assert call_count['n'] == 2

    db_racing_request = _read_request_via_fresh_connection(racing_request.id)
    assert db_racing_request is not None
    assert db_racing_request.number == racing_request.number


def test_link_is_idempotent_under_concurrent_creates(party, proposer, decider):
    """A second link attempt racing the first, already-committed one
    (fix cycle workspace-cg0k.2) is `Err`, not a second orga
    appointment or a second log entry.

    `create_tournament` now links atomically in one transaction, so
    the "concurrent" second attempt is exercised directly against
    `link_created_tournament_flush` -- the same precondition
    `create_tournament` re-runs under the row lock.
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Linkable Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    tournament_result = tournament_service.create_tournament(
        party.id,
        'Linkable Cup Tournament',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert tournament_result.is_ok()
    tournament, _event = tournament_result.unwrap()

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.tournament_created.value
    assert db_request.created_tournament_id == tournament.id

    second_link_result = (
        tournament_request_service.link_created_tournament_flush(
            request.id, party.id, tournament.id, decider.id
        )
    )
    assert second_link_result.is_err()
    assert second_link_result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.tournament_created.value
    assert db_request.created_tournament_id == tournament.id

    log_entries = tournament_request_service.get_request_history(request.id)
    creation_entries = [
        entry
        for entry in log_entries
        if entry.event_type == 'tournament-request-tournament-created'
    ]
    assert len(creation_entries) == 1

    # `link_created_tournament_flush` is flush-only and called directly
    # here, not through a caller that commits -- roll back so its
    # `get_request_for_update` row lock does not linger on the session.
    db.session.rollback()


def test_second_create_from_same_request_returns_err(party, proposer, decider):
    """AC2: a duplicate `created_from_request_id` create returns `Err`.

    The partial unique index `uq_lan_tournaments_created_from_request_id`
    catches a double submit, back-button resubmit, or two open tabs at
    the second `create_tournament` call's own flush (fix cycle
    workspace-cg0k.2 made the tournament insert and the request link
    one transaction, but the unique index still fires at flush time,
    before that transaction ever commits) -- and the view must see
    `Err`, never an uncaught `IntegrityError` (HTTP 500).
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Duplicate Create Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    first_result = tournament_service.create_tournament(
        party.id,
        'Duplicate Create Tournament A',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert first_result.is_ok()

    second_result = tournament_service.create_tournament(
        party.id,
        'Duplicate Create Tournament B',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )

    assert second_result.is_err()
    assert second_result.unwrap_err() == (
        'A tournament has already been created from this request.'
    )

    # The service's own session -- the one `create_tournament` used
    # and the one an `IntegrityError` leaves in a failed-transaction
    # state without a rollback -- must still accept a new statement.
    # A missing/incomplete rollback would surface here as
    # `PendingRollbackError`, not in a fresh connection.
    still_usable = db.session.execute(
        select(DbTournamentRequest).filter_by(id=request.id)
    ).scalar_one_or_none()
    assert still_usable is not None

    # Exactly one tournament carries this request's link -- no orphan
    # second row from the failed second create.
    db_tournaments = (
        db.session.execute(
            select(DbTournament).filter_by(created_from_request_id=request.id)
        )
        .scalars()
        .all()
    )
    assert len(db_tournaments) == 1


# --------------------------------------------------------------------- #
# Delete a tournament created from a request (fix cycle 3, workspace-dim0.17)
# --------------------------------------------------------------------- #


def test_delete_tournament_created_from_request(party, proposer, decider):
    """AC1/AC2: deleting a tournament created from a request succeeds.

    Migration 016's `fk_lan_tournament_requests_created_tournament_id`
    carries no `ON DELETE`, so `delete_tournament` must clear the
    request's link itself before the tournament row goes away, or
    real PostgreSQL raises `ForeignKeyViolation`. After the delete,
    the request stays `tournament_created` as history, its link is
    NULL, `tournament_deleted` is True, and its history ends with the
    deletion entry carrying the tournament's name.
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Deletable Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    tournament_result = tournament_service.create_tournament(
        party.id,
        'Deletable Cup Tournament',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert tournament_result.is_ok()
    tournament, _event = tournament_result.unwrap()

    # The real defect (QT pass 2 FIX-A): this used to raise
    # `ForeignKeyViolation` on `fk_lan_tournament_requests_created_
    # tournament_id`. No exception here is the assertion.
    tournament_service.delete_tournament(tournament.id, decider.id)

    db_tournament = db.session.execute(
        select(DbTournament).filter_by(id=tournament.id)
    ).scalar_one_or_none()
    assert db_tournament is None

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.tournament_created.value
    assert db_request.created_tournament_id is None

    reloaded = tournament_request_repository.find_request(request.id)
    assert reloaded is not None
    assert reloaded.status is TournamentRequestStatus.tournament_created
    assert reloaded.created_tournament_id is None
    assert reloaded.tournament_deleted is True

    log_entries = tournament_request_service.get_request_history(request.id)
    assert log_entries[-1].event_type == (
        'tournament-request-tournament-deleted'
    )
    assert log_entries[-1].data['tournament_id'] == str(tournament.id)
    assert log_entries[-1].data['tournament_name'] == tournament.name


def test_delete_tournament_forced_failure_rolls_back_unlink(
    party, proposer, decider, monkeypatch
):
    """AC2: a failure later in the cascade rolls the unlink back too.

    The unlink is staged with `commit=False` alongside every other
    step of the cascade; forcing the final `delete_tournament` repo
    call to raise must leave the request's link exactly as it was --
    proving the unlink is not committed on its own ahead of the rest.
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Rollback Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    tournament_result = tournament_service.create_tournament(
        party.id,
        'Rollback Cup Tournament',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert tournament_result.is_ok()
    tournament, _event = tournament_result.unwrap()

    def _boom(tournament_id, *, commit=True):
        raise RuntimeError('forced failure late in the cascade')

    monkeypatch.setattr(
        tournament_service.tournament_repository, 'delete_tournament', _boom
    )

    with pytest.raises(RuntimeError, match='forced failure'):
        tournament_service.delete_tournament(tournament.id, decider.id)

    monkeypatch.undo()

    # A fresh connection cannot tell "rolled back" from "flushed but
    # never committed" -- both look like nothing happened. Commit this
    # session first: if delete_tournament's except-branch ever dropped
    # its rollback_session() call, the flushed unlink (created_
    # tournament_id cleared) would land in the database right here,
    # and the fresh-connection read below would catch it.
    db.session.commit()

    # The whole cascade -- unlink included -- must have rolled back:
    # the tournament row still exists, and the request's link is
    # untouched.
    db_tournament = db.session.execute(
        select(DbTournament).filter_by(id=tournament.id)
    ).scalar_one_or_none()
    assert db_tournament is not None

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.created_tournament_id == tournament.id
    assert db_request.status == TournamentRequestStatus.tournament_created.value


def test_recreate_after_delete_links_new_tournament(party, proposer, decider):
    """AC3: recreate after delete succeeds and re-links.

    Once the old tournament row is gone, the partial unique index
    `uq_lan_tournaments_created_from_request_id` is free again, so the
    same request can produce a second tournament, and the link points
    at the new one.
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Recreate Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    first_tournament_result = tournament_service.create_tournament(
        party.id,
        'Recreate Cup Tournament (first)',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert first_tournament_result.is_ok()
    first_tournament, _event = first_tournament_result.unwrap()

    tournament_service.delete_tournament(first_tournament.id, decider.id)

    reloaded = tournament_request_repository.find_request(request.id)
    assert reloaded is not None
    assert reloaded.tournament_deleted is True

    second_tournament_result = tournament_service.create_tournament(
        party.id,
        'Recreate Cup Tournament (second)',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert second_tournament_result.is_ok()
    second_tournament, _event = second_tournament_result.unwrap()
    assert second_tournament.id != first_tournament.id

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.tournament_created.value
    assert db_request.created_tournament_id == second_tournament.id


def test_link_refuses_overwriting_live_link(party, proposer, decider):
    """AC3: a live link is never overwritten.

    Once a request is `tournament_created` with a link to an existing
    tournament, a further `link_created_tournament_flush` call --
    pointing at a different tournament -- is refused, not silently
    applied over the original link.
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Live Link Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    tournament_result = tournament_service.create_tournament(
        party.id,
        'Live Link Tournament',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert tournament_result.is_ok()
    tournament, _event = tournament_result.unwrap()

    other_tournament_result = tournament_service.create_tournament(
        party.id,
        'Other Tournament (unrelated to the request)',
        contestant_type=ContestantType.SOLO,
    )
    assert other_tournament_result.is_ok()
    other_tournament, _event = other_tournament_result.unwrap()

    overwrite_result = tournament_request_service.link_created_tournament_flush(
        request.id, party.id, other_tournament.id, decider.id
    )

    assert overwrite_result.is_err()
    assert overwrite_result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.created_tournament_id == tournament.id

    # `link_created_tournament_flush` is flush-only and called directly
    # here, not through a caller that commits -- roll back so its
    # `get_request_for_update` row lock does not linger on the session.
    db.session.rollback()


# --------------------------------------------------------------------- #
# Create-from-request atomicity (fix cycle workspace-cg0k.2)
# --------------------------------------------------------------------- #


def _fresh_tournaments_for_request(request_id):
    """Read tournaments linked to that request through a separate session."""
    with SqlaSession(bind=db.engine) as fresh:
        return (
            fresh.execute(
                select(DbTournament).filter_by(
                    created_from_request_id=request_id
                )
            )
            .scalars()
            .all()
        )


def test_create_from_accepted_request_commits_atomically(
    party, proposer, decider
):
    """T1: tournament, request link, and audit entry are one commit.

    All three must be visible through a fresh connection -- proving
    they are actually committed, not merely flushed in the test's own
    session.
    """
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Atomic T1 Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    tournament_result = tournament_service.create_tournament(
        party.id,
        'Atomic T1 Tournament',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert tournament_result.is_ok()
    tournament, _event = tournament_result.unwrap()

    fresh_tournaments = _fresh_tournaments_for_request(request.id)
    assert len(fresh_tournaments) == 1
    assert fresh_tournaments[0].id == tournament.id

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.tournament_created.value
    assert db_request.created_tournament_id == tournament.id

    log_entries = tournament_request_service.get_request_history(request.id)
    assert any(
        entry.event_type == 'tournament-request-tournament-created'
        for entry in log_entries
    )


def test_create_from_rejected_request_returns_err_and_persists_nothing(
    party, proposer, decider
):
    """T2: a rejected request refuses the create -- no orphan tournament."""
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Atomic T2 Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    reject_result = tournament_request_service.reject_request(
        request.id, decider.id, 'Not this time'
    )
    assert reject_result.is_ok()

    received_events = []

    def _on_created(sender, *, event):
        received_events.append(event)

    lan_tournament_signals.tournament_created.connect(
        _on_created, weak=False
    )
    try:
        result = tournament_service.create_tournament(
            party.id,
            'Atomic T2 Tournament',
            contestant_type=ContestantType.SOLO,
            created_from_request_id=request.id,
            initiator_id=decider.id,
        )
    finally:
        lan_tournament_signals.tournament_created.disconnect(_on_created)

    assert result.is_err()
    assert result.unwrap_err() == 'Request is no longer in the expected state.'
    assert received_events == []

    # A fresh connection cannot tell "rolled back" from "flushed but
    # never committed" -- both look like nothing happened. Commit this
    # session first: if create_tournament's link-Err branch ever
    # dropped its rollback_session() call, the flushed orphan
    # tournament row would land in the database right here, and the
    # fresh-connection read below would catch it.
    db.session.commit()
    assert _fresh_tournaments_for_request(request.id) == []


def test_create_from_request_of_another_party_returns_err(
    party, other_party, proposer, decider
):
    """T3: a request belonging to a different party refuses the create."""
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Atomic T3 Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    result = tournament_service.create_tournament(
        other_party.id,
        'Atomic T3 Tournament (wrong party)',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Request is no longer in the expected state.'

    # See the T2 test above for why the commit comes before the
    # fresh-connection read: it turns an un-rolled-back flush into a
    # real, visible row instead of letting it hide as "nothing
    # committed either way".
    db.session.commit()
    assert _fresh_tournaments_for_request(request.id) == []

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.accepted.value
    assert db_request.created_tournament_id is None


def test_create_from_request_link_failure_after_flush_persists_nothing(
    party, proposer, decider, monkeypatch
):
    """T4: a failure in the link step, after the tournament insert has
    already flushed, must not leave a persisted tournament behind --
    the insert and the link are one transaction, not two."""
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Atomic T4 Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    def _boom(*args, **kwargs):
        return Err('forced failure injected by test')

    monkeypatch.setattr(
        tournament_service.tournament_request_service,
        'link_created_tournament_flush',
        _boom,
    )

    received_events = []

    def _on_created(sender, *, event):
        received_events.append(event)

    lan_tournament_signals.tournament_created.connect(
        _on_created, weak=False
    )
    try:
        result = tournament_service.create_tournament(
            party.id,
            'Atomic T4 Tournament',
            contestant_type=ContestantType.SOLO,
            created_from_request_id=request.id,
            initiator_id=decider.id,
        )
    finally:
        monkeypatch.undo()
        lan_tournament_signals.tournament_created.disconnect(_on_created)

    assert result.is_err()
    assert result.unwrap_err() == 'forced failure injected by test'
    assert received_events == []

    # See the T2 test above for why the commit comes before the
    # fresh-connection read: it turns an un-rolled-back flush into a
    # real, visible row instead of letting it hide as "nothing
    # committed either way".
    db.session.commit()
    assert _fresh_tournaments_for_request(request.id) == []

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.accepted.value
    assert db_request.created_tournament_id is None


def test_create_from_request_link_raise_after_staging_write_rolls_back(
    party, proposer, decider, monkeypatch
):
    """T4 variant: the link step can raise, not just return `Err`, after
    it has already staged its own write via the real repository. That
    write must not survive -- and the caller's `db.session` must still
    be usable right after, proving the exception handler around the
    link step actually rolls back rather than leaving the session
    pending-rollback."""
    submit_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Atomic T4b Cup')
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    def _boom_after_staging_write(
        request_id, party_id, tournament_id, decider_id, **kwargs
    ):
        staged_request = tournament_request_repository.get_request_for_update(
            request_id
        )
        updated = dataclasses.replace(
            staged_request,
            status=TournamentRequestStatus.tournament_created,
            created_tournament_id=tournament_id,
            updated_at=datetime.now(UTC),
        )
        # Stage a real write via the real repository (flushed, not yet
        # committed), then force a genuine DBAPI-level error -- not a
        # bare Python raise, which SQLAlchemy would not treat as
        # poisoning the session -- so this exercises the same
        # DataError/OperationalError-from-the-flush scenario
        # `create_tournament`'s exception handling exists for.
        tournament_request_repository.update_request_flush(updated)
        db.session.execute(text('SELECT 1/0'))

    monkeypatch.setattr(
        tournament_service.tournament_request_service,
        'link_created_tournament_flush',
        _boom_after_staging_write,
    )

    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 -- a raw DBAPI error
            tournament_service.create_tournament(
                party.id,
                'Atomic T4b Tournament',
                contestant_type=ContestantType.SOLO,
                created_from_request_id=request.id,
                initiator_id=decider.id,
            )
    finally:
        monkeypatch.undo()

    assert _fresh_tournaments_for_request(request.id) == []

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.accepted.value
    assert db_request.created_tournament_id is None

    # The session must be usable right after -- a botched rollback
    # would leave Postgres's own transaction aborted and this would
    # raise (`PendingRollbackError` / "current transaction is aborted").
    usable = db.session.execute(
        select(DbTournamentRequest).filter_by(id=request.id)
    ).scalar_one_or_none()
    assert usable is not None
    db.session.rollback()


def test_create_from_request_skips_orga_appointment_for_suspended_proposer(
    party, decider, make_user
):
    """T5: a suspended proposer still gets their tournament, but is never
    appointed as its orga -- and the outcome says so."""
    suspended_proposer = make_user('SuspendedRequestProposer')

    submit_result = tournament_request_service.submit_request(
        party.id,
        suspended_proposer.id,
        **_submit_kwargs(name='Atomic T5 Cup'),
    )
    assert submit_result.is_ok()
    request, _event = submit_result.unwrap()

    accept_result = tournament_request_service.accept_request(
        request.id, decider.id
    )
    assert accept_result.is_ok()

    user_command_service.suspend_account(
        suspended_proposer, decider, 'test: proposer suspended'
    )

    tournament_result = tournament_service.create_tournament(
        party.id,
        'Atomic T5 Tournament',
        contestant_type=ContestantType.SOLO,
        created_from_request_id=request.id,
        initiator_id=decider.id,
    )
    assert tournament_result.is_ok()
    tournament, _event = tournament_result.unwrap()

    db_request = _read_request_via_fresh_connection(request.id)
    assert db_request is not None
    assert db_request.status == TournamentRequestStatus.tournament_created.value
    assert db_request.created_tournament_id == tournament.id

    orga_result = tournament_request_service.appoint_proposer_orga(
        tournament.id, suspended_proposer.id, decider.id
    )

    assert orga_result.is_err()
    assert orga_result.unwrap_err() == (
        'The proposer could not be appointed as orga of the tournament.'
    )
    assert not tournament_orga_repository.exists_orga_for_tournament_and_user(
        tournament.id, suspended_proposer.id
    )


def test_plain_create_without_request_commits_and_fires_signal_once(party):
    """T6: a plain create (no request) behaves exactly as before it --
    one commit, one `tournament_created` signal."""
    received_events = []

    def _on_created(sender, *, event):
        received_events.append(event)

    lan_tournament_signals.tournament_created.connect(
        _on_created, weak=False
    )
    try:
        result = tournament_service.create_tournament(
            party.id,
            'Atomic T6 Plain Tournament',
            contestant_type=ContestantType.SOLO,
        )
    finally:
        lan_tournament_signals.tournament_created.disconnect(_on_created)

    assert result.is_ok()
    tournament, event = result.unwrap()

    assert len(received_events) == 1
    assert received_events[0] is event

    with SqlaSession(bind=db.engine) as fresh:
        fresh_tournament = fresh.execute(
            select(DbTournament).filter_by(id=tournament.id)
        ).scalar_one_or_none()
    assert fresh_tournament is not None
    assert fresh_tournament.created_from_request_id is None


def test_count_requests_for_party_with_statuses_filters_by_value(
    party, decider, make_user
):
    """AC4: the nav COUNT query filters against the DB's `.value` column.

    Seed one `submitted` and one `accepted` request (both "pending"
    for the nav badge) plus one `rejected` request (not pending), then
    check the count against real PostgreSQL -- a mocked repository, or
    a `.name` instead of `.value` slip in the query, would not catch a
    mismatch against the column's actual stored strings.

    Uses its own proposer rather than the module-scoped `proposer`
    fixture: that one accumulates `submitted` requests left behind by
    earlier tests in this module, and three fresh submissions here
    would trip `MAX_OPEN_REQUESTS_PER_PROPOSER`.
    """
    proposer = make_user('CountRequestsProposer')

    pending_statuses = frozenset(
        {TournamentRequestStatus.submitted, TournamentRequestStatus.accepted}
    )
    count_before = (
        tournament_request_repository.count_requests_for_party_with_statuses(
            party.id, pending_statuses
        )
    )

    left_submitted_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Count Pending Cup A')
    )
    assert left_submitted_result.is_ok()

    to_accept_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Count Pending Cup B')
    )
    assert to_accept_result.is_ok()
    to_accept_request, _event = to_accept_result.unwrap()
    accept_result = tournament_request_service.accept_request(
        to_accept_request.id, decider.id
    )
    assert accept_result.is_ok()

    to_reject_result = tournament_request_service.submit_request(
        party.id, proposer.id, **_submit_kwargs(name='Count Rejected Cup')
    )
    assert to_reject_result.is_ok()
    to_reject_request, _event = to_reject_result.unwrap()
    reject_result = tournament_request_service.reject_request(
        to_reject_request.id, decider.id, 'No thanks'
    )
    assert reject_result.is_ok()

    count_after = (
        tournament_request_repository.count_requests_for_party_with_statuses(
            party.id, pending_statuses
        )
    )

    # Two new pending requests (one left `submitted`, one `accepted`);
    # the `rejected` one must not be in that delta.
    assert count_after - count_before == 2


def _strip_transaction_control(sql: str) -> str:
    """Remove the `BEGIN;` and `COMMIT;` lines from a migration."""
    return '\n'.join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {'BEGIN;', 'COMMIT;'}
    )


def _table_exists(connection, table_name: str) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_name = :table_name'
            ')'
        ),
        {'table_name': table_name},
    ).scalar_one()


def _index_exists(connection, index_name: str) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM pg_indexes '
            'WHERE indexname = :index_name'
            ')'
        ),
        {'index_name': index_name},
    ).scalar_one()


def _column_exists(connection, table_name: str, column_name: str) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table_name AND column_name = :column_name'
            ')'
        ),
        {'table_name': table_name, 'column_name': column_name},
    ).scalar_one()


def _log_entry_exists(connection, entry_id) -> bool:
    return connection.execute(
        text(
            'SELECT EXISTS ('
            'SELECT 1 FROM lan_tournament_log_entries WHERE id = :entry_id'
            ')'
        ),
        {'entry_id': entry_id},
    ).scalar_one()


def _request_log_entry_count(connection) -> int:
    return connection.execute(
        text(
            'SELECT COUNT(*) FROM lan_tournament_log_entries '
            "WHERE starts_with(event_type, 'tournament-request-')"
        )
    ).scalar_one()


def test_rollback_016_removes_all_objects():
    """Run migration 016 and its rollback in a transaction rolled back."""
    forward_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / '016_add_tournament_requests.sql').read_text()
    )
    rollback_sql = _strip_transaction_control(
        (MIGRATIONS_DIR / 'rollback_016.sql').read_text()
    )

    # Release the session's locks, or the DROP TABLE below blocks.
    db.session.close()

    # Print and assert the target database before any DDL, per the
    # epic's hard rule: never let a scratch/verification run land
    # against the shared fixture database by accident. Parallel test
    # runs use an isolated `POSTGRES_DB` override (e.g.
    # `byceps_test_m1`), so accept any `byceps_test*` name rather than
    # the literal default -- only the prefix is the actual guarantee.
    print(f'db.engine.url = {db.engine.url!r}')
    expected_db_name = os.environ.get('POSTGRES_DB', 'byceps_test')
    assert db.engine.url.database == expected_db_name
    assert expected_db_name.startswith('byceps_test')

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(forward_sql)

            assert _table_exists(connection, 'lan_tournament_requests')
            assert _index_exists(
                connection, 'ix_lan_tournament_requests_party_id'
            )
            assert _index_exists(
                connection, 'ix_lan_tournament_requests_proposer_id'
            )
            assert _index_exists(
                connection, 'ix_lan_tournament_requests_status'
            )
            assert _index_exists(
                connection,
                'ix_lan_tournament_requests_created_tournament_id',
            )
            assert _column_exists(
                connection, 'lan_tournaments', 'created_from_request_id'
            )
            assert _index_exists(
                connection, 'uq_lan_tournaments_created_from_request_id'
            )

            # Seed one request audit entry (must be deleted by the
            # rollback) and one unrelated entry (must survive it).
            request_log_id = uuid4()
            other_log_id = uuid4()
            connection.execute(
                text(
                    'INSERT INTO lan_tournament_log_entries '
                    '(id, occurred_at, event_type, tournament_id, '
                    'initiator_id, data) '
                    "VALUES (:id, now(), 'tournament-request-submitted', "
                    ":tournament_id, NULL, '{}'::jsonb)"
                ),
                {'id': request_log_id, 'tournament_id': uuid4()},
            )
            connection.execute(
                text(
                    'INSERT INTO lan_tournament_log_entries '
                    '(id, occurred_at, event_type, tournament_id, '
                    'initiator_id, data) '
                    "VALUES (:id, now(), 'tournament-status-changed', "
                    ":tournament_id, NULL, '{}'::jsonb)"
                ),
                {'id': other_log_id, 'tournament_id': uuid4()},
            )
            assert _log_entry_exists(connection, request_log_id)
            assert _log_entry_exists(connection, other_log_id)

            connection.exec_driver_sql(rollback_sql)

            assert not _table_exists(connection, 'lan_tournament_requests')
            assert not _index_exists(
                connection, 'ix_lan_tournament_requests_party_id'
            )
            assert not _index_exists(
                connection, 'ix_lan_tournament_requests_proposer_id'
            )
            assert not _index_exists(
                connection, 'ix_lan_tournament_requests_status'
            )
            assert not _index_exists(
                connection,
                'ix_lan_tournament_requests_created_tournament_id',
            )
            assert not _column_exists(
                connection, 'lan_tournaments', 'created_from_request_id'
            )
            assert not _index_exists(
                connection, 'uq_lan_tournaments_created_from_request_id'
            )

            # The request audit entry is gone; the unrelated one, and
            # nothing else matching the request event-type prefix,
            # remains.
            assert not _log_entry_exists(connection, request_log_id)
            assert _log_entry_exists(connection, other_log_id)
            assert _request_log_entry_count(connection) == 0
        finally:
            transaction.rollback()


def test_dbmodel_schema_has_created_tournament_id_partial_index():
    """create_all()'s schema carries the partial index too (F-17 issue d).

    The migration text and the dbmodel `Index` must declare the same
    name, column and predicate, or a fresh `create_all()`-built test
    database and a migrated production one would diverge.
    """
    indexdef = db.session.execute(
        text(
            'SELECT indexdef FROM pg_indexes '
            "WHERE indexname = "
            "'ix_lan_tournament_requests_created_tournament_id'"
        )
    ).scalar_one()

    assert 'lan_tournament_requests' in indexdef
    assert 'created_tournament_id' in indexdef
    assert 'WHERE (created_tournament_id IS NOT NULL)' in indexdef

    dbmodel_index = next(
        idx
        for idx in DbTournamentRequest.__table__.indexes
        if idx.name == 'ix_lan_tournament_requests_created_tournament_id'
    )
    compiled = str(
        CreateIndex(dbmodel_index).compile(dialect=postgresql.dialect())
    )
    assert 'created_tournament_id' in compiled
    assert 'WHERE created_tournament_id IS NOT NULL' in compiled
