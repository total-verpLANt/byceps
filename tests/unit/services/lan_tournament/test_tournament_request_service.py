"""
tests.unit.services.lan_tournament.test_tournament_request_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from byceps.services.lan_tournament import tournament_request_service
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_log_entry import (
    TournamentLogEntry,
    TournamentLogEntryID,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


PARTY_ID = PartyID('lan-party-2026-requests')

MOCK_PREFIX = 'byceps.services.lan_tournament.tournament_request_service'


def _make_request(
    *,
    request_id=None,
    party_id=PARTY_ID,
    number=1,
    proposer_id=None,
    status=TournamentRequestStatus.submitted,
    name='Sunday Cup',
    game='Rocket League',
    game_format=GameFormat.ONE_V_ONE,
    elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    team_size=3,
    participant_limit=8,
    preferred_start_time=None,
    preferred_end_time=None,
    description='A friendly Sunday cup.',
    special_rules=None,
    notes=None,
    desired_template=None,
    decided_at=None,
    decided_by_id=None,
    rejection_reason=None,
    created_tournament_id=None,
    updated_at=None,
) -> TournamentRequest:
    if request_id is None:
        request_id = TournamentRequestID(generate_uuid())
    if proposer_id is None:
        proposer_id = UserID(generate_uuid())
    if preferred_start_time is None:
        preferred_start_time = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
    if preferred_end_time is None:
        preferred_end_time = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)

    return TournamentRequest(
        id=request_id,
        party_id=party_id,
        number=number,
        proposer_id=proposer_id,
        created_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        updated_at=updated_at,
        status=status,
        name=name,
        game=game,
        game_format=game_format,
        elimination_mode=elimination_mode,
        team_size=team_size,
        participant_limit=participant_limit,
        preferred_start_time=preferred_start_time,
        preferred_end_time=preferred_end_time,
        description=description,
        special_rules=special_rules,
        notes=notes,
        desired_template=desired_template,
        decided_at=decided_at,
        decided_by_id=decided_by_id,
        rejection_reason=rejection_reason,
        created_tournament_id=created_tournament_id,
    )


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


# -------------------------------------------------------------------- #
# submit_request
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_persists_all_mandatory_fields(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    proposer_id = UserID(generate_uuid())
    mock_repo.count_open_requests_for_proposer.return_value = 0
    mock_repo.get_next_number_for_party.return_value = 1

    result = tournament_request_service.submit_request(
        PARTY_ID, proposer_id, **_submit_kwargs()
    )

    assert result.is_ok()
    request, event = result.unwrap()

    assert request.party_id == PARTY_ID
    assert request.proposer_id == proposer_id
    assert request.status is TournamentRequestStatus.submitted
    assert request.name == 'Sunday Cup'
    assert request.game == 'Rocket League'
    assert request.game_format is GameFormat.ONE_V_ONE
    assert request.elimination_mode is EliminationMode.SINGLE_ELIMINATION
    assert request.team_size == 3
    assert request.participant_limit == 8
    assert request.description == 'A friendly Sunday cup.'

    mock_repo.create_request.assert_called_once_with(request)
    mock_db.session.commit.assert_called_once()

    assert event.request_id == request.id
    assert event.party_id == PARTY_ID
    assert event.proposer_id == proposer_id
    mock_signals.tournament_request_submitted.send.assert_called_once_with(
        None, event=event
    )


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_allocates_sequential_number_per_party(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.count_open_requests_for_proposer.return_value = 0
    mock_repo.get_next_number_for_party.return_value = 7

    result = tournament_request_service.submit_request(
        PARTY_ID, UserID(generate_uuid()), **_submit_kwargs()
    )

    assert result.is_ok()
    request, _event = result.unwrap()
    assert request.number == 7

    mock_repo.get_next_number_for_party.assert_called_once_with(PARTY_ID)


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_rejects_fourth_open_request(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.count_open_requests_for_proposer.return_value = (
        tournament_request_service.MAX_OPEN_REQUESTS_PER_PROPOSER
    )

    result = tournament_request_service.submit_request(
        PARTY_ID, UserID(generate_uuid()), **_submit_kwargs()
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Too many open tournament requests.'

    mock_repo.create_request.assert_not_called()
    mock_db.session.commit.assert_not_called()
    mock_signals.tournament_request_submitted.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_stores_optional_fields_as_none_when_absent(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.count_open_requests_for_proposer.return_value = 0
    mock_repo.get_next_number_for_party.return_value = 1

    result = tournament_request_service.submit_request(
        PARTY_ID,
        UserID(generate_uuid()),
        **_submit_kwargs(
            special_rules='   ',
            notes='',
            desired_template=None,
        ),
    )

    assert result.is_ok()
    request, _event = result.unwrap()
    assert request.special_rules is None
    assert request.notes is None
    assert request.desired_template is None


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_persists_aware_utc_from_naive_input(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """`flask_babel.to_utc` hands the views' naive UTC shape down here."""
    mock_repo.count_open_requests_for_proposer.return_value = 0
    mock_repo.get_next_number_for_party.return_value = 1

    result = tournament_request_service.submit_request(
        PARTY_ID,
        UserID(generate_uuid()),
        **_submit_kwargs(
            preferred_start_time=datetime(2026, 10, 24, 18, 0),
            preferred_end_time=datetime(2026, 10, 24, 22, 0),
        ),
    )

    assert result.is_ok()
    request, _event = result.unwrap()
    assert request.preferred_start_time == datetime(
        2026, 10, 24, 18, 0, tzinfo=UTC
    )
    assert request.preferred_start_time.tzinfo is UTC
    assert request.preferred_end_time == datetime(
        2026, 10, 24, 22, 0, tzinfo=UTC
    )
    assert request.preferred_end_time.tzinfo is UTC


# -------------------------------------------------------------------- #
# update_request
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_records_changed_field_names(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(name='Old Name', game='Old Game')
    editor_id = existing.proposer_id
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id,
        editor_id,
        **_submit_kwargs(
            name='New Name',
            game='Old Game',
        ),
    )

    assert result.is_ok()
    updated, event = result.unwrap()
    assert updated.name == 'New Name'

    mock_repo.update_request_flush.assert_called_once_with(updated)

    _args, kwargs = mock_log_service.create_log_entry.call_args
    assert set(kwargs['data']['changed_fields']) == {'name'}
    assert kwargs['data']['by'] == 'proposer'

    assert event.request_id == existing.id
    mock_signals.tournament_request_edited.send.assert_called_once_with(
        None, event=event
    )


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_with_naive_datetimes_of_same_instant_audits_name_only(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """A naive edit of the same instant must not falsely audit the times.

    The stored request holds aware UTC datetimes (read back from
    `TIMESTAMPTZ`). The view passes `flask_babel.to_utc`'s naive
    shape. Without normalization, `aware != naive` is always `True` in
    Python, so every edit would wrongly claim both preferred times
    changed, even when only `name` did.
    """
    existing = _make_request(
        name='Old Name',
        preferred_start_time=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
    )
    editor_id = existing.proposer_id
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id,
        editor_id,
        **_submit_kwargs(
            name='New Name',
            preferred_start_time=datetime(2026, 10, 24, 18, 0),
            preferred_end_time=datetime(2026, 10, 24, 22, 0),
        ),
    )

    assert result.is_ok()
    updated, _event = result.unwrap()
    assert updated.preferred_start_time.tzinfo is UTC
    assert updated.preferred_end_time.tzinfo is UTC

    _args, kwargs = mock_log_service.create_log_entry.call_args
    assert set(kwargs['data']['changed_fields']) == {'name'}


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_on_accepted_returns_err(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id, UserID(generate_uuid()), **_submit_kwargs()
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_edited.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_rejects_non_proposer(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Mirrors `withdraw_request`'s ownership guard: a `by='proposer'`
    edit from anyone but the request's own proposer is refused."""
    proposer_id = UserID(generate_uuid())
    other_user_id = UserID(generate_uuid())
    existing = _make_request(proposer_id=proposer_id)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id, other_user_id, **_submit_kwargs()
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Only the proposer may edit this request.'
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_edited.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_rejects_unrecognized_by_value_for_non_proposer(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Regression: the ownership check used to fail open for any `by`
    other than `'proposer'` -- only `by == 'admin'` may now skip it,
    so an unrecognized value such as `'orga'` must still be refused
    for a non-proposer editor."""
    proposer_id = UserID(generate_uuid())
    other_user_id = UserID(generate_uuid())
    existing = _make_request(proposer_id=proposer_id)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id, other_user_id, by='orga', **_submit_kwargs()
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Only the proposer may edit this request.'
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_edited.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_by_admin_skips_proposer_ownership_check(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """`by='admin'` (an admin editing on the proposer's behalf) is not
    subject to the proposer-ownership guard at all."""
    proposer_id = UserID(generate_uuid())
    admin_id = UserID(generate_uuid())
    existing = _make_request(proposer_id=proposer_id, name='Old Name')
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id,
        admin_id,
        by='admin',
        **_submit_kwargs(name='New Name'),
    )

    assert result.is_ok()
    updated, _event = result.unwrap()
    assert updated.name == 'New Name'


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_expected_status_cannot_widen_past_submitted(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """`expected_status` may only NARROW the allowed-from set (here,
    just `submitted`), never widen it -- passing the request's own
    non-submitted status back as `expected_status` must not let an
    edit through it."""
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id,
        existing.proposer_id,
        expected_status=TournamentRequestStatus.accepted,
        **_submit_kwargs(),
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_edited.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_update_request_expected_status_narrowing_still_refuses_mismatch(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Narrowing to the one allowed status does not create a second
    way in: it still refuses a request that is not actually in it."""
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.update_request(
        existing.id,
        existing.proposer_id,
        expected_status=TournamentRequestStatus.submitted,
        **_submit_kwargs(),
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()


# -------------------------------------------------------------------- #
# withdraw_request
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_withdraw_request_rejects_non_proposer(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    proposer_id = UserID(generate_uuid())
    other_user_id = UserID(generate_uuid())
    existing = _make_request(proposer_id=proposer_id)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.withdraw_request(
        existing.id, other_user_id
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Only the proposer may withdraw this request.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_withdrawn.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_withdraw_request_expected_status_cannot_widen_past_submitted(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Same widening guard as `update_request`: an `accepted` request
    must not be withdrawable just because a caller passes
    `expected_status=accepted`."""
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.withdraw_request(
        existing.id,
        existing.proposer_id,
        expected_status=TournamentRequestStatus.accepted,
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_withdrawn.send.assert_not_called()


# -------------------------------------------------------------------- #
# accept_request
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_accept_request_creates_no_tournament(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request()
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.accept_request(
        existing.id, UserID(generate_uuid())
    )

    assert result.is_ok()
    updated, _event = result.unwrap()
    assert updated.status is TournamentRequestStatus.accepted
    assert updated.created_tournament_id is None
    assert not hasattr(tournament_request_service, 'tournament_service')


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_accept_request_freezes_editability(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request()
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.accept_request(
        existing.id, UserID(generate_uuid())
    )

    assert result.is_ok()
    updated, _event = result.unwrap()
    assert updated.is_editable is False


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_accept_request_on_already_accepted_returns_err(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.accept_request(
        existing.id, UserID(generate_uuid())
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_accepted.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_accept_request_expected_status_cannot_revive_rejected(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Regression: `accept_request(expected_status=rejected)` used to
    accept -- and thus revive -- an already-rejected request, since
    the old default-argument shape let `expected_status` replace the
    precondition instead of narrowing it."""
    existing = _make_request(status=TournamentRequestStatus.rejected)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.accept_request(
        existing.id,
        UserID(generate_uuid()),
        expected_status=TournamentRequestStatus.rejected,
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_accepted.send.assert_not_called()


# -------------------------------------------------------------------- #
# reject_request
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_requires_reason(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    result = tournament_request_service.reject_request(
        TournamentRequestID(generate_uuid()),
        UserID(generate_uuid()),
        '   ',
    )

    assert result.is_err()
    assert result.unwrap_err() == ('A reason is required to reject a request.')
    mock_repo.get_request_for_update.assert_not_called()
    mock_signals.tournament_request_rejected.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_accepted_request_is_allowed(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.reject_request(
        existing.id, UserID(generate_uuid()), 'Venue unavailable'
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.status is TournamentRequestStatus.rejected
    assert updated.rejection_reason == 'Venue unavailable'
    mock_signals.tournament_request_rejected.send.assert_called_once()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_expected_status_cannot_widen_past_submitted_or_accepted(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Regression: `reject_request(expected_status=withdrawn)` used to
    move an already-`withdrawn` request straight to `rejected`,
    since the old default-argument shape let `expected_status`
    replace the `{submitted, accepted}` precondition instead of
    narrowing it."""
    existing = _make_request(status=TournamentRequestStatus.withdrawn)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.reject_request(
        existing.id,
        UserID(generate_uuid()),
        'Some reason',
        expected_status=TournamentRequestStatus.withdrawn,
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_rejected.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_expected_status_narrowing_refuses_other_allowed_status(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """`expected_status` may pin the transition to just one of the two
    allowed-from statuses; narrowing to `submitted` must still refuse
    a request that is `accepted`, even though `accepted` is itself in
    the allowed set."""
    existing = _make_request(status=TournamentRequestStatus.accepted)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.reject_request(
        existing.id,
        UserID(generate_uuid()),
        'Some reason',
        expected_status=TournamentRequestStatus.submitted,
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()
    mock_signals.tournament_request_rejected.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_reason_of_exactly_max_length_is_allowed(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(status=TournamentRequestStatus.submitted)
    mock_repo.get_request_for_update.return_value = existing
    reason = 'x' * 2000

    result = tournament_request_service.reject_request(
        existing.id, UserID(generate_uuid()), reason
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.rejection_reason == reason


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_reason_over_max_length_is_refused(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Mirrors `TournamentRequestRejectForm`'s `Length(max=2000)` at
    the service boundary, so a caller that bypasses the form (or a
    future non-admin caller) cannot persist an over-long reason."""
    reason = 'x' * 2001

    result = tournament_request_service.reject_request(
        TournamentRequestID(generate_uuid()), UserID(generate_uuid()), reason
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'The reason must not exceed 2000 characters.'
    )
    mock_repo.get_request_for_update.assert_not_called()
    mock_signals.tournament_request_rejected.send.assert_not_called()


# -------------------------------------------------------------------- #
# link_created_tournament_flush
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_sets_origin_and_status(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(status=TournamentRequestStatus.accepted)
    tournament_id = TournamentID(generate_uuid())
    decider_id = UserID(generate_uuid())
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id, existing.party_id, tournament_id, decider_id
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.status is TournamentRequestStatus.tournament_created
    assert updated.created_tournament_id == tournament_id

    mock_repo.update_request_flush.assert_called_once_with(updated)
    _args, kwargs = mock_log_service.create_log_entry.call_args
    assert kwargs['data'] == {'tournament_id': str(tournament_id)}

    # Flush only -- committing is the caller's job
    # (`tournament_service.create_tournament`), so this must never
    # commit on its own.
    mock_db.session.commit.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_refuses_other_party(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """I2: a request belonging to a different party than the one
    creating the tournament is refused, even though the request itself
    is `accepted`."""
    existing = _make_request(
        status=TournamentRequestStatus.accepted,
        party_id=PartyID('some-other-party'),
    )
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id, PARTY_ID, TournamentID(generate_uuid()),
        UserID(generate_uuid()),
    )

    assert result.is_err()
    assert (
        result.unwrap_err() == 'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_returns_err_for_unknown_request(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """I2: an unknown request ID is an `Err`, never a raised exception."""
    mock_repo.get_request_for_update.side_effect = ValueError(
        'Unknown tournament request ID "..."'
    )

    result = tournament_request_service.link_created_tournament_flush(
        TournamentRequestID(generate_uuid()),
        PARTY_ID,
        TournamentID(generate_uuid()),
        UserID(generate_uuid()),
    )

    assert result.is_err()
    assert (
        result.unwrap_err() == 'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()


# -------------------------------------------------------------------- #
# link_created_tournament_flush -- recreate after delete_tournament (.17)
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_allows_recreate_after_delete(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """AC3: `tournament_deleted` (link cleared) may be linked again.

    `expected_status` still defaults to `accepted`; the request here
    is `tournament_created` with no live link, which only
    `_is_linkable`'s special case lets through.
    """
    existing = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )
    new_tournament_id = TournamentID(generate_uuid())
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id, existing.party_id, new_tournament_id,
        UserID(generate_uuid()),
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.status is TournamentRequestStatus.tournament_created
    assert updated.created_tournament_id == new_tournament_id


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_explicit_accepted_refuses_recreate_after_delete(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """G3 (bead workspace-c9o4.3): `_is_linkable` used to gate the
    `tournament_created`+`tournament_deleted` special case on
    `tournament_deleted` alone, ignoring `expected_status` entirely --
    so an explicit `expected_status=accepted` would still re-link a
    deleted-tournament request, even though `accepted` narrowing must
    never admit a `tournament_created` request. Only `expected_status
    =None` (the default) or `expected_status=tournament_created` may
    let the recreate-after-delete case through.
    """
    existing = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
    )
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id,
        existing.party_id,
        TournamentID(generate_uuid()),
        UserID(generate_uuid()),
        expected_status=TournamentRequestStatus.accepted,
    )

    assert result.is_err()
    assert (
        result.unwrap_err() == 'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_refuses_overwriting_live_link(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """AC3: `tournament_created` with a *live* link is never re-linked.

    A request whose `created_tournament_id` still points at an
    existing tournament must be refused -- that link must never be
    overwritten, no matter what `expected_status` a future caller
    might pass.
    """
    live_tournament_id = TournamentID(generate_uuid())
    existing = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=live_tournament_id,
    )
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id, existing.party_id, TournamentID(generate_uuid()),
        UserID(generate_uuid()),
    )

    assert result.is_err()
    assert (
        result.unwrap_err() == 'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_expected_status_cannot_link_rejected(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Regression: `link_created_tournament_flush(expected_status=
    rejected)` used to link a rejected request to a tournament,
    since the old default-argument shape let `expected_status`
    replace the `{accepted}` precondition (plus the `tournament_created`
    special case) instead of narrowing it."""
    existing = _make_request(status=TournamentRequestStatus.rejected)
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id,
        existing.party_id,
        TournamentID(generate_uuid()),
        UserID(generate_uuid()),
        expected_status=TournamentRequestStatus.rejected,
    )

    assert result.is_err()
    assert (
        result.unwrap_err() == 'Request is no longer in the expected state.'
    )
    mock_repo.update_request_flush.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_link_created_tournament_flush_explicit_narrow_to_accepted_still_works(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """Narrowing to the one allowed status (`accepted`) explicitly
    must not break the happy path the default (now `None`) covers."""
    existing = _make_request(status=TournamentRequestStatus.accepted)
    tournament_id = TournamentID(generate_uuid())
    mock_repo.get_request_for_update.return_value = existing

    result = tournament_request_service.link_created_tournament_flush(
        existing.id,
        existing.party_id,
        tournament_id,
        UserID(generate_uuid()),
        expected_status=TournamentRequestStatus.accepted,
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.created_tournament_id == tournament_id


# -------------------------------------------------------------------- #
# appoint_proposer_orga
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_success(mock_user_service, mock_orga_service):
    from byceps.util.result import Ok

    proposer_id = UserID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    decider_id = UserID(generate_uuid())
    mock_user_service.find_user.return_value = SimpleNamespace(
        id=proposer_id, deleted=False, suspended=False
    )
    mock_orga_service.assign_orga.return_value = Ok((Mock(), Mock()))

    result = tournament_request_service.appoint_proposer_orga(
        tournament_id, proposer_id, decider_id
    )

    assert result.is_ok()
    mock_orga_service.assign_orga.assert_called_once_with(
        tournament_id, proposer_id, decider_id
    )


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_skips_deleted_proposer(
    mock_user_service, mock_orga_service
):
    proposer_id = UserID(generate_uuid())
    mock_user_service.find_user.return_value = SimpleNamespace(
        id=proposer_id, deleted=True, suspended=False
    )

    result = tournament_request_service.appoint_proposer_orga(
        TournamentID(generate_uuid()), proposer_id, UserID(generate_uuid())
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'The proposer could not be appointed as orga of the tournament.'
    )
    mock_orga_service.assign_orga.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_skips_suspended_proposer(
    mock_user_service, mock_orga_service
):
    proposer_id = UserID(generate_uuid())
    mock_user_service.find_user.return_value = SimpleNamespace(
        id=proposer_id, deleted=False, suspended=True
    )

    result = tournament_request_service.appoint_proposer_orga(
        TournamentID(generate_uuid()), proposer_id, UserID(generate_uuid())
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'The proposer could not be appointed as orga of the tournament.'
    )
    mock_orga_service.assign_orga.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_skips_unknown_proposer(
    mock_user_service, mock_orga_service
):
    mock_user_service.find_user.return_value = None

    result = tournament_request_service.appoint_proposer_orga(
        TournamentID(generate_uuid()),
        UserID(generate_uuid()),
        UserID(generate_uuid()),
    )

    assert result.is_err()
    mock_orga_service.assign_orga.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_survives_find_user_exception(
    mock_user_service, mock_orga_service
):
    """Regression: `find_user` used to run outside any try, so a DB
    error there raised into the caller -- after the tournament was
    already committed -- violating this function's own never-raise
    contract."""
    mock_user_service.find_user.side_effect = RuntimeError('boom')

    result = tournament_request_service.appoint_proposer_orga(
        TournamentID(generate_uuid()),
        UserID(generate_uuid()),
        UserID(generate_uuid()),
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'The proposer could not be appointed as orga of the tournament.'
    )
    mock_orga_service.assign_orga.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_survives_failed_assignment(
    mock_user_service, mock_orga_service
):
    from byceps.util.result import Err

    proposer_id = UserID(generate_uuid())
    mock_user_service.find_user.return_value = SimpleNamespace(
        id=proposer_id, deleted=False, suspended=False
    )
    mock_orga_service.assign_orga.return_value = Err(
        'User is already an orga of this tournament.'
    )

    result = tournament_request_service.appoint_proposer_orga(
        TournamentID(generate_uuid()), proposer_id, UserID(generate_uuid())
    )

    # A failed appointment is never allowed to raise into the caller --
    # the tournament it concerns already exists by the time this runs.
    assert result.is_err()
    assert result.unwrap_err() == (
        'The proposer could not be appointed as orga of the tournament.'
    )


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
@patch(f'{MOCK_PREFIX}.user_service')
def test_appoint_proposer_orga_survives_assignment_exception(
    mock_user_service, mock_orga_service
):
    proposer_id = UserID(generate_uuid())
    mock_user_service.find_user.return_value = SimpleNamespace(
        id=proposer_id, deleted=False, suspended=False
    )
    mock_orga_service.assign_orga.side_effect = RuntimeError('boom')

    result = tournament_request_service.appoint_proposer_orga(
        TournamentID(generate_uuid()), proposer_id, UserID(generate_uuid())
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'The proposer could not be appointed as orga of the tournament.'
    )


# -------------------------------------------------------------------- #
# TournamentRequest.tournament_deleted property
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    ('status', 'created_tournament_id', 'expected'),
    [
        (TournamentRequestStatus.submitted, None, False),
        (TournamentRequestStatus.accepted, None, False),
        (TournamentRequestStatus.rejected, None, False),
        (TournamentRequestStatus.withdrawn, None, False),
        (TournamentRequestStatus.tournament_created, None, True),
        (
            TournamentRequestStatus.tournament_created,
            TournamentID(generate_uuid()),
            False,
        ),
    ],
)
# fmt: on
def test_tournament_deleted_property(status, created_tournament_id, expected):
    """True only for `tournament_created` with its link cleared."""
    request = _make_request(
        status=status, created_tournament_id=created_tournament_id
    )

    assert request.tournament_deleted is expected


# -------------------------------------------------------------------- #
# terminal states
# -------------------------------------------------------------------- #


@pytest.mark.parametrize(
    'status',
    [
        TournamentRequestStatus.withdrawn,
        TournamentRequestStatus.rejected,
        TournamentRequestStatus.tournament_created,
    ],
)
@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_terminal_request_rejects_further_transitions(
    mock_repo, mock_log_service, mock_signals, mock_db, status
):
    existing = _make_request(status=status)
    mock_repo.get_request_for_update.return_value = existing

    accept_result = tournament_request_service.accept_request(
        existing.id, UserID(generate_uuid())
    )
    assert accept_result.is_err()

    reject_result = tournament_request_service.reject_request(
        existing.id, UserID(generate_uuid()), 'Some reason'
    )
    assert reject_result.is_err()

    withdraw_result = tournament_request_service.withdraw_request(
        existing.id, existing.proposer_id
    )
    assert withdraw_result.is_err()

    mock_repo.update_request_flush.assert_not_called()


# -------------------------------------------------------------------- #
# get_request_history
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_log_service')
def test_request_history_returns_submit_and_edit_entries(
    mock_log_service,
):
    request_id = TournamentRequestID(generate_uuid())
    submitted_entry = TournamentLogEntry(
        id=TournamentLogEntryID(generate_uuid()),
        occurred_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        event_type='tournament-request-submitted',
        tournament_id=TournamentID(request_id),
        initiator_id=None,
        data={'number': 1},
    )
    edited_entry = TournamentLogEntry(
        id=TournamentLogEntryID(generate_uuid()),
        occurred_at=datetime(2026, 9, 19, 13, 0, tzinfo=UTC),
        event_type='tournament-request-edited',
        tournament_id=TournamentID(request_id),
        initiator_id=None,
        data={'changed_fields': ['name'], 'by': 'proposer'},
    )
    mock_log_service.get_entries_for_tournament.return_value = [
        submitted_entry,
        edited_entry,
    ]

    result = tournament_request_service.get_request_history(request_id)

    assert result == [submitted_entry, edited_entry]
    mock_log_service.get_entries_for_tournament.assert_called_once_with(
        TournamentID(request_id)
    )


# -------------------------------------------------------------------- #
# number allocation retry
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_retries_once_on_number_collision(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.count_open_requests_for_proposer.return_value = 0
    mock_repo.get_next_number_for_party.side_effect = [3, 4]

    orig = Mock()
    orig.diag = Mock(constraint_name='uq_lan_tournament_requests_party_number')
    mock_repo.create_request.side_effect = [
        IntegrityError('', {}, orig),
        None,
    ]

    result = tournament_request_service.submit_request(
        PARTY_ID, UserID(generate_uuid()), **_submit_kwargs()
    )

    assert result.is_ok()
    request, _event = result.unwrap()
    assert request.number == 4
    assert mock_repo.get_next_number_for_party.call_count == 2
    assert mock_db.session.rollback.call_count == 1


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_submit_request_gives_up_after_second_collision(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    mock_repo.count_open_requests_for_proposer.return_value = 0
    mock_repo.get_next_number_for_party.side_effect = [3, 4]

    orig = Mock()
    orig.diag = Mock(constraint_name='uq_lan_tournament_requests_party_number')
    mock_repo.create_request.side_effect = [
        IntegrityError('', {}, orig),
        IntegrityError('', {}, orig),
    ]

    result = tournament_request_service.submit_request(
        PARTY_ID, UserID(generate_uuid()), **_submit_kwargs()
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Could not allocate a request number.'
    assert mock_repo.get_next_number_for_party.call_count == 2
    mock_signals.tournament_request_submitted.send.assert_not_called()


# -------------------------------------------------------------------- #
# reject_request -- rejection reason must not carry a NUL/other Cc char
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_rejects_nul_byte_in_reason(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    """A NUL byte in the reason must not reach the flush: PostgreSQL
    text columns cannot store it and raise an unhandled `DataError`
    (`psycopg`'s "PostgreSQL text fields cannot contain NUL (0x00)
    bytes"). Caught here, before `get_request_for_update` is even
    called."""
    result = tournament_request_service.reject_request(
        TournamentRequestID(generate_uuid()),
        UserID(generate_uuid()),
        'Not enough interest\x00; DROP TABLE users;',
    )

    assert result.is_err()
    assert (
        result.unwrap_err() == 'The reason must not contain control characters.'
    )
    mock_repo.get_request_for_update.assert_not_called()
    mock_signals.tournament_request_rejected.send.assert_not_called()


@patch(f'{MOCK_PREFIX}.db')
@patch(f'{MOCK_PREFIX}.signals')
@patch(f'{MOCK_PREFIX}.tournament_log_service')
@patch(f'{MOCK_PREFIX}.tournament_request_repository')
def test_reject_request_allows_newlines_and_tabs_in_reason(
    mock_repo, mock_log_service, mock_signals, mock_db
):
    existing = _make_request(status=TournamentRequestStatus.submitted)
    mock_repo.get_request_for_update.return_value = existing
    reason = 'Line one\r\nLine two\twith a tab'

    result = tournament_request_service.reject_request(
        existing.id, UserID(generate_uuid()), reason
    )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.rejection_reason == reason
