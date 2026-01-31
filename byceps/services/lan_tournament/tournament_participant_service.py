from .models.tournament_participant import TournamentParticipant, TournamentParticipantId
from .models.tournament_team import TournamentTeam, TournamentTeamId
from .models.tournament import TournamentId


def create_participant(participant: TournamentParticipant) -> TournamentParticipant:
    raise NotImplementedError


def update_participant(participant: TournamentParticipant) -> None:
    raise NotImplementedError


def delete_participant(participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError


def get_participant(participant_id: TournamentParticipantId) -> TournamentParticipant:
    raise NotImplementedError


def get_participants_for_tournament(tournament_id: TournamentId) -> list[TournamentParticipant]:
    raise NotImplementedError


def create_team(team: TournamentTeam) -> TournamentTeam:
    raise NotImplementedError


def update_team(team: TournamentTeam) -> None:
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
