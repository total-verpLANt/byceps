from datetime import UTC, datetime
from unittest.mock import patch

from byceps.services.lan_tournament import tournament_match_service
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
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.elimination_mode import EliminationMode
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')
INITIATOR_ID = UserID(generate_uuid())


# -------------------------------------------------------------------- #
# handle_defwin_for_removed_participant
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_no_entries(mock_repo):
    """No contestant entries => no events, no deletions."""
    participant_id = TournamentParticipantID(generate_uuid())

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = []

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.delete_contestant_from_match.assert_not_called()
    mock_repo.create_match_contestant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_sole_opponent_advances(mock_repo):
    """When removed participant leaves one opponent alone in an
    unconfirmed match with a next_match_id, the opponent auto-
    advances."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=next_match_id)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    # After deletion, only the opponent remains
    mock_repo.get_contestants_for_match.return_value = [opponent]
    # Next match has no contestants yet
    mock_repo.get_contestants_for_match.side_effect = lambda mid: (
        [opponent] if mid == match_id else []
    )

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert len(result.advanced) == 1
    assert result.advanced[0].tournament_id == TOURNAMENT_ID
    assert result.advanced[0].match_id == next_match_id
    assert result.advanced[0].from_match_id == match_id
    assert result.advanced[0].advanced_participant_id == opponent_participant_id
    # No initiator_id => no confirmation/completion events
    assert result.confirmed == []
    assert result.completed == []

    # Contestant was deleted from the original match
    mock_repo.delete_contestant_from_match.assert_called_once_with(
        match_id, participant_id=participant_id
    )
    # Opponent was advanced to next match
    mock_repo.create_match_contestant.assert_called_once()
    # No initiator_id => confirm_match NOT called
    mock_repo.confirm_match.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_terminal_no_advance_no_initiator(mock_repo):
    """When removed participant is in a terminal match (no next_match_id)
    and no initiator_id is provided, no advancement and no confirmation
    occurs."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=None)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.return_value = [opponent]

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.create_match_contestant.assert_not_called()
    mock_repo.confirm_match.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_both_removed_no_advance(mock_repo):
    """When both contestants are removed from a match (len==0),
    no advancement occurs."""
    participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=next_match_id)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    # After deletion, no contestants remain
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.create_match_contestant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_already_advanced_skips(mock_repo):
    """When the sole remaining opponent is already in the next match,
    skip advancement to avoid duplicates."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=next_match_id)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )
    # Opponent already in next match
    opponent_in_next = _create_contestant(
        match_id=next_match_id,
        participant_id=opponent_participant_id,
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.side_effect = lambda mid: (
        [opponent] if mid == match_id else [opponent_in_next]
    )

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.create_match_contestant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_multiple_matches(mock_repo):
    """When a participant appears in multiple unconfirmed matches,
    all are processed."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent1_id = TournamentParticipantID(generate_uuid())
    opponent2_id = TournamentParticipantID(generate_uuid())

    match1_id = TournamentMatchID(generate_uuid())
    match2_id = TournamentMatchID(generate_uuid())
    next1_id = TournamentMatchID(generate_uuid())
    next2_id = TournamentMatchID(generate_uuid())

    match1 = _create_match(match_id=match1_id, next_match_id=next1_id)
    match2 = _create_match(match_id=match2_id, next_match_id=next2_id)

    contestant1 = _create_contestant(
        match_id=match1_id, participant_id=participant_id
    )
    contestant2 = _create_contestant(
        match_id=match2_id, participant_id=participant_id
    )
    opponent1 = _create_contestant(
        match_id=match1_id, participant_id=opponent1_id
    )
    opponent2 = _create_contestant(
        match_id=match2_id, participant_id=opponent2_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant1, match1),
        (contestant2, match2),
    ]

    def get_contestants(mid):
        if mid == match1_id:
            return [opponent1]
        if mid == match2_id:
            return [opponent2]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert len(result.advanced) == 2
    # No initiator_id => no confirmation/completion events
    assert result.confirmed == []
    assert result.completed == []
    assert mock_repo.delete_contestant_from_match.call_count == 2
    assert mock_repo.create_match_contestant.call_count == 2


# -------------------------------------------------------------------- #
# handle_defwin_for_removed_team
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_team_no_entries(mock_repo):
    """No contestant entries for team => no events."""
    team_id = TournamentTeamID(generate_uuid())

    mock_repo.find_contestant_entries_for_team_in_tournament.return_value = []

    result = tournament_match_service.handle_defwin_for_removed_team(
        TOURNAMENT_ID, team_id
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.delete_contestant_from_match.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_team_sole_opponent_advances(mock_repo):
    """When removed team leaves one opponent team alone, the opponent
    auto-advances."""
    team_id = TournamentTeamID(generate_uuid())
    opponent_team_id = TournamentTeamID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=next_match_id)
    contestant = _create_contestant(match_id=match_id, team_id=team_id)
    opponent = _create_contestant(match_id=match_id, team_id=opponent_team_id)

    mock_repo.find_contestant_entries_for_team_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.side_effect = lambda mid: (
        [opponent] if mid == match_id else []
    )

    result = tournament_match_service.handle_defwin_for_removed_team(
        TOURNAMENT_ID, team_id
    )

    assert len(result.advanced) == 1
    assert result.advanced[0].advanced_team_id == opponent_team_id
    assert result.advanced[0].match_id == next_match_id
    # No initiator_id => no confirmation/completion events
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.create_match_contestant.assert_called_once()
    # No initiator_id => confirm_match NOT called
    mock_repo.confirm_match.assert_not_called()


# -------------------------------------------------------------------- #
# DEFWIN auto-confirmation with initiator_id
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_with_initiator_calls_confirm_match(mock_repo):
    """When initiator_id is provided, confirm_match() is called on the
    DEFWIN match after auto-advancing the sole opponent."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=next_match_id)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.side_effect = lambda mid: (
        [opponent] if mid == match_id else []
    )

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id, initiator_id=INITIATOR_ID
    )

    assert len(result.advanced) == 1
    assert result.advanced[0].advanced_participant_id == opponent_participant_id

    # confirm_match called with original match_id and initiator
    mock_repo.confirm_match.assert_called_once_with(match_id, INITIATOR_ID)

    # MatchConfirmedEvent emitted for the defwin match
    assert len(result.confirmed) == 1
    assert result.confirmed[0].match_id == match_id
    assert result.confirmed[0].winner_participant_id == opponent_participant_id
    # Non-terminal → no tournament completion
    assert result.completed == []


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_terminal_with_initiator_confirms(mock_repo):
    """When a terminal match (no next_match_id) has a sole remaining
    opponent and initiator_id is provided, the match IS confirmed
    even though no advancement occurs."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=None)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.return_value = [opponent]

    # get_tournament() is called for auto-complete check on terminal matches.
    mock_tournament = _create_tournament(game_format=GameFormat.ONE_V_ONE, elimination_mode=EliminationMode.ROUND_ROBIN)
    mock_repo.get_tournament.return_value = mock_tournament

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id, initiator_id=INITIATOR_ID
    )

    # No advancement (no next_match_id), but match IS confirmed.
    assert result.advanced == []
    mock_repo.create_match_contestant.assert_not_called()
    mock_repo.confirm_match.assert_called_once_with(match_id, INITIATOR_ID)

    # MatchConfirmedEvent emitted
    assert len(result.confirmed) == 1
    assert result.confirmed[0].match_id == match_id
    # RR mode → no auto-complete
    assert result.completed == []
    mock_repo.set_tournament_winner.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_multiple_matches_with_initiator(mock_repo):
    """When a participant is in multiple matches and initiator_id is set,
    confirm_match() is called once per DEFWIN auto-advance."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent1_id = TournamentParticipantID(generate_uuid())
    opponent2_id = TournamentParticipantID(generate_uuid())

    match1_id = TournamentMatchID(generate_uuid())
    match2_id = TournamentMatchID(generate_uuid())
    next1_id = TournamentMatchID(generate_uuid())
    next2_id = TournamentMatchID(generate_uuid())

    match1 = _create_match(match_id=match1_id, next_match_id=next1_id)
    match2 = _create_match(match_id=match2_id, next_match_id=next2_id)

    contestant1 = _create_contestant(
        match_id=match1_id, participant_id=participant_id
    )
    contestant2 = _create_contestant(
        match_id=match2_id, participant_id=participant_id
    )
    opponent1 = _create_contestant(
        match_id=match1_id, participant_id=opponent1_id
    )
    opponent2 = _create_contestant(
        match_id=match2_id, participant_id=opponent2_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant1, match1),
        (contestant2, match2),
    ]

    def get_contestants(mid):
        if mid == match1_id:
            return [opponent1]
        if mid == match2_id:
            return [opponent2]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id, initiator_id=INITIATOR_ID
    )

    assert len(result.advanced) == 2
    assert mock_repo.confirm_match.call_count == 2
    mock_repo.confirm_match.assert_any_call(match1_id, INITIATOR_ID)
    mock_repo.confirm_match.assert_any_call(match2_id, INITIATOR_ID)

    # Two MatchConfirmedEvents (one per match)
    assert len(result.confirmed) == 2
    # Non-terminal → no completion
    assert result.completed == []


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_team_with_initiator_calls_confirm_match(mock_repo):
    """When initiator_id is provided for a team removal, confirm_match()
    is called on the DEFWIN match."""
    team_id = TournamentTeamID(generate_uuid())
    opponent_team_id = TournamentTeamID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=next_match_id)
    contestant = _create_contestant(match_id=match_id, team_id=team_id)
    opponent = _create_contestant(match_id=match_id, team_id=opponent_team_id)

    mock_repo.find_contestant_entries_for_team_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.side_effect = lambda mid: (
        [opponent] if mid == match_id else []
    )

    result = tournament_match_service.handle_defwin_for_removed_team(
        TOURNAMENT_ID, team_id, initiator_id=INITIATOR_ID
    )

    assert len(result.advanced) == 1
    assert result.advanced[0].advanced_team_id == opponent_team_id
    mock_repo.confirm_match.assert_called_once_with(match_id, INITIATOR_ID)

    # MatchConfirmedEvent emitted for team defwin
    assert len(result.confirmed) == 1
    assert result.confirmed[0].match_id == match_id
    assert result.confirmed[0].winner_team_id == opponent_team_id
    # Non-terminal → no completion
    assert result.completed == []


# -------------------------------------------------------------------- #
# Terminal defwin auto-confirmation
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_terminal_no_initiator_no_confirm(mock_repo):
    """Terminal match without initiator_id: no confirmation, no events."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=None)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.return_value = [opponent]

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.create_match_contestant.assert_not_called()
    mock_repo.confirm_match.assert_not_called()
    mock_repo.get_tournament.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_terminal_elimination_triggers_auto_complete(mock_repo):
    """Terminal SE match with initiator: confirm_match called AND
    auto-complete triggered (set_tournament_winner + status flush)."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=None)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.return_value = [opponent]

    # SE mode → auto-complete should trigger.
    from byceps.util.result import Ok

    mock_tournament = _create_tournament(game_format=GameFormat.ONE_V_ONE, elimination_mode=EliminationMode.SINGLE_ELIMINATION)
    mock_repo.get_tournament.return_value = mock_tournament
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id, initiator_id=INITIATOR_ID
    )

    assert result.advanced == []
    mock_repo.confirm_match.assert_called_once_with(match_id, INITIATOR_ID)
    mock_repo.get_tournament.assert_called_once_with(TOURNAMENT_ID)

    # MatchConfirmedEvent emitted
    assert len(result.confirmed) == 1
    assert result.confirmed[0].match_id == match_id
    assert result.confirmed[0].winner_participant_id == opponent_participant_id

    # TournamentCompletedEvent emitted (SE terminal auto-complete)
    assert len(result.completed) == 1
    assert result.completed[0].tournament_id == TOURNAMENT_ID
    assert result.completed[0].winner_participant_id == opponent_participant_id

    # Auto-complete: winner set to sole opponent.
    mock_repo.set_tournament_winner.assert_called_once_with(
        TOURNAMENT_ID,
        winner_team_id=opponent.team_id,
        winner_participant_id=opponent.participant_id,
    )
    mock_repo.set_tournament_status_flush.assert_called_once_with(
        TOURNAMENT_ID, TournamentStatus.COMPLETED
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_terminal_rr_no_auto_complete(mock_repo):
    """Terminal RR match with initiator: confirm_match called but NO
    auto-complete (RR mode is not eligible)."""
    participant_id = TournamentParticipantID(generate_uuid())
    opponent_participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=None)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )
    opponent = _create_contestant(
        match_id=match_id, participant_id=opponent_participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.return_value = [opponent]

    # RR mode → auto-complete should NOT trigger.
    mock_tournament = _create_tournament(game_format=GameFormat.ONE_V_ONE, elimination_mode=EliminationMode.ROUND_ROBIN)
    mock_repo.get_tournament.return_value = mock_tournament

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id, initiator_id=INITIATOR_ID
    )

    assert result.advanced == []
    mock_repo.confirm_match.assert_called_once_with(match_id, INITIATOR_ID)

    # MatchConfirmedEvent emitted
    assert len(result.confirmed) == 1
    assert result.confirmed[0].match_id == match_id
    # RR → no TournamentCompletedEvent
    assert result.completed == []
    mock_repo.set_tournament_winner.assert_not_called()
    mock_repo.set_tournament_status_flush.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_both_removed_terminal_no_confirm(mock_repo):
    """Terminal match, both contestants removed (len==0): no confirm,
    no events — same behavior as non-terminal."""
    participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())

    match = _create_match(match_id=match_id, next_match_id=None)
    contestant = _create_contestant(
        match_id=match_id, participant_id=participant_id
    )

    mock_repo.find_contestant_entries_for_participant_in_tournament.return_value = [
        (contestant, match)
    ]
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id, initiator_id=INITIATOR_ID
    )

    assert result.advanced == []
    assert result.confirmed == []
    assert result.completed == []
    mock_repo.create_match_contestant.assert_not_called()
    mock_repo.confirm_match.assert_not_called()


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_match(
    *,
    match_id: TournamentMatchID | None = None,
    next_match_id: TournamentMatchID | None = None,
) -> TournamentMatch:
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=next_match_id,
        confirmed_by=None,
        created_at=NOW,
    )


def _create_contestant(
    *,
    match_id: TournamentMatchID | None = None,
    participant_id: TournamentParticipantID | None = None,
    team_id: TournamentTeamID | None = None,
) -> TournamentMatchToContestant:
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )


def _create_tournament(
    *,
    game_format: GameFormat = GameFormat.ONE_V_ONE,
    elimination_mode: EliminationMode = EliminationMode.SINGLE_ELIMINATION,
) -> Tournament:
    return Tournament(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID,
        name='Test Tournament',
        game=None,
        description=None,
        image_url=None,
        ruleset=None,
        start_time=None,
        created_at=NOW,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        contestant_type=None,
        tournament_status=TournamentStatus.ONGOING,
        game_format=game_format,
        elimination_mode=elimination_mode,
    )
