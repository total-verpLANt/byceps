from typing import BinaryIO

from byceps.services.lan_tournament.models.tournament_team import TournamentTeamId


def set_team_image(team_id: TournamentTeamId, stream: BinaryIO) -> None:
    raise NotImplementedError
