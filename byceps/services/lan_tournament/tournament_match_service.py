from dataclasses import replace
from datetime import UTC, datetime
from typing import NamedTuple

from byceps.services.user.models.user import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import tournament_repository
from .events import (
    ContestantAdvancedEvent,
    MatchConfirmedEvent,
    MatchUnconfirmedEvent,
    TournamentCompletedEvent,
    TournamentUncompletedEvent,
)
from .models.tournament import Tournament, TournamentID
from .models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from .models.tournament_mode import TournamentMode
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
    match_unconfirmed,
    tournament_completed,
    tournament_uncompleted,
)
from .tournament_domain_service import (
    determine_match_winner,
    generate_round_robin_schedule,
)


MAX_MATCH_SCORE = 999_999_999

class MatchUserRole(NamedTuple):
    contestant: TournamentMatchToContestant | None
    is_loser: bool
    can_confirm: bool
    can_submit: bool


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
) -> Result[int, str]:
    """Generate single elimination bracket with all rounds."""
    import math
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
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

    # Build rounds from final backwards so we can set next_match_id
    # rounds_matches[r] holds match IDs for round r
    rounds_matches: list[list[TournamentMatchID]] = [
        [] for _ in range(num_rounds)
    ]
    match_events: list[MatchCreatedEvent] = []

    # Create matches round by round, final first
    for r in range(num_rounds - 1, -1, -1):
        num_matches_in_round = 2 ** (num_rounds - 1 - r)
        for m in range(num_matches_in_round):
            match_id = TournamentMatchID(generate_uuid7())

            # Determine next_match_id from the next round
            if r < num_rounds - 1:
                next_match_id = rounds_matches[r + 1][m // 2]
            else:
                next_match_id = None

            match = TournamentMatch(
                id=match_id,
                tournament_id=tournament_id,
                group_order=None,
                match_order=m,
                round=r,
                next_match_id=next_match_id,
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

    # Single transaction commit
    tournament_repository.commit_session()

    # Dispatch events after successful commit
    for event in match_events:
        signals.match_created.send(None, event=event)

    total_matches = bracket_size - 1
    return Ok(total_matches)


def generate_double_elimination_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
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

    # Validate tournament mode.
    if tournament.tournament_mode != TournamentMode.DOUBLE_ELIMINATION:
        return Err('Tournament mode must be DOUBLE_ELIMINATION.')

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
) -> list[ContestantAdvancedEvent]:
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

    return _process_defwin_entries(tournament_id, entries)


def handle_defwin_for_removed_team(
    tournament_id: TournamentID,
    team_id: TournamentTeamID,
) -> list[ContestantAdvancedEvent]:
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

    return _process_defwin_entries(tournament_id, entries)


def _process_defwin_entries(
    tournament_id: TournamentID,
    entries: list[tuple[TournamentMatchToContestant, TournamentMatch]],
) -> list[ContestantAdvancedEvent]:
    """Shared defwin advancement logic for removed contestants.

    After the removed contestant's entry has been deleted from each
    match, check whether the sole remaining opponent should be
    auto-advanced to the next round.

    Does NOT commit — caller handles the transaction.
    """
    now = datetime.now(UTC)
    events: list[ContestantAdvancedEvent] = []

    # Each entry's contestant has already been deleted from its match
    # by the caller, so `remaining` below reflects the post-deletion
    # state of that match.
    for _contestant, match in entries:
        remaining = tournament_repository.get_contestants_for_match(match.id)

        # If both contestants were removed (len == 0),
        # no advancement is possible — skip silently.
        if len(remaining) != 1 or match.next_match_id is None:
            continue

        sole = remaining[0]

        # Guard: skip if contestant already in next match
        next_contestants = tournament_repository.get_contestants_for_match(
            match.next_match_id
        )
        already_advanced = any(
            c.participant_id == sole.participant_id
            and c.team_id == sole.team_id
            for c in next_contestants
        )
        if already_advanced:
            continue

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

        events.append(
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

    return events


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
        return MatchUserRole(None, False, False, False)

    contestant = _resolve_initiator_contestant(
        tournament_id, user_id, contestants
    )
    if contestant is None:
        return MatchUserRole(None, False, False, False)

    # DEFWIN: fewer than 2 real contestants — match needs no score
    # submission; the bracket generator has already auto-advanced
    # the sole player.
    real_contestants = [
        c for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]
    if len(real_contestants) < 2:
        return MatchUserRole(contestant, False, False, False)

    all_have_scores = all(c.score is not None for c in real_contestants)
    if not all_have_scores:
        return MatchUserRole(contestant, False, False, True)

    winner_result = determine_match_winner(contestants)
    if winner_result.is_err():
        return MatchUserRole(contestant, False, False, False)

    winner = winner_result.unwrap()
    if winner is None:
        # Draw — any participant may confirm; both may submit revised
        # scores until confirmation.
        return MatchUserRole(contestant, False, True, True)
    elif winner.id != contestant.id:
        # This user is the loser.
        return MatchUserRole(contestant, True, True, True)
    else:
        # This user is the winner — no action needed.
        return MatchUserRole(contestant, False, False, False)


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
        key = contestant.participant_id or contestant.team_id
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
    if tournament.tournament_mode != TournamentMode.ROUND_ROBIN:
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
    )

    return Ok(events)


def _try_lb_defwin_advance(
    lb_match_id: TournamentMatchID,
    tournament_id: TournamentID,
    now: datetime,
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
    if match.next_match_id is not None:
        return Ok(False)

    if tournament.tournament_mode not in (
        TournamentMode.SINGLE_ELIMINATION,
        TournamentMode.DOUBLE_ELIMINATION,
    ):
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
        )
        if lb.is_err():
            return Err(lb.unwrap_err())
        adv_events += lb.unwrap()
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
    return Ok(None)


def _unconfirm_match_impl(
    match_id: TournamentMatchID,
    initiator_id: UserID,
    _visited: set[TournamentMatchID] | None = None,
    _match: TournamentMatch | None = None,
) -> Result[tuple[list[MatchUnconfirmedEvent], bool], str]:
    """Flush-only, event-collecting helper for unconfirm_match.

    Performs all unconfirmation logic (including cascade) using
    repository flush calls for intermediate operations.  Collects
    and returns domain events rather than dispatching them.  The
    caller is responsible for the single ``commit()`` and event
    dispatch.

    Returns ``Ok((events, tournament_was_uncompleted))`` where
    ``tournament_was_uncompleted`` is ``True`` when tournament
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
            cascade_events, cascade_uncompleted = (
                cascade_result.unwrap()
            )
            collected_events.extend(cascade_events)
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
            cascade_events, cascade_uncompleted = (
                cascade_result.unwrap()
            )
            collected_events.extend(cascade_events)
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

    # Revert tournament state for terminal elimination
    # matches.
    if match.next_match_id is None:
        tournament = tournament_repository.get_tournament(
            match.tournament_id
        )
        if tournament.tournament_mode in (
            TournamentMode.SINGLE_ELIMINATION,
            TournamentMode.DOUBLE_ELIMINATION,
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

    return Ok((collected_events, tournament_was_uncompleted))


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

    events, tournament_was_uncompleted = result.unwrap()

    # Single commit for the entire cascade.
    tournament_repository.commit_session()

    # Dispatch all collected events after commit.
    for event in events:
        match_unconfirmed.send(None, event=event)

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
