from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any
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
    tournament_participant_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.game_format import (
    VALID_COMBINATIONS,
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
    MAX_POINT_TABLE_PLACES,
    MAX_POINTS_PER_PLACE,
    compute_ffa_cumulative_standings,
    compute_ffa_round_standings,
    compute_round_robin_standings,
)
from byceps.services.lan_tournament.tournament_request_domain_service import (
    MAX_PARTICIPANT_LIMIT,
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


def is_ffa_tournament(tournament: Tournament) -> bool:
    """Return `True` if the tournament's matches are decided by placement."""
    return (
        tournament.game_format is not None
        and tournament.game_format.uses_placements
    )


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
    ('from_request_id', 'submission_token'),
)
# `req_map` is a client-only pseudo-field (the request-values decision).
# It has no form field; it is listed so that the JS and Python step
# tables stay identical.

_REVIEW_STEP = len(CREATE_WIZARD_STEP_FIELDS) - 1

_UPLOAD_BYTES = 5 * 1024 * 1024


def first_error_step(form: Form) -> int | None:
    """Return the index of the earliest step that holds a form error."""
    errors = form.errors
    if not errors:
        return None

    for index, field_names in enumerate(CREATE_WIZARD_STEP_FIELDS):
        if any(name in errors for name in field_names):
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
