"""Tournament score service for highscore mode tournaments."""

from collections import defaultdict
from datetime import datetime, UTC

from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_repository,
)
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.score_ordering import (
    ScoreOrdering,
)
from byceps.services.lan_tournament.models.score_submission import (
    ScoreSubmission,
    ScoreSubmissionID,
)
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

SCORES_LOCKED_ERROR = 'The qualification is closed. Scores are locked.'

CLOSE_NOT_ONGOING_ERROR = (
    'The leaderboard can only be closed while the tournament is ongoing.'
)


def _scores_locked(tournament: Tournament) -> bool:
    """Return `True` once the leaderboard is closed or playoffs released.

    The `datetime` test keeps Mock tournaments (truthy attributes)
    on the open path, as `_has_playoffs` does in the match service.
    """
    return isinstance(tournament.leaderboard_closed_at, datetime) or (
        isinstance(tournament.playoff_released_at, datetime)
    )


def _lock_for_playoffs(tournament: Tournament) -> tuple[Tournament, bool]:
    """Lock a tournament with playoffs and re-read it under the lock.

    A close or release can commit between an unlocked read and the
    write that follows. Without playoffs neither exists, so no lock is
    taken. Return the tournament and whether the row is now locked.
    """
    if not tournament.has_playoffs:
        return tournament, False
    tournament_repository.lock_tournament_for_update(tournament.id)
    return tournament_repository.get_tournament(tournament.id), True


def submit_score(
    tournament_id: TournamentID,
    score: int,
    *,
    participant_id: TournamentParticipantID | None = None,
    team_id: TournamentTeamID | None = None,
    submitted_by: UserID | None = None,
    note: str | None = None,
) -> Result[ScoreSubmission, str]:
    """Submit a score for a highscore tournament."""
    # Validate exactly one of participant_id/team_id is set.
    if participant_id is None and team_id is None:
        return Err('Exactly one of participant_id or team_id must be provided.')
    if participant_id is not None and team_id is not None:
        return Err('Only one of participant_id or team_id may be provided.')

    tournament = tournament_repository.get_tournament(tournament_id)

    if score < 0:
        return Err('Score must not be negative.')

    if tournament.game_format != GameFormat.HIGHSCORE:
        return Err('Tournament mode must be HIGHSCORE to submit scores.')

    tournament, holds_lock = _lock_for_playoffs(tournament)
    if _scores_locked(tournament):
        if holds_lock:
            tournament_repository.rollback_session()
        return Err(SCORES_LOCKED_ERROR)

    now = datetime.now(UTC)
    submission_id = ScoreSubmissionID(generate_uuid7())

    submission = ScoreSubmission(
        id=submission_id,
        tournament_id=tournament_id,
        participant_id=participant_id,
        team_id=team_id,
        score=score,
        submitted_at=now,
        submitted_by=submitted_by,
        is_official=True,
        note=note,
    )

    tournament_repository.create_score_submission(submission)

    return Ok(submission)


def submit_score_by_participant(
    tournament_id: TournamentID,
    initiator_id: UserID,
    score: int,
    note: str | None = None,
) -> Result[ScoreSubmission, str]:
    """Submit a score for the calling user in a highscore tournament.

    Resolves the user's participant/team identity automatically.
    Only valid for HIGHSCORE mode tournaments.
    """
    participant = tournament_repository.find_participant_by_user(
        tournament_id, initiator_id
    )
    if participant is None:
        return Err(
            'You are not registered as a participant in this tournament.'
        )

    tournament = tournament_repository.get_tournament(tournament_id)

    participant_id = None
    team_id = None
    if tournament.contestant_type == ContestantType.TEAM:
        if participant.team_id is None:
            return Err('You must be in a team to submit a score.')
        team_id = participant.team_id
    else:
        participant_id = participant.id

    return submit_score(
        tournament_id,
        score,
        participant_id=participant_id,
        team_id=team_id,
        submitted_by=initiator_id,
        note=note,
    )


def get_leaderboard(
    tournament_id: TournamentID,
) -> Result[list[ScoreSubmission], str]:
    """Get the leaderboard for a highscore tournament."""
    tournament = tournament_repository.get_tournament(tournament_id)

    if tournament.game_format != GameFormat.HIGHSCORE:
        return Err('Tournament mode must be HIGHSCORE to view leaderboard.')

    if tournament.score_ordering is None:
        return Err('Tournament score_ordering is not configured.')

    submissions = tournament_repository.get_official_submissions_for_tournament(
        tournament_id
    )

    if not submissions:
        return Ok([])

    score_ordering = tournament.score_ordering

    # Group by contestant key.
    grouped: dict[
        tuple[
            TournamentParticipantID | None,
            TournamentTeamID | None,
        ],
        list[ScoreSubmission],
    ] = defaultdict(list)
    for sub in submissions:
        key = (sub.participant_id, sub.team_id)
        grouped[key].append(sub)

    # Pick the best submission per contestant.
    best_per_contestant: list[ScoreSubmission] = []
    for _key, subs in grouped.items():
        if score_ordering == ScoreOrdering.HIGHER_IS_BETTER:
            best = max(
                subs,
                key=lambda s: (
                    s.score,
                    -int(s.submitted_at.timestamp() * 1_000_000),
                ),
            )
        else:
            best = min(
                subs,
                key=lambda s: (
                    s.score,
                    s.submitted_at.timestamp(),
                ),
            )
        best_per_contestant.append(best)

    # Sort leaderboard.
    if score_ordering == ScoreOrdering.HIGHER_IS_BETTER:
        best_per_contestant.sort(key=lambda s: (-s.score, s.submitted_at))
    else:
        best_per_contestant.sort(key=lambda s: (s.score, s.submitted_at))

    return Ok(best_per_contestant)


def get_qualification_values(
    tournament_id: TournamentID,
) -> Result[dict[str, int], str]:
    """Return every contestant's best score, by contestant ID.

    Equal values stay equal: submission time never breaks a tie.
    """
    tournament = tournament_repository.get_tournament(tournament_id)

    if tournament.game_format != GameFormat.HIGHSCORE:
        return Err('Tournament mode must be HIGHSCORE to view leaderboard.')

    if tournament.score_ordering is None:
        return Err('Tournament score_ordering is not configured.')

    pick = (
        max
        if tournament.score_ordering == ScoreOrdering.HIGHER_IS_BETTER
        else min
    )
    values: dict[str, int] = {}
    for sub in tournament_repository.get_official_submissions_for_tournament(
        tournament_id
    ):
        contestant = str(sub.participant_id or sub.team_id)
        values[contestant] = (
            pick(values[contestant], sub.score)
            if contestant in values
            else sub.score
        )
    return Ok(values)


def close_leaderboard(
    tournament_id: TournamentID, *, initiator_id: UserID
) -> Result[None, str]:
    """Close the leaderboard, ending phase 1 of a highscore tournament.

    Scores are locked from then on; the qualification can be computed
    and the playoffs released. The automatic release runs after the
    commit, for a tournament that releases by itself.
    """
    tournament_repository.lock_tournament_for_update(tournament_id)
    tournament = tournament_repository.get_tournament(tournament_id)

    refusal = _close_refusal(tournament)
    if refusal is not None:
        tournament_repository.rollback_session()
        return Err(refusal)

    tournament_repository.set_leaderboard_closed(
        tournament_id, datetime.now(UTC).replace(tzinfo=None)
    )
    tournament_log_service.create_log_entry(
        'qualification-leaderboard-closed',
        tournament_id,
        initiator_id,
        commit=False,
    )
    tournament_repository.commit_session()

    from byceps.services.lan_tournament import tournament_qualification_service

    tournament_qualification_service.try_auto_release(
        tournament_id, triggered_by=initiator_id
    )
    return Ok(None)


def _close_refusal(tournament: Tournament) -> str | None:
    if tournament.game_format != GameFormat.HIGHSCORE:
        return 'Tournament mode must be HIGHSCORE to view leaderboard.'
    if not tournament.has_playoffs:
        return 'This tournament has no playoff phase.'
    if _scores_locked(tournament):
        return SCORES_LOCKED_ERROR
    if tournament.tournament_status != TournamentStatus.ONGOING:
        return CLOSE_NOT_ONGOING_ERROR
    return None


def delete_scores_for_tournament(
    tournament_id: TournamentID,
) -> Result[None, str]:
    """Delete all score submissions for a tournament."""
    tournament, holds_lock = _lock_for_playoffs(
        tournament_repository.get_tournament(tournament_id)
    )
    if _scores_locked(tournament):
        if holds_lock:
            tournament_repository.rollback_session()
        return Err(SCORES_LOCKED_ERROR)
    tournament_repository.delete_submissions_for_tournament(tournament_id)
    return Ok(None)
