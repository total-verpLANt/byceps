from typing import List


from byceps.services.party.models import PartyID

from .models.tournament import Tournament, TournamentId
from .models.tournament_mode import TournamentMode
from .models.tournament_participant import TournamentParticipantId
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeamId


def create_tournament(
    party_id: PartyID,
    name: str,
    *,
    description: str | None,
    image_url: str | None,
    ruleset: str | None,
    min_players: int | None,
    max_players: int | None,
    min_teams: int | None,
    max_teams: int | None,
    min_players_in_team: int | None,
    max_players_in_team: int | None,
    tournament_status: TournamentStatus = TournamentStatus.NOT_SET,
    tournament_mode: TournamentMode = TournamentMode.NOT_SET,
) -> Tournament:
    raise NotImplementedError


def update_tournament(
    tournament_id: TournamentId,
    *,
    name: str,
    description: str | None,
    image_url: str | None,
    ruleset: str | None,
    min_players: int | None,
    max_players: int | None,
    min_teams: int | None,
    max_teams: int | None,
    min_players_in_team: int | None,
    max_players_in_team: int | None,
    tournament_status: TournamentStatus,
    tournament_mode: TournamentMode,
) -> None:
    raise NotImplementedError


def delete_tournament(tournament_id: TournamentId) -> None:
    raise NotImplementedError


def get_tournament(tournament_id: TournamentId) -> Tournament:
    raise NotImplementedError


def get_tournaments_for_party(party_id: PartyID) -> List[Tournament]:
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
