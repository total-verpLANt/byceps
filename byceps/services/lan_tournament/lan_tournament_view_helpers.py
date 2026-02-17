from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.user import user_service
from byceps.services.user.models.user import User


def build_contestant_name_lookups(
    tournament_id: TournamentID,
    contestants_list: list[list[TournamentMatchToContestant]],
) -> tuple[
    dict[TournamentTeamID, TournamentTeam],
    dict[TournamentParticipantID, User],
]:
    """Build lookup dicts to resolve contestant IDs to names.

    Returns (teams_by_id, participants_by_id) where
    participants_by_id maps participant_id to User.
    """
    team_ids: set[TournamentTeamID] = set()
    participant_ids: set[TournamentParticipantID] = set()
    for contestants in contestants_list:
        for c in contestants:
            if c.team_id:
                team_ids.add(c.team_id)
            if c.participant_id:
                participant_ids.add(c.participant_id)

    teams_by_id: dict[TournamentTeamID, TournamentTeam] = {}
    if team_ids:
        teams = tournament_team_service.get_teams_by_ids(team_ids)
        teams_by_id = {t.id: t for t in teams}

    participants_by_id: dict[TournamentParticipantID, User] = {}
    if participant_ids:
        participants = (
            tournament_participant_service.get_participants_for_tournament(
                tournament_id
            )
        )
        user_ids = {p.user_id for p in participants if p.id in participant_ids}
        users_by_id = user_service.get_users_indexed_by_id(user_ids)
        participants_by_id = {
            p.id: users_by_id[p.user_id]
            for p in participants
            if p.id in participant_ids and p.user_id in users_by_id
        }

    return teams_by_id, participants_by_id
