from datetime import datetime, UTC
from unittest.mock import patch
from uuid import UUID

from byceps.services.lan_tournament import tournament_participant_service
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
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

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
REPO = (
    'byceps.services.lan_tournament.tournament_participant_service'
    '.tournament_repository'
)


def _participant(participant_id, team_id):
    return TournamentParticipant(
        id=participant_id,
        user_id=generate_uuid(),
        tournament_id=TOURNAMENT_ID,
        substitute_player=False,
        team_id=team_id,
        created_at=NOW,
    )


def _entry(match_id, *, team_id=None, participant_id=None):
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )


def _world():
    team_id = TournamentTeamID(generate_uuid())
    members = [TournamentParticipantID(generate_uuid()) for _ in range(3)]
    own_match = TournamentMatchID(generate_uuid())
    foreign_match = TournamentMatchID(generate_uuid())
    contestants = {
        own_match: [_entry(own_match, team_id=team_id)],
        foreign_match: [
            _entry(foreign_match, team_id=TournamentTeamID(generate_uuid()))
        ],
    }
    return team_id, members, own_match, foreign_match, contestants


@patch(REPO)
def test_str_ids_from_a_url_lock_and_match_like_uuids(mock_repo):
    team_id, members, own_match, foreign_match, contestants = _world()
    mock_repo.get_participants_for_team.return_value = [
        _participant(m, team_id) for m in members
    ]
    mock_repo.get_contestants_for_tournament.return_value = contestants
    mock_repo.get_matches_for_tournament.return_value = [
        type('M', (), {'id': own_match})(),
        type('M', (), {'id': foreign_match})(),
    ]

    affected = tournament_participant_service._lock_roster_matches_flush(
        TOURNAMENT_ID,
        participant_ids=[str(members[0])],
        team_ids=[str(team_id)],
    )

    assert affected == [own_match]
    locked_team_ids = [
        c.args[0] for c in mock_repo.get_team_for_update.call_args_list
    ]
    assert locked_team_ids == [team_id]
    assert all(isinstance(t, UUID) for t in locked_team_ids)
    (locked_member_ids,) = mock_repo.get_participants_for_update.call_args.args
    assert locked_member_ids == sorted(members)
    assert all(isinstance(m, UUID) for m in locked_member_ids)
    mock_repo.get_participants_for_update.assert_called_once()
    mock_repo.get_participant_for_update.assert_not_called()


@patch(REPO)
def test_str_participant_id_alone_matches_its_solo_entry(mock_repo):
    participant_id = TournamentParticipantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    mock_repo.get_contestants_for_tournament.return_value = {
        match_id: [_entry(match_id, participant_id=participant_id)]
    }
    mock_repo.get_matches_for_tournament.return_value = [
        type('M', (), {'id': match_id})()
    ]

    affected = tournament_participant_service._lock_roster_matches_flush(
        TOURNAMENT_ID, participant_ids=[str(participant_id)]
    )

    assert affected == [match_id]
    mock_repo.get_participants_for_update.assert_called_once_with(
        [participant_id]
    )


@patch(REPO)
def test_a_caller_that_locked_every_member_skips_the_member_lock(mock_repo):
    mock_repo.get_contestants_for_tournament.return_value = {}
    mock_repo.get_matches_for_tournament.return_value = []

    tournament_participant_service._lock_roster_matches_flush(
        TOURNAMENT_ID,
        participant_ids=[TournamentParticipantID(generate_uuid())],
        members_locked=True,
    )

    mock_repo.get_participants_for_update.assert_not_called()
