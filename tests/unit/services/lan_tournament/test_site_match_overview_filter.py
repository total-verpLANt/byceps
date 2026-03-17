"""
tests.unit.services.lan_tournament.test_site_match_overview_filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the match readiness filter applied in the matches()
overview view.  The filter keeps only matches that are ready to play
(all contestants assigned) or already confirmed (including DEFWIN).

The logic under test is a list comprehension in ``views.matches()``:

    match_data = [
        entry for entry in match_data
        if len(entry['contestants']) >= 2
        or entry['match'].confirmed_by is not None
    ]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest

from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.user.models import UserID


# -- helpers ----------------------------------------------------------------


def _make_match(*, confirmed: bool = False) -> TournamentMatch:
    """Create a minimal TournamentMatch for filter testing."""
    return TournamentMatch(
        id=TournamentMatchID(uuid4()),
        tournament_id=TournamentID(uuid4()),
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=UserID(uuid4()) if confirmed else None,
        created_at=datetime.utcnow(),
    )


def _make_contestant() -> dict[str, Any]:
    """Minimal stand-in for a TournamentMatchToContestant."""
    return {'id': uuid4()}


def _apply_filter(match_data: list[dict]) -> list[dict]:
    """Reproduce the exact filter from views.matches()."""
    return [
        entry
        for entry in match_data
        if len(entry['contestants']) >= 2
        or entry['match'].confirmed_by is not None
    ]


# -- tests ------------------------------------------------------------------


class TestMatchOverviewFilter:
    """Match readiness filter for the overview page."""

    def test_filter_excludes_matches_with_no_contestants(self):
        """An empty match (0 contestants, not confirmed) is hidden."""
        entry = {'match': _make_match(), 'contestants': []}
        assert _apply_filter([entry]) == []

    def test_filter_includes_matches_with_two_contestants(self):
        """A fully-populated match (2 contestants) is shown."""
        entry = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        result = _apply_filter([entry])
        assert len(result) == 1
        assert result[0] is entry

    def test_filter_includes_confirmed_defwin_match(self):
        """A confirmed defwin (1 contestant, confirmed) is shown."""
        entry = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant()],
        }
        result = _apply_filter([entry])
        assert len(result) == 1
        assert result[0] is entry

    def test_filter_excludes_partially_filled_unconfirmed_match(self):
        """A match with 1 contestant but NOT confirmed is hidden."""
        entry = {
            'match': _make_match(confirmed=False),
            'contestants': [_make_contestant()],
        }
        assert _apply_filter([entry]) == []

    def test_filter_preserves_order(self):
        """Filtered output maintains original match ordering."""
        ready_a = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        empty = {'match': _make_match(), 'contestants': []}
        ready_b = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        result = _apply_filter([ready_a, empty, ready_b])
        assert result == [ready_a, ready_b]

    def test_filter_includes_confirmed_match_with_two_contestants(self):
        """A confirmed match with 2 contestants (normal completed) is shown."""
        entry = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        result = _apply_filter([entry])
        assert len(result) == 1

    def test_filter_returns_empty_when_no_matches_ready(self):
        """All-empty input produces empty output."""
        entries = [
            {'match': _make_match(), 'contestants': []},
            {'match': _make_match(), 'contestants': []},
            {'match': _make_match(), 'contestants': [_make_contestant()]},
        ]
        assert _apply_filter(entries) == []
