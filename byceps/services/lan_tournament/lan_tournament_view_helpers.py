from collections.abc import Callable

from byceps.services.lan_tournament import (
    tournament_participant_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.round_robin_standing import (
    RoundRobinStanding,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    compute_round_robin_standings,
)
from byceps.services.party.models import PartyID
from byceps.services.user import user_service
from byceps.services.user.models import User, UserID


def build_contestant_name_lookups(
    tournament_id: TournamentID,
    contestants_list: list[list[TournamentMatchToContestant]],
    *,
    participants: list[TournamentParticipant] | None = None,
) -> tuple[
    dict[TournamentTeamID, TournamentTeam],
    dict[TournamentParticipantID, User],
]:
    """Build lookup dicts to resolve contestant IDs to names.

    Returns (teams_by_id, participants_by_id) where
    participants_by_id maps participant_id to User.

    Pass *participants* to reuse an already-fetched list and avoid an
    extra DB round-trip.
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
        if participants is None:
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
    *,
    participants: list[TournamentParticipant] | None = None,
) -> tuple[
    dict[UserID, str], dict[TournamentTeamID, list[tuple[str, str | None]]]
]:
    """Build seat and team-members lookups for hover card rendering.

    Returns (seats_by_user_id, team_members_by_team_id).

    For team tournaments, fetches all team members and their seats.
    For individual tournaments, builds seat lookup from existing
    participants_by_id and returns an empty team_members dict.

    Pass *participants* to reuse an already-fetched list and avoid an
    extra DB round-trip (deduplicates the fetch shared with
    ``build_contestant_name_lookups``).
    """
    if tournament.contestant_type == ContestantType.TEAM:
        if participants is None:
            all_participants = (
                tournament_participant_service.get_participants_for_tournament(
                    tournament.id
                )
            )
        else:
            all_participants = participants
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


def build_round_robin_standings(
    match_data: list[dict],
) -> list[RoundRobinStanding]:
    """Compute round-robin standings from pre-loaded match data.

    Filters to confirmed matches, extracts contestant pairs,
    then delegates to the pure domain function.
    """
    confirmed_pairs: list[list[TournamentMatchToContestant]] = []
    for data in match_data:
        if data['match'].confirmed_by is None:
            continue
        confirmed_pairs.append(data['contestants'])

    return compute_round_robin_standings(confirmed_pairs)


def _resolve_contestant_name(
    contestant: TournamentMatchToContestant,
    teams_by_id: dict[TournamentTeamID, TournamentTeam],
    participants_by_id: dict[TournamentParticipantID, User],
) -> str:
    """Resolve a contestant to a display name."""
    if contestant.team_id and contestant.team_id in teams_by_id:
        return teams_by_id[contestant.team_id].name
    if contestant.participant_id and contestant.participant_id in participants_by_id:
        user = participants_by_id[contestant.participant_id]
        return user.screen_name or str(user.id)
    return 'TBD'


def compute_feed_counts(match_data: list[dict]) -> dict[str, int]:
    """Count incoming feeds per match from next_match_id/loser_next_match_id.

    Returns a mapping of ``str(match_id) -> count`` so callers can tell
    how many feeder matches route into a given match.  A count of 0 (or
    absent key) means the match has no incoming feeds — it sits at the
    leaf of the bracket tree.
    """
    feed_counts: dict[str, int] = {}
    for data in match_data:
        m = data['match']
        if m.next_match_id:
            key = str(m.next_match_id)
            feed_counts[key] = feed_counts.get(key, 0) + 1
        if m.loser_next_match_id:
            key = str(m.loser_next_match_id)
            feed_counts[key] = feed_counts.get(key, 0) + 1
    return feed_counts


def serialize_bracket_json(
    tournament: Tournament,
    match_data: list[dict],
    teams_by_id: dict[TournamentTeamID, TournamentTeam],
    participants_by_id: dict[TournamentParticipantID, User],
    seats_by_user_id: dict[UserID, str],
    team_members_by_team_id: dict[TournamentTeamID, list[tuple[str, str | None]]],
    *,
    url_builder: Callable[[TournamentMatch], str] | None = None,
) -> dict:
    """Serialize bracket data to a JSON-safe dict for client-side rendering."""
    # Compute incoming feed counts from the match graph so the
    # client can identify dead matches (0 feeds) without
    # recomputing the routing topology from next/loser links.
    feed_counts = compute_feed_counts(match_data)

    return {
        'tournament': {
            'id': str(tournament.id),
            'name': tournament.name,
            'mode': tournament.tournament_mode.name if tournament.tournament_mode else None,
            'contestant_type': tournament.contestant_type.name if tournament.contestant_type else 'SOLO',
            'status': tournament.tournament_status.name if tournament.tournament_status else None,
        },
        'matches': [
            {
                'id': str(match.id),
                'round': match.round,
                'match_order': match.match_order,
                'bracket': match.bracket.value if match.bracket else None,
                'next_match_id': str(match.next_match_id) if match.next_match_id else None,
                'loser_next_match_id': str(match.loser_next_match_id) if match.loser_next_match_id else None,
                'confirmed': match.confirmed_by is not None,
                'incoming_feed_count': feed_counts.get(str(match.id), 0),
                'contestants': [
                    {
                        'name': _resolve_contestant_name(c, teams_by_id, participants_by_id),
                        'score': c.score,
                        'team_id': str(c.team_id) if c.team_id else None,
                        'participant_id': str(c.participant_id) if c.participant_id else None,
                    }
                    for c in contestants
                ],
            }
            for data in match_data
            for match, contestants in [(data['match'], data['contestants'])]
        ],
        'match_urls': {
            str(data['match'].id): url_builder(data['match']) if url_builder else None
            for data in match_data
        },
        'hover_data': {
            'seats': {
                str(pid): seats_by_user_id[user.id]
                for pid, user in participants_by_id.items()
                if user.id in seats_by_user_id
            },
            'team_members': {
                str(tid): members
                for tid, members in team_members_by_team_id.items()
            },
        },
    }
