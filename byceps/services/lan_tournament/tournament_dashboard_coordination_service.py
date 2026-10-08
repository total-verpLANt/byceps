"""
byceps.services.lan_tournament.tournament_dashboard_coordination_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from dataclasses import dataclass
import unicodedata
from uuid import UUID

from byceps.services.authn.session.models import CurrentUser
from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from . import tournament_log_service, tournament_repository
from .models.operational_timing import (
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
    MatchPinState,
    OperationalClock,
    TrafficTier,
)
from .models.tournament import Tournament
from .models.tournament_match import TournamentMatch, TournamentMatchID
from .models.tournament_status import TournamentStatus
from .tournament_dashboard_service import (
    DASHBOARD_UNAUTHENTICATED_ERROR,
    resolve_dashboard_scope,
)
from .tournament_dashboard_settings_service import (
    get_effective_dashboard_settings,
)
from .tournament_operational_domain_service import (
    clock_value_us,
    derive_traffic_tier,
)


# An unknown match, a match of another party and a match of a tournament
# the viewer has no authority over all answer the same.
DASHBOARD_MATCH_NOT_FOUND_ERROR = 'dashboard_match_not_found'
DASHBOARD_MATCH_TERMINAL_ERROR = 'dashboard_match_terminal'
DASHBOARD_PIN_CONFLICT_ERROR = 'dashboard_pin_conflict'

# A stale or duplicate request: the episode or the revision it was built
# from is not the current one.
DASHBOARD_ACK_CONFLICT_ERROR = 'dashboard_ack_conflict'
DASHBOARD_ACK_COMMENT_INVALID_ERROR = 'dashboard_ack_comment_invalid'
# One refusal for each reason the read side gives for not offering an
# acknowledgement, named after it (`AckUnavailableReason`).
DASHBOARD_ACK_TERMINAL_ERROR = 'dashboard_ack_terminal'
DASHBOARD_ACK_PAUSED_ERROR = 'dashboard_ack_paused'
DASHBOARD_ACK_NOT_DUE_ERROR = 'dashboard_ack_not_due'
DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR = 'dashboard_ack_clock_unknown'
DASHBOARD_ACK_BELOW_THRESHOLD_ERROR = 'dashboard_ack_below_threshold'
DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR = (
    'dashboard_ack_recently_acknowledged'
)

MATCH_PINNED_EVENT = 'match-pinned'
MATCH_UNPINNED_EVENT = 'match-unpinned'
MATCH_ACKNOWLEDGED_EVENT = 'match-acknowledged'

# The width of the comment column.
MAX_COMMENT_LENGTH = 500

_TERMINAL_TOURNAMENT_STATUSES = frozenset(
    {TournamentStatus.COMPLETED, TournamentStatus.CANCELLED}
)

_ALLOWED_CONTROL_CHARACTERS = frozenset('\n\t')
_REFUSED_CATEGORIES = frozenset({'Cc', 'Cs', 'Zl', 'Zp'})
# Bidirectional overrides and isolates: a shared comment could reorder what
# other orgas read.
_BIDI_CONTROLS = frozenset(
    '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
)


@dataclass(frozen=True, kw_only=True)
class _LockedMatch:
    """A match and its tournament, read afresh under both locks."""

    match: TournamentMatch
    tournament: Tournament


def set_match_pin(
    viewer: CurrentUser,
    party_id: PartyID,
    match_id: TournamentMatchID,
    *,
    pinned: bool,
    expected_revision: int,
) -> Result[MatchPinState | None, str]:
    """Pin or unpin the match for every orga who may see it.

    The pin is shared state, not a personal flag. It marks a row and
    changes nothing else: not the match clock, the last change, an
    episode or an acknowledgement, and it sends no message.

    A revision other than the current one is a conflict, also for a
    duplicate request. Pinning a pinned match, or unpinning an unpinned
    one, at the current revision writes nothing and returns the state as
    it is. That state is `None` for a match nobody ever pinned.

    The caller must not hold an open write transaction. On an error the
    session is rolled back, so no lock outlives the call. An audit
    failure rolls the pin back and is raised.
    """
    try:
        locked_result = _lock_authorized_match(viewer, party_id, match_id)
        if locked_result.is_err():
            return Err(locked_result.unwrap_err())
        locked = locked_result.unwrap()
        tournament_id = locked.tournament.id
        locked_match_id = locked.match.id

        state = tournament_repository.find_match_pin_state(locked_match_id)
        revision = state.revision if state is not None else 0
        if expected_revision != revision:
            return _refuse(DASHBOARD_PIN_CONFLICT_ERROR)

        is_pinned = state is not None and state.pinned_at is not None
        if pinned == is_pinned:
            tournament_repository.rollback_session()
            return Ok(state)

        now = tournament_repository.get_operation_time()
        saved = tournament_repository.save_match_pin_flush(
            locked_match_id,
            tournament_id,
            pinned_at=now if pinned else None,
            pinned_by=viewer.id if pinned else None,
            updated_at=now,
            updated_by=viewer.id,
            expected_revision=revision,
        )
        if saved is None:
            return _refuse(DASHBOARD_PIN_CONFLICT_ERROR)

        tournament_log_service.create_log_entry(
            MATCH_PINNED_EVENT if pinned else MATCH_UNPINNED_EVENT,
            tournament_id,
            viewer.id,
            data={'match_id': str(locked_match_id), 'revision': saved.revision},
            commit=False,
        )

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    return Ok(saved)


def acknowledge_match(
    viewer: CurrentUser,
    party_id: PartyID,
    match_id: TournamentMatchID,
    *,
    expected_episode_id: MatchDueEpisodeID,
    expected_ack_revision: int,
    comment: str | None,
) -> Result[MatchEscalationAcknowledgement, str]:
    """Record that the viewer checked the delay of a due match.

    The acknowledgement is shared state: it says "I checked this delay
    and noted the situation", not that somebody owns the match or that
    it is resolved. It is appended to the one open due episode, and only
    if the request was built from that episode at its current revision.
    A stale or duplicate request is a conflict and writes nothing, so it
    can neither add a second record nor prolong the quiet time.

    The match must be due and running with a known clock, and its alert
    interval must have reached the yellow threshold of the party. The
    current clock value becomes the new baseline of that interval. The
    original occupancy, the episode's opening, the total wait and the
    last change stay as they are.

    The comment is plain text, stripped and bounded; it is stored as it
    is and escaped where it is shown. The caller must not hold an open
    write transaction. On an error the session is rolled back, so no
    lock outlives the call. An audit failure rolls back every fact and
    is raised.
    """
    try:
        return _acknowledge(
            viewer,
            party_id,
            match_id,
            expected_episode_id=expected_episode_id,
            expected_ack_revision=expected_ack_revision,
            comment=comment,
        )
    except Exception:
        tournament_repository.rollback_session()
        raise


def _acknowledge(
    viewer: CurrentUser,
    party_id: PartyID,
    match_id: TournamentMatchID,
    *,
    expected_episode_id: MatchDueEpisodeID,
    expected_ack_revision: int,
    comment: str | None,
) -> Result[MatchEscalationAcknowledgement, str]:
    locked_result = _lock_authorized_match(
        viewer,
        party_id,
        match_id,
        terminal_error=DASHBOARD_ACK_TERMINAL_ERROR,
    )
    if locked_result.is_err():
        return Err(locked_result.unwrap_err())
    locked = locked_result.unwrap()
    tournament = locked.tournament
    locked_match_id = locked.match.id

    comment_result = _normalize_comment(comment)
    if comment_result.is_err():
        return _refuse(comment_result.unwrap_err())
    text = comment_result.unwrap()

    settings_result = get_effective_dashboard_settings(party_id)
    if settings_result.is_err():
        return _refuse(settings_result.unwrap_err())
    settings = settings_result.unwrap()

    clock = _clock_of(tournament)
    episode = tournament_repository.find_open_due_episode(locked_match_id)
    if episode is None:
        return _refuse(
            DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR
            if clock.activated_at is None
            else DASHBOARD_ACK_NOT_DUE_ERROR
        )

    if (
        not _is_same_id(expected_episode_id, episode.id)
        or expected_ack_revision != episode.ack_revision
    ):
        return _refuse(DASHBOARD_ACK_CONFLICT_ERROR)

    if tournament.tournament_status is TournamentStatus.PAUSED:
        return _refuse(DASHBOARD_ACK_PAUSED_ERROR)
    if tournament.tournament_status is not TournamentStatus.ONGOING:
        return _refuse(DASHBOARD_ACK_NOT_DUE_ERROR)
    if clock.activated_at is None:
        return _refuse(DASHBOARD_ACK_CLOCK_UNKNOWN_ERROR)

    now = tournament_repository.get_operation_time()
    clock_us = clock_value_us(clock, now)
    latest = tournament_repository.find_latest_escalation_ack(episode.id)
    baseline_us = (
        latest.clock_us if latest is not None else episode.opened_clock_us
    )
    alert_us = max(clock_us - baseline_us, 0)
    if derive_traffic_tier(alert_us, settings) is TrafficTier.GREEN:
        return _refuse(
            DASHBOARD_ACK_BELOW_THRESHOLD_ERROR
            if latest is None
            else DASHBOARD_ACK_RECENTLY_ACKNOWLEDGED_ERROR
        )

    advanced = tournament_repository.advance_episode_ack_revision_flush(
        episode.id, expected_revision=episode.ack_revision
    )
    if not advanced:
        return _refuse(DASHBOARD_ACK_CONFLICT_ERROR)

    acknowledgement = MatchEscalationAcknowledgement(
        id=MatchEscalationAcknowledgementID(generate_uuid7()),
        episode_id=episode.id,
        tournament_id=tournament.id,
        match_id=locked_match_id,
        revision=episode.ack_revision + 1,
        occurred_at=now,
        clock_us=clock_us,
        actor_id=viewer.id,
        comment=text,
    )
    tournament_repository.create_escalation_ack_flush(acknowledgement)

    tournament_log_service.create_log_entry(
        MATCH_ACKNOWLEDGED_EVENT,
        tournament.id,
        viewer.id,
        data={
            'match_id': str(locked_match_id),
            'episode_id': str(episode.id),
            'revision': acknowledgement.revision,
        },
        commit=False,
    )

    tournament_repository.commit_session()

    return Ok(acknowledgement)


def _lock_authorized_match(
    viewer: CurrentUser,
    party_id: PartyID,
    match_id: TournamentMatchID,
    *,
    terminal_error: str = DASHBOARD_MATCH_TERMINAL_ERROR,
) -> Result[_LockedMatch, str]:
    """Lock the tournament, then the match, and check the viewer's authority.

    Nothing the request says is trusted. The viewer's current authority
    over the party and the match's tournament is read under the lock,
    which a revocation of an assignment also takes first. A terminal
    match is refused with `terminal_error`. On an error the session is
    rolled back.
    """
    if not viewer.authenticated:
        return Err(DASHBOARD_UNAUTHENTICATED_ERROR)

    candidate = _find_match_unlocked(match_id)
    if candidate is None:
        return _refuse_unknown_match(viewer, party_id)

    tournament_id = candidate.tournament_id
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament_repository.lock_matches_for_update([candidate.id])

    match = tournament_repository.find_match_fresh(candidate.id)
    if match is None or match.tournament_id != tournament_id:
        return _refuse_unknown_match(viewer, party_id)
    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)

    scope_result = resolve_dashboard_scope(viewer, party_id, 'all')
    if scope_result.is_err():
        return _refuse(scope_result.unwrap_err())
    if tournament_id not in scope_result.unwrap().tournament_ids:
        return _refuse(DASHBOARD_MATCH_NOT_FOUND_ERROR)

    if (
        match.confirmed_by is not None
        or tournament.tournament_status in _TERMINAL_TOURNAMENT_STATUSES
    ):
        return _refuse(terminal_error)

    return Ok(_LockedMatch(match=match, tournament=tournament))


def _find_match_unlocked(match_id: TournamentMatchID) -> TournamentMatch | None:
    """Return the match as far as it tells which tournament to lock.

    The ID comes from a request and is a `str` there.
    """
    try:
        coerced = TournamentMatchID(UUID(str(match_id)))
    except ValueError:
        return None

    return tournament_repository.find_match(coerced)


def _refuse_unknown_match(viewer: CurrentUser, party_id: PartyID) -> Err[str]:
    """Refuse a match that is not there, unless the viewer has no dashboard.

    Somebody without any authority in the party learns no more than a
    missing match would tell.
    """
    scope_result = resolve_dashboard_scope(viewer, party_id, 'all')
    if scope_result.is_err():
        return _refuse(scope_result.unwrap_err())

    return _refuse(DASHBOARD_MATCH_NOT_FOUND_ERROR)


def _clock_of(tournament: Tournament) -> OperationalClock:
    return OperationalClock(
        elapsed_us=tournament.operational_clock_elapsed_us,
        running_since=tournament.operational_clock_running_since,
        activated_at=tournament.operational_clock_activated_at,
    )


def _is_same_id(expected: object, actual: UUID) -> bool:
    """Tell if a request's ID, a `str` or a UUID, names the stored one."""
    try:
        return UUID(str(expected)) == actual
    except ValueError:
        return False


def _normalize_comment(comment: object) -> Result[str | None, str]:
    """Return the comment as stored: stripped, `None` if blank.

    Only plain text is accepted. Its length is bounded, and control and
    bidirectional characters are refused, as in the form.
    """
    if comment is None:
        return Ok(None)

    if not isinstance(comment, str):
        return Err(DASHBOARD_ACK_COMMENT_INVALID_ERROR)

    text = comment.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return Ok(None)

    if len(text) > MAX_COMMENT_LENGTH or not _is_plain_text(text):
        return Err(DASHBOARD_ACK_COMMENT_INVALID_ERROR)

    return Ok(text)


def _is_plain_text(text: str) -> bool:
    return not any(
        character in _BIDI_CONTROLS
        or (
            unicodedata.category(character) in _REFUSED_CATEGORIES
            and character not in _ALLOWED_CONTROL_CHARACTERS
        )
        for character in text
    )


def _refuse(error: str) -> Err[str]:
    """Release the locks and return the error."""
    tournament_repository.rollback_session()
    return Err(error)
