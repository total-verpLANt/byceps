from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column, relationship

from byceps.database import db
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.util.instances import ReprBuilder
from byceps.util.uuid import generate_uuid7

from .match import DbTournamentMatch
from .participant import DbTournamentParticipant
from .team import DbTournamentTeam


class DbTournamentMatchToContestant(db.Model):
    """A contestant entry for a match in a LAN tournament."""

    __tablename__ = 'lan_tournament_match_contestants'
    __table_args__ = (
        db.CheckConstraint(
            '(team_id IS NOT NULL AND participant_id IS NULL)'
            ' OR (team_id IS NULL AND participant_id IS NOT NULL)',
            name='ck_exactly_one_contestant',
        ),
    )

    id: Mapped[TournamentMatchToContestantID] = mapped_column(
        db.Uuid, default=generate_uuid7, primary_key=True
    )
    tournament_match_id: Mapped[TournamentMatchID] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_matches.id'),
        index=True,
    )
    match: Mapped[DbTournamentMatch] = relationship(DbTournamentMatch)
    team_id: Mapped[TournamentTeamID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_teams.id'),
    )
    team: Mapped[DbTournamentTeam | None] = relationship(DbTournamentTeam)
    participant_id: Mapped[TournamentParticipantID | None] = mapped_column(
        db.Uuid,
        db.ForeignKey('lan_tournament_participants.id'),
    )
    participant: Mapped[DbTournamentParticipant | None] = relationship(
        DbTournamentParticipant
    )
    score: Mapped[int | None]
    created_at: Mapped[datetime]

    def __init__(
        self,
        contestant_id: TournamentMatchToContestantID,
        tournament_match_id: TournamentMatchID,
        created_at: datetime,
        *,
        team_id: TournamentTeamID | None = None,
        participant_id: TournamentParticipantID | None = None,
        score: int | None = None,
    ) -> None:
        if (team_id is None) == (participant_id is None):
            raise ValueError(
                'Exactly one of team_id and participant_id must be provided.'
            )

        self.id = contestant_id
        self.tournament_match_id = tournament_match_id
        self.created_at = created_at
        self.team_id = team_id
        self.participant_id = participant_id
        self.score = score

    def __repr__(self) -> str:
        return (
            ReprBuilder(self)
            .add_with_lookup('tournament_match_id')
            .add_with_lookup('team_id')
            .add_with_lookup('participant_id')
            .build()
        )
