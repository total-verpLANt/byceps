import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cache, partial
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    tournament_qualification_domain_service as qualification_domain,
    tournament_qualification_repository,
    tournament_repository,
    tournament_seeding_repository,
)
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
    MatchInvitationID,
    MatchSide,
    MatchUserRole,
    TournamentMatch,
    TournamentMatchID,
)
from .models.match_readiness import (
    MatchReadiness,
    derive_match_readiness,
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
    FFA_CUT_REQUIRED_MSGID,
    FfaDeadEnd,
    ffa_single_track_dead_end,
    contestant_id,
    compute_ffa_cumulative_standings,
    derive_contestant_type,
    determine_match_winner,
    generate_round_robin_schedule,
    map_placement_to_points,
    snake_seed_groups,
    elimination_mode_for_phase,
    game_format_for_phase,
)
from .tournament_log_service import create_log_entry
from .models.seeding import SeedingFormat
from .models.readiness_change import ReadinessChange
from .tournament_seeding_domain_service import (
    derive_layout,
    group_sizes as seeding_group_sizes,
)

logger = logging.getLogger(__name__)

WINNER_SCOPE = 'winner'


MAX_MATCH_SCORE = 999_999_999

# Keep this a static msgid; the views translate it.
QUALIFICATION_TIE_ERROR = (
    'Qualification cannot be determined automatically because of a tie. '
    'An orga decision is required.'
)

# Keep this a static msgid; the views translate it.
PLAYOFF_ROUNDS_FROM_DRAFT_ERROR = (
    'Playoff rounds are generated from the seeding draft.'
)

FFA_GRAND_FINAL_ERROR = 'The grand final is next. Generate it instead.'
FFA_ROUND_LOCKED_ERROR = (
    'A match of this round already has a result. Its seeding is locked.'
)

FFA_UNTAGGED_WB_ERROR = (
    'This winners round was not made from a seeding. Its seeding is locked.'
)

# Keep this a static msgid; the views translate it without parameters.
FFA_LOBBY_BELOW_MINIMUM_ERROR = (
    'A lobby has fewer contestants than the minimum group size.'
)

# Keep this a static msgid; the views translate it without parameters.
FFA_STALLS_ERROR = (
    'This setup stalls in a later round: its lobbies would fall below the '
    'minimum size. Change the number advancing per lobby or the lobby sizes.'
)
FFA_TEAM_LIMIT_BELOW_LOBBY_MIN_ERROR = (
    'The maximum number of teams is below the minimum lobby size.'
)

# Keep this a static msgid; the views translate it without parameters.
FFA_LONE_SURVIVOR_ERROR = (
    'Only one contestant is left in the losers pool. They get a bye and'
    ' join the next losers round.'
)

# The seeding codec stores the lobby size in one byte.
_FFA_LOBBY_PARAM_MAX = 255

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


def _decided_by_placements(
    tournament: Tournament, match: TournamentMatch | None = None
) -> bool:
    """Return `True` if the tournament's matches are decided by placement.

    With a *match* of a playoff tournament the answer is that phase's
    game format, so a phase-2 lobby of a highscore tournament counts.

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
    game_format = tournament.game_format
    if match is not None and _has_playoffs(tournament):
        game_format = game_format_for_phase(tournament, match.phase)
    return isinstance(game_format, GameFormat) and game_format.uses_placements


def _has_playoffs(tournament: Tournament) -> bool:
    """Return `True` if the tournament has a playoff phase.

    The identity test keeps Mock tournaments (truthy attributes) on
    the phase-less path, as `_decided_by_placements` does.
    """
    return tournament.has_playoffs is True


def _ffa_phase(tournament: Tournament) -> int | None:
    """Return the phase that runs free-for-all lobbies, or `None`.

    1 for a free-for-all tournament, 2 for a highscore tournament whose
    playoffs are free-for-all. Phase 1 of the latter is the leaderboard.
    """
    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        return 1
    if (
        _has_playoffs(tournament)
        and tournament.game_format == GameFormat.HIGHSCORE
        and tournament.playoff_game_format == GameFormat.FREE_FOR_ALL
    ):
        return 2
    return None


def _ffa_elimination_mode(tournament: Tournament) -> EliminationMode | None:
    """Return the elimination mode of the phase that runs the lobbies."""
    phase = _ffa_phase(tournament)
    if phase is None:
        return None
    return elimination_mode_for_phase(tournament, phase)


def ffa_stall_for(tournament: Tournament, count: int) -> FfaDeadEnd | None:
    """Return the dead end a plain FFA single-elimination setup runs into."""
    if (
        _ffa_phase(tournament) != 1
        or _ffa_elimination_mode(tournament)
        != EliminationMode.SINGLE_ELIMINATION
        or not tournament.advancement_count
        or not tournament.group_size_max
    ):
        return None
    return ffa_single_track_dead_end(
        count,
        tournament.group_size_min,
        tournament.group_size_max,
        tournament.advancement_count,
    )


PHASE1_LOCKED_ERROR = (
    'The playoffs are released. Group results are locked. '
    'Take the release back first.'
)


def _refuse_phase1_change_after_release(
    tournament: Tournament, match: TournamentMatch
) -> Result[None, str]:
    """Refuse a change to a phase-1 match once the playoffs are released."""
    if (
        _has_playoffs(tournament)
        and tournament.playoff_released_at is not None
        and match.phase == 1
    ):
        return Err(PHASE1_LOCKED_ERROR)
    return Ok(None)


def _try_auto_release(
    tournament_id: TournamentID, triggered_by: UserID
) -> None:
    """Release the playoffs if the committed change made that due.

    A direct call after the commit, not a signal handler. A refusal
    leaves the tournament as it is: the qualification panel shows why.
    `triggered_by` is the user whose change may have made it due.
    """
    tournament = tournament_repository.get_tournament(tournament_id)
    if not _has_playoffs(tournament):
        return

    from . import tournament_qualification_service

    tournament_qualification_service.auto_release_after_commit(
        tournament_id, triggered_by=triggered_by
    )


def _elimination_mode_of(
    tournament: Tournament, match: TournamentMatch
) -> EliminationMode | None:
    """Return the elimination mode that governs the match's phase."""
    if _has_playoffs(tournament) and match.phase == 2:
        return elimination_mode_for_phase(tournament, 2)
    return tournament.elimination_mode


class DefwinResult(NamedTuple):
    """Events produced by defwin processing, for post-commit dispatch."""

    advanced: list[ContestantAdvancedEvent]
    confirmed: list[MatchConfirmedEvent]
    completed: list[TournamentCompletedEvent]


def has_matches(tournament_id: TournamentID) -> bool:
    """Check if tournament already has matches."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    return len(matches) > 0


def _create_match_contestant_flush(
    contestant: TournamentMatchToContestant,
) -> None:
    """Engine insert plus pairing/audit in the owner's transaction.

    The engine already holds the tournament and ordered reachable match locks;
    generated rows are new and cannot yet be observed by other transactions.
    No Ready prerequisite and no effects dispatched here.
    """
    from . import tournament_readiness_service

    try:
        tournament_repository.create_match_contestant(contestant)
        result = tournament_readiness_service.refresh_pairing_and_invitations_flush(
            contestant.tournament_match_id,
            occurred_at=datetime.now(UTC).replace(tzinfo=None),
        )
        if result.is_err():
            raise ValueError(result.unwrap_err())
    except Exception:
        tournament_repository.rollback_session()
        raise


def _audit_engine_pairing_change_flush(before: TournamentMatch) -> None:
    """Audit repository deletion backstops before the owner's commit."""
    after = tournament_repository.get_match(before.id)
    if after.readiness_revision == before.readiness_revision:
        return
    try:
        create_log_entry(
            'match-pairing-invalidated', before.tournament_id, None,
            data={
                'match_id': str(before.id),
                'previous_pairing_id': str(before.pairing_id) if before.pairing_id else None,
                'previous_pairing_generation': before.pairing_generation,
                'pairing_generation': after.pairing_generation,
                'readiness_revision': after.readiness_revision,
            },
            commit=False,
        )
    except Exception:
        tournament_repository.rollback_session()
        raise


def _delete_contestant_from_match_flush(
    match_id: TournamentMatchID,
    *,
    team_id: TournamentTeamID | None = None,
    participant_id: TournamentParticipantID | None = None,
) -> None:
    before = tournament_repository.find_match(match_id)
    if before is None:
        return  # A dangling destination has no assignment to retract.
    tournament_repository.delete_contestant_from_match(
        match_id, team_id=team_id, participant_id=participant_id,
    )
    _audit_engine_pairing_change_flush(before)


def _delete_contestants_for_match_flush(match_id: TournamentMatchID) -> None:
    before = tournament_repository.get_match(match_id)
    tournament_repository.delete_contestants_for_match_flush(match_id)
    _audit_engine_pairing_change_flush(before)


def _reset_match_readiness_flush(match_id: TournamentMatchID) -> None:
    """Same-pair replay invalidates revision, never acceptance or occupancy."""
    from . import tournament_readiness_service

    before = tournament_repository.get_match(match_id)
    tournament_repository.unconfirm_match(match_id, reset_readiness=False)
    result = tournament_readiness_service.reset_readiness_flush(
        match_id, occurred_at=datetime.now(UTC).replace(tzinfo=None),
    )
    if result.is_err():
        tournament_repository.rollback_session()
        raise ValueError(result.unwrap_err())
    if result.unwrap().match.readiness_revision == before.readiness_revision:
        # Reset even without claims must invalidate old request capabilities.
        tournament_repository.clear_match_readiness_flush(
            match_id, increment_revision=True,
        )
        try:
            create_log_entry(
                'match-readiness-reset', before.tournament_id, None,
                data={'match_id': str(match_id),
                      'pairing_generation': before.pairing_generation,
                      'readiness_revision': before.readiness_revision + 1},
                commit=False,
            )
        except Exception:
            tournament_repository.rollback_session()
            raise
    # Reconcile after the unconditional revision bump as well. Old pre-send
    # tokens retire; accepted/sending/unknown work remains historical fact.
    _pending_invitations_flush((match_id,), datetime.now(UTC))


def clear_bracket(
    tournament_id: TournamentID,
    *,
    phase: int | None = None,
    initiator_id: UserID | None = None,
) -> list[MatchDeletedEvent]:
    """Delete the bracket, or one *phase* of it (flush only).

    Return the events to dispatch.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    all_matches = tournament_repository.get_matches_for_tournament_ordered_fresh(
        tournament_id
    )
    matches = [m for m in all_matches if phase is None or m.phase == phase]
    if not matches:
        return []

    now = datetime.now(UTC)

    # Collect event data before deletion (IDs won't be accessible after).
    match_ids = [m.id for m in matches]
    tournament_repository.lock_matches_for_update(match_ids)

    confirmed_ids = [m.id for m in matches if m.confirmed_by is not None]
    contestants_by_match_id = tournament_repository.get_contestants_for_matches(
        confirmed_ids
    )
    log_data: dict[str, object] = {
        'match_count': len(matches),
        'confirmed_results': {
            str(match_id): _snapshot_contestant_scores(
                match_id,
                contestants=contestants_by_match_id.get(match_id, []),
            )
            for match_id in confirmed_ids
        },
    }
    if phase is not None:
        log_data['phase'] = phase
    create_log_entry(
        'bracket-cleared',
        tournament_id,
        initiator_id,
        data=log_data,
        commit=False,
    )

    # NULL out self-referential FKs first to avoid IntegrityError
    # on PostgreSQL (next_match_id / loser_next_match_id point to
    # sibling rows in the same table). Phase 1 of a playoff
    # tournament holds no links, so clearing it must not null the
    # links of the phase 2 matches that remain.
    if phase != 1 or len(matches) == len(all_matches):
        tournament_repository.null_self_referential_fks(tournament_id)

    # Delete children (comments, contestants) then matches.
    for match_id in match_ids:
        tournament_repository.delete_comments_for_match_flush(match_id)
        _delete_contestants_for_match_flush(match_id)
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
    phase: int = 1,
    roster: Sequence[str] | None = None,
) -> Result[tuple[Tournament, list[str], list[MatchDeletedEvent]], str]:
    """Shared preamble for bracket generation functions.

    Locks the tournament, validates contestant type, fetches
    contestant IDs, checks minimum count and the generator's own
    *check*, and only then clears existing matches when
    *force_regenerate* is set (flush only).

    Phase 2 generates for the given *roster* (the qualifiers) and
    only ever sees and clears phase-2 matches.

    Returns ``Ok((tournament, contestant_ids, deleted_events))``
    on success or ``Err(reason)`` on failure.
    """
    from .models.contestant_type import ContestantType

    # Lock tournament to prevent race conditions.
    tournament_repository.lock_tournament_for_update(tournament_id)

    # Check if matches already exist (atomic with lock).
    if phase == 2:
        had_matches = any(
            m.phase == 2
            for m in tournament_repository.get_matches_for_tournament(
                tournament_id
            )
        )
    else:
        had_matches = has_matches(tournament_id)
    if had_matches and not force_regenerate:
        return Err(
            'Tournament already has matches.'
            ' Use force regenerate to clear'
            ' and rebuild.'
        )

    # Get tournament to check contestant type. A legacy row can still
    # have a NULL contestant type; derive it from team size in memory
    # for the rest of this call (and its caller), rather than
    # persisting it here.
    tournament = tournament_repository.get_tournament(tournament_id)
    tournament = replace(
        tournament,
        contestant_type=derive_contestant_type(
            tournament.contestant_type,
            tournament.max_players_in_team,
            tournament.min_players_in_team,
        ),
    )

    # Get contestants (participants or teams).
    if roster is not None:
        contestant_ids = list(roster)
    elif tournament.contestant_type == ContestantType.TEAM:
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

    # Clear last, so a refused regeneration keeps the old bracket.
    deleted_events: list[MatchDeletedEvent] = []
    if force_regenerate and had_matches:
        deleted_events = clear_bracket(
            tournament_id,
            phase=2 if phase == 2 else None,
            initiator_id=initiator_id,
        )

    return Ok((tournament, contestant_ids, deleted_events))


def _check_double_elimination(
    tournament: Tournament,
    contestant_ids: list[str],
    phase: int = 1,
) -> Result[None, str]:
    """Check the preconditions a double-elimination bracket adds."""
    if len(contestant_ids) < 4:
        return Err(
            'Need at least 4 contestants for double-elimination bracket.'
        )

    mode = (
        elimination_mode_for_phase(tournament, 2)
        if phase == 2
        else tournament.elimination_mode
    )
    if mode != EliminationMode.DOUBLE_ELIMINATION:
        return Err('Tournament elimination mode must be DOUBLE_ELIMINATION.')

    return Ok(None)


LAYOUT_ROSTER_ERROR = 'The seeding does not match the roster.'


@dataclass(frozen=True)
class GenerationOutcome:
    """What a flush-only generator created, for post-commit dispatch.

    `unchanged`: nothing was generated, the matches already follow the code.
    """

    count: int
    created_events: list[MatchCreatedEvent]
    deleted_events: list[MatchDeletedEvent]
    ready_match_ids: frozenset[TournamentMatchID]
    occurred_at: datetime
    completed_event: TournamentCompletedEvent | None = None
    unchanged: bool = False
    pending_invitation_ids: tuple[MatchInvitationID, ...] = ()


def _pending_invitations_flush(
    match_ids: Collection[TournamentMatchID], occurred_at: datetime,
) -> tuple[MatchInvitationID, ...]:
    from . import tournament_readiness_service

    try:
        result = tournament_readiness_service.reconcile_invitations_flush(
            match_ids, occurred_at=occurred_at,
        )
        if result.is_err():
            raise ValueError(result.unwrap_err())
        return result.unwrap()
    except Exception:
        tournament_repository.rollback_session()
        raise


def collect_generation_invitations_flush(
    outcome: GenerationOutcome,
) -> GenerationOutcome:
    """Final coalesced assignment reconciliation before the owning commit."""
    pending = _pending_invitations_flush(
        outcome.ready_match_ids, outcome.occurred_at,
    )
    return replace(outcome, pending_invitation_ids=pending)


def dispatch_generation_events(
    tournament_id: TournamentID, outcome: GenerationOutcome
) -> None:
    """Send deleted, created and ready signals; call only after the commit."""
    from . import signals

    from . import tournament_readiness_service

    try:
        for deleted in outcome.deleted_events:
            signals.match_deleted.send(None, event=deleted)
        for created in outcome.created_events:
            signals.match_created.send(None, event=created)

        ready_events = _collect_ready_match_events(
            set(outcome.ready_match_ids), tournament_id, outcome.occurred_at
        )
        for ready in ready_events:
            match_ready.send(None, event=ready)
        if outcome.completed_event is not None:
            tournament_completed.send(None, event=outcome.completed_event)
    finally:
        # Even a failed listener cannot prevent dispatch of committed intents.
        tournament_readiness_service.dispatch_pending_invitations(
            outcome.pending_invitation_ids,
        )


def _check_elimination_layout(
    layout: Sequence[str | None],
    also: Callable[[Tournament, list[str]], Result[None, str]] | None = None,
) -> Callable[[Tournament, list[str]], Result[None, str]]:
    """Return a preamble check that the layout fits the bracket and roster."""
    import math

    def check(
        tournament: Tournament, contestant_ids: list[str]
    ) -> Result[None, str]:
        if also is not None:
            also_result = also(tournament, contestant_ids)
            if also_result.is_err():
                return also_result

        bracket_size = 2 ** math.ceil(math.log2(len(contestant_ids)))
        placed = [cid for cid in layout if cid is not None]
        if (
            len(layout) != bracket_size
            or len(placed) != len(set(placed))
            or set(placed) != set(contestant_ids)
        ):
            return Err(LAYOUT_ROSTER_ERROR)
        return Ok(None)

    return check


def _dispatch_confirmation_effects(
    confirmed_event, completed_event, adv_events, created_events, ready_events,
    pending_invitation_ids: Collection[MatchInvitationID],
) -> None:
    """Post-commit events, then durable work even if a listener raises."""
    from . import tournament_readiness_service

    try:
        match_confirmed.send(None, event=confirmed_event)
        if completed_event is not None:
            tournament_completed.send(None, event=completed_event)
        for event in adv_events:
            contestant_advanced.send(None, event=event)
        for event in created_events:
            match_created.send(None, event=event)
        for event in ready_events:
            match_ready.send(None, event=event)
    finally:
        tournament_readiness_service.dispatch_pending_invitations(
            pending_invitation_ids,
        )


def generate_single_elimination_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate single elimination bracket with all rounds."""
    result = _generate_single_elimination_impl(
        tournament_id, force_regenerate, initiator_id=initiator_id
    )
    if result.is_err():
        return Err(result.unwrap_err())

    outcome = result.unwrap()
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise
    dispatch_generation_events(tournament_id, outcome)
    return Ok(outcome.count)


def _generate_single_elimination_impl(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    layout: Sequence[str | None] | None = None,
    initiator_id: UserID | None = None,
    phase: int = 1,
    roster: Sequence[str] | None = None,
    seeding_target: str | None = None,
) -> Result[GenerationOutcome, str]:
    """Generate single elimination bracket without committing.

    *layout* lists the contestant (or `None` for a bye) of every
    first-round slot; by default it is derived from the standard
    seed order over the roster. *phase* 2 builds the playoffs for
    the qualifiers in *roster*. The caller owns commit and dispatch.
    """
    import math
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from .models.bracket import Bracket
    from .models.contestant_type import ContestantType
    from .tournament_domain_service import _standard_seed_order

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(
        tournament_id,
        force_regenerate,
        initiator_id=initiator_id,
        check=(
            _check_elimination_layout(layout) if layout is not None else None
        ),
        phase=phase,
        roster=roster,
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
            phase=phase,
            seeding_target=seeding_target,
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
                phase=phase,
                seeding_target=seeding_target,
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

    if layout is None:
        # Seed round 0 using standard seed order, padded with None for DEFWINs.
        padded: list[str | None] = list(contestant_ids) + [None] * (
            bracket_size - num_contestants
        )
        layout = [padded[s] for s in _standard_seed_order(bracket_size)]

    # Place contestants into round 0 matches
    for slot_idx, cid in enumerate(layout):
        match_idx = slot_idx // 2
        match_id = rounds_matches[0][match_idx]

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
        _create_match_contestant_flush(contestant)

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
            _create_match_contestant_flush(advanced)
            # Auto-confirm the DEFWIN match
            if initiator_id is not None:
                tournament_repository.confirm_match(
                    match_id, initiator_id
                )

    all_match_ids: set[TournamentMatchID] = set()
    for round_matches in rounds_matches:
        all_match_ids.update(round_matches)
    if p3_id is not None:
        all_match_ids.add(p3_id)

    total_matches = bracket_size - 1
    if p3_id is not None:
        total_matches += 1
    return Ok(
        collect_generation_invitations_flush(GenerationOutcome(
            count=total_matches,
            created_events=match_events,
            deleted_events=deleted_events,
            ready_match_ids=frozenset(all_match_ids),
            occurred_at=now,
        ))
    )


def generate_double_elimination_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate double elimination bracket with WB, LB, and
    GF.  The Grand Final is the terminal match.
    """
    result = _generate_double_elimination_impl(
        tournament_id, force_regenerate, initiator_id=initiator_id
    )
    if result.is_err():
        return Err(result.unwrap_err())

    outcome = result.unwrap()
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise
    dispatch_generation_events(tournament_id, outcome)
    return Ok(outcome.count)


def _generate_double_elimination_impl(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    layout: Sequence[str | None] | None = None,
    initiator_id: UserID | None = None,
    phase: int = 1,
    roster: Sequence[str] | None = None,
    seeding_target: str | None = None,
) -> Result[GenerationOutcome, str]:
    """Generate double elimination bracket without committing.

    *layout*, *phase* and *roster* are as for
    `_generate_single_elimination_impl`, the layout for the first
    winners-bracket round. The caller owns commit and dispatch.
    """
    import math
    from uuid import UUID

    from .models.bracket import Bracket
    from .models.contestant_type import ContestantType
    from .tournament_domain_service import _standard_seed_order

    check_de = (
        _check_double_elimination
        if phase == 1
        else partial(_check_double_elimination, phase=phase)
    )

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(
        tournament_id,
        force_regenerate,
        initiator_id=initiator_id,
        check=(
            _check_elimination_layout(layout, also=check_de)
            if layout is not None
            else check_de
        ),
        phase=phase,
        roster=roster,
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
        phase=phase,
        seeding_target=seeding_target,
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
                phase=phase,
                seeding_target=seeding_target,
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
                phase=phase,
                seeding_target=seeding_target,
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
    if layout is None:
        padded: list[str | None] = list(contestant_ids) + [None] * (
            bracket_size - num_contestants
        )
        layout = [padded[s] for s in _standard_seed_order(bracket_size)]

    for slot_idx, cid in enumerate(layout):
        match_idx = slot_idx // 2
        match_id = wb_ids[0][match_idx]

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
        _create_match_contestant_flush(contestant)

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
            _create_match_contestant_flush(advanced)
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

    all_match_ids: set[TournamentMatchID] = set()
    for round_matches in wb_ids:
        all_match_ids.update(round_matches)
    for round_matches in lb_ids:
        all_match_ids.update(round_matches)
    if gf_id:
        all_match_ids.add(gf_id)

    return Ok(
        collect_generation_invitations_flush(GenerationOutcome(
            count=len(match_events),
            created_events=match_events,
            deleted_events=deleted_events,
            ready_match_ids=frozenset(all_match_ids),
            occurred_at=now,
        ))
    )


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
    """Return the structural violations that block a tournament start."""
    if tournament is None:
        tournament = tournament_repository.get_tournament(tournament_id)

    if tournament.game_format == GameFormat.FREE_FOR_ALL:
        # FFA generates its rounds on the fly, but round 1 must exist.
        if not tournament_repository.get_matches_for_tournament_ordered(
            tournament_id
        ):
            return ['no matches generated']
        return []

    if not (
        tournament.game_format
        and tournament.game_format.requires_bracket_generation
    ):
        # HIGHSCORE bypasses bracket generation.
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
        phase_one = [m for m in matches if m.phase == 1]
        return _validate_round_robin_bracket(
            phase_one,
            {m.id: contestants_by_match.get(m.id, []) for m in phase_one},
        )

    # An unset or invalid elimination mode must not pass as valid.
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
    """Count the distinct real contestants across those matches."""
    ids: set[str] = set()
    for match_contestants in contestants_by_match.values():
        for c in match_contestants:
            if c.participant_id is None and c.team_id is None:
                continue
            ids.add(contestant_id(c))
    return len(ids)


def _collect_dangling_links(matches: list[TournamentMatch]) -> list[str]:
    """Return violations for links to matches outside the tournament."""
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

    # A match without a round is a violation itself and would break
    # `max()` below.
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

    # A live LB match must route its winner onward; a dead one need not.
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
    """Validate a round-robin bracket against itself, not the roster.

    Matches that carry a `group_order` are checked per group.
    """
    violations = []
    if not matches:
        return ['no matches generated']

    if any(m.group_order is not None for m in matches):
        return _validate_round_robin_groups(matches, contestants_by_match)

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


def _validate_round_robin_groups(
    matches: list[TournamentMatch],
    contestants_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ],
) -> list[str]:
    """Return the violations of each round-robin group, k(k-1)/2 apiece."""
    violations = []
    group_orders = sorted(
        {m.group_order for m in matches if m.group_order is not None}
    )
    if any(m.group_order is None for m in matches):
        violations.append('round-robin match without a group')

    for group_order in group_orders:
        group_matches = [m for m in matches if m.group_order == group_order]
        group_contestants = {
            m.id: contestants_by_match.get(m.id, []) for m in group_matches
        }
        count = _distinct_contestant_count(group_contestants)
        if count < 2:
            violations.append(
                f'group {group_order}: expected at least 2 contestants, '
                f'found {count}'
            )
        expected = count * (count - 1) // 2
        if len(group_matches) != expected:
            violations.append(
                f'group {group_order}: expected {expected} round-robin '
                f'matches, found {len(group_matches)}'
            )

    return violations


def generate_round_robin_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    initiator_id: UserID | None = None,
) -> Result[int, str]:
    """Generate round-robin bracket with all pairings."""
    result = _generate_round_robin_impl(
        tournament_id, force_regenerate, initiator_id=initiator_id
    )
    if result.is_err():
        return Err(result.unwrap_err())

    outcome = result.unwrap()
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise
    dispatch_generation_events(tournament_id, outcome)
    return Ok(outcome.count)


def _check_round_robin_groups(
    seed_list: Sequence[str], group_sizes: Sequence[int]
) -> Callable[[Tournament, list[str]], Result[None, str]]:
    """Return a preamble check that the groups partition the roster."""

    def check(
        tournament: Tournament, contestant_ids: list[str]
    ) -> Result[None, str]:
        if (
            sum(group_sizes) != len(seed_list)
            or min(group_sizes, default=0) < 2
            or len(set(seed_list)) != len(seed_list)
            or set(seed_list) != set(contestant_ids)
        ):
            return Err(LAYOUT_ROSTER_ERROR)
        return Ok(None)

    return check


def _generate_round_robin_impl(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    seed_list: Sequence[str] | None = None,
    group_sizes: Sequence[int] | None = None,
    initiator_id: UserID | None = None,
    seeding_target: str | None = None,
) -> Result[GenerationOutcome, str]:
    """Generate round-robin pairings without committing.

    *seed_list* is cut into consecutive groups of *group_sizes*; each
    group plays its own schedule and its matches carry the group index
    as `group_order`. Without them, the whole roster is one group with
    no `group_order`. The caller owns commit and dispatch.
    """
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from .models.contestant_type import ContestantType

    if (seed_list is None) != (group_sizes is None):
        raise ValueError('seed_list and group_sizes go together')

    # Shared preamble: lock, validate, fetch contestants.
    prep_result = _prepare_bracket_generation(
        tournament_id,
        force_regenerate,
        initiator_id=initiator_id,
        check=(
            _check_round_robin_groups(seed_list, group_sizes)
            if seed_list is not None and group_sizes is not None
            else None
        ),
    )
    if prep_result.is_err():
        return Err(prep_result.unwrap_err())

    tournament, contestant_ids, deleted_events = prep_result.unwrap()

    if seed_list is None and tournament.playoff_group_count:
        # Playoff tournaments always play in groups: snake-seed the roster.
        group_sizes = seeding_group_sizes(
            SeedingFormat.ROUND_ROBIN,
            len(contestant_ids),
            tournament.playoff_group_count,
        )
        seed_list = [
            cid
            for cid in derive_layout(
                SeedingFormat.ROUND_ROBIN,
                contestant_ids,
                tournament.playoff_group_count,
            )
            if cid is not None
        ]

    groups: list[list[str]] = []
    if seed_list is not None and group_sizes is not None:
        start = 0
        for size in group_sizes:
            groups.append(list(seed_list[start : start + size]))
            start += size
    else:
        groups.append(list(contestant_ids))
    has_groups = group_sizes is not None

    is_team = tournament.contestant_type == ContestantType.TEAM
    now = datetime.now(UTC)
    match_events: list[MatchCreatedEvent] = []
    total_matches = 0

    for group_idx, group in enumerate(groups):
        # Generate round-robin schedule via domain service.
        schedule = generate_round_robin_schedule(sorted(group))

        for round_num, round_pairings in enumerate(schedule):
            for match_idx, (p1, p2) in enumerate(round_pairings):
                match_id = TournamentMatchID(generate_uuid7())
                match = TournamentMatch(
                    id=match_id,
                    tournament_id=tournament_id,
                    group_order=group_idx if has_groups else None,
                    match_order=match_idx,
                    round=round_num,
                    next_match_id=None,
                    confirmed_by=None,
                    created_at=now,
                    seeding_target=seeding_target,
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
                            participant_id=(
                                TournamentParticipantID(UUID(cid))
                            ),
                            score=None,
                            created_at=now,
                        )
                    _create_match_contestant_flush(contestant)

                match_events.append(
                    MatchCreatedEvent(
                        occurred_at=now,
                        initiator=None,
                        tournament_id=tournament_id,
                        match_id=match_id,
                    )
                )
                total_matches += 1

    return Ok(
        collect_generation_invitations_flush(GenerationOutcome(
            count=total_matches,
            created_events=match_events,
            deleted_events=deleted_events,
            ready_match_ids=frozenset(e.match_id for e in match_events),
            occurred_at=now,
        ))
    )


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

    # Lock before any write.
    _lock_defwin_entry_matches(tournament_id, entries)

    for _contestant, match in entries:
        _delete_contestant_from_match_flush(
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

    # Lock before any write.
    _lock_defwin_entry_matches(tournament_id, entries)

    for _contestant, match in entries:
        _delete_contestant_from_match_flush(
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
                _create_match_contestant_flush(advanced)

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
                elif not completed_events:
                    plain = try_complete_plain_round_robin(tournament)
                    if plain.is_err():
                        logger.warning(
                            'Auto-complete failed for tournament %s '
                            'after defwin on match %s: %s',
                            tournament_id,
                            match.id,
                            plain.unwrap_err(),
                        )
                    else:
                        plain_event = plain.unwrap()
                        if plain_event is not None:
                            completed_events.append(plain_event)

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

    On `Err`, the session has been rolled back.
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

    locked = _refuse_phase1_change_after_release(
        tournament_repository.get_tournament(match.tournament_id), match
    )
    if locked.is_err():
        return _reject(locked.unwrap_err())

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
    """Return `{contestant key: score}` for the real contestants.

    Call this before the retraction cascade clears the scores.
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
    """Return the scores the cascade clears from downstream matches.

    Best effort: return `{}` if the classification fails.
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

    Return the contestant row ID to score map on success.
    """
    contestants = tournament_repository.get_contestants_for_match(
        match_id
    )

    # Exclude DEFWIN slots (no participant or team assigned).
    real_contestants = [
        c for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]

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

    # Draws are only accepted in round-robin tournaments.
    match = tournament_repository.get_match(match_id)
    tournament = tournament_repository.get_tournament(match.tournament_id)
    if _elimination_mode_of(tournament, match) != EliminationMode.ROUND_ROBIN:
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
    """Set every contestant score and confirm the match; flush only.

    The caller commits and dispatches the events. `_locks_held` says
    the caller already locked the reachable set in this transaction.
    """
    # Lock the match rows before the score write locks contestant rows,
    # or this deadlocks against a concurrent correction.
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
    locked = _refuse_phase1_change_after_release(tournament, match)
    if locked.is_err():
        return Err(locked.unwrap_err())
    if _decided_by_placements(tournament, match):
        return Err(PLACEMENT_FORMAT_CONFIRM_ERROR)

    validation = _validate_match_scores(match_id, scores)
    if validation.is_err():
        return Err(validation.unwrap_err())

    id_to_score = validation.unwrap()

    # Flush-only write — all scores flushed together.
    tournament_repository.update_contestant_scores(id_to_score)

    # Auto-confirm: admin submitted scores → match is resolved.
    # _confirm_match_impl will see the flushed scores.
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

    Post-rollback note: when this function returns ``Err``, the
    database session has been rolled back.  Any ORM-managed objects
    fetched before this call may be expired or detached.  Callers
    must NOT access attributes on those objects after receiving an
    ``Err`` result.
    """
    result = _admin_set_and_confirm_match_impl(match_id, admin_id, scores)
    if result.is_err():
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

    pending = _pending_invitations_flush(
        {event.match_id for event in ready_events}, confirmed_event.occurred_at,
    )
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    _dispatch_confirmation_effects(
        confirmed_event, completed_event, adv_events, created_events, ready_events,
        pending,
    )
    _try_auto_release(confirmed_event.tournament_id, admin_id)
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
    """Confirm a drawn match (round-robin only).

    Flush only; the caller commits and dispatches the events.
    """
    if _elimination_mode_of(tournament, match) != EliminationMode.ROUND_ROBIN:
        return Err(
            'Match is a draw; a winner is required '
            'in this tournament mode.'
        )

    tournament_repository.confirm_match(
        match_id, initiator_id,
    )

    completed = try_complete_plain_round_robin(tournament)
    if completed.is_err():
        return Err(completed.unwrap_err())

    now = datetime.now(UTC)
    confirmed_event = MatchConfirmedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=match.tournament_id,
        match_id=match_id,
        winner_team_id=None,
        winner_participant_id=None,
    )
    return Ok((confirmed_event, completed.unwrap(), [], [], []))


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
    _create_match_contestant_flush(new_contestant)

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
    _create_match_contestant_flush(loser_entry)

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
    _create_match_contestant_flush(advanced)

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
        phase=match.phase,
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
    _create_match_contestant_flush(
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
    _create_match_contestant_flush(
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

    if _has_playoffs(tournament):
        # Phase 1 only seeds the playoffs; phase 2 decides.
        if match.phase != 2:
            return False
        game_format = game_format_for_phase(tournament, 2)
        elimination_mode = elimination_mode_for_phase(tournament, 2)
    else:
        game_format = tournament.game_format
        elimination_mode = tournament.elimination_mode

    if elimination_mode not in (
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ):
        return False

    # FFA matches never have a next match, so that test cannot apply.
    if game_format == GameFormat.FREE_FOR_ALL:
        if elimination_mode == EliminationMode.DOUBLE_ELIMINATION:
            return match.bracket == Bracket.GRAND_FINAL
        round_matches = tournament_repository.get_matches_for_round(
            tournament.id, match.round, bracket=None,
        )
        if _has_playoffs(tournament):
            round_matches = [m for m in round_matches if m.phase == 2]
        return len(round_matches) == 1 or (
            _single_survivor_source_plan(match, tournament) is not None
        )

    return match.next_match_id is None


def retraction_reverts_completion(
    match: TournamentMatch,
    tournament: Tournament,
) -> bool:
    """Return `True` if retracting the match reopens the tournament."""
    return tournament.tournament_status == TournamentStatus.COMPLETED and (
        is_deciding_match(match, tournament)
        or is_plain_round_robin(tournament)
    )


def _single_survivor_source_plan(
    match: TournamentMatch,
    tournament: Tournament,
    *,
    matches: Sequence[TournamentMatch] | None = None,
    contestants_by_match: Mapping[
        TournamentMatchID, list[TournamentMatchToContestant]
    ] | None = None,
    decisions: qualification_domain.DecisionOrders | None = None,
    active_ids: Collection[str] | None = None,
) -> 'FfaAdvancePlan | None':
    """Recognize the fully confirmed source of a no-lobby SE completion."""
    phase = _ffa_phase(tournament)
    if (
        phase is None or match.phase != phase or match.bracket is not None
        or _ffa_elimination_mode(tournament) is not EliminationMode.SINGLE_ELIMINATION
        or (_has_playoffs(tournament) and tournament.playoff_released_at is None)
        or tournament.advancement_count is None or tournament.advancement_count < 1
    ):
        return None
    if matches is None:
        matches = tournament_repository.get_matches_for_tournament(tournament.id)
    phase_matches = [
        m for m in matches if m.phase == phase and m.bracket is None
    ]
    if not phase_matches or match.round is None:
        return None
    source = max(m.round for m in phase_matches if m.round is not None)
    round_matches = [m for m in phase_matches if m.round == source]
    if (
        match.round != source or match.id not in {m.id for m in round_matches}
        or len(round_matches) < 2
        or any(m.confirmed_by is None for m in round_matches)
    ):
        return None
    ranked = _round_standings(
        round_matches,
        tournament.advancement_count,
        ffa_decisions(tournament.id) if decisions is None else decisions,
        contestants_by_match=contestants_by_match,
        active_ids=active_ids,
    )
    if ranked.is_err():
        return None
    survivors = _order_by_standing(ranked.unwrap())
    if len(survivors) != 1:
        return None
    return FfaAdvancePlan(
        pool=None, round_number=source + 1,
        survivors=tuple(s.contestant_id for s in survivors),
        bands={s.contestant_id: s.band for s in survivors},
        grand_final_eligible=False,
    )


def ffa_round_already_advanced(
    match: TournamentMatch,
    tournament: Tournament,
    *,
    matches: Sequence[TournamentMatch] | None = None,
) -> bool:
    """Return `True` if a later FFA round was built from the match's round."""
    if _ffa_phase(tournament) is None:
        return False

    if match.bracket == Bracket.GRAND_FINAL:
        return False

    if matches is None:
        matches = tournament_repository.get_matches_for_tournament(tournament.id)

    # Double elimination seeds its grand final from both pools.
    if match.bracket is not None and any(
        m.bracket == Bracket.GRAND_FINAL for m in matches
    ):
        return True

    # A waiting winners advance builds only the merged losers round,
    # which carries the next winners round's target.
    if match.bracket is Bracket.WINNERS and match.round is not None:
        merged = ffa_round_seeding_target(Bracket.WINNERS, match.round + 1)
        if any(
            m.bracket is Bracket.LOSERS and m.seeding_target == merged
            for m in matches
        ):
            return True

    return any(
        m.bracket == match.bracket and (m.round or 0) > (match.round or 0)
        for m in matches
    )


def ffa_round_consumed(
    match: TournamentMatch,
    tournament: Tournament,
    *,
    matches: Sequence[TournamentMatch] | None = None,
    contestants_by_match: Mapping[
        TournamentMatchID, list[TournamentMatchToContestant]
    ] | None = None,
    decisions: qualification_domain.DecisionOrders | None = None,
    active_ids: Collection[str] | None = None,
) -> bool:
    """Return `True` if the match's round fed a later round or decided
    the tournament.
    """
    return ffa_round_already_advanced(match, tournament, matches=matches) or (
        tournament.tournament_status is TournamentStatus.COMPLETED
        and _single_survivor_source_plan(
            match, tournament, matches=matches,
            contestants_by_match=contestants_by_match,
            decisions=decisions,
            active_ids=active_ids,
        ) is not None
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


def is_plain_round_robin(tournament: Tournament) -> bool:
    """Return `True` for a round robin without a playoff phase."""
    return (
        not _has_playoffs(tournament)
        and tournament.elimination_mode == EliminationMode.ROUND_ROBIN
    )


def active_contestant_ids(tournament_id: TournamentID) -> set[str]:
    """Return the IDs of the participants and teams still in the tournament."""
    return {
        str(p.id)
        for p in tournament_repository.get_participants_for_tournament(
            tournament_id
        )
    } | {
        str(t.id)
        for t in tournament_repository.get_teams_for_tournament(tournament_id)
    }


class PlainRoundRobinStanding(NamedTuple):
    ranking: qualification_domain.Ranking
    open_match_count: int
    total_match_count: int
    contestants: dict[str, TournamentMatchToContestant]


def plain_round_robin_standing(
    tournament: Tournament,
) -> PlainRoundRobinStanding:
    """Rank a plain round robin for its winner, with the stored decision.

    Removed contestants are not ranked; their results still count for
    their opponents.
    """
    matches = tournament_repository.get_matches_for_tournament_ordered_fresh(
        tournament.id
    )
    by_match = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    decision = tournament_qualification_repository.find_decision(
        tournament.id, WINNER_SCOPE
    )

    members: list[str] = []
    by_id: dict[str, TournamentMatchToContestant] = {}
    results = []
    walkovers = []
    for match in matches:
        entries = by_match.get(match.id, [])
        ids = [contestant_id(c) for c in entries]
        for cid, entry in zip(ids, entries, strict=True):
            if cid not in by_id:
                members.append(cid)
                by_id[cid] = entry
        if len(entries) == 1 and match.confirmed_by is not None:
            walkovers.append(ids[0])
        if len(entries) != 2:
            continue
        results.append(
            qualification_domain.MatchResult(
                a=ids[0],
                b=ids[1],
                score_a=entries[0].score or 0,
                score_b=entries[1].score or 0,
                confirmed=match.confirmed_by is not None,
            )
        )

    ranking = qualification_domain.classify_ties(
        qualification_domain.rank_round_robin(
            WINNER_SCOPE,
            members,
            results,
            decision.orders if decision else (),
            active_ids=active_contestant_ids(tournament.id),
            walkovers=walkovers,
        ),
        cut=None,
        plain_winner=True,
    )
    return PlainRoundRobinStanding(
        ranking=ranking,
        open_match_count=sum(1 for m in matches if m.confirmed_by is None),
        total_match_count=len(matches),
        contestants=by_id,
    )


def try_complete_plain_round_robin(
    tournament: Tournament,
) -> Result[TournamentCompletedEvent | None, str]:
    """Complete a plain round robin that has a winner (flush only).

    Every match must be confirmed, and the first place must be clear or
    decided by an orga. Otherwise the tournament stays as it is and the
    result is `Ok(None)`. The caller commits and dispatches the event.
    Only an ongoing tournament completes; a change into ONGOING checks
    again.
    """
    if not is_plain_round_robin(tournament):
        return Ok(None)
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return Ok(None)

    standing = plain_round_robin_standing(tournament)
    if (
        standing.total_match_count == 0
        or standing.open_match_count
        or not standing.ranking.entries
    ):
        return Ok(None)

    winner_id = qualification_domain.plain_round_robin_winner(
        standing.ranking
    )
    if winner_id.is_err():
        return Ok(None)
    winner = standing.contestants[winner_id.unwrap()]

    winner_set = tournament_repository.set_tournament_winner(
        tournament.id,
        winner_team_id=winner.team_id,
        winner_participant_id=winner.participant_id,
    )
    if winner_set.is_err():
        return Err(winner_set.unwrap_err())
    status_set = tournament_repository.set_tournament_status_flush(
        tournament.id,
        TournamentStatus.COMPLETED,
    )
    if status_set.is_err():
        return Err(status_set.unwrap_err())

    return Ok(
        TournamentCompletedEvent(
            occurred_at=datetime.now(UTC),
            initiator=None,
            tournament_id=tournament.id,
            winner_team_id=winner.team_id,
            winner_participant_id=winner.participant_id,
        )
    )


def complete_settled_plain_round_robin(tournament_id: TournamentID) -> None:
    """Complete a plain round robin settled while it was not running.

    Call it after the commit of a change into ONGOING; it commits on its
    own.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)
    completed = try_complete_plain_round_robin(tournament)
    event = completed.unwrap() if completed.is_ok() else None
    if event is None:
        tournament_repository.rollback_session()
        return
    tournament_repository.commit_session()
    tournament_completed.send(None, event=event)


# -------------------------------------------------------------------- #
# F-04: per-side readiness claim / revocation
# -------------------------------------------------------------------- #


def claim_ready(
    match_id: TournamentMatchID,
    side: MatchSide,
    initiator_id: UserID,
    *,
    expected_pairing_generation: int,
    expected_readiness_revision: int,
) -> Result[ReadinessChange, str]:
    """Flush-only facade; owning caller rolls back/commits, then dispatches."""
    from . import tournament_readiness_service

    return tournament_readiness_service.claim_ready_flush(
        match_id, side, initiator_id,
        expected_pairing_generation=expected_pairing_generation,
        expected_readiness_revision=expected_readiness_revision,
    )


def revoke_ready(
    match_id: TournamentMatchID,
    side: MatchSide,
    initiator_id: UserID,
    *,
    expected_pairing_generation: int,
    expected_readiness_revision: int,
) -> Result[ReadinessChange, str]:
    """Flush-only explicit-side facade, with no trusted orga argument."""
    from . import tournament_readiness_service

    return tournament_readiness_service.revoke_ready_flush(
        match_id, side, initiator_id,
        expected_pairing_generation=expected_pairing_generation,
        expected_readiness_revision=expected_readiness_revision,
    )


def dispatch_readiness_effects(change: ReadinessChange) -> Result[None, str]:
    from . import tournament_readiness_service

    return tournament_readiness_service.dispatch_readiness_effects(change)


def get_match_readiness(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
) -> MatchReadiness:
    """Derive the §25.3 display status (shared by both view layers)."""
    tournament = tournament_repository.get_tournament(match.tournament_id)
    return derive_match_readiness(
        match, contestants,
        pairing=tournament_repository.get_match_pairing(match.id),
        supports_readiness=(
            game_format_for_phase(tournament, match.phase) == GameFormat.ONE_V_ONE
        ),
    )


def get_user_readiness_sides(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
    user_id: UserID,
) -> set[MatchSide]:
    """Return the sides whose readiness the user may claim/revoke.

    View-layer helper for rendering controls; authorization is still
    enforced inside the service mutations.
    """
    from . import tournament_readiness_authorization_service

    tournament = tournament_repository.get_tournament(match.tournament_id)
    if tournament.tournament_status != TournamentStatus.ONGOING:
        return set()
    if not get_match_readiness(match, contestants).mutation_available:
        return set()
    pairing = tournament_repository.get_match_pairing(match.id)
    if pairing is None:
        return set()
    result = tournament_readiness_authorization_service.get_user_readiness_sides(
        match.tournament_id, pairing, user_id
    )
    return set(result.unwrap()) if result.is_ok() else set()


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
    """Confirm the match, collecting its events; flush only.

    Lock the reachable bracket in ID order first, so this cannot
    deadlock against a concurrent retraction. The caller commits and
    dispatches the events. `_locks_held` says the caller already locked
    the reachable set in this transaction.
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
    if _decided_by_placements(tournament, match):
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
        and _elimination_mode_of(tournament, match)
        == EliminationMode.DOUBLE_ELIMINATION
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
    plain_completed = None
    if not tournament_was_completed:
        plain = try_complete_plain_round_robin(tournament)
        if plain.is_err():
            return Err(plain.unwrap_err())
        plain_completed = plain.unwrap()
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
    elif plain_completed is not None:
        completed_event = plain_completed

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

    On `Err`, the session has been rolled back. `_locks_held` is
    internal; external callers must not pass it.
    """
    result = _confirm_match_impl(
        match_id, initiator_id, _locks_held=_locks_held
    )
    if result.is_err():
        # Roll back writes flushed before the failure.
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    (
        confirmed_event,
        completed_event,
        adv_events,
        created_events,
        ready_events,
    ) = result.unwrap()

    pending = _pending_invitations_flush(
        {event.match_id for event in ready_events}, confirmed_event.occurred_at,
    )
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    _dispatch_confirmation_effects(
        confirmed_event, completed_event, adv_events, created_events, ready_events,
        pending,
    )
    _try_auto_release(confirmed_event.tournament_id, initiator_id)
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
        # A dangling routing entry counts as absent, as in the classification.
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
        _delete_contestant_from_match_flush(
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
        _delete_contestant_from_match_flush(
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
                _delete_contestant_from_match_flush(
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
            _delete_contestants_for_match_flush(
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

    _reset_match_readiness_flush(match_id)
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
    """Retract the match result and cascade; flush only.

    The caller must hold the reachable-set lock, commit, dispatch the
    events, and roll back on an exception as well as on `Err`. With a
    `reason`, stage a `match-result-retracted` log entry with the
    destroyed scores.
    """
    # Lock the match row to prevent TOCTOU races.
    match = tournament_repository.get_match_for_update(match_id)

    tournament = tournament_repository.get_tournament(match.tournament_id)
    locked = _refuse_phase1_change_after_release(tournament, match)
    if locked.is_err():
        return Err(locked.unwrap_err())
    if ffa_round_already_advanced(match, tournament):
        return Err(
            'A later round has already been built from this result, so it '
            'can no longer be unconfirmed.'
        )

    # Read the scores while locked, before the cascade clears them.
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
        data = {
            'match_id': str(match_id),
            'reason': reason,
            'retracted_scores': retracted_scores,
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
    all collected events afterwards.

    Acquires row locks (SELECT ... FOR UPDATE) on every match the
    cascade can reach, in ID order, to prevent concurrent
    unconfirmation races and deadlocks.

    With a `reason`, a `match-result-retracted` log entry is written
    in the same transaction.
    """
    _lock_reachable_matches(match_id)

    try:
        result = _unconfirm_match_flush(match_id, initiator_id, reason=reason)
    except Exception:
        # Roll back flushed writes; this also runs outside a request.
        tournament_repository.rollback_session()
        raise

    if result.is_err():
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    events, deleted_events, tournament_was_uncompleted, tournament_id = (
        result.unwrap()
    )

    pending = _pending_invitations_flush(
        {event.match_id for event in events}
        - {event.match_id for event in deleted_events}, datetime.now(UTC),
    )
    # Single commit for the entire cascade.
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    from . import tournament_readiness_service

    try:
        for event in events:
            match_unconfirmed.send(None, event=event)
        for event in deleted_events:
            match_deleted.send(None, event=event)
        if tournament_was_uncompleted:
            now = datetime.now(UTC)
            tournament_uncompleted.send(
                None,
                event=TournamentUncompletedEvent(
                    occurred_at=now, initiator=None, tournament_id=tournament_id,
                ),
            )
    finally:
        tournament_readiness_service.dispatch_pending_invitations(pending)

    _try_auto_release(tournament_id, initiator_id)
    return Ok(None)


def _collect_reachable_match_ids(
    match: TournamentMatch,
    matches_by_id: dict[TournamentMatchID, TournamentMatch],
) -> list[TournamentMatchID]:
    """Return every match a retraction from `match` could reach.

    Include the subject match, and do not stop at unconfirmed matches:
    a concurrent admin can change their confirmation state.
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
    """Return the match ID as a real `UUID`.

    A `NewType` wrap of a URL segment is a `str`, which misses in
    `UUID`-keyed lookups.
    """
    if isinstance(match_id, UUID):
        return match_id
    return TournamentMatchID(UUID(str(match_id)))


def _lock_reachable_matches(match_id: TournamentMatchID) -> None:
    """Lock every match reachable from that match, in ID order.

    A single ordered acquisition keeps writers from deadlocking. An
    unknown match locks nothing. The set is re-read fresh after locking
    and locked again while it keeps growing.
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

    Walk what the retraction cascade reaches, continuing only past
    confirmed matches; a cycle ends the walk instead of failing.
    Return the case and the affected match IDs in breadth-first order,
    without the subject match.
    """
    match_id = _as_match_id(match_id)

    match = tournament_repository.find_match_fresh(match_id)
    if match is None:
        return Err(f'Unknown match ID "{match_id}".')

    # Index the tournament's matches once.
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
                    # Absent, or already seen; a cycle ends the walk.
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
                    # The structural-DEFWIN undo also reaches this LB
                    # match's next match.
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

    # Correcting GF M1 deletes the bracket-reset match GF M2 outright.
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


class _InPlaceCorrection(NamedTuple):
    match: TournamentMatch
    contestants: list[TournamentMatchToContestant]
    id_to_score: dict[TournamentMatchToContestantID, int]
    winner: TournamentMatchToContestant


def _plan_in_place_correction(
    match_id: TournamentMatchID,
    contestants: list[TournamentMatchToContestant],
    corrected_scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> _InPlaceCorrection | None:
    """Return the plan if the scores leave the winner and loser as they are.

    Only a confirmed match that advances contestants qualifies: what it
    advanced stays valid, so the retraction cascade has nothing to
    undo. Everything else, including every refusal, takes the full
    correction path.
    """
    match = tournament_repository.find_match_fresh(match_id)
    if (
        match is None
        or match.confirmed_by is None
        or (match.next_match_id is None and match.loser_next_match_id is None)
        or len(contestants) != 2
        or any(
            c.participant_id is None and c.team_id is None for c in contestants
        )
    ):
        return None

    validation = _validate_match_scores(match_id, corrected_scores)
    if validation.is_err():
        return None
    id_to_score = validation.unwrap()

    if all(c.score == id_to_score[c.id] for c in contestants):
        # Unchanged scores are refused by the full path.
        return None

    current = determine_match_winner(contestants)
    proposed = determine_match_winner(
        [replace(c, score=id_to_score[c.id]) for c in contestants]
    )
    if current.is_err() or proposed.is_err():
        return None
    current_winner = current.unwrap()
    winner = proposed.unwrap()
    if (
        winner is None
        or current_winner is None
        or current_winner.id != winner.id
    ):
        return None

    return _InPlaceCorrection(match, contestants, id_to_score, winner)


def _correct_result_in_place(
    plan: _InPlaceCorrection,
    initiator_id: UserID,
    *,
    reason: str,
    corrected_scores: dict[TournamentParticipantID | TournamentTeamID, int],
) -> Result[tuple[CorrectionCase, bool], str]:
    """Write the corrected scores to a match that stays confirmed.

    The corrector becomes the confirmer. The advanced contestants, and
    with them the next matches' pairings and Ready claims, stay as they
    are. Under the locks the caller holds; commit and signals happen
    here.
    """
    match = plan.match
    case = CorrectionCase.NO_DOWNSTREAM

    try:
        create_log_entry(
            'match-result-corrected',
            match.tournament_id,
            initiator_id,
            data={
                'match_id': str(match.id),
                'case': case.value,
                'reason': reason,
                'scores_applied': True,
                'previous_scores': _snapshot_contestant_scores(
                    match.id, contestants=plan.contestants
                ),
                'new_scores': {
                    str(key): score for key, score in corrected_scores.items()
                },
            },
            commit=False,
        )
        tournament_repository.update_contestant_scores(plan.id_to_score)
        tournament_repository.confirm_match(match.id, initiator_id)
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    now = datetime.now(UTC)
    match_unconfirmed.send(
        None,
        event=MatchUnconfirmedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match.id,
            unconfirmed_by=initiator_id,
        ),
    )
    match_confirmed.send(
        None,
        event=MatchConfirmedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match.id,
            winner_team_id=plan.winner.team_id,
            winner_participant_id=plan.winner.participant_id,
        ),
    )

    _try_auto_release(match.tournament_id, initiator_id)

    return Ok((case, True))


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
    """Correct a match result, with audit logging.

    Retraction and score re-entry share one transaction; nothing is
    committed on `Err`. A critical case needs `ack_critical`, and is
    refused when more matches are at stake than
    `acknowledged_match_ids` names.

    Corrected scores that keep the winner and loser of a match that
    advances contestants are written in place: the match stays
    confirmed and nothing downstream is touched or needs an
    acknowledgement, so the case is `NO_DOWNSTREAM`.

    Return the case and whether corrected scores were applied.
    """
    if reason is None or not reason.strip():
        return Err('A correction reason is required.')

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
    if _decided_by_placements(tournament, subject):
        return Err(PLACEMENT_FORMAT_CORRECTION_ERROR)

    # Lock before classifying, or the acknowledgement gate may be stale.
    _lock_reachable_matches(match_id)

    # Read the release under the lock, not from the pre-lock read above.
    locked = _refuse_phase1_change_after_release(
        tournament_repository.get_tournament(subject.tournament_id), subject
    )
    if locked.is_err():
        tournament_repository.rollback_session()
        return Err(locked.unwrap_err())

    # From here on, every early return must roll back to release the
    # locks.

    # A walkover has no winner, so it could never be confirmed again.
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

    if corrected_scores:
        in_place = _plan_in_place_correction(
            match_id, contestants, corrected_scores
        )
        if in_place is not None:
            return _correct_result_in_place(
                in_place,
                initiator_id,
                reason=reason,
                corrected_scores=corrected_scores,
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
        # Validate before the destructive retraction below.
        validation = _validate_match_scores(match_id, corrected_scores)
        if validation.is_err():
            tournament_repository.rollback_session()
            return Err(validation.unwrap_err())

        id_to_score = validation.unwrap()

        # Refuse unchanged scores; the retraction would still cascade.
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

    # Retract, flush only, under the locks taken above.
    try:
        retract_result = _unconfirm_match_flush(
            match_id,
            initiator_id,
            reason=reason,
            # Reuse the classification the acknowledgement gate decided on.
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

        try:
            apply_result = _admin_set_and_confirm_match_impl(
                match_id,
                initiator_id,
                corrected_scores,
                # Locked once, above, for the whole correction.
                _locks_held=True,
            )
        except Exception:
            tournament_repository.rollback_session()
            raise

        if apply_result.is_err():
            # This also undoes the retraction above.
            tournament_repository.rollback_session()
            # Return the msgid unwrapped; the view translates it.
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

    pending = _pending_invitations_flush(
        ({event.match_id for event in retract_events}
         | {event.match_id for event in ready_events})
        - {event.match_id for event in retract_deleted_events}, datetime.now(UTC),
    )
    try:
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    from . import tournament_readiness_service

    try:
        for event in retract_events:
            match_unconfirmed.send(None, event=event)
        for event in retract_deleted_events:
            match_deleted.send(None, event=event)
        if tournament_was_uncompleted:
            now = datetime.now(UTC)
            tournament_uncompleted.send(
                None,
                event=TournamentUncompletedEvent(
                    occurred_at=now, initiator=None, tournament_id=tournament_id,
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
    finally:
        tournament_readiness_service.dispatch_pending_invitations(pending)

    _try_auto_release(tournament_id, initiator_id)

    return Ok((case, scores_applied))


def set_score(
    match_id: TournamentMatchID,
    contestant_id: TournamentParticipantID | TournamentTeamID,
    score: int,
) -> Result[None, str]:
    """Set the score for a contestant in a match."""
    if score < 0:
        return Err('Score cannot be negative.')

    match = tournament_repository.find_match(match_id)
    holds_lock = False
    if match is not None:
        tournament = tournament_repository.get_tournament(match.tournament_id)
        if _has_playoffs(tournament) and match.phase == 1:
            # Re-read the release under the tournament lock, or a release
            # can commit between the check and the score write.
            tournament_repository.lock_tournament_for_update(
                match.tournament_id
            )
            tournament = tournament_repository.get_tournament(
                match.tournament_id
            )
            holds_lock = True
        locked = _refuse_phase1_change_after_release(tournament, match)
        if locked.is_err():
            if holds_lock:
                tournament_repository.rollback_session()
            return locked

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
        if holds_lock:
            tournament_repository.rollback_session()
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

    # Tournament first, then fresh match; one commit and post-commit signal.
    match = tournament_repository.get_match(match_id)
    tournament_repository.lock_tournament_for_update(match.tournament_id)
    match = tournament_repository.get_match_for_update(match_id)
    try:
        tournament_repository.delete_comments_for_match_flush(match_id)
        _delete_contestants_for_match_flush(match_id)
        tournament_repository.delete_match_flush(match_id)
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

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
    """Return the contestants of those matches, grouped by match ID."""
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
    tournament = tournament_repository.get_tournament(tournament_id)
    if _ffa_phase(tournament) == 2:
        return Err(PLAYOFF_ROUNDS_FROM_DRAFT_ERROR)
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
    groups: Sequence[Sequence[str]] | None = None,
    seeding_target: str | None = None,
    allow_undersized: bool = False,
) -> Result[int, str]:
    """Internal: generate FFA round matches without committing.

    When *round_number* is ``None`` the next round number is determined
    automatically (after the lock is held).
    When *groups* is given, it replaces the snake seeding; it must
    partition the roster and respect the minimum group size, unless
    *allow_undersized* says the advance plan permits the shortfall.

    The matches of a highscore tournament's playoffs are phase 2 and
    need the *contestant_ids* (the qualifiers) from the caller.

    Returns ``Ok(match_count)`` on success.
    Caller is responsible for committing the session.
    """
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    # Lock tournament for atomic generation.
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    # Validate game format.
    phase = _ffa_phase(tournament)
    if phase is None:
        return Err('Tournament game format is not FREE_FOR_ALL.')
    if phase == 2 and contestant_ids is None:
        return Err(PLAYOFF_ROUNDS_FROM_DRAFT_ERROR)

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
        group_min = tournament.group_size_min or 2
        max_teams = tournament.max_teams
        if max_teams is not None and max_teams < group_min:
            return Err(FFA_TEAM_LIMIT_BELOW_LOBBY_MIN_ERROR)

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

    if (
        round_number == 0
        and bracket is not Bracket.LOSERS
        and ffa_stall_for(tournament, len(contestant_ids)) is not None
    ):
        return Err(FFA_STALLS_ERROR)

    # Distribute into groups via snake seeding.
    group_size_min = tournament.group_size_min or 2
    group_size_max = tournament.group_size_max or len(contestant_ids)
    if groups is None:
        groups_result = snake_seed_groups(
            contestant_ids, group_size_min, group_size_max,
        )
        if groups_result.is_err():
            return Err(groups_result.unwrap_err())
        groups = groups_result.unwrap()
    else:
        placed = [cid for group in groups for cid in group]
        if len(placed) != len(set(placed)) or set(placed) != set(
            contestant_ids
        ):
            return Err(LAYOUT_ROSTER_ERROR)
        if not allow_undersized and any(
            len(group) < group_size_min for group in groups
        ):
            return Err(FFA_LOBBY_BELOW_MINIMUM_ERROR)

    # A lone contestant never makes a lobby, whatever the minimum.
    if any(len(group) < 2 for group in groups):
        return Err(FFA_LOBBY_BELOW_MINIMUM_ERROR)

    if (
        tournament.advancement_count is None
        and round_number == 0
        and bracket != Bracket.LOSERS
        and (
            _ffa_elimination_mode(tournament)
            == EliminationMode.DOUBLE_ELIMINATION
            or len(groups) > 1
        )
    ):
        return Err(FFA_CUT_REQUIRED_MSGID)

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
            phase=phase,
            seeding_target=seeding_target,
        )
        tournament_repository.create_match(match)
        match_count += 1

        # Create contestant entries for each group member.
        for cid in sorted(group):
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
            _create_match_contestant_flush(contestant)

    return Ok(match_count)


def _generate_ffa_initial_impl(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
    *,
    groups: Sequence[Sequence[str]] | None = None,
    initiator_id: UserID | None = None,
    roster: Sequence[str] | None = None,
    seeding_target: str | None = None,
) -> Result[GenerationOutcome, str]:
    """Generate the first FFA round without committing.

    Replaces an existing bracket when *force_regenerate* is set. On Err
    the caller must roll back, as the old bracket is already cleared.
    The playoffs of a highscore tournament are built for the *roster*
    of qualifiers. The caller owns commit and dispatch.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    existing = tournament_repository.get_matches_for_tournament(tournament_id)
    if existing and not force_regenerate:
        return Err(
            'Tournament already has matches.'
            ' Use force regenerate to clear'
            ' and rebuild.'
        )

    tournament = tournament_repository.get_tournament(tournament_id)
    bracket = (
        Bracket.WINNERS
        if _ffa_elimination_mode(tournament)
        == EliminationMode.DOUBLE_ELIMINATION
        else None
    )

    deleted_events: list[MatchDeletedEvent] = []
    if existing:
        deleted_events = clear_bracket(tournament_id, initiator_id=initiator_id)

    # A release with fewer qualifiers than configured plays smaller lobbies.
    short_release = (
        roster is not None
        and _ffa_phase(tournament) == 2
        and len(roster) < (tournament.playoff_qualifier_count or 0)
    )

    result = _generate_ffa_round_impl(
        tournament_id,
        0,
        list(roster) if roster is not None else None,
        bracket=bracket,
        initiator_id=initiator_id,
        groups=groups,
        seeding_target=seeding_target,
        allow_undersized=short_release,
    )
    if result.is_err():
        return Err(result.unwrap_err())

    now = datetime.now(UTC)
    created = tournament_repository.get_matches_for_tournament(tournament_id)
    created_ids = [m.id for m in created]
    return Ok(
        GenerationOutcome(
            count=result.unwrap(),
            created_events=[
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match_id,
                )
                for match_id in created_ids
            ],
            deleted_events=deleted_events,
            ready_match_ids=frozenset(created_ids),
            occurred_at=now,
        )
    )


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
    try:
        match = tournament_repository.find_match(match_id)
        if match is not None:
            tournament_repository.lock_tournament_for_update(match.tournament_id)
        match = tournament_repository.get_match_for_update(match_id)
        tournament = tournament_repository.get_tournament(match.tournament_id)
        result = _set_ffa_placements_impl(match, tournament, placements)
        if result.is_err():
            tournament_repository.rollback_session()
            return Err(result.unwrap_err())
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    return Ok(None)


def _set_ffa_placements_impl(
    match: TournamentMatch,
    tournament: Tournament,
    placements: dict[str, int],
) -> Result[None, str]:
    """Validate and write placements for an already locked match; flush only."""

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
    locked = _refuse_phase1_change_after_release(tournament, match)
    if locked.is_err():
        return locked
    if not _decided_by_placements(tournament, match):
        return Err('Placements apply only to free-for-all matches.')

    contestants = tournament_repository.get_contestants_for_match(match.id)

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
    return Ok(None)


@dataclass(frozen=True)
class _FfaConfirmationOutcome:
    """Confirmation state carried out of the result transaction for dispatch."""

    tournament_id: TournamentID
    match_id: TournamentMatchID
    winner: TournamentMatchToContestant | None
    tournament_was_completed: bool
    single_survivor_event: TournamentCompletedEvent | None


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
    try:
        match = tournament_repository.find_match(match_id)
        if match is not None:
            tournament_repository.lock_tournament_for_update(match.tournament_id)
        match = tournament_repository.get_match_for_update(match_id)
        tournament = tournament_repository.get_tournament(match.tournament_id)
        result = _confirm_ffa_match_impl(match, tournament, initiator_id)
        if result.is_err():
            tournament_repository.rollback_session()
            return Err(result.unwrap_err())
        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    _dispatch_ffa_confirmation(result.unwrap(), initiator_id)
    return Ok(None)


def _confirm_ffa_match_impl(
    match: TournamentMatch,
    tournament: Tournament,
    initiator_id: UserID,
) -> Result[_FfaConfirmationOutcome, str]:
    """Confirm an already locked FFA match and stage completion/audit; flush only."""

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
    locked = _refuse_phase1_change_after_release(tournament, match)
    if locked.is_err():
        return Err(locked.unwrap_err())
    if not _decided_by_placements(tournament, match):
        return Err('Placements apply only to free-for-all matches.')

    contestants = tournament_repository.get_contestants_for_match(match.id)

    # Validate all placements are set.
    missing = [c for c in contestants if c.placement is None]
    if missing:
        return Err(
            f'Not all placements are set. '
            f'{len(missing)} contestant(s) lack placements.'
        )

    tournament_repository.confirm_match(match.id, initiator_id)

    tournament_was_completed = False
    winner = None

    single_survivor_event = None

    # Phase 1 of a playoff tournament never completes it.
    if _has_playoffs(tournament):
        completion_phase = 2 if match.phase == 2 else None
    else:
        completion_phase = 1
    elimination_mode = (
        elimination_mode_for_phase(tournament, completion_phase)
        if completion_phase is not None
        else None
    )
    game_format = (
        game_format_for_phase(tournament, completion_phase)
        if completion_phase is not None
        else None
    )

    # Check for FFA+SE auto-complete.
    if (
        elimination_mode == EliminationMode.SINGLE_ELIMINATION
        and game_format == GameFormat.FREE_FOR_ALL
    ):
        round_matches = tournament_repository.get_matches_for_round(
            tournament.id, match.round, bracket=None,
        )
        if _has_playoffs(tournament):
            round_matches = [m for m in round_matches if m.phase == 2]
        # Auto-complete when exactly 1 group in the round (final round).
        if len(round_matches) == 1:
            first_place = [
                c for c in contestants if c.placement == 1
            ]
            if first_place:
                winner = first_place[0]
                comp = _try_auto_complete_tournament(match, tournament, winner)
                if comp.is_err():
                    return Err(comp.unwrap_err())
                tournament_was_completed = comp.unwrap()

        elif tournament.tournament_status is TournamentStatus.ONGOING:
            plan = _single_survivor_source_plan(match, tournament)
            if plan is not None:
                completed = complete_ffa_single_survivor(tournament, plan, initiator_id)
                if completed.is_err():
                    return Err(completed.unwrap_err())
                single_survivor_event = completed.unwrap()

    # Check for FFA+DE Grand Final completion.
    if (
        elimination_mode == EliminationMode.DOUBLE_ELIMINATION
        and game_format == GameFormat.FREE_FOR_ALL
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
                    return Err(comp.unwrap_err())
                tournament_was_completed = comp.unwrap()

    create_log_entry(
        'ffa-match-confirmed',
        match.tournament_id,
        initiator_id,
        data={
            'match_id': str(match.id),
            'placements': _snapshot_contestant_placements(contestants),
        },
        commit=False,
    )

    # Resolve winner for signal dispatch if not already set from
    # auto-complete paths above.
    if winner is None:
        first_place = [c for c in contestants if c.placement == 1]
        if first_place:
            winner = first_place[0]

    return Ok(_FfaConfirmationOutcome(
        tournament_id=match.tournament_id,
        match_id=match.id,
        winner=winner,
        tournament_was_completed=tournament_was_completed,
        single_survivor_event=single_survivor_event,
    ))


def _dispatch_ffa_confirmation(
    outcome: _FfaConfirmationOutcome,
    initiator_id: UserID,
) -> None:
    """Dispatch FFA events and qualification follow-up after the result commit."""
    winner = outcome.winner
    if winner is not None:
        now = datetime.now(UTC)
        tid = outcome.tournament_id
        match_confirmed.send(None, event=MatchConfirmedEvent(
            occurred_at=now, initiator=None,
            tournament_id=tid, match_id=outcome.match_id,
            winner_team_id=winner.team_id,
            winner_participant_id=winner.participant_id,
        ))
        if outcome.tournament_was_completed:
            tournament_completed.send(None, event=TournamentCompletedEvent(
                occurred_at=now, initiator=None,
                tournament_id=tid,
                winner_team_id=winner.team_id,
                winner_participant_id=winner.participant_id,
            ))

    if outcome.single_survivor_event is not None:
        tournament_completed.send(None, event=outcome.single_survivor_event)

    _try_auto_release(outcome.tournament_id, initiator_id)


def set_and_confirm_ffa_match(
    match_id: TournamentMatchID,
    placements: dict[str, int],
    initiator_id: UserID,
) -> Result[None, str]:
    """Set placements and confirm them in one locked result transaction.

    Placements, points, confirmation, completion and audit commit together.
    Refusals roll back; mutation/commit exceptions roll back and propagate.
    A commit exception is not proof that the server did not commit. Signals
    and qualification follow-up run outside that cleanup boundary, after a
    successful commit, and may own independent transactions.
    """
    try:
        match = tournament_repository.find_match(match_id)
        if match is not None:
            tournament_repository.lock_tournament_for_update(match.tournament_id)
        match = tournament_repository.get_match_for_update(match_id)
        tournament = tournament_repository.get_tournament(match.tournament_id)

        placements_result = _set_ffa_placements_impl(match, tournament, placements)
        if placements_result.is_err():
            tournament_repository.rollback_session()
            return Err(placements_result.unwrap_err())

        result = _confirm_ffa_match_impl(match, tournament, initiator_id)
        if result.is_err():
            tournament_repository.rollback_session()
            return Err(result.unwrap_err())

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    _dispatch_ffa_confirmation(result.unwrap(), initiator_id)
    return Ok(None)


@dataclass(frozen=True)
class _Standing:
    """One contestant's place after an FFA lobby."""

    contestant_id: str
    band: int
    points: int
    lobby: int
    place: int


@dataclass(frozen=True)
class FfaAdvancePlan:
    """The FFA round(s) an advance creates, before anything is written.

    `survivors` play `round_number` of `pool`, ordered by standing, and
    `bands` gives each one's rank band (0 = lobby winners). In the winners
    bracket, `lb_pool` is the pool of the losers round that goes with it.
    """

    pool: Bracket | None
    round_number: int
    survivors: tuple[str, ...]
    bands: Mapping[str, int]
    grand_final_eligible: bool
    lb_pool: tuple[str, ...] = ()
    lb_round_number: int | None = None


@dataclass(frozen=True)
class UndersizedPool:
    """A pool permitted below the minimum, with its shortfall reason."""

    pool: Bracket | None
    round_number: int
    count: int
    lobbies: tuple[int, ...]
    minimum: int
    natural_shortfall: bool = False


@dataclass(frozen=True)
class LobbyBye:
    """A lone contestant of a pool, who gets no lobby and carries over."""

    pool: Bracket | None
    round_number: int
    contestant_id: str


def ffa_lobby_byes(plan: FfaAdvancePlan) -> tuple[LobbyBye, ...]:
    """Return the byes of a plan: a losers pool of one has no lobby."""
    if len(plan.lb_pool) == 1 and plan.lb_round_number is not None:
        return (
            LobbyBye(Bracket.LOSERS, plan.lb_round_number, plan.lb_pool[0]),
        )
    return ()


def is_lone_losers_round(plan: FfaAdvancePlan) -> bool:
    """Tell whether the plan's own round is a losers round of one."""
    return plan.pool is Bracket.LOSERS and len(plan.survivors) == 1


FFA_WINNERS_FINISHED_ERROR = (
    'The winners bracket is finished. Continue the losers bracket.'
)
FFA_LOSERS_UNCONFIRMED_ERROR = 'Losers bracket matches are not confirmed.'
FFA_GRAND_FINAL_TARGET = 'ffa:GF'
FFA_GRAND_FINAL_NOT_ELIGIBLE_ERROR = (
    'The Grand Final is not eligible yet. Continue the bracket rounds.'
)


def is_waiting_winners_round(plan: FfaAdvancePlan) -> bool:
    """Tell whether the WB winner waits while the merged LB pool plays."""
    return plan.pool is Bracket.WINNERS and len(plan.survivors) == 1


def is_single_survivor(plan: FfaAdvancePlan) -> bool:
    """Tell whether a single-track plan has only its winner left."""
    return plan.pool is None and len(plan.survivors) == 1


def ffa_draft_survivors(plan: FfaAdvancePlan) -> tuple[str, ...]:
    """Return the contestants who actually play the draft's lobbies."""
    return plan.lb_pool if is_waiting_winners_round(plan) else plan.survivors


def complete_ffa_single_survivor(
    tournament: Tournament,
    plan: FfaAdvancePlan,
    initiator_id: UserID | None,
) -> Result[TournamentCompletedEvent, str]:
    """Persist the sole single-track survivor as winner without committing."""
    if (
        not is_single_survivor(plan)
        or tournament.tournament_status is not TournamentStatus.ONGOING
        or _ffa_phase(tournament) is None
        or _ffa_elimination_mode(tournament) == EliminationMode.DOUBLE_ELIMINATION
    ):
        return Err('The tournament must be ongoing to advance an FFA round.')
    cid = plan.survivors[0]
    if cid not in active_contestant_ids(tournament.id):
        return Err(LAYOUT_ROSTER_ERROR)
    team_id = (
        TournamentTeamID(UUID(cid))
        if tournament.contestant_type is ContestantType.TEAM else None
    )
    participant_id = (
        TournamentParticipantID(UUID(cid)) if team_id is None else None
    )
    winner_set = tournament_repository.set_tournament_winner(
        tournament.id,
        winner_team_id=team_id,
        winner_participant_id=participant_id,
    )
    if winner_set.is_err():
        return Err(winner_set.unwrap_err())
    status_set = tournament_repository.set_tournament_status_flush(
        tournament.id, TournamentStatus.COMPLETED
    )
    if status_set.is_err():
        return Err(status_set.unwrap_err())
    create_log_entry(
        'bracket-single-survivor',
        tournament.id,
        initiator_id,
        data={'pool': 'SE', 'round': plan.round_number, 'contestant': cid},
        commit=False,
    )
    return Ok(
        TournamentCompletedEvent(
            occurred_at=datetime.now(UTC),
            initiator=None,
            tournament_id=tournament.id,
            winner_team_id=team_id,
            winner_participant_id=participant_id,
        )
    )


def ffa_pool_token(bracket: Bracket | None) -> str:
    """Return the pool token of a bracket in targets and scopes."""
    if bracket is Bracket.WINNERS:
        return 'WB'
    if bracket is Bracket.LOSERS:
        return 'LB'
    return 'SE'


def ffa_lobby_scope(match: TournamentMatch) -> str:
    """Return the decision scope of an FFA lobby."""
    return (
        f'ffa:{ffa_pool_token(match.bracket)}:{match.round}'
        f':{match.group_order or 0}'
    )


def ffa_decisions(
    tournament_id: TournamentID,
) -> dict[str, tuple[tuple[str, ...], ...]]:
    """Return the stored FFA tie decision orders, by scope."""
    return {
        scope: decision.orders
        for scope, decision in (
            tournament_qualification_repository.get_decisions_for_tournament(
                tournament_id
            ).items()
        )
        if scope.startswith('ffa:')
    }


def rank_ffa_lobby(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
    cut: int,
    decisions: qualification_domain.DecisionOrders,
    active_ids: Collection[str] | None = None,
) -> qualification_domain.Ranking:
    """Rank one lobby by points, honouring the orga decision for its scope.

    Removed contestants keep their entry, but are not ranked: they take
    no slot at the cut. *active_ids* defaults to the tournament's own.
    """
    if active_ids is None:
        active_ids = active_contestant_ids(match.tournament_id)
    scope = ffa_lobby_scope(match)
    # A tie keeps its input order: fix it by placement, then ID.
    in_order = sorted(
        contestants,
        key=lambda c: (
            c.placement is None,
            c.placement or 0,
            contestant_id(c),
        ),
    )
    ranking = qualification_domain.rank_by_value(
        scope,
        {
            contestant_id(c): c.points or 0
            for c in in_order
            if contestant_id(c) in active_ids
        },
        higher_is_better=True,
        orders=decisions.get(scope, ()),
    )
    return qualification_domain.classify_ties(
        ranking, cut=cut, plain_winner=False
    )


def _open_cut_tie(ranking: qualification_domain.Ranking) -> list[str]:
    """Return the contestants of the undecided ties across the cut."""
    return [
        cid
        for tie in ranking.ties
        if tie.kind is qualification_domain.TieKind.CUT and not tie.decided
        for cid in tie.contestant_ids
    ]


def ffa_lobby_cut(
    match: TournamentMatch,
    contestants: Sequence[TournamentMatchToContestant],
    cut: int,
    active_ids: Collection[str] | None = None,
    *,
    lobbies_in_round: int | None = None,
) -> int:
    """Return how many of the lobby advance.

    A winners pool no larger than the cut is a single lobby: all but one
    of it advance, so somebody drops and the lone winner can wait.
    """
    if match.bracket is not Bracket.WINNERS:
        return cut
    if active_ids is None:
        active_ids = active_contestant_ids(match.tournament_id)
    active = sum(1 for c in contestants if contestant_id(c) in active_ids)
    if active > cut:
        return cut
    if lobbies_in_round is None:
        lobbies_in_round = len(tournament_repository.get_matches_for_round(
            match.tournament_id, match.round or 0, bracket=Bracket.WINNERS
        ))
    return max(1, active - 1) if lobbies_in_round == 1 else cut


def _split_ffa_lobby(
    match: TournamentMatch,
    contestants: list[TournamentMatchToContestant],
    cut: int,
    decisions: qualification_domain.DecisionOrders,
    *,
    active_ids: Collection[str] | None = None,
    lobbies_in_round: int | None = None,
) -> Result[tuple[list[_Standing], list[_Standing]], list[str]]:
    """Split a lobby into advancing and dropped contestants.

    Returns ``Err(tied_ids)`` on an undecided tie across the cut. The
    band of a contestant counts the distinct ranks above it among those
    on its side of the cut.
    """
    if active_ids is None:
        active_ids = active_contestant_ids(match.tournament_id)
    cut = ffa_lobby_cut(
        match, contestants, cut, active_ids, lobbies_in_round=lobbies_in_round
    )
    ranking = rank_ffa_lobby(match, contestants, cut, decisions, active_ids)
    tied = _open_cut_tie(ranking)
    if tied:
        return Err(tied)

    lobby = match.group_order or 0
    placed = list(enumerate(ranking.entries))

    def standings(part: list) -> list[_Standing]:
        ranks = sorted({entry.rank for _place, entry in part})
        return [
            _Standing(
                contestant_id=entry.contestant_id,
                band=ranks.index(entry.rank),
                points=entry.value or 0,
                lobby=lobby,
                place=place,
            )
            for place, entry in part
        ]

    return Ok((standings(placed[:cut]), standings(placed[cut:])))


def _order_by_standing(standings: Iterable[_Standing]) -> list[_Standing]:
    """Order by rank band, then points, then lobby and place."""
    return sorted(
        standings, key=lambda s: (s.band, -s.points, s.lobby, s.place)
    )


def _ffa_lobbies(
    tournament: Tournament, ordered_ids: Sequence[str]
) -> list[list[str]]:
    """Cut contestants ordered by standing into balanced lobbies."""
    count = len(ordered_ids)
    param = min(tournament.group_size_max or count, _FFA_LOBBY_PARAM_MAX)
    layout = derive_layout(SeedingFormat.FREE_FOR_ALL, ordered_ids, param)
    lobbies: list[list[str]] = []
    start = 0
    for size in seeding_group_sizes(SeedingFormat.FREE_FOR_ALL, count, param):
        lobbies.append([cid for cid in layout[start : start + size] if cid])
        start += size
    return lobbies


def _ffa_lobby_sizes(tournament: Tournament, count: int) -> tuple[int, ...]:
    """Return the lobby sizes `count` contestants are cut into."""
    param = min(tournament.group_size_max or count, _FFA_LOBBY_PARAM_MAX)
    return tuple(
        seeding_group_sizes(SeedingFormat.FREE_FOR_ALL, count, param)
    )


@dataclass(frozen=True)
class RemovedInRace:
    """The removed contestants who were still in the race.

    *winners* were still in the single track or the winners bracket,
    *racing* in either pool.
    """

    winners: frozenset[str] = frozenset()
    racing: frozenset[str] = frozenset()

    def count_for(self, pool: Bracket | None) -> int:
        """Return how many removed contestants the pool lost."""
        if pool is Bracket.LOSERS:
            return len(self.racing)
        return len(self.winners)


def _round_zero_seeding_target(
    matches: Iterable[TournamentMatch],
) -> str | None:
    """Return the seeding target the first round of the phase was made from."""
    first = [
        m
        for m in matches
        if m.bracket not in (Bracket.LOSERS, Bracket.GRAND_FINAL)
    ]
    if not first:
        return None
    start = min(m.round or 0 for m in first)
    return next(
        (m.seeding_target for m in first if (m.round or 0) == start), None
    )


def removed_in_race(tournament: Tournament) -> RemovedInRace:
    """Return the removed contestants who were still in the race."""
    phase = _ffa_phase(tournament)
    if phase is None:
        return RemovedInRace()
    removed = (
        tournament_repository.get_contestant_ids_removed_since_phase_start(
            tournament.id,
            phase,
            teams=tournament.contestant_type == ContestantType.TEAM,
        )
    )
    if not removed:
        return RemovedInRace()
    matches = [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == phase
    ]
    entries_of = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches]
    )
    decisions = ffa_decisions(tournament.id)
    cut = tournament.advancement_count or 1
    seen: set[str] = set()
    out_of_winners: set[str] = set()
    out_of_race: set[str] = set()
    for match in matches:
        entries = entries_of.get(match.id, [])
        leavers = [c for c in entries if contestant_id(c) in removed]
        if not leavers:
            continue
        below_cut: set[str] = set()
        grand_final = match.bracket is Bracket.GRAND_FINAL
        if match.confirmed_by is not None and not grand_final:
            # Rank every entrant, so a stored block still applies to a
            # leaver. A tie across the cut that is undecided cut nobody.
            entrant_ids = {contestant_id(c) for c in entries}
            lobby_cut = ffa_lobby_cut(
                match, entries, cut, active_ids=entrant_ids
            )
            ranking = rank_ffa_lobby(
                match,
                entries,
                lobby_cut,
                decisions,
                active_ids=entrant_ids,
            )
            undecided = set(_open_cut_tie(ranking))
            below_cut = {
                entry.contestant_id
                for place, entry in enumerate(ranking.entries)
                if place >= lobby_cut and entry.contestant_id not in undecided
            }
        for entry in leavers:
            cid = contestant_id(entry)
            seen.add(cid)
            if grand_final:
                out_of_race.add(cid)
                out_of_winners.add(cid)
                continue
            if match.bracket is Bracket.LOSERS:
                out_of_winners.add(cid)
            if cid in below_cut:
                out_of_winners.add(cid)
                if match.bracket is not Bracket.WINNERS:
                    out_of_race.add(cid)
    counted = set(seen)
    unseen = set(removed) - seen
    if unseen:
        target = _round_zero_seeding_target(matches)
        seeding = (
            tournament_seeding_repository.find_seeding(tournament.id, target)
            if target is not None
            else None
        )
        if seeding is None or seeding.generated_seed_code is None:
            counted |= unseen
        else:
            seeded = {entry.id for entry in seeding.roster_snapshot}
            counted |= unseen & seeded
    return RemovedInRace(
        frozenset(counted - out_of_winners), frozenset(counted - out_of_race)
    )


def is_natural_shortfall(
    tournament: Tournament,
    pool: Bracket | None,
    round_number: int,
    sizes: Sequence[int],
    removed: Callable[[], int],
) -> bool:
    """Tell whether the pool is short without its own removals.

    The pool is a winners pool, or the one lobby of the single track's
    final. The shortfall stays with the pool's removals added back.
    """
    minimum = tournament.group_size_min or 2
    if round_number <= 0 or not 2 <= min(sizes) < minimum:
        return False
    if pool is not Bracket.WINNERS and not (pool is None and len(sizes) == 1):
        return False
    return min(_ffa_lobby_sizes(tournament, sum(sizes) + removed())) < minimum


def _undersized_pool(
    tournament: Tournament,
    pool: Bracket | None,
    round_number: int,
    count: int,
    removed: Callable[[], int],
) -> UndersizedPool | None:
    """Permit a shortfall that stays with the pool's removals back.

    The other permitted shortfall is one the removals alone cause. A lone
    contestant never makes a lobby.
    """
    minimum = tournament.group_size_min or 2
    if count < 2:
        return None
    sizes = _ffa_lobby_sizes(tournament, count)
    if min(sizes) >= minimum or min(sizes) < 2:
        return None
    if is_natural_shortfall(tournament, pool, round_number, sizes, removed):
        return UndersizedPool(
            pool, round_number, count, sizes, minimum, natural_shortfall=True
        )
    left = removed()
    if left and min(_ffa_lobby_sizes(tournament, count + left)) >= minimum:
        return UndersizedPool(pool, round_number, count, sizes, minimum)
    return None


def ffa_undersized_pools(
    tournament: Tournament, plan: FfaAdvancePlan
) -> tuple[UndersizedPool, ...]:
    """Return pools permitted below minimum by removals or natural WB shrinkage."""
    in_race = cache(lambda: removed_in_race(tournament))
    pools = [
        _undersized_pool(
            tournament,
            plan.pool,
            plan.round_number,
            len(plan.survivors),
            lambda: in_race().count_for(plan.pool),
        )
    ]
    if plan.lb_pool and plan.lb_round_number is not None:
        pools.append(
            _undersized_pool(
                tournament,
                Bracket.LOSERS,
                plan.lb_round_number,
                len(plan.lb_pool),
                lambda: in_race().count_for(Bracket.LOSERS),
            )
        )
    return tuple(p for p in pools if p is not None)


def advance_ffa_round(
    tournament_id: TournamentID,
    *,
    pool: Bracket | None = None,
    initiator_id: UserID | None = None,
) -> Result[int | str, str]:
    """Advance an FFA tournament to the next round in one call.

    Each lobby is ranked by points. A tie across the cut returns
    ``Err(QUALIFICATION_TIE_ERROR)`` until an orga decision for that
    lobby's scope exists. The survivors, ordered by standing, fill the
    next round's lobbies in a balanced snake. The routes go through
    ``tournament_seeding_service.prepare_ffa_round_draft`` instead.

    For single-track (``pool=None``): the top ``advancement_count`` of
    each lobby advance, the rest are eliminated.

    For double elimination (``pool=Bracket.WINNERS`` or
    ``pool=Bracket.LOSERS``): routes players between WB/LB pools.

    Returns ``Ok(new_match_count)`` on success,
    ``Ok('advanced_wb')``, ``Ok('advanced_lb')``, or
    ``Ok('grand_final_eligible')`` for DE pools,
    or ``Err(reason)`` on failure.

    A losers pool of one contestant gets no lobby: the contestant has a
    bye and carries over to the next losers round, or to the Grand
    Final. A winners advance that leaves one behind in the losers pool
    still creates the winners round, and logs ``bracket-lobby-bye``. A
    losers advance with one survivor creates nothing and returns
    ``Err(FFA_LONE_SURVIVOR_ERROR)``, so repeating it changes nothing.
    """
    # Lock tournament.
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    plan_result = plan_ffa_advance(tournament, pool)
    if plan_result.is_err():
        return Err(plan_result.unwrap_err())
    plan = plan_result.unwrap()

    if plan.grand_final_eligible:
        # Signal GF eligibility; the admin generates the Grand Final.
        return Ok('grand_final_eligible')
    if is_lone_losers_round(plan):
        return Err(FFA_LONE_SURVIVOR_ERROR)

    if is_single_survivor(plan):
        completed = complete_ffa_single_survivor(tournament, plan, initiator_id)
        if completed.is_err():
            tournament_repository.rollback_session()
            return Err(completed.unwrap_err())
        tournament_repository.commit_session()
        tournament_completed.send(None, event=completed.unwrap())
        return Ok('completed')

    # Use _impl (no commit) so WB + LB rounds are created atomically.
    created = _create_planned_ffa_rounds(
        tournament, plan, initiator_id=initiator_id,
        seeding_target=(
            ffa_round_seeding_target(pool, plan.round_number)
            if is_waiting_winners_round(plan) else None
        ),
    )
    if created.is_err():
        tournament_repository.rollback_session()
        return Err(created.unwrap_err())

    tournament_repository.commit_session()
    if pool is None:
        return Ok(created.unwrap())
    return Ok('advanced_wb' if pool is Bracket.WINNERS else 'advanced_lb')


def plan_ffa_advance(
    tournament: Tournament,
    pool: Bracket | None,
    *,
    next_round: int | None = None,
) -> Result[FfaAdvancePlan, str]:
    """Plan the round that follows a confirmed FFA round, writing nothing.

    The source is the latest round of the pool, or the one before
    *next_round* when that is given. The caller holds the tournament lock.
    """
    if _ffa_phase(tournament) is None:
        return Err('Tournament game format is not FREE_FOR_ALL.')
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return Err('The tournament must be ongoing to advance an FFA round.')

    cut = tournament.advancement_count
    if cut is None or cut < 1:
        return Err('Tournament advancement_count is not configured.')

    is_de = (
        _ffa_elimination_mode(tournament) == EliminationMode.DOUBLE_ELIMINATION
    )

    # DE requires an explicit pool parameter.
    if is_de and pool is None:
        return Err(
            'Double elimination requires pool parameter '
            '(Bracket.WINNERS or Bracket.LOSERS).'
        )

    # Single-track must not specify a pool.
    if not is_de and pool is not None:
        return Err('Single-track FFA does not use pool parameter.')

    all_matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament.id
    )
    if not all_matches:
        return Err('Tournament has no matches.')

    decisions = ffa_decisions(tournament.id)
    if pool == Bracket.WINNERS:
        return _plan_ffa_wb(tournament, all_matches, cut, decisions, next_round)
    if pool == Bracket.LOSERS:
        return _plan_ffa_lb(tournament, all_matches, cut, decisions, next_round)
    if pool is not None:
        return Err(f'Invalid pool for DE advancement: {pool}')
    return _plan_ffa_single(tournament, all_matches, cut, decisions, next_round)


def _source_round(rounds: Iterable[int | None], next_round: int | None) -> int:
    """Return the round whose survivors play *next_round*."""
    if next_round is not None:
        return next_round - 1
    return max(r for r in rounds if r is not None)


def _plan_ffa_single(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    cut: int,
    decisions: qualification_domain.DecisionOrders,
    next_round: int | None,
) -> Result[FfaAdvancePlan, str]:
    """Single-track FFA advancement: bottom eliminated, top advance."""
    source = _source_round((m.round for m in all_matches), next_round)
    round_matches = tournament_repository.get_matches_for_round(
        tournament.id,
        source,
    )
    if not round_matches:
        return Err('Tournament has no matches.')

    # Validate all matches in the round are confirmed.
    unconfirmed = [m for m in round_matches if m.confirmed_by is None]
    if unconfirmed:
        return Err(
            f'{len(unconfirmed)} match(es) in round {source} are not confirmed.'
        )

    survivors_result = _round_standings(round_matches, cut, decisions)
    if survivors_result.is_err():
        return Err(QUALIFICATION_TIE_ERROR)
    survivors = _order_by_standing(survivors_result.unwrap())
    if not survivors:
        return Err('No contestants qualified for advancement.')

    # When survivors fit in a single group this becomes the final round
    # (single group = winner-takes-all).  No special signal needed --
    # auto-complete fires when the single-group final is confirmed.
    return Ok(
        FfaAdvancePlan(
            pool=None,
            round_number=source + 1,
            survivors=tuple(s.contestant_id for s in survivors),
            bands={s.contestant_id: s.band for s in survivors},
            grand_final_eligible=False,
        )
    )


def _plan_ffa_wb(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    cut: int,
    decisions: qualification_domain.DecisionOrders,
    next_round: int | None,
) -> Result[FfaAdvancePlan, str]:
    """Winners bracket advancement: top stay in WB, bottom drop to LB."""
    if next_round is not None:
        target = ffa_round_seeding_target(Bracket.WINNERS, next_round)
        all_matches = [m for m in all_matches if m.seeding_target != target]
    wb_matches = [m for m in all_matches if m.bracket == Bracket.WINNERS]
    if not wb_matches:
        return Err('No Winners bracket matches found.')

    source = _source_round((m.round for m in wb_matches), next_round)
    wb_round_matches = tournament_repository.get_matches_for_round(
        tournament.id,
        source,
        bracket=Bracket.WINNERS,
    )
    if not wb_round_matches:
        return Err('No Winners bracket matches found.')

    # Validate all WB round matches confirmed.
    unconfirmed = [m for m in wb_round_matches if m.confirmed_by is None]
    if unconfirmed:
        return Err(
            f'{len(unconfirmed)} WB match(es) in round {source} '
            f'are not confirmed.'
        )

    # Select top N from each WB group; remainder drops to LB.
    wb_advancing: list[_Standing] = []
    wb_dropped: list[_Standing] = []

    for match in wb_round_matches:
        contestants = tournament_repository.get_contestants_for_match(match.id)
        split = _split_ffa_lobby(match, contestants, cut, decisions)
        if split.is_err():
            return Err(QUALIFICATION_TIE_ERROR)
        advancing, dropped = split.unwrap()
        wb_advancing.extend(advancing)
        wb_dropped.extend(dropped)

    if not wb_advancing:
        return Err('No WB contestants qualified for advancement.')

    lb_matches = [m for m in all_matches if m.bracket is Bracket.LOSERS]
    if len(wb_advancing) == 1:
        waiting_target = ffa_round_seeding_target(Bracket.WINNERS, source + 1)
        if next_round is None and any(
            m.seeding_target == waiting_target for m in lb_matches
        ):
            return Err(FFA_WINNERS_FINISHED_ERROR)

    # Every winners advance merges the latest losers round, so it must be in.
    if lb_matches:
        latest = max(m.round for m in lb_matches if m.round is not None)
        if any(
            m.confirmed_by is None for m in lb_matches if m.round == latest
        ):
            return Err(FFA_LOSERS_UNCONFIRMED_ERROR)

    # Collect existing LB survivors (top N from latest LB round).
    lb_result = _collect_lb_standings(tournament, all_matches, decisions)
    if lb_result.is_err():
        return Err(QUALIFICATION_TIE_ERROR)
    lb_survivors = lb_result.unwrap()

    # Players of earlier winners rounds who got a bye are still waiting.
    carried = _wb_dropped_pending_standings(
        tournament, all_matches, decisions, before_round=source
    )
    if carried.is_err():
        return Err(QUALIFICATION_TIE_ERROR)

    # Check GF trigger: total survivors <= group_size_max.
    total_survivors = (
        len(wb_advancing)
        + len(lb_survivors)
        + len(wb_dropped)
        + len(carried.unwrap())
    )
    eligible = _check_grand_final_trigger(tournament, total_survivors)

    # Merge dropped players with existing LB survivors for next LB round.
    lb_pool = _order_by_standing(
        [*wb_dropped, *lb_survivors, *carried.unwrap()]
    )
    wb_ordered = _order_by_standing(wb_advancing)
    return Ok(
        FfaAdvancePlan(
            pool=Bracket.WINNERS,
            round_number=source + 1,
            survivors=tuple(s.contestant_id for s in wb_ordered),
            bands={s.contestant_id: s.band for s in (*wb_ordered, *lb_pool)},
            grand_final_eligible=eligible,
            lb_pool=tuple(s.contestant_id for s in lb_pool),
            lb_round_number=(
                _next_lb_round_number(all_matches) if lb_pool else None
            ),
        )
    )


def _plan_ffa_lb(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    cut: int,
    decisions: qualification_domain.DecisionOrders,
    next_round: int | None,
) -> Result[FfaAdvancePlan, str]:
    """Losers bracket advancement: top survive, bottom eliminated."""
    lb_matches = [m for m in all_matches if m.bracket == Bracket.LOSERS]
    if not lb_matches:
        return Err('No Losers bracket matches found.')

    source = _source_round((m.round for m in lb_matches), next_round)
    lb_round_matches = tournament_repository.get_matches_for_round(
        tournament.id,
        source,
        bracket=Bracket.LOSERS,
    )
    if not lb_round_matches:
        return Err('No Losers bracket matches found.')

    # Validate all LB round matches confirmed.
    unconfirmed = [m for m in lb_round_matches if m.confirmed_by is None]
    if unconfirmed:
        return Err(
            f'{len(unconfirmed)} LB match(es) in round {source} '
            f'are not confirmed.'
        )

    # Select top N from each LB group; bottom eliminated entirely.
    lb_advancing: list[_Standing] = []
    for match in lb_round_matches:
        contestants = tournament_repository.get_contestants_for_match(match.id)
        split = _split_ffa_lobby(match, contestants, cut, decisions)
        if split.is_err():
            return Err(QUALIFICATION_TIE_ERROR)
        lb_advancing.extend(split.unwrap()[0])

    if not lb_advancing:
        return Err('No LB contestants qualified for advancement.')

    # Count what a Grand Final would hold: WB survivors, WB players
    # dropped but not yet in the LB, and these LB survivors.
    wb_survivors_result = _collect_wb_survivors(
        tournament, all_matches, decisions
    )
    wb_dropped_result = _collect_wb_dropped_pending(
        tournament, all_matches, decisions
    )
    if wb_survivors_result.is_err() or wb_dropped_result.is_err():
        return Err(QUALIFICATION_TIE_ERROR)

    total_survivors = (
        len(wb_survivors_result.unwrap())
        + len(wb_dropped_result.unwrap())
        + len(lb_advancing)
    )
    ordered = _order_by_standing(lb_advancing)
    return Ok(
        FfaAdvancePlan(
            pool=Bracket.LOSERS,
            round_number=source + 1,
            survivors=tuple(s.contestant_id for s in ordered),
            bands={s.contestant_id: s.band for s in ordered},
            grand_final_eligible=_check_grand_final_trigger(
                tournament, total_survivors
            ),
        )
    )


def _create_planned_ffa_rounds(
    tournament: Tournament,
    plan: FfaAdvancePlan,
    *,
    groups: Sequence[Sequence[str]] | None = None,
    initiator_id: UserID | None = None,
    seeding_target: str | None = None,
    log_waiting: bool = True,
    log_natural_shortfall: bool = True,
) -> Result[int, str]:
    """Create the round of a plan, and its losers round, without committing.

    *groups* are the lobbies of the plan's own round; without them they
    derive from the standing order. The losers round always derives and
    carries the same *seeding_target*. A losers pool of one has no round:
    it is logged as ``bracket-lobby-bye`` and the contestant carries over.
    """
    if is_lone_losers_round(plan):
        return Err(FFA_LONE_SURVIVOR_ERROR)
    if groups is None:
        groups = _ffa_lobbies(tournament, ffa_draft_survivors(plan))
    undersized = {
        (p.pool, p.round_number): p
        for p in ffa_undersized_pools(tournament, plan)
    }
    short = undersized.get((plan.pool, plan.round_number))
    if (
        short and short.natural_shortfall
        and any(len(group) < 2 for group in groups)
    ):
        return Err(FFA_LOBBY_BELOW_MINIMUM_ERROR)
    waiting = is_waiting_winners_round(plan)
    created = Ok(0) if waiting else _generate_ffa_round_impl(
        tournament.id,
        plan.round_number,
        list(plan.survivors),
        bracket=plan.pool,
        initiator_id=initiator_id,
        groups=groups,
        seeding_target=seeding_target,
        allow_undersized=(plan.pool, plan.round_number) in undersized,
    )
    if created.is_err():
        return created
    count = created.unwrap()

    byes = ffa_lobby_byes(plan)
    if plan.lb_pool and plan.lb_round_number is not None and not byes:
        created_lb = _generate_ffa_round_impl(
            tournament.id,
            plan.lb_round_number,
            list(plan.lb_pool),
            bracket=Bracket.LOSERS,
            initiator_id=initiator_id,
            groups=groups if waiting else _ffa_lobbies(tournament, plan.lb_pool),
            seeding_target=seeding_target,
            allow_undersized=(Bracket.LOSERS, plan.lb_round_number)
            in undersized,
        )
        if created_lb.is_err():
            return created_lb
        count += created_lb.unwrap()
    waiting_byes = (
        (LobbyBye(Bracket.WINNERS, plan.round_number, plan.survivors[0]),)
        if waiting and log_waiting else ()
    )
    for bye in (*byes, *waiting_byes):
        create_log_entry(
            'bracket-lobby-bye',
            tournament.id,
            initiator_id,
            data={
                'pool': ffa_pool_token(bye.pool),
                'round': bye.round_number,
                'contestant': bye.contestant_id,
            },
            commit=False,
        )
    for short in undersized.values():
        if short.natural_shortfall and not log_natural_shortfall:
            continue
        create_log_entry(
            'bracket-lobby-undersized',
            tournament.id,
            initiator_id,
            data={
                'pool': ffa_pool_token(short.pool),
                'round': short.round_number,
                'count': short.count,
                'lobbies': list(short.lobbies),
                'minimum': short.minimum,
                **(
                    {'reason': 'natural_shortfall'}
                    if short.natural_shortfall else {}
                ),
            },
            commit=False,
        )
    return Ok(count)


def is_untagged_winners_round(
    pool: Bracket | None, matches: Iterable[TournamentMatch]
) -> bool:
    """Tell whether a winners round was made without a draft.

    Its losers round carries no marker either, so it cannot be replaced.
    """
    return pool is Bracket.WINNERS and any(
        m.seeding_target is None for m in matches
    )


def ffa_round_seeding_target(pool: Bracket | None, round_number: int) -> str:
    """Return the seeding target of an FFA round, e.g. `ffa:WB:1`."""
    return f'ffa:{ffa_pool_token(pool)}:{round_number}'


def has_ffa_round(
    tournament_id: TournamentID, pool: Bracket | None, round_number: int
) -> bool:
    """Tell whether the pool already has matches in that round."""
    return bool(
        tournament_repository.get_matches_for_round(
            tournament_id, round_number, bracket=pool
        )
    )


def _generate_ffa_advance_impl(
    tournament_id: TournamentID,
    pool: Bracket | None,
    round_number: int,
    *,
    groups: Sequence[Sequence[str]],
    initiator_id: UserID | None = None,
) -> Result[GenerationOutcome, str]:
    """Generate one later FFA round from a draft without committing.

    Replaces the round, and the losers round a winners round came with,
    while none of them has a confirmed result; the survivors must still
    be the ones it was made for. Every created match carries the
    round's seeding target. The caller owns commit and dispatch.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    target = ffa_round_seeding_target(pool, round_number)
    existing = tournament_repository.get_matches_for_round(
        tournament_id, round_number, bracket=pool
    )
    to_delete = {m.id: m for m in existing}
    for m in tournament_repository.get_matches_for_seeding_target(
        tournament_id, target
    ):
        to_delete[m.id] = m
    if is_untagged_winners_round(pool, existing):
        return Err(FFA_UNTAGGED_WB_ERROR)
    if any(m.confirmed_by is not None for m in to_delete.values()):
        return Err(FFA_ROUND_LOCKED_ERROR)

    placed = {
        contestant_id(c)
        for m in existing
        for c in tournament_repository.get_contestants_for_match(m.id)
    }
    # The plan reads the matches, so the round goes before it is planned.
    deleted_events = _delete_matches_flush(
        tournament_id, list(to_delete.values())
    )

    plan_result = plan_ffa_advance(tournament, pool, next_round=round_number)
    if plan_result.is_err():
        return Err(plan_result.unwrap_err())
    plan = plan_result.unwrap()
    if plan.grand_final_eligible:
        return Err(FFA_GRAND_FINAL_ERROR)
    if existing and placed != set(plan.survivors):
        return Err(LAYOUT_ROSTER_ERROR)

    created = _create_planned_ffa_rounds(
        tournament,
        plan,
        groups=groups,
        initiator_id=initiator_id,
        seeding_target=target,
        log_waiting=not (
            pool is Bracket.WINNERS and not existing and to_delete
        ),
        log_natural_shortfall=not bool(to_delete),
    )
    if created.is_err():
        return Err(created.unwrap_err())

    now = datetime.now(UTC)
    created_ids = [
        m.id
        for m in tournament_repository.get_matches_for_round(
            tournament_id, round_number, bracket=pool
        )
    ]
    if plan.lb_round_number is not None and plan.lb_pool:
        created_ids.extend(
            m.id
            for m in tournament_repository.get_matches_for_round(
                tournament_id, plan.lb_round_number, bracket=Bracket.LOSERS
            )
        )
    return Ok(
        GenerationOutcome(
            count=created.unwrap(),
            created_events=[
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match_id,
                )
                for match_id in created_ids
            ],
            deleted_events=deleted_events,
            ready_match_ids=frozenset(created_ids),
            occurred_at=now,
        )
    )


def _delete_matches_flush(
    tournament_id: TournamentID, matches: Iterable[TournamentMatch]
) -> list[MatchDeletedEvent]:
    """Delete matches without links, with their children (flush only)."""
    matches = list(matches)
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament_repository.lock_matches_for_update([match.id for match in matches])
    now = datetime.now(UTC)
    events = []
    for match in matches:
        tournament_repository.delete_comments_for_match_flush(match.id)
        _delete_contestants_for_match_flush(match.id)
        tournament_repository.delete_match_flush(match.id)
        events.append(
            MatchDeletedEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=tournament_id,
                match_id=match.id,
            )
        )
    return events


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
    decisions: qualification_domain.DecisionOrders | None = None,
) -> Result[list[str], list[str]]:
    """Collect surviving contestant IDs from the latest LB round.

    Survivors = top ``advancement_count`` from each LB group.
    Returns an empty list when no LB rounds exist yet, and
    ``Err(tied_ids)`` on an undecided tie at the cut of a confirmed match.
    """
    result = _collect_lb_standings(tournament, all_matches, decisions)
    if result.is_err():
        return Err(result.unwrap_err())
    return Ok([s.contestant_id for s in result.unwrap()])


def _collect_lb_standings(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    decisions: qualification_domain.DecisionOrders | None = None,
) -> Result[list[_Standing], list[str]]:
    lb_matches = [m for m in all_matches if m.bracket == Bracket.LOSERS]
    if not lb_matches:
        return Ok([])

    latest_lb_round = max(m.round for m in lb_matches if m.round is not None)
    lb_round_matches = tournament_repository.get_matches_for_round(
        tournament.id,
        latest_lb_round,
        bracket=Bracket.LOSERS,
    )

    return _round_standings(
        lb_round_matches,
        tournament.advancement_count or 1,
        _decisions_or_stored(tournament, decisions),
    )


def _losers_entrant_ids(all_matches: Iterable[TournamentMatch]) -> set[str]:
    """Return everyone with an entry in a losers lobby."""
    return {
        contestant_id(c)
        for m in all_matches
        if m.bracket == Bracket.LOSERS
        for c in tournament_repository.get_contestants_for_match(m.id)
    }


def _collect_wb_survivors(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    decisions: qualification_domain.DecisionOrders | None = None,
) -> Result[list[str], list[str]]:
    """Collect surviving contestant IDs from the latest WB round.

    Returns ``Err(tied_ids)`` on an undecided tie at the cut of a
    confirmed match.
    """
    wb_matches = [m for m in all_matches if m.bracket == Bracket.WINNERS]
    if not wb_matches:
        return Ok([])

    latest_wb_round = max(m.round for m in wb_matches if m.round is not None)
    wb_round_matches = tournament_repository.get_matches_for_round(
        tournament.id,
        latest_wb_round,
        bracket=Bracket.WINNERS,
    )

    in_lb = _losers_entrant_ids(all_matches)
    entries = {
        m.id: tournament_repository.get_contestants_for_match(m.id)
        for m in wb_round_matches
    }
    if any(contestant_id(c) in in_lb for es in entries.values() for c in es):
        # The round's drops already play in the losers bracket: read its
        # survivors from the entries, so a removal cannot re-rank it.
        active = active_contestant_ids(tournament.id)
        return Ok(
            [
                contestant_id(c)
                for m in sorted(
                    wb_round_matches, key=lambda m: m.group_order or 0
                )
                for c in sorted(
                    entries[m.id],
                    key=lambda c: (
                        -(c.points or 0),
                        c.placement is None,
                        c.placement or 0,
                        contestant_id(c),
                    ),
                )
                if contestant_id(c) in active and contestant_id(c) not in in_lb
            ]
        )

    return _collect_round_survivors(
        wb_round_matches,
        tournament.advancement_count or 1,
        _decisions_or_stored(tournament, decisions),
    )


def _decisions_or_stored(
    tournament: Tournament,
    decisions: qualification_domain.DecisionOrders | None,
) -> qualification_domain.DecisionOrders:
    if decisions is not None:
        return decisions
    return ffa_decisions(tournament.id)


def _collect_round_survivors(
    round_matches: list[TournamentMatch],
    advancement_count: int,
    decisions: qualification_domain.DecisionOrders,
) -> Result[list[str], list[str]]:
    """Collect the contestants still alive after a round.

    An unconfirmed match keeps all its contestants alive.
    """
    result = _round_standings(round_matches, advancement_count, decisions)
    if result.is_err():
        return Err(result.unwrap_err())
    return Ok([s.contestant_id for s in result.unwrap()])


def _round_standings(
    round_matches: list[TournamentMatch],
    cut: int,
    decisions: qualification_domain.DecisionOrders,
    *,
    contestants_by_match: Mapping[
        TournamentMatchID, list[TournamentMatchToContestant]
    ] | None = None,
    active_ids: Collection[str] | None = None,
) -> Result[list[_Standing], list[str]]:
    """Rank the contestants still alive after a round, lobby by lobby."""
    alive: list[_Standing] = []

    for match in round_matches:
        contestants = (
            tournament_repository.get_contestants_for_match(match.id)
            if contestants_by_match is None
            else contestants_by_match[match.id]
        )
        if match.confirmed_by is None:
            alive.extend(
                _Standing(
                    contestant_id=contestant_id(c),
                    band=0,
                    points=0,
                    lobby=match.group_order or 0,
                    place=place,
                )
                for place, c in enumerate(contestants)
            )
            continue

        split = _split_ffa_lobby(
            match, contestants, cut, decisions, active_ids=active_ids,
            lobbies_in_round=(
                len(round_matches) if contestants_by_match is not None else None
            ),
        )
        if split.is_err():
            return Err(split.unwrap_err())
        alive.extend(split.unwrap()[0])

    return Ok(alive)


def _collect_wb_dropped_pending(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    decisions: qualification_domain.DecisionOrders | None = None,
) -> Result[list[str], list[str]]:
    """Collect WB players dropped in a confirmed WB round, not yet in the LB.

    That is the players of the latest round whose losers round is not
    made yet, and the players of any round who had a bye. Returns
    ``Err(tied_ids)`` on an undecided tie at the cut of a confirmed match.
    """
    result = _wb_dropped_pending_standings(tournament, all_matches, decisions)
    if result.is_err():
        return Err(result.unwrap_err())
    return Ok([s.contestant_id for s in result.unwrap()])


def _wb_dropped_pending_standings(
    tournament: Tournament,
    all_matches: list[TournamentMatch],
    decisions: qualification_domain.DecisionOrders | None = None,
    *,
    before_round: int | None = None,
) -> Result[list[_Standing], list[str]]:
    """Rank the active WB players dropped but in no LB match.

    The latest round is judged by its ranking. In an earlier round, a
    player who is in no later WB round and in no LB match had a bye; that
    is read from the entries, so a later removal cannot re-rank the
    round and lose the player. *before_round* treats every round before
    it as earlier, and leaves out the rest.
    """
    wb_matches = [
        m
        for m in all_matches
        if m.bracket == Bracket.WINNERS and m.round is not None
    ]
    if not wb_matches:
        return Ok([])
    latest = max(m.round for m in wb_matches if m.round is not None)
    cutoff = latest if before_round is None else before_round

    in_lb = _losers_entrant_ids(all_matches)

    advancement_count = tournament.advancement_count or 1
    stored = _decisions_or_stored(tournament, decisions)
    contestants_of = {
        m.id: tournament_repository.get_contestants_for_match(m.id)
        for m in wb_matches
    }
    dropped: list[_Standing] = []

    if before_round is None:
        for match in wb_matches:
            if match.round != latest or match.confirmed_by is None:
                continue
            if any(contestant_id(c) in in_lb for c in contestants_of[match.id]):
                # Merged round: every drop plays in the losers bracket, so
                # splitting it can only raise a tie nobody has to decide.
                continue
            split = _split_ffa_lobby(
                match, contestants_of[match.id], advancement_count, stored
            )
            if split.is_err():
                return Err(split.unwrap_err())
            dropped.extend(
                s for s in split.unwrap()[1] if s.contestant_id not in in_lb
            )

    earlier = [m for m in wb_matches if (m.round or 0) < cutoff]
    if not earlier:
        return Ok(dropped)

    active = active_contestant_ids(tournament.id)
    for match in earlier:
        later = {
            contestant_id(c)
            for other in wb_matches
            if (other.round or 0) > (match.round or 0)
            for c in contestants_of[other.id]
        }
        left = [
            c
            for c in contestants_of[match.id]
            if contestant_id(c) in active
            and contestant_id(c) not in in_lb
            and contestant_id(c) not in later
        ]
        if not left:
            continue
        split = _split_ffa_lobby(
            match, contestants_of[match.id], advancement_count, stored
        )
        known = (
            {s.contestant_id: s for part in split.unwrap() for s in part}
            if split.is_ok()
            else {}
        )
        dropped.extend(
            known.get(contestant_id(c))
            or _Standing(
                contestant_id=contestant_id(c),
                band=0,
                points=c.points or 0,
                lobby=match.group_order or 0,
                place=c.placement or 0,
            )
            for c in left
        )

    return Ok(dropped)


def _ranked_ids_with_points(
    contestants: list[TournamentMatchToContestant],
) -> list[tuple[str, int]]:
    """Return contestant IDs with points, highest points first."""
    return sorted(
        ((contestant_id(c), c.points or 0) for c in contestants),
        key=lambda entry: entry[1],
        reverse=True,
    )


def split_at_cut(
    sorted_ids_with_points: list[tuple[str, int]],
    cut: int,
) -> Result[tuple[list[str], list[str]], list[str]]:
    """Split ranked entries into advancing and dropped at `cut`.

    Returns ``Err(tied_ids)`` when the entries on both sides of the cut
    have equal points.
    """
    if len(sorted_ids_with_points) <= cut:
        return Ok(([cid for cid, _points in sorted_ids_with_points], []))

    cut_points = sorted_ids_with_points[cut - 1][1]
    if cut_points == sorted_ids_with_points[cut][1]:
        return Err(
            [
                cid
                for cid, points in sorted_ids_with_points
                if points == cut_points
            ]
        )

    return Ok(
        (
            [cid for cid, _points in sorted_ids_with_points[:cut]],
            [cid for cid, _points in sorted_ids_with_points[cut:]],
        )
    )


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


def _refuse_ffa_grand_final(reason: str) -> Result[int, str]:
    """Release the generation transaction without producing a Grand Final."""
    tournament_repository.rollback_session()
    return Err(reason)


@dataclass(frozen=True)
class _FfaGrandFinalInputs:
    tournament: Tournament
    phase: int
    all_matches: list[TournamentMatch]
    wb_survivors: list[str]
    wb_dropped: list[str]
    lb_survivors: list[str]


def _check_ffa_grand_final(
    tournament_id: TournamentID,
) -> Result[_FfaGrandFinalInputs, str]:
    """Run every Grand Final refusal check; read-only, no lock, no writes."""
    tournament = tournament_repository.get_tournament(tournament_id)

    phase = _ffa_phase(tournament)
    if phase is None:
        return Err('Tournament game format is not FREE_FOR_ALL.')

    if _ffa_elimination_mode(tournament) != EliminationMode.DOUBLE_ELIMINATION:
        return Err('Grand Final is only for double elimination tournaments.')
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return Err('The tournament must be ongoing to advance an FFA round.')
    if _has_playoffs(tournament) and tournament.playoff_released_at is None:
        return Err('The playoffs are not released yet.')

    all_matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament_id
    )
    all_matches = [m for m in all_matches if m.phase == phase]

    # Reject if Grand Final already exists.
    existing_gf = [
        m for m in all_matches if m.bracket == Bracket.GRAND_FINAL
    ]
    if existing_gf:
        return Err('Grand Final has already been generated.')
    if any(
        m.confirmed_by is None
        for m in all_matches
        if m.bracket in (Bracket.WINNERS, Bracket.LOSERS)
    ):
        return Err('Bracket matches are not confirmed.')

    # Collect all survivors from both pools.
    wb_survivors_result = _collect_wb_survivors(tournament, all_matches)
    wb_dropped_result = _collect_wb_dropped_pending(tournament, all_matches)
    lb_survivors_result = _collect_lb_survivors(tournament, all_matches)
    if (
        wb_survivors_result.is_err()
        or wb_dropped_result.is_err()
        or lb_survivors_result.is_err()
    ):
        return Err(QUALIFICATION_TIE_ERROR)

    wb_survivors = wb_survivors_result.unwrap()
    wb_dropped = wb_dropped_result.unwrap()
    lb_survivors = lb_survivors_result.unwrap()
    total = len(wb_survivors) + len(wb_dropped) + len(lb_survivors)

    if total < 2:
        return Err('Need at least 2 survivors for Grand Final.')
    if not _check_grand_final_trigger(tournament, total):
        return Err(FFA_GRAND_FINAL_NOT_ELIGIBLE_ERROR)

    return Ok(
        _FfaGrandFinalInputs(
            tournament=tournament,
            phase=phase,
            all_matches=all_matches,
            wb_survivors=wb_survivors,
            wb_dropped=wb_dropped,
            lb_survivors=lb_survivors,
        )
    )


def ffa_grand_final_gate(tournament_id: TournamentID) -> Result[int, str]:
    """Return the Grand Final size, or why it cannot be generated yet.

    Read-only: takes no lock and writes nothing.
    """
    checked = _check_ffa_grand_final(tournament_id)
    if checked.is_err():
        return Err(checked.unwrap_err())
    inputs = checked.unwrap()
    return Ok(
        len(inputs.wb_survivors)
        + len(inputs.wb_dropped)
        + len(inputs.lb_survivors)
    )


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

    checked = _check_ffa_grand_final(tournament_id)
    if checked.is_err():
        return _refuse_ffa_grand_final(checked.unwrap_err())
    inputs = checked.unwrap()
    tournament = inputs.tournament
    all_matches = inputs.all_matches
    wb_survivors = inputs.wb_survivors
    wb_dropped = inputs.wb_dropped
    lb_survivors = inputs.lb_survivors
    all_survivors = wb_survivors + wb_dropped + lb_survivors
    is_team = tournament.contestant_type == ContestantType.TEAM

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
    wb_survivor_set = set(wb_survivors) | set(wb_dropped)
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
    # Members go in contestant-ID order, never in seed order.
    member_ids = sorted(ordered_survivors)
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
        phase=_ffa_phase(tournament) or 1,
    )
    tournament_repository.create_match(gf_match)

    for cid in member_ids:
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
        _create_match_contestant_flush(contestant)

    create_log_entry(
        'bracket-generated',
        tournament_id,
        initiator_id,
        data={'target': FFA_GRAND_FINAL_TARGET, 'contestants': member_ids},
        commit=False,
    )
    tournament_repository.commit_session()
    dispatch_generation_events(
        tournament_id,
        GenerationOutcome(
            count=1,
            created_events=[
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match_id,
                )
            ],
            deleted_events=[],
            ready_match_ids=frozenset({match_id}),
            occurred_at=now,
        ),
    )
    return Ok(1)
