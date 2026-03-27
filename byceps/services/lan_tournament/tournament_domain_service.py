from datetime import datetime, UTC
from math import ceil

from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from .events import (
    TournamentCreatedEvent,
    TournamentStatusChangedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.score_ordering import ScoreOrdering
from .models.game_format import GameFormat
from .models.elimination_mode import EliminationMode
from .models.round_robin_standing import RoundRobinStanding
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from .models.tournament_status import TournamentStatus


_VALID_STATUS_TRANSITIONS: dict[TournamentStatus, set[TournamentStatus]] = {
    TournamentStatus.DRAFT: {
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.REGISTRATION_OPEN: {
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.REGISTRATION_CLOSED: {
        TournamentStatus.ONGOING,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.ONGOING: {
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.PAUSED: {
        TournamentStatus.ONGOING,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.COMPLETED: set(),
    TournamentStatus.CANCELLED: set(),
}


def create_tournament(
    party_id: PartyID,
    name: str,
    *,
    game: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    ruleset: str | None = None,
    start_time: datetime | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    min_teams: int | None = None,
    max_teams: int | None = None,
    min_players_in_team: int | None = None,
    max_players_in_team: int | None = None,
    contestant_type: ContestantType | None = None,
    tournament_status: TournamentStatus | None = None,
    game_format: GameFormat | None = None,
    elimination_mode: EliminationMode | None = None,
    score_ordering: ScoreOrdering | None = None,
    point_table: list[int] | None = None,
    advancement_count: int | None = None,
    group_size_min: int | None = None,
    group_size_max: int | None = None,
    points_carry_to_losers: bool | None = None,
    position: int = 0,
) -> tuple[Tournament, TournamentCreatedEvent]:
    """Create a new tournament."""
    tournament_id = TournamentID(generate_uuid7())
    now = datetime.now(UTC)

    tournament = Tournament(
        id=tournament_id,
        party_id=party_id,
        name=name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        created_at=now,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_status=tournament_status,
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        point_table=point_table,
        advancement_count=advancement_count,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        points_carry_to_losers=points_carry_to_losers,
        position=position,
    )

    event = TournamentCreatedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
    )

    return tournament, event


def validate_status_transition(
    current_status: TournamentStatus | None,
    new_status: TournamentStatus,
) -> Result[TournamentStatus, str]:
    """Validate that a status transition is allowed."""
    if current_status is None:
        return Ok(new_status)

    allowed = _VALID_STATUS_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        return Err(
            f'Cannot transition from {current_status.name}'
            f' to {new_status.name}.'
        )

    return Ok(new_status)


def change_tournament_status(
    tournament: Tournament,
    new_status: TournamentStatus,
) -> Result[tuple[TournamentStatusChangedEvent], str]:
    """Validate and produce a status change event."""
    result = validate_status_transition(
        tournament.tournament_status, new_status
    )
    if result.is_err():
        return Err(result.unwrap_err())

    now = datetime.now(UTC)

    event = TournamentStatusChangedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament.id,
        old_status=tournament.tournament_status,
        new_status=new_status,
    )

    return Ok((event,))


def validate_participant_count(
    tournament: Tournament,
    current_count: int,
) -> Result[None, str]:
    """Check if the tournament can accept more participants."""
    if (
        tournament.max_players is not None
        and current_count >= tournament.max_players
    ):
        return Err('Tournament is full.')

    return Ok(None)


def validate_team_count(
    tournament: Tournament,
    current_count: int,
) -> Result[None, str]:
    """Check if the tournament can accept more teams."""
    if (
        tournament.max_teams is not None
        and current_count >= tournament.max_teams
    ):
        return Err('Maximum number of teams reached.')

    return Ok(None)


def determine_match_winner(
    contestants: list[TournamentMatchToContestant],
) -> Result[TournamentMatchToContestant | None, str]:
    """Determine the winner of a match by highest score.

    Returns ``Ok(winner)`` when there is a clear winner,
    ``Ok(None)`` when the match is a draw (equal scores),
    or ``Err(reason)`` on validation failure.
    """
    if len(contestants) < 2:
        return Err('Need at least 2 contestants to determine winner.')

    for contestant in contestants:
        if contestant.score is None:
            return Err('All contestants must have scores.')

    sorted_contestants = sorted(
        contestants, key=lambda c: c.score, reverse=True
    )

    if sorted_contestants[0].score == sorted_contestants[1].score:
        return Ok(None)

    return Ok(sorted_contestants[0])


def generate_round_robin_schedule(
    contestant_ids: list[str],
) -> list[list[tuple[str | None, str | None]]]:
    """Generate round-robin schedule using circle method."""
    players: list[str | None] = list(contestant_ids)
    if len(players) % 2 == 1:
        players.append(None)  # defwin

    n = len(players)
    num_rounds = n - 1
    schedule: list[list[tuple[str | None, str | None]]] = []

    for _round_num in range(num_rounds):
        round_matches: list[tuple[str | None, str | None]] = []
        top = players[: n // 2]
        bottom = players[n // 2 :][::-1]
        for p1, p2 in zip(top, bottom, strict=True):
            if p1 is not None and p2 is not None:
                round_matches.append((p1, p2))
        schedule.append(round_matches)
        players = [players[0]] + [players[-1]] + players[1:-1]

    return schedule


def contestant_id(
    c: TournamentMatchToContestant,
) -> str:
    """Return the effective contestant ID as a string."""
    if c.participant_id is None and c.team_id is None:
        raise ValueError(
            'Contestant has neither participant_id'
            ' nor team_id.'
        )
    return str(
        c.participant_id
        if c.participant_id is not None
        else c.team_id
    )


def compute_round_robin_standings(
    matches: list[list[TournamentMatchToContestant]],
) -> list[RoundRobinStanding]:
    """Compute round-robin standings from confirmed matches.

    Each element in *matches* is a list of exactly two
    ``TournamentMatchToContestant`` entries representing one
    completed match.  Points are awarded as: Win = 3, Draw = 1,
    Loss = 0.  The returned list is sorted by points DESC,
    score_diff DESC, score_for DESC.
    """
    stats: dict[str, dict[str, int]] = {}

    def _ensure(cid: str) -> dict[str, int]:
        if cid not in stats:
            stats[cid] = {
                'points': 0,
                'wins': 0,
                'draws': 0,
                'losses': 0,
                'score_for': 0,
                'score_against': 0,
            }
        return stats[cid]

    for contestants in matches:
        if len(contestants) != 2:
            continue

        c1, c2 = contestants
        cid1 = contestant_id(c1)
        cid2 = contestant_id(c2)

        s1 = _ensure(cid1)
        s2 = _ensure(cid2)

        score1 = c1.score if c1.score is not None else 0
        score2 = c2.score if c2.score is not None else 0

        s1['score_for'] += score1
        s1['score_against'] += score2
        s2['score_for'] += score2
        s2['score_against'] += score1

        winner_result = determine_match_winner(contestants)
        if winner_result.is_err():
            # Validation failure — skip this match.
            continue

        winner = winner_result.unwrap()
        if winner is None:
            # Draw (equal scores).
            s1['draws'] += 1
            s1['points'] += 1
            s2['draws'] += 1
            s2['points'] += 1
        else:
            winner_id = contestant_id(winner)
            if winner_id == cid1:
                s1['wins'] += 1
                s1['points'] += 3
                s2['losses'] += 1
            else:
                s2['wins'] += 1
                s2['points'] += 3
                s1['losses'] += 1

    standings = [
        RoundRobinStanding(
            contestant_id=cid,
            points=s['points'],
            wins=s['wins'],
            draws=s['draws'],
            losses=s['losses'],
            score_for=s['score_for'],
            score_against=s['score_against'],
            score_diff=s['score_for'] - s['score_against'],
        )
        for cid, s in stats.items()
    ]

    standings.sort(
        key=lambda s: (s.points, s.score_diff, s.score_for),
        reverse=True,
    )

    return standings


def _standard_seed_order(bracket_size: int) -> list[int]:
    """Return standard seeding order for single elimination.

    For bracket_size=8: [0, 7, 3, 4, 1, 6, 2, 5]
    This produces matchups: 1v8, 4v5, 2v7, 3v6 (1-indexed).
    """
    if bracket_size == 1:
        return [0]

    if bracket_size == 2:
        return [0, 1]

    half = bracket_size // 2
    prev = _standard_seed_order(half)
    result = []
    for seed in prev:
        result.append(seed)
        result.append(bracket_size - 1 - seed)
    return result


# -------------------------------------------------------------------- #
# FFA domain logic
# -------------------------------------------------------------------- #


def snake_seed_groups(
    contestant_ids: list[str],
    group_size_min: int,
    group_size_max: int,
) -> Result[list[list[str]], str]:
    """Distribute contestants into groups using serpentine/snake ordering.

    Groups are as equal as possible (sizes differ by at most 1).
    Returns ``Err`` when the input is empty or any resulting group
    would be smaller than *group_size_min*.
    """
    if not contestant_ids:
        return Err('No contestants to distribute.')

    num_groups = ceil(len(contestant_ids) / group_size_max)
    groups: list[list[str]] = [[] for _ in range(num_groups)]

    for row_idx, cid in enumerate(contestant_ids):
        group_in_row = row_idx % num_groups
        row_number = row_idx // num_groups
        # Even rows go L->R, odd rows go R->L (serpentine).
        if row_number % 2 == 0:
            target = group_in_row
        else:
            target = num_groups - 1 - group_in_row
        groups[target].append(cid)

    # Validate minimum group size.
    for idx, group in enumerate(groups):
        if len(group) < group_size_min:
            return Err(
                f'Group {idx} has {len(group)} contestants,'
                f' below the minimum of {group_size_min}.'
            )

    return Ok(groups)


def map_placement_to_points(placement: int, point_table: list[int]) -> int:
    """Map a 1-indexed placement to points from the given table.

    Returns ``0`` for placements beyond the table length.
    """
    idx = placement - 1
    if 0 <= idx < len(point_table):
        return point_table[idx]
    return 0


def compute_ffa_round_standings(
    matches: list[list[TournamentMatchToContestant]],
) -> list[tuple[str, int]]:
    """Compute per-player standings from a single FFA round.

    *matches* is a list of groups, where each group is a list of
    ``TournamentMatchToContestant`` entries with ``placement`` and
    ``points`` already set.

    Returns a list of ``(contestant_id, points)`` tuples sorted by
    points descending.
    """
    standings: dict[str, int] = {}

    for group in matches:
        for contestant in group:
            cid = contestant_id(contestant)
            pts = contestant.points if contestant.points is not None else 0
            standings[cid] = standings.get(cid, 0) + pts

    result = sorted(standings.items(), key=lambda item: item[1], reverse=True)
    return result


def compute_ffa_cumulative_standings(
    all_round_matches: list[list[list[TournamentMatchToContestant]]],
) -> list[tuple[str, int]]:
    """Aggregate FFA standings across all rounds.

    *all_round_matches* is a list of rounds, where each round is
    structured as in :func:`compute_ffa_round_standings`.

    Returns a list of ``(contestant_id, total_points)`` tuples sorted
    by total points descending.
    """
    cumulative: dict[str, int] = {}

    for round_matches in all_round_matches:
        round_standings = compute_ffa_round_standings(round_matches)
        for cid, pts in round_standings:
            cumulative[cid] = cumulative.get(cid, 0) + pts

    result = sorted(
        cumulative.items(), key=lambda item: item[1], reverse=True
    )
    return result
