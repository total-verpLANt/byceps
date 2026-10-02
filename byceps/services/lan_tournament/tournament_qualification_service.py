"""
byceps.services.lan_tournament.tournament_qualification_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any, NamedTuple
import re
import unicodedata

from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import (
    signals,
    tournament_log_service,
    tournament_match_service,
    tournament_qualification_domain_service as domain,
    tournament_qualification_repository,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
)
from .events import MatchDeletedEvent, TournamentCompletedEvent
from .models.bracket import Bracket
from .models.elimination_mode import EliminationMode
from .models.game_format import GameFormat
from .models.playoff import PlayoffReleaseMode
from .models.score_ordering import ScoreOrdering
from .models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
    QualificationDecisionID,
)
from .models.tournament import Tournament, TournamentID
from .models.tournament_status import TournamentStatus
from .models.tournament_match import TournamentMatch
from .tournament_qualification_domain_service import (
    Qualifier,
    Ranking,
    TieBlock,
    TieKind,
)
from .tournament_seeding_domain_service import MIN_DOUBLE_ELIMINATION


SOURCE_GROUPS = 'groups'
SOURCE_LEADERBOARD = 'leaderboard'
SOURCE_WINNER = 'winner'
SOURCE_FFA = 'ffa'

MAX_REASON_LENGTH = 500
MIN_DOUBLE_ELIMINATION_QUALIFIERS = MIN_DOUBLE_ELIMINATION

ERR_ALREADY_RELEASED = 'The playoffs are already released.'
ERR_NOT_RELEASED = 'The playoffs are not released.'
REASON_MISSING_ERROR = 'Please give a reason for the decision.'
UNRELEASE_REASON_MISSING_ERROR = (
    'Please give a reason for taking the release back.'
)
ERR_CANNOT_UNRELEASE = (
    'The release cannot be taken back: a playoff match already has a result.'
)
ERR_REMOVED_QUALIFIER = (
    'The playoff draft contains a contestant who is no longer in the'
    ' tournament. Re-seed the playoffs first.'
)
ERR_FFA_ROUND_BUILT = (
    'A later round has already been built from this lobby, so its tie'
    ' decision can no longer be changed.'
)
ERR_DECISION_COMPLETED = (
    'The tournament is completed. Take back a result first.'
)
ERR_DECISION_CANCELLED = (
    'The tournament is cancelled. Tie decisions can no longer change.'
)
ERR_UNRELEASE_STATUS = (
    'The release can only be taken back while the tournament is ongoing or'
    ' paused.'
)

_BIDI_CONTROLS = frozenset(
    chr(code)
    for code in (
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    )
)
_FORBIDDEN_CATEGORIES = frozenset({'Cc', 'Zl', 'Zp'})
_INVISIBLE_CATEGORIES = frozenset({'Zs', 'Zl', 'Zp', 'Cc', 'Cf', 'Mn', 'Me'})
_INVISIBLE_LETTERS = frozenset('\u115f\u1160\u2800\u3164\uffa0')

_FFA_SCOPE = re.compile(r'ffa:(SE|WB|LB):([0-9]{1,4}):([0-9]{1,4})')
_FFA_POOLS = {'SE': None, 'WB': Bracket.WINNERS, 'LB': Bracket.LOSERS}


@dataclass(frozen=True, kw_only=True)
class QualificationState:
    tournament_id: TournamentID
    source: str
    rankings: tuple[Ranking, ...]
    blockers: tuple[TieBlock, ...]
    open_match_count: int
    total_match_count: int
    ready: bool
    qualifiers: tuple[Qualifier, ...] | None
    released_at: datetime | None
    released_by: UserID | None
    release_mode: PlayoffReleaseMode
    auto_release_suspended: bool
    can_unrelease: bool
    crossover: Ranking | None = None
    seed_order: tuple[Qualifier, ...] | None = None
    configured_qualifier_count: int | None = None
    de_fallback: bool = False


@dataclass(frozen=True, kw_only=True)
class FfaTiedContestant:
    contestant_id: str
    points: int


@dataclass(frozen=True, kw_only=True)
class FfaCutTie:
    """A tie across the cut of one confirmed FFA lobby.

    The contestants are in the decided order once `decided`, else in the
    order the lobby ranking lists them. `decision` is the block that
    decides this tie. `locked` is set when a later round was built from
    the lobby, so the decision can no longer change.
    """

    scope: str
    pool: str
    round_number: int
    lobby: int
    rank_from: int
    rank_to: int
    contestants: tuple[FfaTiedContestant, ...]
    decided: bool
    decision: DecisionBlock | None
    locked: bool


@dataclass(frozen=True, kw_only=True)
class FfaOutdatedDecision:
    """A stored block that decides no tie of its lobby any more."""

    scope: str
    pool: str
    round_number: int
    lobby: int
    block: DecisionBlock
    locked: bool


class FfaDecisionReport(NamedTuple):
    """The cut ties and the outdated decisions of the current FFA round."""

    ties: tuple[FfaCutTie, ...]
    outdated: tuple[FfaOutdatedDecision, ...]


class PhaseTwoProgress(NamedTuple):
    """Whether phase 2 has matches, and whether any has a played result."""

    generated: bool
    has_result: bool


def get_qualification(
    tournament_id: TournamentID,
) -> Result[QualificationState, str]:
    """Return who qualifies for the playoffs, and what blocks it."""
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    return _state_for(tournament)


def get_ffa_cut_ties(tournament_id: TournamentID) -> tuple[FfaCutTie, ...]:
    """Return the cut ties of the confirmed lobbies in the current FFA round.

    See `get_ffa_decision_report`.
    """
    return get_ffa_decision_report(tournament_id).ties


def get_ffa_decision_report(tournament_id: TournamentID) -> FfaDecisionReport:
    """Return the cut ties and outdated decisions of the current FFA round.

    The current round is the latest one of each pool. A lobby whose round
    was built upon reports only its decided ties: an undecided tie there
    blocks nothing and can no longer be decided. Its outdated blocks are
    still reported. Reads only: it takes no lock and writes nothing.
    """
    tournament = tournament_repository.find_tournament(tournament_id)
    if (
        tournament is None
        or tournament_match_service._ffa_phase(tournament) is None
    ):
        return FfaDecisionReport((), ())

    latest: dict[Bracket | None, int] = {}
    matches = tournament_repository.get_matches_for_tournament_ordered(
        tournament.id
    )
    for match in matches:
        if match.bracket is Bracket.GRAND_FINAL or match.round is None:
            continue
        latest[match.bracket] = max(latest.get(match.bracket, 0), match.round)

    repository = tournament_qualification_repository
    decisions = repository.get_decisions_for_tournament(tournament.id)
    ties: list[FfaCutTie] = []
    outdated: list[FfaOutdatedDecision] = []
    for match in sorted(
        (m for m in matches if latest.get(m.bracket) == m.round),
        key=lambda m: (
            tournament_match_service.ffa_pool_token(m.bracket),
            m.group_order or 0,
        ),
    ):
        if match.bracket is Bracket.GRAND_FINAL:
            continue
        scope = tournament_match_service.ffa_lobby_scope(match)
        match _ffa_state(tournament, scope):
            case Err(_):
                continue
            case Ok(state):
                pass
        ranking = state.rankings[0]
        points = {e.contestant_id: e.value or 0 for e in ranking.entries}
        ranked = set(points)
        pool = tournament_match_service.ffa_pool_token(match.bracket)
        locked = tournament_match_service.ffa_round_consumed(match, tournament)
        decision = decisions.get(scope)
        blocks = decision.blocks if decision else ()
        for tie in ranking.ties:
            if tie.kind is not TieKind.CUT:
                continue
            if locked and not tie.decided:
                continue
            members = set(tie.contestant_ids)
            ties.append(
                FfaCutTie(
                    scope=scope,
                    pool=pool,
                    round_number=match.round or 0,
                    lobby=match.group_order or 0,
                    rank_from=tie.rank_from,
                    rank_to=tie.rank_to,
                    contestants=tuple(
                        FfaTiedContestant(
                            contestant_id=cid, points=points.get(cid, 0)
                        )
                        for cid in tie.contestant_ids
                    ),
                    decided=tie.decided,
                    decision=next(
                        (
                            b
                            for b in blocks
                            if tie.decided
                            and {c for c in b.contestant_ids if c in ranked}
                            == members
                        ),
                        None,
                    ),
                    locked=locked,
                )
            )
        outdated.extend(
            FfaOutdatedDecision(
                scope=scope,
                pool=pool,
                round_number=match.round or 0,
                lobby=match.group_order or 0,
                block=block,
                locked=locked,
            )
            for block in blocks
            if block.contestant_ids in ranking.outdated
        )
    return FfaDecisionReport(tuple(ties), tuple(outdated))


def get_phase_two_progress(tournament_id: TournamentID) -> PhaseTwoProgress:
    """Return how far phase 2 has come."""
    matches = [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament_id)
        if m.phase == 2
    ]
    contestants = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches if m.confirmed_by is not None]
    )
    return PhaseTwoProgress(bool(matches), _has_result(matches, contestants))


def release_playoffs(
    tournament_id: TournamentID,
    *,
    expected_version: int,
    initiator_id: UserID,
) -> Result[int, str]:
    """Release the playoffs by hand; return the number of phase-2 matches.

    `expected_version` is the version of the playoff draft the orga saw.
    On `Err`, the session has been rolled back.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    result = _release_locked(
        tournament_id,
        expected_version=expected_version,
        initiator_id=initiator_id,
    )
    return _finish_release(tournament_id, result)


def try_auto_release(
    tournament_id: TournamentID, *, triggered_by: UserID
) -> Result[bool, str]:
    """Release the playoffs if they are due; return whether they were.

    Due means an automatic release mode, not suspended, not yet
    released, and a ready qualification. The release is logged with no
    actor, the system; `triggered_by`, the user whose change made it
    due, confirms the byes and is recorded in the log data. In the
    manual mode nothing is released, but a ready qualification gets its
    playoff draft here, so that a page view never has to write it. Call
    it after the commit of the change that may have made the
    qualification ready.
    """
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    if _manual_draft_due(tournament):
        _stage_manual_draft(tournament)
        return Ok(False)
    if not _auto_release_due(tournament):
        return Ok(False)

    tournament_repository.lock_tournament_for_update(tournament_id)
    result = _auto_release_locked(tournament_id, triggered_by)
    if result.is_err():
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())
    outcome = result.unwrap()
    if outcome is None:
        tournament_repository.rollback_session()
        return Ok(False)

    tournament_repository.commit_session()
    tournament_match_service.dispatch_generation_events(tournament_id, outcome)
    return Ok(True)


def unrelease_playoffs(
    tournament_id: TournamentID,
    *,
    reason: str,
    initiator_id: UserID,
) -> Result[None, str]:
    """Take the release back and delete the phase-2 matches.

    Refused once a phase-2 match has a result. After an automatic
    release, the automatic release is suspended. On `Err`, the session
    has been rolled back.
    """
    match validate_reason(reason, missing=UNRELEASE_REASON_MISSING_ERROR):
        case Err(e):
            return Err(e)
        case Ok(clean_reason):
            pass

    tournament_repository.lock_tournament_for_update(tournament_id)
    result = _unrelease_locked(tournament_id, clean_reason, initiator_id)
    if result.is_err():
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    tournament_repository.commit_session()
    for event in result.unwrap():
        signals.match_deleted.send(None, event=event)
    return Ok(None)


def save_decision(
    tournament_id: TournamentID,
    scope: str,
    ordered_ids: list[str] | tuple[str, ...],
    *,
    reason: str,
    initiator_id: UserID,
) -> Result[None, str]:
    """Order the contestants of an open tie, with the reason.

    On `Err`, the session has been rolled back.
    """
    result = _save_decision_impl(
        tournament_id, scope, ordered_ids, reason, initiator_id
    )
    if result.is_err():
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())
    tournament_repository.commit_session()
    completed_event = result.unwrap()
    if completed_event is not None:
        signals.tournament_completed.send(None, event=completed_event)
    try_auto_release(tournament_id, triggered_by=initiator_id)
    return Ok(None)


def withdraw_decision(
    tournament_id: TournamentID,
    scope: str,
    *,
    contestant_ids: Sequence[str],
    reason: str,
    initiator_id: UserID,
) -> Result[None, str]:
    """Remove the one decision block of a scope that orders `contestant_ids`.

    On `Err`, the session has been rolled back.
    """
    result = _withdraw_decision_impl(
        tournament_id, scope, contestant_ids, reason, initiator_id
    )
    if result.is_err():
        tournament_repository.rollback_session()
        return result
    tournament_repository.commit_session()
    try_auto_release(tournament_id, triggered_by=initiator_id)
    return result


def _save_decision_impl(
    tournament_id: TournamentID,
    scope: str,
    ordered_ids: list[str] | tuple[str, ...],
    reason: str,
    initiator_id: UserID,
) -> Result[TournamentCompletedEvent | None, str]:
    match validate_reason(reason):
        case Err(e):
            return Err(e)
        case Ok(clean_reason):
            pass

    ids = tuple(str(i) for i in ordered_ids)

    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    if (err := _terminal_decision_error(tournament)) is not None:
        return Err(err)
    if tournament.playoff_released_at is not None and not _is_ffa_scope(scope):
        return Err('Tie decisions are locked after the playoffs are released.')
    if _ffa_round_built(tournament, scope):
        return Err(ERR_FFA_ROUND_BUILT)

    match _state_for_scope(tournament, scope):
        case Err(e):
            return Err(e)
        case Ok(state):
            pass

    match _find_tie(state, scope, ids):
        case Err(e):
            return Err(e)
        case Ok(block):
            pass

    existing = tournament_qualification_repository.find_decision(
        tournament_id, scope
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    new_block = DecisionBlock(
        contestant_ids=ids,
        reason=clean_reason,
        decided_by=initiator_id,
        decided_at=now,
    )
    # `_find_tie` accepts only an undecided tie, so a stored block that
    # overlaps it is an outdated one: replace those.
    previous = existing.blocks if existing else ()
    superseded = tuple(
        b for b in previous if not set(b.contestant_ids).isdisjoint(ids)
    )
    kept = tuple(b for b in previous if set(b.contestant_ids).isdisjoint(ids))

    tournament_qualification_repository.upsert_decision(
        QualificationDecision(
            id=QualificationDecisionID(generate_uuid7()),
            tournament_id=tournament_id,
            scope=scope,
            blocks=kept + (new_block,),
            reason=clean_reason,
            decided_by=initiator_id,
            decided_at=now,
        )
    )
    audit_data: dict[str, Any] = {
        'scope': scope,
        'contestant_ids': list(ids),
        'reason': clean_reason,
        'rank_from': block.rank_from,
        'rank_to': block.rank_to,
    }
    if superseded:
        audit_data['superseded'] = [list(b.contestant_ids) for b in superseded]
    tournament_log_service.create_log_entry(
        'qualification-tie-decided',
        tournament_id,
        initiator_id,
        data=audit_data,
        commit=False,
    )
    if tournament_match_service.is_plain_round_robin(tournament):
        return tournament_match_service.try_complete_plain_round_robin(
            tournament
        )
    return Ok(None)


def _block_places(
    tournament: Tournament, scope: str, block: DecisionBlock
) -> dict[str, int]:
    """Return the places of the decided tie that `block` orders.

    Empty if the block is outdated, so it decides no tie of the scope.
    """
    match _state_for_scope(tournament, scope):
        case Err(_):
            return {}
        case Ok(state):
            pass
    for ranking in _scope_rankings(state):
        if ranking.scope != scope:
            continue
        ranked = {e.contestant_id for e in ranking.entries}
        members = {c for c in block.contestant_ids if c in ranked}
        for tie in ranking.ties:
            if tie.decided and set(tie.contestant_ids) == members:
                return {
                    'rank_from': tie.rank_from,
                    'rank_to': tie.rank_to,
                }
    return {}


def _withdraw_decision_impl(
    tournament_id: TournamentID,
    scope: str,
    contestant_ids: Sequence[str],
    reason: str,
    initiator_id: UserID,
) -> Result[None, str]:
    match validate_reason(reason):
        case Err(e):
            return Err(e)
        case Ok(clean_reason):
            pass

    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    if (err := _terminal_decision_error(tournament)) is not None:
        return Err(err)
    if tournament.playoff_released_at is not None and not _is_ffa_scope(scope):
        return Err('Tie decisions are locked after the playoffs are released.')
    if _ffa_round_built(tournament, scope):
        return Err(ERR_FFA_ROUND_BUILT)

    decision = tournament_qualification_repository.find_decision(
        tournament_id, scope
    )
    if decision is None:
        return Err('There is no decision to withdraw.')

    wanted = {str(i) for i in contestant_ids}
    block = next(
        (
            b
            for b in decision.blocks
            if wanted and set(b.contestant_ids) == wanted
        ),
        None,
    )
    if block is None:
        return Err('There is no decision to withdraw.')

    places = _block_places(tournament, scope, block)
    remaining = tuple(b for b in decision.blocks if b is not block)
    if remaining:
        newest = max(remaining, key=lambda b: b.decided_at)
        tournament_qualification_repository.upsert_decision(
            QualificationDecision(
                id=decision.id,
                tournament_id=tournament_id,
                scope=scope,
                blocks=remaining,
                reason=newest.reason,
                decided_by=newest.decided_by,
                decided_at=newest.decided_at,
            )
        )
    else:
        tournament_qualification_repository.delete_decision(
            tournament_id, scope
        )
    withdrawn: dict[str, Any] = {
        'scope': scope,
        'contestant_ids': list(block.contestant_ids),
        'reason': clean_reason,
        'decision_reason': block.reason,
        **places,
    }
    if not places:
        withdrawn['outdated'] = True
    tournament_log_service.create_log_entry(
        'qualification-tie-withdrawn',
        tournament_id,
        initiator_id,
        data=withdrawn,
        commit=False,
    )
    return Ok(None)


def _finish_release(
    tournament_id: TournamentID,
    result: Result[tournament_match_service.GenerationOutcome, str],
) -> Result[int, str]:
    if result.is_err():
        tournament_repository.rollback_session()
        return Err(result.unwrap_err())

    outcome = result.unwrap()
    tournament_repository.commit_session()
    tournament_match_service.dispatch_generation_events(tournament_id, outcome)
    return Ok(outcome.count)


def _auto_release_due(tournament: Tournament) -> bool:
    return (
        tournament.has_playoffs
        and tournament.playoff_release_mode is PlayoffReleaseMode.AUTOMATIC
        and not tournament.playoff_auto_release_suspended
        and tournament.playoff_released_at is None
    )


def _manual_draft_due(tournament: Tournament) -> bool:
    """Tell whether the release waits for an orga, so the draft is kept."""
    return (
        tournament.has_playoffs
        and (
            tournament.playoff_release_mode is PlayoffReleaseMode.MANUAL
            or tournament.playoff_auto_release_suspended
        )
        and tournament.playoff_released_at is None
        and tournament.tournament_status is TournamentStatus.ONGOING
    )


def _stage_manual_draft(tournament: Tournament) -> None:
    """Create the playoff draft once the qualification is ready."""
    state = _state_for(tournament)
    if state.is_err():
        return
    ready = state.unwrap()
    if ready.ready and ready.source != SOURCE_WINNER:
        tournament_seeding_service.ensure_playoff_draft(tournament.id)


def _auto_release_locked(
    tournament_id: TournamentID, triggered_by: UserID
) -> Result[tournament_match_service.GenerationOutcome | None, str]:
    """Release if still due under the lock; `Ok(None)` if nothing to do."""
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    if not _auto_release_due(tournament):
        return Ok(None)

    match _compute_state(tournament):
        case Err(e):
            return Err(e)
        case Ok(state):
            pass
    if not state.ready:
        return Ok(None)

    released = _release_locked(
        tournament_id,
        expected_version=None,
        initiator_id=None,
        triggered_by=triggered_by,
    )
    if released.is_err():
        return Err(released.unwrap_err())
    return Ok(released.unwrap())


def falls_back_to_single_elimination(
    tournament: Tournament, qualifier_count: int
) -> bool:
    """Tell whether a double-elimination playoff runs as single knockout."""
    return (
        tournament.playoff_game_format is GameFormat.ONE_V_ONE
        and tournament.playoff_elimination_mode
        is EliminationMode.DOUBLE_ELIMINATION
        and 2 <= qualifier_count < MIN_DOUBLE_ELIMINATION_QUALIFIERS
    )


def _release_locked(
    tournament_id: TournamentID,
    *,
    expected_version: int | None,
    initiator_id: UserID | None,
    triggered_by: UserID | None = None,
) -> Result[tournament_match_service.GenerationOutcome, str]:
    """Generate phase 2 and record the release; flush only.

    `expected_version` is `None` for the automatic release, which takes
    the draft as it finds it. The automatic release has no
    `initiator_id`; `triggered_by` confirms the byes instead.
    """
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    if tournament.playoff_released_at is not None:
        return Err(ERR_ALREADY_RELEASED)

    match _compute_state(tournament):
        case Err(e):
            return Err(e)
        case Ok(state):
            pass
    if not state.ready:
        return Err(tournament_seeding_service.ERR_PLAYOFF_NOT_READY)

    qualifier_count = len(state.qualifiers or ())
    fallback = falls_back_to_single_elimination(tournament, qualifier_count)
    if fallback:
        tournament_repository.set_playoff_elimination_mode(
            tournament_id, EliminationMode.SINGLE_ELIMINATION
        )

    draft_result = tournament_seeding_service.stage_playoff_draft(
        tournament_id, initiator_id
    )
    if draft_result.is_err():
        return Err(draft_result.unwrap_err())

    draft = draft_result.unwrap()
    active = tournament_match_service.active_contestant_ids(tournament_id)
    if any(entry.id not in active for entry in draft.roster_snapshot):
        return Err(ERR_REMOVED_QUALIFIER)

    if expected_version is None:
        expected_version = draft.version
    outcome_result = tournament_seeding_service.stage_generation(
        tournament_id,
        tournament_seeding_service.PLAYOFF_TARGET,
        expected_version=expected_version,
        initiator_id=initiator_id,
        confirmer_id=initiator_id or triggered_by,
    )
    if outcome_result.is_err():
        return outcome_result
    outcome = outcome_result.unwrap()

    tournament_repository.set_playoff_release(
        tournament_id,
        released_at=datetime.now(UTC).replace(tzinfo=None),
        # `None` is the system, for the automatic release.
        released_by=initiator_id,  # type: ignore[arg-type]
    )
    data: dict[str, Any] = {
        'mode': 'manual' if initiator_id is not None else 'automatic',
        'match_count': outcome.count,
        'qualifier_count': qualifier_count,
    }
    if initiator_id is None and triggered_by is not None:
        data['triggered_by'] = str(triggered_by)
    tournament_log_service.create_log_entry(
        'playoffs-released',
        tournament_id,
        initiator_id,
        data=data,
        commit=False,
    )
    audit: dict[str, Any] = {}
    if initiator_id is None and triggered_by is not None:
        audit['triggered_by'] = str(triggered_by)
    configured = state.configured_qualifier_count
    if configured is not None and qualifier_count < configured:
        tournament_log_service.create_log_entry(
            'playoffs-shortfall',
            tournament_id,
            initiator_id,
            data={
                'configured': configured,
                'qualified': qualifier_count,
                **audit,
            },
            commit=False,
        )
    if fallback:
        tournament_log_service.create_log_entry(
            'playoffs-de-fallback',
            tournament_id,
            initiator_id,
            data={
                'qualified': qualifier_count,
                'from': EliminationMode.DOUBLE_ELIMINATION.name,
                'to': EliminationMode.SINGLE_ELIMINATION.name,
                **audit,
            },
            commit=False,
        )
    return Ok(outcome)


def _can_unrelease_status(tournament: Tournament) -> bool:
    return (
        tournament.tournament_status
        in (TournamentStatus.ONGOING, TournamentStatus.PAUSED)
        and tournament.winner_participant_id is None
        and tournament.winner_team_id is None
    )


def _terminal_decision_error(tournament: Tournament) -> str | None:
    if tournament.tournament_status is TournamentStatus.COMPLETED:
        return ERR_DECISION_COMPLETED
    if tournament.tournament_status is TournamentStatus.CANCELLED:
        return ERR_DECISION_CANCELLED
    return None


def _unrelease_locked(
    tournament_id: TournamentID, reason: str, initiator_id: UserID
) -> Result[list[MatchDeletedEvent], str]:
    tournament = tournament_repository.find_tournament(tournament_id)
    if tournament is None:
        return Err('Tournament not found.')
    if tournament.playoff_released_at is None:
        return Err(ERR_NOT_RELEASED)
    if not _can_unrelease_status(tournament):
        return Err(ERR_UNRELEASE_STATUS)

    match _compute_state(tournament):
        case Err(e):
            return Err(e)
        case Ok(state):
            pass
    if not state.can_unrelease:
        return Err(ERR_CANNOT_UNRELEASE)

    suspend_auto = (
        tournament.playoff_release_mode is PlayoffReleaseMode.AUTOMATIC
    )
    deleted_events = tournament_match_service.clear_bracket(
        tournament_id, phase=2, initiator_id=initiator_id
    )
    tournament_repository.clear_playoff_release(
        tournament_id, suspend_auto=suspend_auto
    )
    tournament_log_service.create_log_entry(
        'playoffs-unreleased',
        tournament_id,
        initiator_id,
        data={'reason': reason, 'auto_release_suspended': suspend_auto},
        commit=False,
    )
    return Ok(deleted_events)


def _has_result(phase_two: list[TournamentMatch], contestants: dict) -> bool:
    """Tell whether a phase-2 match was played and confirmed.

    Bye matches are confirmed by the generator with one contestant and
    no result, so they do not count.
    """
    return any(
        m.confirmed_by is not None and len(contestants.get(m.id, ())) >= 2
        for m in phase_two
    )


def validate_reason(
    reason: str, *, missing: str = REASON_MISSING_ERROR
) -> Result[str, str]:
    """Return the reason with normalised line breaks, or an `Err`.

    A reason may hold line breaks, but needs at least one visible
    character, else `missing` is the `Err`.
    """
    clean = (
        reason.replace('\r\n', '\n').replace('\r', '\n').strip()
        if isinstance(reason, str)
        else ''
    )
    if not any(_is_visible(char) for char in clean):
        return Err(missing)
    if len(clean) > MAX_REASON_LENGTH:
        return Err('The reason is too long.')
    for char in clean:
        if char != '\n' and (
            unicodedata.category(char) in _FORBIDDEN_CATEGORIES
            or char in _BIDI_CONTROLS
        ):
            return Err('The reason must not contain control characters.')
    return Ok(clean)


def _is_visible(char: str) -> bool:
    return (
        unicodedata.category(char) not in _INVISIBLE_CATEGORIES
        and char not in _INVISIBLE_LETTERS
    )


def _scope_rankings(state: QualificationState) -> tuple[Ranking, ...]:
    """Return every ranking a decision can order, the crossover included."""
    if state.crossover is None:
        return state.rankings
    return (*state.rankings, state.crossover)


def _find_tie(
    state: QualificationState, scope: str, ids: tuple[str, ...]
) -> Result[TieBlock, str]:
    """Return the open blocking tie of the scope that `ids` orders."""
    scope_ties = [
        tie
        for ranking in _scope_rankings(state)
        if ranking.scope == scope
        for tie in ranking.ties
        if tie.kind is not TieKind.HARMLESS
    ]
    if not scope_ties:
        return Err('There is no tie to decide in this scope.')

    open_ties = [t for t in scope_ties if not t.decided]
    if not open_ties:
        return Err('This tie is already decided. Withdraw the decision first.')

    if len(set(ids)) != len(ids):
        return Err('The order must list every tied contestant exactly once.')
    for tie in open_ties:
        if set(tie.contestant_ids) == set(ids):
            return Ok(tie)
    return Err('The order must list exactly the tied contestants.')


def _is_ffa_scope(scope: str) -> bool:
    return scope.startswith('ffa:')


def _state_for_scope(
    tournament: Tournament, scope: str
) -> Result[QualificationState, str]:
    if _is_ffa_scope(scope):
        return _ffa_state(tournament, scope)
    return _state_for(tournament)


def _ffa_scope_lobby(
    tournament: Tournament, scope: str
) -> TournamentMatch | None:
    """Return the one lobby an `ffa:<pool>:<round>:<group>` scope names."""
    found = _FFA_SCOPE.fullmatch(scope)
    if found is None:
        return None
    lobbies = [
        m
        for m in tournament_repository.get_matches_for_round(
            tournament.id,
            int(found.group(2)),
            bracket=_FFA_POOLS[found.group(1)],
        )
        if (m.group_order or 0) == int(found.group(3))
    ]
    return lobbies[0] if len(lobbies) == 1 else None


def _ffa_round_built(tournament: Tournament, scope: str) -> bool:
    """Return `True` if a later FFA round was built from the scope's lobby."""
    if not _is_ffa_scope(scope):
        return False
    lobby = _ffa_scope_lobby(tournament, scope)
    if lobby is None:
        return False
    return tournament_match_service.ffa_round_consumed(lobby, tournament)


def _ffa_state(
    tournament: Tournament, scope: str
) -> Result[QualificationState, str]:
    """Return the ranking of one confirmed FFA lobby.

    The scope is `ffa:<pool>:<round>:<group>`. Only a tie across the cut
    blocks; ties above it may still be ordered.
    """
    no_tie = Err('There is no tie to decide in this scope.')
    found = _FFA_SCOPE.fullmatch(scope)
    cut = tournament.advancement_count
    if (
        found is None
        or tournament_match_service._ffa_phase(tournament) is None
        or cut is None
        or cut < 1
    ):
        return no_tie

    lobby = _ffa_scope_lobby(tournament, scope)
    if lobby is None or lobby.confirmed_by is None:
        return no_tie

    ranking = tournament_match_service.rank_ffa_lobby(
        lobby,
        tournament_repository.get_contestants_for_match(lobby.id),
        cut,
        tournament_match_service.ffa_decisions(tournament.id),
    )
    blockers = tuple(
        t for t in domain.blocking_ties([ranking]) if t.kind is TieKind.CUT
    )
    return Ok(
        QualificationState(
            tournament_id=tournament.id,
            source=SOURCE_FFA,
            rankings=(ranking,),
            blockers=blockers,
            open_match_count=0,
            total_match_count=1,
            ready=not blockers,
            qualifiers=None,
            released_at=None,
            released_by=None,
            release_mode=tournament.playoff_release_mode
            or PlayoffReleaseMode.MANUAL,
            auto_release_suspended=False,
            can_unrelease=False,
        )
    )


def _state_for(tournament: Tournament) -> Result[QualificationState, str]:
    if tournament_match_service.is_plain_round_robin(tournament):
        return Ok(_winner_state(tournament))
    return _compute_state(tournament)


def _winner_state(tournament: Tournament) -> QualificationState:
    """Return the winner tie of a plain round robin as a blocker."""
    standing = tournament_match_service.plain_round_robin_standing(tournament)
    ranking = standing.ranking
    blockers = domain.blocking_ties([ranking]) if ranking.entries else ()
    return QualificationState(
        tournament_id=tournament.id,
        source=SOURCE_WINNER,
        rankings=(ranking,),
        blockers=blockers,
        open_match_count=standing.open_match_count,
        total_match_count=standing.total_match_count,
        ready=bool(standing.total_match_count)
        and not standing.open_match_count
        and not blockers,
        qualifiers=None,
        released_at=None,
        released_by=None,
        release_mode=tournament.playoff_release_mode
        or PlayoffReleaseMode.MANUAL,
        auto_release_suspended=False,
        can_unrelease=False,
    )


def _compute_state(tournament: Tournament) -> Result[QualificationState, str]:
    if not tournament.has_playoffs:
        return Err('This tournament has no playoff phase.')
    if tournament.playoff_release_mode is None:
        return Err('Please choose how the playoffs are released.')

    if tournament.game_format == GameFormat.HIGHSCORE:
        return _leaderboard_state(tournament, tournament.playoff_release_mode)
    return _groups_state(tournament, tournament.playoff_release_mode)


def _leaderboard_state(
    tournament: Tournament, release_mode: PlayoffReleaseMode
) -> Result[QualificationState, str]:
    """Return the top of a closed leaderboard and the ties that block it.

    Equal values tie, whenever they were submitted; only an orga
    decision orders them. The state is not ready before the orga
    closes the leaderboard, and has no matches to count.
    """
    cut = tournament.playoff_qualifier_count
    if cut is None:
        return Err('Please enter the number of qualifiers.')

    match tournament_score_service.get_qualification_values(tournament.id):
        case Err(e):
            return Err(e)
        case Ok(values):
            pass
    active = tournament_match_service.active_contestant_ids(tournament.id)
    values = {c: v for c, v in values.items() if c in active}
    decision = tournament_qualification_repository.get_decisions_for_tournament(
        tournament.id
    ).get(SOURCE_LEADERBOARD)
    ranking = domain.classify_ties(
        domain.rank_by_value(
            SOURCE_LEADERBOARD,
            values,
            higher_is_better=(
                tournament.score_ordering is ScoreOrdering.HIGHER_IS_BETTER
            ),
            orders=decision.orders if decision else (),
        ),
        cut=cut,
        plain_winner=False,
    )
    blockers = domain.blocking_ties([ranking])

    qualifiers = None
    closed = isinstance(tournament.leaderboard_closed_at, datetime)
    if closed and not blockers:
        match domain.qualifiers_top_k(ranking, cut):
            case Ok(found) if len(found) >= 2:
                qualifiers = found
            case _:
                pass

    return Ok(
        QualificationState(
            tournament_id=tournament.id,
            source=SOURCE_LEADERBOARD,
            rankings=(ranking,),
            blockers=blockers,
            open_match_count=0,
            total_match_count=0,
            ready=qualifiers is not None,
            qualifiers=qualifiers,
            released_at=tournament.playoff_released_at,
            released_by=tournament.playoff_released_by,
            release_mode=release_mode,
            auto_release_suspended=tournament.playoff_auto_release_suspended,
            can_unrelease=tournament.playoff_released_at is not None
            and _can_unrelease_status(tournament)
            and not get_phase_two_progress(tournament.id).has_result,
            seed_order=qualifiers,
            configured_qualifier_count=cut,
            de_fallback=qualifiers is not None
            and falls_back_to_single_elimination(tournament, len(qualifiers)),
        )
    )


def _groups_state(
    tournament: Tournament, release_mode: PlayoffReleaseMode
) -> Result[QualificationState, str]:
    cut = tournament.playoff_qualifiers_per_group
    if cut is None:
        return Err('Please enter how many advance from each group.')

    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    decisions = (
        tournament_qualification_repository.get_decisions_for_tournament(
            tournament.id
        )
    )

    active = tournament_match_service.active_contestant_ids(tournament.id)
    phase_one = [m for m in matches if m.phase == 1]
    phase_two = [m for m in matches if m.phase == 2]
    by_group: dict[int, list[TournamentMatch]] = {}
    for match in phase_one:
        by_group.setdefault(match.group_order or 0, []).append(match)

    rankings = []
    for group in sorted(by_group):
        scope = f'group:{group}'
        members: list[str] = []
        results = []
        for match in by_group[group]:
            entries = contestants.get(match.id, [])
            ids = [str(c.participant_id or c.team_id) for c in entries]
            for i in ids:
                if i not in members:
                    members.append(i)
            if len(entries) != 2:
                continue
            results.append(
                domain.MatchResult(
                    a=ids[0],
                    b=ids[1],
                    score_a=entries[0].score or 0,
                    score_b=entries[1].score or 0,
                    confirmed=match.confirmed_by is not None,
                )
            )
        decision = decisions.get(scope)
        ranking = domain.rank_round_robin(
            scope,
            members,
            results,
            decision.orders if decision else (),
            active_ids=active,
        )
        rankings.append(
            domain.classify_ties(ranking, cut=cut, plain_winner=False)
        )

    blockers = domain.blocking_ties(rankings)
    open_count = sum(1 for m in phase_one if m.confirmed_by is None)
    ready = bool(phase_one) and open_count == 0 and not blockers

    qualifiers = None
    if ready:
        match domain.qualifiers_top_per_scope(rankings, cut):
            case Ok(found):
                qualifiers = found
            case Err(_):
                ready = False

    crossover = None
    seed_order = None
    if qualifiers is not None:
        crossover_decision = decisions.get(domain.CROSSOVER_SCOPE)
        crossover_orders = (
            crossover_decision.orders if crossover_decision else ()
        )
        seeds = domain.crossover_seed_list(qualifiers, crossover_orders)
        if seeds.is_ok():
            seed_order = seeds.unwrap()
        if not domain.crossover_is_exempt(qualifiers):
            crossover = domain.rank_crossover(qualifiers, crossover_orders)
            open_ties = domain.blocking_ties([crossover])
            if open_ties:
                blockers = (*blockers, *open_ties)
                ready = False

    released = tournament.playoff_released_at is not None
    configured_count = (
        tournament.playoff_group_count * cut
        if tournament.playoff_group_count is not None
        else None
    )
    return Ok(
        QualificationState(
            tournament_id=tournament.id,
            source=SOURCE_GROUPS,
            rankings=tuple(rankings),
            blockers=blockers,
            open_match_count=open_count,
            total_match_count=len(phase_one),
            ready=ready,
            qualifiers=qualifiers,
            released_at=tournament.playoff_released_at,
            released_by=tournament.playoff_released_by,
            release_mode=release_mode,
            auto_release_suspended=tournament.playoff_auto_release_suspended,
            can_unrelease=released
            and _can_unrelease_status(tournament)
            and not _has_result(phase_two, contestants),
            crossover=crossover,
            seed_order=seed_order,
            configured_qualifier_count=configured_count,
            de_fallback=qualifiers is not None
            and falls_back_to_single_elimination(tournament, len(qualifiers)),
        )
    )
