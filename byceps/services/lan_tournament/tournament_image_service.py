from typing import BinaryIO

from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)


def set_team_image(team_id: TournamentTeamID, stream: BinaryIO) -> None:
    raise NotImplementedError
