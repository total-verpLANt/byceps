import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import NamedTuple

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

logger = logging.getLogger(__name__)


MAX_MATCH_SCORE = 999_999_999

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


def clear_bracket(tournament_id: TournamentID) -> Result[None, str]:
    """Clear all matches from a tournament bracket."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)

    for match in matches:
        delete_match(match.id)

    tournament_repository.commit_session()

    return Ok(None)


def _prepare_bracket_generation(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
) -> Result[tuple[Tournament, list[str], bool], str]:
    """Shared preamble for bracket generation functions.

    Locks the tournament, validates contestant type, fetches
    contestant IDs, checks minimum count, and optionally clears
    existing matches when *force_regenerate* is set.

    Returns ``Ok((tournament, contestant_ids, had_matches))``
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

    # Clear existing matches if force regenerate.
    if force_regenerate and had_matches:
        clear_result = clear_bracket(tournament_id)
        if clear_result.is_err():
            return Err(
                f'Failed to clear existing bracket: {clear_result.unwrap_err()}'
            )

    # Get tournament to check contestant type.
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        return Err('Tournament contestant type is not set.')

    # Get contestants (participants or teams).
    if tournament.contestant_type == ContestantType.TEAM:
        teams = tournament_repository.get_teams_for_tournament(tournament_id)
        contestant_ids = [str(team.id) for team in teams]
    else:
        participants = tournament_repository.get_participants_for_tournament(
            tournament_id
        )
        contestant_ids = [str(p.id) for p in participants]

    if len(contestant_ids) < 2:
        return Err('Need at least 2 contestants for bracket.')

    return Ok((tournament, contestant_ids, had_matches))


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
    prep_result = _prepare_bracket_generation(tournament_id, force_regenerate)
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, _had_matches = prep_result.unwrap()
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
    prep_result = _prepare_bracket_generation(tournament_id, force_regenerate)
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, _had_matches = prep_result.unwrap()
    num_contestants = len(contestant_ids)

    # Double-elimination requires at least 4 contestants.
    if num_contestants < 4:
        return Err(
            'Need at least 4 contestants for double-elimination bracket.'
        )

    # Validate elimination mode.
    if tournament.elimination_mode != EliminationMode.DOUBLE_ELIMINATION:
        return Err('Tournament elimination mode must be DOUBLE_ELIMINATION.')

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


def generate_round_robin_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
) -> Result[int, str]:
    """Generate round-robin bracket with all pairings."""
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
    from .models.contestant_type import ContestantType

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(tournament_id, force_regenerate)
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, _had_matches = prep_result.unwrap()

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
    for event in match_events:
        signals.match_created.send(None, event=event)

    rr_match_ids = {e.match_id for e in match_events}
    ready_events = _collect_ready_match_events(rr_match_ids, tournament_id, now)
    for event in ready_events:
        match_ready.send(None, event=event)

    return Ok(total_matches)


def reset_match(match_id: TournamentMatchID) -> None:
    """Reset a match (with cascading delete of dependents)."""
    delete_match(match_id)


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

    Post-rollback note: when this function returns ``Err``, the
    database session has been rolled back.  Any ORM-managed objects
    fetched before this call (match, contestants, etc.) may be
    expired or detached.  Callers must NOT access attributes on
    those objects after receiving an ``Err`` result — fetch fresh
    instances if needed.
    """
    match = tournament_repository.get_match_for_update(match_id)
    if match.confirmed_by is not None:
        return Err('Cannot modify scores of a confirmed match.')

    contestants = tournament_repository.get_contestants_for_match(
        match_id
    )

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

    # Validate: every real contestant must have an entry in the scores dict.
    if len(scores) != len(real_contestants):
        return Err(
            'All contestants in the match must have scores submitted.'
        )

    # Resolve each submitted key to a contestant and validate scores.
    id_to_score: dict[TournamentMatchToContestantID, int] = {}
    proposed_contestants: list[TournamentMatchToContestant] = []
    for contestant in real_contestants:
        # Match by participant_id or team_id.
        key = contestant.team_id or contestant.participant_id
        if key not in scores:
            return Err(
                f'Missing score for contestant "{key}".'
            )
        score = scores[key]
        if score < 0:
            return Err('Score cannot be negative.')
        if score > MAX_MATCH_SCORE:
            return Err(f'Score cannot exceed {MAX_MATCH_SCORE:,}.')
        id_to_score[contestant.id] = score
        proposed_contestants.append(replace(contestant, score=score))

    # Determine proposed winner to enforce loser-only submission.
    winner_result = determine_match_winner(proposed_contestants)
    if winner_result.is_err():
        return Err(winner_result.unwrap_err())
    winner = winner_result.unwrap()

    if winner is not None and winner.id == initiator_contestant.id:
        return Err('Only the losing side may submit scores.')

    # Atomic write — all scores flushed together.
    tournament_repository.update_contestant_scores(id_to_score)
    # Auto-confirm: loser submitted scores → match is resolved.
    # confirm_match() will see the flushed scores and commit everything.
    confirm_result = confirm_match(match_id, initiator_id)
    if confirm_result.is_err():
        # Scores were flushed but confirm failed (e.g. draw in SE mode).
        # Roll back so we don't persist partial state (scores without
        # a confirmed match).  The caller can retry with corrected scores.
        tournament_repository.rollback_session()
        return Err(confirm_result.unwrap_err())
    return Ok(None)


def admin_set_and_confirm_match(
    match_id: TournamentMatchID,
    admin_id: UserID,
    scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> Result[None, str]:
    """Set all contestant scores and confirm a match atomically.

    Admin-only variant: no loser-only enforcement, no participant
    check.  The admin supplies scores for every real contestant and
    the match is confirmed in one operation.

    Post-rollback note: when this function returns ``Err``, the
    database session has been rolled back.  Any ORM-managed objects
    fetched before this call may be expired or detached.  Callers
    must NOT access attributes on those objects after receiving an
    ``Err`` result.
    """
    match = tournament_repository.get_match_for_update(match_id)
    if match.confirmed_by is not None:
        return Err('Match is already confirmed.')

    contestants = tournament_repository.get_contestants_for_match(
        match_id
    )

    # Exclude DEFWIN slots (no participant or team assigned).
    real_contestants = [
        c for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]

    if len(scores) != len(real_contestants):
        return Err(
            'All contestants in the match must have scores submitted.'
        )

    # Resolve each submitted key to a contestant and validate scores.
    id_to_score: dict[TournamentMatchToContestantID, int] = {}
    for contestant in real_contestants:
        key = contestant.team_id or contestant.participant_id
        if key not in scores:
            return Err(f'Missing score for contestant "{key}".')
        score = scores[key]
        if score < 0:
            return Err('Score cannot be negative.')
        if score > MAX_MATCH_SCORE:
            return Err(f'Score cannot exceed {MAX_MATCH_SCORE:,}.')
        id_to_score[contestant.id] = score

    # Atomic write — all scores flushed together.
    tournament_repository.update_contestant_scores(id_to_score)

    # Auto-confirm: admin submitted scores → match is resolved.
    # confirm_match() will see the flushed scores and commit everything.
    confirm_result = confirm_match(match_id, admin_id)
    if confirm_result.is_err():
        # Scores were flushed but confirm failed (e.g. draw in SE mode).
        # Roll back so we don't persist partial state.
        tournament_repository.rollback_session()
        return Err(confirm_result.unwrap_err())
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


def _confirm_draw(
    match: TournamentMatch,
    match_id: TournamentMatchID,
    initiator_id: UserID,
    tournament: Tournament,
) -> Result[None, str]:
    """Confirm a drawn match (round-robin only).

    Self-contained: owns its own commit + event dispatch.
    """
    if tournament.elimination_mode != EliminationMode.ROUND_ROBIN:
        return Err(
            'Match is a draw; a winner is required '
            'in this tournament mode.'
        )

    tournament_repository.confirm_match(
        match_id, initiator_id,
    )

    tournament_repository.commit_session()

    now = datetime.now(UTC)
    match_confirmed.send(
        None,
        event=MatchConfirmedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match_id,
            winner_team_id=None,
            winner_participant_id=None,
        ),
    )
    return Ok(None)


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


def _try_auto_complete_tournament(
    match: TournamentMatch,
    tournament: Tournament,
    winner: TournamentMatchToContestant,
) -> Result[bool, str]:
    """Complete the tournament if this is a terminal elimination
    match (flush only).

    Returns ``Ok(True)`` when the tournament was completed,
    ``Ok(False)`` when the condition does not apply.
    Caller owns commit and event dispatch.
    """
    # Gate: only SE/DE modes can auto-complete (regardless of game format)
    if tournament.elimination_mode not in (
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ):
        return Ok(False)

    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        # FFA+DE: auto-complete only when Grand Final match is confirmed
        if tournament.elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
            if match.bracket != Bracket.GRAND_FINAL:
                return Ok(False)
        # FFA+SE: auto-complete when the confirmed round has exactly 1 group
        # (all remaining players were in a single group — this was the final round)
        else:
            round_matches = tournament_repository.get_matches_for_round(
                tournament.id, match.round, bracket=None,
            )
            if len(round_matches) != 1:
                return Ok(False)
    # 1v1: existing logic — terminal match (no next_match_id)
    else:
        if match.next_match_id is not None:
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


def confirm_match(
    match_id: TournamentMatchID,
    initiator_id: UserID,
) -> Result[None, str]:
    """Confirm a match result."""
    match = tournament_repository.get_match_for_update(match_id)
    contestants = (
        tournament_repository.get_contestants_for_match(match_id)
    )
    validation = _validate_match_confirmable(match, contestants)
    if validation.is_err():
        return validation
    winner_result = determine_match_winner(contestants)
    if winner_result.is_err():
        return Err(winner_result.unwrap_err())
    winner = winner_result.unwrap()
    tournament = tournament_repository.get_tournament(
        match.tournament_id,
    )
    if winner is None:
        return _confirm_draw(
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
    tournament_repository.commit_session()
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
    for event in adv_events:
        contestant_advanced.send(None, event=event)
    for event in bracket_reset_events:
        match_created.send(None, event=event)
    destination_match_ids: set[TournamentMatchID] = set()
    for event in adv_events:
        destination_match_ids.add(event.match_id)
    for event in bracket_reset_events:
        destination_match_ids.add(event.match_id)
    if destination_match_ids:
        ready_events = _collect_ready_match_events(destination_match_ids, tid, now)
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
        next_match = tournament_repository.get_match(
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

        loser_next_match = tournament_repository.get_match(
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

    # Revert tournament state for terminal elimination
    # matches.
    if match.next_match_id is None:
        tournament = tournament_repository.get_tournament(
            match.tournament_id
        )
        if tournament.elimination_mode in (
            EliminationMode.SINGLE_ELIMINATION,
            EliminationMode.DOUBLE_ELIMINATION,
        ):
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


def unconfirm_match(
    match_id: TournamentMatchID,
    initiator_id: UserID,
) -> Result[None, str]:
    """Unconfirm a match and cascade-retract advanced contestants.

    Uses a single DB commit for the entire cascade and dispatches
    all collected events afterwards.

    Acquires a row lock (SELECT ... FOR UPDATE) on the initial
    match to prevent concurrent unconfirmation races.
    """
    # Lock the match row to prevent TOCTOU races.
    match = tournament_repository.get_match_for_update(match_id)

    result = _unconfirm_match_impl(
        match_id, initiator_id, _match=match,
    )
    if result.is_err():
        return Err(result.unwrap_err())

    events, deleted_events, tournament_was_uncompleted = result.unwrap()

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
                tournament_id=match.tournament_id,
            ),
        )

    return Ok(None)


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
    match = tournament_repository.get_match_for_update(match_id)
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

    # Map placements to points.
    tournament = tournament_repository.get_tournament(match.tournament_id)
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
    match = tournament_repository.get_match_for_update(match_id)

    # Reject already-confirmed matches.
    if match.confirmed_by is not None:
        return Err('Match is already confirmed.')

    contestants = tournament_repository.get_contestants_for_match(match_id)

    # Validate all placements are set.
    missing = [c for c in contestants if c.placement is None]
    if missing:
        return Err(
            f'Not all placements are set. '
            f'{len(missing)} contestant(s) lack placements.'
        )

    tournament_repository.confirm_match(match_id, initiator_id)

    tournament = tournament_repository.get_tournament(match.tournament_id)

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
