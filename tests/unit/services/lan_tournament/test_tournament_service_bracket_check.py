"""
tests.unit.services.lan_tournament.test_tournament_service_bracket_check
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Verify that ``change_status()`` enforces (or skips) the bracket guard
depending on the tournament's ``GameFormat.requires_bracket_generation`` flag.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament import (
    seed_code,
    tournament_seeding_domain_service as seeding_domain,
)
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)

SEEDING_SERVICE = 'byceps.services.lan_tournament.tournament_seeding_service'


@pytest.fixture(autouse=True)
def no_seeding_draft():
    """Give every tournament the legacy shape: no seeding draft."""
    with patch(f'{SEEDING_SERVICE}.tournament_seeding_repository') as repo:
        repo.find_seeding.return_value = None
        yield repo


def _create_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('test-party'),
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _build_valid_se_bracket(tournament_id):
    """Return a single-match SE bracket that passes start validation."""
    match = TournamentMatch(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=tournament_id,
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    contestants = [
        TournamentMatchToContestant(
            id=TournamentMatchToContestantID(generate_uuid()),
            tournament_match_id=match.id,
            team_id=None,
            participant_id=TournamentParticipantID(generate_uuid()),
            score=None,
            created_at=NOW,
        )
        for _ in range(2)
    ]
    return [match], {match.id: contestants}


# -------------------------------------------------------------------- #
# bracketless mode (HIGHSCORE) bypasses the bracket guard
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_start_bracketless_mode_without_matches_succeeds(
    mock_create_log_entry, mock_signals, mock_repository
):
    """A HIGHSCORE tournament can transition to ONGOING even without
    any generated matches, because its mode does not require a bracket."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
    )

    mock_repository.get_tournament.return_value = tournament
    # workspace-pv3b.24: the status is now written through this
    # targeted, `Result`-returning setter, not the full-row writer.
    mock_repository.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()

    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert event.old_status == TournamentStatus.REGISTRATION_CLOSED
    assert event.new_status == TournamentStatus.ONGOING

    # Bracket helper must NOT have been consulted
    mock_repository.get_matches_for_tournament.assert_not_called()


# -------------------------------------------------------------------- #
# bracket mode (SE) without matches -> blocked
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_bracket_mode_without_matches_fails(
    mock_signals, mock_repository, mock_match_repo
):
    """An SE tournament must not start with an invalid bracket."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_match_repo.get_matches_for_tournament_ordered.return_value = []
    mock_match_repo.get_contestants_for_tournament.return_value = {}

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    # An ungenerated bracket reports the catalogued sentence.
    assert result.unwrap_err() == (
        'Cannot start tournament without generated brackets. '
        'Generate brackets first.'
    )


# -------------------------------------------------------------------- #
# bracket mode (SE) with valid bracket -> allowed
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_start_bracket_mode_with_matches_succeeds(
    mock_create_log_entry, mock_signals, mock_repository, mock_match_repo
):
    """An SE tournament with a structurally valid bracket can start."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    matches, contestants_by_match = _build_valid_se_bracket(tournament.id)

    mock_repository.get_tournament.return_value = tournament
    mock_repository.set_tournament_status_flush.return_value = Ok(None)
    mock_match_repo.get_matches_for_tournament_ordered.return_value = matches
    mock_match_repo.get_contestants_for_tournament.return_value = (
        contestants_by_match
    )

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()

    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert event.new_status == TournamentStatus.ONGOING


# -------------------------------------------------------------------- #
# invalid bracket blocks start without any override
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_blocked_on_invalid_bracket_without_override(
    mock_signals, mock_repository, mock_validate
):
    """A corrupted bracket blocks the start; the status stays unchanged."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_validate.return_value = [
        'match abc is missing next_match_id',
    ]
    update_mock = mock_repository.update_tournament

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    update_mock.assert_not_called()
    mock_signals.tournament_status_changed.send.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_error_lists_violations(
    mock_signals, mock_repository, mock_validate
):
    """The Err message contains the individual violation strings."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    violations = [
        'match abc is missing next_match_id',
        'no grand-final match',
    ]

    mock_repository.get_tournament.return_value = tournament
    mock_validate.return_value = violations

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    message = result.unwrap_err()
    for violation in violations:
        assert violation in message


# -------------------------------------------------------------------- #
# resume (PAUSED -> ONGOING) must NOT re-run start validation
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_match_service'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_resume_from_paused_skips_bracket_validation(
    mock_create_log_entry, mock_signals, mock_repository, mock_match_service
):
    """A resume is not a start, so the pre-start gate must not run."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.PAUSED,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_repository.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()
    updated, event = result.unwrap()
    assert updated.tournament_status == TournamentStatus.ONGOING
    assert event.old_status == TournamentStatus.PAUSED

    mock_match_service.validate_bracket_for_start.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
def test_resume_from_paused_succeeds_with_wired_grand_final(
    mock_create_log_entry, mock_signals, mock_repository, mock_match_repo
):
    """A paused tournament resumes after a DE bracket reset."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        tournament_status=TournamentStatus.PAUSED,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_repository.set_tournament_status_flush.return_value = Ok(None)
    # An empty bracket would fail start validation, were it run.
    mock_match_repo.get_matches_for_tournament_ordered.return_value = []
    mock_match_repo.get_contestants_for_tournament.return_value = {}

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_ok()
    assert result.unwrap()[0].tournament_status == TournamentStatus.ONGOING
    mock_match_repo.get_matches_for_tournament_ordered.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_from_registration_closed_still_validates(
    mock_signals, mock_repository, mock_match_repo
):
    """Gating the resume must not weaken the real start path."""
    from byceps.services.lan_tournament import tournament_service

    tournament = _create_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )

    mock_repository.get_tournament.return_value = tournament
    mock_match_repo.get_matches_for_tournament_ordered.return_value = []
    mock_match_repo.get_contestants_for_tournament.return_value = {}

    result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Cannot start tournament without generated brackets. '
        'Generate brackets first.'
    )
    mock_match_repo.get_matches_for_tournament_ordered.assert_called_once()


# -------------------------------------------------------------------- #
# seeding: the roster must equal the one the bracket was generated from
# -------------------------------------------------------------------- #


def _generated_code(roster_ids) -> str:
    state = seeding_domain.initial_state(
        SeedingFormat.SINGLE_ELIMINATION,
        0,
        roster_ids,
        tier_count=1,
        draw_seed=7,
    )
    return seed_code.encode_seed_code(state, state.layout)


def test_changed_board_refusal_runs_under_lock_without_status_or_audit_writes():
    from byceps.services.lan_tournament import (
        tournament_seeding_service,
        tournament_service,
    )

    tournament = _create_tournament()
    order = []
    with (
        patch.object(tournament_service, 'tournament_repository') as repo,
        patch.object(tournament_service, 'create_log_entry') as audit,
        patch.object(tournament_service, 'signals') as signals,
        patch.object(
            tournament_service.tournament_match_service,
            'validate_bracket_for_start',
            side_effect=lambda *a, **kw: order.append('bracket') or [],
        ),
        patch.object(
            tournament_seeding_service,
            'start_violations',
            side_effect=lambda *a: order.append('roster') or [],
        ),
        patch.object(
            tournament_seeding_service,
            'peek_initial_generation_status',
            side_effect=lambda *a: order.append('probe')
            or tournament_seeding_service.GenerationStatus.DIFFERS,
        ) as probe,
    ):
        repo.lock_tournament_for_update.side_effect = lambda *a: order.append(
            'lock'
        )
        repo.get_tournament.return_value = tournament

        repo.set_tournament_status_flush.return_value = Ok(None)

        result = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING
        )

        assert result.is_err()
        assert result.unwrap_err() == (
            'Confirm that the generated layout is used before starting.'
        )
        assert order == ['lock', 'bracket', 'roster', 'probe']
        probe.assert_called_once_with(tournament.id)
        repo.rollback_session.assert_called_once()
        repo.set_tournament_status_flush.assert_not_called()
        repo.commit_session.assert_not_called()
        audit.assert_not_called()
        signals.tournament_status_changed.send.assert_not_called()


@pytest.mark.parametrize(
    ('status', 'format_', 'generation', 'confirmed', 'probed', 'allow_reopen'),
    # fmt: off
    [
        (TournamentStatus.REGISTRATION_CLOSED, GameFormat.ONE_V_ONE, None, False, True, False),
        (TournamentStatus.REGISTRATION_CLOSED, GameFormat.ONE_V_ONE, 'NOT_GENERATED', False, True, False),
        (TournamentStatus.REGISTRATION_CLOSED, GameFormat.ONE_V_ONE, 'MATCHES', False, True, False),
        (TournamentStatus.REGISTRATION_CLOSED, GameFormat.ONE_V_ONE, 'DIFFERS', True, False, False),
        (TournamentStatus.PAUSED, GameFormat.ONE_V_ONE, 'DIFFERS', False, False, False),
        (TournamentStatus.COMPLETED, GameFormat.ONE_V_ONE, 'DIFFERS', False, False, True),
        (TournamentStatus.REGISTRATION_CLOSED, GameFormat.HIGHSCORE, 'DIFFERS', False, False, False),
    ],
    # fmt: on
)
def test_confirmation_only_gates_unconfirmed_changed_board_starts(
    status, format_, generation, confirmed, probed, allow_reopen
):
    from byceps.services.lan_tournament import (
        tournament_seeding_service,
        tournament_service,
    )

    tournament = _create_tournament(
        tournament_status=status, game_format=format_
    )
    with (
        patch.object(tournament_service, 'tournament_repository') as repo,
        patch.object(tournament_service, 'create_log_entry'),
        patch.object(tournament_service, 'signals'),
        patch.object(
            tournament_service.tournament_match_service,
            'validate_bracket_for_start',
            return_value=[],
        ),
        patch.object(
            tournament_seeding_service, 'start_violations', return_value=[]
        ) as roster,
        patch.object(
            tournament_seeding_service,
            'peek_initial_generation_status',
            return_value=(
                tournament_seeding_service.GenerationStatus[generation]
                if generation
                else None
            ),
        ) as probe,
    ):
        repo.get_tournament.return_value = tournament
        repo.set_tournament_status_flush.return_value = Ok(None)
        repo.set_tournament_winner.return_value = Ok(None)

        result = tournament_service.change_status(
            tournament.id,
            TournamentStatus.ONGOING,
            confirm_generated_layout=confirmed,
            allow_completed_reopen=allow_reopen,
        )

        assert result.is_ok()
        assert probe.called is probed
        if status is TournamentStatus.REGISTRATION_CLOSED:
            roster.assert_called_once_with(tournament.id)
        else:
            roster.assert_not_called()
        repo.commit_session.assert_called_once()


def test_explicit_confirmation_cannot_override_roster_refusal():
    from byceps.services.lan_tournament import (
        tournament_seeding_service,
        tournament_service,
    )

    tournament = _create_tournament()
    with (
        patch.object(tournament_service, 'tournament_repository') as repo,
        patch.object(tournament_service, 'create_log_entry') as audit,
        patch.object(
            tournament_service.tournament_match_service,
            'validate_bracket_for_start',
            return_value=[],
        ),
        patch.object(
            tournament_seeding_service,
            'start_violations',
            return_value=[tournament_seeding_service.ERR_ROSTER_CHANGED],
        ),
        patch.object(
            tournament_seeding_service, 'peek_initial_generation_status'
        ) as probe,
    ):
        repo.get_tournament.return_value = tournament

        result = tournament_service.change_status(
            tournament.id,
            TournamentStatus.ONGOING,
            confirm_generated_layout=True,
        )

        assert (
            result.unwrap_err() == tournament_seeding_service.ERR_ROSTER_CHANGED
        )
        probe.assert_not_called()
        repo.rollback_session.assert_called_once()
        repo.set_tournament_status_flush.assert_not_called()
        audit.assert_not_called()


def _roster(ids):
    return SimpleNamespace(ids=tuple(ids))


def _start(tournament):
    from byceps.services.lan_tournament import tournament_service

    return tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )


@pytest.mark.parametrize(
    ('current_ids',),
    # fmt: off
    [
        (['p1', 'p2', 'p3'],),  # removal
        (['p1', 'p2', 'p3', 'p4', 'p5'],),  # join after generation
        (['p1', 'p2', 'p3', 'p9'],),  # swap, same size
    ],
    # fmt: on
)
@patch(f'{SEEDING_SERVICE}._roster')
@patch(f'{SEEDING_SERVICE}.tournament_repository')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start',
    return_value=[],
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_blocked_when_roster_changed_after_generation(
    mock_signals,
    mock_repository,
    mock_validate,
    mock_seeding_tournament_repo,
    mock_roster,
    no_seeding_draft,
    current_ids,
):
    tournament = _create_tournament()
    mock_repository.get_tournament.return_value = tournament
    mock_seeding_tournament_repo.get_tournament.return_value = tournament
    no_seeding_draft.find_seeding.return_value = SimpleNamespace(
        generated_seed_code=_generated_code(['p1', 'p2', 'p3', 'p4'])
    )
    mock_roster.return_value = _roster(current_ids)

    result = _start(tournament)

    assert result.is_err()
    assert result.unwrap_err() == (
        'The roster changed after generation. Re-seed and regenerate first.'
    )
    mock_repository.rollback_session.assert_called_once()
    mock_signals.tournament_status_changed.send.assert_not_called()


@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
@patch(f'{SEEDING_SERVICE}._roster')
@patch(f'{SEEDING_SERVICE}.tournament_repository')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start',
    return_value=[],
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_allowed_when_roster_matches_generation(
    mock_signals,
    mock_repository,
    mock_validate,
    mock_seeding_tournament_repo,
    mock_roster,
    mock_create_log_entry,
    no_seeding_draft,
):
    tournament = _create_tournament()
    mock_repository.get_tournament.return_value = tournament
    mock_repository.set_tournament_status_flush.return_value = Ok(None)
    mock_seeding_tournament_repo.get_tournament.return_value = tournament
    no_seeding_draft.find_seeding.return_value = SimpleNamespace(
        target='initial',
        seed_code=_generated_code(['p1', 'p2', 'p3', 'p4']),
        generated_seed_code=_generated_code(['p1', 'p2', 'p3', 'p4'])
    )
    mock_roster.return_value = _roster(['p4', 'p3', 'p2', 'p1'])

    assert _start(tournament).is_ok()


@patch(f'{SEEDING_SERVICE}._roster')
@patch(f'{SEEDING_SERVICE}.tournament_repository')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start',
    return_value=[],
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_blocked_when_structure_changed_after_generation(
    mock_signals,
    mock_repository,
    mock_validate,
    mock_seeding_tournament_repo,
    mock_roster,
    no_seeding_draft,
):
    ids = ['p1', 'p2', 'p3', 'p4']
    tournament = _create_tournament(
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    mock_repository.get_tournament.return_value = tournament
    mock_seeding_tournament_repo.get_tournament.return_value = tournament
    no_seeding_draft.find_seeding.return_value = SimpleNamespace(
        generated_seed_code=_generated_code(ids)
    )
    mock_roster.return_value = _roster(ids)

    result = _start(tournament)

    assert result.unwrap_err() == (
        'The tournament structure changed after generation. '
        'Regenerate on the seeding board first.'
    )
    mock_repository.rollback_session.assert_called_once()
    mock_signals.tournament_status_changed.send.assert_not_called()


@patch('byceps.services.lan_tournament.tournament_service.create_log_entry')
@patch(f'{SEEDING_SERVICE}.tournament_repository')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start',
    return_value=[],
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_allowed_for_legacy_tournament_without_seeding(
    mock_signals,
    mock_repository,
    mock_validate,
    mock_seeding_tournament_repo,
    mock_create_log_entry,
    no_seeding_draft,
):
    tournament = _create_tournament()
    mock_repository.get_tournament.return_value = tournament
    mock_repository.set_tournament_status_flush.return_value = Ok(None)
    mock_seeding_tournament_repo.get_tournament.return_value = tournament

    # No draft at all, then a draft that was never generated from.
    assert _start(tournament).is_ok()

    no_seeding_draft.find_seeding.return_value = SimpleNamespace(
        target='initial', seed_code='x', generated_seed_code=None
    )
    assert _start(tournament).is_ok()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start',
    return_value=['no grand-final match'],
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_reports_bracket_violation_without_a_seeding_reason(
    mock_signals, mock_repository, mock_validate, no_seeding_draft
):
    tournament = _create_tournament()
    mock_repository.get_tournament.return_value = tournament

    message = _start(tournament).unwrap_err()

    assert message == 'Cannot start tournament: no grand-final match'


@patch(f'{SEEDING_SERVICE}._roster')
@patch(f'{SEEDING_SERVICE}.tournament_repository')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.validate_bracket_for_start',
    return_value=['no grand-final match'],
)
@patch(
    'byceps.services.lan_tournament.tournament_service.tournament_repository'
)
@patch('byceps.services.lan_tournament.tournament_service.signals')
def test_start_names_the_structure_change_over_a_bracket_violation(
    mock_signals,
    mock_repository,
    mock_validate,
    mock_seeding_tournament_repo,
    mock_roster,
    no_seeding_draft,
):
    ids = ['p1', 'p2', 'p3', 'p4']
    tournament = _create_tournament(
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    mock_repository.get_tournament.return_value = tournament
    mock_seeding_tournament_repo.get_tournament.return_value = tournament
    no_seeding_draft.find_seeding.return_value = SimpleNamespace(
        generated_seed_code=_generated_code(ids)
    )
    mock_roster.return_value = _roster(ids)

    message = _start(tournament).unwrap_err()

    assert message == (
        'The tournament structure changed after generation. '
        'Regenerate on the seeding board first.'
    )
