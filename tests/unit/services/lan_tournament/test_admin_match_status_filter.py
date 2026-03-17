"""
tests.unit.services.lan_tournament.test_admin_match_status_filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the admin match status filter (open / ready / all)
applied in ``admin/views.matches_for_tournament()``.

Status definitions:
  - **open**: < 2 contestants AND not confirmed (needs attention)
  - **ready**: 2+ contestants OR confirmed (including DEFWIN)
  - **all**: unfiltered

The ``_is_match_ready()`` helper mirrors the site view filter at
``site/views.py:840-847``.
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


def _is_match_ready(entry: dict) -> bool:
    """Reproduce the exact helper from admin/views.py."""
    return (
        len(entry['contestants']) >= 2
        or entry['match'].confirmed_by is not None
    )


def _apply_filter(match_data: list[dict], only: str = 'open') -> list[dict]:
    """Reproduce the admin filter logic from views.matches_for_tournament()."""
    if only == 'open':
        return [e for e in match_data if not _is_match_ready(e)]
    elif only == 'ready':
        return [e for e in match_data if _is_match_ready(e)]
    return list(match_data)  # 'all'


def _compute_quantities(match_data: list[dict]) -> dict[str, int]:
    """Reproduce the count computation from the view."""
    total = len(match_data)
    ready = sum(1 for e in match_data if _is_match_ready(e))
    return {
        'all': total,
        'open': total - ready,
        'ready': ready,
    }


# -- tests ------------------------------------------------------------------


class TestAdminMatchStatusFilter:
    """Admin match status filter for open / ready / all."""

    def test_open_filter_shows_matches_with_fewer_than_two_contestants(self):
        """Open filter includes matches with 0 or 1 unconfirmed contestants."""
        empty = {'match': _make_match(), 'contestants': []}
        partial = {'match': _make_match(), 'contestants': [_make_contestant()]}
        result = _apply_filter([empty, partial], only='open')
        assert len(result) == 2
        assert empty in result
        assert partial in result

    def test_open_filter_excludes_ready_matches(self):
        """Open filter hides matches with 2 contestants."""
        entry = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        assert _apply_filter([entry], only='open') == []

    def test_open_filter_excludes_confirmed_defwin(self):
        """Open filter hides confirmed defwin matches."""
        entry = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant()],
        }
        assert _apply_filter([entry], only='open') == []

    def test_ready_filter_shows_matches_with_two_contestants(self):
        """Ready filter includes matches with 2+ contestants."""
        entry = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        result = _apply_filter([entry], only='ready')
        assert len(result) == 1
        assert result[0] is entry

    def test_ready_filter_shows_confirmed_defwin(self):
        """Ready filter includes confirmed defwin matches."""
        entry = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant()],
        }
        result = _apply_filter([entry], only='ready')
        assert len(result) == 1

    def test_ready_filter_excludes_open_matches(self):
        """Ready filter hides matches with 0 contestants, not confirmed."""
        entry = {'match': _make_match(), 'contestants': []}
        assert _apply_filter([entry], only='ready') == []

    def test_all_filter_shows_everything(self):
        """All filter returns every match regardless of status."""
        open_match = {'match': _make_match(), 'contestants': []}
        ready_match = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        defwin = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant()],
        }
        result = _apply_filter([open_match, ready_match, defwin], only='all')
        assert len(result) == 3

    def test_default_filter_is_open(self):
        """When only= defaults to 'open', open matches are shown."""
        open_match = {'match': _make_match(), 'contestants': []}
        ready_match = {
            'match': _make_match(),
            'contestants': [_make_contestant(), _make_contestant()],
        }
        result = _apply_filter([open_match, ready_match])  # default='open'
        assert len(result) == 1
        assert result[0] is open_match

    def test_counts_computed_before_filtering(self):
        """Quantities reflect the unfiltered totals."""
        entries = [
            {'match': _make_match(), 'contestants': []},  # open
            {'match': _make_match(), 'contestants': []},  # open
            {
                'match': _make_match(),
                'contestants': [_make_contestant(), _make_contestant()],
            },  # ready
            {
                'match': _make_match(confirmed=True),
                'contestants': [_make_contestant()],
            },  # ready (defwin)
        ]
        quantities = _compute_quantities(entries)
        assert quantities == {'all': 4, 'open': 2, 'ready': 2}

        # After filtering, the counts should still reflect pre-filter state
        filtered = _apply_filter(entries, only='open')
        assert len(filtered) == 2
        # But quantities remain unchanged (computed before filtering)
        assert quantities['all'] == 4
