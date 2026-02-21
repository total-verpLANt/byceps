from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.contestant_type import ContestantType
from byceps.services.lan_tournament.models.tournament import Tournament, TournamentID
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
from byceps.services.party.models import PartyID
from byceps.services.user import user_service
from byceps.services.user.models.user import User, UserID


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


def build_seat_lookup(
    user_ids: set[UserID],
    party_id: PartyID,
) -> dict[UserID, str]:
    """Map user IDs to their seat labels for the party."""
    return tournament_participant_service.get_seats_for_users(
        user_ids, party_id
    )


def build_team_members_lookup(
    participants: list,
    team_ids: set[TournamentTeamID],
    users_by_id: dict[UserID, User],
    seats_by_user_id: dict[UserID, str],
) -> dict[TournamentTeamID, list[tuple[str, str | None]]]:
    """Map team IDs to [(screen_name, seat_label|None), ...].

    Used by hover cards to show team composition with seats.
    """
    result: dict[TournamentTeamID, list[tuple[str, str | None]]] = {}
    for p in participants:
        if p.team_id not in team_ids:
            continue
        if p.removed_at is not None:
            continue
        user = users_by_id.get(p.user_id)
        if user is None or user.screen_name is None:
            continue
        seat = seats_by_user_id.get(p.user_id)
        result.setdefault(p.team_id, []).append((user.screen_name, seat))
    return result


def build_hover_lookups(
    tournament: Tournament,
    participants_by_id: dict[TournamentParticipantID, User],
    teams_by_id: dict[TournamentTeamID, TournamentTeam],
    party_id: PartyID,
) -> tuple[dict[UserID, str], dict[TournamentTeamID, list[tuple[str, str | None]]]]:
    """Build seat and team-members lookups for hover card rendering.

    Returns (seats_by_user_id, team_members_by_team_id).

    For team tournaments, fetches all team members and their seats.
    For individual tournaments, builds seat lookup from existing
    participants_by_id and returns an empty team_members dict.
    """
    if tournament.contestant_type == ContestantType.TEAM:
        all_participants = (
            tournament_participant_service.get_participants_for_tournament(
                tournament.id
            )
        )
        all_member_user_ids = {
            p.user_id for p in all_participants if p.removed_at is None
        }
        all_users_by_id = user_service.get_users_indexed_by_id(
            all_member_user_ids
        )
        seats_by_user_id = build_seat_lookup(all_member_user_ids, party_id)
        team_members_by_team_id = build_team_members_lookup(
            all_participants,
            set(teams_by_id.keys()),
            all_users_by_id,
            seats_by_user_id,
        )
    else:
        all_user_ids = {u.id for u in participants_by_id.values()}
        seats_by_user_id = build_seat_lookup(all_user_ids, party_id)
        team_members_by_team_id = {}

    return seats_by_user_id, team_members_by_team_id
