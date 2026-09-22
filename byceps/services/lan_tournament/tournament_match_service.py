import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import tournament_repository
from .events import (
    ContestantAdvancedEvent,
    MatchConfirmedEvent,
    MatchCreatedEvent,
    MatchDeletedEvent,
    MatchReadyEvent,
    MatchUnconfirmedEvent,
    TournamentCompletedEvent,
    TournamentUncompletedEvent,
)
from .models.tournament import Tournament, TournamentID
from .models.bracket import Bracket
from .models.tournament_match import (
    CorrectionCase,
    MatchUserRole,
    TournamentMatch,
    TournamentMatchID,
)
from .models.game_format import GameFormat
from .models.elimination_mode import EliminationMode
from .models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_seed import TournamentSeed
from .models.tournament_team import TournamentTeamID
from .models.tournament_status import TournamentStatus
from .signals import (
    contestant_advanced,
    match_confirmed,
    match_created,
    match_deleted,
    match_ready,
    match_unconfirmed,
    tournament_completed,
    tournament_uncompleted,
)
from .models.contestant_type import ContestantType
from .tournament_domain_service import (
    contestant_id,
    compute_ffa_cumulative_standings,
    determine_match_winner,
    generate_round_robin_schedule,
    map_placement_to_points,
    snake_seed_groups,
)
from .tournament_log_service import create_log_entry

logger = logging.getLogger(__name__)


MAX_MATCH_SCORE = 999_999_999

# A STATIC msgid, not an f-string over MAX_MATCH_SCORE. Both admin
# and site views flash this through gettext(), and these service
# errors are hand-added catalogue entries -- a computed string
# matches no msgid and renders in English inside a German flash.
# Spelling the limit out matches the site view's own
# 'Score must be between 0 and 999,999,999.', the entry already in
# the catalogue. test_max_match_score_error_states_the_real_limit
# fails if the two ever drift apart.
MAX_MATCH_SCORE_ERROR = 'Score cannot exceed 999,999,999.'

# A STATIC msgid, for the same reason as MAX_MATCH_SCORE_ERROR above.
PLACEMENT_FORMAT_CONFIRM_ERROR = (
    'Free-for-all matches are decided by placements, not by scores.'
)

# Already in the catalogue; the two views flash the same sentence.
PLACEMENT_FORMAT_CORRECTION_ERROR = (
    'Free-for-all matches are corrected by unconfirming '
    'them and re-entering the placements.'
)


def _decided_by_placements(tournament: Tournament) -> bool:
    """Return `True` if the tournament's matches are decided by placement.

    The predicate the views spell as ``is_ffa_tournament``. It lives
    here too because the format split has to be enforced in the
    service: the views' own checks only cover the forms they render,
    and every write path below is reachable by a direct POST.

    The ``isinstance`` is load-bearing, not a type-checker sop. This
    gate now decides whether a match may be confirmed at all, and the
    unit tests in this module drive the service against Mock
    repositories whose ``game_format`` is a ``Mock``: reading
    ``.uses_placements`` off one yields a truthy ``Mock``, which would
    make every stubbed tournament look like a free-for-all and refuse
    every confirmation. A guard that a test double can flip is a
    guard that a future refactor can flip too. ``GameFormat`` stays
    the source of truth for what "decided by placements" means --
    this only refuses to answer for something that is not one.
    """
    return (
        isinstance(tournament.game_format, GameFormat)
        and tournament.game_format.uses_placements
    )


class DefwinResult(NamedTuple):
    """Events produced by defwin processing, for post-commit dispatch."""

    advanced: list[ContestantAdvancedEvent]
    confirmed: list[MatchConfirmedEvent]
    completed: list[TournamentCompletedEvent]


def set_seed(
    seed_list: list[TournamentSeed],
    tournament_id: TournamentID,
) -> Result[None, str]:
    """Set seeding for a tournament."""
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
    from .models.contestant_type import ContestantType

    # Get tournament to check contestant type
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        return Err('Tournament contestant type is not set.')

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    now = datetime.now(UTC)

    match_events: list[MatchCreatedEvent] = []

    # Create matches and contestants for each seed
    for seed in seed_list:
        # Create the match
        match_id = TournamentMatchID(generate_uuid7())
        match = TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=seed.match_order,
            round=seed.round,
            next_match_id=None,
            confirmed_by=None,
            created_at=now,
        )
        tournament_repository.create_match(match)

        match_event = MatchCreatedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=tournament_id,
            match_id=match_id,
        )
        match_events.append(match_event)

        # Create contestants for entry_a and entry_b
        # Skip if entry is "DEFWIN"
        if seed.entry_a.upper() != 'DEFWIN':
            contestant_a_id = TournamentMatchToContestantID(generate_uuid7())
            if is_team_tournament:
                contestant_a = TournamentMatchToContestant(
                    id=contestant_a_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(seed.entry_a)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                contestant_a = TournamentMatchToContestant(
                    id=contestant_a_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(UUID(seed.entry_a)),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant_a)

        if seed.entry_b.upper() != 'DEFWIN':
            contestant_b_id = TournamentMatchToContestantID(generate_uuid7())
            if is_team_tournament:
                contestant_b = TournamentMatchToContestant(
                    id=contestant_b_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(seed.entry_b)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                contestant_b = TournamentMatchToContestant(
                    id=contestant_b_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(UUID(seed.entry_b)),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant_b)

    # Commit entire seeding as a single transaction
    tournament_repository.commit_session()

    # Dispatch events after successful commit
    for match_event in match_events:
        signals.match_created.send(None, event=match_event)

    seeded_match_ids = {e.match_id for e in match_events}
    ready_events = _collect_ready_match_events(seeded_match_ids, tournament_id, now)
    for event in ready_events:
        match_ready.send(None, event=event)

    return Ok(None)


def has_matches(tournament_id: TournamentID) -> bool:
    """Check if tournament already has matches."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    return len(matches) > 0


def clear_bracket(
    tournament_id: TournamentID,
    *,
    initiator_id: UserID | None = None,
) -> list[MatchDeletedEvent]:
    """Delete the bracket (flush only); return the events to dispatch."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    if not matches:
        return []

    now = datetime.now(UTC)

    # Collect event data before deletion (IDs won't be accessible after).
    match_ids = [m.id for m in matches]

    confirmed_ids = [m.id for m in matches if m.confirmed_by is not None]
    contestants_by_match_id = tournament_repository.get_contestants_for_matches(
        confirmed_ids
    )
    create_log_entry(
        'bracket-cleared',
        tournament_id,
        initiator_id,
        data={
            'match_count': len(matches),
            'confirmed_results': {
                str(match_id): _snapshot_contestant_scores(
                    match_id,
                    contestants=contestants_by_match_id.get(match_id, []),
                )
                for match_id in confirmed_ids
            },
        },
        commit=False,
    )

    # NULL out self-referential FKs first to avoid IntegrityError
    # on PostgreSQL (next_match_id / loser_next_match_id point to
    # sibling rows in the same table).
    tournament_repository.null_self_referential_fks(tournament_id)

    # Delete children (comments, contestants) then matches.
    for match_id in match_ids:
        tournament_repository.delete_comments_for_match_flush(match_id)
        tournament_repository.delete_contestants_for_match_flush(match_id)
        tournament_repository.delete_match_flush(match_id)

    return [
        MatchDeletedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=tournament_id,
            match_id=match_id,
        )
        for match_id in match_ids
    ]


def _prepare_bracket_generation(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
    check: Callable[[Tournament, list[str]], Result[None, str]] | None = None,
) -> Result[tuple[Tournament, list[str], list[MatchDeletedEvent]], str]:
    """Shared preamble for bracket generation functions.

    Locks the tournament, validates contestant type, fetches
    contestant IDs, checks minimum count and the generator's own
    *check*, and only then clears existing matches when
    *force_regenerate* is set (flush only).

    Returns ``Ok((tournament, contestant_ids, deleted_events))``
    on success or ``Err(reason)`` on failure.
    """
    from .models.contestant_type import ContestantType

    # Lock tournament to prevent race conditions.
    tournament_repository.lock_tournament_for_update(tournament_id)

    # Check if matches already exist (atomic with lock).
    had_matches = has_matches(tournament_id)
    if had_matches and not force_regenerate:
        return Err(
            'Tournament already has matches.'
            ' Use force regenerate to clear'
            ' and rebuild.'
        )

    # Get tournament to check contestant type.
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        return Err('Tournament contestant type is not set.')

    # Get contestants (participants or teams).
    if tournament.contestant_type == ContestantType.TEAM:
        teams = tournament_repository.get_teams_for_tournament(tournament_id)
        member_counts = tournament_repository.get_team_member_counts(tournament_id)
        empty_teams = [t for t in teams if member_counts.get(t.id, 0) == 0]
        if empty_teams:
            names = ', '.join(t.name for t in empty_teams)
            return Err(
                f'Cannot generate bracket: the following teams have no'
                f' members: {names}. Remove or fill them first.'
            )
        contestant_ids = [str(team.id) for team in teams]
    else:
        participants = tournament_repository.get_participants_for_tournament(
            tournament_id
        )
        contestant_ids = [str(p.id) for p in participants]

    if len(contestant_ids) < 2:
        return Err('Need at least 2 contestants for bracket.')

    if check is not None:
        check_result = check(tournament, contestant_ids)
        if check_result.is_err():
            return Err(check_result.unwrap_err())

    # Cleared last and flush only: a refused regeneration keeps the old
    # bracket, and the tournament lock holds until the generator commits.
    deleted_events: list[MatchDeletedEvent] = []
    if force_regenerate and had_matches:
        deleted_events = clear_bracket(tournament_id, initiator_id=initiator_id)

    return Ok((tournament, contestant_ids, deleted_events))


def _check_double_elimination(
    tournament: Tournament,
    contestant_ids: list[str],
) -> Result[None, str]:
    """Check the preconditions a double-elimination bracket adds."""
    if len(contestant_ids) < 4:
        return Err(
            'Need at least 4 contestants for double-elimination bracket.'
        )

    if tournament.elimination_mode != EliminationMode.DOUBLE_ELIMINATION:
        return Err('Tournament elimination mode must be DOUBLE_ELIMINATION.')

    return Ok(None)


def generate_single_elimination_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate single elimination bracket with all rounds."""
    import math
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
    from .models.bracket import Bracket
    from .models.contestant_type import ContestantType
    from .tournament_domain_service import _standard_seed_order

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(
        tournament_id, force_regenerate, initiator_id=initiator_id
    )
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, deleted_events = prep_result.unwrap()
    num_contestants = len(contestant_ids)

    # Calculate bracket geometry
    bracket_size = 2 ** math.ceil(math.log2(num_contestants))
    num_rounds = int(math.log2(bracket_size))
    is_team = tournament.contestant_type == ContestantType.TEAM
    now = datetime.now(UTC)

    # ---- Third-place match (P3) ----
    # Only for brackets with semifinals (>=4 contestants = >=2 rounds).
    # Created before the main bracket so the FK target exists when
    # semifinal matches reference p3_id via loser_next_match_id.
    p3_id: TournamentMatchID | None = None
    match_events: list[MatchCreatedEvent] = []

    if num_rounds >= 2:
        p3_id = TournamentMatchID(generate_uuid7())
        p3_match = TournamentMatch(
            id=p3_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=0,
            round=num_rounds - 1,  # same round as the final
            next_match_id=None,
            bracket=Bracket.THIRD_PLACE,
            loser_next_match_id=None,
            confirmed_by=None,
            created_at=now,
        )
        tournament_repository.create_match(p3_match)
        match_events.append(
            MatchCreatedEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
                match_id=p3_id,
            )
        )

    # Build rounds from final backwards so we can set next_match_id
    # rounds_matches[r] holds match IDs for round r
    rounds_matches: list[list[TournamentMatchID]] = [
        [] for _ in range(num_rounds)
    ]

    # Create matches round by round, final first
    semifinal_round = num_rounds - 2  # round with 2 matches feeding the final
    for r in range(num_rounds - 1, -1, -1):
        num_matches_in_round = 2 ** (num_rounds - 1 - r)
        for m in range(num_matches_in_round):
            match_id = TournamentMatchID(generate_uuid7())

            # Determine next_match_id from the next round
            if r < num_rounds - 1:
                next_match_id = rounds_matches[r + 1][m // 2]
            else:
                next_match_id = None

            # Wire semifinal losers to the P3 match
            loser_target = p3_id if (p3_id and r == semifinal_round) else None

            match = TournamentMatch(
                id=match_id,
                tournament_id=tournament_id,
                group_order=None,
                match_order=m,
                round=r,
                next_match_id=next_match_id,
                loser_next_match_id=loser_target,
                confirmed_by=None,
                created_at=now,
            )
            tournament_repository.create_match(match)
            rounds_matches[r].append(match_id)

            match_events.append(
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match_id,
                )
            )

    # Seed round 0 using standard seed order
    seed_order = _standard_seed_order(bracket_size)

    # Pad contestant list with None for DEFWINs
    padded: list[str | None] = list(contestant_ids) + [None] * (
        bracket_size - num_contestants
    )

    # Place contestants into round 0 matches
    for slot_idx, seed_pos in enumerate(seed_order):
        match_idx = slot_idx // 2
        match_id = rounds_matches[0][match_idx]
        cid = padded[seed_pos]

        if cid is None:
            continue  # DEFWIN — no contestant to place

        contestant_id = TournamentMatchToContestantID(generate_uuid7())
        if is_team:
            contestant = TournamentMatchToContestant(
                id=contestant_id,
                tournament_match_id=match_id,
                team_id=TournamentTeamID(UUID(cid)),
                participant_id=None,
                score=None,
                created_at=now,
            )
        else:
            contestant = TournamentMatchToContestant(
                id=contestant_id,
                tournament_match_id=match_id,
                team_id=None,
                participant_id=TournamentParticipantID(UUID(cid)),
                score=None,
                created_at=now,
            )
        tournament_repository.create_match_contestant(contestant)

    # Auto-advance DEFWIN matches: if a round 0 match has only
    # 1 contestant, advance that contestant to the next match
    for match_idx, match_id in enumerate(rounds_matches[0]):
        contestants = tournament_repository.get_contestants_for_match(match_id)
        if len(contestants) == 1 and num_rounds > 1:
            # Advance the sole contestant to the next round
            sole = contestants[0]
            next_mid = rounds_matches[1][match_idx // 2]
            adv_id = TournamentMatchToContestantID(generate_uuid7())
            advanced = TournamentMatchToContestant(
                id=adv_id,
                tournament_match_id=next_mid,
                team_id=sole.team_id,
                participant_id=sole.participant_id,
                score=None,
                created_at=now,
            )
            tournament_repository.create_match_contestant(advanced)
            # Auto-confirm the DEFWIN match
            if initiator_id is not None:
                tournament_repository.confirm_match(
                    match_id, initiator_id
                )

    # Single transaction commit
    tournament_repository.commit_session()

    # Dispatch events after successful commit
    for event in deleted_events:
        signals.match_deleted.send(None, event=event)
    for event in match_events:
        signals.match_created.send(None, event=event)

    all_match_ids = set()
    for round_matches in rounds_matches:
        all_match_ids.update(round_matches)
    if p3_id is not None:
        all_match_ids.add(p3_id)
    ready_events = _collect_ready_match_events(all_match_ids, tournament_id, now)
    for event in ready_events:
        match_ready.send(None, event=event)

    total_matches = bracket_size - 1
    if p3_id is not None:
        total_matches += 1
    return Ok(total_matches)


def generate_double_elimination_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate double elimination bracket with WB, LB, and
    GF.  The Grand Final is the terminal match.
    """
    import math
    from uuid import UUID

    from . import signals
    from .events import MatchCreatedEvent
    from .models.bracket import Bracket
    from .models.contestant_type import ContestantType
    from .tournament_domain_service import _standard_seed_order

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(
        tournament_id,
        force_regenerate,
        initiator_id=initiator_id,
        check=_check_double_elimination,
    )
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, deleted_events = prep_result.unwrap()
    num_contestants = len(contestant_ids)

    # Calculate bracket geometry.
    p = math.ceil(math.log2(num_contestants))
    bracket_size = 2**p
    wb_rounds = p  # WB rounds 0..p-1
    lb_rounds = 2 * (p - 1)  # LB rounds 1..lb_rounds

    is_team = tournament.contestant_type == ContestantType.TEAM
    now = datetime.now(UTC)
    match_events: list[MatchCreatedEvent] = []

    # -- Pre-generate all match IDs so linkage can be
    # -- computed before any create_match call.

    # WB: wb_ids[r][m]
    wb_ids: list[list[TournamentMatchID]] = []
    for r in range(wb_rounds):
        n = 2 ** (wb_rounds - 1 - r)
        wb_ids.append([TournamentMatchID(generate_uuid7()) for _ in range(n)])

    # LB: lb_ids[lb_r][m]  (index 0 unused)
    lb_ids: list[list[TournamentMatchID]] = [[]]
    lb_count = bracket_size // 4
    for lb_r in range(1, lb_rounds + 1):
        lb_ids.append(
            [TournamentMatchID(generate_uuid7()) for _ in range(lb_count)]
        )
        if lb_r % 2 == 0:
            lb_count = max(lb_count // 2, 1)

    # Grand Final
    gf_id = TournamentMatchID(generate_uuid7())

    # -- Compute LB next_match_id mapping.
    def _lb_next(
        lb_r: int,
        m: int,
    ) -> TournamentMatchID | None:
        if lb_r >= lb_rounds:
            return gf_id
        next_round = lb_r + 1
        if lb_r % 2 == 1:
            # Minor round: same index in next round.
            return lb_ids[next_round][m]
        # Major round: halves.
        return lb_ids[next_round][m // 2]

    # -- Compute WB loser -> LB routing.
    def _wb_loser_target(
        wb_r: int,
        m_idx: int,
    ) -> TournamentMatchID | None:
        target_round = 1 if wb_r == 0 else 2 * wb_r
        targets = lb_ids[target_round]
        if not targets:
            return None
        return targets[m_idx % len(targets)]

    # ---- Grand Final (bracket='GF', round=0) ----
    # Created FIRST so the FK target exists when WB/LB matches
    # reference gf_id.  LB is created next so WB's
    # loser_next_match_id FK targets also exist before WB flush.
    gf_match = TournamentMatch(
        id=gf_id,
        tournament_id=tournament_id,
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        bracket=Bracket.GRAND_FINAL,
        loser_next_match_id=None,
        confirmed_by=None,
        created_at=now,
    )
    tournament_repository.create_match(gf_match)
    match_events.append(
        MatchCreatedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=tournament_id,
            match_id=gf_id,
        )
    )

    # ---- Create LB matches (last round first) ----
    # LB before WB so that WB's loser_next_match_id FK targets
    # (LB match rows) exist at flush time.
    for lb_r in range(lb_rounds, 0, -1):
        for m in range(len(lb_ids[lb_r])):
            mid = lb_ids[lb_r][m]
            next_mid = _lb_next(lb_r, m)

            match = TournamentMatch(
                id=mid,
                tournament_id=tournament_id,
                group_order=None,
                match_order=m,
                round=lb_r,
                next_match_id=next_mid,
                bracket=Bracket.LOSERS,
                loser_next_match_id=None,
                confirmed_by=None,
                created_at=now,
            )
            tournament_repository.create_match(match)

            match_events.append(
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=mid,
                )
            )

    # ---- Create WB matches (final first) ----
    for r in range(wb_rounds - 1, -1, -1):
        for m in range(len(wb_ids[r])):
            mid = wb_ids[r][m]

            # Winner next: next WB round, or GF for
            # the WB final.
            if r < wb_rounds - 1:
                next_mid = wb_ids[r + 1][m // 2]
            else:
                next_mid = gf_id

            loser_mid = _wb_loser_target(r, m)

            match = TournamentMatch(
                id=mid,
                tournament_id=tournament_id,
                group_order=None,
                match_order=m,
                round=r,
                next_match_id=next_mid,
                bracket=Bracket.WINNERS,
                loser_next_match_id=loser_mid,
                confirmed_by=None,
                created_at=now,
            )
            tournament_repository.create_match(match)

            match_events.append(
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=mid,
                )
            )

    # ---- Seed WBR0 ----
    seed_order = _standard_seed_order(bracket_size)
    padded: list[str | None] = list(contestant_ids) + [None] * (
        bracket_size - num_contestants
    )

    for slot_idx, seed_pos in enumerate(seed_order):
        match_idx = slot_idx // 2
        match_id = wb_ids[0][match_idx]
        cid = padded[seed_pos]

        if cid is None:
            continue  # DEFWIN slot

        contestant_id = TournamentMatchToContestantID(generate_uuid7())
        if is_team:
            contestant = TournamentMatchToContestant(
                id=contestant_id,
                tournament_match_id=match_id,
                team_id=TournamentTeamID(UUID(cid)),
                participant_id=None,
                score=None,
                created_at=now,
            )
        else:
            contestant = TournamentMatchToContestant(
                id=contestant_id,
                tournament_match_id=match_id,
                team_id=None,
                participant_id=TournamentParticipantID(UUID(cid)),
                score=None,
                created_at=now,
            )
        tournament_repository.create_match_contestant(contestant)

    # ---- Auto-advance DEFWIN in WBR0 ----
    for match_idx, match_id in enumerate(wb_ids[0]):
        contestants = tournament_repository.get_contestants_for_match(match_id)
        if len(contestants) == 1 and wb_rounds > 1:
            sole = contestants[0]
            next_mid = wb_ids[1][match_idx // 2]
            adv_id = TournamentMatchToContestantID(generate_uuid7())
            advanced = TournamentMatchToContestant(
                id=adv_id,
                tournament_match_id=next_mid,
                team_id=sole.team_id,
                participant_id=sole.participant_id,
                score=None,
                created_at=now,
            )
            tournament_repository.create_match_contestant(advanced)
            # Auto-confirm the DEFWIN match
            if initiator_id is not None:
                tournament_repository.confirm_match(
                    match_id, initiator_id
                )

    # ---- Null loser_next_match_id for WBR0 DEFWIN matches ----
    # A DEFWIN match produces no loser, so the loser link
    # is invalid and must be cleared to prevent downstream
    # LB matches from expecting a feeder that will never arrive.
    for _match_idx, match_id in enumerate(wb_ids[0]):
        contestants = tournament_repository.get_contestants_for_match(match_id)
        if len(contestants) <= 1:
            tournament_repository.clear_loser_next_match_id(match_id)

    # ---- Propagate dead LB matches ----
    # After DEFWIN nullification, some LB matches may have
    # zero incoming feeds.  Walk LB rounds forward and break
    # their next_match_id links so downstream matches do not
    # expect phantom feeders.
    _propagate_dead_lb_matches(lb_ids, lb_rounds)

    # Single transaction commit.
    tournament_repository.commit_session()

    # Dispatch events after successful commit.
    for event in deleted_events:
        signals.match_deleted.send(None, event=event)
    for event in match_events:
        signals.match_created.send(None, event=event)

    all_match_ids = set()
    for round_matches in wb_ids:
        all_match_ids.update(round_matches)
    for round_matches in lb_ids:
        all_match_ids.update(round_matches)
    if gf_id:
        all_match_ids.add(gf_id)
    ready_events = _collect_ready_match_events(all_match_ids, tournament_id, now)
    for event in ready_events:
        match_ready.send(None, event=event)

    return Ok(len(match_events))


def _propagate_dead_lb_matches(
    lb_ids: list[list[TournamentMatchID]],
    lb_rounds: int,
) -> None:
    """Clear next_match_id on LB matches with zero incoming
    feeds.

    After WBR0 DEFWIN nullification removes loser links,
    some LB matches lose all feeders.  Walk LB rounds
    forward: any match with 0 incoming feeds is dead and
    its next_match_id must be cleared so the downstream
    match does not expect a phantom feeder.
    """
    for lb_r in range(1, lb_rounds + 1):
        for match_id in lb_ids[lb_r]:
            feeds = (
                tournament_repository.count_incoming_feeds(
                    match_id
                )
            )
            if feeds == 0:
                tournament_repository.clear_next_match_id(
                    match_id
                )


def validate_bracket_for_start(
    tournament_id: TournamentID,
    *,
    tournament: Tournament | None = None,
) -> list[str]:
    """Return list of violation strings; empty list = valid.

    Format-aware structural bracket validation enforced on
    tournament start: a technically invalid
    bracket must never be started and cannot be administratively
    ignored.

    ``tournament`` may be passed by callers that already hold the
    loaded ``Tournament`` (e.g. ``change_status``) to avoid an extra
    repository round-trip; otherwise it is fetched here.
    """
    if tournament is None:
        tournament = tournament_repository.get_tournament(tournament_id)

    if not (
        tournament.game_format
        and tournament.game_format.requires_bracket_generation
    ):
        # FFA / HIGHSCORE bypass bracket generation.
        return []

    matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament_id
    )
    contestants_by_match = (
        tournament_repository.get_contestants_for_tournament(tournament_id)
    )

    if tournament.elimination_mode == EliminationMode.SINGLE_ELIMINATION:
        return _validate_se_bracket(matches, contestants_by_match)
    if tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
        return _validate_de_bracket(matches, contestants_by_match)
    if tournament.elimination_mode == EliminationMode.ROUND_ROBIN:
        return _validate_round_robin_bracket(
            matches, contestants_by_match
        )

    # Fallthrough: the game format requires bracket generation but
    # elimination_mode did not match any known bracket structure --
    # either unset (None) or a mode invalid for this format (e.g.
    # NONE, valid only for HIGHSCORE, on a ONE_V_ONE tournament since
    # VALID_COMBINATIONS is not enforced at the DB level). This must
    # never silently pass as valid and let the tournament start with
    # zero matches.
    violations = []
    if not matches:
        violations.append('no matches generated')
    if tournament.elimination_mode is None:
        violations.append(
            'tournament requires bracket generation but has no '
            'elimination mode set'
        )
    else:
        violations.append(
            f'elimination mode {tournament.elimination_mode.value!r} is '
            'not valid for a game format that requires bracket generation'
        )
    return violations


def _distinct_contestant_count(
    contestants_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ],
) -> int:
    """Count the distinct real contestants across those matches.

    Skips rows that carry neither a participant nor a team -- DEFWIN
    slots -- the same way every other reader of a contestant list in
    this module does (``_validate_match_scores``,
    ``_snapshot_contestant_scores``, the view helpers' status
    classifier). Without the filter, ``contestant_id`` RAISES on such
    a row, and this function is only ever reached from
    ``validate_bracket_for_start``: a single keyless row anywhere in
    the tournament would turn "start tournament" into an uncaught
    ValueError -- a 500 at the one moment an organiser cannot work
    around it -- instead of the violation list this gate exists to
    report.
    """
    ids: set[str] = set()
    for match_contestants in contestants_by_match.values():
        for c in match_contestants:
            if c.participant_id is None and c.team_id is None:
                continue
            ids.add(contestant_id(c))
    return len(ids)


def _collect_dangling_links(matches: list[TournamentMatch]) -> list[str]:
    """Return violations for next/loser links pointing outside the
    tournament's match set."""
    violations = []
    match_ids = {m.id for m in matches}
    for m in matches:
        for attr in ('next_match_id', 'loser_next_match_id'):
            target = getattr(m, attr)
            if target is not None and target not in match_ids:
                violations.append(
                    f'match {m.id} has {attr} pointing to unknown '
                    f'match {target}'
                )
    return violations


def _validate_se_bracket(
    matches: list[TournamentMatch],
    contestants_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ],
) -> list[str]:
    violations = []
    if not matches:
        return ['no matches generated']

    main = [m for m in matches if m.bracket in (None, Bracket.WINNERS)]
    p3 = [m for m in matches if m.bracket == Bracket.THIRD_PLACE]
    unknown = [
        m
        for m in matches
        if m.bracket not in (None, Bracket.WINNERS, Bracket.THIRD_PLACE)
    ]
    for m in unknown:
        violations.append(
            f'match {m.id} has unexpected bracket {m.bracket.value!r} '
            'for single elimination'
        )

    contestant_count = _distinct_contestant_count(contestants_by_match)
    if contestant_count < 2:
        violations.append(
            f'expected at least 2 contestants, found {contestant_count}'
        )

    # A match with no round is itself the violation, and it must be
    # taken out before max(): TournamentMatch.round is `int | None`
    # (the column is nullable), and one None in the generator below
    # raises TypeError comparing NoneType to int. That escapes
    # validate_bracket_for_start, escapes change_status, and 500s the
    # one action an organiser cannot work around -- instead of
    # producing the violation list this gate exists for. Same reason
    # _distinct_contestant_count filters keyless contestant rows.
    roundless = [m for m in main if m.round is None]
    for m in roundless:
        violations.append(f'match {m.id} has no round number')

    rounds = [m for m in main if m.round is not None]
    if rounds:
        final_round = max(m.round for m in rounds)
        for m in rounds:
            if m.round == final_round:
                if m.next_match_id is not None:
                    violations.append(
                        f'terminal match {m.id} has next_match_id'
                    )
            elif m.next_match_id is None:
                violations.append(
                    f'match {m.id} (round {m.round}) is missing '
                    'next_match_id'
                )

    p3_ids = {m.id for m in p3}
    for m in matches:
        if m.loser_next_match_id is not None and (
            m.loser_next_match_id not in p3_ids
        ):
            violations.append(
                f'match {m.id} has orphaned loser_next_match_id '
                f'{m.loser_next_match_id}'
            )
    for m in p3:
        if m.next_match_id is not None:
            violations.append(f'third-place match {m.id} has next_match_id')

    violations.extend(_collect_dangling_links(matches))
    return violations


def _validate_de_bracket(
    matches: list[TournamentMatch],
    contestants_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ],
) -> list[str]:
    violations = []
    if not matches:
        return ['no matches generated']

    wb = [m for m in matches if m.bracket == Bracket.WINNERS]
    lb = [m for m in matches if m.bracket == Bracket.LOSERS]
    gf = [m for m in matches if m.bracket == Bracket.GRAND_FINAL]

    if not wb:
        violations.append('no winners-bracket matches')
    if not lb:
        violations.append('no losers-bracket matches')
    if not gf:
        violations.append('no grand-final match')
    for m in gf:
        if m.next_match_id is not None:
            violations.append(f'grand final {m.id} has next_match_id')
        if m.loser_next_match_id is not None:
            violations.append(f'grand final {m.id} has loser_next_match_id')

    contestant_count = _distinct_contestant_count(contestants_by_match)
    if contestant_count < 4:
        violations.append(
            f'expected at least 4 contestants, found {contestant_count}'
        )

    for m in wb:
        if m.next_match_id is None:
            violations.append(
                f'winners-bracket match {m.id} is missing next_match_id'
            )
        if (
            m.loser_next_match_id is not None
            and not any(lb_m.id == m.loser_next_match_id for lb_m in lb)
        ):
            violations.append(
                f'match {m.id} has orphaned loser_next_match_id '
                f'{m.loser_next_match_id}'
            )

    # LB chain: dead-LB propagation respected.  A live LB match
    # (incoming feeds >= 1) must route its winner onward; a dead
    # one may have been cleared by _propagate_dead_lb_matches.
    feed_counts: dict[TournamentMatchID, int] = {}
    for m in matches:
        for target in (m.next_match_id, m.loser_next_match_id):
            if target is not None:
                feed_counts[target] = feed_counts.get(target, 0) + 1

    lb_ids = {m.id for m in lb}
    gf_ids = {m.id for m in gf}
    for m in lb:
        if m.next_match_id is None and feed_counts.get(m.id, 0) > 0:
            violations.append(
                f'losers-bracket match {m.id} has incoming feeds but no '
                'next_match_id'
            )
        if (
            m.next_match_id is not None
            and m.next_match_id not in lb_ids
            and m.next_match_id not in gf_ids
        ):
            violations.append(
                f'losers-bracket match {m.id} routes to unexpected target'
            )

    violations.extend(_collect_dangling_links(matches))
    return violations


def _validate_round_robin_bracket(
    matches: list[TournamentMatch],
    contestants_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ],
) -> list[str]:
    """Validate a round-robin bracket against itself.

    Deliberately does NOT re-query the live roster
    (``get_teams_for_tournament`` / ``get_participants_for_tournament``):
    once a bracket is generated, a later roster change (e.g. a
    dropout being soft-deleted) must not make an already-valid
    bracket fail forever.
    """
    violations = []
    if not matches:
        return ['no matches generated']

    contestant_count = _distinct_contestant_count(contestants_by_match)
    if contestant_count < 2:
        violations.append(
            f'expected at least 2 contestants, found {contestant_count}'
        )

    expected_pairings = contestant_count * (contestant_count - 1) // 2
    if len(matches) != expected_pairings:
        violations.append(
            f'expected {expected_pairings} round-robin matches, '
            f'found {len(matches)}'
        )

    return violations


def generate_round_robin_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate round-robin bracket with all pairings."""
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
    from .models.contestant_type import ContestantType

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(
        tournament_id, force_regenerate, initiator_id=initiator_id
    )
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, deleted_events = prep_result.unwrap()

    # Generate round-robin schedule via domain service.
    schedule = generate_round_robin_schedule(contestant_ids)

    is_team = tournament.contestant_type == ContestantType.TEAM
    now = datetime.now(UTC)
    match_events: list[MatchCreatedEvent] = []
    total_matches = 0

    for round_num, round_pairings in enumerate(schedule):
        for match_idx, (p1, p2) in enumerate(round_pairings):
            match_id = TournamentMatchID(generate_uuid7())
            match = TournamentMatch(
                id=match_id,
                tournament_id=tournament_id,
                group_order=None,
                match_order=match_idx,
                round=round_num,
                next_match_id=None,
                confirmed_by=None,
                created_at=now,
            )
            tournament_repository.create_match(match)

            # Create contestants for both sides.
            for cid in (p1, p2):
                c_id = TournamentMatchToContestantID(generate_uuid7())
                if is_team:
                    contestant = TournamentMatchToContestant(
                        id=c_id,
                        tournament_match_id=match_id,
                        team_id=TournamentTeamID(UUID(cid)),
                        participant_id=None,
                        score=None,
                        created_at=now,
                    )
                else:
                    contestant = TournamentMatchToContestant(
                        id=c_id,
                        tournament_match_id=match_id,
                        team_id=None,
                        participant_id=(TournamentParticipantID(UUID(cid))),
                        score=None,
                        created_at=now,
                    )
                tournament_repository.create_match_contestant(contestant)

            match_events.append(
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match_id,
                )
            )
            total_matches += 1

    # Single transaction commit.
    tournament_repository.commit_session()

    # Dispatch events after successful commit.
    for event in deleted_events:
        signals.match_deleted.send(None, event=event)
    for event in match_events:
        signals.match_created.send(None, event=event)

    rr_match_ids = {e.match_id for e in match_events}
    ready_events = _collect_ready_match_events(rr_match_ids, tournament_id, now)
    for event in ready_events:
        match_ready.send(None, event=event)

    return Ok(total_matches)


def get_match(
    match_id: TournamentMatchID,
) -> TournamentMatch:
    """Return the match."""
    return tournament_repository.get_match(match_id)


def find_match(
    match_id: TournamentMatchID,
) -> TournamentMatch | None:
    """Return the match, or `None` if not found."""
    return tournament_repository.find_match(match_id)


def get_matches_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentMatch]:
    """Return all matches for that tournament."""
    return tournament_repository.get_matches_for_tournament(tournament_id)


def get_matches_for_tournament_ordered(
    tournament_id: TournamentID,
) -> list[TournamentMatch]:
    """Return all matches for that tournament, ordered by round."""
    return tournament_repository.get_matches_for_tournament_ordered(
        tournament_id
    )


def get_matches_by_ids(
    match_ids: list[TournamentMatchID],
) -> list[TournamentMatch]:
    """Return the matches with those IDs, in arbitrary order."""
    return tournament_repository.get_matches_by_ids(match_ids)


def _lock_defwin_entry_matches(
    tournament_id: TournamentID,
    entries: list[tuple[TournamentMatchToContestant, TournamentMatch]],
) -> None:
    """Lock the tournament row, then the pass's match rows in id order."""
    tournament_repository.lock_tournament_for_update(tournament_id)

    match_ids: set[TournamentMatchID] = set()
    for _contestant, match in entries:
        match_ids.add(match.id)
        if match.next_match_id is not None:
            match_ids.add(match.next_match_id)

    if match_ids:
        tournament_repository.lock_matches_for_update(sorted(match_ids))


def handle_defwin_for_removed_participant(
    tournament_id: TournamentID,
    participant_id: TournamentParticipantID,
    *,
    initiator_id: UserID | None = None,
) -> DefwinResult:
    """Handle defwin logic when removing a participant from an
    active tournament. Removes their contestant entries from
    unconfirmed matches and auto-advances sole remaining opponents.

    Does NOT commit — caller must call commit_session().
    Returns events to dispatch after commit.
    """
    entries = tournament_repository.find_contestant_entries_for_participant_in_tournament(
        tournament_id, participant_id
    )

    # Before any write -- see _lock_defwin_entry_matches.
    _lock_defwin_entry_matches(tournament_id, entries)

    for _contestant, match in entries:
        tournament_repository.delete_contestant_from_match(
            match.id, participant_id=participant_id
        )

    return _process_defwin_entries(
        tournament_id, entries, initiator_id=initiator_id
    )


def handle_defwin_for_removed_team(
    tournament_id: TournamentID,
    team_id: TournamentTeamID,
    *,
    initiator_id: UserID | None = None,
) -> DefwinResult:
    """Handle defwin logic when removing a team from an active
    tournament.

    Removes team's contestant entries from unconfirmed matches and
    auto-advances sole remaining opponents.
    Does NOT commit — caller must call commit_session().
    """
    entries = (
        tournament_repository.find_contestant_entries_for_team_in_tournament(
            tournament_id, team_id
        )
    )

    # Before any write -- see _lock_defwin_entry_matches.
    _lock_defwin_entry_matches(tournament_id, entries)

    for _contestant, match in entries:
        tournament_repository.delete_contestant_from_match(
            match.id, team_id=team_id
        )

    return _process_defwin_entries(
        tournament_id, entries, initiator_id=initiator_id
    )


def _process_defwin_entries(
    tournament_id: TournamentID,
    entries: list[tuple[TournamentMatchToContestant, TournamentMatch]],
    *,
    initiator_id: UserID | None = None,
) -> DefwinResult:
    """Shared defwin advancement and confirmation logic for removed
    contestants.

    After the removed contestant's entry has been deleted from each
    match, check whether the sole remaining opponent should be
    auto-advanced to the next round and whether the defwin match
    should be auto-confirmed.

    Advancement requires ``next_match_id`` (cannot advance without a
    destination).  Confirmation happens for ALL sole-opponent defwins
    when ``initiator_id`` is provided, regardless of
    ``next_match_id``.  For terminal elimination matches,
    auto-complete is triggered via
    ``_try_auto_complete_tournament()``.

    Does NOT commit — caller handles the transaction and dispatches
    the returned events post-commit.
    """
    now = datetime.now(UTC)
    advanced_events: list[ContestantAdvancedEvent] = []
    confirmed_events: list[MatchConfirmedEvent] = []
    completed_events: list[TournamentCompletedEvent] = []

    # Each entry's contestant has already been deleted from its match
    # by the caller, so `remaining` below reflects the post-deletion
    # state of that match.
    for _contestant, match in entries:
        remaining = tournament_repository.get_contestants_for_match(match.id)

        # If both contestants were removed (len == 0) or more than
        # one remains, no defwin processing is needed.
        if len(remaining) != 1:
            continue

        sole = remaining[0]

        # --- Advancement (requires next_match_id) ---
        if match.next_match_id is not None:
            next_contestants = tournament_repository.get_contestants_for_match(
                match.next_match_id
            )
            already_advanced = any(
                c.participant_id == sole.participant_id
                and c.team_id == sole.team_id
                for c in next_contestants
            )
            if not already_advanced:
                adv_id = TournamentMatchToContestantID(generate_uuid7())
                advanced = TournamentMatchToContestant(
                    id=adv_id,
                    tournament_match_id=match.next_match_id,
                    team_id=sole.team_id,
                    participant_id=sole.participant_id,
                    score=None,
                    created_at=now,
                )
                tournament_repository.create_match_contestant(advanced)

                advanced_events.append(
                    ContestantAdvancedEvent(
                        occurred_at=now,
                        initiator=None,
                        tournament_id=tournament_id,
                        match_id=match.next_match_id,
                        from_match_id=match.id,
                        advanced_team_id=sole.team_id,
                        advanced_participant_id=sole.participant_id,
                    )
                )

        # --- Confirmation (always for sole-opponent defwins) ---
        if initiator_id is not None:
            tournament_repository.confirm_match(
                match.id, initiator_id
            )

            confirmed_events.append(
                MatchConfirmedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match.id,
                    winner_team_id=sole.team_id,
                    winner_participant_id=sole.participant_id,
                )
            )

            # Terminal elimination match → auto-complete tournament.
            if match.next_match_id is None:
                tournament = tournament_repository.get_tournament(
                    tournament_id
                )
                result = _try_auto_complete_tournament(match, tournament, sole)
                if result.is_err():
                    logger.warning(
                        'Auto-complete failed for tournament %s '
                        'after defwin on match %s: %s',
                        tournament_id,
                        match.id,
                        result.unwrap_err(),
                    )
                elif result.unwrap():
                    completed_events.append(
                        TournamentCompletedEvent(
                            occurred_at=now,
                            initiator=None,
                            tournament_id=tournament_id,
                            winner_team_id=sole.team_id,
                            winner_participant_id=sole.participant_id,
                        )
                    )

    return DefwinResult(advanced_events, confirmed_events, completed_events)


def _determine_loser(
    contestants: list[TournamentMatchToContestant],
    winner: TournamentMatchToContestant,
) -> Result[TournamentMatchToContestant, str]:
    """Return the contestant that is NOT the winner."""
    if len(contestants) != 2:
        return Err(f'Expected 2 contestants, got {len(contestants)}')
    for contestant in contestants:
        if contestant.id != winner.id:
            return Ok(contestant)
    return Err('Could not determine loser.')


def find_contestant_for_user(
    match_id: TournamentMatchID,
    user_id: UserID,
) -> TournamentMatchToContestant | None:
    """Return the contestant entry for a user in a match, or None.

    Handles both SOLO (participant_id) and TEAM (team_id) modes.
    """
    match = tournament_repository.get_match(match_id)
    contestants = tournament_repository.get_contestants_for_match(match_id)
    return _resolve_initiator_contestant(
        match.tournament_id, user_id, contestants
    )


def _resolve_initiator_contestant(
    tournament_id: TournamentID,
    user_id: UserID,
    contestants: list[TournamentMatchToContestant],
) -> TournamentMatchToContestant | None:
    """Match a user to their contestant entry using pre-fetched data."""
    participant = tournament_repository.find_participant_by_user(
        tournament_id, user_id
    )
    if participant is None:
        return None
    for contestant in contestants:
        if (
            contestant.participant_id is not None
            and contestant.participant_id == participant.id
        ):
            return contestant
        if (
            contestant.team_id is not None
            and participant.team_id is not None
            and contestant.team_id == participant.team_id
        ):
            return contestant
    return None


def get_user_match_role(
    tournament_id: TournamentID,
    user_id: UserID,
    contestants: list[TournamentMatchToContestant],
    match_confirmed: bool,
) -> MatchUserRole:
    """Determine a user's role in a match for UI display."""
    if match_confirmed:
        return MatchUserRole(contestant=None, is_loser=False, can_confirm=False, can_submit=False)

    contestant = _resolve_initiator_contestant(
        tournament_id, user_id, contestants
    )
    if contestant is None:
        return MatchUserRole(contestant=None, is_loser=False, can_confirm=False, can_submit=False)

    # DEFWIN: fewer than 2 real contestants — match needs no score
    # submission; the bracket generator has already auto-advanced
    # the sole player.
    real_contestants = [
        c for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]
    if len(real_contestants) < 2:
        return MatchUserRole(contestant=contestant, is_loser=False, can_confirm=False, can_submit=False)

    all_have_scores = all(c.score is not None for c in real_contestants)
    if not all_have_scores:
        return MatchUserRole(contestant=contestant, is_loser=False, can_confirm=False, can_submit=True)

    winner_result = determine_match_winner(contestants)
    if winner_result.is_err():
        return MatchUserRole(contestant=contestant, is_loser=False, can_confirm=False, can_submit=False)

    winner = winner_result.unwrap()
    if winner is None:
        # Draw — any participant may confirm; both may submit revised
        # scores until confirmation.
        return MatchUserRole(contestant=contestant, is_loser=False, can_confirm=True, can_submit=True)
    elif winner.id != contestant.id:
        # This user is the loser.
        return MatchUserRole(contestant=contestant, is_loser=True, can_confirm=True, can_submit=True)
    else:
        # This user is the winner — no action needed.
        return MatchUserRole(contestant=contestant, is_loser=False, can_confirm=False, can_submit=False)


def set_score_by_participant(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    contestant_id: TournamentParticipantID | TournamentTeamID,
    score: int,
) -> Result[None, str]:
    """Set a score for a contestant in an unconfirmed match.

    Caller must be a participant in the match.
    """
    match = tournament_repository.get_match(match_id)
    if match.confirmed_by is not None:
        return Err('Cannot modify scores of a confirmed match.')
    contestants = tournament_repository.get_contestants_for_match(match_id)
    if _resolve_initiator_contestant(match.tournament_id, initiator_id, contestants) is None:
        return Err('You are not a participant in this match.')
    return set_score(match_id, contestant_id, score)


def set_match_scores(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> Result[None, str]:
    """Set all contestant scores for a match atomically.

    Only the proposed loser may submit scores.  If the proposed
    scores would make the initiator the winner the request is
    rejected.  Draws are accepted from any participant.

    The submission is validated before any lock is taken, so a refused
    one locks nothing, and again under the reachable-set lock.

    When this returns `Err`, the session has been rolled back; ORM
    objects fetched before the call may be expired or detached.
    """

    def _reject(error_message: str) -> Result[None, str]:
        tournament_repository.rollback_session()
        return Err(error_message)

    match = tournament_repository.find_match(match_id)
    if match is None:
        tournament_repository.rollback_session()
        raise ValueError(f'Unknown match ID "{match_id}"')

    precheck = _validate_score_submission(
        match,
        tournament_repository.get_contestants_for_match(match_id),
        initiator_id,
        scores,
    )
    if precheck.is_err():
        return _reject(precheck.unwrap_err())

    _lock_reachable_matches(match_id)

    match = tournament_repository.find_match_fresh(match_id)
    if match is None:
        tournament_repository.rollback_session()
        raise ValueError(f'Unknown match ID "{match_id}"')

    validation = _validate_score_submission(
        match,
        tournament_repository.get_contestants_for_match(match_id),
        initiator_id,
        scores,
    )
    if validation.is_err():
        return _reject(validation.unwrap_err())

    # Atomic write — all scores flushed together.
    tournament_repository.update_contestant_scores(validation.unwrap())
    # Auto-confirm: loser submitted scores → match is resolved.
    confirm_result = confirm_match(
        match_id, initiator_id, _locks_held=True
    )
    if confirm_result.is_err():
        return _reject(confirm_result.unwrap_err())
    return Ok(None)


def _validate_score_submission(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
    initiator_id: UserID,
    scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> Result[dict[TournamentMatchToContestantID, int], str]:
    """Validate a participant's score submission and map it to rows."""
    if match.confirmed_by is not None:
        return Err('Cannot modify scores of a confirmed match.')

    # Exclude DEFWIN slots (no participant or team assigned) — they carry
    # no score and must not appear in the submitted scores dict.
    real_contestants = [
        c for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]

    initiator_contestant = _resolve_initiator_contestant(
        match.tournament_id, initiator_id, contestants
    )
    if initiator_contestant is None:
        return Err('You are not a participant in this match.')

    if len(scores) != len(real_contestants):
        return Err(
            'All contestants in the match must have scores submitted.'
        )

    id_to_score: dict[TournamentMatchToContestantID, int] = {}
    proposed_contestants: list[TournamentMatchToContestant] = []
    for contestant in real_contestants:
        key = contestant.team_id or contestant.participant_id
        if key not in scores:
            return Err('A score is missing for one of the contestants.')
        score = scores[key]
        if score < 0:
            return Err('Score cannot be negative.')
        if score > MAX_MATCH_SCORE:
            return Err(MAX_MATCH_SCORE_ERROR)
        id_to_score[contestant.id] = score
        proposed_contestants.append(replace(contestant, score=score))

    # Determine proposed winner to enforce loser-only submission.
    winner_result = determine_match_winner(proposed_contestants)
    if winner_result.is_err():
        return Err(winner_result.unwrap_err())
    winner = winner_result.unwrap()

    if winner is not None and winner.id == initiator_contestant.id:
        return Err('Only the losing side may submit scores.')

    return Ok(id_to_score)


def _snapshot_contestant_scores(
    match_id: TournamentMatchID,
    *,
    contestants: list[TournamentMatchToContestant] | None = None,
) -> dict[str, int | None]:
    """Return ``{contestant key: score}`` for the real contestants.

    Must run before a retraction cascade: ``_unconfirm_match_impl``
    ends with ``clear_contestant_scores``. Keys are stringified for
    JSONB storage. Pass ``contestants`` to reuse an existing fetch.
    """
    if contestants is None:
        contestants = tournament_repository.get_contestants_for_match(
            match_id
        )
    return {
        str(c.team_id or c.participant_id): c.score
        for c in contestants
        if (c.team_id or c.participant_id) is not None
    }


def _snapshot_contestant_placements(
    contestants: list[TournamentMatchToContestant],
) -> dict[str, dict[str, int | None]]:
    """Return `{contestant key: {placement, points}}` for placed contestants."""
    return {
        str(c.team_id or c.participant_id): {
            'placement': c.placement,
            'points': c.points,
        }
        for c in contestants
        if (c.team_id or c.participant_id) is not None
        and c.placement is not None
    }


def _snapshot_cascade_scores(
    match_id: TournamentMatchID,
    *,
    classification: (
        tuple[CorrectionCase, list[TournamentMatchID]] | None
    ) = None,
) -> dict[str, dict[str, int | None]]:
    """Return ``{match id: {contestant key: score}}`` for every
    DOWNSTREAM match the retraction cascade is about to clear.

    The subject match's own scores are recorded separately (see
    ``_snapshot_contestant_scores``); this covers what the cascade
    destroys beyond it. ``_unconfirm_match_impl`` recurses into a
    downstream match only when that match is itself confirmed, and
    ends every such recursion with ``clear_contestant_scores``: those
    results are gone from the database the moment the correction
    commits, and nothing else records them. Without this, a
    correction that an admin acknowledged as CONFIRMED_DOWNSTREAM
    logged the one result the admin was looking at and silently
    dropped every result the cascade wiped underneath it -- leaving
    the audit log unable to answer the only question worth asking
    after a mistaken acknowledgement: what did the numbers used to
    be?

    The affected set comes from ``classify_result_correction``, the
    same walk the admin panel previews the cascade with, so the log
    and the warning cannot describe different sets of matches.
    Best effort: a classification error yields ``{}`` rather than
    blocking the retraction, since an unrecorded score is worse than
    nothing but a refused correction is worse still.

    ``classification`` lets a caller that has already classified this
    match hand its result in rather than pay for a second walk
    (``classify_result_correction`` reads the whole bracket).
    ``correct_match_result`` does exactly that: it classifies under
    the reachable-set lock to drive the acknowledgement gate, and
    nothing commits between that call and this one, so recomputing
    here could only produce the same answer. Reusing it also makes
    the guarantee above -- that the log and the admin's warning
    describe the same set of matches -- hold by construction rather
    than by two walks agreeing.
    """
    if classification is None:
        classification_result = classify_result_correction(match_id)
        if classification_result.is_err():
            return {}
        classification = classification_result.unwrap()

    _case, affected_ids = classification
    if not affected_ids:
        return {}

    confirmed_ids = [
        m.id
        for m in tournament_repository.get_matches_by_ids(
            list(affected_ids)
        )
        if m.confirmed_by is not None
    ]
    if not confirmed_ids:
        return {}

    contestants_by_match = (
        tournament_repository.get_contestants_for_matches(confirmed_ids)
    )
    return {
        str(downstream_id): _snapshot_contestant_scores(
            downstream_id,
            contestants=contestants_by_match.get(downstream_id, []),
        )
        for downstream_id in confirmed_ids
    }


def _validate_match_scores(
    match_id: TournamentMatchID,
    scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> Result[dict[TournamentMatchToContestantID, int], str]:
    """Validate proposed contestant scores for a match.

    Pure validation — no DB writes.  Resolves the real (non-DEFWIN)
    contestants for the match, requires a score for each, and
    rejects a negative score, a score above ``MAX_MATCH_SCORE``, or
    a score-count mismatch.  Where the tournament's elimination mode
    forbids draws, also rejects proposed scores that would tie
    (mirroring the check ``_confirm_draw_impl`` makes at commit time).

    On success returns the resolved contestant-row-ID -> score map,
    so a caller that goes on to write does not re-query the
    contestants it has already resolved here.

    Shared by ``admin_set_and_confirm_match`` and
    ``correct_match_result`` so the two paths cannot drift apart.
    """
    contestants = tournament_repository.get_contestants_for_match(
        match_id
    )

    # Exclude DEFWIN slots (no participant or team assigned).
    real_contestants = [
        c for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]

    # A match with fewer than 2 real contestants can never be
    # confirmed, and determine_match_winner returns Err rather than a
    # draw, so the tie check below cannot catch it. Without this,
    # correcting an auto-confirmed DEFWIN match would cascade the
    # retraction and only then fail the re-confirm.
    if len(real_contestants) < 2:
        return Err(
            'Cannot confirm match with less than 2 contestants.'
        )

    if len(scores) != len(real_contestants):
        return Err(
            'All contestants in the match must have scores submitted.'
        )

    # Resolve each submitted key to a contestant and validate scores.
    proposed_contestants: list[TournamentMatchToContestant] = []
    id_to_score: dict[TournamentMatchToContestantID, int] = {}
    for contestant in real_contestants:
        key = contestant.team_id or contestant.participant_id
        if key not in scores:
            return Err(f'Missing score for contestant "{key}".')
        score = scores[key]
        if score < 0:
            return Err('Score cannot be negative.')
        if score > MAX_MATCH_SCORE:
            return Err(MAX_MATCH_SCORE_ERROR)
        proposed_contestants.append(replace(contestant, score=score))
        id_to_score[contestant.id] = score

    # Draws are only accepted in round-robin tournaments; reject a
    # tied proposed score elsewhere, mirroring _confirm_draw_impl. Any
    # other outcome of determine_match_winner (e.g. too few real
    # contestants) is left for the write path to handle, unchanged.
    match = tournament_repository.get_match(match_id)
    tournament = tournament_repository.get_tournament(match.tournament_id)
    if tournament.elimination_mode != EliminationMode.ROUND_ROBIN:
        winner_result = determine_match_winner(proposed_contestants)
        if winner_result.is_ok() and winner_result.unwrap() is None:
            return Err(
                'Match is a draw; a winner is required '
                'in this tournament mode.'
            )

    return Ok(id_to_score)


def _admin_set_and_confirm_match_impl(
    match_id: TournamentMatchID,
    admin_id: UserID,
    scores: dict[TournamentParticipantID | TournamentTeamID, int],
    *,
    _locks_held: bool = False,
) -> Result[
    tuple[
        MatchConfirmedEvent,
        TournamentCompletedEvent | None,
        list[ContestantAdvancedEvent],
        list[MatchCreatedEvent],
        list[MatchReadyEvent],
    ],
    str,
]:
    """Flush-only, event-collecting core of admin_set_and_confirm_match.

    Sets every contestant score and confirms the match using
    repository flush calls, via ``_confirm_match_impl``. Collects
    domain events rather than dispatching them. The caller is
    responsible for the single ``commit()`` (or ``rollback()``) and
    event dispatch -- mirrors ``_confirm_match_impl`` /
    ``_unconfirm_match_flush``.

    Used directly by ``correct_match_result`` so a correction's score
    application shares its transaction with the retraction that
    precedes it, instead of committing separately (workspace-ukkj).

    The already-confirmed check below reads WITHOUT a row lock, on
    purpose. ``_lock_reachable_matches`` is the first match-row lock
    any writer may take, and it runs inside ``_confirm_match_impl``
    below; taking ``get_match_for_update`` on the subject here would
    put this path's first lock on the subject row instead, ahead of
    the id-ordered set. That is a real inversion, not a theoretical
    one: bracket ids are uuid7 and generation order is
    reverse-topological (the third-place match is created first and
    rounds are built final-first), so the subject is routinely the
    HIGHEST id in its own reachable set. A concurrent
    ``correct_match_result`` on the same match would then hold the
    downstream rows and want the subject while this path held the
    subject and wanted them -- a deadlock. The unlocked read here is
    only a fast fail; the authoritative, locked check is
    ``_validate_match_confirmable`` inside ``_confirm_match_impl``,
    which rejects an already-confirmed match under the ordered lock.

    The ordered lock is taken HERE, first, rather than being left to
    ``_confirm_match_impl`` below: the score write between the two
    (``update_contestant_scores``) takes row locks on this match's
    contestant rows, which a concurrent retraction cascade also wants
    (``clear_contestant_scores``). Acquiring those before the match
    rows would deadlock against a ``correct_match_result`` holding the
    match rows and reaching for the contestant rows. Nothing here may
    lock anything before this call.

    ``_confirm_match_impl`` below is then told the lock is already
    held. Re-taking the row locks would indeed be nearly free, but
    computing the reachable set again is not: each
    ``_lock_reachable_matches`` call issues two full-bracket SELECTs
    (one to walk, one to re-verify the walk under the lock). The
    choke point still covers any caller that reaches that core
    without locking first, because ``_locks_held`` defaults to False.

    ``_locks_held`` on THIS function says the same thing one level
    up: ``correct_match_result`` locks the reachable set once for the
    whole correction and nothing commits before this runs, so the
    acquisition here would be pure cost.
    """
    if not _locks_held:
        _lock_reachable_matches(match_id)

    match = tournament_repository.find_match_fresh(match_id)
    if match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    if match.confirmed_by is not None:
        return Err('Match is already confirmed.')

    # Fail before the score write below rather than leaving it to
    # _confirm_match_impl's own guard: the caller does roll back, but
    # reporting the format mismatch at the point the scores are
    # rejected keeps the flash accurate about what was refused.
    tournament = tournament_repository.get_tournament(match.tournament_id)
    if _decided_by_placements(tournament):
        return Err(PLACEMENT_FORMAT_CONFIRM_ERROR)

    validation = _validate_match_scores(match_id, scores)
    if validation.is_err():
        return Err(validation.unwrap_err())

    # _validate_match_scores already resolved every real (non-DEFWIN)
    # contestant row and mapped it to its submitted score, so there is
    # no second get_contestants_for_match round-trip here.
    id_to_score = validation.unwrap()

    # Flush-only write — all scores flushed together.
    tournament_repository.update_contestant_scores(id_to_score)

    # Auto-confirm: admin submitted scores → match is resolved.
    # _confirm_match_impl will see the flushed scores. The reachable
    # set is locked either way by the time we get here -- by this
    # function above, or by the caller that set _locks_held.
    return _confirm_match_impl(match_id, admin_id, _locks_held=True)


def admin_set_and_confirm_match(
    match_id: TournamentMatchID,
    admin_id: UserID,
    scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> Result[None, str]:
    """Set all contestant scores and confirm a match atomically.

    Admin-only variant: no loser-only enforcement, no participant
    check.  The admin supplies scores for every real contestant and
    the match is confirmed in one operation.

    Uses a single DB commit and dispatches all collected events
    afterwards -- see ``_admin_set_and_confirm_match_impl``.

    Post-rollback note: when this function returns ``Err``, the
    database session has been rolled back.  Any ORM-managed objects
    fetched before this call may be expired or detached.  Callers
    must NOT access attributes on those objects after receiving an
    ``Err`` result.
    """
    result = _admin_set_and_confirm_match_impl(match_id, admin_id, scores)
    if result.is_err():
        # Roll back unconditionally, mirroring unconfirm_match's own
        # wrapper: whether the failure happened before any write (e.g.
        # "already confirmed") or after one (e.g. a flushed score
        # update followed by a failed confirm), nothing from this
        # call may survive. A no-write early Err rolls back an empty
        # transaction, which is a harmless no-op.
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    (
        confirmed_event,
        completed_event,
        adv_events,
        created_events,
        ready_events,
    ) = result.unwrap()

    try:
        create_log_entry(
            'match-result-entered',
            confirmed_event.tournament_id,
            admin_id,
            data={
                'match_id': str(match_id),
                'scores': {str(key): score for key, score in scores.items()},
            },
            commit=False,
        )
    except Exception:
        tournament_repository.rollback_session()
        raise

    tournament_repository.commit_session()

    match_confirmed.send(None, event=confirmed_event)
    if completed_event is not None:
        tournament_completed.send(None, event=completed_event)
    for event in adv_events:
        contestant_advanced.send(None, event=event)
    for event in created_events:
        match_created.send(None, event=event)
    for event in ready_events:
        match_ready.send(None, event=event)
    return Ok(None)


def _validate_match_confirmable(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
) -> Result[None, str]:
    """Check that the match can be confirmed.

    Pure validation — no DB writes.
    """
    if match.confirmed_by is not None:
        return Err('Match is already confirmed.')

    if len(contestants) < 2:
        return Err(
            'Cannot confirm match with less than 2 contestants.'
        )

    for contestant in contestants:
        if contestant.score is None:
            return Err(
                'Cannot confirm match: '
                'all contestants must have scores.'
            )

    return Ok(None)


def _confirm_draw_impl(
    match: TournamentMatch,
    match_id: TournamentMatchID,
    initiator_id: UserID,
    tournament: Tournament,
) -> Result[
    tuple[
        MatchConfirmedEvent,
        TournamentCompletedEvent | None,
        list[ContestantAdvancedEvent],
        list[MatchCreatedEvent],
        list[MatchReadyEvent],
    ],
    str,
]:
    """Flush-only core of confirming a drawn match (round-robin only).

    Caller (``_confirm_match_impl``) owns the single commit and event
    dispatch. Returns the same event-tuple shape as
    ``_confirm_match_impl`` so its caller does not need to
    special-case the draw outcome.
    """
    if tournament.elimination_mode != EliminationMode.ROUND_ROBIN:
        return Err(
            'Match is a draw; a winner is required '
            'in this tournament mode.'
        )

    tournament_repository.confirm_match(
        match_id, initiator_id,
    )

    now = datetime.now(UTC)
    confirmed_event = MatchConfirmedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=match.tournament_id,
        match_id=match_id,
        winner_team_id=None,
        winner_participant_id=None,
    )
    return Ok((confirmed_event, None, [], [], []))


def _collect_ready_match_events(
    match_ids: set[TournamentMatchID],
    tournament_id: TournamentID,
    now: datetime,
) -> list[MatchReadyEvent]:
    """Return MatchReadyEvent for each match that has >= 2 contestants."""
    events = []
    for match_id in match_ids:
        contestants = tournament_repository.get_contestants_for_match(match_id)
        if len(contestants) >= 2:
            events.append(MatchReadyEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
                match_id=match_id,
            ))
    return events


def _advance_winner(
    match: TournamentMatch,
    winner: TournamentMatchToContestant,
    now: datetime,
) -> list[ContestantAdvancedEvent]:
    """Create winner entry in next match (flush only).

    Caller owns commit and event dispatch.
    """
    if match.next_match_id is None:
        return []

    contestant_id = TournamentMatchToContestantID(
        generate_uuid7()
    )
    new_contestant = TournamentMatchToContestant(
        id=contestant_id,
        tournament_match_id=match.next_match_id,
        team_id=winner.team_id,
        participant_id=winner.participant_id,
        score=None,
        created_at=now,
    )
    tournament_repository.create_match_contestant(new_contestant)

    return [
        ContestantAdvancedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match.next_match_id,
            from_match_id=match.id,
            advanced_team_id=winner.team_id,
            advanced_participant_id=winner.participant_id,
        )
    ]


def _advance_loser_to_lb(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
    winner: TournamentMatchToContestant,
    now: datetime,
    *,
    initiator_id: UserID | None = None,
) -> Result[list[ContestantAdvancedEvent], str]:
    """Route loser to losers bracket (flush only).

    Caller owns commit and event dispatch.
    """
    if match.loser_next_match_id is None:
        return Ok([])

    loser_result = _determine_loser(contestants, winner)
    if loser_result.is_err():
        return Err(loser_result.unwrap_err())
    loser = loser_result.unwrap()

    loser_contestant_id = TournamentMatchToContestantID(
        generate_uuid7()
    )
    loser_entry = TournamentMatchToContestant(
        id=loser_contestant_id,
        tournament_match_id=match.loser_next_match_id,
        team_id=loser.team_id,
        participant_id=loser.participant_id,
        score=None,
        created_at=now,
    )
    tournament_repository.create_match_contestant(loser_entry)

    events: list[ContestantAdvancedEvent] = [
        ContestantAdvancedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match.loser_next_match_id,
            from_match_id=match.id,
            advanced_team_id=loser.team_id,
            advanced_participant_id=loser.participant_id,
        )
    ]

    events += _try_lb_defwin_advance(
        match.loser_next_match_id,
        match.tournament_id,
        now,
        initiator_id=initiator_id,
    )

    return Ok(events)


def _try_lb_defwin_advance(
    lb_match_id: TournamentMatchID,
    tournament_id: TournamentID,
    now: datetime,
    *,
    initiator_id: UserID | None = None,
) -> list[ContestantAdvancedEvent]:
    """Auto-advance if LB match is a structural DEFWIN (flush only).

    Only one feeder remains after WBR0 DEFWIN nullification.
    Caller owns commit and event dispatch.
    """
    lb_contestants = (
        tournament_repository.get_contestants_for_match(
            lb_match_id
        )
    )
    if len(lb_contestants) != 1:
        return []

    incoming = tournament_repository.count_incoming_feeds(
        lb_match_id
    )
    if incoming > 1:
        return []

    lb_match = tournament_repository.find_match(lb_match_id)
    if lb_match is None or lb_match.next_match_id is None:
        return []

    sole = lb_contestants[0]
    adv_id = TournamentMatchToContestantID(generate_uuid7())
    advanced = TournamentMatchToContestant(
        id=adv_id,
        tournament_match_id=lb_match.next_match_id,
        team_id=sole.team_id,
        participant_id=sole.participant_id,
        score=None,
        created_at=now,
    )
    tournament_repository.create_match_contestant(advanced)

    # Auto-confirm the structural DEFWIN match
    if initiator_id is not None:
        tournament_repository.confirm_match(
            lb_match_id, initiator_id
        )

    return [
        ContestantAdvancedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=tournament_id,
            match_id=lb_match.next_match_id,
            from_match_id=lb_match_id,
            advanced_team_id=sole.team_id,
            advanced_participant_id=sole.participant_id,
        )
    ]


def _is_lb_champion_winner(
    match: TournamentMatch,
    winner: TournamentMatchToContestant,
) -> bool:
    """Check if the GF M1 winner came from the losers bracket.

    Finds the LB feeder match (bracket=LOSERS) and checks if
    the winner's identity matches the contestant advanced from it.
    """
    if match.bracket != Bracket.GRAND_FINAL:
        return False

    feeders = tournament_repository.find_feeder_matches(match.id)
    lb_feeder = next(
        (f for f in feeders if f.bracket == Bracket.LOSERS), None
    )
    if lb_feeder is None:
        return False

    # The LB feeder's winner was advanced to GF M1.
    lb_contestants = tournament_repository.get_contestants_for_match(
        lb_feeder.id
    )
    lb_winner_result = determine_match_winner(lb_contestants)
    if lb_winner_result.is_err():
        return False
    lb_winner = lb_winner_result.unwrap()
    if lb_winner is None:
        return False

    # Compare identity
    return (
        winner.team_id == lb_winner.team_id
        and winner.participant_id == lb_winner.participant_id
    )


def _create_bracket_reset(
    match: TournamentMatch,
    winner: TournamentMatchToContestant,
    contestants: list[TournamentMatchToContestant],
    now: datetime,
) -> tuple[TournamentMatchID, list]:
    """Create GF M2 bracket reset match and advance both contestants.

    Returns (gf_m2_id, events).
    """
    loser_result = _determine_loser(contestants, winner)
    loser = loser_result.unwrap()  # caller guarantees this succeeds

    # Caller only invokes this when LB champion won GF M1.
    # Therefore: winner = LB champ, loser = WB champ.
    wb_champ = loser   # WB champion lost GF M1
    lb_champ = winner  # LB champion won GF M1

    gf_m2_id = TournamentMatchID(generate_uuid7())
    gf_m2 = TournamentMatch(
        id=gf_m2_id,
        tournament_id=match.tournament_id,
        group_order=None,
        match_order=1,  # GF M1 = 0, GF M2 = 1
        round=0,
        next_match_id=None,  # GF M2 is the true terminal
        bracket=Bracket.GRAND_FINAL,
        loser_next_match_id=None,
        confirmed_by=None,
        created_at=now,
    )
    tournament_repository.create_match(gf_m2)

    # Wire GF M1 → GF M2
    tournament_repository.set_next_match_id_flush(
        match.id, gf_m2_id
    )

    events = [
        MatchCreatedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=gf_m2_id,
        )
    ]

    # Insert WB champion as top slot (slot 0)
    wb_contestant_id = TournamentMatchToContestantID(generate_uuid7())
    tournament_repository.create_match_contestant(
        TournamentMatchToContestant(
            id=wb_contestant_id,
            tournament_match_id=gf_m2_id,
            team_id=wb_champ.team_id,
            participant_id=wb_champ.participant_id,
            score=None,
            created_at=now,
        )
    )

    # Insert LB champion as bottom slot (slot 1)
    lb_contestant_id = TournamentMatchToContestantID(generate_uuid7())
    tournament_repository.create_match_contestant(
        TournamentMatchToContestant(
            id=lb_contestant_id,
            tournament_match_id=gf_m2_id,
            team_id=lb_champ.team_id,
            participant_id=lb_champ.participant_id,
            score=None,
            created_at=now,
        )
    )

    return gf_m2_id, events


def is_deciding_match(
    match: TournamentMatch,
    tournament: Tournament,
) -> bool:
    """Return `True` if the match's result decides the tournament."""
    if match.bracket == Bracket.THIRD_PLACE:
        return False

    if tournament.elimination_mode not in (
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ):
        return False

    # FFA matches never have a next match, so that test cannot apply.
    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        if tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
            return match.bracket == Bracket.GRAND_FINAL
        round_matches = tournament_repository.get_matches_for_round(
            tournament.id, match.round, bracket=None,
        )
        return len(round_matches) == 1

    return match.next_match_id is None


def retraction_reverts_completion(
    match: TournamentMatch,
    tournament: Tournament,
) -> bool:
    """Return `True` if retracting the match reopens the tournament."""
    return (
        tournament.tournament_status == TournamentStatus.COMPLETED
        and is_deciding_match(match, tournament)
    )


def ffa_round_already_advanced(
    match: TournamentMatch,
    tournament: Tournament,
) -> bool:
    """Return `True` if a later FFA round was built from the match's round."""
    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        return False

    if match.bracket == Bracket.GRAND_FINAL:
        return False

    matches = tournament_repository.get_matches_for_tournament(tournament.id)

    # Double elimination seeds its grand final from both pools.
    if match.bracket is not None and any(
        m.bracket == Bracket.GRAND_FINAL for m in matches
    ):
        return True

    return any(
        m.bracket == match.bracket and (m.round or 0) > (match.round or 0)
        for m in matches
    )


def _try_auto_complete_tournament(
    match: TournamentMatch,
    tournament: Tournament,
    winner: TournamentMatchToContestant,
) -> Result[bool, str]:
    """Complete the tournament if this match decides it (flush only).

    Returns ``Ok(True)`` when the tournament was completed,
    ``Ok(False)`` when the condition does not apply.
    Caller owns commit and event dispatch.
    """
    if not is_deciding_match(match, tournament):
        return Ok(False)

    winner_set = tournament_repository.set_tournament_winner(
        match.tournament_id,
        winner_team_id=winner.team_id,
        winner_participant_id=winner.participant_id,
    )
    if winner_set.is_err():
        return Err(winner_set.unwrap_err())

    status_set = tournament_repository.set_tournament_status_flush(
        match.tournament_id,
        TournamentStatus.COMPLETED,
    )
    if status_set.is_err():
        return Err(status_set.unwrap_err())

    return Ok(True)


def _confirm_match_impl(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    *,
    _locks_held: bool = False,
) -> Result[
    tuple[
        MatchConfirmedEvent,
        TournamentCompletedEvent | None,
        list[ContestantAdvancedEvent],
        list[MatchCreatedEvent],
        list[MatchReadyEvent],
    ],
    str,
]:
    """Flush-only, event-collecting core of confirm_match.

    Performs all confirmation logic (winner advance, loser-to-LB
    advance, DE bracket reset, tournament auto-complete) using
    repository flush calls. Collects domain events rather than
    dispatching them. The caller is responsible for the single
    commit() and event dispatch -- mirrors ``_unconfirm_match_flush``.

    Locks the whole reachable bracket in id order first, via
    ``_lock_reachable_matches``, before taking the row lock on
    ``match_id`` itself -- the same ordered acquisition
    ``unconfirm_match`` / ``correct_match_result`` use. Confirming a
    match advances the winner (and, in DE, the loser) into whatever
    ``match.next_match_id`` / ``match.loser_next_match_id`` name, which
    is exactly the reachable set those functions lock; without this,
    this path and a concurrent retraction could acquire the same rows
    in opposing orders and deadlock (workspace-k9hm). Reached from
    every writer that advances a contestant downstream --
    ``confirm_match``, ``admin_set_and_confirm_match`` (and, through
    it, ``correct_match_result``'s score re-application), and
    ``set_match_scores`` (which calls ``confirm_match``). The FFA
    paths (``confirm_ffa_match``, ``set_ffa_placements``) do not
    advance a contestant into another match at all -- FFA does not use
    ``next_match_id`` -- so they are correctly outside this set.

    ``_locks_held`` says the caller already took that ordered lock
    for THIS match, in this transaction, and nothing has committed
    since. Only the three in-module callers that demonstrably did so
    pass it. It is not an optimisation of the lock itself -- re-taking
    a row lock this transaction already holds is nearly free -- but of
    the two full-bracket SELECTs ``_lock_reachable_matches`` issues to
    compute and then re-verify the reachable set. Those are the
    expensive part, and on a 256-entrant double-elimination bracket
    they materialise ~511 rows each. Skipping them is sound because
    the set cannot grow while we hold it: another transaction can
    only add an edge into or out of the reachable set by UPDATEing a
    match row we already have locked, so it blocks until we commit.
    The default is False, so a caller that forgets simply pays for
    the locking again rather than losing it.
    """
    if not _locks_held:
        _lock_reachable_matches(match_id)
    match = tournament_repository.get_match_for_update(match_id)
    contestants = (
        tournament_repository.get_contestants_for_match(match_id)
    )
    validation = _validate_match_confirmable(match, contestants)
    if validation.is_err():
        return validation
    tournament = tournament_repository.get_tournament(
        match.tournament_id,
    )

    # A free-for-all match is decided by placements and confirmed by
    # confirm_ffa_match, which does NOT come through here. Letting one
    # through this path confirms it on raw scores while placement and
    # points -- the only numbers the FFA standings read -- stay NULL,
    # and _try_auto_complete_tournament below then declares a
    # tournament winner from those scores: for FFA+SE any round with a
    # single group satisfies its terminal test, so confirming the
    # first group of a four-player tournament completes it outright.
    # COMPLETED is a terminal status, so that is not merely wrong but
    # hard to undo.
    #
    # The two blueprints already refuse this per route, but only for
    # the forms they render. This is the choke point every bracket
    # confirmation actually passes through -- confirm_match,
    # set_match_scores (the participant score submission, which needs
    # no permission beyond being in the match),
    # admin_set_and_confirm_match, and correct_match_result's score
    # re-application -- so the rule is enforced once, here, for
    # direct POSTs as well.
    if _decided_by_placements(tournament):
        return Err(PLACEMENT_FORMAT_CONFIRM_ERROR)

    winner_result = determine_match_winner(contestants)
    if winner_result.is_err():
        return Err(winner_result.unwrap_err())
    winner = winner_result.unwrap()
    if winner is None:
        return _confirm_draw_impl(
            match, match_id, initiator_id, tournament,
        )
    tournament_repository.confirm_match(
        match_id, initiator_id,
    )
    now = datetime.now(UTC)
    adv_events = _advance_winner(match, winner, now)
    if match.loser_next_match_id is not None:
        lb = _advance_loser_to_lb(
            match, contestants, winner, now,
            initiator_id=initiator_id,
        )
        if lb.is_err():
            return Err(lb.unwrap_err())
        adv_events += lb.unwrap()
    # ---- Bracket Reset (DE Grand Final) ----
    # If this is GF M1, tournament is DE with bracket reset enabled,
    # and the LB champion won, create GF M2.
    bracket_reset_events = []
    if (
        match.bracket == Bracket.GRAND_FINAL
        and match.match_order == 0  # GF M1
        and match.next_match_id is None  # still terminal (no existing GF M2)
        and tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION
        and tournament.use_bracket_reset
        and _is_lb_champion_winner(match, winner)
    ):
        gf_m2_id, reset_events = _create_bracket_reset(
            match, winner, contestants, now,
        )
        bracket_reset_events = reset_events
        # Re-read match after wiring so _try_auto_complete sees next_match_id
        match = tournament_repository.get_match(match_id)
    comp = _try_auto_complete_tournament(
        match, tournament, winner,
    )
    if comp.is_err():
        return comp
    tournament_was_completed = comp.unwrap()
    tid = match.tournament_id

    confirmed_event = MatchConfirmedEvent(
        occurred_at=now, initiator=None,
        tournament_id=tid, match_id=match_id,
        winner_team_id=winner.team_id,
        winner_participant_id=winner.participant_id,
    )
    completed_event = None
    if tournament_was_completed:
        completed_event = TournamentCompletedEvent(
            occurred_at=now, initiator=None,
            tournament_id=tid,
            winner_team_id=winner.team_id,
            winner_participant_id=winner.participant_id,
        )

    destination_match_ids: set[TournamentMatchID] = set()
    for event in adv_events:
        destination_match_ids.add(event.match_id)
    for event in bracket_reset_events:
        destination_match_ids.add(event.match_id)
    ready_events: list[MatchReadyEvent] = []
    if destination_match_ids:
        ready_events = _collect_ready_match_events(
            destination_match_ids, tid, now
        )

    return Ok(
        (confirmed_event, completed_event, adv_events, bracket_reset_events, ready_events)
    )


def confirm_match(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    *,
    _locks_held: bool = False,
) -> Result[None, str]:
    """Confirm a match result.

    Uses a single DB commit and dispatches all collected events
    afterwards -- see ``_confirm_match_impl``.

    Post-rollback note: when this function returns ``Err``, the
    database session has been rolled back. Any ORM-managed objects
    fetched before this call may be expired or detached. Callers must
    NOT access attributes on those objects after receiving an ``Err``
    result.

    ``_locks_held`` is internal and passed straight through to
    ``_confirm_match_impl`` -- see its docstring. Only
    ``set_match_scores``, which took the ordered lock for this match
    itself, sets it. External callers must leave it alone.
    """
    result = _confirm_match_impl(
        match_id, initiator_id, _locks_held=_locks_held
    )
    if result.is_err():
        # _confirm_match_impl is flush-only and can fail after writing
        # (_advance_loser_to_lb, _try_auto_complete_tournament). Roll
        # back so those writes are not left for the next commit, and
        # so its reachable-set locks are released. Mirrors
        # admin_set_and_confirm_match / unconfirm_match.
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    (
        confirmed_event,
        completed_event,
        adv_events,
        created_events,
        ready_events,
    ) = result.unwrap()

    tournament_repository.commit_session()

    match_confirmed.send(None, event=confirmed_event)
    if completed_event is not None:
        tournament_completed.send(None, event=completed_event)
    for event in adv_events:
        contestant_advanced.send(None, event=event)
    for event in created_events:
        match_created.send(None, event=event)
    for event in ready_events:
        match_ready.send(None, event=event)
    return Ok(None)


def _unconfirm_match_impl(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    _visited: set[TournamentMatchID] | None = None,
    _match: TournamentMatch | None = None,
) -> Result[tuple[list[MatchUnconfirmedEvent], list[MatchDeletedEvent], bool], str]:
    """Flush-only, event-collecting helper for unconfirm_match.

    Performs all unconfirmation logic (including cascade) using
    repository flush calls for intermediate operations.  Collects
    and returns domain events rather than dispatching them.  The
    caller is responsible for the single ``commit()`` and event
    dispatch.

    Returns ``Ok((events, deleted_events, tournament_was_uncompleted))``
    where ``tournament_was_uncompleted`` is ``True`` when tournament
    winner/status was reverted (elimination terminal match).

    When ``_match`` is provided (pre-locked by the caller) it
    is used directly.  Recursive calls acquire their own row
    locks via ``get_match_for_update`` to prevent TOCTOU races
    on concurrent unconfirmations.
    """
    if _visited is None:
        _visited = set()
    if match_id in _visited:
        return Err('Circular match reference detected.')
    _visited.add(match_id)

    # Use provided match or acquire row lock.
    if _match is not None:
        match = _match
    else:
        match = tournament_repository.get_match_for_update(
            match_id
        )

    if match.confirmed_by is None:
        return Err('Match is not confirmed.')

    collected_events: list[MatchUnconfirmedEvent] = []
    deleted_events: list[MatchDeletedEvent] = []
    tournament_was_uncompleted = False

    contestants = tournament_repository.get_contestants_for_match(
        match_id
    )

    # Determine winner for cascade retraction.
    winner_result = determine_match_winner(contestants)

    winner = (
        winner_result.unwrap() if winner_result.is_ok() else None
    )

    if match.next_match_id is not None and winner is not None:
        # find_match, not get_match: get_match RAISES on an unknown
        # ID, so the `is not None` guard below was unreachable and a
        # dangling next_match_id turned the whole cascade into an
        # uncaught ValueError (a 500 on the correction route).
        # classify_result_correction, which the panel shows the admin
        # as a preview of this cascade, deliberately treats a dangling
        # routing entry as absent -- so the preview said "safe" and
        # the act crashed. Same contract on both sides now.
        next_match = tournament_repository.find_match(
            match.next_match_id
        )
        if (
            next_match is not None
            and next_match.confirmed_by is not None
        ):
            # Recursively unconfirm downstream match.
            cascade_result = _unconfirm_match_impl(
                match.next_match_id,
                initiator_id,
                _visited=_visited,
            )
            if cascade_result.is_err():
                return cascade_result
            cascade_events, cascade_deleted, cascade_uncompleted = (
                cascade_result.unwrap()
            )
            collected_events.extend(cascade_events)
            deleted_events.extend(cascade_deleted)
            tournament_was_uncompleted = (
                tournament_was_uncompleted or cascade_uncompleted
            )

        # Remove advanced contestant from next match.
        tournament_repository.delete_contestant_from_match(
            match.next_match_id,
            team_id=winner.team_id,
            participant_id=winner.participant_id,
        )

    # Retract loser from losers bracket (DE only)
    if (
        match.loser_next_match_id is not None
        and winner_result.is_ok()
        and winner_result.unwrap() is not None
    ):
        winner = winner_result.unwrap()
        loser_result = _determine_loser(contestants, winner)
        if loser_result.is_err():
            return Err(loser_result.unwrap_err())
        loser = loser_result.unwrap()

        # find_match, not get_match -- same reason as the
        # next_match_id lookup above.
        loser_next_match = tournament_repository.find_match(
            match.loser_next_match_id
        )
        if (
            loser_next_match is not None
            and loser_next_match.confirmed_by is not None
        ):
            # Recursively unconfirm downstream LB match.
            cascade_result = _unconfirm_match_impl(
                match.loser_next_match_id,
                initiator_id,
                _visited=_visited,
            )
            if cascade_result.is_err():
                return cascade_result
            cascade_events, cascade_deleted, cascade_uncompleted = (
                cascade_result.unwrap()
            )
            collected_events.extend(cascade_events)
            deleted_events.extend(cascade_deleted)
            tournament_was_uncompleted = (
                tournament_was_uncompleted or cascade_uncompleted
            )

        # Remove advanced loser from LB match.
        tournament_repository.delete_contestant_from_match(
            match.loser_next_match_id,
            team_id=loser.team_id,
            participant_id=loser.participant_id,
        )

        # Retract LB auto-advance (structural DEFWIN undo).
        # Guard: only delete if the contestant actually exists
        # in the downstream match (may not if no DEFWIN occurred).
        if (
            loser_next_match is not None
            and loser_next_match.next_match_id is not None
        ):
            existing = (
                tournament_repository.find_contestant_for_match(
                    loser_next_match.next_match_id,
                    team_id=loser.team_id,
                    participant_id=loser.participant_id,
                )
            )
            if existing is not None:
                tournament_repository.delete_contestant_from_match(
                    loser_next_match.next_match_id,
                    team_id=loser.team_id,
                    participant_id=loser.participant_id,
                )

    # ---- Bracket Reset cleanup (DE Grand Final) ----
    # If this is GF M1 and a dynamically-created GF M2 exists,
    # clean up the remaining contestant and delete GF M2.
    # Note: the existing cascade already handled:
    #   - recursive unconfirm of GF M2
    #   - removal of winner contestant from GF M2
    # We still need to remove the loser contestant and delete the match.
    if (
        match.bracket == Bracket.GRAND_FINAL
        and match.match_order == 0  # GF M1
        and match.next_match_id is not None
    ):
        gf_m2 = tournament_repository.find_match(match.next_match_id)
        if (
            gf_m2 is not None
            and gf_m2.bracket == Bracket.GRAND_FINAL
            and gf_m2.match_order == 1  # GF M2
        ):
            # Delete GF M2 children in FK order: comments → contestants → match
            tournament_repository.delete_comments_for_match_flush(
                gf_m2.id
            )
            tournament_repository.delete_contestants_for_match_flush(
                gf_m2.id
            )
            # Null out GF M1's next_match_id BEFORE deleting GF M2 (FK)
            tournament_repository.set_next_match_id_flush(
                match_id, None
            )
            tournament_repository.delete_match_flush(gf_m2.id)

            deleted_events.append(MatchDeletedEvent(
                occurred_at=datetime.now(UTC),
                initiator=None,
                tournament_id=match.tournament_id,
                match_id=gf_m2.id,
            ))

            # CRITICAL: Re-read match so the terminal-match check below
            # sees next_match_id=None and reverts tournament completion.
            match = tournament_repository.get_match(match_id)

    # Avoid the tournament lookup for matches that cannot decide it.
    if match.next_match_id is None:
        tournament = tournament_repository.get_tournament(
            match.tournament_id
        )
        if retraction_reverts_completion(match, tournament):
            winner_result2 = (
                tournament_repository.set_tournament_winner(
                    match.tournament_id,
                    winner_team_id=None,
                    winner_participant_id=None,
                )
            )
            if winner_result2.is_err():
                return Err(winner_result2.unwrap_err())
            status_result = (
                tournament_repository.set_tournament_status_flush(
                    match.tournament_id,
                    TournamentStatus.ONGOING,
                )
            )
            if status_result.is_err():
                return Err(status_result.unwrap_err())
            tournament_was_uncompleted = True

    tournament_repository.unconfirm_match(match_id)
    # Clear scores to prevent stale data from being re-confirmed.
    tournament_repository.clear_contestant_scores(match_id)

    now = datetime.now(UTC)
    collected_events.append(
        MatchUnconfirmedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match_id,
            unconfirmed_by=initiator_id,
        )
    )

    return Ok((collected_events, deleted_events, tournament_was_uncompleted))


def _unconfirm_match_flush(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    *,
    reason: str | None = None,
    _classification: (
        tuple[CorrectionCase, list[TournamentMatchID]] | None
    ) = None,
) -> Result[
    tuple[
        list[MatchUnconfirmedEvent],
        list[MatchDeletedEvent],
        bool,
        TournamentID,
    ],
    str,
]:
    """Flush-only, event-collecting core of unconfirm_match.

    Locks the initial match row (TOCTOU guard), runs the retraction
    cascade via ``_unconfirm_match_impl``, and -- when ``reason`` is
    given -- stages a ``'match-result-retracted'`` audit log entry
    (flush-only) carrying the scores the cascade destroyed, read
    before it clears them: this match's own under
    ``retracted_scores``, and those of every confirmed downstream
    match the cascade also clears under
    ``cascaded_retracted_scores`` (see ``_snapshot_cascade_scores``).

    Does NOT call ``_lock_reachable_matches``: the caller must already
    hold the reachable-set lock (``unconfirm_match`` takes it itself;
    ``correct_match_result`` takes it once, up front, for the whole
    correction -- see workspace-ubjc / workspace-ukkj). Does NOT
    commit and does NOT dispatch events -- the caller is responsible
    for the single ``commit()`` and dispatch, and, since
    ``create_log_entry`` can raise, for rolling back on an exception
    from this call as well as on an ``Err`` result.

    ``_classification`` is the caller's already-computed
    ``classify_result_correction`` result, handed to
    ``_snapshot_cascade_scores`` instead of letting it walk the
    bracket a second time. ``correct_match_result`` passes the very
    classification its acknowledgement gate decided on;
    ``unconfirm_match`` passes nothing and the snapshot classifies
    for itself.

    Returns ``Ok((events, deleted_events, tournament_was_uncompleted,
    tournament_id))``.
    """
    # Lock the match row to prevent TOCTOU races. Still raises on an
    # unknown match ID, which _lock_reachable_matches (caller's job)
    # leaves to it.
    match = tournament_repository.get_match_for_update(match_id)

    tournament = tournament_repository.get_tournament(match.tournament_id)
    if ffa_round_already_advanced(match, tournament):
        return Err(
            'A later round has already been built from this result, so it '
            'can no longer be unconfirmed.'
        )

    # Before the cascade clears them -- the subject's own scores, and
    # the scores of every confirmed downstream match the cascade will
    # clear along with it. Both reads must happen here, while the row
    # lock is held and before _unconfirm_match_impl runs; afterwards
    # the numbers exist nowhere.
    retracted_scores = None
    retracted_placements = None
    cascaded_retracted_scores = None
    if reason is not None:
        contestants = tournament_repository.get_contestants_for_match(
            match_id
        )
        retracted_scores = _snapshot_contestant_scores(
            match_id, contestants=contestants
        )
        retracted_placements = _snapshot_contestant_placements(contestants)
        cascaded_retracted_scores = _snapshot_cascade_scores(
            match_id, classification=_classification
        )

    result = _unconfirm_match_impl(
        match_id, initiator_id, _match=match,
    )
    if result.is_err():
        return result

    events, deleted_events, tournament_was_uncompleted = result.unwrap()

    if reason is not None:
        # Staged after the cascade's own flushes, so this INSERT does
        # not flush ahead of it. Rides the caller's single commit and
        # is discarded with it on rollback. May raise -- the caller
        # must roll back on that too, not just on an Err return.
        data = {
            'match_id': str(match_id),
            'reason': reason,
            'retracted_scores': retracted_scores,
            # What the cascade destroyed beyond this match. Empty
            # for the ordinary case; the whole point of the entry
            # for an acknowledged CONFIRMED_DOWNSTREAM correction.
            'cascaded_retracted_scores': cascaded_retracted_scores,
        }
        # FFA results are placements, not scores.
        if retracted_placements:
            data['retracted_placements'] = retracted_placements
        create_log_entry(
            'match-result-retracted',
            match.tournament_id,
            initiator_id,
            data=data,
            commit=False,
        )

    return Ok(
        (events, deleted_events, tournament_was_uncompleted, match.tournament_id)
    )


def unconfirm_match(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    *,
    reason: str | None = None,
) -> Result[None, str]:
    """Unconfirm a match and cascade-retract advanced contestants.

    Uses a single DB commit for the entire cascade and dispatches
    all collected events afterwards -- see ``_unconfirm_match_flush``.

    Acquires row locks (SELECT ... FOR UPDATE) on every match the
    cascade can reach, in one id-ordered acquisition, before locking
    the initial match -- see ``_lock_reachable_matches``. This both
    prevents concurrent unconfirmation races and keeps this path from
    acquiring rows in an order opposing ``correct_match_result``'s,
    which would deadlock the two against each other.

    ``reason`` is optional; when provided, a ``'match-result-retracted'``
    audit log entry is written. The entry is staged (flush-only) in
    the same transaction as the cascade, after the row lock is
    acquired, so it is discarded together with the state change on
    rollback and never changes the cascade behavior itself. When
    ``reason`` is ``None``, no entry is written.

    The entry carries the scores the retraction destroyed -- this
    match's own and those of every confirmed downstream match the
    cascade clears with it. They are read under the row lock below,
    before the cascade clears them.
    """
    # Lock the whole reachable bracket in id order first, so the
    # cascade's own traversal-order locks below only re-take rows
    # this transaction already holds.
    _lock_reachable_matches(match_id)

    try:
        result = _unconfirm_match_flush(match_id, initiator_id, reason=reason)
    except Exception:
        # _unconfirm_match_flush's cascade may have already flushed
        # partial deletions and status changes, or its log-entry stage
        # may have raised after those flushes. Either way, roll back
        # so those flushed-but-uncommitted writes are not left in the
        # live session to be committed by whatever calls
        # commit_session() next -- this function must not depend on
        # request-lifecycle teardown (db.session.remove()) to keep the
        # database consistent, since it also runs outside a request
        # (e.g. from CLI commands).
        tournament_repository.rollback_session()
        raise

    if result.is_err():
        # The cascade may have already flushed partial deletions and
        # status changes before failing (e.g. a circular reference
        # detected partway through, or a repository Err from setting
        # the tournament winner/status). Roll back so those flushed-
        # but-uncommitted writes are not left in the live session to
        # be committed by whatever calls commit_session() next.
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    events, deleted_events, tournament_was_uncompleted, tournament_id = (
        result.unwrap()
    )

    # Single commit for the entire cascade.
    tournament_repository.commit_session()

    # Dispatch all collected events after commit.
    for event in events:
        match_unconfirmed.send(None, event=event)
    for event in deleted_events:
        match_deleted.send(None, event=event)

    # Dispatch TournamentUncompletedEvent only for elimination
    # modes where tournament state was actually reverted.
    if tournament_was_uncompleted:
        now = datetime.now(UTC)
        tournament_uncompleted.send(
            None,
            event=TournamentUncompletedEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
            ),
        )

    return Ok(None)


def _collect_reachable_match_ids(
    match: TournamentMatch,
    matches_by_id: dict[TournamentMatchID, TournamentMatch],
) -> list[TournamentMatchID]:
    """Return every match a retraction from ``match`` could reach.

    Unlike ``classify_result_correction`` this does NOT stop at an
    unconfirmed match: confirmation state is what a concurrent admin
    can change, so a lock set derived from it would be the wrong set.
    Includes the subject match itself.
    """
    reachable: list[TournamentMatchID] = [match.id]
    visited: set[TournamentMatchID] = {match.id}
    frontier = [match]

    while frontier:
        next_frontier = []
        for current in frontier:
            for downstream_id in (
                current.next_match_id,
                current.loser_next_match_id,
            ):
                if downstream_id is None or downstream_id in visited:
                    continue
                downstream = matches_by_id.get(downstream_id)
                if downstream is None:
                    continue
                visited.add(downstream_id)
                reachable.append(downstream_id)
                next_frontier.append(downstream)
        frontier = next_frontier

    return reachable


_MAX_LOCK_REFRESH_ROUNDS = 3


def _as_match_id(match_id: TournamentMatchID) -> TournamentMatchID:
    """Normalise a match ID to a real ``UUID``.

    ``TournamentMatchID`` is a ``NewType``, i.e. a no-op at runtime, so
    a blueprint that wraps a raw URL segment hands this module a
    ``str``. Every query still works -- SQLAlchemy coerces on bind --
    but the UUID-keyed dict and set lookups below do not: a ``str`` key
    matches no entry, so ``_lock_reachable_matches`` would fall back to
    its pre-lock snapshot of the subject match on every refresh round
    and never observe an edge committed in between, and
    ``classify_result_correction``'s cycle guard would not recognise
    the subject match. Coerce once, at the entry points that depend on
    the key type, rather than trusting every caller.
    """
    if isinstance(match_id, UUID):
        return match_id
    return TournamentMatchID(UUID(str(match_id)))


def _lock_reachable_matches(match_id: TournamentMatchID) -> None:
    """Row-lock every match a retraction OR a confirmation from
    ``match_id`` can reach.

    Best effort and side-effect only: an unknown match locks nothing
    and is left for the caller to report, so every entry point keeps
    the not-found contract it already had.

    The point is a SINGLE id-ordered acquisition for the whole
    reachable set. Both the retraction cascade (``_unconfirm_match_impl``,
    taking its own per-node ``get_match_for_update`` locks in traversal
    order as it recurses) and confirmation's own downstream advance
    (``_advance_winner`` / ``_advance_loser_to_lb`` writing into
    ``match.next_match_id`` / ``match.loser_next_match_id``) touch
    exactly this reachable set; once this has run in the same
    transaction, those writers only re-take locks already held, so no
    two callers can acquire the same rows in opposing orders and
    deadlock (workspace-k9hm). Covered callers -- everything that
    retracts or advances a contestant into another match -- come
    through here first: ``unconfirm_match``, ``correct_match_result``,
    and, via ``_confirm_match_impl``, ``confirm_match``,
    ``admin_set_and_confirm_match`` and ``set_match_scores``. The FFA
    writers (``confirm_ffa_match``, ``set_ffa_placements``) do not
    advance a contestant into another match -- FFA does not use
    ``next_match_id`` -- so they are correctly not covered.

    The reachable set is derived from an unlocked read, so it is
    recomputed after locking and the lock re-taken while it keeps
    growing: a transaction that commits a new edge in between (a DE
    bracket reset wiring GF M1 to a fresh GF M2) can make a match
    reachable that the first pass did not lock. In practice this
    settles on the second pass. Re-locking passes the whole set, not
    just the new IDs, so the acquisition order stays stable.

    Each round's re-read uses the ``_fresh`` (``populate_existing``)
    repository variant, not the plain one. Every match in this
    tournament is already in the identity map by the second round
    (this function loaded them all in round one), so a plain re-read
    would hand back byte-identical, pre-lock objects: the growth
    check below could never observe a newly committed edge and the
    whole retry loop would be a no-op (workspace-ubjc).

    ``match_id`` is normalised to a real ``UUID`` first: the refresh
    round below looks the subject match up in a ``UUID``-keyed index,
    and a ``str`` ID (what a ``NewType`` wrap of a URL segment yields)
    would miss it every time and silently pin the walk to the pre-lock
    snapshot -- turning the whole loop back into the no-op it exists
    to avoid.
    """
    match_id = _as_match_id(match_id)

    subject = tournament_repository.find_match(match_id)
    if subject is None:
        return

    # Every writer locks the tournament row before any match row.
    tournament_repository.lock_tournament_for_update(subject.tournament_id)

    locked: set[TournamentMatchID] = set()

    for _ in range(_MAX_LOCK_REFRESH_ROUNDS):
        matches_by_id = {
            m.id: m
            for m in (
                tournament_repository.get_matches_for_tournament_ordered_fresh(
                    subject.tournament_id
                )
            )
        }
        current = matches_by_id.get(match_id, subject)
        reachable = set(
            _collect_reachable_match_ids(current, matches_by_id)
        )
        if reachable <= locked:
            return

        locked |= reachable
        tournament_repository.lock_matches_for_update(sorted(locked))

    logger.warning(
        'Reachable match set for %s kept growing over %d lock rounds; '
        'proceeding with %d locked matches.',
        match_id,
        _MAX_LOCK_REFRESH_ROUNDS,
        len(locked),
    )


def acknowledgement_match_ids(
    case: CorrectionCase,
    affected_matches: Iterable[TournamentMatch],
) -> list[TournamentMatchID]:
    """Return the affected matches an acknowledgement has to cover."""
    if case is CorrectionCase.BRACKET_RESET_DELETION:
        return [m.id for m in affected_matches]
    if case is CorrectionCase.CONFIRMED_DOWNSTREAM:
        return [m.id for m in affected_matches if m.confirmed_by is not None]
    return []


def classify_result_correction(
    match_id: TournamentMatchID,
) -> Result[tuple[CorrectionCase, list[TournamentMatchID]], str]:
    """Classify a result correction by what lies downstream.

    Walks the SAME transitive closure that ``_unconfirm_match_impl``
    actually retracts, not just the direct downstream matches: from
    ``match_id``, each match's ``next_match_id`` / ``loser_next_match_id``
    (the inverse of ``find_feeder_matches`` traversal) is added to the
    affected set, and traversal continues past a downstream match only
    if it is itself confirmed -- exactly the condition
    ``_unconfirm_match_impl`` uses to decide whether to recurse. A
    round trip back to an already-visited match (a cycle) simply stops
    traversing there instead of raising, since classification must
    never error on a bracket topology the cascade itself already
    tolerates.

    - no downstream at all -> NO_DOWNSTREAM (correct freely)
    - downstream exists, all unconfirmed -> UNCONFIRMED_DOWNSTREAM
      (warn, recalculate)
    - any affected match is confirmed -> CONFIRMED_DOWNSTREAM
      (critical warning + explicit acknowledgement)
    - subject is DE grand final M1 and the bracket-reset match GF M2
      exists -> BRACKET_RESET_DELETION (critical warning + explicit
      acknowledgement). Checked last and overriding, because the
      cascade DELETES GF M2 rather than retracting it; the two
      retraction cases above would both describe that wrongly.

    Two cascade effects are deliberately accounted for here even
    though the confirmed-only recursion does not reach them: the
    GF M2 deletion above, and the structural-DEFWIN undo that strips
    the loser's auto-advanced row from an unconfirmed LB match's own
    next match.

    Returns the affected downstream match IDs (excluding the subject
    match itself) for warning text, in breadth-first order with
    ``next_match_id`` visited before ``loser_next_match_id``.

    Uses the ``_fresh`` (``populate_existing``) repository reads, not
    the plain ones. ``correct_match_result`` calls this specifically
    *after* ``_lock_reachable_matches``, so the ack_critical gate
    below decides on post-lock confirmation state -- that is the
    whole point of locking before classifying. Every match in this
    bracket is already in the identity map from that locking pass, so
    a plain (non-``_fresh``) read here would silently hand back the
    same pre-lock objects: a second admin who confirms a downstream
    match between the first unlocked read and the lock acquisition
    would be invisible, and the acknowledgement gate this function
    drives would decide on stale data (workspace-ubjc). This function
    is also called unlocked, from the admin correction-preview view;
    ``_fresh`` reads are harmless there too, since the SELECT already
    runs regardless -- it only changes whether a cached instance's
    attributes get refreshed from it.

    ``match_id`` is normalised to a real ``UUID`` first: the cycle
    guard below seeds its ``visited`` set with it and compares it
    against ``UUID`` routing columns, so a ``str`` ID would leave the
    subject match unguarded and let a bracket edge routing back to it
    report the subject as its own downstream.
    """
    match_id = _as_match_id(match_id)

    match = tournament_repository.find_match_fresh(match_id)
    if match is None:
        return Err(f'Unknown match ID "{match_id}".')

    # Index the tournament's matches once. The walk below used to call
    # find_match per node, which put one query per bracket node on a
    # page admins reload throughout an event; this keeps the whole
    # classification at two queries regardless of bracket size.
    matches_by_id = {
        m.id: m
        for m in (
            tournament_repository.get_matches_for_tournament_ordered_fresh(
                match.tournament_id
            )
        )
    }

    affected: list[TournamentMatchID] = []
    any_confirmed = False
    visited: set[TournamentMatchID] = {match_id}
    frontier = [match]

    while frontier:
        next_frontier = []
        for current in frontier:
            for downstream_id in (
                current.next_match_id,
                current.loser_next_match_id,
            ):
                if downstream_id is None or downstream_id in visited:
                    # Absent, or already seen -- a cycle guard, mirroring
                    # _unconfirm_match_impl's _visited set. Unlike that
                    # cascade, a cycle here is not an error: just stop
                    # traversing through this edge.
                    continue
                downstream = matches_by_id.get(downstream_id)
                if downstream is None:
                    # Dangling routing entry; treat as absent.
                    continue
                visited.add(downstream_id)
                # The cascade always removes the advanced contestant
                # from a direct downstream match, confirmed or not.
                affected.append(downstream.id)
                if downstream.confirmed_by is not None:
                    any_confirmed = True
                    # Only a confirmed downstream match is itself
                    # unconfirmed-and-cascaded further.
                    next_frontier.append(downstream)
                elif downstream_id == current.loser_next_match_id:
                    # Structural-DEFWIN undo: even when this LB match
                    # is unconfirmed -- and therefore not cascaded
                    # into -- _unconfirm_match_impl still deletes the
                    # loser's auto-advanced row from ITS next match.
                    # That grandchild is reachable by the cascade but
                    # not by the confirmed-only recursion above, so
                    # account for it here; otherwise a confirmed match
                    # can have its contestant set mutated without the
                    # acknowledgement this gate exists to require.
                    grandchild_id = downstream.next_match_id
                    if (
                        grandchild_id is not None
                        and grandchild_id not in visited
                    ):
                        grandchild = matches_by_id.get(grandchild_id)
                        if grandchild is not None:
                            visited.add(grandchild_id)
                            affected.append(grandchild_id)
                            if grandchild.confirmed_by is not None:
                                any_confirmed = True
        frontier = next_frontier

    # A correction of GF M1 is not a retraction of GF M2 -- the
    # bracket-reset cleanup in _unconfirm_match_impl deletes GF M2
    # entirely (comments, contestants, match row) and emits a
    # MatchDeletedEvent. This is the normal post-reset state (GF M1
    # confirmed, GF M2 pending), so reporting it as
    # UNCONFIRMED_DOWNSTREAM would tell the admin the match merely
    # loses a contestant, and would wave the deletion through with no
    # acknowledgement at all.
    if (
        match.bracket == Bracket.GRAND_FINAL
        and match.match_order == 0  # GF M1
        and match.next_match_id is not None
    ):
        gf_m2 = matches_by_id.get(match.next_match_id)
        if (
            gf_m2 is not None
            and gf_m2.bracket == Bracket.GRAND_FINAL
            and gf_m2.match_order == 1  # GF M2
        ):
            return Ok((CorrectionCase.BRACKET_RESET_DELETION, affected))

    if not affected:
        return Ok((CorrectionCase.NO_DOWNSTREAM, []))
    if any_confirmed:
        return Ok((CorrectionCase.CONFIRMED_DOWNSTREAM, affected))
    return Ok((CorrectionCase.UNCONFIRMED_DOWNSTREAM, affected))


def correct_match_result(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    *,
    reason: str,
    corrected_scores: (
        dict[TournamentParticipantID | TournamentTeamID, int] | None
    ) = None,
    ack_critical: bool = False,
    acknowledged_match_ids: Collection[TournamentMatchID] | None = None,
) -> Result[tuple[CorrectionCase, bool], str]:
    """Orchestrate an admin result correction with audit logging.

    If `acknowledged_match_ids` is given, a critical correction is
    refused when more matches are at stake than were acknowledged.

    Atomic: the retraction and the score re-application are ONE
    transaction, with a single commit at the end and every collected
    event dispatched only after it succeeds (workspace-ukkj). Earlier,
    ``unconfirm_match`` committed the retraction on its own before the
    corrected scores were applied by a second, separate commit; a
    process death, a concurrent confirm taking the match, or a
    deadlock abort between the two left the bracket retracted with the
    correction unapplied, recoverable only by hand. That is no longer
    reachable: every write below is flush-only until the single commit
    near the end, and any failure before it rolls the whole
    transaction back, so a correction either fully applies or never
    happened.

    Flow:

    1. Reject blank reasons.
    2. Lock every match the cascade could reach, via
       ``_lock_reachable_matches``, so the classification below
       cannot go stale between the decision and the act -- and so
       this path and a concurrent ``unconfirm_match`` acquire the
       same rows in the same order. Held for the ENTIRE correction:
       nothing below commits (and so nothing releases these locks)
       until the single commit at the end.
    3. Classify via ``classify_result_correction``, under those
       locks.
    4. Refuse CONFIRMED_DOWNSTREAM and BRACKET_RESET_DELETION unless
       ``ack_critical`` is set — there is no automatic chain
       correction; admins handle confirmed downstream matches, and
       the deletion of a bracket-reset match, manually after explicit
       acknowledgement.
    5. If corrected scores are supplied, validate them via the same
       read-only ``_validate_match_scores`` check
       ``admin_set_and_confirm_match`` uses, BEFORE anything
       destructive runs. A bad score (negative, over the max,
       missing, a disallowed draw, or a match that can never be
       confirmed because it holds fewer than 2 real contestants)
       must fail here so the retraction below never happens on
       invalid input. Scores identical to the current result are
       rejected too: a correction that corrects nothing still
       cascades.
    6. Retract the result via ``_unconfirm_match_flush(reason=...)``
       -- the flush-only core ``unconfirm_match`` itself is built on
       -- which stages (but does not commit) a
       ``'match-result-retracted'`` entry. This is the single writer
       of that entry.
    7. If corrected scores are supplied, stage a
       ``'match-result-corrected'`` entry (flush-only), then apply
       the scores via ``_admin_set_and_confirm_match_impl`` (the
       flush-only core ``admin_set_and_confirm_match`` itself is
       built on). When no scores are supplied, the retraction entry
       alone is the record.
    8. A single ``commit_session()`` covers everything staged above;
       any ``Err`` or exception before it rolls the whole transaction
       back instead, undoing the retraction along with it. Every
       collected event (from both the retraction and, when
       applicable, the re-confirmation) is dispatched only after that
       commit succeeds.

    Returns ``(case, scores_applied)`` on success.
    """
    if reason is None or not reason.strip():
        return Err('A correction reason is required.')

    # Normalise once, up front, so every ID-keyed step below -- the
    # lock refresh and the classification especially -- sees a real
    # UUID rather than whatever a caller's NewType wrap handed in.
    match_id = _as_match_id(match_id)

    subject = tournament_repository.find_match(match_id)
    if subject is None:
        return Err(f'Unknown match ID "{match_id}".')

    # The whole cascade below is built on next_match_id, which a
    # free-for-all bracket does not use, so a correction here would
    # degrade into a plain unconfirm wearing a correction's audit
    # entries and reason -- and, with corrected scores, would try to
    # re-confirm through the bracket path that PLACEMENT_FORMAT_
    # CONFIRM_ERROR refuses. Both blueprints already send the admin
    # to the unconfirm route instead; enforce it here too, so a
    # direct POST cannot retract an FFA result under a correction's
    # audit trail. Checked before _lock_reachable_matches so the
    # refusal takes no locks.
    tournament = tournament_repository.get_tournament(subject.tournament_id)
    if _decided_by_placements(tournament):
        return Err(PLACEMENT_FORMAT_CORRECTION_ERROR)

    # Lock every match the cascade could reach BEFORE classifying.
    # The ack gate is a decision about downstream confirmation state,
    # which a second admin can change; classified unlocked, a match
    # confirmed after the gate passed would be retracted without the
    # acknowledgement it requires.
    _lock_reachable_matches(match_id)

    # Past this point the reachable bracket is locked, so every early
    # return must release it. This runs outside a request too, where
    # no teardown rolls the session back.

    # A walkover has no determinable winner, so its retraction would
    # not cascade and it could never be confirmed again.
    contestants = tournament_repository.get_contestants_for_match(match_id)
    real_contestant_count = sum(
        1
        for c in contestants
        if c.participant_id is not None or c.team_id is not None
    )
    if real_contestant_count < 2:
        tournament_repository.rollback_session()
        return Err(
            'A walkover cannot be corrected: the match has fewer than '
            '2 contestants.'
        )

    classification_result = classify_result_correction(match_id)
    if classification_result.is_err():
        tournament_repository.rollback_session()
        return Err(classification_result.unwrap_err())
    classification = classification_result.unwrap()
    case, affected = classification

    if (
        case
        in (
            CorrectionCase.CONFIRMED_DOWNSTREAM,
            CorrectionCase.BRACKET_RESET_DELETION,
        )
        and not ack_critical
    ):
        tournament_repository.rollback_session()
        if case is CorrectionCase.BRACKET_RESET_DELETION:
            return Err(
                'Correcting the first grand final deletes the '
                'bracket-reset match; explicit acknowledgement is '
                'required.'
            )
        return Err(
            'Downstream matches already started or completed; '
            'explicit acknowledgement is required.'
        )

    if acknowledged_match_ids is not None:
        at_stake = set(
            acknowledgement_match_ids(
                case, tournament_repository.get_matches_by_ids(affected)
            )
        )
        acknowledged = {_as_match_id(i) for i in acknowledged_match_ids}
        if not at_stake <= acknowledged:
            tournament_repository.rollback_session()
            return Err(
                'The downstream matches changed since this page was '
                'loaded. Review the correction and acknowledge it again.'
            )

    previous_scores = None
    if corrected_scores:
        # Validate BEFORE the destructive retraction below. Without
        # this, an invalid score would still commit the
        # unconfirm_match cascade (retracting the result, and
        # anything downstream of it) and only then fail applying the
        # new scores -- leaving the bracket wiped for no reason.
        validation = _validate_match_scores(match_id, corrected_scores)
        if validation.is_err():
            tournament_repository.rollback_session()
            return Err(validation.unwrap_err())

        id_to_score = validation.unwrap()

        # Refuse a correction that corrects nothing: the panel
        # pre-fills the current scores, and the retraction would
        # still clear any confirmed downstream result.
        current_by_row = {c.id: c.score for c in contestants}
        if id_to_score and all(
            current_by_row.get(row_id) == score
            for row_id, score in id_to_score.items()
        ):
            tournament_repository.rollback_session()
            return Err(
                'The submitted scores are identical to the current '
                'result; nothing was corrected. Change a score, or '
                'clear every score field to retract the result.'
            )

        previous_scores = _snapshot_contestant_scores(
            match_id, contestants=contestants
        )

    tournament_id = subject.tournament_id

    # Retract (flush only) -- shares this transaction and the locks
    # taken above rather than committing on its own. Wrapped in
    # try/except because _unconfirm_match_flush's staged log entry
    # (below, inside it) can raise: this function also runs outside a
    # request, so it must not depend on request-lifecycle teardown
    # (db.session.remove()) to discard a partial cascade on a bare
    # re-raise.
    try:
        retract_result = _unconfirm_match_flush(
            match_id,
            initiator_id,
            reason=reason,
            # The classification the acknowledgement gate above
            # decided on, reused for the cascade score snapshot
            # instead of walking the bracket a second time. Nothing
            # commits between the two, so a fresh walk could only
            # return the same answer -- and reusing it makes the log
            # and the admin's warning describe the same match set by
            # construction.
            _classification=classification,
        )
    except Exception:
        tournament_repository.rollback_session()
        raise

    if retract_result.is_err():
        tournament_repository.rollback_session()
        return Err(retract_result.unwrap_err())

    (
        retract_events,
        retract_deleted_events,
        tournament_was_uncompleted,
        _tournament_id,
    ) = retract_result.unwrap()

    scores_applied = False
    confirmed_events: list[MatchConfirmedEvent] = []
    completed_event: TournamentCompletedEvent | None = None
    adv_events: list[ContestantAdvancedEvent] = []
    created_events: list[MatchCreatedEvent] = []
    ready_events: list[MatchReadyEvent] = []

    if corrected_scores:
        # Staged before the call so this entry is flushed in the same
        # transaction as the score write it describes. Wrapped for
        # the same reason as _unconfirm_match_flush above: this can
        # raise, and the retraction's already-flushed writes above
        # must not survive a bare re-raise.
        try:
            create_log_entry(
                'match-result-corrected',
                tournament_id,
                initiator_id,
                data={
                    'match_id': str(match_id),
                    'case': case.value,
                    'reason': reason,
                    'scores_applied': True,
                    'previous_scores': previous_scores,
                    'new_scores': {
                        str(key): score
                        for key, score in corrected_scores.items()
                    },
                },
                commit=False,
            )
        except Exception:
            tournament_repository.rollback_session()
            raise

        # Wrapped for the same reason as the two calls above: this
        # can raise (an unknown match, a repository error on the score
        # write), and the retraction's already-flushed cascade must
        # not survive a bare re-raise. Without this the atomicity
        # guarantee this function documents holds for every Err path
        # but not for an exception, outside a request where no
        # teardown rolls the session back.
        try:
            apply_result = _admin_set_and_confirm_match_impl(
                match_id,
                initiator_id,
                corrected_scores,
                # Locked once at the top of this function, for the
                # whole correction; nothing below commits until the
                # single commit_session(), so the locks still stand.
                _locks_held=True,
            )
        except Exception:
            tournament_repository.rollback_session()
            raise

        if apply_result.is_err():
            # Nothing below commits until the single commit_session()
            # near the end of this function, so this rollback undoes
            # the retraction above too: the whole correction never
            # happened, matching every other early-return path here.
            tournament_repository.rollback_session()
            # Returned unwrapped, NOT wrapped in an f-string. Every
            # Err this module returns is itself a msgid the view
            # translates with gettext(); an f-string around one
            # matches no msgid at all, so the flash came out as a
            # German wrapper with an English tail. The "nothing was
            # changed" reassurance that used to live in that wrapper
            # is true of every Err this function returns (see the
            # atomicity contract above), so it now sits in the view's
            # own translatable wrapper instead.
            return Err(apply_result.unwrap_err())
        (
            confirmed_event,
            completed_event,
            adv_events,
            created_events,
            ready_events,
        ) = apply_result.unwrap()
        confirmed_events = [confirmed_event]
        scores_applied = True

    # Single commit for the whole correction.
    tournament_repository.commit_session()

    # Dispatch every collected event only after that commit succeeds.
    for event in retract_events:
        match_unconfirmed.send(None, event=event)
    for event in retract_deleted_events:
        match_deleted.send(None, event=event)
    if tournament_was_uncompleted:
        now = datetime.now(UTC)
        tournament_uncompleted.send(
            None,
            event=TournamentUncompletedEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
            ),
        )
    for event in confirmed_events:
        match_confirmed.send(None, event=event)
    if completed_event is not None:
        tournament_completed.send(None, event=completed_event)
    for event in adv_events:
        contestant_advanced.send(None, event=event)
    for event in created_events:
        match_created.send(None, event=event)
    for event in ready_events:
        match_ready.send(None, event=event)

    return Ok((case, scores_applied))


def set_score(
    match_id: TournamentMatchID,
    contestant_id: TournamentParticipantID | TournamentTeamID,
    score: int,
) -> Result[None, str]:
    """Set the score for a contestant in a match."""
    if score < 0:
        return Err('Score cannot be negative.')

    # Find the contestant entry for this match.
    # Try as participant first, then as team.
    contestant = tournament_repository.find_contestant_for_match(
        match_id,
        participant_id=contestant_id,  # type: ignore[arg-type]
    )
    if contestant is None:
        contestant = tournament_repository.find_contestant_for_match(
            match_id,
            team_id=contestant_id,  # type: ignore[arg-type]
        )
    if contestant is None:
        return Err(
            f'Contestant "{contestant_id}" not found in match "{match_id}"'
        )

    tournament_repository.update_contestant_score(contestant.id, score)

    return Ok(None)


def add_comment(
    match_id: TournamentMatchID,
    created_by_user_id: UserID,
    comment: str,
) -> Result[None, str]:
    """Add a comment to a match."""
    # Validate comment length (max 1000 chars per Task #17)
    if len(comment) > 1000:
        return Err('Comment cannot exceed 1000 characters.')

    now = datetime.now(UTC)
    comment_id = TournamentMatchCommentID(generate_uuid7())

    match_comment = TournamentMatchComment(
        id=comment_id,
        tournament_match_id=match_id,
        created_by=created_by_user_id,
        comment=comment,
        created_at=now,
    )

    tournament_repository.create_match_comment(match_comment)

    return Ok(None)


def update_comment(
    comment_id: TournamentMatchCommentID,
    comment: str,
) -> Result[None, str]:
    """Update a match comment."""
    # Validate comment length (max 1000 chars)
    if len(comment) > 1000:
        return Err('Comment cannot exceed 1000 characters.')

    tournament_repository.update_match_comment(comment_id, comment)

    return Ok(None)


def delete_comment(
    comment_id: TournamentMatchCommentID,
    match_id: TournamentMatchID,
) -> Result[None, str]:
    """Delete a match comment, verifying it belongs to the match."""
    comment = tournament_repository.find_match_comment(comment_id)
    if comment is None:
        return Err('Comment not found.')
    if comment.tournament_match_id != match_id:
        return Err('Comment does not belong to this match.')
    tournament_repository.delete_match_comment(comment_id)
    return Ok(None)


def delete_match(
    match_id: TournamentMatchID,
) -> None:
    """Delete a match and all dependent entities.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.administrate' permission).

    CASCADE HANDLING: Deletes all dependent entities in correct order:
    1. Match comments
    2. Match contestants
    3. Match itself
    """
    from . import signals
    from .events import MatchDeletedEvent

    # Get match to retrieve tournament_id before deletion
    match = tournament_repository.get_match(match_id)

    # Delete in dependency order (children first, then parent)
    tournament_repository.delete_comments_for_match(match_id)
    tournament_repository.delete_contestants_for_match(match_id)
    tournament_repository.delete_match(match_id)

    event = MatchDeletedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=match.tournament_id,
        match_id=match_id,
    )
    signals.match_deleted.send(None, event=event)


def get_comments_from_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchComment]:
    """Return all comments for that match."""
    return tournament_repository.get_comments_for_match(match_id)


def get_contestants_for_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchToContestant]:
    """Return all contestants for that match."""
    return tournament_repository.get_contestants_for_match(match_id)


def get_contestants_for_tournament(
    tournament_id: TournamentID,
) -> dict[TournamentMatchID, list[TournamentMatchToContestant]]:
    """Return all contestants for a tournament, grouped by match ID.

    Single query -- use this in match-list / bracket views to avoid N+1.
    """
    return tournament_repository.get_contestants_for_tournament(tournament_id)


def get_contestants_for_matches(
    match_ids: list[TournamentMatchID],
) -> dict[TournamentMatchID, list[TournamentMatchToContestant]]:
    """Return the contestants of those matches, grouped by match ID.

    Single query, bounded by the passed IDs -- use this where a view
    needs a known handful of matches rather than the whole tournament.
    """
    return tournament_repository.get_contestants_for_matches(match_ids)


# -------------------------------------------------------------------- #
# FFA match service functions
# -------------------------------------------------------------------- #


def generate_ffa_round(
    tournament_id: TournamentID,
    round_number: int | None = None,
    contestant_ids: list[str] | None = None,
    *,
    bracket: Bracket | None = None,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate matches for one FFA round.

    *round_number* is 0-indexed (round 0, 1, 2 ...).
    When *round_number* is ``None`` (the default) it is determined
    automatically after acquiring the tournament lock, avoiding
    TOCTOU races.
    When *contestant_ids* is ``None`` the roster is fetched from the
    tournament's participant/team list (typical for round 0).

    Returns ``Ok(match_count)`` on success.  Commits the session.
    """
    result = _generate_ffa_round_impl(
        tournament_id, round_number, contestant_ids,
        bracket=bracket, initiator_id=initiator_id,
    )
    if result.is_ok():
        tournament_repository.commit_session()
    return result


def _generate_ffa_round_impl(
    tournament_id: TournamentID,
    round_number: int | None = None,
    contestant_ids: list[str] | None = None,
    *,
    bracket: Bracket | None = None,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Internal: generate FFA round matches without committing.

    When *round_number* is ``None`` the next round number is determined
    automatically (after the lock is held).

    Returns ``Ok(match_count)`` on success.
    Caller is responsible for committing the session.
    """
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    # Lock tournament for atomic generation.
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    # Validate game format.
    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        return Err('Tournament game format is not FREE_FOR_ALL.')

    # Auto-determine round number under the lock to prevent TOCTOU races.
    if round_number is None:
        all_matches = tournament_repository.get_matches_for_tournament_ordered(
            tournament_id
        )
        bracket_matches = (
            [m for m in all_matches if m.bracket == bracket]
            if bracket is not None
            else all_matches
        )
        rounds_with_values = [
            m.round for m in bracket_matches if m.round is not None
        ]
        round_number = (max(rounds_with_values) + 1) if rounds_with_values else 0

    is_team = tournament.contestant_type == ContestantType.TEAM

    # Reject team tournaments where group cannot be formed.
    if is_team:
        max_teams = tournament.max_teams or 0
        group_min = tournament.group_size_min or 2
        if max_teams < group_min:
            return Err(
                f'Team tournament has max_teams={max_teams} which is '
                f'less than group_size_min={group_min}. '
                'Cannot form valid FFA groups.'
            )

    # Fetch contestant IDs when not supplied.
    if contestant_ids is None:
        if is_team:
            teams = tournament_repository.get_teams_for_tournament(
                tournament_id
            )
            contestant_ids = [str(team.id) for team in teams]
        else:
            participants = (
                tournament_repository.get_participants_for_tournament(
                    tournament_id
                )
            )
            contestant_ids = [str(p.id) for p in participants]

    if len(contestant_ids) < 2:
        return Err('Need at least 2 contestants for FFA round.')

    # Distribute into groups via snake seeding.
    group_size_min = tournament.group_size_min or 2
    group_size_max = tournament.group_size_max or len(contestant_ids)
    groups_result = snake_seed_groups(
        contestant_ids, group_size_min, group_size_max,
    )
    if groups_result.is_err():
        return Err(groups_result.unwrap_err())
    groups = groups_result.unwrap()

    now = datetime.now(UTC)
    match_count = 0

    for group_idx, group in enumerate(groups):
        match_id = TournamentMatchID(generate_uuid7())
        match = TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=group_idx,
            match_order=group_idx,
            round=round_number,
            next_match_id=None,
            confirmed_by=None,
            created_at=now,
            bracket=bracket,
        )
        tournament_repository.create_match(match)
        match_count += 1

        # Create contestant entries for each group member.
        for cid in group:
            contestant_rec_id = TournamentMatchToContestantID(
                generate_uuid7()
            )
            if is_team:
                contestant = TournamentMatchToContestant(
                    id=contestant_rec_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(cid)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                contestant = TournamentMatchToContestant(
                    id=contestant_rec_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(UUID(cid)),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant)

    return Ok(match_count)


def set_ffa_placements(
    match_id: TournamentMatchID,
    placements: dict[str, int],
) -> Result[None, str]:
    """Set placements (and derived points) for all contestants in an
    FFA match.

    *placements* maps contestant ID (as string) to a 1-based
    placement integer.  Placements must be sequential (1..N) with
    no gaps and must cover every contestant in the match.

    Returns ``Ok(None)`` on success.
    """
    match = tournament_repository.find_match(match_id)
    if match is not None:
        tournament_repository.lock_tournament_for_update(match.tournament_id)
    match = tournament_repository.get_match_for_update(match_id)

    # A confirmed result changes only through the audited unconfirm.
    if match.confirmed_by is not None:
        return Err('Cannot modify placements of a confirmed match.')

    # The mirror of PLACEMENT_FORMAT_CONFIRM_ERROR: a bracket match
    # has no placements, and writing some is the first half of a way
    # to stall the bracket -- confirm_ffa_match below accepts any
    # match whose contestants all carry one, and deliberately does
    # NOT advance a winner, so a bracket match confirmed through it
    # never feeds its next_match_id and can no longer be confirmed
    # properly. Both site routes already refuse this; the admin ones
    # did not, so enforce it here for both.
    tournament = tournament_repository.get_tournament(match.tournament_id)
    if not _decided_by_placements(tournament):
        return Err('Placements apply only to free-for-all matches.')

    contestants = tournament_repository.get_contestants_for_match(match_id)

    # Build lookup: contestant-id-string -> contestant record.
    cid_to_contestant: dict[str, TournamentMatchToContestant] = {}
    for c in contestants:
        cid = contestant_id(c)
        cid_to_contestant[cid] = c

    # Validate completeness.
    if len(placements) != len(contestants):
        return Err(
            f'Expected placements for {len(contestants)} contestants, '
            f'got {len(placements)}.'
        )

    # Validate all contestant IDs are known.
    for cid in placements:
        if cid not in cid_to_contestant:
            return Err(f'Unknown contestant ID: {cid}')

    # Validate sequential 1..N.
    expected = set(range(1, len(contestants) + 1))
    actual = set(placements.values())
    if actual != expected:
        return Err(
            f'Placements must be sequential 1..{len(contestants)}. '
            f'Got: {sorted(actual)}'
        )

    # Map placements to points. The tournament is already loaded by
    # the format guard above.
    point_table = tournament.point_table or []

    updates: dict[TournamentMatchToContestantID, tuple[int, int]] = {}
    for cid, placement in placements.items():
        c = cid_to_contestant[cid]
        points = map_placement_to_points(placement, point_table)
        updates[c.id] = (placement, points)

    tournament_repository.update_contestant_placement_and_points(updates)
    tournament_repository.commit_session()
    return Ok(None)


def confirm_ffa_match(
    match_id: TournamentMatchID,
    initiator_id: UserID,
) -> Result[None, str]:
    """Confirm an FFA match after placements are set.

    Validates that all contestants have placements assigned.
    Does NOT trigger bracket advancement (FFA does not use
    ``next_match_id``).

    Returns ``Ok(None)`` on success.
    """
    match = tournament_repository.find_match(match_id)
    if match is not None:
        tournament_repository.lock_tournament_for_update(match.tournament_id)
    match = tournament_repository.get_match_for_update(match_id)

    # Reject already-confirmed matches. Checked before the format
    # guard below so a confirmed match keeps reporting the more
    # specific of the two reasons it is refused.
    if match.confirmed_by is not None:
        return Err('Match is already confirmed.')

    # Same guard as set_ffa_placements above, and the reason this one
    # matters most: this function confirms WITHOUT advancing a winner,
    # which is correct for FFA and ruinous for a bracket match -- it
    # would be marked confirmed, never feed its next_match_id, and be
    # refused by the normal confirm path from then on.
    tournament = tournament_repository.get_tournament(match.tournament_id)
    if not _decided_by_placements(tournament):
        return Err('Placements apply only to free-for-all matches.')

    contestants = tournament_repository.get_contestants_for_match(match_id)

    # Validate all placements are set.
    missing = [c for c in contestants if c.placement is None]
    if missing:
        return Err(
            f'Not all placements are set. '
            f'{len(missing)} contestant(s) lack placements.'
        )

    tournament_repository.confirm_match(match_id, initiator_id)

    tournament_was_completed = False
    winner = None

    # Check for FFA+SE auto-complete.
    if (
        tournament.elimination_mode == EliminationMode.SINGLE_ELIMINATION
        and tournament.game_format == GameFormat.FREE_FOR_ALL
    ):
        round_matches = tournament_repository.get_matches_for_round(
            tournament.id, match.round, bracket=None,
        )
        # Auto-complete when exactly 1 group in the round (final round).
        if len(round_matches) == 1:
            first_place = [
                c for c in contestants if c.placement == 1
            ]
            if first_place:
                winner = first_place[0]
                comp = _try_auto_complete_tournament(match, tournament, winner)
                if comp.is_err():
                    return comp
                tournament_was_completed = comp.unwrap()

    # Check for FFA+DE Grand Final completion.
    if (
        tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION
        and tournament.game_format == GameFormat.FREE_FOR_ALL
        and match.bracket == Bracket.GRAND_FINAL
    ):
        # Check if all GF matches are confirmed.
        gf_matches = tournament_repository.get_matches_for_round(
            tournament.id, match.round, bracket=Bracket.GRAND_FINAL,
        )
        all_gf_confirmed = all(
            m.confirmed_by is not None for m in gf_matches
        )
        if all_gf_confirmed:
            first_place = [
                c for c in contestants if c.placement == 1
            ]
            if first_place:
                winner = first_place[0]
                comp = _try_auto_complete_tournament(match, tournament, winner)
                if comp.is_err():
                    return comp
                tournament_was_completed = comp.unwrap()

    create_log_entry(
        'ffa-match-confirmed',
        match.tournament_id,
        initiator_id,
        data={
            'match_id': str(match_id),
            'placements': _snapshot_contestant_placements(contestants),
        },
        commit=False,
    )

    tournament_repository.commit_session()

    # Resolve winner for signal dispatch if not already set from
    # auto-complete paths above.
    if winner is None:
        first_place = [c for c in contestants if c.placement == 1]
        if first_place:
            winner = first_place[0]

    if winner is not None:
        now = datetime.now(UTC)
        tid = match.tournament_id
        match_confirmed.send(None, event=MatchConfirmedEvent(
            occurred_at=now, initiator=None,
            tournament_id=tid, match_id=match_id,
            winner_team_id=winner.team_id,
            winner_participant_id=winner.participant_id,
        ))
        if tournament_was_completed:
            tournament_completed.send(None, event=TournamentCompletedEvent(
                occurred_at=now, initiator=None,
                tournament_id=tid,
                winner_team_id=winner.team_id,
                winner_participant_id=winner.participant_id,
            ))

    return Ok(None)


def advance_ffa_round(
    tournament_id: TournamentID,
    *,
    pool: Bracket | None = None,
    initiator_id: UserID | None = None,
) -> Result[int | str, str]:
    """Advance an FFA tournament to the next round.

    For single-track (``pool=None``): selects top
    ``advancement_count`` from each group, bottom eliminated.

    For double elimination (``pool=Bracket.WINNERS`` or
    ``pool=Bracket.LOSERS``): routes players between WB/LB pools.

    Returns ``Ok(new_match_count)`` on success,
    ``Ok('advanced_wb')``, ``Ok('advanced_lb')``, or
    ``Ok('grand_final_eligible')`` for DE pools,
    or ``Err(reason)`` on failure.
    """
    # Lock tournament.
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        return Err('Tournament game format is not FREE_FOR_ALL.')

    advancement_count = tournament.advancement_count
    if advancement_count is None or advancement_count < 1:
        return Err('Tournament advancement_count is not configured.')

    is_de = tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION

    # DE requires an explicit pool parameter.
    if is_de and pool is None:
        return Err(
            'Double elimination requires pool parameter '
            '(Bracket.WINNERS or Bracket.LOSERS).'
        )

    # Single-track must not specify a pool.
    if not is_de and pool is not None:
        return Err('Single-track FFA does not use pool parameter.')

    if is_de:
        return _advance_ffa_round_de(
            tournament, pool, advancement_count, initiator_id,
        )
    else:
        return _advance_ffa_round_single(
            tournament, advancement_count, initiator_id,
        )


def _advance_ffa_round_single(
    tournament: Tournament,
    advancement_count: int,
    initiator_id: UserID | None,
) -> Result[int | str, str]:
    """Single-track FFA advancement: bottom eliminated, top advance."""
    tournament_id = tournament.id

    # Find the latest round with matches.
    all_matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament_id
    )
    if not all_matches:
        return Err('Tournament has no matches.')

    latest_round = max(m.round for m in all_matches if m.round is not None)

    round_matches = tournament_repository.get_matches_for_round(
        tournament_id, latest_round,
    )

    # Validate all matches in the round are confirmed.
    unconfirmed = [m for m in round_matches if m.confirmed_by is None]
    if unconfirmed:
        return Err(
            f'{len(unconfirmed)} match(es) in round {latest_round} '
            f'are not confirmed.'
        )

    advancing_ids, err = _select_top_n_from_round(
        round_matches, advancement_count,
    )
    if err is not None:
        return Err(err)

    if not advancing_ids:
        return Err('No contestants qualified for advancement.')

    # Generate the next round with advancing contestants.
    # When survivors fit in a single group this becomes the final round
    # (single group = winner-takes-all).  No special signal needed —
    # auto-complete fires when the single-group final is confirmed.
    next_round = latest_round + 1
    gen_result = _generate_ffa_round_impl(
        tournament_id,
        next_round,
        advancing_ids,
        initiator_id=initiator_id,
    )
    if gen_result.is_err():
        return Err(gen_result.unwrap_err())

    tournament_repository.commit_session()
    return Ok(gen_result.unwrap())


def _advance_ffa_round_de(
    tournament: Tournament,
    pool: Bracket | None,
    advancement_count: int,
    initiator_id: UserID | None,
) -> Result[int | str, str]:
    """Double elimination FFA advancement with WB/LB pool routing."""
    tournament_id = tournament.id

    all_matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament_id
    )
    if not all_matches:
        return Err('Tournament has no matches.')

    if pool == Bracket.WINNERS:
        return _advance_ffa_wb(
            tournament, all_matches, advancement_count, initiator_id,
        )
    elif pool == Bracket.LOSERS:
        return _advance_ffa_lb(
            tournament, all_matches, advancement_count, initiator_id,
        )
    else:
        return Err(f'Invalid pool for DE advancement: {pool}')


def _advance_ffa_wb(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    advancement_count: int,
    initiator_id: UserID | None,
) -> Result[int | str, str]:
    """Winners bracket advancement: top stay in WB, bottom drop to LB."""
    tournament_id = tournament.id

    # Find latest WB round.
    wb_matches = [
        m for m in all_matches if m.bracket == Bracket.WINNERS
    ]
    if not wb_matches:
        return Err('No Winners bracket matches found.')

    latest_wb_round = max(
        m.round for m in wb_matches if m.round is not None
    )

    wb_round_matches = tournament_repository.get_matches_for_round(
        tournament_id, latest_wb_round, bracket=Bracket.WINNERS,
    )

    # Validate all WB round matches confirmed.
    unconfirmed = [m for m in wb_round_matches if m.confirmed_by is None]
    if unconfirmed:
        return Err(
            f'{len(unconfirmed)} WB match(es) in round {latest_wb_round} '
            f'are not confirmed.'
        )

    # Select top N from each WB group; remainder drops to LB.
    wb_advancing: list[str] = []
    wb_dropped: list[str] = []

    for match in wb_round_matches:
        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        sorted_contestants = sorted(
            contestants,
            key=lambda c: c.points if c.points is not None else 0,
            reverse=True,
        )

        if len(sorted_contestants) <= advancement_count:
            for c in sorted_contestants:
                wb_advancing.append(contestant_id(c))
            continue

        # Tie check at cutoff.
        cutoff_points = sorted_contestants[advancement_count - 1].points or 0
        next_points = sorted_contestants[advancement_count].points or 0
        if cutoff_points == next_points:
            tied = [
                contestant_id(c)
                for c in sorted_contestants
                if (c.points or 0) == cutoff_points
            ]
            return Err(
                f'Tie at WB advancement cutoff in match {match.id}. '
                f'Tied contestants: {", ".join(tied)}'
            )

        for c in sorted_contestants[:advancement_count]:
            wb_advancing.append(contestant_id(c))
        for c in sorted_contestants[advancement_count:]:
            wb_dropped.append(contestant_id(c))

    if not wb_advancing:
        return Err('No WB contestants qualified for advancement.')

    # Collect existing LB survivors (top N from latest LB round).
    lb_survivors = _collect_lb_survivors(tournament, all_matches)

    # Check GF trigger: total survivors <= group_size_max.
    total_survivors = len(wb_advancing) + len(lb_survivors) + len(wb_dropped)
    if _check_grand_final_trigger(tournament, total_survivors):
        # Signal GF eligibility — admin decides whether to generate
        # Grand Final or run another round.  Do NOT generate new
        # WB/LB rounds; the admin will call generate_ffa_grand_final()
        # or run advance again after choosing.
        return Ok('grand_final_eligible')

    # Generate next WB round for WB survivors.
    # Use _impl (no commit) so WB + LB rounds are created atomically.
    next_wb_round = latest_wb_round + 1
    gen_wb = _generate_ffa_round_impl(
        tournament_id,
        next_wb_round,
        wb_advancing,
        bracket=Bracket.WINNERS,
        initiator_id=initiator_id,
    )
    if gen_wb.is_err():
        return Err(gen_wb.unwrap_err())

    # Merge dropped players with existing LB survivors for next LB round.
    lb_pool = wb_dropped + lb_survivors
    if lb_pool:
        lb_round_num = _next_lb_round_number(all_matches)
        gen_lb = _generate_ffa_round_impl(
            tournament_id,
            lb_round_num,
            lb_pool,
            bracket=Bracket.LOSERS,
            initiator_id=initiator_id,
        )
        if gen_lb.is_err():
            return Err(gen_lb.unwrap_err())

    tournament_repository.commit_session()
    return Ok('advanced_wb')


def _advance_ffa_lb(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    advancement_count: int,
    initiator_id: UserID | None,
) -> Result[int | str, str]:
    """Losers bracket advancement: top survive, bottom eliminated."""
    tournament_id = tournament.id

    lb_matches = [
        m for m in all_matches if m.bracket == Bracket.LOSERS
    ]
    if not lb_matches:
        return Err('No Losers bracket matches found.')

    latest_lb_round = max(
        m.round for m in lb_matches if m.round is not None
    )

    lb_round_matches = tournament_repository.get_matches_for_round(
        tournament_id, latest_lb_round, bracket=Bracket.LOSERS,
    )

    # Validate all LB round matches confirmed.
    unconfirmed = [m for m in lb_round_matches if m.confirmed_by is None]
    if unconfirmed:
        return Err(
            f'{len(unconfirmed)} LB match(es) in round {latest_lb_round} '
            f'are not confirmed.'
        )

    # Select top N from each LB group; bottom eliminated entirely.
    lb_advancing: list[str] = []

    for match in lb_round_matches:
        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        sorted_contestants = sorted(
            contestants,
            key=lambda c: c.points if c.points is not None else 0,
            reverse=True,
        )

        if len(sorted_contestants) <= advancement_count:
            for c in sorted_contestants:
                lb_advancing.append(contestant_id(c))
            continue

        # Tie check at cutoff.
        cutoff_points = sorted_contestants[advancement_count - 1].points or 0
        next_points = sorted_contestants[advancement_count].points or 0
        if cutoff_points == next_points:
            tied = [
                contestant_id(c)
                for c in sorted_contestants
                if (c.points or 0) == cutoff_points
            ]
            return Err(
                f'Tie at LB advancement cutoff in match {match.id}. '
                f'Tied contestants: {", ".join(tied)}'
            )

        for c in sorted_contestants[:advancement_count]:
            lb_advancing.append(contestant_id(c))

    if not lb_advancing:
        return Err('No LB contestants qualified for advancement.')

    # Collect WB survivors for GF trigger check.
    wb_survivors = _collect_wb_survivors(tournament, all_matches)

    # Check GF trigger.
    total_survivors = len(wb_survivors) + len(lb_advancing)
    if _check_grand_final_trigger(tournament, total_survivors):
        return Ok('grand_final_eligible')

    # Generate next LB round.
    next_lb_round = latest_lb_round + 1
    gen_result = _generate_ffa_round_impl(
        tournament_id,
        next_lb_round,
        lb_advancing,
        bracket=Bracket.LOSERS,
        initiator_id=initiator_id,
    )
    if gen_result.is_err():
        return Err(gen_result.unwrap_err())

    tournament_repository.commit_session()
    return Ok('advanced_lb')


def _select_top_n_from_round(
    round_matches: list[TournamentMatch],
    advancement_count: int,
) -> tuple[list[str], str | None]:
    """Select top N contestants from each match in a round.

    Returns ``(advancing_ids, None)`` on success or
    ``([], error_message)`` on failure (tie at cutoff).
    """
    advancing_ids: list[str] = []

    for match in round_matches:
        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        sorted_contestants = sorted(
            contestants,
            key=lambda c: c.points if c.points is not None else 0,
            reverse=True,
        )

        if len(sorted_contestants) <= advancement_count:
            for c in sorted_contestants:
                advancing_ids.append(contestant_id(c))
            continue

        cutoff_points = sorted_contestants[advancement_count - 1].points or 0
        next_points = sorted_contestants[advancement_count].points or 0
        if cutoff_points == next_points:
            tied = [
                contestant_id(c)
                for c in sorted_contestants
                if (c.points or 0) == cutoff_points
            ]
            return [], (
                f'Tie at advancement cutoff in match {match.id}. '
                f'Tied contestants: {", ".join(tied)}'
            )

        for c in sorted_contestants[:advancement_count]:
            advancing_ids.append(contestant_id(c))

    return advancing_ids, None


def _check_grand_final_trigger(
    tournament: Tournament,
    total_survivors: int,
) -> bool:
    """Return True when total survivors fit within group_size_max."""
    group_size_max = tournament.group_size_max or total_survivors
    return total_survivors <= group_size_max


def _collect_lb_survivors(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
) -> list[str]:
    """Collect surviving contestant IDs from the latest LB round.

    Survivors = top ``advancement_count`` from each LB group.
    Returns empty list when no LB rounds exist yet.
    """
    lb_matches = [
        m for m in all_matches if m.bracket == Bracket.LOSERS
    ]
    if not lb_matches:
        return []

    latest_lb_round = max(
        m.round for m in lb_matches if m.round is not None
    )
    lb_round_matches = tournament_repository.get_matches_for_round(
        tournament.id, latest_lb_round, bracket=Bracket.LOSERS,
    )

    advancement_count = tournament.advancement_count or 1
    survivors: list[str] = []

    for match in lb_round_matches:
        # Only consider confirmed matches for survivor collection.
        if match.confirmed_by is None:
            # Unconfirmed LB match — all contestants are still "alive".
            contestants = tournament_repository.get_contestants_for_match(
                match.id
            )
            for c in contestants:
                survivors.append(contestant_id(c))
            continue

        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        sorted_c = sorted(
            contestants,
            key=lambda c: c.points if c.points is not None else 0,
            reverse=True,
        )
        for c in sorted_c[:advancement_count]:
            survivors.append(contestant_id(c))

    return survivors


def _collect_wb_survivors(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
) -> list[str]:
    """Collect surviving contestant IDs from the latest WB round."""
    wb_matches = [
        m for m in all_matches if m.bracket == Bracket.WINNERS
    ]
    if not wb_matches:
        return []

    latest_wb_round = max(
        m.round for m in wb_matches if m.round is not None
    )
    wb_round_matches = tournament_repository.get_matches_for_round(
        tournament.id, latest_wb_round, bracket=Bracket.WINNERS,
    )

    advancement_count = tournament.advancement_count or 1
    survivors: list[str] = []

    for match in wb_round_matches:
        if match.confirmed_by is None:
            contestants = tournament_repository.get_contestants_for_match(
                match.id
            )
            for c in contestants:
                survivors.append(contestant_id(c))
            continue

        contestants = tournament_repository.get_contestants_for_match(
            match.id
        )
        sorted_c = sorted(
            contestants,
            key=lambda c: c.points if c.points is not None else 0,
            reverse=True,
        )
        for c in sorted_c[:advancement_count]:
            survivors.append(contestant_id(c))

    return survivors


def _next_lb_round_number(
    all_matches: list[TournamentMatch],
) -> int:
    """Determine the next LB round number (max existing LB round + 1,
    or 0 if no LB rounds exist)."""
    lb_matches = [
        m for m in all_matches if m.bracket == Bracket.LOSERS
    ]
    if not lb_matches:
        return 0
    latest = max(m.round for m in lb_matches if m.round is not None)
    return latest + 1


def generate_ffa_grand_final(
    tournament_id: TournamentID,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate the Grand Final round for an FFA-DE tournament.

    Merges all WB + LB survivors into a single GF group.
    GF is exempt from ``group_size_min``.

    Returns ``Ok(match_count)`` on success (always 1).
    """
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    # Lock tournament for atomic generation.
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    if tournament.game_format != GameFormat.FREE_FOR_ALL:
        return Err('Tournament game format is not FREE_FOR_ALL.')

    if tournament.elimination_mode != EliminationMode.DOUBLE_ELIMINATION:
        return Err('Grand Final is only for double elimination tournaments.')

    is_team = tournament.contestant_type == ContestantType.TEAM

    all_matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament_id
    )

    # Reject if Grand Final already exists.
    existing_gf = [
        m for m in all_matches if m.bracket == Bracket.GRAND_FINAL
    ]
    if existing_gf:
        return Err('Grand Final has already been generated.')

    # Collect all survivors from both pools.
    wb_survivors = _collect_wb_survivors(tournament, all_matches)
    lb_survivors = _collect_lb_survivors(tournament, all_matches)

    all_survivors = wb_survivors + lb_survivors

    if len(all_survivors) < 2:
        return Err('Need at least 2 survivors for Grand Final.')

    # Build per-bracket round groupings for standings computation.
    wb_round_matches: list[list[list[TournamentMatchToContestant]]] = []
    lb_round_matches: list[list[list[TournamentMatchToContestant]]] = []
    all_round_matches: list[list[list[TournamentMatchToContestant]]] = []

    rounds_seen: dict[tuple[int | None, str | None], list[TournamentMatch]] = {}
    for m in all_matches:
        key = (m.round, m.bracket.value if m.bracket else None)
        rounds_seen.setdefault(key, []).append(m)

    for _key, round_match_list in sorted(
        rounds_seen.items(), key=lambda x: (x[0][0] or 0, x[0][1] or ''),
    ):
        round_groups: list[list[TournamentMatchToContestant]] = []
        for rm in round_match_list:
            contestants = tournament_repository.get_contestants_for_match(
                rm.id
            )
            round_groups.append(contestants)
        all_round_matches.append(round_groups)

        # Partition into WB / LB buckets.
        bracket_val = _key[1]
        if bracket_val == Bracket.WINNERS.value:
            wb_round_matches.append(round_groups)
        elif bracket_val == Bracket.LOSERS.value:
            lb_round_matches.append(round_groups)

    # Seed GF participants respecting points_carry_to_losers flag.
    wb_survivor_set = set(wb_survivors)
    lb_survivor_set = set(lb_survivors)
    survivor_set = set(all_survivors)

    if tournament.points_carry_to_losers:
        # Full cross-bracket cumulative — all points count for everyone.
        cumulative = compute_ffa_cumulative_standings(all_round_matches)
        ordered_survivors = [
            cid for cid, _pts in cumulative if cid in survivor_set
        ]
    else:
        # Separate cumulative: WB survivors ranked by WB points,
        # LB survivors ranked by LB-only points.  WB survivors
        # rank first (they never lost).
        wb_cumulative = compute_ffa_cumulative_standings(wb_round_matches)
        lb_cumulative = compute_ffa_cumulative_standings(lb_round_matches)
        ordered_survivors = [
            cid for cid, _pts in wb_cumulative if cid in wb_survivor_set
        ] + [
            cid for cid, _pts in lb_cumulative if cid in lb_survivor_set
        ]

    # Add any survivors not in cumulative (safety fallback).
    for cid in all_survivors:
        if cid not in ordered_survivors:
            ordered_survivors.append(cid)

    # Create the GF match — single group, exempt from group_size_min.
    now = datetime.now(UTC)
    match_id = TournamentMatchID(generate_uuid7())
    gf_match = TournamentMatch(
        id=match_id,
        tournament_id=tournament_id,
        group_order=0,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=None,
        created_at=now,
        bracket=Bracket.GRAND_FINAL,
    )
    tournament_repository.create_match(gf_match)

    for cid in ordered_survivors:
        contestant_rec_id = TournamentMatchToContestantID(generate_uuid7())
        if is_team:
            contestant = TournamentMatchToContestant(
                id=contestant_rec_id,
                tournament_match_id=match_id,
                team_id=TournamentTeamID(UUID(cid)),
                participant_id=None,
                score=None,
                created_at=now,
            )
        else:
            contestant = TournamentMatchToContestant(
                id=contestant_rec_id,
                tournament_match_id=match_id,
                team_id=None,
                participant_id=TournamentParticipantID(UUID(cid)),
                score=None,
                created_at=now,
            )
        tournament_repository.create_match_contestant(contestant)

    tournament_repository.commit_session()
    return Ok(1)
