"""
byceps.services.lan_tournament.tournament_operational_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Flush-only operations. The caller owns the transaction (rollback, commit
and signals) and never calls them from a read path.
"""

from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import datetime
from uuid import UUID

from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import tournament_repository
from .models.game_format import GameFormat
from .models.match_readiness import real_contestants
from .models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    OperationalClock,
)
from .models.tournament import Tournament, TournamentID
from .models.tournament_match import TournamentMatchID
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from .tournament_domain_service import game_format_for_phase
from .tournament_operational_domain_service import (
    clock_value_us,
    derive_due_match_ids,
    normalize_utc,
    pairing_key,
)


LOBBY_NOT_FREE_FOR_ALL_ERROR = 'lobby_not_free_for_all'
LOBBY_ROSTER_INCOMPLETE_ERROR = 'lobby_roster_incomplete'


def _clock_of(tournament: Tournament) -> OperationalClock:
    return OperationalClock(
        elapsed_us=tournament.operational_clock_elapsed_us,
        running_since=tournament.operational_clock_running_since,
        activated_at=tournament.operational_clock_activated_at,
    )


def _fresh_tournament(tournament_id: TournamentID) -> Result[Tournament, str]:
    try:
        return Ok(
            tournament_repository.get_tournament(tournament_id, fresh=True)
        )
    except ValueError:
        return Err('tournament_not_found')


def _match_id_set(
    match_ids: Collection[TournamentMatchID],
) -> frozenset[TournamentMatchID]:
    """Return the IDs as UUIDs, because a request may supply strings."""
    return frozenset(TournamentMatchID(UUID(str(i))) for i in match_ids)


def _lock_tournaments_first(
    match_ids: frozenset[TournamentMatchID],
) -> Result[
    dict[TournamentID, tuple[Tournament, list[TournamentMatchID]]], str
]:
    """Lock the tournaments of those matches, then the matches.

    Return each tournament, freshly read, with its share of the matches.
    """
    found = tournament_repository.get_matches_by_ids(list(match_ids))
    if frozenset(m.id for m in found) != match_ids:
        return Err('match_not_found')

    ids_by_tournament: defaultdict[TournamentID, list[TournamentMatchID]] = (
        defaultdict(list)
    )
    for match in found:
        ids_by_tournament[match.tournament_id].append(match.id)

    tournament_ids = sorted(ids_by_tournament, key=str)
    for tournament_id in tournament_ids:
        tournament_repository.lock_tournament_for_update(tournament_id)

    locked: dict[TournamentID, tuple[Tournament, list[TournamentMatchID]]] = {}
    for tournament_id in tournament_ids:
        result = _fresh_tournament(tournament_id)
        if result.is_err():
            return Err(result.unwrap_err())
        locked[tournament_id] = (
            result.unwrap(),
            ids_by_tournament[tournament_id],
        )

    tournament_repository.lock_matches_for_update(sorted(m.id for m in found))
    return Ok(locked)


def reconcile_due_matches_flush(
    tournament_id: TournamentID, *, occurred_at: datetime
) -> Result[None, str]:
    """Bring the open due episodes in line with the due matches.

    Run it after the final roster is assembled and before the owner
    commits. An episode that is no longer due, or whose pairing changed,
    is closed at the current clock. A due match without an open episode
    gets a new one. A due match whose episode still fits keeps it, and
    with it the acknowledgements. A tournament without a known clock
    history gets nothing, because that history is never invented.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament_result = _fresh_tournament(tournament_id)
    if tournament_result.is_err():
        return Err(tournament_result.unwrap_err())
    tournament = tournament_result.unwrap()
    tournament_id = tournament.id

    clock = _clock_of(tournament)
    if clock.activated_at is None:
        return Ok(None)

    matches = tournament_repository.get_matches_for_tournament_ordered_fresh(
        tournament_id
    )
    match_ids = [m.id for m in matches]
    tournament_repository.lock_matches_for_update(match_ids)
    contestants = tournament_repository.get_contestants_for_matches(match_ids)

    completed_lobby_ids = frozenset(
        m.id for m in matches if m.occupied_since is not None
    )
    due_ids = derive_due_match_ids(
        tournament, matches, contestants, completed_lobby_ids
    )
    keys = {i: pairing_key(contestants.get(i, ())) for i in due_ids}

    open_episodes = {
        e.match_id: e
        for e in tournament_repository.list_open_due_episodes(tournament_id)
    }
    obsolete_ids = [
        match_id
        for match_id, episode in open_episodes.items()
        if keys.get(match_id) != episode.pairing_key
    ]
    kept_ids = open_episodes.keys() - set(obsolete_ids)

    at = normalize_utc(occurred_at)
    clock_us = clock_value_us(clock, at)
    if obsolete_ids:
        tournament_repository.close_due_episodes_flush(
            obsolete_ids, occurred_at=at, clock_us=clock_us
        )
    for match in matches:
        if match.id in due_ids and match.id not in kept_ids:
            tournament_repository.open_due_episode_flush(
                MatchDueEpisode(
                    id=MatchDueEpisodeID(generate_uuid7()),
                    tournament_id=tournament_id,
                    match_id=match.id,
                    pairing_key=keys[match.id],
                    opened_at=at,
                    opened_clock_us=clock_us,
                )
            )

    return Ok(None)


def invalidate_due_matches_flush(
    match_ids: Collection[TournamentMatchID], *, occurred_at: datetime
) -> Result[None, str]:
    """Close the open episodes of matches whose matchup is replaced.

    Call it before the destructive change, even when the change may end
    in the same pairing again: a restored pairing is a new demand, and
    its episode must not inherit the acknowledgements of the old one.
    The next reconcile opens the new episode.
    """
    wanted = _match_id_set(match_ids)
    if not wanted:
        return Ok(None)

    locked_result = _lock_tournaments_first(wanted)
    if locked_result.is_err():
        return Err(locked_result.unwrap_err())

    at = normalize_utc(occurred_at)
    for tournament, ids in locked_result.unwrap().values():
        clock = _clock_of(tournament)
        if clock.activated_at is None:
            continue

        tournament_repository.close_due_episodes_flush(
            ids, occurred_at=at, clock_us=clock_value_us(clock, at)
        )

    return Ok(None)


def _lobby_roster_is_complete(
    tournament: Tournament,
    contestants: Sequence[TournamentMatchToContestant],
    *,
    allow_undersized: bool,
) -> bool:
    """Tell whether the lobby holds the roster its generator assembled.

    A lone contestant never makes a lobby. A lobby below the minimum
    size is complete only if its generator planned the shortfall.
    """
    count = len(real_contestants(contestants))
    if count < 2:
        return False

    return allow_undersized or count >= (tournament.group_size_min or 2)


def mark_completed_lobbies_occupied_flush(
    match_ids: Collection[TournamentMatchID],
    *,
    occurred_at: datetime,
    allow_undersized: bool = False,
) -> Result[None, str]:
    """Accept only free-for-all lobbies whose roster is complete.

    The generator calls it once it has assembled the lobbies, never
    after each inserted contestant: two inserted rows are not a roster.
    `allow_undersized` says that the generator planned lobbies below
    the minimum size. A lobby that is not accepted refuses the whole
    call, and the owner rolls back.

    Every accepted lobby gets its occupancy at `occurred_at`, unless it
    has one already: the original occupancy is never reset. Call it
    before `reconcile_due_matches_flush`, which reads the occupancy as
    the completed-lobby fact.
    """
    wanted = _match_id_set(match_ids)
    if not wanted:
        return Ok(None)

    locked_result = _lock_tournaments_first(wanted)
    if locked_result.is_err():
        return Err(locked_result.unwrap_err())

    accepted: list[TournamentMatchID] = []
    for tournament, ids in locked_result.unwrap().values():
        lobbies = [
            m
            for m in tournament_repository.get_matches_for_tournament_ordered_fresh(
                tournament.id
            )
            if m.id in ids
        ]
        contestants = tournament_repository.get_contestants_for_matches(ids)
        for lobby in lobbies:
            if (
                game_format_for_phase(tournament, lobby.phase)
                != GameFormat.FREE_FOR_ALL
            ):
                return Err(LOBBY_NOT_FREE_FOR_ALL_ERROR)
            if lobby.confirmed_by is not None:
                return Err('match_confirmed')
            if not _lobby_roster_is_complete(
                tournament,
                contestants.get(lobby.id, []),
                allow_undersized=allow_undersized,
            ):
                return Err(LOBBY_ROSTER_INCOMPLETE_ERROR)
            accepted.append(lobby.id)

    at = normalize_utc(occurred_at)
    for lobby_id in accepted:
        tournament_repository.set_ffa_lobby_occupied_since_if_unset_flush(
            lobby_id, at
        )

    return Ok(None)
