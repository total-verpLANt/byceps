"""
byceps.services.lan_tournament.tournament_seeding_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC
from enum import Enum
from functools import cache, cached_property
import re
from typing import Any, TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from byceps.services.user import user_service
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    seed_code,
    tournament_domain_service,
    tournament_match_service,
    tournament_repository,
    tournament_seeding_domain_service as domain,
    tournament_qualification_domain_service as qualification_domain,
    tournament_seeding_repository,
)
from .models.bracket import Bracket
from .models.contestant_type import ContestantType
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.seeding import DecodedSeedCode, SeedingFormat, SeedingState
from .models.tournament import Tournament, TournamentID
from .models.tournament_match import TournamentMatch
from .models.tournament_seeding import (
    RosterEntry,
    TournamentSeeding,
    TournamentSeedingID,
)
from .models.tournament_status import TournamentStatus
from .tournament_domain_service import (
    contestant_id,
    derive_contestant_type,
    elimination_mode_for_phase,
    FfaDeadEnd,
    game_format_for_phase,
)
from .tournament_log_service import create_log_entry


if TYPE_CHECKING:
    from .tournament_qualification_service import (
        PhaseTwoProgress,
        QualificationState,
    )


INITIAL_TARGET = 'initial'
PLAYOFF_TARGET = 'playoff'

_MAX_PLAYOFF_TIERS = 4
_MIN_TIERS = 2

_FFA_TARGET = re.compile(r'ffa:(SE|WB|LB):([0-9]{1,4})')
_ELIMINATION_FORMATS = (
    SeedingFormat.SINGLE_ELIMINATION,
    SeedingFormat.DOUBLE_ELIMINATION,
)
_GROUP_SCOPE = re.compile(r'group:([0-9]+)')
_FFA_POOLS = {'SE': None, 'WB': Bracket.WINNERS, 'LB': Bracket.LOSERS}

MAX_CONTESTANTS = 1024

_LOCKED_STATUSES = frozenset(
    {
        TournamentStatus.ONGOING,
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    }
)

ERR_NOT_OPEN_YET = 'The seeding opens once registration is closed.'
ERR_LOCKED = 'The seeding is locked once the tournament has started.'
ERR_NO_SEEDING = 'There is no seeding for this tournament.'
ERR_UNKNOWN_TARGET = 'Unknown seeding target.'
ERR_NO_FORMAT = 'This tournament mode has no seeding.'
ERR_TOO_FEW = 'Need at least 2 contestants for a seeding.'
ERR_TOO_MANY = 'A seeding supports at most 1024 contestants.'
ERR_STALE = 'The roster changed since the seed code was made. Re-seed first.'
ERR_NO_TIERS = 'Tiers are only available for free-for-all.'
ERR_TIER_COUNT = 'Choose between 2 and 4 tiers.'
ERR_INVALID_CHANGE = 'That change to the seeding is not possible.'
ERR_OTHER_MODE = 'This code belongs to a different tournament mode.'
ERR_STORED_CODE = 'The stored seed code is damaged.'
ERR_CONFLICT = 'The seeding was changed by another orga. Reload the page.'
ERR_RESULTS_EXIST = (
    'A match already has a confirmed result. Take it back before you regenerate.'
)
GENERATION_UNCHANGED = 'unchanged'
MSG_UNCHANGED = (
    'Nothing to regenerate: the matches already follow this seed code.'
)
ERR_PROBLEMS = 'The seeding has problems. Fix them before generating.'
ERR_ROSTER_CHANGED = (
    'The roster changed after generation. Re-seed and regenerate first.'
)
ERR_STRUCTURE_CHANGED = (
    'The tournament structure changed since the seeding was drawn. '
    'Re-seed first.'
)
ERR_RANKS_CHANGED = (
    'The qualification changed since the playoff draft was made. '
    'Re-prefill it from the qualification first.'
)
ERR_STRUCTURE_CHANGED_AFTER_GENERATION = (
    'The tournament structure changed after generation. '
    'Regenerate on the seeding board first.'
)

ERR_PLAYOFF_NOT_RUNNING = (
    'The playoffs can only be seeded while the tournament is running.'
)
ERR_PLAYOFF_NOT_RELEASED = 'The playoffs are not released yet.'
ERR_PLAYOFF_LOCKED = (
    'The playoff seeding is locked once a playoff match has a result.'
)
ERR_PLAYOFF_NOT_READY = 'The qualification is not ready yet.'
ERR_FFA_NOT_RUNNING = (
    'A round can only be seeded while the tournament is running.'
)

ERR_NOTHING_TO_SEPARATE = (
    'No first-round match pairs two players from the same group.'
)
ERR_SEPARATE_STUCK = 'No swap found that separates all pairs.'

PROBLEM_EMPTY_TEAM = 'Team %(name)s has no members. Remove or fill it first.'
PROBLEM_LOBBY_BELOW_MIN = (
    'Lobby sizes %(sizes)s are below the minimum of %(minimum)s.'
)
PROBLEM_FFA_STALLS = tournament_domain_service.FFA_STALLS_MSGID
PROBLEM_FFA_NO_PROGRESS = tournament_domain_service.FFA_NO_PROGRESS_MSGID

NOTICE_FEWER_GROUPS = (
    'The roster gives %(groups)s groups instead of the configured '
    '%(configured)s.'
)
NOTICE_FEWER_QUALIFIERS = (
    'Only %(qualifiers)s contestants qualify instead of the configured '
    '%(configured)s.'
)
NOTICE_KNOCKOUT_FALLBACK = (
    'With fewer than 4 qualifiers the playoffs run as single knockout.'
)
NOTICE_SMALL_PLAYOFF_LOBBIES = (
    'Too few qualifiers for full lobbies: lobby sizes %(sizes)s, below the '
    'minimum of %(minimum)s. The playoffs start with them anyway.'
)


@dataclass(frozen=True)
class Swap:
    p: int
    q: int


@dataclass(frozen=True)
class MoveTier:
    contestant_id: str
    tier: int
    ref_id: str | None = None
    after: bool = False


@dataclass(frozen=True)
class SetTierCount:
    n: int


@dataclass(frozen=True)
class Redraw:
    pass


@dataclass(frozen=True)
class ResetFixes:
    pass


@dataclass(frozen=True)
class Replay:
    code: str


@dataclass(frozen=True)
class ReseedKeepTiers:
    pass


@dataclass(frozen=True)
class Separate:
    pass


@dataclass(frozen=True)
class Reprefill:
    pass


SeedingAction = (
    Swap
    | MoveTier
    | SetTierCount
    | Redraw
    | ResetFixes
    | Replay
    | ReseedKeepTiers
    | Separate
    | Reprefill
)


class GenerationStatus(Enum):
    NOT_AVAILABLE = 'not_available'
    NOT_GENERATED = 'not_generated'
    MATCHES = 'matches'
    DIFFERS = 'differs'
    LOCKED = 'locked'


@dataclass(frozen=True, kw_only=True)
class SeedingBoard:
    """Everything the seeding screen shows; for orgas only.

    `problems` holds raw msgids; `problem_params[i]` fills the
    placeholders of `problems[i]`. `labels` names every entrant of
    `state.roster`, leavers included. `stale_leavers` and
    `stale_joiners` are display labels; the `*_ids` fields are the IDs.
    `new_entrant_ids` are the re-seeded joiners not generated yet.
    `origin_labels` names where a playoff entrant qualified from (`A1`);
    `same_group_matches` are the 1-based first-round matches pairing two
    entrants of one group. Both are empty outside a group playoff.
    `stale_structure` marks a draft drawn for another tournament structure
    (format or its parameter); `state` is then the draft carried over to
    the current one, or the qualification prefill when it cannot carry over.
    `stale_ranks` marks a playoff draft whose qualifiers kept their places
    in the roster but changed their order. `undersized` lists the pools of an FFA round whose lobbies stay
    below the minimum size because contestants were removed. `byes` names
    the contestants of a lone losers pool, who get no lobby.
    `prefilled` is set when a playoff draft still has the seed order and
    the tiers the qualification prefilled.
    """

    tournament_id: TournamentID
    target: str
    state: SeedingState
    version: int
    code: str | None
    stale: bool
    labels: Mapping[str, str]
    stale_leavers: tuple[str, ...]
    stale_joiners: tuple[str, ...]
    stale_leaver_ids: tuple[str, ...]
    stale_joiner_ids: tuple[str, ...]
    new_entrant_ids: tuple[str, ...]
    problems: tuple[str, ...]
    problem_params: tuple[Mapping[str, Any], ...]
    balance: domain.Balance | None
    fix_count: int
    pure_draw: bool
    generated_code: str | None
    generation: GenerationStatus
    locked_reason: str | None
    regenerate_refusal: str | None = None
    origin_labels: Mapping[str, str] = field(default_factory=dict)
    same_group_matches: tuple[int, ...] = ()
    stale_structure: bool = False
    stale_ranks: bool = False
    undersized: tuple[tournament_match_service.UndersizedPool, ...] = ()
    byes: tuple[str, ...] = ()
    waiting_winners: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    notice_params: tuple[Mapping[str, Any], ...] = ()
    prefilled: bool = False


@dataclass(frozen=True)
class _Roster:
    ids: tuple[str, ...]
    labels: Mapping[str, str]
    empty_team_names: tuple[str, ...]
    origins: Mapping[str, tuple[str, int]] = field(default_factory=dict)
    prefill_order: tuple[str, ...] = ()


def get_board(
    tournament_id: TournamentID,
    target: str = INITIAL_TARGET,
    *,
    initiator_id: UserID | None = None,
    qualification: 'QualificationState | None' = None,
) -> Result[SeedingBoard, str]:
    """Return the seeding board, drawing the initial placement on first open.

    An FFA round target has a board once `prepare_ffa_round_draft` made it,
    the playoff target once `ensure_playoff_draft` did. A caller that holds
    the qualification can pass it, so the playoff board does not compute it
    again.
    """
    has_own_draft = (
        _parse_ffa_target(target) is not None or target == PLAYOFF_TARGET
    )
    if target != INITIAL_TARGET and not has_own_draft:
        return Err(ERR_UNKNOWN_TARGET)

    tournament = tournament_repository.get_tournament(tournament_id)
    view = _TargetView(tournament, target, qualification=qualification)
    if has_own_draft:
        seeding = tournament_seeding_repository.find_seeding(
            tournament_id, target
        )
        if seeding is None:
            return Err(ERR_NO_SEEDING)
        return _board(view, seeding)
    status = tournament.tournament_status
    if status in _LOCKED_STATUSES:
        seeding = tournament_seeding_repository.find_seeding(
            tournament_id, target
        )
        if seeding is None:
            return Err(ERR_NO_SEEDING)
        return _board(view, seeding)
    if status is not TournamentStatus.REGISTRATION_CLOSED:
        return Err(ERR_NOT_OPEN_YET)

    seeding = tournament_seeding_repository.find_seeding(tournament_id, target)
    if seeding is None:
        created = _create_draft(tournament_id, target, initiator_id)
        if created.is_err():
            return Err(created.unwrap_err())
        seeding = created.unwrap()
    return _board(view, seeding)


def peek_initial_generation_status(
    tournament_id: TournamentID,
) -> GenerationStatus | None:
    """Return the initial draft's generation status, or `None` without a draft.

    Read-only: it takes no lock and never draws a draft.
    """
    seeding = tournament_seeding_repository.find_seeding(
        tournament_id, INITIAL_TARGET
    )
    if seeding is None:
        return None
    tournament = tournament_repository.get_tournament(tournament_id)
    return _generation_status(
        _TargetView(tournament, INITIAL_TARGET), seeding
    )


def apply_action(
    tournament_id: TournamentID,
    target: str,
    action: SeedingAction,
    *,
    expected_version: int,
    initiator_id: UserID,
) -> Result[SeedingBoard, str]:
    """Apply one orga action to the draft and return the new board."""
    if (
        target not in (INITIAL_TARGET, PLAYOFF_TARGET)
        and _parse_ffa_target(target) is None
    ):
        return Err(ERR_UNKNOWN_TARGET)

    replay = None
    if isinstance(action, Replay):
        # Decode before the lock: the fold is the costly part.
        decoded = seed_code.decode_seed_code(action.code)
        if decoded.is_err():
            return Err(decoded.unwrap_err())
        replay = decoded.unwrap()

    tournament_repository.lock_tournament_for_update(tournament_id)
    result = _apply_locked(
        tournament_id, target, action, expected_version, initiator_id, replay
    )
    if result.is_err():
        tournament_repository.rollback_session()
        return result

    tournament_repository.commit_session()
    return result


def generate_from_seeding(
    tournament_id: TournamentID,
    target: str = INITIAL_TARGET,
    *,
    expected_version: int,
    initiator_id: UserID,
) -> Result[int | str, str]:
    """Generate the bracket, groups or lobbies from the draft.

    The only public entry for initial generation, for regenerating the
    playoffs after their release, and for creating a later FFA round from
    its draft. Generation, the draft's
    `generated_seed_code` and version and the audit entry commit together;
    the signals follow the commit. A draft whose code the matches already
    follow generates nothing and returns `GENERATION_UNCHANGED`.
    """
    if (
        target not in (INITIAL_TARGET, PLAYOFF_TARGET)
        and _parse_ffa_target(target) is None
    ):
        return Err(ERR_UNKNOWN_TARGET)

    try:
        tournament_repository.lock_tournament_for_update(tournament_id)
        result = _generate_locked(
            tournament_id,
            target,
            expected_version,
            initiator_id,
            require_released=True,
            skip_unchanged=True,
        )
        if result.is_err():
            tournament_repository.rollback_session()
            return Err(result.unwrap_err())

        outcome = result.unwrap()
        if outcome.unchanged:
            tournament_repository.rollback_session()
            return Ok(GENERATION_UNCHANGED)

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    tournament_match_service.dispatch_generation_events(tournament_id, outcome)
    if outcome.completed_event is not None:
        return Ok('completed')
    return Ok(outcome.count)


def ensure_playoff_draft(
    tournament_id: TournamentID, initiator_id: UserID | None = None
) -> Result[SeedingBoard, str]:
    """Return the playoff seeding board, drawing its prefill if missing.

    The roster is the qualifiers; the draft turns stale when they change.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    result = _ensure_playoff_board(tournament_id, initiator_id)
    if result.is_err():
        tournament_repository.rollback_session()
        return result

    tournament_repository.commit_session()
    return result


def prepare_ffa_round_draft(
    tournament_id: TournamentID,
    *,
    pool: Bracket | None = None,
    initiator_id: UserID,
) -> Result[str, str]:
    """Draft the lobbies of the next FFA round and return its target key.

    The survivors are ranked lobby by lobby; a tie across the cut needs an
    orga decision first. The tiers are the survivors' rank bands, so the
    derived lobbies mix them. An existing draft is kept while its survivors
    stay the same, and rebuilt when they changed.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    result = _prepare_ffa_draft_locked(tournament_id, pool, initiator_id)
    if result.is_err():
        tournament_repository.rollback_session()
        return result

    completed_event = None
    if result.unwrap() == 'completed':
        tournament = tournament_repository.get_tournament(tournament_id)
        completed_event = tournament_match_service.TournamentCompletedEvent(
            occurred_at=datetime.now(UTC), initiator=None,
            tournament_id=tournament_id,
            winner_team_id=tournament.winner_team_id,
            winner_participant_id=tournament.winner_participant_id,
        )
    tournament_repository.commit_session()
    if completed_event is not None:
        tournament_match_service.tournament_completed.send(
            None, event=completed_event
        )
    return result


def ffa_round_target(pool: Bracket | None, round_number: int) -> str:
    """Return the seeding target of a round of the pool."""
    return f'ffa:{tournament_match_service.ffa_pool_token(pool)}:{round_number}'


def stage_playoff_draft(
    tournament_id: TournamentID, initiator_id: UserID | None = None
) -> Result[TournamentSeeding, str]:
    """Create the prefilled playoff draft, or bring an untouched one up to date.

    An existing draft nobody changed is rewritten when the qualification or
    the playoff structure moved on; a touched one is left alone. Flush
    only: the caller holds the tournament lock and owns commit and
    rollback.
    """
    tournament = tournament_repository.get_tournament(tournament_id)
    return _stage_playoff_draft(
        _TargetView(tournament, PLAYOFF_TARGET), initiator_id
    )


def _stage_playoff_draft(
    view: '_TargetView', initiator_id: UserID | None
) -> Result[TournamentSeeding, str]:
    tournament = view.tournament
    tournament_id = tournament.id
    existing = tournament_seeding_repository.find_seeding(
        tournament_id, PLAYOFF_TARGET
    )
    if existing is not None:
        return _refresh_untouched_prefill(view, existing)

    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return Err(ERR_PLAYOFF_NOT_RUNNING)

    roster_result = view.roster
    if roster_result.is_err():
        return Err(roster_result.unwrap_err())
    roster = roster_result.unwrap()

    format_result = _format_for(tournament, roster, PLAYOFF_TARGET)
    if format_result.is_err():
        return Err(format_result.unwrap_err())
    fmt, param = format_result.unwrap()

    state = _prefill_playoff_state(
        fmt,
        param,
        roster.prefill_order,
        _origin_scopes(roster),
        draw_seed=domain.new_draw_seed(),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    seeding = TournamentSeeding(
        id=TournamentSeedingID(generate_uuid7()),
        tournament_id=tournament_id,
        target=PLAYOFF_TARGET,
        seed_code=_encode(state),
        roster_snapshot=_snapshot(state, roster.labels, prefill=roster),
        version=1,
        generated_seed_code=None,
        generated_at=None,
        updated_by=initiator_id,
        created_at=now,
        updated_at=now,
    )
    tournament_seeding_repository.create_seeding(seeding)
    create_log_entry(
        'seeding-drawn',
        tournament_id,
        initiator_id,
        data={
            'target': PLAYOFF_TARGET,
            'seed_code': seeding.seed_code,
            'version': seeding.version,
        },
        commit=False,
    )
    return Ok(seeding)


def _refresh_untouched_prefill(
    view: '_TargetView', existing: TournamentSeeding
) -> Result[TournamentSeeding, str]:
    """Rewrite an untouched playoff draft that fell behind, else keep it.

    Behind means the qualification order or the playoff structure changed
    since the draft was last prefilled. The draft stays as it is once the
    playoffs are released, while the tournament is not running, or while
    the qualification is not ready. The rewrite is audited with no actor
    and bumps the version, so an open tab gets a conflict.
    """
    tournament = view.tournament
    if (
        tournament.playoff_released_at is not None
        or tournament.tournament_status is not TournamentStatus.ONGOING
    ):
        return Ok(existing)
    roster_result = view.roster
    if roster_result.is_err():
        return Ok(existing)
    roster = roster_result.unwrap()
    format_result = _format_for(tournament, roster, PLAYOFF_TARGET)
    if format_result.is_err():
        return Ok(existing)
    fmt, param = format_result.unwrap()
    decoded_result = seed_code.decode_seed_code(existing.seed_code)
    if decoded_result.is_err():
        return Ok(existing)
    decoded = decoded_result.unwrap()

    if (decoded.format, decoded.param) == (fmt, param) and (
        _prefill_baseline(existing) == roster.prefill_order
    ):
        return Ok(existing)
    if not _is_untouched_prefill(existing, decoded):
        return Ok(existing)

    state = _prefill_playoff_state(
        fmt,
        param,
        roster.prefill_order,
        _origin_scopes(roster),
        draw_seed=decoded.draw_seed,
    )
    updated_result = tournament_seeding_repository.update_seeding_code(
        existing.id,
        seed_code=_encode(state),
        expected_version=existing.version,
        roster_snapshot=_snapshot(state, roster.labels, prefill=roster),
        updated_by=None,
        now=datetime.now(UTC).replace(tzinfo=None),
    )
    if updated_result.is_err():
        return Err(updated_result.unwrap_err())
    updated = updated_result.unwrap()
    create_log_entry(
        'seeding-reprefilled',
        tournament.id,
        None,
        data={
            'target': PLAYOFF_TARGET,
            'previous_seed_code': existing.seed_code,
            'seed_code': updated.seed_code,
            'version': updated.version,
            'automatic': True,
        },
        commit=False,
    )
    return Ok(updated)


def _prefill_baseline(seeding: TournamentSeeding) -> tuple[str, ...] | None:
    """Return the qualification order the draft was last prefilled from.

    `None` when the snapshot has none: an empty snapshot or a draft made
    before the order was stored.
    """
    snapshot = seeding.roster_snapshot
    if not snapshot or any(e.prefill_index is None for e in snapshot):
        return None
    return tuple(
        e.id for e in sorted(snapshot, key=lambda e: e.prefill_index or 0)
    )


def _is_untouched_prefill(
    seeding: TournamentSeeding, decoded: DecodedSeedCode
) -> bool:
    """Tell whether the code is exactly the prefill of its own baseline.

    Every orga action changes the code, so nothing has to be stored to
    know that nobody touched the draft.
    """
    baseline = _prefill_baseline(seeding)
    if baseline is None:
        return False
    try:
        prefill = _prefill_playoff_state(
            decoded.format,
            decoded.param,
            baseline,
            {e.id: e.origin or '' for e in seeding.roster_snapshot},
            draw_seed=decoded.draw_seed,
        )
    except ValueError:
        return False
    return _encode(prefill) == seeding.seed_code


def stage_generation(
    tournament_id: TournamentID,
    target: str,
    *,
    expected_version: int,
    initiator_id: UserID | None,
    confirmer_id: UserID | None = None,
) -> Result[tournament_match_service.GenerationOutcome, str]:
    """Generate from the draft without committing, for the playoff release.

    The caller holds the tournament lock, owns commit and rollback, and
    dispatches the outcome after the commit. `initiator_id` is the audit
    actor (`None` is the system); `confirmer_id` confirms the byes of the
    bracket and defaults to the initiator.
    """
    if target != PLAYOFF_TARGET:
        return Err(ERR_UNKNOWN_TARGET)
    return _generate_locked(
        tournament_id,
        target,
        expected_version,
        initiator_id,
        require_released=False,
        confirmer_id=confirmer_id,
    )


def start_violations(tournament_id: TournamentID) -> list[str]:
    """Return the reasons the generated seeding bars a tournament start.

    A tournament without a generated draft has none.
    """
    seeding = tournament_seeding_repository.find_seeding(
        tournament_id, INITIAL_TARGET
    )
    if seeding is None or seeding.generated_seed_code is None:
        return []

    decoded_result = seed_code.decode_seed_code(seeding.generated_seed_code)
    if decoded_result.is_err():
        return [ERR_STORED_CODE]
    decoded = decoded_result.unwrap()

    tournament = tournament_repository.get_tournament(tournament_id)
    roster = _roster(tournament)
    if len(roster.ids) != decoded.n or (
        seed_code.roster_fingerprint(roster.ids) != decoded.fingerprint
    ):
        return [ERR_ROSTER_CHANGED]

    structure = _format_for(tournament, roster)
    if structure.is_ok() and structure.unwrap() != (
        decoded.format,
        decoded.param,
    ):
        return [ERR_STRUCTURE_CHANGED_AFTER_GENERATION]
    return []


# -------------------------------------------------------------------- #
# internals


def _generate_locked(
    tournament_id: TournamentID,
    target: str,
    expected_version: int,
    initiator_id: UserID | None,
    *,
    require_released: bool = False,
    confirmer_id: UserID | None = None,
    skip_unchanged: bool = False,
) -> Result[tournament_match_service.GenerationOutcome, str]:
    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)
    view = _TargetView(tournament, target)
    is_playoff = target == PLAYOFF_TARGET
    ffa_round = view.ffa_round
    if is_playoff:
        window = _check_playoff_window(view, require_released)
        if window.is_err():
            return Err(window.unwrap_err())
    elif ffa_round is not None:
        window = _check_ffa_window(view)
        if window.is_err():
            return Err(window.unwrap_err())
    else:
        status = tournament.tournament_status
        if status in _LOCKED_STATUSES:
            return Err(ERR_LOCKED)
        if status is not TournamentStatus.REGISTRATION_CLOSED:
            return Err(ERR_NOT_OPEN_YET)

    seeding = tournament_seeding_repository.find_seeding_for_update(
        tournament_id, target
    )
    if seeding is None:
        return Err(ERR_NO_SEEDING)
    if seeding.version != expected_version:
        return Err(ERR_CONFLICT)

    plan_result = view.plan
    if (
        plan_result is not None
        and ffa_round is not None
        and ffa_round[0] is None
    ):
        if plan_result.is_err():
            return Err(plan_result.unwrap_err())
        plan = plan_result.unwrap()
        if tournament_match_service.is_single_survivor(plan):
            completed = tournament_match_service.complete_ffa_single_survivor(
                tournament, plan, initiator_id
            )
            if completed.is_err():
                return Err(completed.unwrap_err())
            event = completed.unwrap()
            return Ok(tournament_match_service.GenerationOutcome(
                count=0, created_events=[], deleted_events=[],
                ready_match_ids=frozenset(), occurred_at=event.occurred_at,
                completed_event=event,
            ))

    roster_result = view.roster
    if roster_result.is_err():
        return Err(roster_result.unwrap_err())
    roster = roster_result.unwrap()
    format_result = _format_for(tournament, roster, target)
    if format_result.is_err():
        return Err(format_result.unwrap_err())
    fmt, param = format_result.unwrap()

    state_result = _current_state(
        seeding,
        roster,
        (fmt, param),
        check_structure=target == INITIAL_TARGET,
        playoff_structure=(fmt, param) if target == PLAYOFF_TARGET else None,
    )
    if state_result.is_err():
        return Err(state_result.unwrap_err())
    state, stale_info = state_result.unwrap()
    if stale_info is not None:
        return Err(_stale_error(stale_info))

    problems, _ = _problems(
        state,
        roster,
        lobby_minimum=_lobby_minimum(view, state),
        stall=_initial_stall(view, state),
        cut=tournament.advancement_count,
    )
    if problems:
        return Err(ERR_PROBLEMS)

    if is_playoff:
        regenerating = view.phase_two.generated
    elif ffa_round is not None:
        regenerating = tournament_match_service.has_ffa_round(
            tournament_id, *ffa_round
        )
    else:
        regenerating = tournament_match_service.has_matches(tournament_id)
    if (
        skip_unchanged
        and regenerating
        and seeding.generated_seed_code == seeding.seed_code
    ):
        return Ok(tournament_match_service.GenerationOutcome(
            count=0, created_events=[], deleted_events=[],
            ready_match_ids=frozenset(), occurred_at=datetime.now(UTC),
            unchanged=True,
        ))
    if (
        target == INITIAL_TARGET
        and regenerating
        and _has_confirmed_result(tournament_id)
    ):
        return Err(ERR_RESULTS_EXIST)
    outcome_result = _run_generator(
        tournament_id,
        state,
        regenerating,
        confirmer_id or initiator_id,
        playoff=is_playoff,
        ffa_round=ffa_round,
    )
    if outcome_result.is_err():
        return outcome_result

    consumed = tournament_seeding_repository.set_generated(
        seeding.id,
        expected_version=expected_version,
        generated_seed_code=seeding.seed_code,
        roster_snapshot=_snapshot(
            state, roster.labels, prefill=roster if is_playoff else None
        ),
        now=datetime.now(UTC).replace(tzinfo=None),
    )
    if consumed.is_err():
        return Err(ERR_CONFLICT)
    create_log_entry(
        'bracket-regenerated' if regenerating else 'bracket-generated',
        tournament_id,
        initiator_id,
        data={
            'target': seeding.target,
            'seed_code': seeding.seed_code,
            'previous_seed_code': seeding.generated_seed_code,
        },
        commit=False,
    )
    return Ok(tournament_match_service.collect_generation_invitations_flush(
        outcome_result.unwrap(),
    ))


def _run_generator(
    tournament_id: TournamentID,
    state: SeedingState,
    regenerating: bool,
    initiator_id: UserID | None,
    *,
    playoff: bool = False,
    ffa_round: tuple[Bracket | None, int] | None = None,
) -> Result[tournament_match_service.GenerationOutcome, str]:
    """Run the flush-only generator for the state's format."""
    if ffa_round is not None:
        pool, round_number = ffa_round
        return tournament_match_service._generate_ffa_advance_impl(
            tournament_id,
            pool,
            round_number,
            groups=_split_groups(state),
            initiator_id=initiator_id,
        )
    # Phase 2 is built for the qualifiers, and only ever touches phase 2.
    phase_args: dict[str, Any] = (
        {'phase': 2, 'roster': state.roster} if playoff else {}
    )
    target = PLAYOFF_TARGET if playoff else INITIAL_TARGET
    match state.format:
        case SeedingFormat.SINGLE_ELIMINATION:
            return tournament_match_service._generate_single_elimination_impl(
                tournament_id,
                regenerating,
                layout=state.layout,
                initiator_id=initiator_id,
                seeding_target=target,
                **phase_args,
            )
        case SeedingFormat.DOUBLE_ELIMINATION:
            return tournament_match_service._generate_double_elimination_impl(
                tournament_id,
                regenerating,
                layout=state.layout,
                initiator_id=initiator_id,
                seeding_target=target,
                **phase_args,
            )
        case SeedingFormat.ROUND_ROBIN:
            return tournament_match_service._generate_round_robin_impl(
                tournament_id,
                regenerating,
                seed_list=_contestants(state.layout),
                group_sizes=_group_sizes(state),
                initiator_id=initiator_id,
                seeding_target=target,
            )
        case SeedingFormat.FREE_FOR_ALL:
            return tournament_match_service._generate_ffa_initial_impl(
                tournament_id,
                regenerating,
                groups=_split_groups(state),
                initiator_id=initiator_id,
                roster=state.roster if playoff else None,
                seeding_target=target,
            )


def _contestants(layout: Sequence[str | None]) -> list[str]:
    return [cid for cid in layout if cid is not None]


def _group_sizes(state: SeedingState) -> list[int]:
    return domain.group_sizes(state.format, len(state.roster), state.param)


def _split_groups(state: SeedingState) -> list[list[str]]:
    """Cut the layout into groups; each group in contestant-ID order."""
    contestants = _contestants(state.layout)
    groups = []
    start = 0
    for size in _group_sizes(state):
        groups.append(sorted(contestants[start : start + size]))
        start += size
    return groups


def _apply_locked(
    tournament_id: TournamentID,
    target: str,
    action: SeedingAction,
    expected_version: int,
    initiator_id: UserID,
    replay: DecodedSeedCode | None = None,
) -> Result[SeedingBoard, str]:
    tournament = tournament_repository.get_tournament(tournament_id)
    view = _TargetView(tournament, target)
    if view.ffa_round is not None:
        window = _check_ffa_window(view)
        if window.is_err():
            return Err(window.unwrap_err())
    elif target == PLAYOFF_TARGET:
        window = _check_playoff_window(view, require_released=False)
        if window.is_err():
            return Err(window.unwrap_err())
    else:
        status = tournament.tournament_status
        if status in _LOCKED_STATUSES:
            return Err(ERR_LOCKED)
        if status is not TournamentStatus.REGISTRATION_CLOSED:
            return Err(ERR_NOT_OPEN_YET)

    seeding = tournament_seeding_repository.find_seeding_for_update(
        tournament_id, target
    )
    if seeding is None:
        return Err(ERR_NO_SEEDING)
    if seeding.version != expected_version:
        return Err(ERR_CONFLICT)

    roster_result = view.roster
    if roster_result.is_err():
        return Err(roster_result.unwrap_err())
    roster = roster_result.unwrap()
    format_result = _format_for(tournament, roster, target)
    if format_result.is_err():
        return Err(format_result.unwrap_err())
    fmt, param = format_result.unwrap()

    state_result = _current_state(
        seeding,
        roster,
        (fmt, param),
        check_structure=target == INITIAL_TARGET,
        playoff_structure=(fmt, param) if target == PLAYOFF_TARGET else None,
    )
    if state_result.is_err():
        return Err(state_result.unwrap_err())
    state, stale_info = state_result.unwrap()

    if stale_info is not None and not isinstance(
        action, Replay | ReseedKeepTiers | Reprefill
    ):
        return Err(_stale_error(stale_info))

    new_state_result = _apply(state, action, roster, fmt, param, replay)
    if new_state_result.is_err():
        return Err(new_state_result.unwrap_err())
    new_state = new_state_result.unwrap()

    new_code = _encode(new_state)
    if new_code == seeding.seed_code and stale_info is None:
        return _board(view, seeding)

    now = datetime.now(UTC).replace(tzinfo=None)
    updated = tournament_seeding_repository.update_seeding_code(
        seeding.id,
        seed_code=new_code,
        expected_version=expected_version,
        roster_snapshot=_snapshot(
            new_state,
            roster.labels,
            previous=(
                () if isinstance(action, Reprefill) else seeding.roster_snapshot
            ),
            joiner_ids=(
                stale_info.joiner_ids
                if stale_info and isinstance(action, ReseedKeepTiers)
                else ()
            ),
            prefill=(
                roster
                if target == PLAYOFF_TARGET
                and isinstance(action, Replay | ReseedKeepTiers | Reprefill)
                else None
            ),
        ),
        updated_by=initiator_id,
        now=now,
    )
    if updated.is_err():
        return Err(updated.unwrap_err())

    create_log_entry(
        _event_type(action, state),
        tournament_id,
        initiator_id,
        data={
            'target': target,
            'previous_seed_code': seeding.seed_code,
            'seed_code': new_code,
            'version': updated.unwrap().version,
            **_event_data(action, state, new_state, roster),
        },
        commit=False,
    )
    return _board(view, updated.unwrap())


def _create_draft(
    tournament_id: TournamentID,
    target: str,
    initiator_id: UserID | None,
) -> Result[TournamentSeeding, str]:
    tournament_repository.lock_tournament_for_update(tournament_id)

    existing = tournament_seeding_repository.find_seeding(tournament_id, target)
    if existing is not None:
        tournament_repository.rollback_session()
        return Ok(existing)

    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.tournament_status is not TournamentStatus.REGISTRATION_CLOSED:
        tournament_repository.rollback_session()
        return Err(ERR_NOT_OPEN_YET)

    roster = _roster(tournament)
    format_result = _format_for(tournament, roster)
    if format_result.is_err():
        tournament_repository.rollback_session()
        return Err(format_result.unwrap_err())
    fmt, param = format_result.unwrap()

    state = domain.initial_state(
        fmt,
        param,
        roster.ids,
        tier_count=_initial_tier_count(fmt, len(roster.ids), param),
        draw_seed=domain.new_draw_seed(),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    seeding = TournamentSeeding(
        id=TournamentSeedingID(generate_uuid7()),
        tournament_id=tournament_id,
        target=target,
        seed_code=_encode(state),
        roster_snapshot=_snapshot(state, roster.labels),
        version=1,
        generated_seed_code=None,
        generated_at=None,
        updated_by=initiator_id,
        created_at=now,
        updated_at=now,
    )
    try:
        tournament_seeding_repository.create_seeding(seeding)
        create_log_entry(
            'seeding-drawn',
            tournament_id,
            initiator_id,
            data={
                'target': target,
                'seed_code': seeding.seed_code,
                'version': seeding.version,
            },
            commit=False,
        )
        tournament_repository.commit_session()
    except IntegrityError:
        tournament_repository.rollback_session()
        found = tournament_seeding_repository.find_seeding(
            tournament_id, target
        )
        if found is None:
            raise
        return Ok(found)
    return Ok(seeding)


def _ensure_playoff_board(
    tournament_id: TournamentID, initiator_id: UserID | None = None
) -> Result[SeedingBoard, str]:
    tournament = tournament_repository.get_tournament(tournament_id)
    view = _TargetView(tournament, PLAYOFF_TARGET)
    seeding_result = _stage_playoff_draft(view, initiator_id)
    if seeding_result.is_err():
        return Err(seeding_result.unwrap_err())
    return _board(view, seeding_result.unwrap())


def _check_playoff_window(
    view: '_TargetView', require_released: bool
) -> Result[None, str]:
    """Check that the playoffs may be generated now.

    Open while the tournament runs, until the first playoff result.
    """
    tournament = view.tournament
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return Err(ERR_PLAYOFF_NOT_RUNNING)
    if require_released and tournament.playoff_released_at is None:
        return Err(ERR_PLAYOFF_NOT_RELEASED)
    if view.phase_two.has_result:
        return Err(ERR_PLAYOFF_LOCKED)
    return Ok(None)


def _parse_ffa_target(target: str) -> tuple[Bracket | None, int] | None:
    """Return the pool and round of an FFA round target, else `None`."""
    found = _FFA_TARGET.fullmatch(target)
    if found is None:
        return None
    return _FFA_POOLS[found.group(1)], int(found.group(2))


def _ffa_lock_reason(
    tournament: Tournament, pool: Bracket | None, round_number: int
) -> str | None:
    """Return why the round's draft can no longer change, if it cannot."""
    in_round = tournament_repository.get_matches_for_round(
        tournament.id, round_number, bracket=pool
    )
    if tournament_match_service.is_untagged_winners_round(pool, in_round):
        return tournament_match_service.FFA_UNTAGGED_WB_ERROR
    target = tournament_match_service.ffa_round_seeding_target(
        pool, round_number
    )
    matches = in_round + tournament_repository.get_matches_for_seeding_target(
        tournament.id, target
    )
    if tournament.tournament_status in (
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    ) or any(m.confirmed_by is not None for m in matches):
        return tournament_match_service.FFA_ROUND_LOCKED_ERROR
    return None


def _check_ffa_window(view: '_TargetView') -> Result[None, str]:
    """Check that the round may be seeded now.

    Open while the tournament runs, until the round's first result.
    """
    if view.tournament.tournament_status is not TournamentStatus.ONGOING:
        return Err(ERR_FFA_NOT_RUNNING)
    reason = view.lock_reason
    if reason is not None:
        return Err(reason)
    return Ok(None)


def _survivor_roster(
    base: _Roster, plan: tournament_match_service.FfaAdvancePlan
) -> _Roster:
    """Return the survivors that play the round, named like the initial roster."""
    labels = base.labels
    ids = tournament_match_service.ffa_draft_survivors(plan)
    return _Roster(ids, {i: labels.get(i) or i for i in ids}, ())


def _prepare_ffa_draft_locked(
    tournament_id: TournamentID,
    pool: Bracket | None,
    initiator_id: UserID,
) -> Result[str, str]:
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return Err(ERR_FFA_NOT_RUNNING)

    plan_result = tournament_match_service.plan_ffa_advance(tournament, pool)
    if plan_result.is_err():
        return Err(plan_result.unwrap_err())
    plan = plan_result.unwrap()
    if plan.grand_final_eligible:
        return Err(tournament_match_service.FFA_GRAND_FINAL_ERROR)
    if tournament_match_service.is_lone_losers_round(plan):
        return Err(tournament_match_service.FFA_LONE_SURVIVOR_ERROR)
    if tournament_match_service.is_single_survivor(plan):
        completed = tournament_match_service.complete_ffa_single_survivor(
            tournament, plan, initiator_id
        )
        if completed.is_err():
            return Err(completed.unwrap_err())
        return Ok('completed')

    target = ffa_round_target(pool, plan.round_number)
    roster = _survivor_roster(_roster(tournament), plan)
    format_result = _format_for(tournament, roster, target)
    if format_result.is_err():
        return Err(format_result.unwrap_err())
    fmt, param = format_result.unwrap()

    existing = tournament_seeding_repository.find_seeding_for_update(
        tournament_id, target
    )
    if existing is not None:
        current = _current_state(existing, roster, None)
        if current.is_ok() and current.unwrap()[1] is None:
            return Ok(target)

    # Tiers are the rank bands; the seed list is the standing order.
    canonical = seed_code.canonical_roster(roster.ids)
    tier_count = max(
        1, min(domain.MAX_TIER_COUNT, max(plan.bands.values()) + 1)
    )
    state = domain.initial_state(
        fmt,
        param,
        roster.ids,
        tier_count=tier_count,
        draw_seed=domain.new_draw_seed(),
        tiers=tuple(min(plan.bands[i], tier_count - 1) for i in canonical),
        seed_list=roster.ids,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    if existing is not None:
        # Rebuilt in place, so a tab of the old draft meets a newer version.
        rebuilt = tournament_seeding_repository.update_seeding_code(
            existing.id,
            seed_code=_encode(state),
            expected_version=existing.version,
            roster_snapshot=_snapshot(state, roster.labels),
            updated_by=initiator_id,
            now=now,
        )
        if rebuilt.is_err():
            return Err(ERR_CONFLICT)
        seeding = rebuilt.unwrap()
    else:
        seeding = TournamentSeeding(
            id=TournamentSeedingID(generate_uuid7()),
            tournament_id=tournament_id,
            target=target,
            seed_code=_encode(state),
            roster_snapshot=_snapshot(state, roster.labels),
            version=1,
            generated_seed_code=None,
            generated_at=None,
            updated_by=initiator_id,
            created_at=now,
            updated_at=now,
        )
        tournament_seeding_repository.create_seeding(seeding)
    create_log_entry(
        'seeding-drawn',
        tournament_id,
        initiator_id,
        data={
            'target': target,
            'seed_code': seeding.seed_code,
            'version': seeding.version,
        },
        commit=False,
    )
    return Ok(target)


def _phase_two_progress(tournament_id: TournamentID) -> 'PhaseTwoProgress':
    # Imported late: the qualification service imports this module.
    from . import tournament_qualification_service

    return tournament_qualification_service.get_phase_two_progress(
        tournament_id
    )


def _qualifiers(
    tournament: Tournament,
) -> Result[tuple[qualification_domain.Qualifier, ...], str]:
    """Return the qualifiers in seed order, once the qualification is ready."""
    from . import tournament_qualification_service

    state_result = tournament_qualification_service.get_qualification(
        tournament.id
    )
    if state_result.is_err():
        return Err(state_result.unwrap_err())
    return _ready_qualifiers(state_result.unwrap())


def _ready_qualifiers(
    state: 'QualificationState',
) -> Result[tuple[qualification_domain.Qualifier, ...], str]:
    if not state.ready or state.seed_order is None:
        return Err(ERR_PLAYOFF_NOT_READY)
    return Ok(state.seed_order)


def _playoff_roster(
    base: _Roster,
    qualifiers: Sequence[qualification_domain.Qualifier],
) -> _Roster:
    """Return the qualifiers as the roster, named like the initial one.

    `prefill_order` is the qualification order a draft is prefilled from.
    """
    labels = base.labels
    ids = tuple(q.contestant_id for q in qualifiers)
    origins = {q.contestant_id: (q.scope, q.rank) for q in qualifiers}
    return _Roster(ids, {i: labels.get(i) or i for i in ids}, (), origins, ids)


def _origin_scopes(roster: _Roster) -> dict[str, str]:
    return {cid: scope for cid, (scope, _) in roster.origins.items()}


class _TargetView:
    """What one call needs about a seeding target, each part read once.

    Make one per service call and drop it with the call. It caches the
    plan, the roster, the lock and the phase-2 progress on first use, so a
    call must not write what they read before it is done with the view.
    """

    def __init__(
        self,
        tournament: Tournament,
        target: str,
        *,
        qualification: 'QualificationState | None' = None,
    ) -> None:
        self.tournament = tournament
        self.target = target
        self.ffa_round = _parse_ffa_target(target)
        self._qualification = qualification

    @cached_property
    def base(self) -> _Roster:
        """The tournament's contestants, the labels source of every target."""
        return _roster(self.tournament)

    @cached_property
    def plan(
        self,
    ) -> Result[tournament_match_service.FfaAdvancePlan, str] | None:
        """The advance plan of an FFA round target, else `None`."""
        if self.ffa_round is None:
            return None
        pool, round_number = self.ffa_round
        return tournament_match_service.plan_ffa_advance(
            self.tournament, pool, next_round=round_number
        )

    @cached_property
    def qualifiers(
        self,
    ) -> Result[tuple[qualification_domain.Qualifier, ...], str] | None:
        """The qualifiers of the playoff target, else `None`."""
        if self.target != PLAYOFF_TARGET:
            return None
        if self._qualification is not None:
            return _ready_qualifiers(self._qualification)
        return _qualifiers(self.tournament)

    @cached_property
    def roster(self) -> Result[_Roster, str]:
        """Who the target seeds."""
        plan_result = self.plan
        if plan_result is not None:
            if plan_result.is_err():
                return Err(plan_result.unwrap_err())
            return Ok(_survivor_roster(self.base, plan_result.unwrap()))
        qualifiers_result = self.qualifiers
        if qualifiers_result is None:
            return Ok(self.base)
        if qualifiers_result.is_err():
            return Err(qualifiers_result.unwrap_err())
        return Ok(_playoff_roster(self.base, qualifiers_result.unwrap()))

    @cached_property
    def lock_reason(self) -> str | None:
        """Why an FFA round's draft can no longer change, if it cannot."""
        if self.ffa_round is None:
            return None
        return _ffa_lock_reason(self.tournament, *self.ffa_round)

    @cached_property
    def phase_two(self) -> 'PhaseTwoProgress':
        """How far phase 2 has come, for the playoff target."""
        return _phase_two_progress(self.tournament.id)


def _prefill_playoff_state(
    fmt: SeedingFormat,
    param: int,
    seed_order: Sequence[str],
    origin: Mapping[str, str],
    *,
    draw_seed: int,
) -> SeedingState:
    """Return the playoff draft the qualification implies.

    Elimination: the qualifiers arrive in seed order, with first-round
    matches pairing one group's qualifiers swapped apart where possible.
    Free-for-all: the leaderboard order, cut into up to four tiers.
    `origin` maps a qualifier to its scope.
    """
    if fmt is SeedingFormat.FREE_FOR_ALL:
        seed_list = tuple(seed_order)
        lobbies = domain.group_count(fmt, len(seed_list), param)
        return domain.initial_state(
            fmt,
            param,
            seed_list,
            tier_count=max(_MIN_TIERS, min(_MAX_PLAYOFF_TIERS, lobbies)),
            draw_seed=draw_seed,
            seed_list=seed_list,
        )

    seed_list = tuple(seed_order)
    state = domain.initial_state(
        fmt,
        param,
        seed_list,
        tier_count=1,
        draw_seed=draw_seed,
        seed_list=seed_list,
    )
    separated = qualification_domain.separate_same_group(state.layout, origin)
    if separated is None:
        return state
    for p, q in separated[1]:
        state = domain.swap_slots(state, p, q)
    return state


def _roster(tournament: Tournament) -> _Roster:
    """Return the active contestants, the same set the generators use."""
    contestant_type = derive_contestant_type(
        tournament.contestant_type,
        tournament.max_players_in_team,
        tournament.min_players_in_team,
    )
    tournament_id = tournament.id

    if contestant_type == ContestantType.TEAM:
        teams = tournament_repository.get_teams_for_tournament(tournament_id)
        member_counts = tournament_repository.get_team_member_counts(
            tournament_id
        )
        labels = {str(t.id): t.name for t in teams}
        empty = tuple(t.name for t in teams if member_counts.get(t.id, 0) == 0)
        return _Roster(tuple(labels), labels, empty)

    participants = tournament_repository.get_participants_for_tournament(
        tournament_id
    )
    users = user_service.get_users_indexed_by_id(
        {p.user_id for p in participants}
    )
    labels = {}
    for p in participants:
        user = users.get(p.user_id)
        labels[str(p.id)] = (
            user.screen_name if user and user.screen_name else str(p.user_id)
        )
    return _Roster(tuple(labels), labels, ())


def _snapshot(
    state: SeedingState,
    labels: Mapping[str, str],
    *,
    previous: Sequence[RosterEntry] = (),
    joiner_ids: Sequence[str] = (),
    prefill: _Roster | None = None,
) -> tuple[RosterEntry, ...]:
    """Return the roster in the codec's canonical order, with labels.

    Entrants marked late in `previous` stay marked; `joiner_ids` get marked.
    With `prefill`, each entrant records its place in that qualification
    order and its scope; without, both are carried over from `previous`.
    """
    late = {e.id for e in previous if e.joined_late} | set(joiner_ids)
    before = {e.id: e for e in previous}
    place = (
        {cid: i for i, cid in enumerate(prefill.prefill_order)}
        if prefill is not None
        else {}
    )
    entries = []
    for cid in state.roster:
        if prefill is not None:
            index = place.get(cid)
            scope = prefill.origins.get(cid)
            origin = scope[0] if scope is not None else None
        else:
            kept = before.get(cid)
            index = kept.prefill_index if kept else None
            origin = kept.origin if kept else None
        entries.append(
            RosterEntry(cid, labels[cid], cid in late, index, origin)
        )
    return tuple(entries)


def _format_for(
    tournament: Tournament, roster: _Roster, target: str = INITIAL_TARGET
) -> Result[tuple[SeedingFormat, int], str]:
    """Return the seeding format and its parameter for the target."""
    if len(roster.ids) < 2:
        return Err(ERR_TOO_FEW)
    if len(roster.ids) > MAX_CONTESTANTS:
        return Err(ERR_TOO_MANY)

    if target == PLAYOFF_TARGET or (
        _parse_ffa_target(target) is not None
        and tournament_match_service._ffa_phase(tournament) == 2
    ):
        game_format = game_format_for_phase(tournament, 2)
        mode = elimination_mode_for_phase(tournament, 2)
    else:
        game_format = tournament.game_format
        mode = tournament.elimination_mode
    if game_format is GameFormat.FREE_FOR_ALL:
        lobby_size = tournament.group_size_max or len(roster.ids)
        param = min(lobby_size, seed_code.MAX_PARAM)
        return Ok((SeedingFormat.FREE_FOR_ALL, param))
    if game_format is GameFormat.ONE_V_ONE:
        if mode is EliminationMode.SINGLE_ELIMINATION:
            return Ok((SeedingFormat.SINGLE_ELIMINATION, 0))
        if mode is EliminationMode.DOUBLE_ELIMINATION:
            if (
                target == PLAYOFF_TARGET
                and len(roster.ids) < domain.MIN_DOUBLE_ELIMINATION
            ):
                return Ok((SeedingFormat.SINGLE_ELIMINATION, 0))
            return Ok((SeedingFormat.DOUBLE_ELIMINATION, 0))
        if mode is EliminationMode.ROUND_ROBIN:
            groups = (
                tournament.playoff_group_count
                if tournament.has_playoffs
                else 1
            )
            param = min(groups or 1, seed_code.MAX_PARAM)
            return Ok((SeedingFormat.ROUND_ROBIN, param))
    return Err(ERR_NO_FORMAT)


def _encode(state: SeedingState) -> str:
    derived = domain.derive_layout(state.format, state.seed_list, state.param)
    return seed_code.encode_seed_code(state, derived)


def _decode_state(
    raw_code: str, roster_ids: Sequence[str]
) -> Result[SeedingState, str]:
    decoded = seed_code.decode_seed_code(raw_code)
    if decoded.is_err():
        return Err(decoded.unwrap_err())
    return seed_code.state_from_code(
        decoded.unwrap(), roster_ids, domain.derive_layout
    )


@dataclass(frozen=True)
class _StaleInfo:
    leaver_ids: tuple[str, ...]
    joiner_ids: tuple[str, ...]
    structure_changed: bool = False
    ranks_changed: bool = False


def _stale_error(info: _StaleInfo) -> str:
    if info.structure_changed:
        return ERR_STRUCTURE_CHANGED
    if info.ranks_changed:
        return ERR_RANKS_CHANGED
    return ERR_STALE


def _current_state(
    seeding: TournamentSeeding,
    roster: _Roster,
    fallback: tuple[SeedingFormat, int] | None,
    *,
    check_structure: bool = False,
    playoff_structure: tuple[SeedingFormat, int] | None = None,
) -> Result[tuple[SeedingState, _StaleInfo | None], str]:
    """Return the draft's state and, if it is stale, how it changed.

    A stale draft is decoded against the roster snapshot stored with it.
    Without a usable snapshot, the state falls back to a fresh draw of the
    current roster (none for a started tournament).

    With `check_structure`, a draft drawn for another format or parameter
    than `fallback` is stale too; its state is a fresh draw in the current
    structure.

    The playoff draft follows the qualification. With a
    `playoff_structure`, in this order:
    - another structure, both single or double elimination, and the draft
      still fits the roster: stale with its seed list and fixes intact, now
      in the new structure;
    - another structure otherwise (a free-for-all lobby size, a switch
      between free-for-all and a bracket, a changed roster): stale with the
      qualification prefill in the new structure;
    - the same structure and roster, but the qualification order differs
      from the one the draft was prefilled from: stale by `ranks_changed`;
    - the same structure and a changed roster: the leavers and joiners.
    """
    decoded_result = seed_code.decode_seed_code(seeding.seed_code)
    if decoded_result.is_err():
        return Err(ERR_STORED_CODE)
    decoded = decoded_result.unwrap()

    if (
        check_structure
        and fallback is not None
        and (decoded.format, decoded.param) != fallback
    ):
        return Ok(_restructured_state(decoded, seeding, roster, fallback))

    current = seed_code.state_from_code(
        decoded, roster.ids, domain.derive_layout
    )
    previous_ids = tuple(e.id for e in seeding.roster_snapshot)
    ranks_changed = False
    if playoff_structure is not None:
        baseline = _prefill_baseline(seeding)
        ranks_changed = (
            baseline is not None and baseline != roster.prefill_order
        )
        if (decoded.format, decoded.param) != playoff_structure:
            fmt, param = playoff_structure
            if current.is_ok() and {decoded.format, fmt}.issubset(
                _ELIMINATION_FORMATS
            ):
                return Ok(
                    (
                        replace(current.unwrap(), format=fmt, param=param),
                        _StaleInfo(
                            (),
                            (),
                            structure_changed=True,
                            ranks_changed=ranks_changed,
                        ),
                    )
                )
            return Ok(
                (
                    _prefill_playoff_state(
                        fmt,
                        param,
                        roster.prefill_order,
                        _origin_scopes(roster),
                        draw_seed=decoded.draw_seed,
                    ),
                    _StaleInfo(
                        tuple(sorted(set(previous_ids) - set(roster.ids))),
                        tuple(sorted(set(roster.ids) - set(previous_ids))),
                        structure_changed=True,
                    ),
                )
            )
    if current.is_ok():
        if ranks_changed:
            return Ok(
                (current.unwrap(), _StaleInfo((), (), ranks_changed=True))
            )
        return Ok((current.unwrap(), None))

    old_state = seed_code.state_from_code(
        decoded, previous_ids, domain.derive_layout
    )
    if old_state.is_ok():
        leavers = tuple(sorted(set(previous_ids) - set(roster.ids)))
        joiners = tuple(sorted(set(roster.ids) - set(previous_ids)))
        return Ok((old_state.unwrap(), _StaleInfo(leavers, joiners)))

    if fallback is None:
        return Err(ERR_STALE)
    fmt, param = fallback
    fresh = domain.initial_state(
        fmt,
        param,
        roster.ids,
        tier_count=_initial_tier_count(fmt, len(roster.ids), param),
        draw_seed=decoded.draw_seed,
    )
    return Ok((fresh, _StaleInfo((), ())))


def _initial_tier_count(fmt: SeedingFormat, n: int, param: int) -> int:
    """Return the tier count a fresh draft starts with."""
    if fmt is not SeedingFormat.FREE_FOR_ALL:
        return 1
    lobbies = domain.group_count(fmt, n, param)
    return max(_MIN_TIERS, min(_MAX_PLAYOFF_TIERS, lobbies))


def _restructured_state(
    decoded: DecodedSeedCode,
    seeding: TournamentSeeding,
    roster: _Roster,
    structure: tuple[SeedingFormat, int],
) -> tuple[SeedingState, _StaleInfo]:
    """Return a fresh draw in `structure`, keeping the old tiers if held."""
    fmt, param = structure
    previous_ids = tuple(e.id for e in seeding.roster_snapshot)
    old = None
    if (
        fmt is SeedingFormat.FREE_FOR_ALL
        and decoded.format is SeedingFormat.FREE_FOR_ALL
    ):
        for ids in (roster.ids, previous_ids):
            restored = seed_code.state_from_code(
                decoded, ids, domain.derive_layout
            )
            if restored.is_ok():
                old = domain.reseed_for_roster(restored.unwrap(), roster.ids)
                break

    if old is None:
        state = domain.initial_state(
            fmt,
            param,
            roster.ids,
            tier_count=_initial_tier_count(fmt, len(roster.ids), param),
            draw_seed=decoded.draw_seed,
        )
    else:
        state = domain.initial_state(
            fmt,
            param,
            roster.ids,
            tier_count=old.tier_count,
            draw_seed=decoded.draw_seed,
            tiers=old.tiers,
            seed_list=old.seed_list,
        )

    leavers: tuple[str, ...] = ()
    joiners: tuple[str, ...] = ()
    if previous_ids:
        leavers = tuple(sorted(set(previous_ids) - set(roster.ids)))
        joiners = tuple(sorted(set(roster.ids) - set(previous_ids)))
    return state, _StaleInfo(leavers, joiners, structure_changed=True)


def _apply(
    state: SeedingState,
    action: SeedingAction,
    roster: _Roster,
    fmt: SeedingFormat,
    param: int,
    replay: DecodedSeedCode | None = None,
) -> Result[SeedingState, str]:
    is_ffa = state.format is SeedingFormat.FREE_FOR_ALL
    match action:
        case Replay() if replay is not None:
            return _replay(replay, roster, fmt, param)
        case MoveTier() | SetTierCount() if not is_ffa:
            return Err(ERR_NO_TIERS)
        case SetTierCount(n=n) if not _MIN_TIERS <= n <= _MAX_PLAYOFF_TIERS:
            return Err(ERR_TIER_COUNT)
    try:
        match action:
            case Swap(p=p, q=q):
                return Ok(domain.swap_slots(state, p, q))
            case MoveTier(
                contestant_id=cid, tier=tier, ref_id=ref, after=after
            ):
                return Ok(
                    domain.move_to_tier(
                        state, cid, tier, ref_id=ref, after=after
                    )
                )
            case SetTierCount(n=n):
                return Ok(domain.set_tier_count(state, n))
            case Redraw():
                return Ok(domain.redraw(state, domain.new_draw_seed()))
            case ResetFixes():
                return Ok(_separated_baseline(state, roster))
            case ReseedKeepTiers():
                if set(state.roster) == set(roster.ids):
                    return Ok(state)
                return Ok(domain.reseed_for_roster(state, roster.ids))
            case Separate():
                return _separate(state, roster)
            case Reprefill():
                if not roster.prefill_order:
                    return Err(ERR_INVALID_CHANGE)
                return Ok(
                    _prefill_playoff_state(
                        fmt,
                        param,
                        roster.prefill_order,
                        _origin_scopes(roster),
                        draw_seed=state.draw_seed,
                    )
                )
    except ValueError:
        return Err(ERR_INVALID_CHANGE)
    return Err(ERR_INVALID_CHANGE)


def _separated_baseline(state: SeedingState, roster: _Roster) -> SeedingState:
    """Return the draft without manual swaps, separated where possible.

    Only an elimination playoff draft has a separation; any other draft
    just loses its swaps. Seed order, tiers and draw seed stay.
    """
    baseline = domain.reset_fixes(state)
    if state.format not in _ELIMINATION_FORMATS or not roster.origins:
        return baseline
    origin = {cid: scope for cid, (scope, _) in roster.origins.items()}
    separated = qualification_domain.separate_same_group(
        baseline.layout, origin
    )
    if separated is None:
        return baseline
    for p, q in separated[1]:
        baseline = domain.swap_slots(baseline, p, q)
    return baseline


def _fix_count(state: SeedingState, roster: _Roster) -> int:
    """Count the manual swaps on top of the separated prefill layout."""
    if state.format not in _ELIMINATION_FORMATS or not roster.origins:
        return domain.fix_count(state)
    baseline = _separated_baseline(state, roster)
    return len(seed_code.canonical_swaps(baseline.layout, state.layout))


def _is_prefilled(state: SeedingState, seeding: TournamentSeeding) -> bool:
    """Tell whether the draft still has the prefilled order and tiers."""
    if seeding.target != PLAYOFF_TARGET:
        return False
    baseline = _prefill_baseline(seeding)
    if baseline is None:
        return False
    try:
        prefill = _prefill_playoff_state(
            state.format,
            state.param,
            baseline,
            {e.id: e.origin or '' for e in seeding.roster_snapshot},
            draw_seed=state.draw_seed,
        )
    except ValueError:
        return False
    return (state.seed_list, state.tiers, state.tier_count) == (
        prefill.seed_list,
        prefill.tiers,
        prefill.tier_count,
    )


def _separate(
    state: SeedingState, roster: _Roster
) -> Result[SeedingState, str]:
    """Swap first-round pairings of one group apart."""
    if state.format not in _ELIMINATION_FORMATS or not roster.origins:
        return Err(ERR_INVALID_CHANGE)
    origin = {cid: scope for cid, (scope, _) in roster.origins.items()}
    separated = qualification_domain.separate_same_group(state.layout, origin)
    if separated is None:
        return Err(ERR_SEPARATE_STUCK)
    swaps = separated[1]
    if not swaps:
        return Err(ERR_NOTHING_TO_SEPARATE)
    for p, q in swaps:
        state = domain.swap_slots(state, p, q)
    return Ok(state)


def _replay(
    decoded: DecodedSeedCode, roster: _Roster, fmt: SeedingFormat, param: int
) -> Result[SeedingState, str]:
    if decoded.format is not fmt or decoded.param != param:
        return Err(ERR_OTHER_MODE)
    return seed_code.state_from_code(decoded, roster.ids, domain.derive_layout)


def _event_type(action: SeedingAction, before: SeedingState) -> str:
    match action:
        case Swap():
            return 'seeding-swapped'
        case MoveTier(contestant_id=cid, tier=tier):
            tier_of = dict(zip(before.roster, before.tiers, strict=True))
            if tier_of.get(cid) == tier:
                return 'seeding-reordered'
            return 'seeding-tier-changed'
        case SetTierCount():
            return 'seeding-tiers-resized'
        case Redraw():
            return 'seeding-drawn'
        case ResetFixes():
            return 'seeding-fixes-reset'
        case Replay():
            return 'seeding-code-replayed'
        case ReseedKeepTiers():
            return 'seeding-roster-reseeded'
        case Separate():
            return 'seeding-separated'
        case Reprefill():
            return 'seeding-reprefilled'
    raise ValueError(f'unknown action {action!r}')


def _event_data(
    action: SeedingAction,
    before: SeedingState,
    after: SeedingState,
    roster: _Roster,
) -> dict[str, Any]:
    """Return the audit detail keys that describe `action`."""
    match action:
        case Swap(p=p, q=q):
            return {
                'p': p,
                'q': q,
                'a': before.layout[p],
                'b': before.layout[q],
                'unit_p': _layout_unit(before, p),
                'unit_q': _layout_unit(before, q),
                'format': before.format.name,
            }
        case MoveTier(contestant_id=cid, tier=tier):
            tier_of = dict(zip(before.roster, before.tiers, strict=True))
            after_tier = dict(zip(after.roster, after.tiers, strict=True))
            members = [c for c in after.seed_list if after_tier[c] == tier]
            return {
                'contestant_id': cid,
                'from_tier': tier_of[cid],
                'to_tier': tier,
                'place_in_tier': members.index(cid) + 1,
            }
        case SetTierCount():
            return {
                'tier_count': after.tier_count,
                'dropped_fixes': _dropped_fixes(before, after, roster),
            }
        case ResetFixes():
            return {'dropped_fixes': _dropped_fixes(before, after, roster)}
        case ReseedKeepTiers():
            return {
                'leaver_ids': sorted(set(before.roster) - set(after.roster)),
                'joiner_ids': sorted(set(after.roster) - set(before.roster)),
            }
    return {}


def _layout_unit(state: SeedingState, slot: int) -> int:
    """Return the match, group or lobby index that holds a layout slot."""
    if state.format in _ELIMINATION_FORMATS:
        return slot // 2
    end = 0
    sizes = domain.group_sizes(state.format, len(state.layout), state.param)
    for index, size in enumerate(sizes):
        end += size
        if slot < end:
            return index
    return len(sizes) - 1


def _dropped_fixes(
    before: SeedingState, after: SeedingState, roster: _Roster
) -> int:
    return max(0, _fix_count(before, roster) - _fix_count(after, roster))


def _has_confirmed_result(tournament_id: TournamentID) -> bool:
    """Ignore confirmed byes and default wins with only one contestant."""
    confirmed = [
        match
        for match in tournament_repository.get_matches_for_tournament(
            tournament_id
        )
        if match.confirmed_by is not None
    ]
    contestants = tournament_repository.get_contestants_for_matches(
        [match.id for match in confirmed]
    )
    return any(len(contestants.get(match.id, ())) >= 2 for match in confirmed)


def _board(
    view: _TargetView, seeding: TournamentSeeding
) -> Result[SeedingBoard, str]:
    tournament = view.tournament
    roster_result = view.roster
    if roster_result.is_err():
        return Err(roster_result.unwrap_err())
    roster = roster_result.unwrap()
    format_result = _format_for(tournament, roster, seeding.target)
    locked = _is_locked(view)
    if format_result.is_err() and not locked:
        return Err(format_result.unwrap_err())

    state_result = _current_state(
        seeding,
        roster,
        format_result.unwrap() if format_result.is_ok() else None,
        check_structure=seeding.target == INITIAL_TARGET and not locked,
        playoff_structure=(
            format_result.unwrap()
            if seeding.target == PLAYOFF_TARGET
            and format_result.is_ok()
            and not locked
            else None
        ),
    )
    if state_result.is_err():
        return Err(state_result.unwrap_err())
    state, stale_info = state_result.unwrap()

    snapshot_labels = {e.id: e.label for e in seeding.roster_snapshot}
    labels = {
        cid: roster.labels.get(cid) or snapshot_labels.get(cid) or cid
        for cid in state.roster
    }
    leaver_ids = stale_info.leaver_ids if stale_info else ()
    joiner_ids = stale_info.joiner_ids if stale_info else ()

    problems, params = _problems(
        state,
        roster,
        lobby_minimum=_lobby_minimum(view, state),
        stall=_initial_stall(view, state),
        cut=tournament.advancement_count,
    )
    notices: list[tuple[str, dict[str, Any]]] = [
        *shortfall_notices(_shortfall(view))
    ]
    notices.extend(_small_lobby_notices(view, state))
    origin_labels = _origin_labels(roster, state)
    return Ok(
        SeedingBoard(
            tournament_id=tournament.id,
            target=seeding.target,
            state=state,
            version=seeding.version,
            code=seed_code.format_seed_code(seeding.seed_code),
            stale=stale_info is not None,
            labels=labels,
            stale_leavers=tuple(snapshot_labels.get(i, i) for i in leaver_ids),
            stale_joiners=tuple(roster.labels.get(i, i) for i in joiner_ids),
            stale_leaver_ids=leaver_ids,
            stale_joiner_ids=joiner_ids,
            new_entrant_ids=tuple(
                e.id
                for e in seeding.roster_snapshot
                if e.joined_late and e.id in state.roster
            ),
            problems=tuple(problems),
            problem_params=tuple(params),
            balance=domain.balance(state),
            fix_count=_fix_count(state, roster),
            pure_draw=domain.is_pure_draw(state),
            generated_code=seeding.generated_seed_code,
            generation=_generation_status(view, seeding),
            locked_reason=_locked_reason(view) if locked else None,
            regenerate_refusal=(
                ERR_RESULTS_EXIST
                if seeding.target == INITIAL_TARGET
                and not locked
                and _has_confirmed_result(tournament.id)
                else None
            ),
            origin_labels=origin_labels,
            same_group_matches=_same_group_matches(roster, state),
            stale_structure=bool(stale_info and stale_info.structure_changed),
            stale_ranks=bool(stale_info and stale_info.ranks_changed),
            undersized=_undersized_pools(view),
            byes=_bye_labels(view),
            waiting_winners=_waiting_winner_labels(view),
            notices=tuple(msgid for msgid, _ in notices),
            notice_params=tuple(params for _, params in notices),
            prefilled=_is_prefilled(state, seeding),
        )
    )


def _small_lobby_notices(
    view: _TargetView, state: SeedingState
) -> list[tuple[str, dict[str, Any]]]:
    """Return the notice for undersized round-0 playoff lobbies."""
    if (
        view.target != PLAYOFF_TARGET
        or state.format is not SeedingFormat.FREE_FOR_ALL
    ):
        return []
    minimum = view.tournament.group_size_min or 2
    sizes = _group_sizes(state)
    if not sizes or not 2 <= min(sizes) < minimum:
        return []
    return [
        (
            NOTICE_SMALL_PLAYOFF_LOBBIES,
            {'sizes': ', '.join(str(n) for n in sizes), 'minimum': minimum},
        )
    ]


def _playoff_double_elimination(tournament: Tournament) -> bool:
    return (
        tournament.playoff_game_format is GameFormat.ONE_V_ONE
        and tournament.playoff_elimination_mode
        is EliminationMode.DOUBLE_ELIMINATION
    )


def _group_stage_shortfall(
    tournament: Tournament, roster: _Roster
) -> domain.Shortfall | None:
    if (
        not tournament.has_playoffs
        or tournament.elimination_mode is not EliminationMode.ROUND_ROBIN
        or not tournament.playoff_group_count
        or not tournament.playoff_qualifiers_per_group
    ):
        return None
    format_result = _format_for(tournament, roster)
    if format_result.is_err():
        return None
    fmt, param = format_result.unwrap()
    if fmt is not SeedingFormat.ROUND_ROBIN:
        return None
    return domain.group_shortfall(
        len(roster.ids),
        param,
        tournament.playoff_group_count,
        tournament.playoff_qualifiers_per_group,
        double_elimination=_playoff_double_elimination(tournament),
    )


def _configured_qualifiers(tournament: Tournament) -> int | None:
    if (
        tournament.playoff_group_count
        and tournament.playoff_qualifiers_per_group
    ):
        return (
            tournament.playoff_group_count
            * tournament.playoff_qualifiers_per_group
        )
    return tournament.playoff_qualifier_count


def _shortfall(view: _TargetView) -> domain.Shortfall | None:
    """Return where the roster of the target gives less than configured."""
    tournament = view.tournament
    if view.target == INITIAL_TARGET:
        return _group_stage_shortfall(tournament, view.base)
    if view.target == PLAYOFF_TARGET and tournament.has_playoffs:
        roster_result = view.roster
        configured = _configured_qualifiers(tournament)
        if roster_result.is_err() or not configured:
            return None
        return domain.qualifier_shortfall(
            len(roster_result.unwrap().ids),
            configured,
            double_elimination=_playoff_double_elimination(tournament),
        )
    return None


def shortfall_notices(
    shortfall: domain.Shortfall | None,
) -> list[tuple[str, dict[str, int]]]:
    """Return the notice msgids and their params for a shortfall."""
    if shortfall is None:
        return []
    notices: list[tuple[str, dict[str, int]]] = []
    if (
        shortfall.groups is not None
        and shortfall.configured_groups is not None
        and 2 <= shortfall.groups < shortfall.configured_groups
    ):
        notices.append(
            (
                NOTICE_FEWER_GROUPS,
                {
                    'groups': shortfall.groups,
                    'configured': shortfall.configured_groups,
                },
            )
        )
    if shortfall.qualifiers < shortfall.configured_qualifiers:
        notices.append(
            (
                NOTICE_FEWER_QUALIFIERS,
                {
                    'qualifiers': shortfall.qualifiers,
                    'configured': shortfall.configured_qualifiers,
                },
            )
        )
    if shortfall.knockout_fallback:
        notices.append((NOTICE_KNOCKOUT_FALLBACK, {}))
    return notices


def start_notices(tournament: Tournament) -> list[tuple[str, dict[str, int]]]:
    """Return the shortfall notices the start of the tournament shows.

    Read-only: it takes no lock and never draws a draft.
    """
    if (
        tournament.tournament_status is not TournamentStatus.REGISTRATION_CLOSED
        or not tournament.has_playoffs
    ):
        return []
    roster = _roster(tournament)
    if tournament.game_format is GameFormat.HIGHSCORE:
        configured = tournament.playoff_qualifier_count
        if not configured:
            return []
        return shortfall_notices(
            domain.qualifier_shortfall(
                min(len(roster.ids), configured),
                configured,
                double_elimination=_playoff_double_elimination(tournament),
            )
        )
    return shortfall_notices(_group_stage_shortfall(tournament, roster))


def _undersized_pools(
    view: _TargetView,
) -> tuple[tournament_match_service.UndersizedPool, ...]:
    """Return the permitted undersized pools of an FFA round target.

    A generated round is judged by its lobbies, a draft by its plan.
    """
    if view.ffa_round is None:
        return ()
    tournament = view.tournament
    generated = tournament_repository.get_matches_for_seeding_target(
        tournament.id, view.target
    )
    if generated:
        return _generated_undersized_pools(tournament, generated)
    plan_result = view.plan
    if plan_result is None or plan_result.is_err():
        return ()
    return tournament_match_service.ffa_undersized_pools(
        tournament, plan_result.unwrap()
    )


def _bye_labels(view: _TargetView) -> tuple[str, ...]:
    """Return who gets a bye from the losers round that goes with a target.

    A contestant who has since joined a losers lobby no longer waits.
    """
    plan_result = view.plan
    if plan_result is None or plan_result.is_err():
        return ()
    byes = tournament_match_service.ffa_lobby_byes(plan_result.unwrap())
    if not byes:
        return ()
    losers_ids = [
        m.id
        for m in tournament_repository.get_matches_for_tournament(
            view.tournament.id
        )
        if m.bracket is Bracket.LOSERS
    ]
    placed = {
        contestant_id(c)
        for members in tournament_repository.get_contestants_for_matches(
            losers_ids
        ).values()
        for c in members
    }
    labels = view.base.labels
    return tuple(
        labels.get(b.contestant_id) or b.contestant_id
        for b in byes
        if b.contestant_id not in placed
    )


def _waiting_winner_labels(view: _TargetView) -> tuple[str, ...]:
    """Name the lone WB winner waiting while this target's LB pool plays."""
    if view.ffa_round is None or view.ffa_round[0] is not Bracket.WINNERS:
        return ()
    plan_result = view.plan
    if plan_result is None or plan_result.is_err():
        return ()
    plan = plan_result.unwrap()
    if not tournament_match_service.is_waiting_winners_round(plan):
        return ()
    labels = view.base.labels
    return tuple(labels.get(i) or i for i in plan.survivors)


def _generated_undersized_pools(
    tournament: Tournament, matches: Sequence[TournamentMatch]
) -> tuple[tournament_match_service.UndersizedPool, ...]:
    minimum = tournament.group_size_min or 2
    contestants = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches]
    )
    in_race = cache(
        lambda: tournament_match_service.removed_in_race(tournament)
    )

    def removed_from(pool: Bracket | None) -> Callable[[], int]:
        return lambda: in_race().count_for(pool)

    rounds: dict[tuple[Bracket | None, int], list[int]] = {}
    for match in sorted(matches, key=lambda m: m.group_order or 0):
        rounds.setdefault((match.bracket, match.round or 0), []).append(
            len(contestants.get(match.id, ()))
        )
    return tuple(
        tournament_match_service.UndersizedPool(
            bracket, round_number, sum(sizes), tuple(sizes), minimum,
            natural_shortfall=tournament_match_service.is_natural_shortfall(
                tournament,
                bracket,
                round_number,
                sizes,
                removed_from(bracket),
            ),
        )
        for (bracket, round_number), sizes in rounds.items()
        if min(sizes) < minimum
    )


def _origin_labels(roster: _Roster, state: SeedingState) -> dict[str, str]:
    """Return `A1`-style labels for the entrants that came out of a group."""
    labels = {}
    for cid, (scope, rank) in roster.origins.items():
        found = _GROUP_SCOPE.fullmatch(scope)
        if found is not None and cid in state.roster:
            labels[cid] = f'{chr(ord("A") + int(found.group(1)))}{rank}'
    return labels


def _same_group_matches(
    roster: _Roster, state: SeedingState
) -> tuple[int, ...]:
    if state.format not in _ELIMINATION_FORMATS or not roster.origins:
        return ()
    origin = {cid: scope for cid, (scope, _) in roster.origins.items()}
    return tuple(qualification_domain.same_group_matches(state.layout, origin))


def _problems(
    state: SeedingState,
    roster: _Roster,
    *,
    lobby_minimum: int | None = None,
    stall: FfaDeadEnd | None = None,
    cut: int | None = None,
) -> tuple[list[str], list[Mapping[str, Any]]]:
    msgids: list[str] = []
    params: list[Mapping[str, Any]] = []

    two_byes = iter(domain.two_bye_matches(state))
    for msgid in domain.layout_problems(state):
        if msgid == domain.PROBLEM_TWO_BYES:
            msgids.append(msgid)
            params.append({'n': next(two_byes)})
        elif (
            msgid == domain.PROBLEM_FEW_GROUPS
            and state.format is SeedingFormat.ROUND_ROBIN
            and state.param < 2
        ):
            # A plain round robin is a single group by design.
            continue
        else:
            msgids.append(msgid)
            params.append({})

    for name in roster.empty_team_names:
        msgids.append(PROBLEM_EMPTY_TEAM)
        params.append({'name': name})

    if state.format is SeedingFormat.FREE_FOR_ALL and lobby_minimum:
        sizes = _group_sizes(state)
        if sizes and 2 <= min(sizes) < lobby_minimum:
            msgids.append(PROBLEM_LOBBY_BELOW_MIN)
            params.append(
                {
                    'sizes': ', '.join(str(n) for n in sizes),
                    'minimum': lobby_minimum,
                }
            )

    if stall is not None:
        msgids.append(
            PROBLEM_FFA_STALLS
            if stall.reason == 'below_minimum'
            else PROBLEM_FFA_NO_PROGRESS
        )
        params.append(
            {
                'count': stall.count,
                'round': stall.round_number + 1,
                'sizes': ', '.join(str(n) for n in stall.lobby_sizes),
                'minimum': stall.minimum,
                'cut': cut,
            }
        )
    return msgids, params


def _lobby_minimum(view: _TargetView, state: SeedingState) -> int | None:
    """Return the minimum lobby size the engine enforces for the draft.

    `None` when the engine generates the lobbies anyway: a natural or
    removal-caused shortfall of a later round, a short playoff release.
    """
    tournament = view.tournament
    minimum = tournament.group_size_min
    if state.format is not SeedingFormat.FREE_FOR_ALL or not minimum:
        return None
    if view.ffa_round is not None:
        plan = view.plan
        if plan is not None and plan.is_ok():
            pool, round_number = view.ffa_round
            permitted = {
                (p.pool, p.round_number)
                for p in tournament_match_service.ffa_undersized_pools(
                    tournament, plan.unwrap()
                )
            }
            if (pool, round_number) in permitted:
                return None
        return minimum
    if view.target == PLAYOFF_TARGET:
        if len(state.roster) < (tournament.playoff_qualifier_count or 0):
            return None
    return minimum


def _initial_stall(view: _TargetView, state: SeedingState) -> FfaDeadEnd | None:
    """Return the stall of a plain FFA single track drafted at the initial target."""
    if (
        view.target != INITIAL_TARGET
        or state.format is not SeedingFormat.FREE_FOR_ALL
    ):
        return None
    return tournament_match_service.ffa_stall_for(
        view.tournament, len(state.roster)
    )


def _locked_reason(view: _TargetView) -> str:
    if view.ffa_round is not None:
        return (
            view.lock_reason
            or tournament_match_service.FFA_ROUND_LOCKED_ERROR
        )
    return ERR_PLAYOFF_LOCKED if view.target == PLAYOFF_TARGET else ERR_LOCKED


def _is_locked(view: _TargetView) -> bool:
    """Tell whether the target's draft can no longer be changed."""
    tournament = view.tournament
    if view.ffa_round is not None:
        return view.lock_reason is not None
    if view.target == PLAYOFF_TARGET:
        return tournament.tournament_status in (
            TournamentStatus.COMPLETED,
            TournamentStatus.CANCELLED,
        ) or view.phase_two.has_result
    return tournament.tournament_status in _LOCKED_STATUSES


def _generation_status(
    view: _TargetView, seeding: TournamentSeeding
) -> GenerationStatus:
    status = view.tournament.tournament_status
    if seeding.target == PLAYOFF_TARGET:
        if _is_locked(view):
            return GenerationStatus.LOCKED
        if status is not TournamentStatus.ONGOING:
            return GenerationStatus.NOT_AVAILABLE
        if not view.phase_two.generated:
            return GenerationStatus.NOT_GENERATED
    elif view.ffa_round is not None:
        if _is_locked(view):
            return GenerationStatus.LOCKED
        if status is not TournamentStatus.ONGOING:
            return GenerationStatus.NOT_AVAILABLE
    else:
        if status in _LOCKED_STATUSES:
            return GenerationStatus.LOCKED
        if status is not TournamentStatus.REGISTRATION_CLOSED:
            return GenerationStatus.NOT_AVAILABLE
    if seeding.generated_seed_code is None:
        return GenerationStatus.NOT_GENERATED
    if seeding.generated_seed_code == seeding.seed_code:
        return GenerationStatus.MATCHES
    return GenerationStatus.DIFFERS
