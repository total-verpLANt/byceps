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

# TODO: Do the converter like in the rest of byceps
# def _db_entity_to_tournament(db_tournament: DbTournament) -> Tournament:
#     """Convert database entity to domain model."""
#     return Tournament(
#         id=db_tournament.id,
#         party_id=db_tournament.party_id,
#         name=db_tournament.name,
#         description=db_tournament.description,
#         image_url=db_tournament.image_url,
#         ruleset=db_tournament.ruleset,
#         min_players=db_tournament.min_players,
#         max_players=db_tournament.max_players,
#         min_teams=db_tournament.min_teams,
#         max_teams=db_tournament.max_teams,
#         min_players_in_team=db_tournament.min_players_in_team,
#         max_players_in_team=db_tournament.max_players_in_team,
#         tournament_status=db_tournament.tournament_status,
#         tournament_mode=db_tournament.tournament_mode,
#     )
