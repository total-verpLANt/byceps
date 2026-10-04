"""Completed reopening requires a trusted opt-in and locked current state."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Ok

from tests.helpers import generate_uuid
from tests.unit.services.lan_tournament.test_tournament_service_bracket_check import (
    _create_tournament,
)


REOPEN_ERROR = (
    'A completed tournament can only be reopened by an administrator.'
)
TRANSITION_ERROR = (
    'Cannot transition the tournament to the requested '
    'status from its current status.'
)


def _completed_tournament(winner_kind):
    winner_fields = {
        'participant': {
            'winner_participant_id': TournamentParticipantID(generate_uuid()),
        },
        'team': {'winner_team_id': TournamentTeamID(generate_uuid())},
    }
    return _create_tournament(
        tournament_status=TournamentStatus.COMPLETED,
        **winner_fields[winner_kind],
    )


@pytest.fixture
def collaborators():
    with (
        patch.object(tournament_service, 'tournament_repository') as repo,
        patch.object(tournament_service, 'create_log_entry') as audit,
        patch.object(tournament_service, 'signals') as signals,
        patch.object(tournament_service, 'tournament_match_service') as matches,
        patch.object(
            tournament_service.tournament_domain_service,
            'change_tournament_status',
            wraps=tournament_service.tournament_domain_service.change_tournament_status,
        ) as domain,
    ):
        repo.set_tournament_winner.return_value = Ok(None)
        repo.set_tournament_status_flush.return_value = Ok(None)
        matches.is_plain_round_robin.return_value = False
        calls = Mock()
        for mock, name in (
            (repo.lock_tournament_for_update, 'lock'),
            (repo.get_tournament, 'read'),
            (domain, 'domain'),
            (repo.set_tournament_winner, 'winner'),
            (audit, 'audit'),
            (repo.set_tournament_status_flush, 'status'),
            (repo.rollback_session, 'rollback'),
            (repo.commit_session, 'commit'),
            (signals.tournament_status_changed.send, 'signal'),
        ):
            calls.attach_mock(mock, name)
        yield SimpleNamespace(
            repo=repo,
            audit=audit,
            signals=signals,
            matches=matches,
            domain=domain,
            calls=calls,
        )


def _assert_no_mutations(collaborators):
    collaborators.repo.set_tournament_winner.assert_not_called()
    collaborators.repo.set_tournament_status_flush.assert_not_called()
    collaborators.repo.update_tournament.assert_not_called()
    collaborators.repo.commit_session.assert_not_called()
    collaborators.audit.assert_not_called()
    assert collaborators.signals.mock_calls == []
    assert collaborators.matches.mock_calls == []


@pytest.mark.parametrize(
    'initial_status',
    # fmt: off
    [None, TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.PAUSED],
    # fmt: on
)
def test_reopen_denied_by_default_after_locked_fresh_read(
    collaborators, initial_status
):
    completed = _completed_tournament('participant')
    stale = _create_tournament(
        id=completed.id, tournament_status=initial_status
    )

    def read(tournament_id, *, fresh=False):
        assert tournament_id == completed.id
        if fresh:
            collaborators.repo.lock_tournament_for_update.assert_called_once_with(
                completed.id
            )
            return completed
        return stale

    collaborators.repo.get_tournament.side_effect = read
    assert collaborators.repo.get_tournament(completed.id) == stale
    collaborators.calls.reset_mock()

    result = tournament_service.change_status(
        completed.id, TournamentStatus.ONGOING, UserID(generate_uuid())
    )

    assert result.is_err()
    assert result.unwrap_err() == REOPEN_ERROR
    assert collaborators.calls.mock_calls == [
        call.lock(completed.id),
        call.read(completed.id, fresh=True),
        call.rollback(),
    ]
    collaborators.domain.assert_not_called()
    _assert_no_mutations(collaborators)


@pytest.mark.parametrize('winner_kind', ['participant', 'team'])
@pytest.mark.parametrize(
    'new_status',
    [
        status
        for status in TournamentStatus
        if status != TournamentStatus.COMPLETED
    ],
)
def test_reopen_refusal_preserves_winner_and_rolls_back(
    collaborators, winner_kind, new_status
):
    tournament = _completed_tournament(winner_kind)
    original_winners = (
        tournament.winner_participant_id,
        tournament.winner_team_id,
    )
    collaborators.repo.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id,
        new_status,
        UserID(generate_uuid()),
        confirm_generated_layout=True,
    )

    assert result.is_err()
    assert result.unwrap_err() == REOPEN_ERROR
    assert tournament.tournament_status == TournamentStatus.COMPLETED
    assert (tournament.winner_participant_id, tournament.winner_team_id) == (
        original_winners
    )
    collaborators.repo.rollback_session.assert_called_once_with()
    collaborators.domain.assert_not_called()
    _assert_no_mutations(collaborators)


@pytest.mark.parametrize('winner_kind', ['participant', 'team'])
def test_reopen_explicit_admin_opt_in_clears_winner_once(
    collaborators, winner_kind
):
    tournament = _completed_tournament(winner_kind)
    initiator_id = UserID(generate_uuid())
    collaborators.repo.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id,
        TournamentStatus.ONGOING,
        initiator_id,
        allow_completed_reopen=True,
    )

    assert result.is_ok()
    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert updated.winner_participant_id is None
    assert updated.winner_team_id is None
    assert event.tournament_id == tournament.id
    assert event.old_status == TournamentStatus.COMPLETED
    assert event.new_status == TournamentStatus.ONGOING
    assert collaborators.calls.mock_calls == [
        call.lock(tournament.id),
        call.read(tournament.id, fresh=True),
        call.domain(tournament, TournamentStatus.ONGOING),
        call.winner(
            tournament.id, winner_team_id=None, winner_participant_id=None
        ),
        call.audit(
            'tournament-status-changed',
            tournament.id,
            initiator_id,
            data={'old_status': 'COMPLETED', 'new_status': 'ONGOING'},
            commit=False,
        ),
        call.status(tournament.id, TournamentStatus.ONGOING),
        call.commit(),
        call.signal(None, event=event),
    ]
    collaborators.repo.rollback_session.assert_not_called()
    collaborators.repo.update_tournament.assert_not_called()
    collaborators.matches.validate_bracket_for_start.assert_not_called()


@pytest.mark.parametrize(
    ('old_status', 'new_status'),
    # fmt: off
    [
        (TournamentStatus.COMPLETED, TournamentStatus.DRAFT),
        (TournamentStatus.COMPLETED, TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.COMPLETED, TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.COMPLETED, TournamentStatus.PAUSED),
        (TournamentStatus.COMPLETED, TournamentStatus.COMPLETED),
        (TournamentStatus.COMPLETED, TournamentStatus.CANCELLED),
        (TournamentStatus.CANCELLED, TournamentStatus.ONGOING),
        (TournamentStatus.DRAFT, TournamentStatus.ONGOING),
        (TournamentStatus.REGISTRATION_OPEN, TournamentStatus.ONGOING),
    ],
    # fmt: on
)
def test_reopen_opt_in_does_not_widen_domain_transitions(
    collaborators, old_status, new_status
):
    tournament = _create_tournament(tournament_status=old_status)
    collaborators.repo.get_tournament.return_value = tournament

    result = tournament_service.change_status(
        tournament.id, new_status, allow_completed_reopen=True
    )

    assert result.is_err()
    assert result.unwrap_err() == TRANSITION_ERROR
    assert collaborators.calls.mock_calls == [
        call.lock(tournament.id),
        call.read(tournament.id, fresh=True),
        call.domain(tournament, new_status),
        call.rollback(),
    ]
    _assert_no_mutations(collaborators)


@pytest.mark.parametrize(
    'operation',
    [tournament_service.start_tournament, tournament_service.resume_tournament],
)
def test_start_and_resume_wrappers_keep_completed_reopen_default_denial(
    collaborators, operation
):
    tournament = _completed_tournament('participant')
    collaborators.repo.get_tournament.return_value = tournament

    result = operation(tournament.id, UserID(generate_uuid()))

    assert result.is_err()
    assert result.unwrap_err() == REOPEN_ERROR
    collaborators.repo.get_tournament.assert_called_once_with(
        tournament.id, fresh=True
    )
    collaborators.repo.rollback_session.assert_called_once_with()
    collaborators.domain.assert_not_called()
    _assert_no_mutations(collaborators)


def test_ordinary_resume_does_not_require_reopen_capability(collaborators):
    tournament = _create_tournament(tournament_status=TournamentStatus.PAUSED)
    collaborators.repo.get_tournament.return_value = tournament

    result = tournament_service.resume_tournament(tournament.id)

    assert result.is_ok()
    assert result.unwrap()[0].tournament_status == TournamentStatus.ONGOING
    collaborators.repo.set_tournament_winner.assert_not_called()
    collaborators.repo.commit_session.assert_called_once_with()
    collaborators.repo.rollback_session.assert_not_called()
    collaborators.matches.validate_bracket_for_start.assert_not_called()


class _CachedTournamentSession:
    """Model a cached ORM row and a newer committed row without a database."""

    def __init__(self, tournament):
        self.committed = DbTournament(
            tournament.id,
            tournament.party_id,
            tournament.name,
            tournament.created_at,
            tournament_status=tournament.tournament_status.name,
        )
        self.committed.position = 0
        self.committed.use_bracket_reset = True
        self.cached = None
        self.autoflush = True
        self.no_autoflush_entries = 0
        self.get_calls = []
        self.autoflush_on_get = []

    @property
    @contextmanager
    def no_autoflush(self):
        previous = self.autoflush
        self.autoflush = False
        self.no_autoflush_entries += 1
        try:
            yield
        finally:
            self.autoflush = previous

    def get(self, model, tournament_id, **kwargs):
        assert model is DbTournament
        self.get_calls.append(call(model, tournament_id, **kwargs))
        self.autoflush_on_get.append(self.autoflush)
        if tournament_id != self.committed.id:
            return None
        first_load = self.cached is None
        if first_load:
            self.cached = DbTournament(
                self.committed.id,
                self.committed.party_id,
                self.committed.name,
                self.committed.created_at,
            )
        if first_load or kwargs.get('populate_existing'):
            for column in DbTournament.__table__.columns:
                setattr(
                    self.cached, column.key, getattr(self.committed, column.key)
                )
        return self.cached


def test_fresh_tournament_read_overrides_identity_map():
    tournament = _create_tournament(tournament_status=TournamentStatus.PAUSED)
    winner_id = TournamentParticipantID(generate_uuid())
    session = _CachedTournamentSession(tournament)
    with patch.object(
        tournament_repository, 'db', SimpleNamespace(session=session)
    ):
        initial = tournament_repository.get_tournament(tournament.id)
        # Keep a strong reference to the loaded ORM row.
        cached = session.cached
        assert isinstance(cached, DbTournament)
        session.committed.tournament_status = TournamentStatus.COMPLETED.name
        session.committed.winner_participant_id = winner_id

        ordinary = tournament_repository.get_tournament(tournament.id)
        assert ordinary == initial
        assert ordinary.tournament_status == TournamentStatus.PAUSED
        assert ordinary.winner_participant_id is None
        assert session.no_autoflush_entries == 0

        fresh = tournament_repository.get_tournament(tournament.id, fresh=True)

    assert session.cached is cached
    assert fresh.tournament_status == TournamentStatus.COMPLETED
    assert fresh.winner_participant_id == winner_id
    assert cached.tournament_status == TournamentStatus.COMPLETED.name
    assert cached.winner_participant_id == winner_id
    assert session.get_calls == [
        call(DbTournament, tournament.id),
        call(DbTournament, tournament.id),
        call(DbTournament, tournament.id, populate_existing=True),
    ]
    assert session.autoflush_on_get == [True, True, False]
    assert session.no_autoflush_entries == 1
    assert session.autoflush is True


@pytest.mark.parametrize('fresh', [False, True])
def test_unknown_tournament_getter_preserves_error_and_autoflush(fresh):
    tournament = _create_tournament(tournament_status=TournamentStatus.PAUSED)
    unknown_id = TournamentID(generate_uuid())
    session = _CachedTournamentSession(tournament)
    with patch.object(
        tournament_repository, 'db', SimpleNamespace(session=session)
    ):
        with pytest.raises(ValueError) as error:
            tournament_repository.get_tournament(unknown_id, fresh=fresh)

    assert str(error.value) == f'Unknown tournament ID "{unknown_id}"'
    assert session.no_autoflush_entries == int(fresh)
    assert session.autoflush_on_get == [not fresh]
    assert session.autoflush is True
