from byceps.services.user.models.user import UserID

from .models.tournament_participant import TournamentParticipant, TournamentParticipantId
from .models.tournament_team import TournamentTeam, TournamentTeamId
from .models.tournament import TournamentId


def create_participant(
    user_id: UserID,
    tournament_id: TournamentId,
    *,
    substitute_player: bool,
    team_id: TournamentTeamId | None = None,
) -> TournamentParticipant:
    raise NotImplementedError


def update_participant(
    participant_id: TournamentParticipantId,
    *,
    substitute_player: bool,
    team_id: TournamentTeamId | None,
) -> None:
    raise NotImplementedError


def delete_participant(participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError


def get_participant(participant_id: TournamentParticipantId) -> TournamentParticipant:
    raise NotImplementedError


def get_participants_for_tournament(tournament_id: TournamentId) -> list[TournamentParticipant]:
    raise NotImplementedError


def create_team(
    *,
    tag: str | None,
    description: str | None,
    image_url: str | None,
) -> TournamentTeam:
    raise NotImplementedError


def update_team(
    team_id: TournamentTeamId,
    *,
    tag: str | None,
    description: str | None,
    image_url: str | None,
) -> None:
    raise NotImplementedError


def delete_team(team_id: TournamentTeamId) -> None:
    raise NotImplementedError


def get_team(team_id: TournamentTeamId) -> TournamentTeam:
    raise NotImplementedError


def get_teams_for_tournament(tournament_id: TournamentId) -> list[TournamentTeam]:
    raise NotImplementedError


def join_team(participant_id: TournamentParticipantId, team_id: TournamentTeamId) -> None:
    raise NotImplementedError


def leave_team(participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError
