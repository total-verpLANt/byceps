"""Batched, read-only queries for the personal tournament overview."""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.seating.dbmodels.area import DbSeatingArea
from byceps.services.seating.dbmodels.seat import DbSeat
from byceps.services.seating.models import SeatID
from byceps.services.ticketing.dbmodels.ticket import DbTicket
from byceps.services.user.models import UserID

from . import tournament_repository as repository
from .dbmodels.participant import DbTournamentParticipant
from .dbmodels.score_submission import DbScoreSubmission
from .dbmodels.team import DbTournamentTeam
from .dbmodels.tournament import DbTournament
from .models.contestant_type import ContestantType
from .models.game_format import GameFormat
from .models.tournament import TournamentID
from .models.tournament_match import TournamentMatch
from .models.tournament_participant import TournamentParticipant


@dataclass(frozen=True)
class ContactSeatArea:
    slug: str
    title: str


@dataclass(frozen=True)
class ContactSeat:
    id: SeatID
    label: str | None
    area: ContactSeatArea


def get_contact_seats(
    party_id: PartyID, user_ids: set[UserID]
) -> dict[UserID, list[ContactSeat]]:
    """Use current ticket users, never owners or seat managers.

    Project only public link data so rendering cannot lazily load tickets.
    Both ticket and seating area must belong to the overview's party.
    """
    if not user_ids:
        return {}
    rows = db.session.execute(
        select(
            DbTicket.used_by_id,
            DbSeat.id,
            DbSeat.label,
            DbSeatingArea.slug,
            DbSeatingArea.title,
        )
        .join(DbSeat, DbTicket.occupied_seat_id == DbSeat.id)
        .join(DbSeatingArea, DbSeat.area_id == DbSeatingArea.id)
        .where(
            DbTicket.used_by_id.in_(user_ids),
            DbTicket.party_id == party_id,
            DbTicket.revoked.is_(False),
            DbSeatingArea.party_id == party_id,
        )
        .distinct()
    ).all()
    seats = defaultdict(list)
    for user_id, seat_id, label, slug, title in rows:
        seats[user_id].append(
            ContactSeat(seat_id, label, ContactSeatArea(slug, title))
        )
    return {
        user_id: sorted(
            user_seats,
            key=lambda s: (
                s.area.title.casefold(),
                s.area.slug,
                (s.label or '').casefold(),
                str(s.id),
            ),
        )
        for user_id, user_seats in seats.items()
    }


def get_active_participations(
    party_id: PartyID, user_id: UserID
) -> list[TournamentParticipant]:
    """Scope membership to the authenticated user and current party."""
    entities = db.session.scalars(
        select(DbTournamentParticipant)
        .join(
            DbTournament,
            DbTournamentParticipant.tournament_id == DbTournament.id,
        )
        .where(
            DbTournament.party_id == party_id,
            DbTournamentParticipant.user_id == user_id,
            DbTournamentParticipant.removed_at.is_(None),
        )
    ).all()
    return [repository._db_participant_to_participant(p) for p in entities]


def get_matches(
    tournament_ids: list[TournamentID],
) -> list[TournamentMatch]:
    return repository.get_matches_for_tournaments(tournament_ids)


def get_participants(
    tournament_ids: list[TournamentID],
) -> list[TournamentParticipant]:
    """Include removed opponents to label saved matches, not membership."""
    if not tournament_ids:
        return []
    entities = db.session.scalars(
        select(DbTournamentParticipant).where(
            DbTournamentParticipant.tournament_id.in_(tournament_ids)
        )
    ).all()
    return [repository._db_participant_to_participant(p) for p in entities]


def get_highscores_with_complete_results(
    party_id: PartyID,
    contestant_types: dict[TournamentID, ContestantType],
) -> set[TournamentID]:
    """Compare current contestants with official submissions in two reads.

    A team counts once and only while it has current participation. Duplicate
    submissions cannot cover a missing contestant; an empty field is not done.
    """
    if not contestant_types:
        return set()
    scope = (
        DbTournament.party_id == party_id,
        DbTournament.id.in_(contestant_types),
        DbTournament.game_format == GameFormat.HIGHSCORE.name,
    )
    rows = db.session.execute(
        select(
            DbTournamentParticipant.tournament_id,
            DbTournamentParticipant.id,
            DbTournamentTeam.id,
        )
        .join(
            DbTournament,
            DbTournament.id == DbTournamentParticipant.tournament_id,
        )
        .outerjoin(
            DbTournamentTeam,
            (DbTournamentTeam.id == DbTournamentParticipant.team_id)
            & (DbTournamentTeam.tournament_id == DbTournament.id)
            & DbTournamentTeam.removed_at.is_(None),
        )
        .where(*scope, DbTournamentParticipant.removed_at.is_(None))
    ).all()
    current = defaultdict(set)
    for tournament_id, participant_id, team_id in rows:
        identity = (
            team_id
            if contestant_types[tournament_id] == ContestantType.TEAM
            else participant_id
        )
        if identity is not None:
            current[tournament_id].add(identity)

    rows = db.session.execute(
        select(
            DbScoreSubmission.tournament_id,
            DbScoreSubmission.participant_id,
            DbScoreSubmission.team_id,
        )
        .join(DbTournament, DbTournament.id == DbScoreSubmission.tournament_id)
        .where(
            *scope,
            DbScoreSubmission.is_official.is_(True),
            DbScoreSubmission.score >= 0,
        )
        .distinct()
    ).all()
    submitted = defaultdict(set)
    for tournament_id, participant_id, team_id in rows:
        if contestant_types[tournament_id] == ContestantType.TEAM:
            if team_id is not None and participant_id is None:
                submitted[tournament_id].add(team_id)
        elif participant_id is not None and team_id is None:
            submitted[tournament_id].add(participant_id)
    return {
        tournament_id
        for tournament_id, identities in current.items()
        if identities <= submitted[tournament_id]
    }
