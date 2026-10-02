from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, UTC
import re
from typing import Any, TypeGuard
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from babel.dates import format_skeleton, get_timezone_name
from flask import current_app
from flask_babel import (
    format_decimal,
    format_time,
    get_locale,
    gettext,
    ngettext,
    to_user_timezone,
)
from wtforms import Form

from byceps.services.lan_tournament import (
    seed_code,
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_domain_service as qualification_domain,
    tournament_repository,
    tournament_seeding_domain_service as seeding_domain,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
    VALID_COMBINATIONS,
)
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.models.round_robin_standing import (
    RoundRobinStanding,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
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
from byceps.services.lan_tournament.models.tournament_image import (
    TournamentImage,
)
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    _standard_seed_order,
    MAX_LOBBY_SIZE,
    MAX_PLAYOFF_GROUP_COUNT,
    MAX_POINT_TABLE_PLACES,
    MAX_POINTS_PER_PLACE,
    compute_ffa_cumulative_standings,
    compute_ffa_round_standings,
    compute_round_robin_standings,
    elimination_mode_for_phase,
    game_format_for_phase,
)
from byceps.services.lan_tournament.tournament_request_domain_service import (
    MAX_PARTICIPANT_LIMIT,
)
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
)
from byceps.services.lan_tournament import (
    tournament_qualification_service,
    tournament_score_service,
    tournament_seeding_service,
)
from byceps.services.lan_tournament.tournament_qualification_service import (
    FfaCutTie,
    FfaOutdatedDecision,
    QualificationState,
)
from byceps.services.lan_tournament.tournament_seeding_service import (
    SeedingBoard,
)
from byceps.services.party.models import Party, PartyID
from byceps.services.user import user_service
from byceps.services.user.models import User, UserID
from byceps.util.result import Err, Ok, Result


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
    """One affected downstream match, as the correction panel shows it."""

    match: TournamentMatch
    label: str
    status: str
    status_label: str
    impact_label: str
    destructive: bool
    contestants: list[TournamentMatchToContestant]
    open_slots: int


def build_match_label(match: TournamentMatch) -> str:
    """Spell out a match's bracket position in words."""
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
    """Return the status and its translated label for a match."""
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
    """Describe what a correction does to each affected match."""
    rows: list[DownstreamImpact] = []

    for match in matches:
        contestants = contestants_by_match_id.get(match.id, [])
        status, status_label = _classify_match_state(match, contestants)

        # Destructive means a result is lost, not just a contestant.
        if correction_case is CorrectionCase.BRACKET_RESET_DELETION:
            impact_label = gettext('Will be deleted')
            destructive = True
        elif status == 'defwin':
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
    origin_labels: Mapping[str, str] | None = None,
) -> dict:
    """Serialize bracket data to a JSON-safe dict for client-side rendering.

    The payload is public: it carries the phase of a match and, with
    `origin_labels` (contestant ID to `A1`), where a contestant qualified
    from, and never a seed, a tier, a code or a decision reason.
    """
    # Compute incoming feed counts from the match graph so the
    # client can identify dead matches (0 feeds) without
    # recomputing the routing topology from next/loser links.
    feed_counts = compute_feed_counts(match_data)
    origin_labels = origin_labels or {}

    # A playoff bracket is drawn in the format of the playoff phase.
    playoff_view = tournament.has_playoffs and any(
        data['match'].phase == 2 for data in match_data
    )
    game_format = (
        tournament.playoff_game_format if playoff_view else tournament.game_format
    )
    elimination_mode = (
        tournament.playoff_elimination_mode
        if playoff_view
        else tournament.elimination_mode
    )

    def origin_of(contestant) -> str | None:
        key = contestant.participant_id or contestant.team_id
        return origin_labels.get(str(key)) if key else None

    return {
        'tournament': {
            'id': str(tournament.id),
            'name': tournament.name,
            'game_format': game_format.name if game_format else None,
            'elimination_mode': elimination_mode.name if elimination_mode else None,
            'contestant_type': tournament.contestant_type.name if tournament.contestant_type else 'SOLO',
            'status': tournament.tournament_status.name if tournament.tournament_status else None,
        },
        'matches': [
            {
                'id': str(match.id),
                'round': match.round,
                'match_order': match.match_order,
                'bracket': match.bracket.value if match.bracket else None,
                'phase': match.phase,
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
                        'origin': origin_of(c),
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
            'final': gettext('Final'),
            'semifinal': gettext('Semifinal'),
            'quarterfinal': gettext('Quarterfinal'),
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


def parse_submitted_contestant_scores(
    contestants: list[TournamentMatchToContestant],
    tournament: Tournament,
    form: Mapping[str, str],
    *,
    field_prefix: str,
    allow_all_blank: bool,
) -> Result[dict[TournamentParticipantID | TournamentTeamID, int], str]:
    """Parse the submitted score of each real contestant of a match.

    Scores are bound by contestant key, as the order of contestants is
    not stable. With `allow_all_blank`, leaving every field empty is
    accepted; a partial fill is always rejected.
    """
    scores: dict[TournamentParticipantID | TournamentTeamID, int] = {}
    num_real = 0
    num_blank = 0

    for contestant in contestants:
        key = contestant.team_id or contestant.participant_id
        if key is None:
            continue  # DEFWIN slot

        num_real += 1
        raw = form.get(f'{field_prefix}{key}', '').strip()

        if not raw:
            if not allow_all_blank:
                return Err(gettext('All contestants must have scores.'))
            num_blank += 1
            continue

        try:
            score_int = int(raw)
        except ValueError:
            return Err(gettext('Invalid score value.'))

        if tournament.contestant_type == ContestantType.TEAM:
            scores[TournamentTeamID(key)] = score_int
        else:
            scores[TournamentParticipantID(key)] = score_int

    if allow_all_blank and num_blank not in (0, num_real):
        return Err(
            gettext(
                'Enter a score for every contestant, or leave them all empty.'
            )
        )

    return Ok(scores)


def ffa_phase(tournament: Tournament) -> int | None:
    """Return the phase that runs free-for-all lobbies, or `None`."""
    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        return 1
    if (
        tournament.has_playoffs is True
        and tournament.game_format == GameFormat.HIGHSCORE
        and tournament.playoff_game_format == GameFormat.FREE_FOR_ALL
    ):
        return 2
    return None


def ffa_elimination_mode(tournament: Tournament) -> EliminationMode | None:
    """Return the elimination mode of the phase that runs the lobbies."""
    phase = ffa_phase(tournament)
    if phase is None:
        return None
    return elimination_mode_for_phase(tournament, phase)


def match_uses_placements(
    tournament: Tournament, match: TournamentMatch
) -> bool:
    """Return `True` if the match is decided by placements, by its phase."""
    game_format = tournament.game_format
    if tournament.has_playoffs is True:
        game_format = game_format_for_phase(tournament, match.phase)
    return isinstance(game_format, GameFormat) and game_format.uses_placements


def ffa_grand_final_refusal(tournament: Tournament) -> str | None:
    """Return the msgid refusing a grand final for this format, or `None`."""
    if ffa_phase(tournament) is None:
        return 'This tournament is not a Free-for-All format.'
    mode = ffa_elimination_mode(tournament)
    if mode is not EliminationMode.DOUBLE_ELIMINATION:
        return 'Grand Final is only for double elimination tournaments.'
    return None


def ffa_grand_final_offer(tournament: Tournament) -> dict[str, Any] | None:
    """Return what the orga panel offers for the grand final, or `None`.

    `None` unless the lobbies run double elimination, the tournament is
    ongoing and the FFA phase has matches. Reads only.
    """
    if ffa_grand_final_refusal(tournament) is not None:
        return None
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return None

    phase = ffa_phase(tournament)
    matches = [
        m
        for m in tournament_match_service.get_matches_for_tournament_ordered(
            tournament.id
        )
        if m.phase == phase
    ]
    if not matches:
        return None

    for match in matches:
        if match.bracket is Bracket.GRAND_FINAL:
            return {
                'state': 'exists',
                'match_id': match.id,
                'count': None,
                'reason': None,
            }

    match tournament_match_service.ffa_grand_final_gate(tournament.id):
        case Ok(count):
            return {
                'state': 'ready',
                'match_id': None,
                'count': count,
                'reason': None,
            }
        case Err(error_message):
            return {
                'state': 'pending',
                'match_id': None,
                'count': None,
                'reason': gettext(error_message),
            }


def plain_round_robin_winner_tie(tournament: Tournament) -> str | None:
    """Return `'open'` or `'decided'` for a tie for the win of a plain
    round robin, else `None`. Reads only.
    """
    if not tournament_match_service.is_plain_round_robin(tournament):
        return None

    match tournament_qualification_service.get_qualification(tournament.id):
        case Ok(state):
            winner = qualification_domain.TieKind.WINNER
            if state.open_match_count == 0 and any(
                b.kind is winner for b in state.blockers
            ):
                return 'open'
            if any(
                tie.decided and tie.kind is winner
                for ranking in state.rankings
                for tie in ranking.ties
            ):
                return 'decided'
            return None
        case _:
            return None


def is_walkover_match(contestants: list[TournamentMatchToContestant]) -> bool:
    """Return `True` if fewer than two real contestants are in the match."""
    real_contestants = [
        c
        for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]
    return len(real_contestants) < 2


def parse_match_ids(raw: str) -> list[TournamentMatchID]:
    """Return the comma-separated match IDs, or `[]` if one is malformed."""
    try:
        return [
            TournamentMatchID(UUID(part)) for part in raw.split(',') if part
        ]
    except ValueError:
        return []


def parse_submitted_ffa_placements(
    form: Mapping[str, str],
) -> Result[dict[str, int], str]:
    """Parse the submitted `placement_<contestant ID>` fields."""
    placements: dict[str, int] = {}

    for key, value in form.items():
        if not key.startswith('placement_'):
            continue

        try:
            placements[key.removeprefix('placement_')] = int(value)
        except ValueError:
            return Err(gettext('Invalid placement value for contestant.'))

    if not placements:
        return Err(gettext('No placement data submitted.'))

    return Ok(placements)


CREATE_WIZARD_STEP_FIELDS: tuple[tuple[str, ...], ...] = (
    (
        'name',
        'game',
        'start_time',
        'description',
        'ruleset',
        'image',
        'image_id',
        'image_url',
        'image_alt_text',
    ),
    ('contestant_type', 'game_format', 'elimination_mode'),
    (
        'req_map',
        'min_players',
        'max_players',
        'min_teams',
        'max_teams',
        'min_players_in_team',
        'max_players_in_team',
    ),
    (
        'score_ordering',
        'point_table',
        'group_size_min',
        'group_size_max',
        'advancement_count',
        'points_carry_to_losers',
    ),
    (
        'playoff_enabled',
        'playoff_group_count',
        'playoff_qualifiers_per_group',
        'playoff_qualifier_count',
        'playoff_elimination_mode',
        'playoff_release_mode',
    ),
    ('from_request_id', 'submission_token'),
)
# `req_map` is a client-only pseudo-field (the request-values decision).
# It has no form field; it is listed so that the JS and Python step
# tables stay identical.

_REVIEW_STEP = len(CREATE_WIZARD_STEP_FIELDS) - 1
_SCORING_STEP = 3
_PLAYOFF_STEP = 4

# Highscore has no scoring step of its own: its Free-for-All fields are
# the settings of the playoff phase and live on the Playoffs step.
_HIGHSCORE_PLAYOFF_FIELDS = frozenset(
    {
        'point_table',
        'group_size_min',
        'group_size_max',
        'advancement_count',
        'points_carry_to_losers',
    }
)

_UPLOAD_BYTES = 5 * 1024 * 1024


def _step_field_names(index: int, is_highscore: bool) -> tuple[str, ...]:
    """Return the fields whose errors belong to a step of the form."""
    field_names = CREATE_WIZARD_STEP_FIELDS[index]
    if not is_highscore:
        return field_names
    if index == _PLAYOFF_STEP:
        return (*field_names, *_HIGHSCORE_PLAYOFF_FIELDS)
    if index == _SCORING_STEP:
        return tuple(
            name
            for name in field_names
            if name not in _HIGHSCORE_PLAYOFF_FIELDS
        )
    return field_names


def first_error_step(form: Form) -> int | None:
    """Return the index of the earliest step that holds a form error."""
    errors = form.errors
    if not errors:
        return None

    game_format = getattr(form, 'game_format', None)
    is_highscore = (
        game_format is not None
        and game_format.data == GameFormat.HIGHSCORE.name
    )

    for index in range(len(CREATE_WIZARD_STEP_FIELDS)):
        if any(
            name in errors for name in _step_field_names(index, is_highscore)
        ):
            return index

    if '' in errors:
        return _REVIEW_STEP

    return None


def build_create_wizard_context(
    party: Party,
    form: Form,
    *,
    source_request: TournamentRequest | None,
    source_proposer_name: str | None,
    staged_image: TournamentImage | None,
    urls: Mapping[str, str],
    refusal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the `wizard` template context of the create form."""
    staged = _staged_image_to_dict(staged_image) if staged_image else None
    step = first_error_step(form)
    source_values = (
        _request_field_values(source_request) if source_request else None
    )

    config = {
        'sessionKey': (
            f'lt-create:{party.id}:'
            f'{source_request.id if source_request else "new"}'
        ),
        'capacity': party.max_ticket_quantity,
        'timezoneLabel': _get_timezone_label(),
        'timezoneName': current_app.config['TIMEZONE'],
        'timezoneDetail': _get_timezone_detail(party),
        'locale': _get_locale_tag(),
        'limits': {
            'nameMax': 80,
            'gameMax': 80,
            'textMax': 10000,
            'altMax': 200,
            'pointTableMax': MAX_POINT_TABLE_PLACES,
            'pointValueMax': MAX_POINTS_PER_PLACE,
            'countMax': MAX_PARTICIPANT_LIMIT,
            'playoffGroupMax': MAX_PLAYOFF_GROUP_COUNT,
            'lobbyMax': MAX_LOBBY_SIZE,
            'uploadBytes': _UPLOAD_BYTES,
            'minWidth': 960,
            'minHeight': 540,
            'maxWidth': 8000,
            'maxHeight': 8000,
        },
        'validCombinations': _get_valid_combinations(),
        'urls': dict(urls),
        'request': (
            _request_to_dict(source_request, source_proposer_name)
            if source_request
            else None
        ),
        'refusal': dict(refusal) if refusal else None,
        'sourceValues': source_values,
        'stagedImage': (
            {
                'imageId': str(staged_image.id),
                'url': staged['url'],
                'filename': staged['filename'],
                'width': staged['width'],
                'height': staged['height'],
                'byteSize': staged_image.byte_size,
            }
            if staged_image and staged
            else None
        ),
        'firstErrorStep': step,
        'serverErrors': {
            name: [str(error) for error in errors]
            for name, errors in form.errors.items()
        },
        'strings': build_create_wizard_strings(),
    }

    return {
        'config': config,
        'timezone_label': config['timezoneLabel'],
        'timezone_name': current_app.config['TIMEZONE'],
        'timezone_detail': _get_timezone_detail(party),
        'capacity': party.max_ticket_quantity,
        'party_dates': _format_party_dates(party),
        'staged_image': staged,
        'first_error_step': step,
        'server_banner': step is not None and step < _REVIEW_STEP,
        'refusal': refusal,
        'source_values': source_values,
    }


def _staged_image_to_dict(image: TournamentImage) -> dict[str, Any]:
    return {
        'url': (
            f'/data/parties/{image.party_id}/lan_tournament/images/'
            f'{image.id}.{image.image_type.name}'
        ),
        'filename': image.filename,
        'width': image.width,
        'height': image.height,
    }


def _request_field_values(source_request: TournamentRequest) -> dict[str, str]:
    """Return the form values a request prefills, as the inputs show them."""
    return {
        'name': source_request.name or '',
        'game': source_request.game or '',
        'description': source_request.description or '',
        'ruleset': source_request.special_rules or '',
        'start_time': to_user_timezone(
            source_request.preferred_start_time
        ).strftime('%Y-%m-%dT%H:%M'),
        'game_format': source_request.game_format.name,
        'elimination_mode': source_request.elimination_mode.name,
    }


def _request_to_dict(
    source_request: TournamentRequest, proposer_name: str | None
) -> dict[str, Any]:
    return {
        'number': source_request.number,
        'proposerName': proposer_name,
        'teamSize': source_request.team_size,
        'participantLimit': source_request.participant_limit,
        'derivedContestantType': (
            ContestantType.SOLO.name
            if source_request.team_size == 1
            else ContestantType.TEAM.name
        ),
    }


def build_request_refusal(
    refused_request: TournamentRequest,
    *,
    proposer_name: str | None,
    decider_name: str | None,
    view_url: str,
) -> dict[str, Any]:
    """Return what the review says about a request that cannot be used."""
    number = f'{refused_request.number:04d}'

    detail = ''
    if refused_request.status is TournamentRequestStatus.withdrawn:
        if refused_request.updated_at is not None:
            detail = gettext(
                '%(name)s withdrew it at %(time)s.',
                name=proposer_name or gettext('Unknown'),
                time=format_time(refused_request.updated_at, 'HH:mm'),
            )
    elif refused_request.status is TournamentRequestStatus.rejected:
        if refused_request.decided_at is not None:
            detail = gettext(
                '%(name)s rejected it at %(time)s.',
                name=decider_name or gettext('Unknown'),
                time=format_time(refused_request.decided_at, 'HH:mm'),
            )

    return {
        'number': number,
        'lead': gettext('Request #%(id)s is no longer accepted.', id=number),
        'detail': detail,
        'closing': gettext('No tournament was created; your entries are kept.'),
        'viewUrl': view_url,
        'proposerName': proposer_name,
        'statusLabel': _request_status_label(refused_request.status),
        'request': _request_to_dict(refused_request, proposer_name),
    }


def _request_status_label(status: TournamentRequestStatus) -> str:
    match status:
        case TournamentRequestStatus.accepted:
            return gettext('Accepted')
        case TournamentRequestStatus.withdrawn:
            return gettext('Withdrawn')
        case TournamentRequestStatus.rejected:
            return gettext('Rejected')
        case TournamentRequestStatus.tournament_created:
            return gettext('Tournament created')
        case _:
            return gettext('Submitted')


def _get_valid_combinations() -> dict[str, list[str]]:
    combinations: dict[str, list[str]] = {}
    for game_format, elimination_mode in sorted(
        VALID_COMBINATIONS, key=lambda pair: (pair[0].name, pair[1].name)
    ):
        combinations.setdefault(game_format.name, []).append(
            elimination_mode.name
        )
    return combinations


def _get_timezone_label() -> str:
    """Return the configured timezone with its current UTC offset."""
    name = current_app.config['TIMEZONE']
    try:
        offset = datetime.now(ZoneInfo(name)).utcoffset()
    except ZoneInfoNotFoundError:
        return name

    minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = '+' if minutes >= 0 else '-'
    hours, minutes = divmod(abs(minutes), 60)
    return f'{name} (UTC{sign}{hours:02d}:{minutes:02d})'


def format_file_size(byte_size: int) -> str:
    """Format a byte size like the wizard: whole KB below 1 MiB, else MB."""
    if byte_size <= 0:
        return format_decimal(0) + ' KB'
    if byte_size < 1048576:
        return (
            format_decimal(max(1, round(byte_size / 1024)), format='#,##0')
            + ' KB'
        )
    return format_decimal(byte_size / 1048576, format='#,##0.0') + ' MB'


def _get_locale_tag() -> str:
    """Return the current locale as a BCP 47 language tag."""
    return str(get_locale() or 'en').replace('_', '-')


def _get_timezone_detail(party: Party) -> str:
    """Return the abbreviation and UTC offset at the party start."""
    return get_timezone_detail_at(party.starts_at)


def get_timezone_detail_at(moment: datetime) -> str:
    """Return the abbreviation and UTC offset at a (naive UTC) moment."""
    try:
        zone = ZoneInfo(current_app.config['TIMEZONE'])
    except ZoneInfoNotFoundError:
        return ''

    local = moment.replace(tzinfo=UTC).astimezone(zone)
    offset = local.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = '+' if minutes >= 0 else '-'
    hours, minutes = divmod(abs(minutes), 60)
    utc_offset = f'UTC{sign}{hours}' + (f':{minutes:02d}' if minutes else '')

    abbreviation = get_timezone_name(
        local, width='short', uncommon=True, locale=get_locale()
    )
    if not abbreviation or abbreviation[0] in '+-':
        abbreviation = local.tzname() or ''

    return f'{abbreviation}, {utc_offset}' if abbreviation else utc_offset


def _format_party_dates(party: Party) -> str:
    """Return the party period, e.g. `02.10.–05.10.2026`."""
    start = to_user_timezone(party.starts_at)
    end = to_user_timezone(party.ends_at)
    locale = get_locale()

    def with_year(value: datetime) -> str:
        return format_skeleton('yMMdd', value, locale=locale)

    if start.date() == end.date():
        return with_year(end)

    if start.year == end.year:
        first = format_skeleton('MMdd', start, locale=locale)
    else:
        first = with_year(start)

    return f'{first}–{with_year(end)}'


def build_create_wizard_strings() -> dict[str, str]:
    """Return the JS-side msgids mapped to their translation."""
    return {
        'Create tournament': gettext('Create tournament'),
        'Basics': gettext('Basics'),
        'Competition': gettext('Competition'),
        'Participants': gettext('Participants'),
        'Scoring and groups': gettext('Scoring and groups'),
        'Review and create': gettext('Review and create'),
        'Step %(n)s of %(total)s': gettext('Step %(n)s of %(total)s'),
        'All steps': gettext('All steps'),
        'current step': gettext('current step'),
        'done': gettext('done'),
        'has errors': gettext('has errors'),
        'not needed': gettext('not needed'),
        'still open': gettext('still open'),
        'Not needed for 1v1': gettext('Not needed for 1v1'),
        'Points, groups': gettext('Points, groups'),
        'Score sorting': gettext('Score sorting'),
        'Depends on the format': gettext('Depends on the format'),
        'Playoffs': gettext('Playoffs'),
        'nothing to set': gettext('nothing to set'),
        'No playoffs': gettext('No playoffs'),
        '%(g)s groups, top %(q)s each': gettext('%(g)s groups, top %(q)s each'),
        '%(g)s groups, the best one each': gettext(
            '%(g)s groups, the best one each'
        ),
        'Top %(k)s of the leaderboard': gettext('Top %(k)s of the leaderboard'),
        'Playoff mode': gettext('Playoff mode'),
        'Pools': gettext('Pools'),
        'Playoffs exist only for 1v1 with "Everyone plays everyone" and for Highscore. This tournament (%(what)s) has one phase and behaves as before.': gettext(
            'Playoffs exist only for 1v1 with "Everyone plays everyone" and for Highscore. This tournament (%(what)s) has one phase and behaves as before.'
        ),
        'Min. group size': gettext('Min. group size'),
        'Min. lobby size': gettext('Min. lobby size'),
        'Max. group size': gettext('Max. group size'),
        'Max. lobby size': gettext('Max. lobby size'),
        'Advancing per lobby': gettext('Advancing per lobby'),
        'Lobby size': gettext('Lobby size'),
        'Summary': gettext('Summary'),
        'No playoffs. One phase, as before.': gettext(
            'No playoffs. One phase, as before.'
        ),
        'Playoffs: %(what)s → %(mode)s, release %(release)s.': gettext(
            'Playoffs: %(what)s → %(mode)s, release %(release)s.'
        ),
        'manual': gettext('manual'),
        '%(n)s teams': gettext('%(n)s teams'),
        '%(n)s players': gettext('%(n)s players'),
        '%(g)s groups': gettext('%(g)s groups'),
        '%(g)s groups of %(s)s': gettext('%(g)s groups of %(s)s'),
        '%(g)s groups of %(s)s to %(t)s': gettext(
            '%(g)s groups of %(s)s to %(t)s'
        ),
        'the best one': gettext('the best one'),
        'the best %(q)s': gettext('the best %(q)s'),
        '%(n)s qualifiers': gettext('%(n)s qualifiers'),
        'too few for double knockout (at least 4)': gettext(
            'too few for double knockout (at least 4)'
        ),
        (
            'At least 2 and at least the min. lobby size (%(min)s). The first'
            ' phase ends with "Close qualification".'
        ): gettext(
            'At least 2 and at least the min. lobby size (%(min)s). The first'
            ' phase ends with "Close qualification".'
        ),
        'bracket with %(slots)s places, no byes': gettext(
            'bracket with %(slots)s places, no byes'
        ),
        'bracket with %(slots)s places, 1 bye': gettext(
            'bracket with %(slots)s places, 1 bye'
        ),
        'bracket with %(slots)s places, %(byes)s byes': gettext(
            'bracket with %(slots)s places, %(byes)s byes'
        ),
        'Up to %(n)s teams': gettext('Up to %(n)s teams'),
        'Up to %(n)s players': gettext('Up to %(n)s players'),
        '%(l)s lobbies of %(s)s': gettext('%(l)s lobbies of %(s)s'),
        '%(l)s lobbies of %(s)s to %(t)s': gettext(
            '%(l)s lobbies of %(s)s to %(t)s'
        ),
        'each %(a)s advance': gettext('each %(a)s advance'),
        'Playoff settings reset. They do not fit the new format.': gettext(
            'Playoff settings reset. They do not fit the new format.'
        ),
        'Please choose a playoff elimination mode.': gettext(
            'Please choose a playoff elimination mode.'
        ),
        'Please choose how the playoffs are released.': gettext(
            'Please choose how the playoffs are released.'
        ),
        'Please enter the number of groups.': gettext(
            'Please enter the number of groups.'
        ),
        'At least two groups are needed.': gettext(
            'At least two groups are needed.'
        ),
        'Please enter how many advance from each group.': gettext(
            'Please enter how many advance from each group.'
        ),
        'The minimum number of contestants is too small for this many groups.': gettext(
            'The minimum number of contestants is too small for this many groups.'
        ),
        'The maximum number of contestants is too small for this many groups.': gettext(
            'The maximum number of contestants is too small for this many groups.'
        ),
        'Fewer must advance from each group than the smallest group holds.': gettext(
            'Fewer must advance from each group than the smallest group holds.'
        ),
        'Double elimination playoffs need at least 4 qualifiers in total.': gettext(
            'Double elimination playoffs need at least 4 qualifiers in total.'
        ),
        'Playoffs need at least 2 qualifiers in total.': gettext(
            'Playoffs need at least 2 qualifiers in total.'
        ),
        'Please enter the number of qualifiers.': gettext(
            'Please enter the number of qualifiers.'
        ),
        'Please enter how many advance per lobby.': gettext(
            'Please enter how many advance per lobby.'
        ),
        'At least two qualifiers are needed.': gettext(
            'At least two qualifiers are needed.'
        ),
        'Qualifiers must be at least the minimum group size.': gettext(
            'Qualifiers must be at least the minimum group size.'
        ),
        'The qualifiers cannot be split into lobbies between the minimum '
        'and maximum group size.': gettext(
            'The qualifiers cannot be split into lobbies between the '
            'minimum and maximum group size.'
        ),
        '%(n)s error': ngettext('%(n)s error', '%(n)s errors', 1, n='%(n)s'),
        '%(n)s errors': ngettext('%(n)s error', '%(n)s errors', 2, n='%(n)s'),
        '%(min)s to %(max)s players': gettext('%(min)s to %(max)s players'),
        '%(min)s to %(max)s teams': gettext('%(min)s to %(max)s teams'),
        '%(min)s to %(max)s per team': gettext('%(min)s to %(max)s per team'),
        'Teams': gettext('Teams'),
        'Single knockout': gettext('Single knockout'),
        'Double knockout': gettext('Double knockout'),
        'Everyone plays everyone': gettext('Everyone plays everyone'),
        'Back': gettext('Back'),
        'Continue: %(step)s': gettext('Continue: %(step)s'),
        'Cancel': gettext('Cancel'),
        'Create draft tournament': gettext('Create draft tournament'),
        'Creating …': gettext('Creating …'),
        'from request': gettext('from request'),
        'From request #%(id)s by %(user)s': gettext(
            'From request #%(id)s by %(user)s'
        ),
        'Tournament image': gettext('Tournament image'),
        'JPEG, PNG or WebP · max. 5 MB · recommended 1920 × 1080 (16:9), at least 960 × 540. No cropping, the centre stays visible.': gettext(
            'JPEG, PNG or WebP · max. 5 MB · recommended 1920 × 1080 (16:9), at least 960 × 540. No cropping, the centre stays visible.'
        ),
        'Drag image here': gettext('Drag image here'),
        'Release to upload': gettext('Release to upload'),
        'Choose file …': gettext('Choose file …'),
        'Uploading … %(pct)s · You can continue meanwhile.': gettext(
            'Uploading … %(pct)s · You can continue meanwhile.'
        ),
        'Cancel upload': gettext('Cancel upload'),
        'Uploaded and checked. The preview shows the 16:9 crop from the tournament list.': gettext(
            'Uploaded and checked. The preview shows the 16:9 crop from the tournament list.'
        ),
        'Replace …': gettext('Replace …'),
        'Remove': gettext('Remove'),
        'Image description': gettext('Image description'),
        'Only needed if the image carries information such as text or a logo. Leave empty if purely decorative.': gettext(
            'Only needed if the image carries information such as text or a logo. Leave empty if purely decorative.'
        ),
        '%(file)s was not accepted': gettext('%(file)s was not accepted'),
        'The file is not a supported image. Allowed are JPEG, PNG and WebP.': gettext(
            'The file is not a supported image. Allowed are JPEG, PNG and WebP.'
        ),
        'The file is %(size)s. The maximum is 5 MB.': gettext(
            'The file is %(size)s. The maximum is 5 MB.'
        ),
        'The image is %(w)s × %(h)s pixels. At least 960 × 540 is required.': gettext(
            'The image is %(w)s × %(h)s pixels. At least 960 × 540 is required.'
        ),
        'The file is damaged or not a readable image.': gettext(
            'The file is damaged or not a readable image.'
        ),
        'Upload failed. Please try again. Your other entries are kept.': gettext(
            'Upload failed. Please try again. Your other entries are kept.'
        ),
        'Choose another file …': gettext('Choose another file …'),
        'Continue without image': gettext('Continue without image'),
        'Choose existing image …': gettext('Choose existing image …'),
        'Choose existing image': gettext('Choose existing image'),
        'This party': gettext('This party'),
        'All parties': gettext('All parties'),
        'Search by file name …': gettext('Search by file name …'),
        'Used by: %(names)s': gettext('Used by: %(names)s'),
        'Not assigned to any tournament yet': gettext(
            'Not assigned to any tournament yet'
        ),
        'current': gettext('current'),
        'Use this image': gettext('Use this image'),
        'The image is not copied. Removing or replacing it here does not change other tournaments.': gettext(
            'The image is not copied. Removing or replacing it here does not change other tournaments.'
        ),
        'No tournament images have been uploaded for this party yet.': gettext(
            'No tournament images have been uploaded for this party yet.'
        ),
        'No image matches the search.': gettext('No image matches the search.'),
        'Show all parties': gettext('Show all parties'),
        'Upload new …': gettext('Upload new …'),
        'or': gettext('or'),
        'On phones, the photo library opens.': gettext(
            'On phones, the photo library opens.'
        ),
        'The file is not a supported image (SVG). Allowed are JPEG, PNG and WebP.': gettext(
            'The file is not a supported image (SVG). Allowed are JPEG, PNG and WebP.'
        ),
        'Your other entries are kept.': gettext('Your other entries are kept.'),
        'Search by file name': gettext('Search by file name'),
        'Image': gettext('Image'),
        'Preview': gettext('Preview'),
        'Image removed.': gettext('Image removed.'),
        'From existing images.': gettext('From existing images.'),
        'From existing images, also used by %(names)s.': gettext(
            'From existing images, also used by %(names)s.'
        ),
        'The preview shows the 16:9 crop from the tournament list.': gettext(
            'The preview shows the 16:9 crop from the tournament list.'
        ),
        'Note:': gettext('Note:'),
        'This image is no longer available. Please choose another.': gettext(
            'This image is no longer available. Please choose another.'
        ),
        'Use an image URL instead': gettext('Use an image URL instead'),
        'For older tournaments and externally hosted images. An uploaded image takes precedence.': gettext(
            'For older tournaments and externally hosted images. An uploaded image takes precedence.'
        ),
        'Who competes?': gettext('Who competes?'),
        'Each person registers individually.': gettext(
            'Each person registers individually.'
        ),
        'Teams register. A captain manages the members.': gettext(
            'Teams register. A captain manages the members.'
        ),
        'Defines how a single match works.': gettext(
            'Defines how a single match works.'
        ),
        'Two compete, one side wins the match.': gettext(
            'Two compete, one side wins the match.'
        ),
        'Several play at once in a group. Placement earns points.': gettext(
            'Several play at once in a group. Placement earns points.'
        ),
        'Everyone plays on their own and submits a result. The leaderboard decides.': gettext(
            'Everyone plays on their own and submits a result. The leaderboard decides.'
        ),
        'Defines who is eliminated when.': gettext(
            'Defines who is eliminated when.'
        ),
        'Losers are eliminated.': gettext('Losers are eliminated.'),
        'The first loss leads to the losers bracket, the second eliminates.': gettext(
            'The first loss leads to the losers bracket, the second eliminates.'
        ),
        'Everyone plays everyone once. The table decides.': gettext(
            'Everyone plays everyone once. The table decides.'
        ),
        'Not available: %(reason)s': gettext('Not available: %(reason)s'),
        'Only available for %(format)s': gettext(
            'Only available for %(format)s'
        ),
        'Choose a game format first. Then only matching modes are shown.': gettext(
            'Choose a game format first. Then only matching modes are shown.'
        ),
        'automatic': gettext('automatic'),
        'No elimination. Highscore has no matches and nobody is eliminated. The leaderboard at the end decides.': gettext(
            'No elimination. Highscore has no matches and nobody is eliminated. The leaderboard at the end decides.'
        ),
        'Example with 8: 3 rounds, 7 matches': gettext(
            'Example with 8: 3 rounds, 7 matches'
        ),
        'Elimination mode reset. "%(mode)s" is not available for %(format)s. Please choose again.': gettext(
            'Elimination mode reset. "%(mode)s" is not available for %(format)s. Please choose again.'
        ),
        'Free-for-All settings no longer apply. Points, group sizes and advancement are not saved for %(format)s.': gettext(
            'Free-for-All settings no longer apply. Points, group sizes and advancement are not saved for %(format)s.'
        ),
        'Score ordering no longer applies. It is only saved for Highscore.': gettext(
            'Score ordering no longer applies. It is only saved for Highscore.'
        ),
        'You switched to %(type)s. %(fields)s only apply to %(other)s and are not saved.': gettext(
            'You switched to %(type)s. %(fields)s only apply to %(other)s and are not saved.'
        ),
        'Undo': gettext('Undo'),
        "All limits are optional. Without them, registration is only limited by the party's seats.": gettext(
            "All limits are optional. Without them, registration is only limited by the party's seats."
        ),
        'no limit': gettext('no limit'),
        'Leave empty for no limit.': gettext('Leave empty for no limit.'),
        'Shown on the tournament page.': gettext(
            'Shown on the tournament page.'
        ),
        'Smaller teams are reported as incomplete.': gettext(
            'Smaller teams are reported as incomplete.'
        ),
        'Up to %(t)s teams × %(p)s players = %(n)s of %(seats)s party seats.': gettext(
            'Up to %(t)s teams × %(p)s players = %(n)s of %(seats)s party seats.'
        ),
        'From the request, not yet mapped': gettext(
            'From the request, not yet mapped'
        ),
        'These values depend on Solo or Teams, so they are not applied automatically.': gettext(
            'These values depend on Solo or Teams, so they are not applied automatically.'
        ),
        'Apply': gettext('Apply'),
        "Don't apply": gettext("Don't apply"),
        'Change decision': gettext('Change decision'),
        'Defines the order of the leaderboard.': gettext(
            'Defines the order of the leaderboard.'
        ),
        'Higher is better': gettext('Higher is better'),
        'Lower is better': gettext('Lower is better'),
        'Template:': gettext('Template:'),
        '+ Add place': gettext('+ Add place'),
        'Points for place %(n)s': gettext('Points for place %(n)s'),
        'Remove place %(n)s': gettext('Remove place %(n)s'),
        'Place 1 first. Places without an entry get 0 points.': gettext(
            'Place 1 first. Places without an entry get 0 points.'
        ),
        'Groups · size in %(unit)s': gettext('Groups · size in %(unit)s'),
        'At most this many %(unit)s play at once.': gettext(
            'At most this many %(unit)s play at once.'
        ),
        'Points carry to losers pool': gettext('Points carry to losers pool'),
        'On: all points from winners and losers bracket count for final seeding. Off: only points scored in the losers bracket count there; winners-bracket survivors are seeded first.': gettext(
            'On: all points from winners and losers bracket count for final seeding. Off: only points scored in the losers bracket count there; winners-bracket survivors are seeded first.'
        ),
        'Sample group with %(n)s %(unit)s, %(k)s advance': gettext(
            'Sample group with %(n)s %(unit)s, %(k)s advance'
        ),
        'advances': gettext('advances'),
        'Not set': gettext('Not set'),
        'Edit': gettext('Edit'),
        'The server is checking your entries …': gettext(
            'The server is checking your entries …'
        ),
        'Create without link to the request': gettext(
            'Create without link to the request'
        ),
        'View request': gettext('View request'),
        'Link to the request removed.': gettext('Link to the request removed.'),
        'All set.': gettext('All set.'),
        'The server checked your entries at %(time)s.': gettext(
            'The server checked your entries at %(time)s.'
        ),
        'Pre-check unavailable right now.': gettext(
            'Pre-check unavailable right now.'
        ),
        'You can still create: the server checks everything again. Your entries are kept.': gettext(
            'You can still create: the server checks everything again. Your entries are kept.'
        ),
        'The tournament is created as %(draft)s. Registration stays closed until you open it on the tournament page.': gettext(
            'The tournament is created as %(draft)s. Registration stays closed until you open it on the tournament page.'
        ),
        'a draft': gettext('a draft'),
        'Scoring': gettext('Scoring'),
        '%(n)s characters': gettext('%(n)s characters'),
        'no minimum': gettext('no minimum'),
        'Players': gettext('Players'),
        'Players per team': gettext('Players per team'),
        'Points by placement': gettext('Points by placement'),
        'Advancing per group': gettext('Advancing per group'),
        'Group size': gettext('Group size'),
        'Values from the request': gettext('Values from the request'),
        'applied': gettext('applied'),
        'not applied': gettext('not applied'),
        'Yes, carried over': gettext('Yes, carried over'),
        'No, the losers bracket counts separately': gettext(
            'No, the losers bracket counts separately'
        ),
        'Points in the losers bracket': gettext('Points in the losers bracket'),
        'Winner per match, no scoring settings needed': gettext(
            'Winner per match, no scoring settings needed'
        ),
        'Origin': gettext('Origin'),
        'Origin request': gettext('Origin request'),
        '#%(id)s by %(user)s · will be linked after creation': gettext(
            '#%(id)s by %(user)s · will be linked after creation'
        ),
        '#%(id)s by %(user)s · will not be linked': gettext(
            '#%(id)s by %(user)s · will not be linked'
        ),
        'Unknown': gettext('Unknown'),
        'Description: "%(alt)s"': gettext('Description: "%(alt)s"'),
        'decorative, no description': gettext('decorative, no description'),
        'URL: %(url)s': gettext('URL: %(url)s'),
        '%(min)s to %(max)s': gettext('%(min)s to %(max)s'),
        '%(min)s to %(max)s %(unit)s': gettext('%(min)s to %(max)s %(unit)s'),
        'Minimum not set, technically 2': gettext(
            'Minimum not set, technically 2'
        ),
        '%(date)s, %(time)s': gettext('%(date)s, %(time)s'),
        'The server rejected the creation. We took you to the affected field.': gettext(
            'The server rejected the creation. We took you to the affected field.'
        ),
        '%(n)s entry needs attention': ngettext(
            '%(n)s entry needs attention',
            '%(n)s entries need attention',
            1,
            n='%(n)s',
        ),
        '%(n)s entries need attention': ngettext(
            '%(n)s entry needs attention',
            '%(n)s entries need attention',
            2,
            n='%(n)s',
        ),
        'Please enter a name.': gettext('Please enter a name.'),
        'Whole numbers from 1 only.': gettext('Whole numbers from 1 only.'),
        'Must be at least "%(other)s" (%(n)s).': gettext(
            'Must be at least "%(other)s" (%(n)s).'
        ),
        'Required for Free-for-All.': gettext('Required for Free-for-All.'),
        'Add points for at least place 1.': gettext(
            'Add points for at least place 1.'
        ),
        'With at most %(n)s teams no group of at least %(min)s teams can form. Lower the minimum to %(n)s or raise "Max. teams" in step 3.': gettext(
            'With at most %(n)s teams no group of at least %(min)s teams can form. Lower the minimum to %(n)s or raise "Max. teams" in step 3.'
        ),
        'Fewer than %(n)s must advance from the smallest group (%(n)s teams).': gettext(
            'Fewer than %(n)s must advance from the smallest group (%(n)s teams).'
        ),
        'Please decide whether to apply the participant values from the request.': gettext(
            'Please decide whether to apply the participant values from the request.'
        ),
        'This combination of game format and elimination mode is not supported.': gettext(
            'This combination of game format and elimination mode is not supported.'
        ),
        'Entries restored from this session.': gettext(
            'Entries restored from this session.'
        ),
        'At most %(max)d characters – currently %(length)d.': gettext(
            'At most %(max)d characters – currently %(length)d.'
        ),
        'At most %(max)s.': gettext('At most %(max)s.'),
        'At least 2.': gettext('At least 2.'),
        'Only complete http or https addresses, e.g. https://example.org/image.png': gettext(
            'Only complete http or https addresses, e.g. https://example.org/image.png'
        ),
        'Please choose whether individuals or teams compete.': gettext(
            'Please choose whether individuals or teams compete.'
        ),
        'Please choose a game format.': gettext('Please choose a game format.'),
        'Please choose an elimination mode.': gettext(
            'Please choose an elimination mode.'
        ),
        'Please choose which results are better.': gettext(
            'Please choose which results are better.'
        ),
        'At most %(max)s places.': gettext('At most %(max)s places.'),
        'Points may be at most %(max)s.': gettext(
            'Points may be at most %(max)s.'
        ),
        'Points may be at least %(min)s.': gettext(
            'Points may be at least %(min)s.'
        ),
        'Must not be larger than the max. group size (%(n)s).': gettext(
            'Must not be larger than the max. group size (%(n)s).'
        ),
        'With at most %(n)s players no group of at least %(min)s can form. Lower the minimum or raise "Max. players" in step 3.': gettext(
            'With at most %(n)s players no group of at least %(min)s can form. Lower the minimum or raise "Max. players" in step 3.'
        ),
        'Fewer than %(n)s must advance from the smallest group (%(n)s players).': gettext(
            'Fewer than %(n)s must advance from the smallest group (%(n)s players).'
        ),
        'Min. players': gettext('Min. players'),
        'Max. players': gettext('Max. players'),
        'Min. teams': gettext('Min. teams'),
        'Max. teams': gettext('Max. teams'),
        'Min. players per team': gettext('Min. players per team'),
        'Max. players per team': gettext('Max. players per team'),
        'teams': gettext('teams'),
        'players': gettext('players'),
        'Solo': gettext('Solo'),
        'Team': gettext('Team'),
        'Up to %(n)s of %(seats)s party seats.': gettext(
            'Up to %(n)s of %(seats)s party seats.'
        ),
        'No limit: up to %(seats)s party seats.': gettext(
            'No limit: up to %(seats)s party seats.'
        ),
        'Without max. teams and max. players per team there is no limit (party: %(seats)s seats).': gettext(
            'Without max. teams and max. players per team there is no limit (party: %(seats)s seats).'
        ),
        'Applied': gettext('Applied'),
        'Not applied': gettext('Not applied'),
        'Change undone.': gettext('Change undone.'),
        'Error': gettext('Error'),
        'From the request': gettext('From the request'),
        'Yes': gettext('Yes'),
        'No': gettext('No'),
        'Team size: %(team_size)s, limit: %(limit)s.': gettext(
            'Team size: %(team_size)s, limit: %(limit)s.'
        ),
        'Limit %(limit)s → max. players = %(limit)s': gettext(
            'Limit %(limit)s → max. players = %(limit)s'
        ),
        'Team size %(team_size)s → min./max. players per team = %(team_size)s · Limit %(limit)s → max. teams = %(limit)s': gettext(
            'Team size %(team_size)s → min./max. players per team = %(team_size)s · Limit %(limit)s → max. teams = %(limit)s'
        ),
        'Load more': gettext('Load more'),
        'The images could not be loaded. Please try again.': gettext(
            'The images could not be loaded. Please try again.'
        ),
        '1v1': gettext('1v1'),
        'Free-for-All': gettext('Free-for-All'),
        'Highscore': gettext('Highscore'),
        'More than the party has seats (%(seats)s).': gettext(
            'More than the party has seats (%(seats)s).'
        ),
        '%(t)s teams × %(p)s players = %(n)s, more than the party has seats (%(seats)s).': gettext(
            '%(t)s teams × %(p)s players = %(n)s, more than the party has seats (%(seats)s).'
        ),
        'Places %(from)s–%(to)s get 0 points.': gettext('Places %(from)s–%(to)s get 0 points.'),
        'Place %(n)s gets 0 points.': gettext('Place %(n)s gets 0 points.'),
        'Step %(n)s': gettext('Step %(n)s'),
        'Team size %(team_size)s · participant limit %(limit)s': gettext(
            'Team size %(team_size)s · participant limit %(limit)s'
        ),
        'Suggestion for %(type)s: %(mapping)s.': gettext(
            'Suggestion for %(type)s: %(mapping)s.'
        ),
        'Team size %(team_size)s → min. and max. players per team = %(team_size)s · participant limit %(limit)s → max. teams = %(limit)s': gettext(
            'Team size %(team_size)s → min. and max. players per team = %(team_size)s · participant limit %(limit)s → max. teams = %(limit)s'
        ),
        'Participant limit %(limit)s → max. players = %(limit)s': gettext(
            'Participant limit %(limit)s → max. players = %(limit)s'
        ),
        'The request asks for team size 1, so rather Solo. Check your choice in step 2.': gettext(
            'The request asks for team size 1, so rather Solo. Check your choice in step 2.'
        ),
        '✓ Applied. You can still adjust the fields below.': gettext(
            '✓ Applied. You can still adjust the fields below.'
        ),
        'Not applied. You set the limits yourself.': gettext(
            'Not applied. You set the limits yourself.'
        ),
        'Sample group with %(n)s %(unit)s': gettext(
            'Sample group with %(n)s %(unit)s'
        ),
        'Sample group with %(n)s %(unit)s, 1 advances': gettext(
            'Sample group with %(n)s %(unit)s, 1 advances'
        ),
        'Place': gettext('Place'),
        'Player': gettext('Player'),
        'Points': gettext('Points'),
        'Result': gettext('Result'),
        'eliminated': gettext('eliminated'),
        'eliminated or losers bracket': gettext('eliminated or losers bracket'),
        'Team %(letter)s': gettext('Team %(letter)s'),
        'Player %(n)s': gettext('Player %(n)s'),
        'The table results from wins and losses.': gettext(
            'The table results from wins and losses.'
        ),
        'The bracket is generated before the start.': gettext(
            'The bracket is generated before the start.'
        ),
        'No knockout': gettext('No knockout'),
    }


def serialize_seeding_board(
    board: SeedingBoard,
    *,
    names: Mapping[str, str],
    strings: Mapping[str, str],
) -> dict[str, Any]:
    """Return the seeding board as a JSON-safe dict, for orga surfaces only."""
    state = board.state
    is_bracket = state.format in (
        SeedingFormat.SINGLE_ELIMINATION,
        SeedingFormat.DOUBLE_ELIMINATION,
    )

    def name_of(contestant_id: str | None) -> str | None:
        if contestant_id is None:
            return None
        return names.get(contestant_id, contestant_id)

    seed_number = {
        cid: index + 1 for index, cid in enumerate(state.seed_list)
    }
    tier_of = dict(zip(state.roster, state.tiers, strict=True))
    tiers = [
        {
            'index': tier,
            'letter': chr(ord('A') + tier),
            'contestants': [
                {
                    'id': cid,
                    'name': name_of(cid),
                    'seed': seed_number[cid],
                    'left': cid in board.stale_leaver_ids,
                    'new': cid in board.new_entrant_ids,
                }
                for cid in state.seed_list
                if tier_of[cid] == tier
            ],
        }
        for tier in range(state.tier_count)
    ]

    is_ffa = state.format is SeedingFormat.FREE_FOR_ALL

    def slot(index: int, group: int | None, seed_position: int | None):
        cid = state.layout[index]
        return {
            'index': index,
            'contestant_id': cid,
            'name': name_of(cid),
            'bye': cid is None,
            'seed_position': seed_position,
            'group': group,
            'left': cid in board.stale_leaver_ids,
            'new': cid in board.new_entrant_ids,
            'tier': chr(ord('A') + tier_of[cid]) if is_ffa and cid else None,
            'seed': (
                seed_number.get(cid)
                if cid and (is_ffa or cid in board.origin_labels)
                else None
            ),
            'origin': board.origin_labels.get(cid) if cid else None,
        }

    two_bye_problems = {
        params['n']: gettext(msgid, **params)
        for msgid, params in zip(
            board.problems, board.problem_params, strict=True
        )
        if msgid == seeding_domain.PROBLEM_TWO_BYES
    }

    def same_group_info(k: int) -> dict[str, Any] | None:
        if (k + 1) not in board.same_group_matches:
            return None
        first, second = (slot(2 * k, None, None), slot(2 * k + 1, None, None))
        return {
            'group': first['origin'][0],
            'a': {'name': first['name'], 'origin': first['origin']},
            'b': {'name': second['name'], 'origin': second['origin']},
        }

    sections: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    if is_bracket:
        size = len(state.layout)
        seed_order = _standard_seed_order(size)
        matches = [
            {
                'number': k + 1,
                'slots': [
                    slot(2 * k, None, seed_order[2 * k] + 1),
                    slot(2 * k + 1, None, seed_order[2 * k + 1] + 1),
                ],
                'problem': two_bye_problems.get(k + 1),
                'same_group': (k + 1) in board.same_group_matches,
                'same_group_info': same_group_info(k),
            }
            for k in range(size // 2)
        ]
        if size >= 8:
            kind, count = 'quarter', 4
        elif size == 4:
            kind, count = 'half', 2
        else:
            kind, count = 'final', 1
        per_section = (size // 2) // count
        sections = [
            {
                'kind': kind,
                'number': i + 1,
                'matches': list(
                    range(i * per_section + 1, (i + 1) * per_section + 1)
                ),
            }
            for i in range(count)
        ]
    else:
        start = 0
        sizes = seeding_domain.group_sizes(
            state.format, len(state.layout), state.param
        )
        for group, group_size in enumerate(sizes):
            filled = [
                cid
                for cid in state.layout[start : start + group_size]
                if cid is not None
            ]
            groups.append(
                {
                    'index': group,
                    'letter': chr(ord('A') + group),
                    'size': group_size,
                    'invalid': len(filled) < 2,
                    'slots': [
                        slot(i, group, None)
                        for i in range(start, start + group_size)
                    ],
                }
            )
            start += group_size

    balance = None
    if board.balance is not None:
        balance = {
            'counts': [list(row) for row in board.balance.counts],
            'allowed': list(board.balance.allowed),
            'over': [
                {'group': group, 'tier': tier, 'count': count}
                for group, tier, count in board.balance.over
            ],
        }

    clusters: list[dict[str, Any]] = []
    if board.balance is not None:
        allowed = board.balance.allowed
        clusters = [
            {
                'lobby': group + 1,
                'tier': tier,
                'letter': chr(ord('A') + tier),
                'count': count,
                'allowed': allowed[tier],
            }
            for group, tier, count in board.balance.over
        ]
    if balance is not None:
        balance['clusters'] = clusters

    byes = sorted(
        (
            (present['seed_position'], present['name'])
            for match in matches
            for present in match['slots']
            if not present['bye'] and any(s['bye'] for s in match['slots'])
        ),
    )

    if board.problems:
        tab_status = {'kind': 'err', 'label': gettext('invalid')}
    elif clusters:
        tab_status = {
            'kind': 'warn',
            'label': ngettext(
                '%(n)s cluster',
                '%(n)s clusters',
                len(clusters),
                n=len(clusters),
            ),
        }
    elif board.balance is not None:
        tab_status = {'kind': 'ok', 'label': gettext('balanced')}
    else:
        tab_status = {'kind': 'ok', 'label': gettext('valid')}

    code_meta = None
    if board.code:
        raw_code = board.code.replace('-', '')
        decoded = seed_code.decode_seed_code(raw_code)
        fingerprint = (
            decoded.unwrap().fingerprint
            if decoded.is_ok()
            else seed_code.roster_fingerprint(state.roster)
        )
        draw_seed = (
            decoded.unwrap().draw_seed if decoded.is_ok() else state.draw_seed
        )
        current_ids = [
            *(i for i in state.roster if i not in board.stale_leaver_ids),
            *board.stale_joiner_ids,
        ]
        code_meta = {
            'length': len(raw_code),
            'fingerprint': f'{fingerprint:08x}',
            'draw_number': f'{draw_seed:08x}',
            'current_fingerprint': (
                f'{seed_code.roster_fingerprint(current_ids):08x}'
            ),
        }

    return {
        'target': board.target,
        'target_label': seeding_target_label(board.target, state.format),
        'version': board.version,
        'format': state.format.value,
        'param': state.param,
        'tier_count': state.tier_count,
        'code': board.code,
        'code_short': (
            _short_seed_code(board.code.replace('-', ''))
            if board.code
            else None
        ),
        'generated_code': board.generated_code,
        'generated_code_short': (
            _short_seed_code(board.generated_code)
            if board.generated_code
            else None
        ),
        'generated_code_display': (
            seed_code.format_seed_code(board.generated_code)
            if board.generated_code
            else None
        ),
        'stale': board.stale,
        'stale_structure': board.stale_structure,
        'stale_ranks': board.stale_ranks,
        'stale_leavers': list(board.stale_leavers),
        'stale_joiners': list(board.stale_joiners),
        'problems': [
            gettext(msgid, **params)
            for msgid, params in zip(
                board.problems, board.problem_params, strict=True
            )
        ],
        'fix_count': board.fix_count,
        'prefilled': board.prefilled,
        'tier_origin': _tier_origin(board, tiers),
        'pure_draw': board.pure_draw,
        'generation': board.generation.value,
        'locked_reason': (
            gettext(board.locked_reason) if board.locked_reason else None
        ),
        'notices': [
            gettext(msgid, **params)
            for msgid, params in zip(
                board.notices, board.notice_params, strict=True
            )
        ],
        'undersized': [_undersized_notice(p) for p in board.undersized],
        'lobby_byes': [_bye_notice(name) for name in board.byes],
        'waiting_winners': [
            _waiting_winner_notice(name)
            for name in getattr(board, 'waiting_winners', ())
        ],
        'tiers': tiers,
        'layout': {
            'kind': 'bracket' if is_bracket else 'groups',
            'sections': sections,
            'matches': matches,
            'groups': groups,
        },
        'balance': balance,
        'player_count': len(state.roster),
        'max_lobby': (
            state.param if state.format is SeedingFormat.FREE_FOR_ALL else None
        ),
        'byes': [
            {'seed_position': position, 'name': name}
            for position, name in byes
        ],
        'group_sizes': [group['size'] for group in groups],
        'tab_status': tab_status,
        'code_meta': code_meta,
        'strings': dict(strings),
    }


def wants_json(request) -> bool:
    """Return whether the client asked for a JSON answer."""
    return request.accept_mimetypes.best == 'application/json'


def parse_int(raw: str | None) -> int | None:
    """Return the integer in `raw`, or `None` if absent or malformed."""
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def parse_seeding_action(form):
    """Return `(expected_version, action)`, or `None` if malformed."""
    svc = tournament_seeding_service
    version = parse_int(form.get('version'))
    if version is None:
        return None

    match form.get('action'):
        case 'swap':
            p, q = parse_int(form.get('p')), parse_int(form.get('q'))
            if p is None or q is None:
                return None
            action = svc.Swap(p, q)
        case 'move_tier':
            tier = parse_int(form.get('tier'))
            contestant_id = form.get('contestant_id')
            if tier is None or not contestant_id:
                return None
            action = svc.MoveTier(
                contestant_id,
                tier,
                ref_id=form.get('ref_id') or None,
                after=form.get('after') in ('1', 'true'),
            )
        case 'set_tier_count':
            n = parse_int(form.get('n'))
            if n is None:
                return None
            action = svc.SetTierCount(n)
        case 'redraw':
            action = svc.Redraw()
        case 'reset_fixes':
            action = svc.ResetFixes()
        case 'replay':
            code = form.get('code', '').strip()
            if not code:
                return None
            action = svc.Replay(code)
        case 'reseed_keep_tiers':
            action = svc.ReseedKeepTiers()
        case 'separate':
            action = svc.Separate()
        case 'reprefill':
            action = svc.Reprefill()
        case _:
            return None
    return version, action


def seeding_error_status(error_message: str) -> int:
    """Return the HTTP status for a seeding service error."""
    if error_message == tournament_seeding_service.ERR_CONFLICT:
        return 409
    return 422


def leaderboard_submission_times(
    tournament_id: TournamentID, state: QualificationState
) -> dict[str, datetime]:
    """Return when each leaderboard value was submitted, for display only."""
    if state.source != 'leaderboard':
        return {}
    match tournament_score_service.get_leaderboard(tournament_id):
        case Ok(submissions):
            return {
                str(s.participant_id or s.team_id): s.submitted_at
                for s in submissions
            }
        case Err(_):
            return {}


def separation_message(
    before: SeedingBoard | None, after: SeedingBoard
) -> str:
    """Return the notice for a separation: the pairs that traded places."""
    if before is None:
        return gettext('Separated: %(pairs)s.', pairs='')
    was, now = before.state.layout, after.state.layout
    used: set[int] = set()
    pairs = []

    def label(contestant: str | None) -> str:
        if contestant is None:
            return gettext('Bye')
        return after.labels.get(contestant, contestant)

    for i, contestant in enumerate(was):
        if i in used or contestant is None or now[i] == contestant:
            continue
        for j in range(i + 1, len(was)):
            if j not in used and now[j] == contestant and was[j] == now[i]:
                used.update((i, j))
                pairs.append(f'{label(contestant)} \u2194 {label(was[j])}')
                break
    return gettext('Separated: %(pairs)s.', pairs=', '.join(pairs))


START_CONFIRM_FIELD = 'confirm_generated_layout'

START_NOT_GENERATED_ERROR = (
    'Cannot start tournament without generated brackets. '
    'Generate brackets first.'
)
START_CONFIRM_REQUIRED_ERROR = (
    'Confirm that the generated layout is used before starting.'
)


@dataclass(frozen=True)
class StartGate:
    """What the start button of a tournament offers.

    `state` is `open` (a plain start), `confirm` (the board changed after
    the generation, so the start needs a confirmation) or `blocked` (the
    start is refused, `reason` is a msgid). `notices` are translated
    warnings shown beside the button; they never change the state.
    """

    state: str
    reason: str | None = None
    notices: tuple[str, ...] = ()


_START_OPEN = StartGate('open')


def start_gate(tournament: Tournament) -> StartGate:
    """Return what the start button offers, from the seeding's status.

    Formats without a seeding board keep the plain start, and so does a
    tournament without a draft (generated before the seeding existed). The
    gate only peeks: it never draws a draft.
    """
    notices = tuple(
        gettext(msgid, **params)
        for msgid, params in tournament_seeding_service.start_notices(
            tournament
        )
    )
    return replace(_start_gate_state(tournament), notices=notices)


def _start_gate_state(tournament: Tournament) -> StartGate:
    game_format = tournament.game_format
    if (
        tournament.tournament_status is not TournamentStatus.REGISTRATION_CLOSED
        or game_format is None
        or not (
            game_format.requires_bracket_generation
            or game_format.uses_placements
        )
    ):
        return _START_OPEN
    if not tournament_match_service.has_matches(tournament.id):
        return StartGate('blocked', START_NOT_GENERATED_ERROR)

    violations = tournament_seeding_service.start_violations(tournament.id)
    if violations:
        return StartGate('blocked', violations[0])
    generation = tournament_seeding_service.peek_initial_generation_status(
        tournament.id
    )
    if generation is None:
        return _START_OPEN
    if generation is tournament_seeding_service.GenerationStatus.NOT_GENERATED:
        return StartGate('blocked', START_NOT_GENERATED_ERROR)
    if generation is tournament_seeding_service.GenerationStatus.DIFFERS:
        return StartGate('confirm')
    return _START_OPEN


def start_refusal(
    tournament: Tournament, form: Mapping[str, Any]
) -> str | None:
    """Return the msgid refusing a start without the confirmation, if any."""
    if start_gate(tournament).state == 'confirm' and not form.get(
        START_CONFIRM_FIELD
    ):
        return START_CONFIRM_REQUIRED_ERROR
    return None


def _undersized_notice(pool: tournament_match_service.UndersizedPool) -> str:
    """Return the board notice with the actual reason for the shortfall."""
    if pool.natural_shortfall:
        return gettext(
            'Advancement leaves %(where)s with %(count)s contestants.'
            ' Lobby sizes: %(sizes)s, below the minimum of %(minimum)s.',
            where=_pool_phrase(tournament_match_service.ffa_pool_token(pool.pool)),
            count=pool.count,
            sizes=', '.join(str(size) for size in pool.lobbies),
            minimum=pool.minimum,
        )
    return gettext(
        'Because participants were removed, %(where)s has only %(count)s'
        ' contestants. Lobby sizes: %(sizes)s, below the minimum of'
        ' %(minimum)s.',
        where=_pool_phrase(tournament_match_service.ffa_pool_token(pool.pool)),
        count=pool.count,
        sizes=', '.join(str(size) for size in pool.lobbies),
        minimum=pool.minimum,
    )


def _tier_origin(
    board: SeedingBoard, tiers: Sequence[Mapping[str, Any]]
) -> str | None:
    """Name the leaderboard places each tier of a prefilled FFA draft holds."""
    if (
        board.target != 'playoff'
        or not board.prefilled
        or board.state.format is not SeedingFormat.FREE_FOR_ALL
    ):
        return None
    bands = []
    first = 1
    for tier in tiers:
        last = first + len(tier['contestants']) - 1
        if last < first:
            continue
        if last == first:
            band = gettext(
                '%(n)s tier %(letter)s', n=first, letter=tier['letter']
            )
        else:
            band = gettext(
                '%(first)s–%(last)s tier %(letter)s',
                first=first,
                last=last,
                letter=tier['letter'],
            )
        bands.append(band)
        first = last + 1
    return gettext(
        'From the leaderboard: places %(bands)s. A tie across a band edge'
        ' would only be a hint here, not an obstacle.',
        bands=', '.join(bands),
    )


def _bye_notice(name: str) -> str:
    """Return the board notice for a contestant alone in the losers pool."""
    return gettext(
        'Bye: %(name)s is alone in the losers pool and gets no lobby this'
        ' round. They join the next losers round, or the Grand Final.',
        name=name,
    )


def _waiting_winner_notice(name: str) -> str:
    """Name the winners bracket winner waiting for the Grand Final."""
    return gettext(
        '%(name)s is waiting for the Grand Final while the lower bracket continues.',
        name=name,
    )


def seeding_board_payload(board: SeedingBoard) -> dict[str, Any]:
    """Return the serialized board with the orga surfaces' names and strings."""
    payload = serialize_seeding_board(
        board,
        names=board.labels,
        strings=seeding_js_strings(),
    )
    payload['format_name'] = _seeding_format_name(board.state.format)
    return payload


_FFA_TARGET = re.compile(r'ffa:(SE|WB|LB):([0-9]{1,4})')


def seeding_target_label(
    target: str, seeding_format: SeedingFormat | None = None
) -> str:
    """Return the heading of a seeding target."""
    if target == tournament_seeding_service.INITIAL_TARGET:
        if seeding_format is SeedingFormat.ROUND_ROBIN:
            return gettext('Initial placement (round 1) · groups')
        return gettext('Initial placement (round 1)')
    if target == tournament_seeding_service.PLAYOFF_TARGET:
        return gettext('Playoffs')
    found = _FFA_TARGET.fullmatch(target)
    if found is None:
        return target
    pool, number = found.group(1), int(found.group(2))
    params = {'n': number + 1, 'prev': number}
    if number == 0:
        return gettext('Round %(n)s', **params)
    if pool == 'WB':
        return gettext(
            'Round %(n)s · winners pool, prefilled from the standings after round %(prev)s',
            **params,
        )
    if pool == 'LB':
        return gettext(
            'Round %(n)s · losers pool, prefilled from the standings after round %(prev)s',
            **params,
        )
    return gettext(
        'Round %(n)s · prefilled from the standings after round %(prev)s',
        **params,
    )


def generation_flash(tournament: Tournament, target: str, count: int) -> str:
    """Return the flash that names what a generation created."""
    found = _FFA_TARGET.fullmatch(target)
    if found is not None:
        return ngettext(
            'Round %(round)d: %(num)d lobby generated.',
            'Round %(round)d: %(num)d lobbies generated.',
            count,
            round=int(found.group(2)) + 1,
        )
    if target == tournament_seeding_service.PLAYOFF_TARGET:
        game_format = tournament.playoff_game_format
        elimination_mode = tournament.playoff_elimination_mode
    else:
        game_format = tournament.game_format
        elimination_mode = tournament.elimination_mode
    if game_format is GameFormat.FREE_FOR_ALL:
        return ngettext(
            '%(num)d lobby generated.',
            '%(num)d lobbies generated.',
            count,
        )
    if elimination_mode is EliminationMode.ROUND_ROBIN:
        return ngettext(
            'Groups generated with %(num)d match.',
            'Groups generated with %(num)d matches.',
            count,
        )
    return ngettext(
        'Bracket generated with %(num)d match.',
        'Bracket generated with %(num)d matches.',
        count,
    )


def _seeding_format_name(seeding_format: SeedingFormat) -> str:
    match seeding_format:
        case SeedingFormat.SINGLE_ELIMINATION:
            return gettext('Single knockout')
        case SeedingFormat.DOUBLE_ELIMINATION:
            return gettext('Double knockout')
        case SeedingFormat.ROUND_ROBIN:
            return gettext('Everyone plays everyone')
        case SeedingFormat.FREE_FOR_ALL:
            return gettext('Free-for-all')


def seeding_js_strings() -> dict[str, str]:
    """Return the texts the seeding board script needs, translated."""
    return {
        'bye': gettext('Bye'),
        'selected': gettext('%(name)s selected. Choose the target.'),
        'hint': gettext('Choose where %(name)s goes.'),
        'cancel': gettext('Cancel'),
        'cancelled': gettext('Cancelled.'),
        'swapped': gettext(
            'Swapped: %(a)s (%(wa)s) with %(b)s (%(wb)s).'
        ),
        'separated': gettext('Separated: %(pairs)s.'),
        'moved': gettext('%(name)s moved to tier %(letter)s, now seed %(n)s.'),
        'moved_reset': gettext(
            '%(name)s moved to tier %(letter)s, now seed %(n)s. Layout fixes reset.'
        ),
        'where_match': gettext('M%(n)s, position %(pos)s'),
        'group': gettext('Group %(letter)s'),
        'lobby': gettext('Lobby %(n)s'),
        'locked': gettext('Locked: %(reason)s'),
        'tier_end': gettext('At the end of tier %(letter)s'),
        'saving': gettext('Saving …'),
        'saved': gettext('Saved'),
        'not_saved': gettext('Not saved – try again'),
        'failed': gettext(
            'The request failed. Check your connection and try again.'
        ),
        'saved_reload': gettext(
            'Saved. Reload the page to see the current board.'
        ),
        'conflict_body': gettext('Your last change was not saved.'),
        'reload': gettext('Reload'),
        'copied': gettext('Copied.'),
        'replayed': gettext(
            'Rebuilt from the code: %(format)s, %(n)s participants, %(fixes)s.'
        ),
        'fixes_none': gettext('no layout fixes'),
        'fixes_one': gettext('1 layout fix'),
        'fixes_many': gettext('%(n)s layout fixes'),
        'notice_reset': gettext(
            'Your layout fixes were reset because the tiers changed. Check the layout again.'
        ),
        'notice_rebuilt': gettext(
            'The layout was rebuilt from the tiers (snake order).'
        ),
        'notice_reseeded': gettext(
            '%(names)s removed; the order of the others is kept. Layout fixes reset.'
        ),
        'notice_move': gettext(
            '%(name)s: tier %(from)s → %(to)s, now seed %(place)s.'
        ),
        'notice_reorder': gettext(
            '%(name)s is now seed %(place)s in tier %(tier)s.'
        ),
        'notice_separated_clean': gettext(
            'No round-1 match has two players from the same group any more.'
        ),
        'open_layout': gettext('Open layout board'),
    }


_SEEDING_EVENT_LABELS: dict[str, Callable[[], str]] = {
    'seeding-drawn': lambda: gettext('Drawn'),
    'seeding-swapped': lambda: gettext('Swapped'),
    'seeding-tier-changed': lambda: gettext('Tier changed'),
    'seeding-reordered': lambda: gettext('Order changed'),
    'seeding-tiers-resized': lambda: gettext('Tier count changed'),
    'seeding-fixes-reset': lambda: gettext('Layout fixes reset'),
    'seeding-code-replayed': lambda: gettext('Code replayed'),
    'seeding-roster-reseeded': lambda: gettext('Re-seeded (roster)'),
    'seeding-separated': lambda: gettext('Same-group pairings separated'),
    'seeding-reprefilled': lambda: gettext('Re-prefilled from qualification'),
    'bracket-generated': lambda: gettext('Generated'),
    'bracket-regenerated': lambda: gettext('Regenerated'),
    'bracket-cleared': lambda: gettext('Bracket cleared'),
    'bracket-lobby-undersized': lambda: gettext('Lobby below the minimum'),
    'bracket-lobby-bye': lambda: gettext('Bye'),
    'bracket-single-survivor': lambda: gettext('Lone survivor wins'),
    'qualification-leaderboard-closed': lambda: gettext(
        'Qualification closed'
    ),
    'qualification-tie-decided': lambda: gettext('Tie decided'),
    'qualification-tie-withdrawn': lambda: gettext('Decision withdrawn'),
    'playoffs-released': lambda: gettext('Playoffs released'),
    'playoffs-unreleased': lambda: gettext('Release undone'),
    'playoffs-shortfall': lambda: gettext('Released with fewer qualifiers'),
    'playoffs-de-fallback': lambda: gettext(
        'Single instead of double elimination'
    ),
    'participant-left': lambda: gettext('Left'),
    'participant-removed': lambda: gettext('Removed'),
}


def seeding_event_label(event_type: str) -> str:
    """Return the readable name of an audit event, never the raw key."""
    label = _SEEDING_EVENT_LABELS.get(event_type)
    return label() if label is not None else gettext('Other action')


_REGISTRATION_STATUS_LABELS: dict[str, Callable[[], str]] = {
    'REGISTRATION_CLOSED': lambda: gettext('Registration closed'),
    'REGISTRATION_OPEN': lambda: gettext('Registration reopened'),
}


def _short_seed_code(raw: str) -> str:
    return seed_code.format_seed_code(raw)[:9] + '-…'


def _audit_scope_label(scope: Any) -> str:
    if scope == 'leaderboard':
        return gettext('Leaderboard')
    if scope == 'winner':
        return gettext('Tournament win')
    if scope == qualification_domain.CROSSOVER_SCOPE:
        return gettext('Playoff seeding')
    if not isinstance(scope, str):
        return ''
    group = re.fullmatch(r'group:([0-9]+)', scope)
    if group is not None:
        return gettext(
            'Group %(letter)s', letter=chr(ord('A') + int(group.group(1)))
        )
    lobby = re.fullmatch(r'ffa:(SE|WB|LB):([0-9]{1,4}):([0-9]{1,4})', scope)
    if lobby is not None:
        pool = {
            'WB': gettext('Winners Pool'),
            'LB': gettext('Losers Pool'),
        }.get(lobby.group(1))
        return ' \u00b7 '.join(
            part
            for part in (
                pool,
                gettext('Round %(n)s', n=int(lobby.group(2)) + 1),
                gettext('Lobby %(n)s', n=int(lobby.group(3)) + 1),
            )
            if part
        )
    return ''


def _audit_reason(data: Mapping[str, Any], key: str = 'reason') -> str | None:
    reason = data.get(key)
    return reason if isinstance(reason, str) and reason else None


def _is_count(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _qualification_event_details(
    event_type: str,
    data: Mapping[str, Any],
    names: Mapping[str, str],
    release_code: str | None = None,
) -> str:
    parts: list[str] = []
    if event_type in (
        'qualification-tie-decided',
        'qualification-tie-withdrawn',
    ):
        ids = data.get('contestant_ids')
        rank_from = data.get('rank_from')
        first = (
            rank_from
            if isinstance(rank_from, int) and not isinstance(rank_from, bool)
            else None
        )
        order = ', '.join(
            (f'{first + i}. ' if first is not None else '')
            + names.get(str(cid), str(cid))
            for i, cid in enumerate(ids if isinstance(ids, list) else [])
        )
        scope = _audit_scope_label(data.get('scope'))
        if scope and order:
            parts.append(f'{scope}: {order}')
        elif scope or order:
            parts.append(scope or order)
        reason = _audit_reason(data)
        if reason is not None:
            parts.append(f"{gettext('Reason')}: {reason}")
        original = _audit_reason(data, 'decision_reason')
        if original is not None:
            parts.append(
                gettext('Original reason: %(reason)s', reason=original)
            )
    elif event_type == 'playoffs-released':
        mode = data.get('mode')
        count = data.get('match_count')
        qualifiers = data.get('qualifier_count')
        mode_label = (
            gettext('Manual') if mode == 'manual' else gettext('Automatic')
        )
        if mode in ('manual', 'automatic') and _is_count(qualifiers):
            parts.append(gettext('(%(mode)s)', mode=mode_label))
            parts.append(
                ngettext(
                    '%(n)s qualifier',
                    '%(n)s qualifiers',
                    qualifiers,
                    n=qualifiers,
                )
            )
            if release_code is not None:
                parts.append(gettext('Code %(code)s', code=release_code))
        elif mode in ('manual', 'automatic') and isinstance(count, int):
            parts.append(
                ngettext(
                    '%(mode)s \u00b7 %(n)s match',
                    '%(mode)s \u00b7 %(n)s matches',
                    count,
                    n=count,
                    mode=mode_label,
                )
            )
    elif event_type == 'playoffs-unreleased':
        reason = _audit_reason(data)
        if reason is not None:
            parts.append(f"{gettext('Reason')}: {reason}")
            if data.get('auto_release_suspended') is True:
                parts.append(gettext('Automatic release suspended'))
    elif event_type == 'playoffs-shortfall':
        qualified = data.get('qualified')
        configured = data.get('configured')
        if isinstance(qualified, int) and isinstance(configured, int):
            parts.append(
                gettext(
                    '%(qualified)s of %(configured)s places filled',
                    qualified=qualified,
                    configured=configured,
                )
            )
    elif event_type == 'playoffs-de-fallback':
        qualified = data.get('qualified')
        if isinstance(qualified, int):
            parts.append(
                ngettext(
                    '%(n)s qualifier',
                    '%(n)s qualifiers',
                    qualified,
                    n=qualified,
                )
            )
    elif event_type == 'qualification-leaderboard-closed':
        parts.append(gettext('Leaderboard'))
    return ' \u00b7 '.join(parts)


def _pool_phrase(pool_code: Any) -> str:
    if pool_code == 'WB':
        return gettext('the winners pool')
    if pool_code == 'LB':
        return gettext('the losers pool')
    return gettext('this round')


def _undersized_event_details(data: Mapping[str, Any]) -> str:
    sizes = data.get('lobbies')
    if not isinstance(sizes, list):
        return ''
    if data.get('reason') == 'natural_shortfall':
        return gettext(
            'Natural shortfall after advancement: %(where)s has lobbies of'
            ' %(sizes)s, below the minimum of %(minimum)s.',
            where=_pool_phrase(data.get('pool')),
            sizes=', '.join(str(size) for size in sizes),
            minimum=data.get('minimum'),
        )
    return gettext(
        'Removed participants: %(where)s has lobbies of %(sizes)s,'
        ' below the minimum of %(minimum)s.',
        where=_pool_phrase(data.get('pool')),
        sizes=', '.join(str(size) for size in sizes),
        minimum=data.get('minimum'),
    )


def _bye_event_details(
    data: Mapping[str, Any], names: Mapping[str, str]
) -> str:
    who = data.get('contestant')
    if not isinstance(who, str):
        return ''
    if data.get('pool') == 'WB':
        return gettext(
            '%(name)s won the winners bracket, round %(round)s, and waits for the Grand Final.',
            name=names.get(who, who),
            round=data.get('round'),
        )
    return gettext(
        'Bye: %(name)s waits in %(where)s, round %(round)s.',
        name=names.get(who, who),
        where=_pool_phrase(data.get('pool')),
        round=data.get('round'),
    )


def _tier_letter(tier: Any) -> str:
    return chr(ord('A') + tier) if _is_count(tier) and tier >= 0 else '?'


def _swap_unit(fmt: Any, unit: Any) -> str | None:
    if not _is_count(unit):
        return None
    if fmt in ('SINGLE_ELIMINATION', 'DOUBLE_ELIMINATION'):
        return gettext('M%(n)s', n=unit + 1)
    if fmt == 'ROUND_ROBIN':
        return gettext('Group %(letter)s', letter=_tier_letter(unit))
    if fmt == 'FREE_FOR_ALL':
        return gettext('Lobby %(n)s', n=unit + 1)
    return None


def _swap_details(data: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """Return the swap sentence, or nothing for an entry without its keys."""
    unit_p = _swap_unit(data.get('format'), data.get('unit_p'))
    unit_q = _swap_unit(data.get('format'), data.get('unit_q'))
    if unit_p is None or unit_q is None or 'a' not in data or 'b' not in data:
        return ''

    def occupant(key: str) -> str:
        who = data.get(key)
        if who is None:
            return gettext('Bye')
        return names.get(str(who), str(who))

    return gettext(
        '%(unit_p)s %(a)s \u2194 %(unit_q)s %(b)s',
        unit_p=unit_p,
        a=occupant('a'),
        unit_q=unit_q,
        b=occupant('b'),
    )


def _seeding_action_details(
    event_type: str, data: Mapping[str, Any], names: Mapping[str, str]
) -> str:
    """Return the logged details, or nothing for an entry without them."""
    if event_type == 'seeding-swapped':
        return _swap_details(data, names)
    if event_type in ('seeding-tier-changed', 'seeding-reordered'):
        who = data.get('contestant_id')
        if not isinstance(who, str):
            return ''
        name = names.get(who, who)
        if event_type == 'seeding-tier-changed':
            if not (
                _is_count(data.get('from_tier'))
                and _is_count(data.get('to_tier'))
            ):
                return ''
            return gettext(
                '%(name)s: tier %(from)s \u2192 %(to)s',
                name=name,
                **{
                    'from': _tier_letter(data.get('from_tier')),
                    'to': _tier_letter(data.get('to_tier')),
                },
            )
        if not (
            _is_count(data.get('place_in_tier'))
            and _is_count(data.get('to_tier'))
        ):
            return ''
        return gettext(
            '%(name)s is now seed %(place)s in tier %(tier)s',
            name=name,
            place=data['place_in_tier'],
            tier=_tier_letter(data.get('to_tier')),
        )
    dropped = data.get('dropped_fixes')
    if event_type == 'seeding-tiers-resized':
        count = data.get('tier_count')
        if not _is_count(count):
            return ''
        text = ngettext('%(n)s tier', '%(n)s tiers', count, n=count)
        if _is_count(dropped) and dropped > 0:
            return f'{text} \u00b7 {_dropped_fixes_text(dropped)}'
        return text
    if event_type == 'seeding-fixes-reset':
        return _dropped_fixes_text(dropped) if _is_count(dropped) else ''
    if event_type == 'seeding-roster-reseeded':
        leavers = data.get('leaver_ids')
        if not isinstance(leavers, list) or not leavers:
            return ''
        return gettext(
            '%(names)s removed; tiers and order of the others kept',
            names=', '.join(names.get(str(cid), str(cid)) for cid in leavers),
        )
    return ''


def _dropped_fixes_text(count: int) -> str:
    return ngettext(
        '%(k)s layout correction dropped',
        '%(k)s layout corrections dropped',
        count,
        k=count,
    )


def _participant_event_details(data: Mapping[str, Any]) -> str:
    before = data.get('roster_before')
    after = data.get('roster_after')
    if not (_is_count(before) and _is_count(after)):
        return ''
    return gettext(
        'Roster %(before)s \u2192 %(after)s', before=before, after=after
    )


def _seeding_event_details(
    event_type: str,
    data: Mapping[str, Any],
    names: Mapping[str, str] | None = None,
    release_code: str | None = None,
) -> str:
    if event_type.startswith(('qualification-', 'playoffs-')):
        return _qualification_event_details(
            event_type, data, names or {}, release_code
        )
    if event_type.startswith('participant-'):
        return _participant_event_details(data)
    if event_type.startswith('seeding-'):
        action = _seeding_action_details(event_type, data, names or {})
        if action:
            return action
    if event_type == 'bracket-lobby-undersized':
        return _undersized_event_details(data)
    if event_type == 'seeding-reprefilled' and data.get('automatic'):
        return gettext('Automatic: nobody had changed the draft')
    if event_type == 'bracket-lobby-bye':
        return _bye_event_details(data, names or {})
    if event_type == 'bracket-single-survivor':
        who = data.get('contestant')
        if not isinstance(who, str):
            return ''
        return gettext(
            '%(name)s is the lone survivor and wins the tournament, round %(round)s.',
            name=(names or {}).get(who, who),
            round=data.get('round'),
        )
    if (
        event_type == 'bracket-generated'
        and data.get('target') == tournament_match_service.FFA_GRAND_FINAL_TARGET
    ):
        contestants = data.get('contestants')
        if not isinstance(contestants, list):
            return ''
        return gettext(
            'Grand Final with %(n)s contestants', n=len(contestants)
        )
    raw = data.get('seed_code')
    if not isinstance(raw, str) or not raw:
        return ''
    short = _short_seed_code(raw)
    if event_type == 'seeding-drawn':
        decoded = seed_code.decode_seed_code(raw)
        if decoded.is_ok():
            code = decoded.unwrap()
            return gettext(
                'Draw number %(draw)s · %(n)s participants',
                draw=f'{code.draw_seed:08x}',
                n=code.n,
            )
    if event_type in ('bracket-generated', 'bracket-regenerated'):
        previous = data.get('previous_seed_code')
        if isinstance(previous, str) and previous:
            return gettext(
                'From %(code)s (previously %(old)s)',
                code=short,
                old=_short_seed_code(previous),
            )
        return gettext('From %(code)s', code=short)
    return gettext('Seed code %(code)s', code=short)


SEEDING_LOG_PREFIXES = ('seeding-', 'bracket-', 'qualification-', 'playoffs-')

_STATUS_CHANGED = 'tournament-status-changed'
_RELEASE_CODE_WINDOW = timedelta(minutes=1)


def is_seeding_audit_entry(entry: Any) -> bool:
    """Tell whether a log entry belongs on the seeding boards' audit log."""
    event_type = entry.event_type
    if event_type.startswith((*SEEDING_LOG_PREFIXES, 'participant-')):
        return True
    return (
        event_type == _STATUS_CHANGED
        and entry.data.get('new_status') in _REGISTRATION_STATUS_LABELS
    )


def _release_codes(entries: Sequence[Any]) -> dict[int, str]:
    """Return the playoff code by index of each release entry.

    The code is the one of the playoff generation the release staged right
    before it. `entries` are newest first.
    """
    codes: dict[int, str] = {}
    for index, entry in enumerate(entries):
        if entry.event_type != 'playoffs-released':
            continue
        for older in entries[index + 1 :]:
            if older.event_type in ('playoffs-released', 'playoffs-unreleased'):
                break
            if (
                older.event_type
                not in ('bracket-generated', 'bracket-regenerated')
                or older.data.get('target') != 'playoff'
            ):
                continue
            raw = older.data.get('seed_code')
            if (
                isinstance(raw, str)
                and raw
                and timedelta(0)
                <= entry.occurred_at - older.occurred_at
                <= _RELEASE_CODE_WINDOW
            ):
                codes[index] = _short_seed_code(raw)
            break
    return codes


def _in_user_timezone(moment: datetime) -> datetime:
    """Return `moment` (naive means UTC) in the viewer's time zone."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        return to_user_timezone(moment)
    except RuntimeError:
        return moment


def _audit_time_label(moment: datetime, now: datetime) -> str:
    """Return `HH:MM` for today and `DD.MM. HH:MM` for any other day."""
    local = _in_user_timezone(moment)
    if local.date() == _in_user_timezone(now).date():
        return local.strftime('%H:%M')
    return local.strftime('%d.%m. %H:%M')


def seeding_audit_rows(
    entries: Sequence[Any],
    users_by_id: Mapping[UserID, User],
    names: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return audit rows, newest first, swaps by one orga folded together.

    `entries` must already be newest first. `names` are the contestant
    names a decided order is shown with. `now` defaults to the current time.
    """
    now = now or datetime.now(UTC)

    release_codes = _release_codes(entries)

    def label(entry: Any) -> str:
        if entry.event_type == _STATUS_CHANGED:
            new_status = entry.data.get('new_status')
            if (
                new_status == 'REGISTRATION_OPEN'
                and entry.data.get('old_status') != 'REGISTRATION_CLOSED'
            ):
                return gettext('Registration opened')
            status = _REGISTRATION_STATUS_LABELS.get(new_status)
            if status is not None:
                return status()
        return seeding_event_label(entry.event_type)

    def row(index: int, entry: Any) -> dict[str, Any]:
        user = users_by_id.get(entry.initiator_id)
        return {
            'occurred_at': entry.occurred_at,
            'time_label': _audit_time_label(entry.occurred_at, now),
            'target': entry.data.get('target'),
            'seed_code': entry.data.get('seed_code'),
            'event_type': entry.event_type,
            'who': user.screen_name if user is not None else None,
            'initiator_id': entry.initiator_id,
            'label': label(entry),
            'details': _seeding_event_details(
                entry.event_type, entry.data, names, release_codes.get(index)
            ),
            'children': [],
        }

    rows: list[dict[str, Any]] = []
    swap_details: dict[int, list[str]] = {}
    previous_entry: Any = None
    for index, entry in enumerate(entries):
        current = row(index, entry)
        previous = rows[-1] if rows else None
        if (
            previous is not None
            and entry.event_type == 'seeding-swapped'
            and previous['event_type'] == 'seeding-swapped'
            and previous['initiator_id'] == entry.initiator_id
        ):
            group = swap_details.setdefault(
                id(previous), [_swap_details(previous_entry.data, names or {})]
            )
            group.append(_swap_details(entry.data, names or {}))
            if not previous['children']:
                previous['children'].append(dict(previous))
            previous['children'].append(current)
            previous['label'] = gettext(
                '%(n)s × swapped', n=len(previous['children'])
            )
            previous['details'] = ' \u00b7 '.join(filter(None, group))
            continue
        rows.append(current)
        previous_entry = entry
    return rows


def qualification_strings() -> dict[str, str]:
    """Return the texts of the qualification surfaces, translated."""
    return {
        'tie_cut': gettext(tournament_match_service.QUALIFICATION_TIE_ERROR),
        'tie_seeding': gettext(
            'Tie between seeding ranks. It decides the playoff seeding; the'
            ' orga sets the order.'
        ),
        'tie_group_win': gettext(
            'Tie for the group win. It decides the playoff seeding; the orga'
            ' sets the order.'
        ),
        'tie_winner': gettext(
            'Tie for the tournament win. The orga sets the order.'
        ),
        'tie_harmless': gettext(
            'Tie without effect on qualification. The shared place stays;'
            ' nobody needs to decide.'
        ),
        'status_qualified': gettext('Qualified'),
        'status_out': gettext('Eliminated'),
        'status_tie': gettext('Tie'),
        'status_open': gettext('Open'),
        'decided_by_head_to_head': gettext('Head-to-head'),
        'decided_by_difference': gettext('Difference'),
        'decided_by_scores_for': gettext('Scored'),
        'decided_by_orga': gettext('Orga decision'),
        'scope_leaderboard': gettext('Leaderboard'),
        'scope_winner': gettext('Tournament win'),
        'scope_group': gettext('Group %(letter)s'),
        'scope_crossover': gettext('Playoff seeding'),
        'decision_outdated': gettext('Decision outdated'),
        'decision_outdated_text': gettext(
            'This decision no longer matches a tie. Decide the tie again, or'
            ' withdraw the decision.'
        ),
    }


def qualification_js_strings() -> dict[str, str]:
    """Return the texts the ordering script shows and announces, translated."""
    return {
        'cancel': gettext('Cancel'),
        'place': gettext('Place %(n)s'),
        'order_moved': gettext('%(name)s is now in place %(place)s.'),
    }


def contestant_names(tournament_id: TournamentID) -> dict[str, str]:
    """Return the display names of a tournament's contestants by ID.

    Removed participants and teams are included: they keep their place
    in a ranking.
    """
    names: dict[str, str] = {}
    teams = tournament_repository.get_teams_for_tournament(
        tournament_id, include_removed=True
    )
    for team in teams:
        names[str(team.id)] = team.name
    participants = tournament_repository.get_participants_for_tournament(
        tournament_id, include_removed=True
    )
    users = user_service.get_users_indexed_by_id(
        {p.user_id for p in participants}
    )
    for participant in participants:
        user = users.get(participant.user_id)
        names[str(participant.id)] = (
            user.screen_name
            if user is not None and user.screen_name
            else str(participant.user_id)
        )
    return names


def _places_label(rank_from: int, rank_to: int) -> str:
    if rank_from == rank_to:
        return str(rank_from)
    return f'{rank_from}–{rank_to}'


def _shortfall_text(
    tournament: Tournament, qualified: int, configured: int
) -> str:
    if tournament.playoff_game_format is GameFormat.FREE_FOR_ALL:
        return gettext(
            'Only %(qualified)s of %(configured)s playoff places are filled.'
            ' The first playoff round may have smaller lobbies, with at'
            ' least 2 each.',
            qualified=qualified,
            configured=configured,
        )
    return gettext(
        'Only %(qualified)s of %(configured)s playoff places are filled.'
        ' The playoffs start smaller; the top seeds get the byes.',
        qualified=qualified,
        configured=configured,
    )


def serialize_qualification(
    state: QualificationState,
    names: Mapping[str, str],
    strings: Mapping[str, str],
    *,
    tournament: Tournament | None = None,
    decisions: Mapping[str, QualificationDecision] | None = None,
    users: Mapping[UserID, User] | None = None,
    submitted_at: Mapping[str, datetime] | None = None,
) -> dict[str, Any]:
    """Return the qualification as a JSON-safe dict, for orga surfaces only.

    Reasons and orga decisions are for orgas; a participant surface must
    not use this. `strings` comes from `qualification_strings`. With the
    `tournament`, entries get the qualified/eliminated status from the
    cut line and the leaderboard knows whether it is closed.
    """
    decisions = decisions or {}
    users = users or {}
    submitted_at = submitted_at or {}
    is_leaderboard = state.source == 'leaderboard'
    cut: int | None = None
    closed = True
    if tournament is not None:
        if is_leaderboard:
            cut = tournament.playoff_qualifier_count
            closed = tournament.leaderboard_closed_at is not None
        elif state.source == 'groups':
            cut = tournament.playoff_qualifiers_per_group

    def name_of(contestant_id: str) -> str:
        return names.get(contestant_id, contestant_id)

    def scope_label(scope: str) -> str:
        if scope == 'leaderboard':
            return strings['scope_leaderboard']
        if scope == 'winner':
            return strings['scope_winner']
        if scope == qualification_domain.CROSSOVER_SCOPE:
            return strings['scope_crossover']
        found = re.fullmatch(r'group:([0-9]+)', scope)
        if found is None:
            return scope
        letter = chr(ord('A') + int(found.group(1)))
        return strings['scope_group'] % {'letter': letter}

    shortfall = None
    configured = state.configured_qualifier_count
    if (
        tournament is not None
        and configured is not None
        and state.qualifiers is not None
        and len(state.qualifiers) < configured
    ):
        shortfall = {
            'qualified': len(state.qualifiers),
            'configured': configured,
            'text': _shortfall_text(
                tournament, len(state.qualifiers), configured
            ),
        }
    de_fallback_text = (
        gettext(
            'Double elimination needs at least 4 qualifiers. With %(n)s,'
            ' the playoffs run as single elimination.',
            n=len(state.qualifiers or ()),
        )
        if state.de_fallback
        else None
    )

    tie_text = {
        qualification_domain.TieKind.CUT: strings['tie_cut'],
        qualification_domain.TieKind.SEEDING: strings['tie_seeding'],
        qualification_domain.TieKind.WINNER: strings['tie_winner'],
        qualification_domain.TieKind.HARMLESS: strings['tie_harmless'],
    }

    def tie_text_of(tie: qualification_domain.TieBlock) -> str:
        if (
            tie.kind is qualification_domain.TieKind.SEEDING
            and tie.rank_from == 1
            and tie.scope.startswith('group:')
        ):
            return strings.get('tie_group_win', strings['tie_seeding'])
        return tie_text[tie.kind]

    def tie_dict(tie: qualification_domain.TieBlock) -> dict[str, Any]:
        return {
            'scope': tie.scope,
            'scope_label': scope_label(tie.scope),
            'kind': tie.kind.value,
            'decided': tie.decided,
            'blocking': (
                tie.kind is not qualification_domain.TieKind.HARMLESS
                and not tie.decided
            ),
            'places': _places_label(tie.rank_from, tie.rank_to),
            'rank_from': tie.rank_from,
            'contestants': [
                {'id': cid, 'name': name_of(cid)} for cid in tie.contestant_ids
            ],
            'text': tie_text_of(tie),
        }

    def status_of(ranking, entry) -> str:
        for tie in ranking.ties:
            if (
                entry.contestant_id in tie.contestant_ids
                and not tie.decided
                and tie.kind is not qualification_domain.TieKind.HARMLESS
            ):
                return 'tie'
        if ranking.open_matches or (is_leaderboard and not closed):
            return 'open'
        if cut is not None:
            return 'qualified' if entry.rank <= cut else 'out'
        return 'open'

    def stats_of(row) -> dict[str, int] | None:
        if row is None:
            return None
        return {
            'played': row.played,
            'won': row.won,
            'drawn': row.drawn,
            'lost': row.lost,
            'points': row.points,
            'score_for': row.score_for,
            'score_against': row.score_against,
            'diff': row.diff,
        }

    def entry_dict(ranking, entry) -> dict[str, Any]:
        status = status_of(ranking, entry)
        return {
            'contestant_id': entry.contestant_id,
            'name': name_of(entry.contestant_id),
            'rank': entry.rank,
            'rank_label': f'{entry.rank}=' if entry.shared else str(entry.rank),
            'shared': entry.shared,
            'decided_by': entry.decided_by,
            'decided_by_label': (
                strings.get(f'decided_by_{entry.decided_by}')
                if entry.decided_by
                else None
            ),
            'status': status,
            'status_label': strings[f'status_{status}'],
            'stats': stats_of(entry.row),
            'value': entry.value,
            'submitted_at': submitted_at.get(entry.contestant_id),
        }

    rankings = [
        {
            'scope': ranking.scope,
            'label': scope_label(ranking.scope),
            'open_matches': ranking.open_matches,
            'cut': cut,
            'entries': [entry_dict(ranking, e) for e in ranking.entries],
            'ties': [tie_dict(tie) for tie in ranking.ties],
        }
        for ranking in state.rankings
    ]

    rankings_by_scope = {r.scope: r for r in state.rankings}
    if state.crossover is not None:
        rankings_by_scope[state.crossover.scope] = state.crossover
    decision_rows = []
    for scope, decision in decisions.items():
        if scope.startswith('ffa:'):
            continue
        ranking_of_scope = rankings_by_scope.get(scope)
        ranked = (
            {e.contestant_id for e in ranking_of_scope.entries}
            if ranking_of_scope is not None
            else set()
        )
        for block in decision.blocks:
            decider = users.get(block.decided_by)
            tie = None
            if ranking_of_scope is not None:
                members = {c for c in block.contestant_ids if c in ranked}
                tie = next(
                    (
                        t
                        for t in ranking_of_scope.ties
                        if t.decided and set(t.contestant_ids) == members
                    ),
                    None,
                )
            if tie is not None:
                contestants = [
                    {
                        'id': cid,
                        'name': name_of(cid),
                        'place': tie.rank_from + i,
                    }
                    for i, cid in enumerate(tie.contestant_ids)
                ]
            else:
                contestants = [
                    {'id': cid, 'name': name_of(cid), 'place': None}
                    for cid in block.contestant_ids
                ]
            decision_rows.append(
                {
                    'scope': scope,
                    'scope_label': scope_label(scope),
                    'status': 'applied' if tie is not None else 'outdated',
                    'places': (
                        _places_label(tie.rank_from, tie.rank_to)
                        if tie is not None
                        else None
                    ),
                    'contestants': contestants,
                    'withdraw_ids': list(block.contestant_ids),
                    'reason': block.reason,
                    'decided_by': (
                        decider.screen_name if decider is not None else None
                    ),
                    'decided_at': block.decided_at,
                }
            )

    released_by = users.get(state.released_by) if state.released_by else None
    return {
        'source': state.source,
        'ready': state.ready,
        'open_match_count': state.open_match_count,
        'total_match_count': state.total_match_count,
        'leaderboard_closed': closed if is_leaderboard else None,
        'rankings': rankings,
        'blockers': [tie_dict(tie) for tie in state.blockers],
        'qualifiers': (
            None
            if state.qualifiers is None
            else [
                {
                    'contestant_id': q.contestant_id,
                    'name': name_of(q.contestant_id),
                    'scope': q.scope,
                    'scope_label': scope_label(q.scope),
                    'rank': q.rank,
                }
                for q in state.qualifiers
            ]
        ),
        'decisions': decision_rows,
        'release': {
            'shortfall': shortfall,
            'de_fallback_text': de_fallback_text,
            'released': state.released_at is not None,
            'released_at': state.released_at,
            'released_by': (
                released_by.screen_name if released_by is not None else None
            ),
            'mode': state.release_mode.value,
            'auto_suspended': state.auto_release_suspended,
            'can_unrelease': state.can_unrelease,
            'unrelease_why': _unrelease_why(tournament),
        },
    }


def _unrelease_why(tournament: Tournament | None) -> str:
    """Say why the release cannot be taken back."""
    status = getattr(tournament, 'tournament_status', None)
    if status is not None and status.name not in ('ONGOING', 'PAUSED'):
        return gettext(
            'The release can only be taken back while the tournament is'
            ' ongoing or paused.'
        )
    return gettext(
        'A playoff result is confirmed. The release can no longer be undone.'
    )


def serialize_ffa_cut_ties(
    ties: Sequence[FfaCutTie],
    names: Mapping[str, str],
    *,
    users: Mapping[UserID, User] | None = None,
    outdated: Sequence[FfaOutdatedDecision] = (),
) -> list[dict[str, Any]]:
    """Return the FFA lobby cut ties as JSON-safe dicts, for orgas only.

    Each row has a `status`: `open`, `decided`, or, for the `outdated`
    decisions appended at the end, `outdated`. The reason and the decider
    of a decision are orga-only; a participant surface must not use this.
    """
    users = users or {}
    pool_labels = {
        'WB': gettext('Winners Pool'),
        'LB': gettext('Losers Pool'),
    }
    text = gettext(tournament_match_service.QUALIFICATION_TIE_ERROR)

    def label_of(item: FfaCutTie | FfaOutdatedDecision) -> str:
        return ' · '.join(
            part
            for part in (
                pool_labels.get(item.pool),
                gettext('Round %(n)s', n=item.round_number + 1),
                gettext('Lobby %(n)s', n=item.lobby + 1),
            )
            if part
        )

    def decision_of(block: DecisionBlock | None) -> dict[str, Any] | None:
        if block is None:
            return None
        decider = users.get(block.decided_by)
        return {
            'reason': block.reason,
            'decided_by': decider.screen_name if decider is not None else None,
            'decided_at': block.decided_at,
        }

    rows = []
    for tie in ties:
        label = label_of(tie)
        decision = decision_of(tie.decision)
        rows.append(
            {
                'scope': tie.scope,
                'scope_label': label,
                'kind': 'cut',
                'status': 'decided' if tie.decided else 'open',
                'decided': tie.decided,
                'blocking': not tie.decided,
                'locked': tie.locked,
                'places': _places_label(tie.rank_from, tie.rank_to),
                'rank_from': tie.rank_from,
                'contestants': [
                    {
                        'id': c.contestant_id,
                        'name': names.get(c.contestant_id, c.contestant_id),
                        'points': c.points,
                    }
                    for c in tie.contestants
                ],
                'text': text,
                'decision': decision,
                'withdraw_ids': (
                    list(tie.decision.contestant_ids)
                    if tie.decision is not None
                    else []
                ),
            }
        )
    for item in outdated:
        rows.append(
            {
                'scope': item.scope,
                'scope_label': label_of(item),
                'kind': 'cut',
                'status': 'outdated',
                'decided': False,
                'blocking': False,
                'locked': item.locked,
                'places': None,
                'rank_from': None,
                'contestants': [
                    {
                        'id': cid,
                        'name': names.get(cid, cid),
                        'points': None,
                    }
                    for cid in item.block.contestant_ids
                ],
                'text': gettext(
                    'This decision no longer matches a tie. Decide the tie'
                    ' again, or withdraw the decision.'
                ),
                'decision': decision_of(item.block),
                'withdraw_ids': list(item.block.contestant_ids),
            }
        )
    return rows


def ffa_cut_ties_payload(tournament_id: TournamentID) -> list[dict[str, Any]]:
    """Return the cut ties of the tournament's current FFA round, for orgas.

    Decisions that match no tie any more are appended with the status
    `outdated`.
    """
    report = tournament_qualification_service.get_ffa_decision_report(
        tournament_id
    )
    if not report.ties and not report.outdated:
        return []
    user_ids = {
        t.decision.decided_by for t in report.ties if t.decision is not None
    } | {o.block.decided_by for o in report.outdated}
    return serialize_ffa_cut_ties(
        report.ties,
        contestant_names(tournament_id),
        users=user_service.get_users_indexed_by_id(user_ids),
        outdated=report.outdated,
    )


def participant_rankings(
    state: QualificationState,
    names: Mapping[str, str],
    strings: Mapping[str, str],
    tournament: Tournament,
) -> list[dict[str, Any]]:
    """Return the group tables or the leaderboard for participants.

    A whitelist of what a participant may see: places, names, results,
    status, and by which criterion a place was decided. No reasons, no
    deciders, no seeds and no tiers.
    """
    full = serialize_qualification(state, names, strings, tournament=tournament)
    tie_label = gettext('Tie \u2013 the orga decides')
    rankings = []
    for ranking in full['rankings']:
        # A table that is still being played has no ties to decide yet.
        running = bool(ranking['open_matches'])
        entries = [
            {
                'name': entry['name'],
                'rank': entry['rank'],
                'rank_label': entry['rank_label'],
                'shared': entry['shared'],
                'status': 'open' if running else entry['status'],
                'status_label': (
                    strings['status_open']
                    if running
                    else tie_label
                    if entry['status'] == 'tie'
                    else entry['status_label']
                ),
                'decided_by': entry['decided_by'],
                'decided_by_label': entry['decided_by_label'],
                'stats': entry['stats'],
                'value': entry['value'],
            }
            for entry in ranking['entries']
        ]
        rankings.append(
            {
                'scope': ranking['scope'],
                'label': ranking['label'],
                'open_matches': ranking['open_matches'],
                'cut': ranking['cut'],
                'has_tie': any(e['status'] == 'tie' for e in entries),
                'entries': entries,
            }
        )
    return rankings


def playoff_waiting_reason(
    state: QualificationState, tournament: Tournament
) -> str | None:
    """Return why the playoffs have not started, or `None` once released.

    Exactly one of `groups`, `leaderboard`, `tie` or `release`.
    """
    if state.released_at is not None:
        return None
    if state.source == 'leaderboard':
        if tournament.leaderboard_closed_at is None:
            return 'leaderboard'
    elif state.open_match_count or not state.total_match_count:
        return 'groups'
    if state.blockers:
        return 'tie'
    return 'release'


def playoff_origin_labels(state: QualificationState) -> dict[str, str]:
    """Return the group and place each qualifier came from, like `A1`."""
    labels: dict[str, str] = {}
    for qualifier in state.qualifiers or ():
        found = re.fullmatch(r'group:([0-9]+)', qualifier.scope)
        if found is None:
            continue
        letter = chr(ord('A') + int(found.group(1)))
        labels[qualifier.contestant_id] = f'{letter}{qualifier.rank}'
    return labels


def phase_match_labels(
    tournament: Tournament, matches: Sequence[TournamentMatch]
) -> dict[str, str]:
    """Return the phase label of each match by ID, like `Group B`."""
    playoff_matches = [m for m in matches if m.phase == 2]
    last_round: dict[Any, int] = {}
    for match in playoff_matches:
        last_round[match.bracket] = max(
            last_round.get(match.bracket, 0), match.round or 0
        )
    first_ffa_round = min((m.round or 0 for m in playoff_matches), default=0)
    is_ffa = tournament.playoff_game_format == GameFormat.FREE_FOR_ALL

    def playoff_label(match: TournamentMatch) -> str:
        number = (match.match_order or 0) + 1
        if is_ffa:
            return gettext(
                'Round %(num)s', num=(match.round or 0) - first_ffa_round + 1
            )
        bracket = match.bracket.value if match.bracket else None
        if bracket == 'GF':
            return gettext('Grand Final')
        if bracket == 'P3':
            return gettext('3rd Place')
        prefix = {'WB': 'WB ', 'LB': 'LB '}.get(bracket or '', '')
        from_end = last_round.get(match.bracket, 0) - (match.round or 0)
        if not prefix and from_end == 0:
            return gettext('Final')
        names = {1: gettext('Semifinal'), 2: gettext('Quarterfinal')}
        if not prefix and from_end in names:
            return f'{names[from_end]} {number}'
        return f'{prefix}{gettext("Round %(num)s", num=(match.round or 0) + 1)}'

    labels: dict[str, str] = {}
    for match in matches:
        if match.phase == 2:
            labels[str(match.id)] = gettext(
                'Playoffs \u00b7 %(label)s', label=playoff_label(match)
            )
        else:
            labels[str(match.id)] = gettext(
                'Group %(letter)s',
                letter=chr(ord('A') + (match.group_order or 0)),
            )
    return labels
