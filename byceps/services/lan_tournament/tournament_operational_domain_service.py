"""
byceps.services.lan_tournament.tournament_operational_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, UTC

from byceps.util.result import Err, Ok, Result

from .models.bracket import Bracket
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.match_readiness import real_contestants
from .models.operational_timing import OperationalClock, TrafficTier
from .models.tournament import Tournament
from .models.tournament_dashboard import DashboardSettings
from .models.tournament_match import TournamentMatch, TournamentMatchID
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from .models.tournament_status import TournamentStatus
from .tournament_domain_service import (
    elimination_mode_for_phase,
    game_format_for_phase,
)


_ContestantsByMatch = Mapping[
    TournamentMatchID, Sequence[TournamentMatchToContestant]
]

UNSUPPORTED_STARTED_CREATION_ERROR = 'unsupported_started_creation'
UNSUPPORTED_STATUS_TRANSITION_ERROR = 'unsupported_status_transition'

_MICROSECONDS_PER_MINUTE = 60_000_000

_PRE_START_STATUSES = frozenset(
    {
        TournamentStatus.DRAFT,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    }
)
_STARTED_STATUSES = frozenset(
    {
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
    }
)
_RESUMABLE_STATUSES = frozenset(
    {TournamentStatus.PAUSED, TournamentStatus.COMPLETED}
)
# A paused tournament keeps its due matches, so that their episodes live on.
_DEMAND_STATUSES = frozenset(
    {TournamentStatus.ONGOING, TournamentStatus.PAUSED}
)
_ELIMINATION_MODES = frozenset(
    {EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION}
)


def normalize_utc(value: datetime) -> datetime:
    """Return the moment as naive UTC, the plain `TIMESTAMP` convention.

    A naive value already is UTC. An aware value is converted.
    """
    if value.tzinfo is None:
        return value

    return value.astimezone(UTC).replace(tzinfo=None)


def _microseconds_between(start: datetime, end: datetime) -> int:
    delta = end - start
    return (
        delta.days * 86_400 + delta.seconds
    ) * 1_000_000 + delta.microseconds


def clock_value_us(clock: OperationalClock, at: datetime) -> int:
    """Return the active microseconds of the clock at the moment `at`.

    A moment before `running_since` adds nothing, so the value never
    drops below the accumulated time.
    """
    if clock.running_since is None:
        return clock.elapsed_us

    running = _microseconds_between(
        normalize_utc(clock.running_since), normalize_utc(at)
    )
    return clock.elapsed_us + max(running, 0)


def transition_clock(
    clock: OperationalClock,
    old: TournamentStatus | None,
    new: TournamentStatus,
    at: datetime,
) -> Result[OperationalClock, str]:
    """Return the clock after a status change at the moment `at`.

    Only `ONGOING` runs. Leaving it freezes the accumulated time, and
    the start from `REGISTRATION_CLOSED` activates the clock. A resume
    from `PAUSED` or `COMPLETED` continues it, except for a clock
    without a known history, which never shows invented time. A status
    that is started without any prior status is refused, because its
    start is unknown.
    """
    if old is new:
        return Ok(clock)

    at = normalize_utc(at)

    if old is None:
        if new in _STARTED_STATUSES:
            return Err(UNSUPPORTED_STARTED_CREATION_ERROR)
        return Ok(clock)

    was_running = old is TournamentStatus.ONGOING
    will_run = new is TournamentStatus.ONGOING

    if was_running:
        return Ok(
            replace(
                clock,
                elapsed_us=clock_value_us(clock, at),
                running_since=None,
            )
        )

    if not will_run:
        return Ok(clock)

    if old is TournamentStatus.REGISTRATION_CLOSED:
        return Ok(
            replace(
                clock,
                running_since=at,
                activated_at=(
                    clock.activated_at if clock.activated_at is not None else at
                ),
            )
        )

    if old not in _RESUMABLE_STATUSES:
        return Err(UNSUPPORTED_STATUS_TRANSITION_ERROR)

    if clock.activated_at is None:
        return Ok(clock)

    return Ok(replace(clock, running_since=at))


def pairing_key(
    contestants: Sequence[TournamentMatchToContestant],
) -> str:
    """Return a stable key of who plays a match, whatever the row order."""
    identities = sorted(
        f'participant:{c.participant_id}'
        if c.participant_id is not None
        else f'team:{c.team_id}'
        for c in real_contestants(contestants)
    )
    return '|'.join(identities)


def derive_traffic_tier(
    alert_wait_us: int, settings: DashboardSettings
) -> TrafficTier:
    """Return the tier of an alert interval, which starts at the minute."""
    if alert_wait_us >= settings.red_minutes * _MICROSECONDS_PER_MINUTE:
        return TrafficTier.RED

    if alert_wait_us >= settings.yellow_minutes * _MICROSECONDS_PER_MINUTE:
        return TrafficTier.YELLOW

    return TrafficTier.GREEN


def derive_due_match_ids(
    tournament: Tournament,
    matches: Sequence[TournamentMatch],
    contestants_by_match: _ContestantsByMatch,
    completed_lobby_ids: frozenset[TournamentMatchID],
) -> frozenset[TournamentMatchID]:
    """Return the IDs of the matches that currently demand attention.

    `matches` are those of the tournament. Only a running or paused
    tournament has demand. Each phase follows the format it runs, so a
    highscore phase has no matches. Future rounds, one-sided matches and
    lobbies that are not complete are not due. `completed_lobby_ids` are
    the free-for-all lobbies whose roster is assembled.
    """
    if tournament.tournament_status not in _DEMAND_STATUSES:
        return frozenset()

    matches_by_phase: dict[int, list[TournamentMatch]] = defaultdict(list)
    for match in matches:
        matches_by_phase[match.phase].append(match)

    due: set[TournamentMatchID] = set()
    for phase, phase_matches in matches_by_phase.items():
        game_format = game_format_for_phase(tournament, phase)
        mode = elimination_mode_for_phase(tournament, phase)

        if game_format is GameFormat.ONE_V_ONE:
            if mode is EliminationMode.ROUND_ROBIN:
                due.update(
                    _round_robin_due(phase_matches, contestants_by_match)
                )
            elif mode in _ELIMINATION_MODES:
                due.update(_knockout_due(phase_matches, contestants_by_match))
        elif game_format is GameFormat.FREE_FOR_ALL:
            if mode in _ELIMINATION_MODES:
                due.update(
                    _free_for_all_due(
                        phase_matches,
                        contestants_by_match,
                        completed_lobby_ids,
                    )
                )

    return frozenset(due)


def _contestant_count(
    match: TournamentMatch,
    contestants_by_match: _ContestantsByMatch,
) -> int:
    return len(real_contestants(contestants_by_match.get(match.id, ())))


def _is_playable(
    match: TournamentMatch,
    contestants_by_match: _ContestantsByMatch,
) -> bool:
    """Tell whether an unconfirmed 1v1 match has both of its sides."""
    return (
        match.confirmed_by is None
        and _contestant_count(match, contestants_by_match) == 2
    )


def _knockout_due(
    matches: Iterable[TournamentMatch],
    contestants_by_match: _ContestantsByMatch,
) -> set[TournamentMatchID]:
    return {
        match.id
        for match in matches
        if _is_playable(match, contestants_by_match)
    }


def _round_robin_due(
    matches: Iterable[TournamentMatch],
    contestants_by_match: _ContestantsByMatch,
) -> set[TournamentMatchID]:
    """Return the playable matches of each group's earliest open round.

    The round counts as open while any of its matches is unconfirmed,
    playable or not, so a one-sided match holds the later rounds back.
    """
    open_matches_by_group: dict[int | None, list[TournamentMatch]] = (
        defaultdict(list)
    )
    for match in matches:
        if match.confirmed_by is None and match.round is not None:
            open_matches_by_group[match.group_order].append(match)

    due: set[TournamentMatchID] = set()
    for open_matches in open_matches_by_group.values():
        frontier = min(
            match.round for match in open_matches if match.round is not None
        )
        due.update(
            match.id
            for match in open_matches
            if match.round == frontier
            and _is_playable(match, contestants_by_match)
        )

    return due


def _free_for_all_due(
    matches: Sequence[TournamentMatch],
    contestants_by_match: _ContestantsByMatch,
    completed_lobby_ids: frozenset[TournamentMatchID],
) -> set[TournamentMatchID]:
    """Return the complete lobbies of each pool's latest round.

    A round that fed a later round is consumed: an earlier round of the
    pool, a winners round that the merged losers round replaced, and
    both pools once the grand final exists.
    """
    grand_final_exists = any(
        match.bracket is Bracket.GRAND_FINAL for match in matches
    )

    latest_round: dict[Bracket | None, int] = {}
    for match in matches:
        if match.round is not None:
            latest_round[match.bracket] = max(
                latest_round.get(match.bracket, match.round), match.round
            )

    merged_targets = {
        match.seeding_target
        for match in matches
        if match.bracket is Bracket.LOSERS and match.seeding_target
    }

    due: set[TournamentMatchID] = set()
    for match in matches:
        if (
            match.id not in completed_lobby_ids
            or match.confirmed_by is not None
        ):
            continue
        if _contestant_count(match, contestants_by_match) < 2:
            continue

        if match.bracket is not Bracket.GRAND_FINAL:
            if (
                match.round is None
                or match.round != latest_round[match.bracket]
            ):
                continue
            if match.bracket is not None and grand_final_exists:
                continue
            if (
                match.bracket is Bracket.WINNERS
                and _winners_target(match.round + 1) in merged_targets
            ):
                continue

        due.add(match.id)

    return due


def _winners_target(round_number: int) -> str:
    """Return the seeding target of a winners round, e.g. `ffa:WB:1`."""
    return f'ffa:WB:{round_number}'
