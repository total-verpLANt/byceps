from collections.abc import Callable
from dataclasses import dataclass

from flask_babel import gettext

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
    CorrectionCase,
    TournamentMatch,
    TournamentMatchID,
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
    compute_ffa_cumulative_standings,
    compute_ffa_round_standings,
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


def build_ffa_standings(
    match_data: list[dict],
) -> list[dict]:
    """Compute FFA cumulative standings from pre-loaded match data.

    Groups matches by round, delegates to the domain functions, then
    returns a list of dicts suitable for Jinja2 template rendering:

        {
            'contestant_id': str,
            'total_points': int,
            'rounds_played': int,
            'avg_points': float,
            'per_round': list[int],   # points per round, 0 if absent
        }

    Matches that are not confirmed are excluded.  An empty list is
    returned when no confirmed FFA matches exist.
    """
    # Group confirmed match contestants by round number.
    rounds_map: dict[int, list[list[TournamentMatchToContestant]]] = {}
    for data in match_data:
        if data['match'].confirmed_by is None:
            continue
        round_num = data['match'].round or 0
        rounds_map.setdefault(round_num, []).append(data['contestants'])

    if not rounds_map:
        return []

    # Sort round numbers so per_round ordering is deterministic.
    sorted_rounds = sorted(rounds_map.keys())

    # Build the nested structure expected by the domain function.
    all_round_matches = [rounds_map[r] for r in sorted_rounds]

    # Cumulative standings (sorted by total_points desc).
    cumulative = compute_ffa_cumulative_standings(all_round_matches)

    # Per-round standings for the breakdown columns.
    per_round_maps: list[dict[str, int]] = []
    for r in sorted_rounds:
        round_standings = compute_ffa_round_standings(rounds_map[r])
        per_round_maps.append(dict(round_standings))

    # Build template-friendly list.
    result: list[dict] = []
    for cid, total_pts in cumulative:
        per_round = [prm.get(cid, 0) for prm in per_round_maps]
        rounds_played = sum(1 for prm in per_round_maps if cid in prm)
        avg = total_pts / rounds_played if rounds_played > 0 else 0.0
        result.append({
            'contestant_id': cid,
            'total_points': total_pts,
            'rounds_played': rounds_played,
            'avg_points': round(avg, 1),
            'per_round': per_round,
        })

    return result


@dataclass(frozen=True, kw_only=True)
class DownstreamImpact:
    """One affected downstream match, as the correction panel shows it.

    The panel used to list nothing but the bracket shorthand
    (``LB R3 M2``), which forced an admin to open every affected
    match in another tab before daring to correct anything. This
    carries the whole row: what the shorthand means, where the match
    stands right now, and what correcting the subject match does to
    it.
    """

    match: TournamentMatch
    label: str
    status: str
    status_label: str
    impact_label: str
    destructive: bool
    contestants: list[TournamentMatchToContestant]
    open_slots: int


def build_match_label(match: TournamentMatch) -> str:
    """Spell out a match's bracket position in words.

    The shorthand (``WB R2 M1``) stays on screen next to this -- it
    is what organisers call across the room -- but it is not
    self-explanatory to everyone who may be sitting at the admin
    machine.
    """
    round_number = (match.round or 0) + 1
    match_number = (match.match_order or 0) + 1
    bracket = match.bracket.value if match.bracket else None

    if bracket == 'GF':
        return gettext('Grand final, match %(match)d', match=match_number)

    if bracket == 'P3':
        return gettext('Third-place match')

    if bracket == 'WB':
        return gettext(
            'Winners bracket, round %(round)d, match %(match)d',
            round=round_number,
            match=match_number,
        )

    if bracket == 'LB':
        return gettext(
            'Losers bracket, round %(round)d, match %(match)d',
            round=round_number,
            match=match_number,
        )

    return gettext(
        'Round %(round)d, match %(match)d',
        round=round_number,
        match=match_number,
    )


def _classify_match_state(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
) -> tuple[str, str]:
    """Return (status, translated status label) for a match.

    Mirrors the status column of the match list so one match does not
    read differently in two places.
    """
    real = [c for c in contestants if c.team_id or c.participant_id]

    if match.confirmed_by and len(real) < 2:
        return 'defwin', gettext('DEFWIN')

    if match.confirmed_by:
        return 'confirmed', gettext('Confirmed')

    if any(c.score is not None for c in real):
        return 'reported', gettext('Awaiting confirmation')

    return 'pending', gettext('Pending')


def build_downstream_impact(
    matches: list[TournamentMatch],
    contestants_by_match_id: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ],
    correction_case: CorrectionCase | None,
) -> list[DownstreamImpact]:
    """Describe what a correction does to each affected match.

    The consequence is per match, not per panel: in the
    CONFIRMED_DOWNSTREAM case only the already-confirmed matches lose
    a result, while the rest merely lose the contestant that advanced
    into them.
    """
    rows: list[DownstreamImpact] = []

    for match in matches:
        contestants = contestants_by_match_id.get(match.id, [])
        status, status_label = _classify_match_state(match, contestants)

        # Destructive means a result disappears. Losing the
        # contestant that advanced into a match that was never played
        # is routine; losing an entered, confirmed result is not, and
        # the panel must not weigh the two the same.
        if correction_case is CorrectionCase.BRACKET_RESET_DELETION:
            impact_label = gettext('Will be deleted')
            destructive = True
        elif status == 'defwin':
            # A defwin holds no score, so "the result will be
            # retracted" reads as a contradiction next to its own
            # em-dash. What it loses is the free advancement.
            impact_label = gettext('Defwin will be retracted')
            destructive = True
        elif status == 'confirmed':
            impact_label = gettext('Result will be retracted')
            destructive = True
        else:
            impact_label = gettext('Contestant will be removed')
            destructive = False

        real_count = len(
            [c for c in contestants if c.team_id or c.participant_id]
        )
        open_slots = (
            max(0, 2 - real_count) if not match.confirmed_by else 0
        )

        rows.append(
            DownstreamImpact(
                match=match,
                label=build_match_label(match),
                status=status,
                status_label=status_label,
                impact_label=impact_label,
                destructive=destructive,
                contestants=contestants,
                open_slots=open_slots,
            )
        )

    return rows


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
            'game_format': tournament.game_format.name if tournament.game_format else None,
            'elimination_mode': tournament.elimination_mode.name if tournament.elimination_mode else None,
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
        # Pre-translated strings for the client-side bracket renderer.
        # The JS reads these via _t(key, fallback) — every key has an
        # English fallback so the bracket works even without this dict.
        'strings': {
            # Status labels
            'statusDone': gettext('Done'),
            'statusDefwin': gettext('DEFWIN'),
            'statusOpen': gettext('Open'),
            'statusPending': gettext('Pending'),
            # Round / match labels
            'round': gettext('Round'),
            'matchSingular': gettext('Match'),
            'matchPlural': gettext('Matches'),
            'grandFinal': gettext('Grand Final'),
            'bracketReset': gettext('Bracket Reset'),
            'thirdPlace': gettext('3rd Place'),
            # Section headers
            'winnersBracket': gettext('Winners Bracket (WB)'),
            'bracket': gettext('Bracket'),
            'losersBracket': gettext('Losers Bracket (LB)'),
            'additionalMatches': gettext('Additional Matches'),
            'thirdPlaceMatch': gettext('3rd Place Match'),
            # Section subtitles
            'wbWinnerVsLbWinner': gettext('WB Winner vs LB Winner'),
            'grandFinalDecisive': gettext('Grand Final \u2013 Decisive Game'),
            'slotField': gettext('-slot field'),
            'optionalResetGame': gettext('Optional reset game and additional matches'),
            'semifinalLosersCompete': gettext('Semifinal losers compete for third place'),
            'playedOnlyIfLbWinner': gettext('Played only if the losers-bracket winner takes Grand Final Game 1.'),
            'trueFinal': gettext('True final: both players enter 1\u20131 in the series'),
            'sourceLabelsCrossBracket': gettext('Source labels on cross-bracket entries with jump navigation'),
            # Stats bar
            'sectionStatistics': gettext('Section statistics'),
            'totalMatches': gettext('Total matches'),
            'matchCountSingular': gettext('match'),
            'matchCountPlural': gettext('matches'),
            'completionPercentage': gettext('Completion percentage'),
            'pctComplete': gettext('complete'),
            'uniqueContestants': gettext('Unique contestants'),
            'contestantSingular': gettext('contestant'),
            'contestantPlural': gettext('contestants'),
            # Source labels / references
            'winner': gettext('Winner'),
            'loser': gettext('Loser'),
            'lbWinner': gettext('LB Winner'),
            'of': gettext('of'),
            'grandFinalGame': gettext('Grand Final \u2013 Game'),
            'thirdPlaceGame': gettext('3rd Place \u2013 Game'),
            'game': gettext('Game'),
            # Compact abbreviations
            'gfG': gettext('GF G'),
            'thirdM': gettext('3rd M'),
            'rAbbrev': gettext('R'),
            'mAbbrev': gettext('M'),
            # Filter dropdown
            'view': gettext('View'),
            'filterBracketView': gettext('Filter bracket view'),
            'allBrackets': gettext('All brackets'),
            'winnersOnly': gettext('Winners only'),
            'losersOnly': gettext('Losers only'),
            'top8': gettext('Top 8'),
            # Misc
            'secondPlace': gettext('2nd Place'),
            'tbd': gettext('TBD'),
            'status': gettext('Status'),
            'noBracketData': gettext('No bracket data available.'),
            'vs': gettext('vs'),
            'clickToJump': gettext('click to jump to source match'),
            'jumpTo': gettext('jump to'),
            'grandFinalBracketReset': gettext('Grand Final \u2013 Bracket Reset'),
            'loserSfVsLoserSf': gettext('Loser SF 1 vs Loser SF 2'),
            'openMatch': gettext('Open'),
        },
    }
