"""
tests.unit.services.lan_tournament.test_tournament_status_transitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime

import pytest

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    change_tournament_status,
    validate_status_transition,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


# -------------------------------------------------------------------- #
# valid transitions


# fmt: off
@pytest.mark.parametrize(
    ('current', 'new'),
    [
        # DRAFT transitions
        (TournamentStatus.DRAFT,               TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.DRAFT,               TournamentStatus.CANCELLED),

        # REGISTRATION_OPEN transitions
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.CANCELLED),

        # REGISTRATION_CLOSED transitions
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.ONGOING),
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.CANCELLED),

        # ONGOING transitions
        (TournamentStatus.ONGOING,             TournamentStatus.PAUSED),
        (TournamentStatus.ONGOING,             TournamentStatus.COMPLETED),
        (TournamentStatus.ONGOING,             TournamentStatus.CANCELLED),

        # PAUSED transitions
        (TournamentStatus.PAUSED,              TournamentStatus.ONGOING),
        (TournamentStatus.PAUSED,              TournamentStatus.CANCELLED),
    ],
)
# fmt: on
def test_valid_status_transition(current, new):
    result = validate_status_transition(current, new)

    assert result.is_ok()
    assert result.unwrap() == new


# -------------------------------------------------------------------- #
# invalid transitions


# fmt: off
@pytest.mark.parametrize(
    ('current', 'new'),
    [
        # DRAFT cannot go to these
        (TournamentStatus.DRAFT,               TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.DRAFT,               TournamentStatus.ONGOING),
        (TournamentStatus.DRAFT,               TournamentStatus.PAUSED),
        (TournamentStatus.DRAFT,               TournamentStatus.COMPLETED),
        (TournamentStatus.DRAFT,               TournamentStatus.DRAFT),

        # REGISTRATION_OPEN cannot go to these
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.DRAFT),
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.ONGOING),
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.PAUSED),
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.COMPLETED),
        (TournamentStatus.REGISTRATION_OPEN,   TournamentStatus.REGISTRATION_OPEN),

        # REGISTRATION_CLOSED cannot go to these
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.DRAFT),
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.PAUSED),
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.COMPLETED),
        (TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.REGISTRATION_CLOSED),

        # ONGOING cannot go to these
        (TournamentStatus.ONGOING,             TournamentStatus.DRAFT),
        (TournamentStatus.ONGOING,             TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.ONGOING,             TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.ONGOING,             TournamentStatus.ONGOING),

        # PAUSED cannot go to these
        (TournamentStatus.PAUSED,              TournamentStatus.DRAFT),
        (TournamentStatus.PAUSED,              TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.PAUSED,              TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.PAUSED,              TournamentStatus.COMPLETED),
        (TournamentStatus.PAUSED,              TournamentStatus.PAUSED),

        # COMPLETED is terminal
        (TournamentStatus.COMPLETED,           TournamentStatus.DRAFT),
        (TournamentStatus.COMPLETED,           TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.COMPLETED,           TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.COMPLETED,           TournamentStatus.ONGOING),
        (TournamentStatus.COMPLETED,           TournamentStatus.PAUSED),
        (TournamentStatus.COMPLETED,           TournamentStatus.CANCELLED),
        (TournamentStatus.COMPLETED,           TournamentStatus.COMPLETED),

        # CANCELLED is terminal
        (TournamentStatus.CANCELLED,           TournamentStatus.DRAFT),
        (TournamentStatus.CANCELLED,           TournamentStatus.REGISTRATION_OPEN),
        (TournamentStatus.CANCELLED,           TournamentStatus.REGISTRATION_CLOSED),
        (TournamentStatus.CANCELLED,           TournamentStatus.ONGOING),
        (TournamentStatus.CANCELLED,           TournamentStatus.PAUSED),
        (TournamentStatus.CANCELLED,           TournamentStatus.COMPLETED),
        (TournamentStatus.CANCELLED,           TournamentStatus.CANCELLED),
    ],
)
# fmt: on
def test_invalid_status_transition(current, new):
    result = validate_status_transition(current, new)

    assert result.is_err()
    assert 'Cannot transition' in result.unwrap_err()


# -------------------------------------------------------------------- #
# transition from None (new tournament)


@pytest.mark.parametrize(
    'new_status',
    list(TournamentStatus),
)
def test_transition_from_none_always_succeeds(new_status):
    result = validate_status_transition(None, new_status)

    assert result.is_ok()
    assert result.unwrap() == new_status


# -------------------------------------------------------------------- #
# CANCELLED reachable from all non-terminal states


@pytest.mark.parametrize(
    'current',
    [
        TournamentStatus.DRAFT,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
    ],
)
def test_cancelled_reachable_from_non_terminal_states(current):
    result = validate_status_transition(current, TournamentStatus.CANCELLED)

    assert result.is_ok()


# -------------------------------------------------------------------- #
# COMPLETED not reachable from CANCELLED


def test_completed_not_reachable_from_cancelled():
    result = validate_status_transition(
        TournamentStatus.CANCELLED, TournamentStatus.COMPLETED
    )

    assert result.is_err()


# -------------------------------------------------------------------- #
# terminal states have no outgoing transitions


@pytest.mark.parametrize(
    'terminal_status',
    [
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    ],
)
def test_terminal_states_have_no_transitions(terminal_status):
    for target in TournamentStatus:
        result = validate_status_transition(terminal_status, target)
        assert result.is_err()


# -------------------------------------------------------------------- #
# PAUSED/RESUME cycle


def test_pause_resume_cycle():
    # ONGOING -> PAUSED
    result1 = validate_status_transition(
        TournamentStatus.ONGOING, TournamentStatus.PAUSED
    )
    assert result1.is_ok()

    # PAUSED -> ONGOING (resume)
    result2 = validate_status_transition(
        TournamentStatus.PAUSED, TournamentStatus.ONGOING
    )
    assert result2.is_ok()


# -------------------------------------------------------------------- #
# change_tournament_status produces events


def test_change_status_valid_produces_event():
    tournament = _create_tournament(
        tournament_status=TournamentStatus.DRAFT
    )

    result = change_tournament_status(
        tournament, TournamentStatus.REGISTRATION_OPEN
    )

    assert result.is_ok()
    (event,) = result.unwrap()
    assert event.tournament_id == tournament.id
    assert event.old_status == TournamentStatus.DRAFT
    assert event.new_status == TournamentStatus.REGISTRATION_OPEN
    assert event.occurred_at is not None


def test_change_status_invalid_returns_error():
    tournament = _create_tournament(
        tournament_status=TournamentStatus.COMPLETED
    )

    result = change_tournament_status(
        tournament, TournamentStatus.DRAFT
    )

    assert result.is_err()
    assert 'Cannot transition' in result.unwrap_err()


def test_change_status_from_none_produces_event():
    tournament = _create_tournament(tournament_status=None)

    result = change_tournament_status(
        tournament, TournamentStatus.DRAFT
    )

    assert result.is_ok()
    (event,) = result.unwrap()
    assert event.old_status is None
    assert event.new_status == TournamentStatus.DRAFT


# -------------------------------------------------------------------- #
# helpers


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
        'tournament_status': None,
        'tournament_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)
