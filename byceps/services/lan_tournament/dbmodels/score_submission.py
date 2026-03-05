from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from byceps.database import db
from byceps.services.lan_tournament.models.score_submission import (
    ScoreSubmissionID,
)
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.user.models.user import UserID
from byceps.util.uuid import generate_uuid7


class DbScoreSubmission(db.Model):
    """A score submission in a highscore tournament."""

    __tablename__ = 'lan_tournament_score_submissions'

    id: Mapped[ScoreSubmissionID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_id: Mapped[TournamentID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournaments.id'),
        index=True,
    )
    participant_id: Mapped[TournamentParticipantID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_participants.id'),
    )
    team_id: Mapped[TournamentTeamID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_teams.id'),
    )
    score: Mapped[int] = mapped_column(db.BigInteger)
    submitted_at: Mapped[datetime]
    submitted_by: Mapped[UserID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('users.id'),
    )
    is_official: Mapped[bool]
    note: Mapped[str | None] = mapped_column(db.UnicodeText)

    def __init__(
        self,
        submission_id: ScoreSubmissionID,
        tournament_id: TournamentID,
        score: int,
        submitted_at: datetime,
        *,
        participant_id: (TournamentParticipantID | None) = None,
        team_id: TournamentTeamID | None = None,
        submitted_by: UserID | None = None,
        is_official: bool = True,
        note: str | None = None,
    ) -> None:
        self.id = submission_id
        self.tournament_id = tournament_id
        self.score = score
        self.submitted_at = submitted_at
        self.participant_id = participant_id
        self.team_id = team_id
        self.submitted_by = submitted_by
        self.is_official = is_official
        self.note = note
