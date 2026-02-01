from byceps.services.party.models import PartyID

from .models.tournament import Tournament, TournamentId
from .models.tournament_participant import TournamentParticipantId
from .models.tournament_team import TournamentTeamId


def create_tournament(newTournament:Tournament) -> Tournament:
    raise NotImplementedError


def update_tournament(modifiedTournament:Tournament):
    raise NotImplementedError


def delete_tournament(tournament_id: TournamentId) -> None:
    raise NotImplementedError


def get_tournament(tournament_id: TournamentId) -> Tournament:
    raise NotImplementedError


def get_tournaments_for_party(party_id: PartyID) -> list[Tournament]:
    raise NotImplementedError


def get_participant_count(tournament_id: TournamentId) -> int:
    raise NotImplementedError


def join_tournament(tournament_id: TournamentId, participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError


def join_tournament_team(tournament_id: TournamentId, team_id: TournamentTeamId, participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError


def leave_tournament_team(tournament_id: TournamentId, participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError


def leave_tournament(tournament_id: TournamentId, participant_id: TournamentParticipantId) -> None:
    raise NotImplementedError