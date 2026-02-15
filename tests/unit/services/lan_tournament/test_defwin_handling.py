from datetime import UTC, datetime
from unittest.mock import patch

from byceps.services.lan_tournament import tournament_match_service
from byceps.services.lan_tournament.models.tournament import TournamentID
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
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')


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

    events = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert events == []
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

    events = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert len(events) == 1
    assert events[0].tournament_id == TOURNAMENT_ID
    assert events[0].match_id == next_match_id
    assert events[0].from_match_id == match_id
    assert events[0].advanced_participant_id == opponent_participant_id

    # Contestant was deleted from the original match
    mock_repo.delete_contestant_from_match.assert_called_once_with(
        match_id, participant_id=participant_id
    )
    # Opponent was advanced to next match
    mock_repo.create_match_contestant.assert_called_once()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_defwin_participant_no_next_match_no_advance(mock_repo):
    """When removed participant is in a final match (no next_match_id),
    no advancement occurs."""
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

    events = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert events == []
    mock_repo.create_match_contestant.assert_not_called()


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

    events = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert events == []
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

    events = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert events == []
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

    events = tournament_match_service.handle_defwin_for_removed_participant(
        TOURNAMENT_ID, participant_id
    )

    assert len(events) == 2
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

    events = tournament_match_service.handle_defwin_for_removed_team(
        TOURNAMENT_ID, team_id
    )

    assert events == []
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

    events = tournament_match_service.handle_defwin_for_removed_team(
        TOURNAMENT_ID, team_id
    )

    assert len(events) == 1
    assert events[0].advanced_team_id == opponent_team_id
    assert events[0].match_id == next_match_id
    mock_repo.create_match_contestant.assert_called_once()


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
