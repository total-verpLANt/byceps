"""
tests.unit.services.lan_tournament.test_tournament_edit_hardening
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests that structural tournament fields are locked when the
tournament status is ONGOING or PAUSED, while cosmetic fields
(description, image_url, ruleset) remain editable at any status.
"""

from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.score_ordering import (
    ScoreOrdering,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament import tournament_service

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID_STR = str(generate_uuid())

_REPO = (
    'byceps.services.lan_tournament.tournament_service'
    '.tournament_repository'
)
_SIGNALS = (
    'byceps.services.lan_tournament.tournament_service.signals'
)


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


def _make_tournament(
    status: TournamentStatus = TournamentStatus.ONGOING,
) -> Tournament:
    """Return a real Tournament dataclass with the given status."""
    return Tournament(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID_STR,
        name='Test Tournament',
        game='TestGame',
        description='Original description',
        image_url='https://example.com/img.png',
        ruleset='Original rules',
        start_time=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        created_at=datetime.now(UTC),
        updated_at=None,
        min_players=2,
        max_players=16,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=ContestantType.SOLO,
        tournament_status=status,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        score_ordering=None,
    )


def _call_update(tournament: Tournament, **overrides):
    """Call update_tournament with the tournament's current values,
    optionally overriding specific fields."""
    kwargs = dict(
        name=tournament.name,
        game=tournament.game,
        description=tournament.description,
        image_url=tournament.image_url,
        ruleset=tournament.ruleset,
        start_time=tournament.start_time,
        min_players=tournament.min_players,
        max_players=tournament.max_players,
        min_teams=tournament.min_teams,
        max_teams=tournament.max_teams,
        min_players_in_team=tournament.min_players_in_team,
        max_players_in_team=tournament.max_players_in_team,
        contestant_type=tournament.contestant_type,
        game_format=tournament.game_format,
        elimination_mode=tournament.elimination_mode,
        score_ordering=tournament.score_ordering,
    )
    kwargs.update(overrides)
    return tournament_service.update_tournament(tournament.id, **kwargs)


# ------------------------------------------------------------------ #
# tests — allowed edits on ongoing tournaments
# ------------------------------------------------------------------ #


def test_update_allowed_fields_when_ongoing():
    """Cosmetic fields (description, image_url, ruleset) should be
    editable even on an ONGOING tournament."""
    tournament = _make_tournament(TournamentStatus.ONGOING)

    with (
        patch(f'{_REPO}.get_tournament', return_value=tournament),
        patch(f'{_REPO}.update_tournament'),
        patch(f'{_SIGNALS}.tournament_updated.send'),
    ):
        result = _call_update(
            tournament,
            description='Updated description',
            image_url='https://example.com/new.png',
            ruleset='Updated rules',
        )

    assert result.is_ok()
    updated = result.unwrap()
    assert updated.description == 'Updated description'
    assert updated.image_url == 'https://example.com/new.png'
    assert updated.ruleset == 'Updated rules'


# ------------------------------------------------------------------ #
# tests — locked field rejection
# ------------------------------------------------------------------ #


def test_update_locked_field_name_when_ongoing():
    """Changing name on an ONGOING tournament must be rejected."""
    tournament = _make_tournament(TournamentStatus.ONGOING)

    with patch(f'{_REPO}.get_tournament', return_value=tournament):
        result = _call_update(tournament, name='New Name')

    assert result.is_err()
    assert 'name' in result.unwrap_err()
    assert 'ongoing' in result.unwrap_err().lower()


def test_update_locked_field_mode_when_ongoing():
    """Changing elimination_mode on an ONGOING tournament must be rejected."""
    tournament = _make_tournament(TournamentStatus.ONGOING)

    with patch(f'{_REPO}.get_tournament', return_value=tournament):
        result = _call_update(
            tournament, elimination_mode=EliminationMode.ROUND_ROBIN
        )

    assert result.is_err()
    assert 'elimination_mode' in result.unwrap_err()


def test_update_locked_field_contestant_type_when_ongoing():
    """Changing contestant_type on an ONGOING tournament must be rejected."""
    tournament = _make_tournament(TournamentStatus.ONGOING)

    with patch(f'{_REPO}.get_tournament', return_value=tournament):
        result = _call_update(
            tournament, contestant_type=ContestantType.TEAM
        )

    assert result.is_err()
    assert 'contestant_type' in result.unwrap_err()


def test_update_locked_field_when_paused():
    """Changing name on a PAUSED tournament must also be rejected."""
    tournament = _make_tournament(TournamentStatus.PAUSED)

    with patch(f'{_REPO}.get_tournament', return_value=tournament):
        result = _call_update(tournament, name='New Name')

    assert result.is_err()
    assert 'paused' in result.unwrap_err().lower()


# ------------------------------------------------------------------ #
# tests — no restrictions on other statuses
# ------------------------------------------------------------------ #


def test_update_all_fields_when_draft():
    """All fields should be editable on a DRAFT tournament."""
    tournament = _make_tournament(TournamentStatus.DRAFT)

    with (
        patch(f'{_REPO}.get_tournament', return_value=tournament),
        patch(f'{_REPO}.update_tournament'),
        patch(f'{_SIGNALS}.tournament_updated.send'),
    ):
        result = _call_update(
            tournament,
            name='New Name',
            game='NewGame',
            elimination_mode=EliminationMode.ROUND_ROBIN,
        )

    assert result.is_ok()


def test_update_all_fields_when_registration_open():
    """All fields should be editable on a REGISTRATION_OPEN tournament."""
    tournament = _make_tournament(TournamentStatus.REGISTRATION_OPEN)

    with (
        patch(f'{_REPO}.get_tournament', return_value=tournament),
        patch(f'{_REPO}.update_tournament'),
        patch(f'{_SIGNALS}.tournament_updated.send'),
    ):
        result = _call_update(
            tournament,
            name='New Name',
            contestant_type=ContestantType.TEAM,
        )

    assert result.is_ok()


# ------------------------------------------------------------------ #
# tests — error message quality
# ------------------------------------------------------------------ #


def test_update_multiple_locked_fields_lists_all():
    """When multiple locked fields change, the error should list all."""
    tournament = _make_tournament(TournamentStatus.ONGOING)

    with patch(f'{_REPO}.get_tournament', return_value=tournament):
        result = _call_update(
            tournament,
            name='New Name',
            game='NewGame',
            start_time=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )

    assert result.is_err()
    err = result.unwrap_err()
    assert 'name' in err
    assert 'game' in err
    assert 'start_time' in err
