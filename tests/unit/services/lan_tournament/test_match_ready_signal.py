"""
tests.unit.services.lan_tournament.test_match_ready_signal
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for ``_collect_ready_match_events``.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import UTC, datetime
from unittest.mock import patch

from byceps.services.lan_tournament import tournament_match_service
from byceps.services.lan_tournament.events import MatchReadyEvent
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())


def _make_contestant(match_id: TournamentMatchID) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=TournamentParticipantID(generate_uuid()),
        score=None,
        created_at=NOW,
    )


# -------------------------------------------------------------------- #
# _collect_ready_match_events
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_collect_ready_match_events_returns_event_when_two_contestants(
    mock_repo,
):
    """Match with 2 contestants produces a MatchReadyEvent."""
    match_id = TournamentMatchID(generate_uuid())
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(match_id),
        _make_contestant(match_id),
    ]

    events = tournament_match_service._collect_ready_match_events(
        {match_id}, TOURNAMENT_ID, NOW,
    )

    assert len(events) == 1
    assert isinstance(events[0], MatchReadyEvent)
    assert events[0].match_id == match_id
    assert events[0].tournament_id == TOURNAMENT_ID


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_collect_ready_match_events_skips_when_one_contestant(mock_repo):
    """Match with only 1 contestant produces no event."""
    match_id = TournamentMatchID(generate_uuid())
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(match_id),
    ]

    events = tournament_match_service._collect_ready_match_events(
        {match_id}, TOURNAMENT_ID, NOW,
    )

    assert events == []


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_collect_ready_match_events_skips_when_zero_contestants(mock_repo):
    """Match with 0 contestants produces no event."""
    match_id = TournamentMatchID(generate_uuid())
    mock_repo.get_contestants_for_match.return_value = []

    events = tournament_match_service._collect_ready_match_events(
        {match_id}, TOURNAMENT_ID, NOW,
    )

    assert events == []


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_collect_ready_match_events_multiple_matches(mock_repo):
    """Multiple matches: only those with >= 2 contestants get events."""
    ready_id = TournamentMatchID(generate_uuid())
    not_ready_id = TournamentMatchID(generate_uuid())
    also_ready_id = TournamentMatchID(generate_uuid())

    def _side_effect(mid):
        if mid == ready_id:
            return [_make_contestant(mid), _make_contestant(mid)]
        if mid == not_ready_id:
            return [_make_contestant(mid)]
        if mid == also_ready_id:
            return [
                _make_contestant(mid),
                _make_contestant(mid),
                _make_contestant(mid),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = _side_effect

    events = tournament_match_service._collect_ready_match_events(
        {ready_id, not_ready_id, also_ready_id}, TOURNAMENT_ID, NOW,
    )

    assert len(events) == 2
    event_match_ids = {e.match_id for e in events}
    assert ready_id in event_match_ids
    assert also_ready_id in event_match_ids
    assert not_ready_id not in event_match_ids
