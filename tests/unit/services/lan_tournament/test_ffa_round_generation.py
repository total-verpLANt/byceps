"""
tests.unit.services.lan_tournament.test_ffa_round_generation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for FFA match service functions:
generate_ffa_round, set_ffa_placements, confirm_ffa_match,
advance_ffa_round.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, UTC
from unittest.mock import Mock, patch

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
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
from byceps.services.lan_tournament import tournament_match_service
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')
USER_ID = UserID(generate_uuid())


# -------------------------------------------------------------------- #
# generate_ffa_round
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_ffa_round_creates_correct_match_count(mock_repo):
    """8 players, groups of max 4 -> 2 matches created."""
    tournament = _create_ffa_tournament(
        group_size_min=3, group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    contestant_ids = [str(generate_uuid()) for _ in range(8)]

    result = tournament_match_service.generate_ffa_round(
        TOURNAMENT_ID, 0, contestant_ids,
    )

    assert result.is_ok()
    assert result.unwrap() == 2
    assert mock_repo.create_match.call_count == 2
    mock_repo.commit_session.assert_called_once()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_ffa_round_assigns_contestants_to_groups(mock_repo):
    """Verify each match gets the right number of contestants."""
    tournament = _create_ffa_tournament(
        group_size_min=2, group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    contestant_ids = [str(generate_uuid()) for _ in range(6)]

    result = tournament_match_service.generate_ffa_round(
        TOURNAMENT_ID, 0, contestant_ids,
    )

    assert result.is_ok()
    # ceil(6/4) = 2 groups; 6 contestants total -> 6 create_match_contestant calls
    assert mock_repo.create_match_contestant.call_count == 6

    # Check all created contestants have participant_id set
    for call in mock_repo.create_match_contestant.call_args_list:
        contestant = call.args[0]
        assert contestant.participant_id is not None
        assert contestant.team_id is None


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_ffa_round_rejects_team_insufficient_max_teams(mock_repo):
    """Team FFA rejects when max_teams < group_size_min."""
    tournament = _create_ffa_tournament(
        contestant_type=ContestantType.TEAM,
        max_teams=2,
        group_size_min=3,
    )
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.generate_ffa_round(
        TOURNAMENT_ID, 0, ['a', 'b', 'c'],
    )

    assert result.is_err()
    assert 'team' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# set_ffa_placements
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_ffa_placements_validates_sequential(mock_repo):
    """Placements must be 1-based sequential with no gaps."""
    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    match = _create_match(match_id=match_id)
    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, match_id),
        _make_contestant(pid_b, match_id),
        _make_contestant(pid_c, match_id),
    ]
    mock_repo.get_tournament.return_value = _create_ffa_tournament()

    # Gap: 1, 2, 4 (missing 3)
    placements = {
        str(pid_a): 1,
        str(pid_b): 2,
        str(pid_c): 4,
    }

    result = tournament_match_service.set_ffa_placements(
        match_id, placements,
    )

    assert result.is_err()
    assert 'sequential' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_ffa_placements_validates_complete(mock_repo):
    """All contestants must have placements."""
    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    match = _create_match(match_id=match_id)
    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, match_id),
        _make_contestant(pid_b, match_id),
        _make_contestant(pid_c, match_id),
    ]
    mock_repo.get_tournament.return_value = _create_ffa_tournament()

    # Only 2 placements for 3 contestants
    placements = {
        str(pid_a): 1,
        str(pid_b): 2,
    }

    result = tournament_match_service.set_ffa_placements(
        match_id, placements,
    )

    assert result.is_err()
    assert 'Expected placements for 3' in result.unwrap_err()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_ffa_placements_stores_points(mock_repo):
    """Valid placements store placement + points on each contestant."""
    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    c_a = _make_contestant(pid_a, match_id)
    c_b = _make_contestant(pid_b, match_id)

    match = _create_match(match_id=match_id)
    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = [c_a, c_b]
    mock_repo.get_tournament.return_value = _create_ffa_tournament(
        point_table=[10, 7],
    )

    placements = {
        str(pid_a): 1,
        str(pid_b): 2,
    }

    result = tournament_match_service.set_ffa_placements(
        match_id, placements,
    )

    assert result.is_ok()
    # Verify update_contestant_placement_and_points was called with correct dict
    mock_repo.update_contestant_placement_and_points.assert_called_once()
    updates = mock_repo.update_contestant_placement_and_points.call_args.args[0]
    assert updates[c_a.id] == (1, 10)
    assert updates[c_b.id] == (2, 7)


# -------------------------------------------------------------------- #
# confirm_ffa_match
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.match_confirmed'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_ffa_match_assigns_points(mock_repo, mock_signal):
    """Confirming an FFA match with placements set succeeds and dispatches signal."""
    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    match = _create_match(match_id=match_id)
    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, match_id, placement=1, points=10),
        _make_contestant(pid_b, match_id, placement=2, points=7),
    ]
    mock_repo.get_tournament.return_value = _create_ffa_tournament()

    result = tournament_match_service.confirm_ffa_match(
        match_id, USER_ID,
    )

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(match_id, USER_ID)
    mock_repo.commit_session.assert_called_once()
    # Signal must be dispatched after commit.
    mock_signal.send.assert_called_once()
    event = mock_signal.send.call_args[1]['event']
    assert event.match_id == match_id
    assert event.winner_participant_id == pid_a


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_ffa_match_rejects_missing_placements(mock_repo):
    """Cannot confirm FFA match when placements are not set."""
    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    match = _create_match(match_id=match_id)
    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, match_id, placement=1, points=10),
        _make_contestant(pid_b, match_id, placement=None, points=None),
    ]

    result = tournament_match_service.confirm_ffa_match(
        match_id, USER_ID,
    )

    assert result.is_err()
    assert 'placements' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_ffa_match_rejects_already_confirmed(mock_repo):
    """Cannot confirm an already-confirmed match."""
    match_id = TournamentMatchID(generate_uuid())
    match = _create_match(match_id=match_id, confirmed_by=USER_ID)
    mock_repo.get_match_for_update.return_value = match

    result = tournament_match_service.confirm_ffa_match(
        match_id, USER_ID,
    )

    assert result.is_err()
    assert 'already confirmed' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# advance_ffa_round
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_advance_ffa_round_selects_top_n(mock_repo):
    """Advances the top N contestants; auto-generates the final round when
    survivors fit in a single group."""
    tournament = _create_ffa_tournament(
        advancement_count=2,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    match_id_1 = TournamentMatchID(generate_uuid())
    match_id_2 = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())
    pid_d = TournamentParticipantID(generate_uuid())

    # Round 0 matches — all confirmed.
    match_1 = _create_match(match_id=match_id_1, confirmed_by=USER_ID, round=0)
    match_2 = _create_match(match_id=match_id_2, confirmed_by=USER_ID, round=0)

    mock_repo.get_matches_for_tournament_ordered.return_value = [
        match_1, match_2,
    ]
    mock_repo.get_matches_for_round.return_value = [match_1, match_2]

    # Contestant data for each match.
    def get_contestants_for_match(mid):
        if mid == match_id_1:
            return [
                _make_contestant(pid_a, match_id_1, placement=1, points=10),
                _make_contestant(pid_b, match_id_1, placement=2, points=7),
            ]
        elif mid == match_id_2:
            return [
                _make_contestant(pid_c, match_id_2, placement=1, points=10),
                _make_contestant(pid_d, match_id_2, placement=2, points=5),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants_for_match

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 4 survivors fit in group_size_max=4 -> final round auto-generated (1 group)
    assert result.unwrap() == 1
    # Verify final round was created via create_match + create_match_contestant
    assert mock_repo.create_match.called
    assert mock_repo.create_match_contestant.called


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_advance_ffa_round_generates_next_round(mock_repo):
    """When survivors exceed group_size_max, a new round is generated."""
    tournament = _create_ffa_tournament(
        advancement_count=3,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    match_ids = [TournamentMatchID(generate_uuid()) for _ in range(3)]
    pids = [TournamentParticipantID(generate_uuid()) for _ in range(12)]

    # Round 0: 3 matches of 4, advancement_count=3 -> 9 survivors
    matches = [
        _create_match(match_id=mid, confirmed_by=USER_ID, round=0)
        for mid in match_ids
    ]

    mock_repo.get_matches_for_tournament_ordered.return_value = matches
    mock_repo.get_matches_for_round.return_value = matches

    def get_contestants_for_match(mid):
        idx = match_ids.index(mid)
        base = idx * 4
        return [
            _make_contestant(
                pids[base + i], mid,
                placement=i + 1,
                points=10 - i * 2,
            )
            for i in range(4)
        ]

    mock_repo.get_contestants_for_match.side_effect = get_contestants_for_match

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 9 survivors > group_size_max=4, so generate_ffa_round was called
    assert mock_repo.create_match.call_count > 0


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_advance_ffa_round_detects_tie(mock_repo):
    """Returns Err when there is a tie at the advancement cutoff."""
    tournament = _create_ffa_tournament(advancement_count=1)
    mock_repo.get_tournament.return_value = tournament

    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    match = _create_match(match_id=match_id, confirmed_by=USER_ID, round=0)

    mock_repo.get_matches_for_tournament_ordered.return_value = [match]
    mock_repo.get_matches_for_round.return_value = [match]

    # Both contestants have the same points -> tie at cutoff.
    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, match_id, placement=1, points=10),
        _make_contestant(pid_b, match_id, placement=2, points=10),
    ]

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'tie' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_advance_ffa_round_rejects_unconfirmed(mock_repo):
    """Returns Err when not all matches in the round are confirmed."""
    tournament = _create_ffa_tournament(advancement_count=2)
    mock_repo.get_tournament.return_value = tournament

    match_id = TournamentMatchID(generate_uuid())
    match = _create_match(match_id=match_id, confirmed_by=None, round=0)

    mock_repo.get_matches_for_tournament_ordered.return_value = [match]
    mock_repo.get_matches_for_round.return_value = [match]

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'not confirmed' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_ffa_tournament(**kwargs) -> Tournament:
    """Create an FFA tournament for testing."""
    defaults = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'FFA Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'updated_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.ONGOING,
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
        'point_table': [10, 7, 5, 3, 1],
        'advancement_count': 4,
        'group_size_min': 3,
        'group_size_max': 8,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _create_match(
    *,
    match_id: TournamentMatchID | None = None,
    confirmed_by: UserID | None = None,
    round: int | None = None,
) -> TournamentMatch:
    """Create a tournament match for testing."""
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=round,
        next_match_id=None,
        confirmed_by=confirmed_by,
        created_at=NOW,
    )


def _make_contestant(
    participant_id: TournamentParticipantID,
    match_id: TournamentMatchID,
    *,
    placement: int | None = None,
    points: int | None = None,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=participant_id,
        score=None,
        placement=placement,
        points=points,
        created_at=NOW,
    )
