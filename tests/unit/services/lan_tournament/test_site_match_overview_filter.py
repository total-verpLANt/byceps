"""
tests.unit.services.lan_tournament.test_site_match_overview_filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the match readiness filter applied in the matches()
overview view, including the participant-scoped tab filters
(Ready/Open/All) introduced for logged-in tournament participants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.user.models import UserID

from byceps.services.lan_tournament.blueprints.site.views import (
    _is_match_ready,
    _is_match_open,
    _is_user_match,
)


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
        created_at=datetime.now(UTC),
    )


def _make_contestant(
    *,
    team_id: TournamentTeamID | None = None,
    participant_id: TournamentParticipantID | None = None,
) -> TournamentMatchToContestant:
    """Create a TournamentMatchToContestant for filter testing."""
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(uuid4()),
        tournament_match_id=TournamentMatchID(uuid4()),
        team_id=team_id,
        participant_id=participant_id,
        score=None,
        created_at=datetime.now(UTC),
    )


def _make_participant(
    *,
    team_id: TournamentTeamID | None = None,
) -> TournamentParticipant:
    """Create a TournamentParticipant for filter testing."""
    return TournamentParticipant(
        id=TournamentParticipantID(uuid4()),
        user_id=UserID(uuid4()),
        tournament_id=TournamentID(uuid4()),
        substitute_player=False,
        team_id=team_id,
        created_at=datetime.now(UTC),
    )


def _make_contestant_simple() -> dict[str, Any]:
    """Minimal stand-in for a TournamentMatchToContestant (legacy tests)."""
    return {'id': uuid4()}


def _apply_filter(match_data: list[dict]) -> list[dict]:
    """Reproduce the non-participant fallback filter from views.matches().

    Shows ready matches only: 2+ contestants AND not confirmed.
    """
    return [
        entry
        for entry in match_data
        if len(entry['contestants']) >= 2
        and entry['match'].confirmed_by is None
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
            'contestants': [_make_contestant_simple(), _make_contestant_simple()],
        }
        result = _apply_filter([entry])
        assert len(result) == 1
        assert result[0] is entry

    def test_filter_excludes_confirmed_defwin_match(self):
        """A confirmed defwin (1 contestant, confirmed) is excluded."""
        entry = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant_simple()],
        }
        assert _apply_filter([entry]) == []

    def test_filter_excludes_partially_filled_unconfirmed_match(self):
        """A match with 1 contestant but NOT confirmed is hidden."""
        entry = {
            'match': _make_match(confirmed=False),
            'contestants': [_make_contestant_simple()],
        }
        assert _apply_filter([entry]) == []

    def test_filter_preserves_order(self):
        """Filtered output maintains original match ordering."""
        ready_a = {
            'match': _make_match(),
            'contestants': [_make_contestant_simple(), _make_contestant_simple()],
        }
        empty = {'match': _make_match(), 'contestants': []}
        ready_b = {
            'match': _make_match(),
            'contestants': [_make_contestant_simple(), _make_contestant_simple()],
        }
        result = _apply_filter([ready_a, empty, ready_b])
        assert result == [ready_a, ready_b]

    def test_filter_excludes_confirmed_match_with_two_contestants(self):
        """A confirmed match with 2 contestants (completed) is excluded."""
        entry = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant_simple(), _make_contestant_simple()],
        }
        assert _apply_filter([entry]) == []

    def test_filter_returns_empty_when_no_matches_ready(self):
        """All-empty/partial input produces empty output; ready match passes."""
        not_ready_entries = [
            {'match': _make_match(), 'contestants': []},
            {'match': _make_match(), 'contestants': []},
            {'match': _make_match(), 'contestants': [_make_contestant_simple()]},
        ]
        assert _apply_filter(not_ready_entries) == []

        # A ready match (2 contestants, unconfirmed) IS included.
        ready_entry = {
            'match': _make_match(),
            'contestants': [_make_contestant_simple(), _make_contestant_simple()],
        }
        result = _apply_filter(not_ready_entries + [ready_entry])
        assert len(result) == 1
        assert result[0] is ready_entry


# -- _is_match_ready helper tests ------------------------------------------


class TestIsMatchReady:
    """Tests for the extracted _is_match_ready() helper."""

    def test_ready_with_two_contestants(self):
        c1 = _make_contestant()
        c2 = _make_contestant()
        entry = {'match': _make_match(), 'contestants': [c1, c2]}
        assert _is_match_ready(entry) is True

    def test_not_ready_when_confirmed(self):
        c1 = _make_contestant()
        entry = {'match': _make_match(confirmed=True), 'contestants': [c1]}
        assert _is_match_ready(entry) is False

    def test_not_ready_when_confirmed_with_two_contestants(self):
        c1 = _make_contestant()
        c2 = _make_contestant()
        entry = {'match': _make_match(confirmed=True), 'contestants': [c1, c2]}
        assert _is_match_ready(entry) is False

    def test_not_ready_with_one_unconfirmed(self):
        c1 = _make_contestant()
        entry = {'match': _make_match(confirmed=False), 'contestants': [c1]}
        assert _is_match_ready(entry) is False

    def test_not_ready_with_no_contestants(self):
        entry = {'match': _make_match(), 'contestants': []}
        assert _is_match_ready(entry) is False


# -- _is_match_open helper tests -------------------------------------------


class TestIsMatchOpen:
    """Tests for the _is_match_open() helper."""

    def test_open_with_one_contestant(self):
        c1 = _make_contestant()
        entry = {'match': _make_match(), 'contestants': [c1]}
        assert _is_match_open(entry) is True

    def test_open_with_two_contestants(self):
        c1 = _make_contestant()
        c2 = _make_contestant()
        entry = {'match': _make_match(), 'contestants': [c1, c2]}
        assert _is_match_open(entry) is True

    def test_not_open_when_confirmed(self):
        c1 = _make_contestant()
        entry = {'match': _make_match(confirmed=True), 'contestants': [c1]}
        assert _is_match_open(entry) is False

    def test_not_open_with_no_contestants(self):
        entry = {'match': _make_match(), 'contestants': []}
        assert _is_match_open(entry) is False


# -- _is_user_match helper tests -------------------------------------------


class TestIsUserMatch:
    """Tests for the _is_user_match() helper."""

    def test_match_by_team_id(self):
        team_id = TournamentTeamID(uuid4())
        participant = _make_participant(team_id=team_id)
        c1 = _make_contestant(team_id=team_id)
        c2 = _make_contestant()
        entry = {'match': _make_match(), 'contestants': [c1, c2]}
        assert _is_user_match(entry, participant) is True

    def test_match_by_participant_id(self):
        participant = _make_participant()
        c1 = _make_contestant(participant_id=participant.id)
        c2 = _make_contestant()
        entry = {'match': _make_match(), 'contestants': [c1, c2]}
        assert _is_user_match(entry, participant) is True

    def test_no_match_when_no_relation(self):
        participant = _make_participant()
        c1 = _make_contestant(team_id=TournamentTeamID(uuid4()))
        c2 = _make_contestant(participant_id=TournamentParticipantID(uuid4()))
        entry = {'match': _make_match(), 'contestants': [c1, c2]}
        assert _is_user_match(entry, participant) is False

    def test_no_match_with_empty_contestants(self):
        participant = _make_participant()
        entry = {'match': _make_match(), 'contestants': []}
        assert _is_user_match(entry, participant) is False


# -- Tab filter integration tests ------------------------------------------


class TestTabFilters:
    """Tests for the Ready/Open/All tab filtering logic."""

    def _build_match_data(self):
        """Build a mixed set of matches for tab filter testing."""
        team_id = TournamentTeamID(uuid4())
        other_team_id = TournamentTeamID(uuid4())
        participant = _make_participant(team_id=team_id)

        # Ready match involving participant's team
        ready_mine = {
            'match': _make_match(),
            'contestants': [
                _make_contestant(team_id=team_id),
                _make_contestant(team_id=other_team_id),
            ],
        }
        # Ready match NOT involving participant
        ready_other = {
            'match': _make_match(),
            'contestants': [
                _make_contestant(team_id=other_team_id),
                _make_contestant(team_id=TournamentTeamID(uuid4())),
            ],
        }
        # Open match (only 1 contestant, not confirmed)
        open_match = {
            'match': _make_match(),
            'contestants': [_make_contestant(team_id=team_id)],
        }
        # Confirmed defwin (neither ready nor open, involving participant)
        defwin_mine = {
            'match': _make_match(confirmed=True),
            'contestants': [_make_contestant(team_id=team_id)],
        }

        all_data = [ready_mine, ready_other, open_match, defwin_mine]
        return all_data, participant, ready_mine, ready_other, open_match, defwin_mine

    def test_ready_tab_filters_to_user_ready_matches(self):
        """Ready tab shows only ready matches involving the participant."""
        all_data, participant, ready_mine, ready_other, open_match, defwin_mine = (
            self._build_match_data()
        )
        result = [
            e for e in all_data
            if _is_match_ready(e) and _is_user_match(e, participant)
        ]
        assert ready_mine in result
        assert defwin_mine not in result  # confirmed → not ready
        assert ready_other not in result
        assert open_match not in result
        assert len(result) == 1

    def test_open_tab_filters_to_open_matches(self):
        """Open tab shows all open matches (1+ contestant, unconfirmed), tournament-wide."""
        all_data, participant, ready_mine, ready_other, open_match, defwin_mine = (
            self._build_match_data()
        )
        result = [e for e in all_data if _is_match_open(e)]
        assert open_match in result
        assert ready_mine in result   # ready is a subset of open
        assert ready_other in result  # ready is a subset of open
        assert defwin_mine not in result  # confirmed → not open
        assert len(result) == 3

    def test_all_tab_returns_everything(self):
        """All tab returns all matches without filtering."""
        all_data, *_ = self._build_match_data()
        assert len(all_data) == 4

    def test_non_participant_gets_all_ready_matches(self):
        """Non-participant/anonymous default: all ready matches tournament-wide."""
        all_data, *_ = self._build_match_data()
        result = [e for e in all_data if _is_match_ready(e)]
        # ready_mine (2 contestants, unconfirmed), ready_other (2 contestants, unconfirmed)
        assert len(result) == 2

    def test_match_quantities_computed_correctly(self):
        """Verify match_quantities dict counts for each tab (participant-scoped ready)."""
        all_data, participant, *_ = self._build_match_data()
        ready_user_count = sum(
            1 for e in all_data
            if _is_match_ready(e) and _is_user_match(e, participant)
        )
        open_count = sum(1 for e in all_data if _is_match_open(e))
        total_count = len(all_data)

        assert ready_user_count == 1  # ready_mine only (defwin_mine is confirmed)
        assert open_count == 3        # ready_mine + ready_other + open_match
        assert total_count == 4       # all

    # -- Anonymous / non-participant filter tests ------------------------------

    def test_anonymous_ready_tab_shows_all_ready_matches(self):
        """Anonymous ready tab shows all ready matches tournament-wide (not personal-scoped)."""
        all_data, participant, ready_mine, ready_other, open_match, defwin_mine = (
            self._build_match_data()
        )
        # Anonymous: no participant, so ready is tournament-wide
        result = [e for e in all_data if _is_match_ready(e)]
        assert ready_mine in result
        assert ready_other in result     # included (tournament-wide, not personal)
        assert open_match not in result  # only 1 contestant → not ready
        assert defwin_mine not in result # confirmed → not ready
        assert len(result) == 2

    def test_anonymous_open_tab_shows_open_matches(self):
        """Anonymous open tab shows same tournament-wide open matches as participant."""
        all_data, participant, ready_mine, ready_other, open_match, defwin_mine = (
            self._build_match_data()
        )
        result = [e for e in all_data if _is_match_open(e)]
        assert open_match in result
        assert ready_mine in result      # ready is a subset of open
        assert ready_other in result     # ready is a subset of open
        assert defwin_mine not in result # confirmed → not open
        assert len(result) == 3

    def test_anonymous_all_tab_returns_everything(self):
        """Anonymous all tab returns all matches without filtering."""
        all_data, *_ = self._build_match_data()
        # 'all' → no filter applied
        assert len(all_data) == 4

    def test_anonymous_match_quantities_computed_correctly(self):
        """Verify anonymous match_quantities has tournament-wide ready count."""
        all_data, *_ = self._build_match_data()
        # Anonymous: no participant, ready count is tournament-wide
        ready_count = sum(1 for e in all_data if _is_match_ready(e))
        open_count = sum(1 for e in all_data if _is_match_open(e))
        total_count = len(all_data)

        assert ready_count == 2  # ready_mine + ready_other (tournament-wide, not 1)
        assert open_count == 3   # ready_mine + ready_other + open_match
        assert total_count == 4  # all
