"""Tournament score service for highscore mode tournaments."""

from collections import defaultdict
from datetime import datetime, UTC

from byceps.services.lan_tournament import tournament_repository
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
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.models.user import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7


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

    if tournament.tournament_mode != TournamentMode.HIGHSCORE:
        return Err('Tournament mode must be HIGHSCORE to submit scores.')

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


def get_leaderboard(
    tournament_id: TournamentID,
) -> Result[list[ScoreSubmission], str]:
    """Get the leaderboard for a highscore tournament."""
    tournament = tournament_repository.get_tournament(tournament_id)

    if tournament.tournament_mode != TournamentMode.HIGHSCORE:
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


def delete_scores_for_tournament(
    tournament_id: TournamentID,
) -> Result[None, str]:
    """Delete all score submissions for a tournament."""
    tournament_repository.delete_submissions_for_tournament(tournament_id)
    return Ok(None)
