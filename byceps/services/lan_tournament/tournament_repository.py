import json
import logging
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import cast, NamedTuple, TYPE_CHECKING, TypeVar
from uuid import UUID

from sqlalchemy import (
    and_,
    BigInteger,
    case,
    ColumnElement,
    delete,
    extract,
    func,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.base import Executable

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import uuid7

from .dbmodels.dashboard import (
    DbDashboardPartyThresholds,
    DbMatchDashboardAnnotation,
    DbMatchDueEpisode,
    DbMatchEscalationAck,
)
from .dbmodels.match import DbTournamentMatch
from .dbmodels.match_comment import DbTournamentMatchComment
from .dbmodels.match_contestant import DbTournamentMatchToContestant
from .dbmodels.match_readiness import DbMatchInvitation, DbMatchPairing
from .dbmodels.participant import DbTournamentParticipant
from .dbmodels.score_submission import DbScoreSubmission
from .dbmodels.team import DbTournamentTeam
from .dbmodels.tournament import DbTournament
from .dbmodels.tournament_log_entry import DbTournamentLogEntry
from .models.bracket import Bracket
from .models.contestant_type import ContestantType
from .models.match_readiness import (
    ContestantIdentity,
    InvitationStatus,
    MatchInvitation,
    MatchPairing,
)
from .models.operational_timing import (
    MatchDueEpisode,
    MatchEscalationAcknowledgement,
    MatchPinState,
    OperationalClock,
)
from .models.tournament import Tournament, TournamentID
from .models.tournament_dashboard import PartyDashboardThresholds
from .models.tournament_category import TournamentCategory
from .models.tournament_image import TournamentImageID
from .models.tournament_log_entry import TournamentLogEntry
from .models.tournament_match import (
    MatchInvitationID,
    MatchPairingID,
    MatchSide,
    TournamentMatch,
    TournamentMatchID,
)
from .models.tournament_match_comment import (
    MatchCommentContext,
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from .models.score_ordering import ScoreOrdering
from .models.score_submission import ScoreSubmission
from .models.contestant_status import ContestantStatus
from .models.game_format import GameFormat
from .models.elimination_mode import EliminationMode
from .models.playoff import PlayoffReleaseMode
from .models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeam, TournamentTeamID
from .tournament_operational_domain_service import transition_clock

if TYPE_CHECKING:
    from .models.tournament_request import TournamentRequestID

logger = logging.getLogger(__name__)

_E = TypeVar('_E')


def _derive_contestant_type(
    contestant_type: ContestantType | None,
    max_players_in_team: int | None,
    min_players_in_team: int | None,
) -> ContestantType:
    """Map legacy nullable contestant types without importing a service."""
    if contestant_type is not None:
        return contestant_type
    team_size = (
        max_players_in_team
        if max_players_in_team is not None
        else min_players_in_team
    )
    if team_size is not None and team_size > 1:
        return ContestantType.TEAM
    return ContestantType.SOLO


# -- tournament --


def _safe_enum_lookup(
    enum_class: type[_E],
    value: str | None,
    default: _E | None = None,
) -> _E | None:
    """Safely look up enum by name, returning default
    if invalid.

    Protects against database corruption or invalid
    enum values.
    """
    if value is None:
        return None
    try:
        return enum_class[value]
    except KeyError:
        logger.warning(
            'Invalid enum value %r for %s, returning default %r',
            value,
            enum_class.__name__,
            default,
        )
        return default


def create_tournament(tournament: Tournament, *, commit: bool = True) -> None:
    """Persist a tournament (flush only when `commit` is `False`)."""
    db_tournament = DbTournament(
        tournament.id,
        tournament.party_id,
        tournament.name,
        tournament.created_at,
        game=tournament.game,
        category=tournament.category.value,
        description=tournament.description,
        image_url=tournament.image_url,
        ruleset=tournament.ruleset,
        start_time=tournament.start_time,
        min_players=tournament.min_players,
        max_players=tournament.max_players,
        min_teams=tournament.min_teams,
        max_teams=tournament.max_teams,
        min_players_in_team=tournament.min_players_in_team,
        max_players_in_team=tournament.max_players_in_team,
        contestant_type=(
            tournament.contestant_type.name
            if tournament.contestant_type
            else None
        ),
        tournament_status=(
            tournament.tournament_status.name
            if tournament.tournament_status
            else None
        ),
        game_format=(
            tournament.game_format.name
            if tournament.game_format
            else None
        ),
        elimination_mode=(
            tournament.elimination_mode.name
            if tournament.elimination_mode
            else None
        ),
        score_ordering=(
            tournament.score_ordering.name
            if tournament.score_ordering
            else None
        ),
        point_table=(
            json.dumps(tournament.point_table)
            if tournament.point_table
            else None
        ),
        advancement_count=tournament.advancement_count,
        group_size_min=tournament.group_size_min,
        group_size_max=tournament.group_size_max,
        points_carry_to_losers=tournament.points_carry_to_losers,
        created_from_request_id=tournament.created_from_request_id,
        image_id=tournament.image_id,
        image_alt_text=tournament.image_alt_text,
        creation_token=tournament.creation_token,
        playoff_game_format=_enum_name(tournament.playoff_game_format),
        playoff_elimination_mode=_enum_name(
            tournament.playoff_elimination_mode
        ),
        playoff_group_count=tournament.playoff_group_count,
        playoff_qualifiers_per_group=tournament.playoff_qualifiers_per_group,
        playoff_qualifier_count=tournament.playoff_qualifier_count,
        playoff_release_mode=_enum_name(tournament.playoff_release_mode),
    )

    db_tournament.position = tournament.position
    db_tournament.operational_clock_elapsed_us = (
        tournament.operational_clock_elapsed_us
    )
    db_tournament.operational_clock_running_since = _naive_utc_or_none(
        tournament.operational_clock_running_since
    )
    db_tournament.operational_clock_activated_at = _naive_utc_or_none(
        tournament.operational_clock_activated_at
    )

    db.session.add(db_tournament)
    if commit:
        db.session.commit()
    else:
        db.session.flush()


def update_tournament(tournament: Tournament) -> None:
    """Update a tournament in place (no delete/recreate).

    Status, winner, position and release state have their own writers.
    """
    db_tournament = db.session.get(DbTournament, tournament.id)
    if db_tournament is None:
        raise ValueError(f'Unknown tournament ID "{tournament.id}"')

    db_tournament.name = tournament.name
    db_tournament.category = tournament.category.value
    db_tournament.game = tournament.game
    db_tournament.description = tournament.description
    db_tournament.image_url = tournament.image_url
    db_tournament.image_id = tournament.image_id
    db_tournament.image_alt_text = tournament.image_alt_text
    db_tournament.ruleset = tournament.ruleset
    db_tournament.start_time = tournament.start_time
    db_tournament.min_players = tournament.min_players
    db_tournament.max_players = tournament.max_players
    db_tournament.min_teams = tournament.min_teams
    db_tournament.max_teams = tournament.max_teams
    db_tournament.min_players_in_team = tournament.min_players_in_team
    db_tournament.max_players_in_team = tournament.max_players_in_team
    db_tournament.contestant_type = (
        tournament.contestant_type.name if tournament.contestant_type else None
    )
    db_tournament.game_format = (
        tournament.game_format.name if tournament.game_format else None
    )
    db_tournament.elimination_mode = (
        tournament.elimination_mode.name
        if tournament.elimination_mode
        else None
    )
    db_tournament.score_ordering = (
        tournament.score_ordering.name if tournament.score_ordering else None
    )
    db_tournament.point_table = (
        json.dumps(tournament.point_table)
        if tournament.point_table
        else None
    )
    db_tournament.advancement_count = tournament.advancement_count
    db_tournament.group_size_min = tournament.group_size_min
    db_tournament.group_size_max = tournament.group_size_max
    db_tournament.points_carry_to_losers = tournament.points_carry_to_losers
    db_tournament.updated_at = tournament.updated_at
    db_tournament.playoff_game_format = _enum_name(
        tournament.playoff_game_format
    )
    db_tournament.playoff_elimination_mode = _enum_name(
        tournament.playoff_elimination_mode
    )
    db_tournament.playoff_group_count = tournament.playoff_group_count
    db_tournament.playoff_qualifiers_per_group = (
        tournament.playoff_qualifiers_per_group
    )
    db_tournament.playoff_qualifier_count = tournament.playoff_qualifier_count
    db_tournament.playoff_release_mode = _enum_name(
        tournament.playoff_release_mode
    )

    db.session.commit()


def set_playoff_release(
    tournament_id: TournamentID,
    *,
    released_at: datetime,
    released_by: UserID,
) -> None:
    """Record the playoff release (flush only, caller owns commit)."""
    db.session.execute(
        update(DbTournament)
        .filter_by(id=tournament_id)
        .values(
            playoff_released_at=released_at,
            playoff_released_by=released_by,
        )
    )
    db.session.flush()


def set_playoff_elimination_mode(
    tournament_id: TournamentID, mode: EliminationMode
) -> None:
    """Set the phase-2 elimination mode (flush only, caller owns commit)."""
    db.session.execute(
        update(DbTournament)
        .filter_by(id=tournament_id)
        .values(playoff_elimination_mode=_enum_name(mode))
    )
    db.session.flush()


def clear_playoff_release(
    tournament_id: TournamentID, *, suspend_auto: bool
) -> None:
    """Clear the playoff release (flush only, caller owns commit)."""
    db.session.execute(
        update(DbTournament)
        .filter_by(id=tournament_id)
        .values(
            playoff_released_at=None,
            playoff_released_by=None,
            playoff_auto_release_suspended=suspend_auto,
        )
    )
    db.session.flush()


def set_leaderboard_closed(
    tournament_id: TournamentID, closed_at: datetime | None
) -> None:
    """Set or clear the leaderboard close time (flush only)."""
    db.session.execute(
        update(DbTournament)
        .filter_by(id=tournament_id)
        .values(leaderboard_closed_at=closed_at)
    )
    db.session.flush()


def delete_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete a tournament."""
    db.session.execute(delete(DbTournament).filter_by(id=tournament_id))
    if commit:
        db.session.commit()


def clear_winner_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """NULL-out winner_team_id and winner_participant_id so the
    tournament row can be deleted without FK violations."""
    db.session.execute(
        update(DbTournament)
        .filter_by(id=tournament_id)
        .values(winner_team_id=None, winner_participant_id=None)
    )
    if commit:
        db.session.commit()


def clear_winner_team_reference(team_id: TournamentTeamID) -> None:
    """NULL-out winner_team_id on any tournament referencing this team
    so the team row can be deleted without FK violations."""
    db.session.execute(
        update(DbTournament)
        .filter_by(winner_team_id=team_id)
        .values(winner_team_id=None)
    )
    db.session.commit()


def clear_winner_participant_reference_flush(
    participant_id: TournamentParticipantID,
) -> None:
    """NULL-out winner_participant_id on any tournament referencing this
    participant so the participant row can be deleted without FK violations.
    Flush only — caller owns the transaction."""
    db.session.execute(
        update(DbTournament)
        .filter_by(winner_participant_id=participant_id)
        .values(winner_participant_id=None)
    )
    db.session.flush()


def find_tournament(
    tournament_id: TournamentID,
) -> Tournament | None:
    """Return the tournament, or `None` if not found."""
    db_tournament = db.session.get(DbTournament, tournament_id)
    if db_tournament is None:
        return None
    return _db_tournament_to_tournament(db_tournament)


def get_tournament(
    tournament_id: TournamentID,
    *,
    fresh: bool = False,
) -> Tournament:
    """Return the tournament.

    Use `fresh` to re-read after locking without flushing queued writes.
    Raise an exception if not found.
    """
    if fresh:
        with db.session.no_autoflush:
            db_tournament = db.session.get(
                DbTournament, tournament_id, populate_existing=True
            )
            tournament = (
                _db_tournament_to_tournament(db_tournament)
                if db_tournament is not None
                else None
            )
    else:
        tournament = find_tournament(tournament_id)
    if tournament is None:
        raise ValueError(f'Unknown tournament ID "{tournament_id}"')
    return tournament


def lock_tournament_for_update(tournament_id: TournamentID) -> None:
    """Acquire row-level lock on tournament for atomic bracket generation.

    Uses SELECT FOR UPDATE to prevent concurrent bracket generation.
    Lock is automatically released when transaction commits/rolls back.
    """
    from sqlalchemy import text

    db.session.execute(
        text(
            'SELECT id FROM lan_tournaments WHERE id = :tournament_id FOR UPDATE'
        ),
        {'tournament_id': str(tournament_id)},
    )


def get_tournament_for_update(
    tournament_id: TournamentID,
) -> Tournament:
    """Return the tournament with row lock for update.

    Raise an exception if not found.
    """
    db_tournament = db.session.execute(
        select(DbTournament)
        .filter_by(id=tournament_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_tournament is None:
        raise ValueError(f'Unknown tournament ID "{tournament_id}"')
    return _db_tournament_to_tournament(db_tournament)


def find_tournament_by_creation_token(
    creation_token: UUID,
) -> Tournament | None:
    """Return the tournament created with the token, if any."""
    db_tournament = db.session.execute(
        select(DbTournament).where(
            DbTournament.creation_token == creation_token
        )
    ).scalar_one_or_none()
    if db_tournament is None:
        return None

    return _db_tournament_to_tournament(db_tournament)


def get_tournaments_for_party(
    party_id: PartyID,
) -> list[Tournament]:
    """Return all tournaments for that party."""
    db_tournaments = (
        db.session.execute(
            select(DbTournament)
            .filter_by(party_id=party_id)
            .order_by(DbTournament.position)
        )
        .scalars()
        .all()
    )
    return [_db_tournament_to_tournament(t) for t in db_tournaments]


def get_max_position_for_party(party_id: PartyID) -> int:
    """Return the maximum position value among tournaments for the party.

    Returns 0 if no tournaments exist for the party.
    """
    result = db.session.execute(
        select(func.max(DbTournament.position)).filter_by(party_id=party_id)
    ).scalar_one()
    return result if result is not None else 0


def reorder_tournaments(tournament_ids: list[str]) -> None:
    """Update each tournament's position to its index in the list.

    Performs a bulk update in a single transaction.
    """
    for index, tid in enumerate(tournament_ids):
        db.session.execute(
            update(DbTournament)
            .where(DbTournament.id == tid)
            .values(position=index)
        )
    db.session.commit()


def set_tournament_winner(
    tournament_id: TournamentID,
    *,
    winner_team_id: TournamentTeamID | None,
    winner_participant_id: TournamentParticipantID | None,
) -> Result[None, str]:
    """Store the winner on the tournament row (flush only)."""
    db_tournament = db.session.get(DbTournament, tournament_id)
    if db_tournament is None:
        return Err(f'Unknown tournament ID "{tournament_id}"')
    db_tournament.winner_team_id = winner_team_id
    db_tournament.winner_participant_id = winner_participant_id
    db.session.flush()
    return Ok(None)


class StatusClockEdge(NamedTuple):
    """The operational clock after a status write, and when it moved."""

    at: datetime
    clock: OperationalClock


def set_tournament_status_flush(
    tournament_id: TournamentID,
    status: TournamentStatus,
    *,
    changed_at: datetime | None = None,
) -> Result[StatusClockEdge | None, str]:
    """Update the tournament status and its operational clock (flush only).

    Every status writer ends here, so the clock follows each of them:
    the start activates it, leaving `ONGOING` freezes it and a resume
    continues it. Call it under the tournament lock. The status is read
    from the row, never from a caller's view of it.

    Return the clock and the time it moved at, or `None` for a tournament
    without a known clock history, whose history is never invented. An
    edge the clock cannot follow refuses only a tournament whose clock
    is known: for the others there is no clock to protect, and an
    ordinary status write is not refused for want of one.
    """
    db_tournament = db.session.get(
        DbTournament, tournament_id, populate_existing=True
    )
    if db_tournament is None:
        return Err(f'Unknown tournament ID "{tournament_id}"')

    at = (
        _naive_utc(changed_at)
        if changed_at is not None
        else get_operation_time()
    )
    clock = OperationalClock(
        elapsed_us=db_tournament.operational_clock_elapsed_us,
        running_since=db_tournament.operational_clock_running_since,
        activated_at=db_tournament.operational_clock_activated_at,
    )
    moved = transition_clock(
        clock,
        _safe_enum_lookup(TournamentStatus, db_tournament.tournament_status),
        status,
        at,
    )
    if moved.is_ok():
        new_clock = moved.unwrap()
    elif clock.activated_at is not None:
        return Err(moved.unwrap_err())
    else:
        new_clock = clock

    db_tournament.tournament_status = status.name
    db_tournament.operational_clock_elapsed_us = new_clock.elapsed_us
    db_tournament.operational_clock_running_since = new_clock.running_since
    db_tournament.operational_clock_activated_at = new_clock.activated_at
    db.session.flush()

    if new_clock.activated_at is None:
        return Ok(None)
    return Ok(StatusClockEdge(at=at, clock=new_clock))


def get_participant_count(
    tournament_id: TournamentID,
) -> int:
    """Return the number of active participants."""
    return db.session.execute(
        select(db.func.count(DbTournamentParticipant.id))
        .filter_by(tournament_id=tournament_id)
        .where(DbTournamentParticipant.removed_at.is_(None))
    ).scalar_one()


def _db_tournament_to_tournament(
    db_tournament: DbTournament,
) -> Tournament:
    return Tournament(
        id=db_tournament.id,
        party_id=db_tournament.party_id,
        name=db_tournament.name,
        category=TournamentCategory(db_tournament.category),
        game=db_tournament.game,
        description=db_tournament.description,
        image_url=db_tournament.image_url,
        ruleset=db_tournament.ruleset,
        start_time=db_tournament.start_time,
        created_at=db_tournament.created_at,
        updated_at=db_tournament.updated_at,
        min_players=db_tournament.min_players,
        max_players=db_tournament.max_players,
        min_teams=db_tournament.min_teams,
        max_teams=db_tournament.max_teams,
        min_players_in_team=db_tournament.min_players_in_team,
        max_players_in_team=db_tournament.max_players_in_team,
        contestant_type=_derive_contestant_type(
            _safe_enum_lookup(ContestantType, db_tournament.contestant_type),
            db_tournament.max_players_in_team,
            db_tournament.min_players_in_team,
        ),
        tournament_status=_safe_enum_lookup(
            TournamentStatus, db_tournament.tournament_status
        ),
        game_format=_safe_enum_lookup(
            GameFormat, db_tournament.game_format
        ),
        elimination_mode=_safe_enum_lookup(
            EliminationMode, db_tournament.elimination_mode
        ),
        score_ordering=_safe_enum_lookup(
            ScoreOrdering, db_tournament.score_ordering
        ),
        point_table=(
            json.loads(db_tournament.point_table)
            if db_tournament.point_table
            else None
        ),
        advancement_count=db_tournament.advancement_count,
        group_size_min=db_tournament.group_size_min,
        group_size_max=db_tournament.group_size_max,
        points_carry_to_losers=db_tournament.points_carry_to_losers,
        position=db_tournament.position,
        use_bracket_reset=db_tournament.use_bracket_reset,
        winner_team_id=db_tournament.winner_team_id,
        winner_participant_id=db_tournament.winner_participant_id,
        created_from_request_id=cast(
            'TournamentRequestID | None', db_tournament.created_from_request_id
        ),
        image_id=(
            TournamentImageID(db_tournament.image_id)
            if db_tournament.image_id is not None
            else None
        ),
        image_alt_text=db_tournament.image_alt_text,
        creation_token=db_tournament.creation_token,
        playoff_game_format=_safe_enum_lookup(
            GameFormat, db_tournament.playoff_game_format
        ),
        playoff_elimination_mode=_safe_enum_lookup(
            EliminationMode, db_tournament.playoff_elimination_mode
        ),
        playoff_group_count=db_tournament.playoff_group_count,
        playoff_qualifiers_per_group=db_tournament.playoff_qualifiers_per_group,
        playoff_qualifier_count=db_tournament.playoff_qualifier_count,
        playoff_release_mode=_safe_enum_lookup(
            PlayoffReleaseMode, db_tournament.playoff_release_mode
        ),
        playoff_auto_release_suspended=(
            db_tournament.playoff_auto_release_suspended
        ),
        playoff_released_at=db_tournament.playoff_released_at,
        playoff_released_by=db_tournament.playoff_released_by,
        leaderboard_closed_at=db_tournament.leaderboard_closed_at,
        operational_clock_elapsed_us=(
            db_tournament.operational_clock_elapsed_us
        ),
        operational_clock_running_since=(
            db_tournament.operational_clock_running_since
        ),
        operational_clock_activated_at=(
            db_tournament.operational_clock_activated_at
        ),
    )


def _enum_name(member: Enum | None) -> str | None:
    return member.name if member is not None else None


# -- team --


def create_team(team: TournamentTeam) -> None:
    """Persist a team (flush only — caller commits)."""
    db_team = DbTournamentTeam(
        team.id,
        team.tournament_id,
        team.name,
        team.captain_user_id,
        team.created_at,
        tag=team.tag,
        description=team.description,
        image_url=team.image_url,
        join_code=team.join_code,
    )

    db.session.add(db_team)
    db.session.flush()


def update_team(team: TournamentTeam) -> None:
    """Update a team in place (no delete/recreate)."""
    db_team = db.session.get(DbTournamentTeam, team.id)
    if db_team is None:
        raise ValueError(f'Unknown team ID "{team.id}"')

    db_team.name = team.name
    db_team.tag = team.tag
    db_team.description = team.description
    db_team.image_url = team.image_url
    db_team.join_code = team.join_code
    db_team.updated_at = team.updated_at

    db.session.commit()


def update_team_captain(
    team_id: TournamentTeamID,
    new_captain_user_id: UserID,
) -> None:
    """Update the captain of a team."""
    update_team_captain_flush(team_id, new_captain_user_id)
    db.session.commit()


def update_team_captain_flush(
    team_id: TournamentTeamID,
    new_captain_user_id: UserID,
) -> None:
    """Update the captain; caller owns commit."""
    db_team = db.session.get(DbTournamentTeam, team_id)
    if db_team is None:
        raise ValueError(f'Unknown team ID "{team_id}"')
    db_team.captain_user_id = new_captain_user_id
    db.session.flush()


def delete_team(team_id: TournamentTeamID) -> None:
    """Delete a team."""
    db.session.execute(delete(DbTournamentTeam).filter_by(id=team_id))
    db.session.commit()


def delete_team_flush(team_id: TournamentTeamID) -> None:
    """Delete a team (flush only, caller commits)."""
    db.session.execute(delete(DbTournamentTeam).filter_by(id=team_id))
    db.session.flush()


def delete_teams_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all teams for a tournament."""
    delete_teams_for_tournament_flush(tournament_id)
    if commit:
        db.session.commit()


def delete_teams_for_tournament_flush(tournament_id: TournamentID) -> None:
    """Delete teams; caller owns commit and dependent-row cleanup."""
    db.session.execute(
        delete(DbTournamentTeam).filter_by(tournament_id=tournament_id)
    )
    db.session.flush()


def find_team(
    team_id: TournamentTeamID,
) -> TournamentTeam | None:
    """Return the team, or `None` if not found."""
    db_team = db.session.get(DbTournamentTeam, team_id)
    if db_team is None:
        return None
    return _db_team_to_team(db_team)


def get_team(team_id: TournamentTeamID) -> TournamentTeam:
    """Return the team.

    Raise an exception if not found.
    """
    team = find_team(team_id)
    if team is None:
        raise ValueError(f'Unknown team ID "{team_id}"')
    return team


def get_team_for_update(team_id: TournamentTeamID) -> TournamentTeam:
    """Return the team with row lock for update.

    Raise an exception if not found.
    """
    db_team = db.session.execute(
        select(DbTournamentTeam)
        .filter_by(id=team_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_team is None:
        raise ValueError(f'Unknown team ID "{team_id}"')
    return _db_team_to_team(db_team)


def get_teams_for_tournament(
    tournament_id: TournamentID,
    *,
    include_removed: bool = False,
) -> list[TournamentTeam]:
    """Return all teams for that tournament."""
    stmt = select(DbTournamentTeam).filter_by(tournament_id=tournament_id)
    if not include_removed:
        stmt = stmt.where(DbTournamentTeam.removed_at.is_(None))
    stmt = stmt.order_by(DbTournamentTeam.created_at, DbTournamentTeam.id)
    db_teams = db.session.execute(stmt).scalars().all()
    return [_db_team_to_team(t) for t in db_teams]


def find_active_team_by_name(
    tournament_id: TournamentID,
    name: str,
) -> TournamentTeam | None:
    """Return the active team with that name in the tournament,
    or `None`.
    """
    db_team = db.session.execute(
        select(DbTournamentTeam).where(
            DbTournamentTeam.tournament_id == tournament_id,
            db.func.lower(DbTournamentTeam.name) == name.lower(),
            DbTournamentTeam.removed_at.is_(None),
        )
    ).scalar_one_or_none()
    if db_team is None:
        return None
    return _db_team_to_team(db_team)


def find_active_team_by_tag(
    tournament_id: TournamentID,
    tag: str,
) -> TournamentTeam | None:
    """Return the active team with that tag in the tournament,
    or `None`.
    """
    db_team = db.session.execute(
        select(DbTournamentTeam).where(
            DbTournamentTeam.tournament_id == tournament_id,
            db.func.upper(DbTournamentTeam.tag) == tag.upper(),
            DbTournamentTeam.removed_at.is_(None),
        )
    ).scalar_one_or_none()
    if db_team is None:
        return None
    return _db_team_to_team(db_team)


def get_teams_by_ids(
    team_ids: set[TournamentTeamID],
) -> list[TournamentTeam]:
    """Return teams matching the given IDs."""
    if not team_ids:
        return []
    db_teams = (
        db.session.execute(
            select(DbTournamentTeam).where(DbTournamentTeam.id.in_(team_ids))
        )
        .scalars()
        .all()
    )
    return [_db_team_to_team(t) for t in db_teams]


def _db_team_to_team(db_team: DbTournamentTeam) -> TournamentTeam:
    return TournamentTeam(
        id=db_team.id,
        tournament_id=db_team.tournament_id,
        name=db_team.name,
        tag=db_team.tag,
        description=db_team.description,
        image_url=db_team.image_url,
        captain_user_id=db_team.captain_user_id,
        join_code=db_team.join_code,
        created_at=db_team.created_at,
        updated_at=db_team.updated_at,
        removed_at=db_team.removed_at,
    )


# -- participant --


def create_participant(
    participant: TournamentParticipant,
) -> None:
    """Persist a participant."""
    db_participant = DbTournamentParticipant(
        participant.id,
        participant.user_id,
        participant.tournament_id,
        participant.created_at,
        substitute_player=participant.substitute_player,
        team_id=participant.team_id,
    )

    db.session.add(db_participant)
    db.session.flush()


def update_participant(participant: TournamentParticipant) -> None:
    """Update a participant in place (no delete/recreate)."""
    update_participant_flush(participant)
    db.session.commit()


def update_participant_flush(participant: TournamentParticipant) -> None:
    """Update membership; caller owns commit."""
    db_participant = db.session.get(DbTournamentParticipant, participant.id)
    if db_participant is None:
        raise ValueError(f'Unknown participant ID "{participant.id}"')

    db_participant.substitute_player = participant.substitute_player
    db_participant.team_id = participant.team_id

    db.session.flush()


def delete_participant(
    participant_id: TournamentParticipantID,
) -> None:
    """Delete a participant."""
    db.session.execute(
        delete(DbTournamentParticipant).filter_by(id=participant_id)
    )
    db.session.commit()


def delete_participants_by_ids(
    participant_ids: set[TournamentParticipantID],
) -> None:
    """Delete multiple participants (flush only, caller commits)."""
    if not participant_ids:
        return
    db.session.execute(
        delete(DbTournamentParticipant).where(
            DbTournamentParticipant.id.in_(participant_ids)
        )
    )
    db.session.flush()


def delete_participants_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all participants for a tournament."""
    delete_participants_for_tournament_flush(tournament_id)
    if commit:
        db.session.commit()


def delete_participants_for_tournament_flush(tournament_id: TournamentID) -> None:
    """Delete participants; caller owns commit and dependent-row cleanup."""
    db.session.execute(
        delete(DbTournamentParticipant).filter_by(tournament_id=tournament_id)
    )
    db.session.flush()


def remove_team_from_participants(team_id: TournamentTeamID) -> None:
    """Set team_id to NULL for all participants in this team."""
    remove_team_from_participants_flush(team_id)
    db.session.commit()


def remove_team_from_participants_flush(
    team_id: TournamentTeamID,
) -> None:
    """Set team_id to NULL for all participants in this team
    (flush only, caller commits)."""
    db.session.execute(
        db.update(DbTournamentParticipant)
        .filter_by(team_id=team_id)
        .values(team_id=None)
    )
    db.session.flush()


def soft_delete_participants_by_ids(
    participant_ids: set[TournamentParticipantID],
    removed_at: datetime,
) -> None:
    """Soft-delete participants by setting removed_at
    (flush only, caller commits)."""
    if not participant_ids:
        return
    db.session.execute(
        db.update(DbTournamentParticipant)
        .where(DbTournamentParticipant.id.in_(participant_ids))
        .values(removed_at=removed_at)
    )
    db.session.flush()


def soft_delete_team_flush(
    team_id: TournamentTeamID,
    removed_at: datetime,
) -> None:
    """Soft-delete a team by setting removed_at
    (flush only, caller commits)."""
    db.session.execute(
        db.update(DbTournamentTeam)
        .where(DbTournamentTeam.id == team_id)
        .values(removed_at=removed_at)
    )
    db.session.flush()


def find_participant(
    participant_id: TournamentParticipantID,
) -> TournamentParticipant | None:
    """Return the participant, or `None` if not found."""
    db_participant = db.session.get(DbTournamentParticipant, participant_id)
    if db_participant is None:
        return None
    return _db_participant_to_participant(db_participant)


def find_participant_fresh(
    participant_id: TournamentParticipantID,
) -> TournamentParticipant | None:
    """Reload membership after acquiring the owning tournament lock."""
    row = db.session.get(
        DbTournamentParticipant, participant_id, populate_existing=True
    )
    return _db_participant_to_participant(row) if row is not None else None


def get_participant_for_update(
    participant_id: TournamentParticipantID,
) -> TournamentParticipant:
    """Lock and refresh membership; caller must lock its tournament first."""
    row = db.session.execute(
        select(DbTournamentParticipant)
        .filter_by(id=participant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f'Unknown participant ID "{participant_id}"')
    return _db_participant_to_participant(row)


def get_participants_for_update(
    participant_ids: Collection[TournamentParticipantID],
) -> list[TournamentParticipant]:
    """Lock and refresh those participants in ID order; unknown IDs are
    omitted. The caller must lock its tournament first.
    """
    ids = {UUID(str(participant_id)) for participant_id in participant_ids}
    if not ids:
        return []
    rows = db.session.scalars(
        select(DbTournamentParticipant)
        .where(DbTournamentParticipant.id.in_(ids))
        .order_by(DbTournamentParticipant.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return [_db_participant_to_participant(row) for row in rows]


def find_active_participant_by_user(
    tournament_id: TournamentID,
    user_id: UserID,
) -> TournamentParticipant | None:
    """Return the active (non-removed) participant for a user in a
    tournament, or `None` if not found.
    """
    stmt = (
        select(DbTournamentParticipant)
        .filter_by(tournament_id=tournament_id, user_id=user_id)
        .where(DbTournamentParticipant.removed_at.is_(None))
    )
    db_participant = db.session.execute(stmt).scalars().first()
    if db_participant is None:
        return None
    return _db_participant_to_participant(db_participant)


def get_participant(
    participant_id: TournamentParticipantID,
) -> TournamentParticipant:
    """Return the participant.

    Raise an exception if not found.
    """
    participant = find_participant(participant_id)
    if participant is None:
        raise ValueError(f'Unknown participant ID "{participant_id}"')
    return participant


def find_participant_by_user(
    tournament_id: TournamentID,
    user_id: UserID,
) -> TournamentParticipant | None:
    """Return the active participant for a user in a tournament,
    or `None`."""
    db_participant = db.session.execute(
        select(DbTournamentParticipant)
        .filter_by(
            tournament_id=tournament_id,
            user_id=user_id,
        )
        .where(DbTournamentParticipant.removed_at.is_(None))
    ).scalar_one_or_none()
    if db_participant is None:
        return None
    return _db_participant_to_participant(db_participant)


def find_soft_deleted_participant_by_user(
    tournament_id: TournamentID,
    user_id: UserID,
) -> TournamentParticipant | None:
    """Return a soft-deleted participant for a user in a tournament,
    or `None`."""
    db_participant = db.session.execute(
        select(DbTournamentParticipant)
        .filter_by(
            tournament_id=tournament_id,
            user_id=user_id,
        )
        .where(DbTournamentParticipant.removed_at.is_not(None))
    ).scalar_one_or_none()
    if db_participant is None:
        return None
    return _db_participant_to_participant(db_participant)


def reactivate_participant(
    participant_id: TournamentParticipantID,
    *,
    substitute_player: bool,
    team_id: TournamentTeamID | None,
    created_at: datetime,
) -> None:
    """Reactivate a soft-deleted participant."""
    db_participant = db.session.get(DbTournamentParticipant, participant_id)
    if db_participant is None:
        raise ValueError(f'Unknown participant ID "{participant_id}"')

    db_participant.removed_at = None
    db_participant.substitute_player = substitute_player
    db_participant.team_id = team_id
    db_participant.created_at = created_at
    db.session.flush()


def get_participants_for_tournament(
    tournament_id: TournamentID,
    *,
    include_removed: bool = False,
) -> list[TournamentParticipant]:
    """Return all participants for that tournament."""
    stmt = select(DbTournamentParticipant).filter_by(
        tournament_id=tournament_id
    )
    if not include_removed:
        stmt = stmt.where(DbTournamentParticipant.removed_at.is_(None))
    stmt = stmt.order_by(
        DbTournamentParticipant.created_at, DbTournamentParticipant.id
    )
    db_participants = db.session.execute(stmt).scalars().all()
    return [_db_participant_to_participant(p) for p in db_participants]


def get_contestant_ids_removed_since_phase_start(
    tournament_id: TournamentID, phase: int, *, teams: bool
) -> frozenset[str]:
    """Return the contestants removed after the phase's first match was made.

    Compares in SQL: PostgreSQL casts the TIMESTAMP `created_at` with the
    session time zone it was written in, so it meets a TIMESTAMPTZ
    `removed_at` correctly.
    """
    started = (
        select(func.min(DbTournamentMatch.created_at))
        .where(
            DbTournamentMatch.tournament_id == tournament_id,
            DbTournamentMatch.phase == phase,
        )
        .scalar_subquery()
    )
    model = DbTournamentTeam if teams else DbTournamentParticipant
    ids = db.session.scalars(
        select(model.id).where(
            model.tournament_id == tournament_id,
            model.removed_at.is_not(None),
            model.removed_at >= started,
        )
    ).all()
    return frozenset(str(i) for i in ids)


def get_participants_for_team(
    team_id: TournamentTeamID,
    *,
    include_removed: bool = False,
) -> list[TournamentParticipant]:
    """Return all participants for that team."""
    stmt = select(DbTournamentParticipant).filter_by(team_id=team_id)
    if not include_removed:
        stmt = stmt.where(DbTournamentParticipant.removed_at.is_(None))
    db_participants = db.session.execute(stmt).scalars().all()
    return [_db_participant_to_participant(p) for p in db_participants]


def get_team_member_counts(
    tournament_id: TournamentID,
) -> dict[TournamentTeamID, int]:
    """Return active member count per team in a single query."""
    rows = (
        db.session.execute(
            select(
                DbTournamentParticipant.team_id,
                db.func.count(DbTournamentParticipant.id),
            )
            .filter_by(tournament_id=tournament_id)
            .where(
                DbTournamentParticipant.team_id.is_not(None),
                DbTournamentParticipant.removed_at.is_(None),
            )
            .group_by(DbTournamentParticipant.team_id)
        )
        .tuples()
        .all()
    )
    return dict(rows)


def get_participant_counts_for_tournaments(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, int]:
    """Return active participant counts per tournament in a single query."""
    if not tournament_ids:
        return {}
    rows = (
        db.session.execute(
            select(
                DbTournamentParticipant.tournament_id,
                db.func.count(DbTournamentParticipant.id),
            )
            .where(
                DbTournamentParticipant.tournament_id.in_(tournament_ids),
                DbTournamentParticipant.removed_at.is_(None),
            )
            .group_by(DbTournamentParticipant.tournament_id)
        )
        .tuples()
        .all()
    )
    return dict(rows)


def get_team_counts_for_tournaments(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, int]:
    """Return active team counts per tournament in a single query."""
    if not tournament_ids:
        return {}
    rows = (
        db.session.execute(
            select(
                DbTournamentTeam.tournament_id,
                db.func.count(DbTournamentTeam.id),
            )
            .where(
                DbTournamentTeam.tournament_id.in_(tournament_ids),
                DbTournamentTeam.removed_at.is_(None),
            )
            .group_by(DbTournamentTeam.tournament_id)
        )
        .tuples()
        .all()
    )
    return dict(rows)


def _db_participant_to_participant(
    db_participant: DbTournamentParticipant,
) -> TournamentParticipant:
    return TournamentParticipant(
        id=db_participant.id,
        user_id=db_participant.user_id,
        tournament_id=db_participant.tournament_id,
        substitute_player=db_participant.substitute_player,
        team_id=db_participant.team_id,
        created_at=db_participant.created_at,
        removed_at=db_participant.removed_at,
    )


# -- match --


def commit_session() -> None:
    """Commit the current database session."""
    db.session.commit()


def rollback_session() -> None:
    """Roll back the current database session."""
    db.session.rollback()


def create_match(
    match: TournamentMatch, *, changed_at: datetime | None = None
) -> None:
    """Persist a match and initialize its last-change fact.

    The fact is the match's own `last_changed_at`, else `changed_at`,
    else the server operation time.
    """
    db_match = DbTournamentMatch(
        match.id,
        match.tournament_id,
        match.created_at,
        group_order=match.group_order,
        match_order=match.match_order,
        round=match.round,
        next_match_id=match.next_match_id,
        bracket=match.bracket.value if match.bracket else None,
        loser_next_match_id=match.loser_next_match_id,
        confirmed_by=match.confirmed_by,
        phase=match.phase,
        seeding_target=match.seeding_target,
        last_changed_at=_initial_last_changed_at(match, changed_at),
    )

    db.session.add(db_match)
    db.session.flush()


def delete_match(match_id: TournamentMatchID) -> None:
    """Delete a match."""
    delete_match_flush(match_id)
    db.session.commit()


def get_matches_for_seeding_target(
    tournament_id: TournamentID, seeding_target: str
) -> list[TournamentMatch]:
    """Return the matches a seeding draft generated for its target."""
    db_matches = (
        db.session.execute(
            select(DbTournamentMatch)
            .filter_by(
                tournament_id=tournament_id, seeding_target=seeding_target
            )
            .order_by(
                DbTournamentMatch.round,
                DbTournamentMatch.group_order.asc().nulls_last(),
                DbTournamentMatch.match_order,
                DbTournamentMatch.id,
            )
        )
        .scalars()
        .all()
    )
    return [_db_match_to_match(m) for m in db_matches]


def _retire_dashboard_state_flush(
    match_ids: Collection[TournamentMatchID], changed_at: datetime | None
) -> None:
    """Close the episodes and drop the pins of matches that are going away.

    Call it with the tournament locked, before the matches are deleted,
    so that no live annotation outlives its match. The server clock is
    sampled only if there is something to retire.
    """
    if match_ids:
        retire_dashboard_matches_flush(
            match_ids, occurred_at=_resolve_changed_at(changed_at)
        )


def delete_match_flush(
    match_id: TournamentMatchID, *, changed_at: datetime | None = None
) -> None:
    """Delete a match (flush only - caller owns commit).

    Its open due episode closes as history, and its live pin goes with it.
    An owner with an operation time passes it as `changed_at`.
    """
    _retire_match_pairing_flush(match_id)
    _retire_dashboard_state_flush([match_id], changed_at)
    db.session.execute(
        delete(DbTournamentMatch).filter_by(id=match_id)
    )
    db.session.flush()



def null_self_referential_fks(tournament_id: TournamentID) -> None:
    """NULL out next_match_id and loser_next_match_id for all matches
    in the tournament, removing self-referential FK constraints that
    would block individual match deletion."""
    db.session.execute(
        db.update(DbTournamentMatch)
        .filter_by(tournament_id=tournament_id)
        .values(next_match_id=None, loser_next_match_id=None)
    )
    db.session.flush()


def delete_matches_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all matches for a tournament.

    Their open due episodes close as history, and their live pins go.
    """
    lock_tournament_for_update(tournament_id)
    matches = get_matches_for_tournament_ordered_fresh(tournament_id)
    lock_matches_for_update([match.id for match in matches])
    for match in sorted(matches, key=lambda match: str(match.id)):
        _retire_match_pairing_flush(match.id)
    _retire_dashboard_state_flush([match.id for match in matches], None)
    null_self_referential_fks(tournament_id)
    db.session.execute(
        delete(DbTournamentMatch).filter_by(tournament_id=tournament_id)
    )
    if commit:
        db.session.commit()


def find_match(
    match_id: TournamentMatchID,
) -> TournamentMatch | None:
    """Return the match, or `None` if not found."""
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        return None
    return _db_match_to_match(db_match)


def find_match_fresh(
    match_id: TournamentMatchID,
) -> TournamentMatch | None:
    """Return the match, or `None` if not found, freshly loaded.

    Use this to re-read a match after locking it.
    """
    db_match = db.session.get(
        DbTournamentMatch, match_id, populate_existing=True
    )
    if db_match is None:
        return None
    return _db_match_to_match(db_match)


def get_match(
    match_id: TournamentMatchID,
) -> TournamentMatch:
    """Return the match.

    Raise an exception if not found.
    """
    match = find_match(match_id)
    if match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    return match


def get_match_for_update(
    match_id: TournamentMatchID,
) -> TournamentMatch:
    """Return the match with a row-level lock.

    Issues ``SELECT ... FOR UPDATE`` to prevent concurrent
    modification (TOCTOU race on confirm).

    Raise an exception if not found.
    """
    db_match = db.session.execute(
        select(DbTournamentMatch)
        .filter_by(id=match_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if db_match is None:
        raise ValueError(
            f'Unknown match ID "{match_id}"'
        )
    return _db_match_to_match(db_match)


def lock_matches_for_update(
    match_ids: list[TournamentMatchID],
) -> None:
    """Lock those matches, in ID order to avoid deadlocks."""
    if not match_ids:
        return

    db.session.execute(
        select(DbTournamentMatch.id)
        .filter(DbTournamentMatch.id.in_(match_ids))
        .order_by(DbTournamentMatch.id)
        .with_for_update()
    ).all()


def get_matches_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentMatch]:
    """Return all matches for that tournament."""
    db_matches = (
        db.session.execute(
            select(DbTournamentMatch).filter_by(tournament_id=tournament_id)
        )
        .scalars()
        .all()
    )
    return [_db_match_to_match(m) for m in db_matches]


def get_matches_for_tournaments(
    tournament_ids: Collection[TournamentID],
) -> list[TournamentMatch]:
    """Return all matches of those tournaments, with one query."""
    if not tournament_ids:
        return []

    db_matches = db.session.scalars(
        select(DbTournamentMatch).where(
            DbTournamentMatch.tournament_id.in_(list(tournament_ids))
        )
    ).all()
    return [_db_match_to_match(m) for m in db_matches]


def get_matches_for_tournament_ordered(
    tournament_id: TournamentID,
) -> list[TournamentMatch]:
    """Return all matches for that tournament, ordered by round."""
    db_matches = (
        db.session.execute(
            select(DbTournamentMatch)
            .filter_by(tournament_id=tournament_id)
            .order_by(
                DbTournamentMatch.round,
                DbTournamentMatch.match_order,
            )
        )
        .scalars()
        .all()
    )
    return [_db_match_to_match(m) for m in db_matches]


def get_matches_for_tournament_ordered_fresh(
    tournament_id: TournamentID,
) -> list[TournamentMatch]:
    """Return the tournament's matches in order, freshly loaded.

    Use this to re-read the bracket after locking it.
    """
    db_matches = (
        db.session.execute(
            select(DbTournamentMatch)
            .filter_by(tournament_id=tournament_id)
            .order_by(
                DbTournamentMatch.round,
                DbTournamentMatch.match_order,
            )
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    return [_db_match_to_match(m) for m in db_matches]


def get_matches_by_ids(
    match_ids: list[TournamentMatchID],
) -> list[TournamentMatch]:
    """Return the matches with those IDs, in arbitrary order."""
    if not match_ids:
        return []

    db_matches = (
        db.session.execute(
            select(DbTournamentMatch).filter(
                DbTournamentMatch.id.in_(match_ids)
            )
        )
        .scalars()
        .all()
    )
    return [_db_match_to_match(m) for m in db_matches]


def get_matches_for_round(
    tournament_id: TournamentID,
    round_number: int,
    *,
    bracket: Bracket | None = None,
) -> list[TournamentMatch]:
    """Return all matches for a specific round of a tournament.

    Optionally filter by bracket (WB/LB/GF).
    """
    query = (
        select(DbTournamentMatch)
        .filter_by(tournament_id=tournament_id, round=round_number)
    )
    if bracket is not None:
        query = query.filter(DbTournamentMatch.bracket == bracket.value)
    query = query.order_by(
        DbTournamentMatch.group_order.asc().nulls_last(),
        DbTournamentMatch.match_order,
        DbTournamentMatch.id,
    )
    db_matches = db.session.execute(query).scalars().all()
    return [_db_match_to_match(m) for m in db_matches]


def confirm_match(
    match_id: TournamentMatchID,
    confirmed_by: UserID,
    *,
    changed_at: datetime | None = None,
) -> None:
    """Set the confirmed_by field on a match."""
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')

    _touch_match_if(
        match_id,
        changed_at,
        DbTournamentMatch.confirmed_by.is_distinct_from(confirmed_by),
    )
    db_match.confirmed_by = confirmed_by
    db.session.flush()


def unconfirm_match(
    match_id: TournamentMatchID,
    *,
    reset_readiness: bool = True,
    changed_at: datetime | None = None,
) -> None:
    """Reset the confirmed_by field on a match."""
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')

    _touch_match_if(
        match_id, changed_at, DbTournamentMatch.confirmed_by.is_not(None)
    )
    db_match.confirmed_by = None
    if reset_readiness:
        clear_match_readiness_flush(match_id, increment_revision=True)
        suppress_match_invitations_flush(match_id, reason='readiness_reset')
    db.session.flush()


def clear_loser_next_match_id(
    match_id: TournamentMatchID,
) -> None:
    """Remove loser routing from a match (e.g. DEFWIN match
    with no loser to route).
    """
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is not None:
        db_match.loser_next_match_id = None
        db.session.flush()


def clear_next_match_id(
    match_id: TournamentMatchID,
) -> None:
    """Remove winner routing from a match (e.g. dead LB
    match with no incoming feeds).
    """
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is not None:
        db_match.next_match_id = None
        db.session.flush()


def set_next_match_id_flush(
    match_id: TournamentMatchID,
    next_match_id: TournamentMatchID | None,
) -> None:
    """Set or clear winner routing on a match (flush only)."""
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is not None:
        db_match.next_match_id = next_match_id
        db.session.flush()


def count_incoming_feeds(
    match_id: TournamentMatchID,
) -> int:
    """Count matches that route winners or losers to this
    match.
    """
    return (
        db.session.scalar(
            db.select(db.func.count())
            .select_from(DbTournamentMatch)
            .where(
                (DbTournamentMatch.next_match_id == match_id)
                | (DbTournamentMatch.loser_next_match_id == match_id)
            )
        )
        or 0
    )


def find_feeder_matches(
    match_id: TournamentMatchID,
) -> list[TournamentMatch]:
    """Return matches whose next_match_id or loser_next_match_id
    points to the given match."""
    db_matches = db.session.execute(
        select(DbTournamentMatch).filter(
            (DbTournamentMatch.next_match_id == match_id)
            | (DbTournamentMatch.loser_next_match_id == match_id)
        )
    ).scalars().all()
    return [_db_match_to_match(m) for m in db_matches]


def _safe_bracket_lookup(value: str | None) -> Bracket | None:
    """Safely convert bracket string to enum, returning
    None on invalid values.
    """
    if value is None:
        return None
    try:
        return Bracket(value)
    except ValueError:
        logger.warning(
            'Invalid bracket value %r, returning None',
            value,
        )
        return None


def _db_match_to_match(
    db_match: DbTournamentMatch,
) -> TournamentMatch:
    return TournamentMatch(
        id=db_match.id,
        tournament_id=db_match.tournament_id,
        group_order=db_match.group_order,
        match_order=db_match.match_order,
        round=db_match.round,
        next_match_id=db_match.next_match_id,
        confirmed_by=db_match.confirmed_by,
        created_at=db_match.created_at,
        bracket=_safe_bracket_lookup(db_match.bracket),
        loser_next_match_id=db_match.loser_next_match_id,
        phase=db_match.phase,
        seeding_target=db_match.seeding_target,
        occupied_since=db_match.occupied_since,
        ready_at_a=db_match.ready_at_a,
        ready_at_b=db_match.ready_at_b,
        ready_by_a=db_match.ready_by_a,
        ready_by_b=db_match.ready_by_b,
        both_ready_notified_at=db_match.both_ready_notified_at,
        pairing_generation=db_match.pairing_generation,
        readiness_revision=db_match.readiness_revision,
        pairing_id=db_match.pairing_id,
        invitation_hold_a=db_match.invitation_hold_a,
        invitation_hold_b=db_match.invitation_hold_b,
        last_changed_at=db_match.last_changed_at,
    )


def _db_pairing_to_pairing(row: DbMatchPairing) -> MatchPairing:
    return MatchPairing(
        id=row.id,
        match_id=row.match_id,
        tournament_id=row.tournament_id,
        generation=row.generation,
        side_a=ContestantIdentity(kind=row.side_a_kind, id=row.side_a_id),
        side_b=ContestantIdentity(kind=row.side_b_kind, id=row.side_b_id),
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def get_match_pairing(match_id: TournamentMatchID) -> MatchPairing | None:
    """Return the live pairing pointer's snapshot, not the latest old pair."""
    return get_match_pairings_for_matches([match_id]).get(match_id)


def get_match_pairings_for_matches(
    match_ids: Collection[TournamentMatchID],
) -> dict[TournamentMatchID, MatchPairing]:
    """Read current immutable side snapshots in one query (no lazy loads)."""
    if not match_ids:
        return {}
    rows = db.session.scalars(
        select(DbMatchPairing)
        .join(
            DbTournamentMatch, DbTournamentMatch.pairing_id == DbMatchPairing.id
        )
        .where(DbTournamentMatch.id.in_(match_ids))
        .execution_options(populate_existing=True)
    ).all()
    return {row.match_id: _db_pairing_to_pairing(row) for row in rows}


def get_match_pairing_history(match_id: TournamentMatchID) -> list[MatchPairing]:
    """Read retained occupancy even after live match/log deletion."""
    rows = db.session.scalars(
        select(DbMatchPairing)
        .where(DbMatchPairing.match_id == match_id)
        .order_by(DbMatchPairing.generation, DbMatchPairing.id)
        .execution_options(populate_existing=True)
    ).all()
    return [_db_pairing_to_pairing(row) for row in rows]


def _clear_match_readiness(row: DbTournamentMatch) -> None:
    row.ready_at_a = row.ready_at_b = None
    row.ready_by_a = row.ready_by_b = None
    row.invitation_hold_a = row.invitation_hold_b = False


def clear_match_readiness_flush(
    match_id: TournamentMatchID,
    *,
    increment_revision: bool,
) -> None:
    """Clear claim snapshots/holds without changing original occupancy."""
    row = db.session.get(DbTournamentMatch, match_id)
    if row is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    _clear_match_readiness(row)
    if increment_revision:
        row.readiness_revision += 1
    db.session.flush()


def set_readiness_revision_flush(
    match_id: TournamentMatchID, revision: int,
) -> None:
    """Set an explicitly computed revision under the caller's locks."""
    if revision < 0:
        raise ValueError('readiness_revision_must_be_nonnegative')
    row = db.session.get(DbTournamentMatch, match_id)
    if row is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    row.readiness_revision = revision
    db.session.flush()


def set_side_invitation_hold_flush(
    match_id: TournamentMatchID,
    side: MatchSide,
    held: bool,
) -> None:
    """Set a side's invitation hold; caller owns commit."""
    if side not in (MatchSide.A, MatchSide.B):
        raise ValueError('invalid_match_side')
    row = db.session.get(DbTournamentMatch, match_id)
    if row is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    if side == MatchSide.A:
        row.invitation_hold_a = held
    else:
        row.invitation_hold_b = held
    db.session.flush()


def refresh_match_pairing_flush(
    match_id: TournamentMatchID,
    *,
    occurred_at: datetime,
) -> Result[bool, str]:
    """Refresh logical opponents; caller holds tournament then match locks.

    Identical reseating preserves A/B and generation. This stores facts only:
    service orchestration owns audits, work invalidation and post-commit effects.
    """
    if occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(UTC).replace(tzinfo=None)
    row = db.session.get(DbTournamentMatch, match_id, populate_existing=True)
    if row is None:
        return Err('match_not_found')
    tournament = db.session.get(
        DbTournament, row.tournament_id, populate_existing=True
    )
    if tournament is None:
        return Err('tournament_not_found')
    game_format = None
    if row.phase == 1:
        game_format = tournament.game_format
    elif row.phase == 2:
        game_format = tournament.playoff_game_format
    contestants = db.session.scalars(
        select(DbTournamentMatchToContestant)
        .where(DbTournamentMatchToContestant.tournament_match_id == match_id)
        .order_by(
            DbTournamentMatchToContestant.created_at,
            DbTournamentMatchToContestant.id,
        )
        .execution_options(populate_existing=True)
    ).all()
    identities = [
        ContestantIdentity(kind='participant', id=c.participant_id)
        if c.participant_id is not None
        else ContestantIdentity(kind='team', id=c.team_id)
        for c in contestants
        if c.participant_id is not None or c.team_id is not None
    ]
    eligible = (
        game_format == GameFormat.ONE_V_ONE.name
        and len(identities) == 2
        and identities[0] != identities[1]
        and identities[0].kind == identities[1].kind
    )
    if eligible:
        for identity in identities:
            model = (
                DbTournamentParticipant
                if identity.kind == 'participant'
                else DbTournamentTeam
            )
            member = db.session.get(model, identity.id, populate_existing=True)
            if (
                member is None
                or member.tournament_id != row.tournament_id
                or member.removed_at is not None
            ):
                eligible = False
                break
    old = (
        db.session.get(DbMatchPairing, row.pairing_id, populate_existing=True)
        if row.pairing_id else None
    )
    if old is not None and eligible and old.ended_at is None:
        old_identities = {
            ContestantIdentity(kind=old.side_a_kind, id=old.side_a_id),
            ContestantIdentity(kind=old.side_b_kind, id=old.side_b_id),
        }
        if old_identities == set(identities):
            return Ok(False)
    if old is None and not eligible:
        return Ok(False)
    if old is not None:
        if old.started_at is not None and occurred_at < old.started_at:
            return Err('pairing_time_before_start')
        old.ended_at = occurred_at
        suppress_match_invitations_flush(match_id, reason='pairing_retired')
    row.pairing_id = None
    row.pairing_generation += 1
    row.readiness_revision += 1
    _clear_match_readiness(row)
    if eligible:
        side_a, side_b = identities
        pairing_id = MatchPairingID(uuid7())
        db.session.add(
            DbMatchPairing(
                pairing_id, match_id, row.tournament_id, row.pairing_generation,
                side_a.kind, side_a.id, side_b.kind, side_b.id,
                started_at=occurred_at,
            )
        )
        # Insert retained snapshot before its live FK pointer is updated.
        db.session.flush()
        row.pairing_id = pairing_id
        if row.occupied_since is None:
            row.occupied_since = occurred_at
    db.session.flush()
    return Ok(True)


def suppress_match_invitations_flush(
    match_id: TournamentMatchID, *, reason: str,
) -> None:
    """Retire only retractable work; preserve acceptance and in-flight outcomes.

    Caller holds tournament/match locks. CAS/worker handling of sending and
    delivery_unknown belongs to the invitation service, not this backstop.
    """
    db.session.execute(
        db.update(DbMatchInvitation)
        .where(
            DbMatchInvitation.match_id == match_id,
            DbMatchInvitation.status.in_(
                ['pending', 'dispatching', 'queued', 'failed', 'suppressed']
            ),
            or_(
                DbMatchInvitation.last_error.is_(None),
                DbMatchInvitation.last_error.not_in(
                    _INVITATION_IRREVERSIBLE_REASONS
                ),
            ),
        )
        .values(
            status='suppressed', dispatch_token=None, lease_until=None,
            next_attempt_at=None, last_error=reason,
            attempts=case(
                (
                    DbMatchInvitation.status.in_(['dispatching', 'queued']),
                    func.greatest(DbMatchInvitation.attempts - 1, 0),
                ),
                else_=DbMatchInvitation.attempts,
            ),
        )
    )
    db.session.flush()


INVITATION_MAX_ATTEMPTS = 3
INVITATION_LEASE_SECONDS = 120
INVITATION_RECOVERY_LIMIT = 100
_INVITATION_FINAL_FACTS = {'accepted', 'sending', 'delivery_unknown'}
_INVITATION_IRREVERSIBLE_REASONS = {'tournament_terminal', 'pairing_retired'}
_INVITATION_RESUMABLE_REASONS = {
    'readiness_hold', 'readiness_reset', 'readiness_revision_changed',
    'tournament_paused', 'recipient_not_current',
}


class _InvitationScope:
    """Per-call reads shared by the eligibility checks of many invitations."""

    def __init__(self, tournament_id: TournamentID) -> None:
        self.tournament_id = tournament_id
        self._audiences: dict[TournamentMatchID, set[UserID]] = {}
        self._tournament_read = False
        self._tournament: DbTournament | None = None

    @property
    def tournament(self) -> DbTournament | None:
        if not self._tournament_read:
            self._tournament = db.session.get(
                DbTournament, self.tournament_id, populate_existing=True
            )
            self._tournament_read = True
        return self._tournament

    def audience(self, match: DbTournamentMatch) -> set[UserID]:
        if match.id not in self._audiences:
            self._audiences[match.id] = _current_invitation_audience(
                match, self.tournament
            )
        return self._audiences[match.id]


def _invitation_time(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _db_invitation_to_invitation(row: DbMatchInvitation) -> MatchInvitation:
    return MatchInvitation(
        id=row.id, match_id=row.match_id, tournament_id=row.tournament_id,
        pairing_generation=row.pairing_generation, recipient_id=row.recipient_id,
        status=InvitationStatus(row.status),
        expected_readiness_revision=row.expected_readiness_revision,
        attempts=row.attempts, dispatch_token=row.dispatch_token,
        next_attempt_at=row.next_attempt_at, lease_until=row.lease_until,
        accepted_at=row.accepted_at, last_error=row.last_error,
    )


def get_match_invitation(
    invitation_id: MatchInvitationID,
) -> MatchInvitation | None:
    row = db.session.get(DbMatchInvitation, invitation_id, populate_existing=True)
    return _db_invitation_to_invitation(row) if row is not None else None


def _lock_invitation_match(
    match_id: TournamentMatchID,
) -> DbTournamentMatch | None:
    tournament_id = db.session.scalar(
        select(DbTournamentMatch.tournament_id).where(DbTournamentMatch.id == match_id)
    )
    if tournament_id is None:
        return None
    lock_tournament_for_update(tournament_id)
    lock_matches_for_update([match_id])
    return db.session.get(DbTournamentMatch, match_id, populate_existing=True)


def _lock_invitation(
    invitation_id: MatchInvitationID,
) -> tuple[DbMatchInvitation | None, DbTournamentMatch | None]:
    subject = db.session.execute(
        select(DbMatchInvitation.tournament_id, DbMatchInvitation.match_id)
        .where(DbMatchInvitation.id == invitation_id)
    ).first()
    if subject is None:
        return None, None
    # Retained sending history can outlive either live row. Lock any surviving
    # tournament before match/work, and never require a live FK for its outcome.
    lock_tournament_for_update(subject.tournament_id)
    lock_matches_for_update([subject.match_id])
    match = db.session.get(
        DbTournamentMatch, subject.match_id, populate_existing=True,
    )
    row = db.session.scalar(
        select(DbMatchInvitation).where(DbMatchInvitation.id == invitation_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    return row, match


def _current_invitation_audience(
    match: DbTournamentMatch,
    tournament: DbTournament | None,
) -> set[UserID]:
    """Scoped, fresh scalar roster queries; each recipient belongs to one side."""
    pair = (
        db.session.get(DbMatchPairing, match.pairing_id, populate_existing=True)
        if match.pairing_id else None
    )
    if tournament is None or pair is None or (
        pair.match_id != match.id
        or pair.tournament_id != match.tournament_id
        or pair.generation != match.pairing_generation
        or pair.ended_at is not None
    ):
        return set()
    game_format = tournament.game_format if match.phase == 1 else (
        tournament.playoff_game_format if match.phase == 2 else None
    )
    if game_format != GameFormat.ONE_V_ONE.name:
        return set()
    sides = [ContestantIdentity(kind=pair.side_a_kind, id=pair.side_a_id),
             ContestantIdentity(kind=pair.side_b_kind, id=pair.side_b_id)]
    assignments = db.session.execute(
        select(
            DbTournamentMatchToContestant.participant_id,
            DbTournamentMatchToContestant.team_id,
        )
        .where(DbTournamentMatchToContestant.tournament_match_id == match.id)
    ).all()
    identities = [
        ContestantIdentity(kind='participant', id=p) if p is not None
        else ContestantIdentity(kind='team', id=t)
        for p, t in assignments if p is not None or t is not None
    ]
    if len(identities) != 2 or set(identities) != set(sides):
        return set()
    audiences = []
    for side in sides:
        if side.kind == 'participant':
            audience = set(db.session.scalars(
                select(DbTournamentParticipant.user_id).where(
                    DbTournamentParticipant.id == side.id,
                    DbTournamentParticipant.tournament_id == match.tournament_id,
                    DbTournamentParticipant.removed_at.is_(None),
                )
            ))
        else:
            audience = set(db.session.scalars(
                select(DbTournamentParticipant.user_id)
                .join(
                    DbTournamentTeam,
                    DbTournamentParticipant.team_id == DbTournamentTeam.id,
                )
                .where(
                    DbTournamentTeam.id == side.id,
                    DbTournamentTeam.tournament_id == match.tournament_id,
                    DbTournamentTeam.removed_at.is_(None),
                    DbTournamentParticipant.tournament_id == match.tournament_id,
                    DbTournamentParticipant.removed_at.is_(None),
                )
            ))
        if not audience:
            return set()
        audiences.append(audience)
    return audiences[0] ^ audiences[1]


def _invitation_ineligibility(
    row: DbMatchInvitation, match: DbTournamentMatch | None,
    audience: set[UserID] | None = None,
    scope: _InvitationScope | None = None,
) -> str | None:
    if row.last_error in _INVITATION_IRREVERSIBLE_REASONS:
        return row.last_error
    if (
        match is None
        or match.tournament_id != row.tournament_id
        or match.pairing_generation != row.pairing_generation
    ):
        return 'pairing_retired'
    if scope is None:
        scope = _InvitationScope(row.tournament_id)
    tournament = scope.tournament
    if tournament is None or tournament.tournament_status in {'COMPLETED', 'CANCELLED'}:
        return 'tournament_terminal'
    if tournament.tournament_status != TournamentStatus.ONGOING.name:
        return 'tournament_paused'
    if match.confirmed_by is not None:
        return 'match_confirmed'
    if match.invitation_hold_a or match.invitation_hold_b:
        return 'readiness_hold'
    if audience is None:
        audience = scope.audience(match)
    if row.recipient_id not in audience:
        return 'recipient_not_current'
    if row.expected_readiness_revision != match.readiness_revision:
        return 'readiness_revision_changed'
    return None


def _suppress_invitation(row: DbMatchInvitation, reason: str) -> None:
    # Retirement is a fact about this work item, not the current live lifecycle.
    # A later reopen/hold/pause/reset cannot replace it with a resumable reason.
    if row.last_error in _INVITATION_IRREVERSIBLE_REASONS:
        return
    _release_attempt(row)
    row.status = InvitationStatus.SUPPRESSED.value
    row.dispatch_token = row.lease_until = row.next_attempt_at = None
    row.last_error = reason[:500]


def _release_attempt(row: DbMatchInvitation) -> None:
    """Give back the attempt of a token retired before SENDING (once, as the
    token dies with the status change that follows).
    """
    if row.status in {'dispatching', 'queued'} and row.attempts > 0:
        row.attempts -= 1


def _invitation_spent(row: DbMatchInvitation) -> bool:
    """Resumable work that has no attempt left and can never be retried."""
    return (
        row.attempts >= INVITATION_MAX_ATTEMPTS
        and row.last_error not in _INVITATION_IRREVERSIBLE_REASONS
        and (
            row.status == 'pending'
            or (
                row.status == 'suppressed'
                and row.last_error in _INVITATION_RESUMABLE_REASONS
            )
        )
    )


def _exhaust_invitation(row: DbMatchInvitation) -> None:
    row.status = InvitationStatus.FAILED.value
    row.dispatch_token = row.lease_until = row.next_attempt_at = None
    row.last_error = 'attempts_exhausted'


def ensure_invitation_intents_flush(
    match_id: TournamentMatchID,
    recipient_ids: Collection[UserID],
    *,
    occurred_at: datetime,
    historical_unknown: bool = False,
) -> list[MatchInvitationID]:
    """Reconcile unsent facts only; caller supplies audience, owns commit/effects."""
    now = _invitation_time(occurred_at)
    match = _lock_invitation_match(match_id)
    if match is None:
        return []
    scope = _InvitationScope(match.tournament_id)
    audience = scope.audience(match)
    rows = db.session.scalars(
        select(DbMatchInvitation).where(DbMatchInvitation.match_id == match_id)
        .order_by(DbMatchInvitation.id).with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    current = {
        r.recipient_id: r for r in rows
        if r.pairing_generation == match.pairing_generation
    }
    for recipient_id in sorted(set(recipient_ids) & audience, key=str):
        if recipient_id not in current:
            row = DbMatchInvitation(
                MatchInvitationID(uuid7()), match_id, match.tournament_id,
                match.pairing_generation, recipient_id,
                (InvitationStatus.DELIVERY_UNKNOWN if historical_unknown
                 else InvitationStatus.PENDING).value,
                match.readiness_revision,
            )
            db.session.add(row)
            rows.append(row)
            current[recipient_id] = row
    eligible = []
    for row in rows:
        if (
            row.status in _INVITATION_FINAL_FACTS
            or row.last_error in _INVITATION_IRREVERSIBLE_REASONS
        ):
            continue
        if _invitation_spent(row):
            _exhaust_invitation(row)
            continue
        revision_changed = row.expected_readiness_revision != match.readiness_revision
        reason = _invitation_ineligibility(row, match, audience, scope)
        if row.pairing_generation == match.pairing_generation:
            row.expected_readiness_revision = match.readiness_revision
        if reason and reason != 'readiness_revision_changed':
            # A temporary hold/pause/audience change must not erase a permanent
            # failure or its retry delay. Selection/reservation still checks it.
            if row.status == 'failed' and reason in _INVITATION_RESUMABLE_REASONS:
                continue
            _suppress_invitation(row, reason)
            continue
        if revision_changed and row.status in {'dispatching', 'queued'}:
            _suppress_invitation(row, 'readiness_revision_changed')
        if row.status == 'suppressed' and row.last_error in _INVITATION_RESUMABLE_REASONS:
            row.status = InvitationStatus.PENDING.value
            row.last_error = None
        if _invitation_retryable(row, now):
            eligible.append(row.id)
    db.session.flush()
    return sorted(eligible, key=str)


def _invitation_retryable(row: DbMatchInvitation, now: datetime) -> bool:
    return (
        row.last_error not in _INVITATION_IRREVERSIBLE_REASONS
        and row.status in {'pending', 'failed'}
        and row.attempts < INVITATION_MAX_ATTEMPTS
        and (row.status == 'pending' or row.next_attempt_at is not None)
        and (row.next_attempt_at is None or row.next_attempt_at <= now)
    )


def _invitation_retryable_clause(now: datetime):
    """SQL twin of `_invitation_retryable`; keep the two in step."""
    return and_(
        or_(
            DbMatchInvitation.last_error.is_(None),
            DbMatchInvitation.last_error.not_in(
                _INVITATION_IRREVERSIBLE_REASONS
            ),
        ),
        DbMatchInvitation.status.in_(['pending', 'failed']),
        DbMatchInvitation.attempts < INVITATION_MAX_ATTEMPTS,
        or_(
            DbMatchInvitation.status == 'pending',
            DbMatchInvitation.next_attempt_at.is_not(None),
        ),
        or_(
            DbMatchInvitation.next_attempt_at.is_(None),
            DbMatchInvitation.next_attempt_at <= now,
        ),
    )


def _invitation_spent_clause():
    """SQL twin of `_invitation_spent`."""
    return and_(
        DbMatchInvitation.attempts >= INVITATION_MAX_ATTEMPTS,
        or_(
            DbMatchInvitation.last_error.is_(None),
            DbMatchInvitation.last_error.not_in(
                _INVITATION_IRREVERSIBLE_REASONS
            ),
        ),
        or_(
            DbMatchInvitation.status == 'pending',
            and_(
                DbMatchInvitation.status == 'suppressed',
                DbMatchInvitation.last_error.in_(_INVITATION_RESUMABLE_REASONS),
            ),
        ),
    )


def _lock_invitation_work(
    invitation_id: MatchInvitationID,
) -> DbMatchInvitation | None:
    """Fresh work lock after the caller has locked tournament/ordered matches."""
    return db.session.scalar(
        select(DbMatchInvitation).where(DbMatchInvitation.id == invitation_id)
        .with_for_update().execution_options(populate_existing=True)
    )


def claim_invitation_dispatch_flush(
    invitation_id: MatchInvitationID,
    *,
    expected_token: UUID | None,
    now: datetime,
) -> Result[MatchInvitation, str]:
    now = _invitation_time(now)
    row, match = _lock_invitation(invitation_id)
    if row is None:
        return Err('invitation_not_found')
    if row.dispatch_token != expected_token or not _invitation_retryable(row, now):
        return Err('invitation_conflict')
    reason = _invitation_ineligibility(row, match)
    if reason:
        return Err(reason)
    row.status = InvitationStatus.DISPATCHING.value
    row.dispatch_token = uuid7()
    row.attempts += 1
    row.lease_until = now + timedelta(seconds=INVITATION_LEASE_SECONDS)
    row.next_attempt_at = row.last_error = None
    db.session.flush()
    return Ok(_db_invitation_to_invitation(row))


def record_invitation_outcome_flush(
    invitation_id: MatchInvitationID,
    dispatch_token: UUID,
    *,
    status: InvitationStatus,
    now: datetime,
    error: str | None = None,
    retryable: bool = False,
) -> Result[None, str]:
    """Token/stage CAS. Sending outcomes may affect only their retained record."""
    now = _invitation_time(now)
    row, match = _lock_invitation(invitation_id)
    if row is None:
        return Err('invitation_not_found')
    if not isinstance(status, InvitationStatus):
        return Err('invalid_invitation_status')
    transitions = {
        'dispatching': {
            InvitationStatus.QUEUED, InvitationStatus.SENDING,
            InvitationStatus.FAILED, InvitationStatus.SUPPRESSED,
        },
        'queued': {
            InvitationStatus.SENDING, InvitationStatus.FAILED,
            InvitationStatus.SUPPRESSED,
        },
        'sending': {
            InvitationStatus.ACCEPTED, InvitationStatus.FAILED,
            InvitationStatus.SUPPRESSED, InvitationStatus.DELIVERY_UNKNOWN,
        },
    }
    if row.dispatch_token != dispatch_token or status not in transitions.get(row.status, set()):
        return Err('invitation_conflict')
    if row.status != 'sending':
        # The matching token proves ownership; recovery retires an expired
        # lease by clearing it, so a late job may still move on.
        reason = _invitation_ineligibility(row, match)
        if reason:
            return Err(reason)
    # Once sending lease expired, even a late claimed acceptance is ambiguous.
    if row.status == 'sending' and row.lease_until <= now:
        status = InvitationStatus.DELIVERY_UNKNOWN
        error = 'sending_lease_expired'
    row.status = status.value
    row.last_error = error[:500] if error else None
    row.accepted_at = now if status == InvitationStatus.ACCEPTED else None
    row.next_attempt_at = None
    if status in {InvitationStatus.QUEUED, InvitationStatus.SENDING}:
        row.lease_until = now + timedelta(seconds=INVITATION_LEASE_SECONDS)
    else:
        row.lease_until = None
        if (
            status == InvitationStatus.FAILED
            and retryable and row.attempts < INVITATION_MAX_ATTEMPTS
        ):
            row.next_attempt_at = now + timedelta(seconds=30 if row.attempts == 1 else 120)
    db.session.flush()
    return Ok(None)


def _lock_invitation_matches(
    match_ids: Collection[TournamentMatchID],
) -> dict[TournamentMatchID, DbTournamentMatch]:
    """Lock and refresh those matches in ID order, with one statement."""
    if not match_ids:
        return {}
    rows = db.session.scalars(
        select(DbTournamentMatch)
        .where(DbTournamentMatch.id.in_(list(match_ids)))
        .order_by(DbTournamentMatch.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {row.id: row for row in rows}


def _exhaust_spent_invitations_flush(
    tournament_id: TournamentID,
    limit: int,
) -> None:
    """Finalise resumable work without an attempt left; caller holds the
    tournament lock, matches and rows are locked here in that order.
    """
    spent = db.session.execute(
        select(DbMatchInvitation.id, DbMatchInvitation.match_id)
        .where(
            DbMatchInvitation.tournament_id == tournament_id,
            _invitation_spent_clause(),
        )
        .order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
        .limit(limit)
    ).all()
    if not spent:
        return
    lock_matches_for_update(sorted({r.match_id for r in spent}, key=str))
    rows = db.session.scalars(
        select(DbMatchInvitation)
        .where(DbMatchInvitation.id.in_([r.id for r in spent]))
        .order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for row in rows:
        if _invitation_spent(row):
            _exhaust_invitation(row)
    db.session.flush()


def select_invitation_retry_ids_flush(
    tournament_id: TournamentID,
    *,
    now: datetime,
    limit: int = INVITATION_RECOVERY_LIMIT,
) -> list[MatchInvitationID]:
    """Select the pending or failed invitations that are due for dispatch.

    Only due rows are read, page by page; spent work is finalised on the way.
    """
    now = _invitation_time(now)
    batch = max(1, min(limit, INVITATION_RECOVERY_LIMIT))
    lock_tournament_for_update(tournament_id)
    _exhaust_spent_invitations_flush(tournament_id, batch)
    scope = _InvitationScope(tournament_id)
    tournament = scope.tournament
    if (
        tournament is None
        or tournament.tournament_status != TournamentStatus.ONGOING.name
    ):
        return []
    selected: list[MatchInvitationID] = []
    after = None
    while len(selected) < batch:
        stmt = (
            select(DbMatchInvitation.id, DbMatchInvitation.match_id)
            .where(
                DbMatchInvitation.tournament_id == tournament_id,
                _invitation_retryable_clause(now),
            )
            .order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
            .limit(batch)
        )
        if after is not None:
            stmt = stmt.where(
                tuple_(DbMatchInvitation.match_id, DbMatchInvitation.id) > after
            )
        page = db.session.execute(stmt).all()
        if not page:
            break
        after = (page[-1].match_id, page[-1].id)
        matches = _lock_invitation_matches({r.match_id for r in page})
        rows = db.session.scalars(
            select(DbMatchInvitation)
            .where(DbMatchInvitation.id.in_([r.id for r in page]))
            .order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        for row in rows:
            match = matches.get(row.match_id)
            if not _invitation_retryable(row, now):
                continue
            if _invitation_ineligibility(row, match, scope=scope):
                continue
            selected.append(row.id)
            if len(selected) >= batch:
                break
        if len(page) < batch:
            break
    db.session.flush()
    return selected


def recover_expired_invitations_flush(
    tournament_id: TournamentID,
    *,
    now: datetime,
    limit: int = INVITATION_RECOVERY_LIMIT,
) -> list[MatchInvitationID]:
    now = _invitation_time(now)
    lock_tournament_for_update(tournament_id)
    rows = db.session.scalars(
        select(DbMatchInvitation).where(
            DbMatchInvitation.tournament_id == tournament_id,
            DbMatchInvitation.status.in_(['dispatching', 'queued', 'sending']),
            DbMatchInvitation.lease_until <= now,
        ).order_by(DbMatchInvitation.match_id, DbMatchInvitation.id)
        .limit(max(1, min(limit, INVITATION_RECOVERY_LIMIT)))
        .execution_options(populate_existing=True)
    ).all()
    matches = _lock_invitation_matches({r.match_id for r in rows})
    scope = _InvitationScope(tournament_id)
    eligible = []
    for candidate in rows:
        row = _lock_invitation_work(candidate.id)
        if (
            row is None
            or row.status not in {'dispatching', 'queued', 'sending'}
            or row.lease_until > now
        ):
            continue
        if row.status == 'sending':
            row.status = InvitationStatus.DELIVERY_UNKNOWN.value
            row.last_error = 'sending_lease_expired'
            row.lease_until = row.next_attempt_at = None
            continue
        reason = _invitation_ineligibility(
            row, matches.get(row.match_id), scope=scope
        )
        if reason:
            _suppress_invitation(row, reason)
        else:
            # A dead claimer (`dispatching`) keeps its attempt spent so that
            # it exhausts; only a slow worker (`queued`) gets it back.
            if row.status == 'queued':
                _release_attempt(row)
            row.status = InvitationStatus.PENDING.value
            row.dispatch_token = row.lease_until = row.next_attempt_at = None
            row.last_error = 'pre_send_lease_expired'
            if _invitation_spent(row):
                _exhaust_invitation(row)
            elif _invitation_retryable(row, now):
                eligible.append(row.id)
    db.session.flush()
    return eligible


def _lock_pairing_subjects(match_ids: Collection[TournamentMatchID]) -> None:
    """Backstop locks before contestant writes, never match-before-tournament."""
    subjects = get_matches_by_ids(list(match_ids))
    for tournament_id in sorted({m.tournament_id for m in subjects}, key=str):
        lock_tournament_for_update(tournament_id)
    lock_matches_for_update([m.id for m in subjects])


def _retire_match_pairing_flush(match_id: TournamentMatchID) -> None:
    """Deletion backstop: retain history, clear capabilities and unsent work.

    Facts only, never audit or commit. Engine/roster/lifecycle owners must audit
    within their transaction. Original occupancy is deliberately untouched.
    """
    subject = find_match(match_id)
    if subject is None:
        return
    lock_tournament_for_update(subject.tournament_id)
    get_match_for_update(match_id)
    row = db.session.get(DbTournamentMatch, match_id)
    if row.pairing_id is not None:
        pair = db.session.get(DbMatchPairing, row.pairing_id)
        if pair is not None and pair.ended_at is None:
            now = datetime.now(UTC).replace(tzinfo=None)
            pair.ended_at = max(now, pair.started_at) if pair.started_at else now
        row.pairing_id = None
        row.pairing_generation += 1
        row.readiness_revision += 1
    elif row.ready_at_a or row.ready_at_b or row.invitation_hold_a or row.invitation_hold_b:
        row.readiness_revision += 1
    _clear_match_readiness(row)
    suppress_match_invitations_flush(match_id, reason='pairing_retired')
    db.session.flush()


def set_side_ready_flush(
    match_id: TournamentMatchID,
    side: MatchSide,
    ready_at: datetime,
    ready_by: UserID,
    *,
    changed_at: datetime | None = None,
) -> None:
    """Record a per-side readiness claim (flush only)."""
    if side not in (MatchSide.A, MatchSide.B):
        raise ValueError('invalid_match_side')
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    if side == MatchSide.A:
        _touch_match_if(
            match_id,
            changed_at,
            or_(
                DbTournamentMatch.ready_at_a.is_distinct_from(ready_at),
                DbTournamentMatch.ready_by_a.is_distinct_from(ready_by),
            ),
        )
        db_match.ready_at_a = ready_at
        db_match.ready_by_a = ready_by
    else:
        _touch_match_if(
            match_id,
            changed_at,
            or_(
                DbTournamentMatch.ready_at_b.is_distinct_from(ready_at),
                DbTournamentMatch.ready_by_b.is_distinct_from(ready_by),
            ),
        )
        db_match.ready_at_b = ready_at
        db_match.ready_by_b = ready_by
    db.session.flush()


def clear_side_ready_flush(
    match_id: TournamentMatchID,
    side: MatchSide,
    *,
    changed_at: datetime | None = None,
) -> None:
    """Remove a per-side readiness claim (flush only)."""
    if side not in (MatchSide.A, MatchSide.B):
        raise ValueError('invalid_match_side')
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    if side == MatchSide.A:
        _touch_match_if(
            match_id,
            changed_at,
            or_(
                DbTournamentMatch.ready_at_a.is_not(None),
                DbTournamentMatch.ready_by_a.is_not(None),
            ),
        )
        db_match.ready_at_a = None
        db_match.ready_by_a = None
    else:
        _touch_match_if(
            match_id,
            changed_at,
            or_(
                DbTournamentMatch.ready_at_b.is_not(None),
                DbTournamentMatch.ready_by_b.is_not(None),
            ),
        )
        db_match.ready_at_b = None
        db_match.ready_by_b = None
    db.session.flush()


def set_both_ready_notified_flush(
    match_id: TournamentMatchID,
    notified_at: datetime | None,
) -> None:
    """Set or clear the both-ready notification marker (flush only).

    Clearing the marker re-enables ready emails after a revocation.
    """
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    db_match.both_ready_notified_at = notified_at
    db.session.flush()


def set_occupied_since_if_unset_flush(
    match_id: TournamentMatchID,
    occupied_since: datetime,
) -> bool:
    """Set ``occupied_since`` unless already set; flush only.

    The column is naive UTC, so an aware value is converted first.
    Return ``True`` if the value was set by this call.
    """
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')
    if db_match.occupied_since is not None:
        return False
    db_match.occupied_since = _naive_utc(occupied_since)
    db.session.flush()
    return True


def set_ffa_lobby_occupied_since_if_unset_flush(
    match_id: TournamentMatchID,
    occupied_since: datetime,
) -> bool:
    """Set `occupied_since` of a completed free-for-all lobby; flush only.

    This is the one way a free-for-all lobby becomes occupied. The
    caller has checked that the roster is complete and passes the
    operation time. An occupancy that is already set is never reset.

    Return `True` if the value was set by this call. Raise `ValueError`
    for an unknown match or one whose phase is not free-for-all.
    """
    if _phase_game_format_name(match_id) != GameFormat.FREE_FOR_ALL.name:
        raise ValueError(f'Match "{match_id}" is not a free-for-all lobby')

    return set_occupied_since_if_unset_flush(match_id, occupied_since)


def get_both_ready_unnotified_match_ids(
    tournament_id: TournamentID,
) -> list[TournamentMatchID]:
    """Return IDs of unconfirmed matches with BOTH sides claimed ready
    whose ready email has not been sent yet.

    Matches whose readiness was revoked are excluded automatically:
    revocation clears one side's claim, so they are no longer both-ready.
    """
    match_ids = (
        db.session.execute(
            select(DbTournamentMatch.id).where(
                DbTournamentMatch.tournament_id == tournament_id,
                DbTournamentMatch.confirmed_by.is_(None),
                DbTournamentMatch.ready_at_a.is_not(None),
                DbTournamentMatch.ready_at_b.is_not(None),
                DbTournamentMatch.both_ready_notified_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    return list(match_ids)


def mark_matches_both_ready_notified(
    match_ids: list[TournamentMatchID],
    notified_at: datetime,
    *,
    commit: bool = True,
) -> int:
    """Set the both-ready notification marker on the given matches.

    Return the number of updated rows.
    """
    if not match_ids:
        return 0
    result = db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id.in_(match_ids))
        .values(both_ready_notified_at=notified_at)
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return result.rowcount


# -- match comment --


def find_match_comment(
    comment_id: TournamentMatchCommentID,
) -> TournamentMatchComment | None:
    """Return the match comment, or `None` if not found."""
    db_comment = db.session.get(DbTournamentMatchComment, comment_id)
    if db_comment is None:
        return None
    return _db_comment_to_comment(db_comment)


def create_match_comment(
    comment: TournamentMatchComment,
) -> None:
    """Persist a match comment."""
    db_comment = DbTournamentMatchComment(
        comment.id,
        comment.tournament_match_id,
        comment.created_by,
        comment.comment,
        comment.created_at,
        comment.context.value if comment.context else None,
    )

    db.session.add(db_comment)
    db.session.commit()


def create_match_comment_flush(
    comment: TournamentMatchComment,
) -> None:
    """Persist a match comment (flush only — caller owns commit)."""
    db_comment = DbTournamentMatchComment(
        comment.id,
        comment.tournament_match_id,
        comment.created_by,
        comment.comment,
        comment.created_at,
        comment.context.value if comment.context else None,
    )

    db.session.add(db_comment)
    db.session.flush()


def update_match_comment(
    comment_id: TournamentMatchCommentID,
    comment: str,
) -> None:
    """Update a match comment's text."""
    db_comment = db.session.get(DbTournamentMatchComment, comment_id)
    if db_comment is None:
        raise ValueError(f'Unknown comment ID "{comment_id}"')

    db_comment.comment = comment
    db.session.commit()


def delete_match_comment(
    comment_id: TournamentMatchCommentID,
) -> None:
    """Delete a match comment."""
    db.session.execute(
        delete(DbTournamentMatchComment).filter_by(id=comment_id)
    )
    db.session.commit()


def delete_comments_for_match(match_id: TournamentMatchID) -> None:
    """Delete all comments for a match."""
    db.session.execute(
        delete(DbTournamentMatchComment).filter_by(tournament_match_id=match_id)
    )
    db.session.commit()


def delete_comments_for_match_flush(
    match_id: TournamentMatchID,
) -> None:
    """Delete all comments for a match (flush only — caller owns commit)."""
    db.session.execute(
        delete(DbTournamentMatchComment).filter_by(
            tournament_match_id=match_id
        )
    )
    db.session.flush()


def delete_comments_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all comments for all matches in a tournament."""
    db.session.execute(
        delete(DbTournamentMatchComment).where(
            DbTournamentMatchComment.tournament_match_id.in_(
                select(DbTournamentMatch.id).filter_by(
                    tournament_id=tournament_id
                )
            )
        )
    )
    if commit:
        db.session.commit()


def get_comments_for_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchComment]:
    """Return all comments for that match."""
    db_comments = (
        db.session.execute(
            select(DbTournamentMatchComment).filter_by(
                tournament_match_id=match_id
            )
        )
        .scalars()
        .all()
    )
    return [_db_comment_to_comment(c) for c in db_comments]


def _db_comment_to_comment(
    db_comment: DbTournamentMatchComment,
) -> TournamentMatchComment:
    return TournamentMatchComment(
        id=db_comment.id,
        tournament_match_id=db_comment.tournament_match_id,
        created_by=db_comment.created_by,
        comment=db_comment.comment,
        created_at=db_comment.created_at,
        context=(
            MatchCommentContext(db_comment.context)
            if db_comment.context
            else None
        ),
    )


# -- match contestant --


def create_match_contestant(
    contestant: TournamentMatchToContestant,
    *,
    changed_at: datetime | None = None,
) -> None:
    """Persist a match contestant."""
    db_contestant = DbTournamentMatchToContestant(
        contestant.id,
        contestant.tournament_match_id,
        contestant.created_at,
        team_id=contestant.team_id,
        participant_id=contestant.participant_id,
        score=contestant.score,
        placement=contestant.placement,
        points=contestant.points,
        contestant_status=(
            contestant.contestant_status.name
            if contestant.contestant_status
            else None
        ),
    )

    db.session.add(db_contestant)
    db.session.flush()
    operation_time = _resolve_changed_at(changed_at)
    _touch_changed_matches_flush(
        [contestant.tournament_match_id], operation_time
    )

    # Occupancy of a 1v1 match starts once both sides are fixed.
    _mark_occupied_if_fully_occupied(
        db_contestant.tournament_match_id, operation_time
    )


def _mark_occupied_if_fully_occupied(
    match_id: TournamentMatchID,
    occupied_since: datetime,
) -> None:
    """Set `occupied_since` on a 1v1 match that just became fully occupied.

    That is exactly 2 real contestants with the marker still unset.
    A free-for-all lobby is never marked here: its roster is complete
    only when the generator says so, through
    `set_ffa_lobby_occupied_since_if_unset_flush`.
    """
    count = (
        db.session.execute(
            select(func.count())
            .select_from(DbTournamentMatchToContestant)
            .where(
                DbTournamentMatchToContestant.tournament_match_id == match_id,
                (DbTournamentMatchToContestant.participant_id.is_not(None))
                | (DbTournamentMatchToContestant.team_id.is_not(None)),
            )
        ).scalar_one()
    )
    if count != 2:
        return

    if _phase_game_format_name(match_id) == GameFormat.ONE_V_ONE.name:
        set_occupied_since_if_unset_flush(match_id, occupied_since)


def _phase_game_format_name(match_id: TournamentMatchID) -> str | None:
    """Return the name of the game format that the match's phase runs.

    The database answers, so a cached row cannot.
    """
    row = db.session.execute(
        select(
            DbTournamentMatch.phase,
            DbTournament.game_format,
            DbTournament.playoff_game_format,
        )
        .select_from(DbTournamentMatch)
        .join(DbTournament, DbTournament.id == DbTournamentMatch.tournament_id)
        .where(DbTournamentMatch.id == match_id)
    ).one_or_none()
    if row is None:
        raise ValueError(f'Unknown match ID "{match_id}"')

    if row.phase == 1:
        return row.game_format
    if row.phase == 2:
        return row.playoff_game_format
    return None


def update_contestant_score(
    contestant_id: TournamentMatchToContestantID,
    score: int,
    *,
    changed_at: datetime | None = None,
    commit: bool = True,
) -> None:
    """Update a contestant's score.

    Commit unless `commit` is false: the caller then owns the transaction.
    """
    db_contestant = db.session.get(DbTournamentMatchToContestant, contestant_id)
    if db_contestant is None:
        raise ValueError(f'Unknown contestant ID "{contestant_id}"')

    changed = db_contestant.score != score
    db_contestant.score = score
    if changed:
        db.session.flush()
        _touch_changed_matches_flush(
            [db_contestant.tournament_match_id], changed_at
        )
    if commit:
        db.session.commit()


def update_contestant_scores(
    scores: dict[TournamentMatchToContestantID, int],
    *,
    changed_at: datetime | None = None,
) -> None:
    """Update multiple contestant scores in a single flush.

    Caller is responsible for committing the session.
    """
    changed_match_ids = []
    for contestant_id, score in scores.items():
        db_contestant = db.session.get(
            DbTournamentMatchToContestant, contestant_id
        )
        if db_contestant is None:
            raise ValueError(f'Unknown contestant ID "{contestant_id}"')
        if db_contestant.score != score:
            changed_match_ids.append(db_contestant.tournament_match_id)
        db_contestant.score = score
    db.session.flush()
    _touch_changed_matches_flush(changed_match_ids, changed_at)


def clear_contestant_scores(
    match_id: TournamentMatchID, *, changed_at: datetime | None = None
) -> None:
    """Clear all contestant scores for a match.

    Caller is responsible for committing the session.
    """
    contestants = (
        db.session.query(DbTournamentMatchToContestant)
        .filter_by(tournament_match_id=match_id)
        .all()
    )
    changed = any(contestant.score is not None for contestant in contestants)
    for contestant in contestants:
        contestant.score = None
    db.session.flush()
    if changed:
        _touch_changed_matches_flush([match_id], changed_at)


def update_contestant_placement_and_points(
    updates: dict[TournamentMatchToContestantID, tuple[int, int]],
    *,
    changed_at: datetime | None = None,
) -> None:
    """Update placement and points for multiple contestants in a single flush.

    *updates* maps contestant ID to ``(placement, points)``.
    Caller is responsible for committing the session.
    """
    changed_match_ids = []
    for contestant_id, (placement, points) in updates.items():
        db_contestant = db.session.get(
            DbTournamentMatchToContestant, contestant_id
        )
        if db_contestant is None:
            raise ValueError(f'Unknown contestant ID "{contestant_id}"')
        if (db_contestant.placement, db_contestant.points) != (
            placement,
            points,
        ):
            changed_match_ids.append(db_contestant.tournament_match_id)
        db_contestant.placement = placement
        db_contestant.points = points
    db.session.flush()
    _touch_changed_matches_flush(changed_match_ids, changed_at)


def get_contestants_for_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchToContestant]:
    """Return all contestants for that match."""
    db_contestants = (
        db.session.execute(
            select(DbTournamentMatchToContestant)
            .filter_by(tournament_match_id=match_id)
            .order_by(
                DbTournamentMatchToContestant.created_at,
                DbTournamentMatchToContestant.id,
            )
        )
        .scalars()
        .all()
    )
    return [_db_contestant_to_contestant(c) for c in db_contestants]


def get_contestants_for_tournament(
    tournament_id: TournamentID,
) -> dict[TournamentMatchID, list[TournamentMatchToContestant]]:
    """Return all contestants for a tournament, grouped by match ID.

    Performs a single query joining contestants with matches instead of
    one query per match (eliminates N+1).
    """
    db_contestants = (
        db.session.execute(
            select(DbTournamentMatchToContestant)
            .join(
                DbTournamentMatch,
                DbTournamentMatchToContestant.tournament_match_id
                == DbTournamentMatch.id,
            )
            .filter(DbTournamentMatch.tournament_id == tournament_id)
            .order_by(
                DbTournamentMatchToContestant.created_at,
                DbTournamentMatchToContestant.id,
            )
        )
        .scalars()
        .all()
    )
    result: dict[TournamentMatchID, list[TournamentMatchToContestant]] = {}
    for db_c in db_contestants:
        contestant = _db_contestant_to_contestant(db_c)
        result.setdefault(contestant.tournament_match_id, []).append(contestant)
    return result


def get_contestants_for_matches(
    match_ids: list[TournamentMatchID],
) -> dict[TournamentMatchID, list[TournamentMatchToContestant]]:
    """Return the contestants of those matches, grouped by match ID."""
    if not match_ids:
        return {}

    db_contestants = (
        db.session.execute(
            select(DbTournamentMatchToContestant)
            .filter(
                DbTournamentMatchToContestant.tournament_match_id.in_(
                    match_ids
                )
            )
            .order_by(
                DbTournamentMatchToContestant.created_at,
                DbTournamentMatchToContestant.id,
            )
        )
        .scalars()
        .all()
    )
    result: dict[TournamentMatchID, list[TournamentMatchToContestant]] = {}
    for db_c in db_contestants:
        contestant = _db_contestant_to_contestant(db_c)
        result.setdefault(contestant.tournament_match_id, []).append(contestant)
    return result


def find_contestant_for_match(
    match_id: TournamentMatchID,
    participant_id: TournamentParticipantID | None = None,
    team_id: TournamentTeamID | None = None,
) -> TournamentMatchToContestant | None:
    """Return contestant by match and participant/team, or None."""
    query = select(DbTournamentMatchToContestant).filter_by(
        tournament_match_id=match_id
    )
    if participant_id is not None:
        query = query.filter_by(participant_id=participant_id)
    elif team_id is not None:
        query = query.filter_by(team_id=team_id)
    else:
        raise ValueError('Either participant_id or team_id must be provided.')

    db_contestant = db.session.execute(query).scalar_one_or_none()
    if db_contestant is None:
        return None
    return _db_contestant_to_contestant(db_contestant)


def _db_contestant_to_contestant(
    db_contestant: DbTournamentMatchToContestant,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=db_contestant.id,
        tournament_match_id=db_contestant.tournament_match_id,
        team_id=db_contestant.team_id,
        participant_id=db_contestant.participant_id,
        score=db_contestant.score,
        created_at=db_contestant.created_at,
        placement=db_contestant.placement,
        points=db_contestant.points,
        contestant_status=_safe_enum_lookup(
            ContestantStatus, db_contestant.contestant_status
        ),
    )


def delete_match_contestant(
    contestant_id: TournamentMatchToContestantID,
) -> None:
    """Delete a match contestant."""
    delete_match_contestant_flush(contestant_id)
    db.session.commit()


def delete_match_contestant_flush(
    contestant_id: TournamentMatchToContestantID,
    *,
    changed_at: datetime | None = None,
) -> None:
    """Delete a contestant; caller owns pairing cleanup and commit."""
    match_ids = db.session.scalars(
        select(DbTournamentMatchToContestant.tournament_match_id)
        .filter_by(id=contestant_id)
    ).all()
    _lock_pairing_subjects(match_ids)
    db.session.execute(
        delete(DbTournamentMatchToContestant).filter_by(id=contestant_id)
    )
    for match_id in match_ids:
        _retire_match_pairing_flush(match_id)
    db.session.flush()
    _touch_changed_matches_flush(match_ids, changed_at)


def find_contestant_entries_for_participant_in_tournament(
    tournament_id: TournamentID,
    participant_id: TournamentParticipantID,
) -> list[tuple[TournamentMatchToContestant, TournamentMatch]]:
    """Find all contestant entries for a participant across
    unconfirmed matches in a tournament."""
    rows = db.session.execute(
        select(DbTournamentMatchToContestant, DbTournamentMatch)
        .join(
            DbTournamentMatch,
            DbTournamentMatchToContestant.tournament_match_id
            == DbTournamentMatch.id,
        )
        .where(
            DbTournamentMatch.tournament_id == tournament_id,
            DbTournamentMatchToContestant.participant_id == participant_id,
            DbTournamentMatch.confirmed_by.is_(None),
        )
    ).all()
    return [
        (
            _db_contestant_to_contestant(db_c),
            _db_match_to_match(db_m),
        )
        for db_c, db_m in rows
    ]


def find_contestant_entries_for_team_in_tournament(
    tournament_id: TournamentID,
    team_id: TournamentTeamID,
) -> list[tuple[TournamentMatchToContestant, TournamentMatch]]:
    """Find all contestant entries for a team across
    unconfirmed matches in a tournament."""
    rows = db.session.execute(
        select(DbTournamentMatchToContestant, DbTournamentMatch)
        .join(
            DbTournamentMatch,
            DbTournamentMatchToContestant.tournament_match_id
            == DbTournamentMatch.id,
        )
        .where(
            DbTournamentMatch.tournament_id == tournament_id,
            DbTournamentMatchToContestant.team_id == team_id,
            DbTournamentMatch.confirmed_by.is_(None),
        )
    ).all()
    return [
        (
            _db_contestant_to_contestant(db_c),
            _db_match_to_match(db_m),
        )
        for db_c, db_m in rows
    ]


def delete_contestant_from_match(
    match_id: TournamentMatchID,
    *,
    team_id: TournamentTeamID | None = None,
    participant_id: TournamentParticipantID | None = None,
    changed_at: datetime | None = None,
) -> None:
    """Delete a specific contestant from a match."""
    query = delete(DbTournamentMatchToContestant).filter_by(
        tournament_match_id=match_id
    )
    if team_id is not None:
        query = query.filter_by(team_id=team_id)
    elif participant_id is not None:
        query = query.filter_by(participant_id=participant_id)
    else:
        raise ValueError('Either team_id or participant_id required.')
    _lock_pairing_subjects([match_id])
    deleted = db.session.scalars(query.returning(DbTournamentMatchToContestant.id)).all()
    if deleted:
        _retire_match_pairing_flush(match_id)
    db.session.flush()
    if deleted:
        _touch_changed_matches_flush([match_id], changed_at)


def delete_contestants_for_match(match_id: TournamentMatchID) -> None:
    """Delete all contestants for a match."""
    delete_contestants_for_match_flush(match_id)
    db.session.commit()


def delete_contestants_for_match_flush(
    match_id: TournamentMatchID,
    *,
    changed_at: datetime | None = None,
) -> None:
    """Delete all contestants for a match (flush only)."""
    _retire_match_pairing_flush(match_id)
    deleted = list(
        db.session.execute(
            delete(DbTournamentMatchToContestant)
            .filter_by(tournament_match_id=match_id)
            .returning(DbTournamentMatchToContestant.id)
        ).scalars()
    )
    db.session.flush()
    if deleted:
        _touch_changed_matches_flush([match_id], changed_at)


def delete_contestants_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all contestants for all matches in a tournament."""
    delete_contestants_for_tournament_flush(tournament_id)
    if commit:
        db.session.commit()


def delete_contestants_for_tournament_flush(tournament_id: TournamentID) -> None:
    """Delete all contestants; caller owns pairing cleanup and commit."""
    lock_tournament_for_update(tournament_id)
    matches = get_matches_for_tournament_ordered_fresh(tournament_id)
    lock_matches_for_update([match.id for match in matches])
    for match in sorted(matches, key=lambda match: str(match.id)):
        _retire_match_pairing_flush(match.id)
    db.session.execute(
        delete(DbTournamentMatchToContestant).where(
            DbTournamentMatchToContestant.tournament_match_id.in_(
                select(DbTournamentMatch.id).filter_by(
                    tournament_id=tournament_id
                )
            )
        )
    )
    db.session.flush()


def remove_team_from_contestants(team_id: TournamentTeamID) -> None:
    """Delete all match contestants referencing this team."""
    remove_team_from_contestants_flush(team_id)
    db.session.commit()


def remove_team_from_contestants_flush(
    team_id: TournamentTeamID, *, changed_at: datetime | None = None
) -> None:
    """Delete team slots; caller owns pairing cleanup and commit."""
    team = get_team(team_id)
    lock_tournament_for_update(team.tournament_id)
    match_ids = list(db.session.scalars(
        select(DbTournamentMatchToContestant.tournament_match_id)
        .where(DbTournamentMatchToContestant.team_id == team_id)
        .distinct()
    ))
    lock_matches_for_update(match_ids)
    for match_id in sorted(match_ids, key=str):
        _retire_match_pairing_flush(match_id)
    db.session.execute(
        db.delete(DbTournamentMatchToContestant).filter_by(team_id=team_id)
    )
    db.session.flush()
    _touch_changed_matches_flush(match_ids, changed_at)


# -- score submission --


def create_score_submission(
    submission: ScoreSubmission,
) -> None:
    """Persist a score submission."""
    db_submission = DbScoreSubmission(
        submission.id,
        submission.tournament_id,
        submission.score,
        submission.submitted_at,
        participant_id=submission.participant_id,
        team_id=submission.team_id,
        submitted_by=submission.submitted_by,
        is_official=submission.is_official,
        note=submission.note,
    )
    db.session.add(db_submission)
    db.session.commit()


def get_official_submissions_for_tournament(
    tournament_id: TournamentID,
) -> list[ScoreSubmission]:
    """Return all official score submissions for a
    tournament.
    """
    db_subs = (
        db.session.execute(
            select(DbScoreSubmission).filter_by(
                tournament_id=tournament_id,
                is_official=True,
            )
        )
        .scalars()
        .all()
    )
    return [_db_score_submission_to_score_submission(s) for s in db_subs]


def delete_submissions_for_tournament(
    tournament_id: TournamentID, *, commit: bool = True
) -> None:
    """Delete all score submissions for a tournament."""
    db.session.execute(
        delete(DbScoreSubmission).filter_by(tournament_id=tournament_id)
    )
    if commit:
        db.session.commit()


def _db_score_submission_to_score_submission(
    db_sub: DbScoreSubmission,
) -> ScoreSubmission:
    """Convert a DB score submission to a domain model."""
    return ScoreSubmission(
        id=db_sub.id,
        tournament_id=db_sub.tournament_id,
        participant_id=db_sub.participant_id,
        team_id=db_sub.team_id,
        score=db_sub.score,
        submitted_at=db_sub.submitted_at,
        submitted_by=db_sub.submitted_by,
        is_official=db_sub.is_official,
        note=db_sub.note,
    )


def get_ready_unconfirmed_match_ids(
    tournament_id: TournamentID,
) -> list[TournamentMatchID]:
    """Return IDs of matches that are ready (>= 2 contestants)
    and not yet confirmed."""
    ready_match_ids_subq = (
        select(DbTournamentMatchToContestant.tournament_match_id)
        .group_by(DbTournamentMatchToContestant.tournament_match_id)
        .having(func.count() >= 2)
        .subquery()
    )
    match_ids = (
        db.session.execute(
            select(DbTournamentMatch.id)
            .where(
                DbTournamentMatch.tournament_id == tournament_id,
                DbTournamentMatch.confirmed_by.is_(None),
                DbTournamentMatch.id.in_(select(ready_match_ids_subq)),
            )
        )
        .scalars()
        .all()
    )
    return list(match_ids)


def get_log_entries_with_prefixes(
    tournament_id: TournamentID,
    prefixes: Sequence[str],
    limit: int,
    *,
    registration_statuses: Sequence[str] = (),
) -> list[TournamentLogEntry]:
    """Return matching audit events, newest first, at most `limit`."""
    if not prefixes:
        raise ValueError('prefixes must not be empty')
    predicates = [
        DbTournamentLogEntry.event_type.startswith(prefix, autoescape=True)
        for prefix in prefixes
    ]
    if registration_statuses:
        predicates.append(
            and_(
                DbTournamentLogEntry.event_type == 'tournament-status-changed',
                DbTournamentLogEntry.data['new_status'].astext.in_(
                    registration_statuses
                ),
            )
        )
    rows = db.session.scalars(
        select(DbTournamentLogEntry)
        .where(
            DbTournamentLogEntry.tournament_id == tournament_id,
            or_(*predicates),
        )
        .order_by(
            DbTournamentLogEntry.occurred_at.desc(),
            DbTournamentLogEntry.id.desc(),
        )
        .limit(limit)
    ).all()
    return [
        TournamentLogEntry(
            id=row.id,
            occurred_at=row.occurred_at,
            event_type=row.event_type,
            tournament_id=row.tournament_id,
            initiator_id=row.initiator_id,
            data=row.data.copy(),
        )
        for row in rows
    ]


def delete_log_entries_older_than(occurred_before: datetime) -> int:
    """Delete tournament log entries which occurred before the given date.

    Return the number of deleted log entries.
    """
    result = db.session.execute(
        delete(DbTournamentLogEntry).filter(
            DbTournamentLogEntry.occurred_at < occurred_before
        )
    )
    db.session.commit()

    num_deleted = result.rowcount
    return num_deleted


# -- operational timing and dashboard coordination --


def _naive_utc(value: datetime) -> datetime:
    """Return the moment as naive UTC, the plain `TIMESTAMP` convention.

    A naive value already is UTC. An aware value would otherwise be
    shifted by the session time zone on its way into a naive column.
    """
    if value.tzinfo is None:
        return value

    return value.astimezone(UTC).replace(tzinfo=None)


def _naive_utc_or_none(value: datetime | None) -> datetime | None:
    return _naive_utc(value) if value is not None else None


def get_operation_time() -> datetime:
    """Return the server wall time as naive UTC.

    Sample it once per operation, after the locks are taken, and share
    the value between all facts of that operation. `clock_timestamp()`
    moves while a statement waits for a lock, which a transaction
    start time would not.
    """
    return db.session.execute(
        select(func.timezone('UTC', func.clock_timestamp()))
    ).scalar_one()


def _initial_last_changed_at(
    match: TournamentMatch, changed_at: datetime | None
) -> datetime:
    value = (
        changed_at if match.last_changed_at is None else match.last_changed_at
    )
    return _naive_utc(value) if value is not None else get_operation_time()


def _resolve_changed_at(changed_at: datetime | None) -> datetime:
    """Return the operation's shared time, sampling the server if none."""
    return get_operation_time() if changed_at is None else changed_at


def _touch_matches(
    match_ids: Collection[TournamentMatchID],
    changed_at: datetime,
    *conditions: ColumnElement[bool],
) -> None:
    db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id.in_(list(match_ids)), *conditions)
        .values(
            last_changed_at=func.greatest(
                DbTournamentMatch.last_changed_at, _naive_utc(changed_at)
            )
        )
    )


def touch_matches_last_changed_flush(
    match_ids: Collection[TournamentMatchID], *, changed_at: datetime
) -> None:
    """Record a domain change of those matches (flush only).

    The stored time never moves backwards, and a match with unknown
    history (NULL) takes `changed_at`.
    """
    if not match_ids:
        return

    _touch_matches(match_ids, changed_at)
    db.session.flush()


def _touch_changed_matches_flush(
    match_ids: Iterable[TournamentMatchID], changed_at: datetime | None
) -> None:
    """Record a domain change of the matches a writer really changed.

    The server clock is sampled only if there is a change, and once for
    all of them.
    """
    ids = sorted(set(match_ids), key=str)
    if ids:
        touch_matches_last_changed_flush(
            ids, changed_at=_resolve_changed_at(changed_at)
        )


def _touch_match_if(
    match_id: TournamentMatchID,
    changed_at: datetime | None,
    differs: ColumnElement[bool],
) -> None:
    """Record a domain change of the match if `differs` holds for its row.

    The database compares, so the locked row decides, not a cached copy.
    Call it before the new value is assigned.
    """
    _touch_matches([match_id], _resolve_changed_at(changed_at), differs)


def _db_episode_to_episode(row: DbMatchDueEpisode) -> MatchDueEpisode:
    return MatchDueEpisode(
        id=row.id,
        tournament_id=row.tournament_id,
        match_id=row.match_id,
        pairing_key=row.pairing_key,
        opened_at=row.opened_at,
        opened_clock_us=row.opened_clock_us,
        closed_at=row.closed_at,
        closed_clock_us=row.closed_clock_us,
        ack_revision=row.ack_revision,
    )


def list_open_due_episodes(
    tournament_id: TournamentID,
) -> list[MatchDueEpisode]:
    """Return the open due episodes of a tournament, freshly loaded."""
    rows = db.session.scalars(
        select(DbMatchDueEpisode)
        .where(
            DbMatchDueEpisode.tournament_id == tournament_id,
            DbMatchDueEpisode.closed_at.is_(None),
        )
        .order_by(DbMatchDueEpisode.opened_at, DbMatchDueEpisode.id)
        .execution_options(populate_existing=True)
    ).all()
    return [_db_episode_to_episode(row) for row in rows]


def open_due_episode_flush(episode: MatchDueEpisode) -> None:
    """Persist a due episode (flush only).

    The database allows one open episode per match.
    """
    db.session.add(
        DbMatchDueEpisode(
            episode.id,
            episode.tournament_id,
            episode.match_id,
            episode.pairing_key,
            _naive_utc(episode.opened_at),
            episode.opened_clock_us,
            closed_at=_naive_utc_or_none(episode.closed_at),
            closed_clock_us=episode.closed_clock_us,
            ack_revision=episode.ack_revision,
        )
    )
    db.session.flush()


def close_due_episodes_flush(
    match_ids: Collection[TournamentMatchID],
    *,
    occurred_at: datetime,
    clock_us: int,
) -> None:
    """Close the open episodes of those matches (flush only)."""
    if not match_ids:
        return

    db.session.execute(
        update(DbMatchDueEpisode)
        .where(
            DbMatchDueEpisode.match_id.in_(list(match_ids)),
            DbMatchDueEpisode.closed_at.is_(None),
        )
        .values(closed_at=_naive_utc(occurred_at), closed_clock_us=clock_us)
    )
    db.session.flush()


def _tournament_clock_us_at(at: datetime) -> ColumnElement[int]:
    """Return the SQL twin of `clock_value_us` for a tournament row.

    `at` is naive UTC. A moment before `running_since` adds nothing.
    """
    since = DbTournament.operational_clock_running_since
    running_us = func.greatest(
        (extract('epoch', literal(at) - since) * 1_000_000).cast(BigInteger),
        0,
    )
    return DbTournament.operational_clock_elapsed_us + case(
        (since.is_(None), 0), else_=running_us
    )


def retire_dashboard_matches_flush(
    match_ids: Collection[TournamentMatchID], *, occurred_at: datetime
) -> None:
    """Close the episodes of matches that are going away (flush only).

    The closed episodes stay as history, closed at the tournament clock
    of `occurred_at`. The live pins are deleted, because they must not
    block the removal of their match. Call it before the matches are
    deleted.
    """
    if not match_ids:
        return

    ids = list(match_ids)
    at = _naive_utc(occurred_at)
    clock_us = (
        select(_tournament_clock_us_at(at))
        .where(DbTournament.id == DbMatchDueEpisode.tournament_id)
        .scalar_subquery()
    )
    db.session.execute(
        update(DbMatchDueEpisode)
        .where(
            DbMatchDueEpisode.match_id.in_(ids),
            DbMatchDueEpisode.closed_at.is_(None),
        )
        .values(
            closed_at=at,
            closed_clock_us=func.greatest(
                DbMatchDueEpisode.opened_clock_us, clock_us
            ),
        )
    )
    db.session.execute(
        delete(DbMatchDashboardAnnotation).where(
            DbMatchDashboardAnnotation.match_id.in_(ids)
        )
    )
    db.session.flush()


def _db_ack_to_ack(row: DbMatchEscalationAck) -> MatchEscalationAcknowledgement:
    return MatchEscalationAcknowledgement(
        id=row.id,
        episode_id=row.episode_id,
        tournament_id=row.tournament_id,
        match_id=row.match_id,
        revision=row.revision,
        occurred_at=row.occurred_at,
        clock_us=row.clock_us,
        actor_id=row.actor_id,
        comment=row.comment,
    )


def create_escalation_ack_flush(ack: MatchEscalationAcknowledgement) -> None:
    """Append an immutable acknowledgement (flush only).

    The database allows one acknowledgement per episode revision.
    """
    db.session.add(
        DbMatchEscalationAck(
            ack.id,
            ack.episode_id,
            ack.tournament_id,
            ack.match_id,
            ack.actor_id,
            ack.revision,
            _naive_utc(ack.occurred_at),
            ack.clock_us,
            comment=ack.comment,
        )
    )
    db.session.flush()


def find_open_due_episode(
    match_id: TournamentMatchID,
) -> MatchDueEpisode | None:
    """Return the freshly loaded open episode of a match, or `None`."""
    row = db.session.scalars(
        select(DbMatchDueEpisode)
        .where(
            DbMatchDueEpisode.match_id == match_id,
            DbMatchDueEpisode.closed_at.is_(None),
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    return _db_episode_to_episode(row) if row is not None else None


def find_latest_escalation_ack(
    episode_id: UUID,
) -> MatchEscalationAcknowledgement | None:
    """Return the freshly loaded latest acknowledgement of an episode."""
    row = db.session.scalars(
        select(DbMatchEscalationAck)
        .where(DbMatchEscalationAck.episode_id == episode_id)
        .order_by(DbMatchEscalationAck.revision.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    ).one_or_none()
    return _db_ack_to_ack(row) if row is not None else None


def advance_episode_ack_revision_flush(
    episode_id: UUID, *, expected_revision: int
) -> bool:
    """Raise the acknowledgement revision of an open episode by one.

    This happens only if the episode is still open at `expected_revision`.
    Return whether it did (flush only).
    """
    advanced = db.session.execute(
        update(DbMatchDueEpisode)
        .where(
            DbMatchDueEpisode.id == episode_id,
            DbMatchDueEpisode.closed_at.is_(None),
            DbMatchDueEpisode.ack_revision == expected_revision,
        )
        .values(ack_revision=DbMatchDueEpisode.ack_revision + 1)
        .returning(DbMatchDueEpisode.id)
    ).one_or_none()
    db.session.flush()
    return advanced is not None


def _db_pin_to_pin_state(row: DbMatchDashboardAnnotation) -> MatchPinState:
    return MatchPinState(
        match_id=row.match_id,
        tournament_id=row.tournament_id,
        revision=row.revision,
        pinned_at=row.pinned_at,
        pinned_by=row.pinned_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def find_match_pin_state(match_id: TournamentMatchID) -> MatchPinState | None:
    """Return the freshly loaded pin of a match, or `None` if never set."""
    row = db.session.scalars(
        select(DbMatchDashboardAnnotation)
        .filter_by(match_id=match_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    return _db_pin_to_pin_state(row) if row is not None else None


def save_match_pin_flush(
    match_id: TournamentMatchID,
    tournament_id: TournamentID,
    *,
    pinned_at: datetime | None,
    pinned_by: UserID | None,
    updated_at: datetime,
    updated_by: UserID,
    expected_revision: int,
) -> MatchPinState | None:
    """Store the pin of a match if its revision is `expected_revision`.

    Revision 0 means never annotated and inserts. Return the new state,
    or `None` if another writer got there first (flush only). A pin
    without `pinned_at` and `pinned_by` is an unpin.
    """
    values = {
        'pinned_at': _naive_utc_or_none(pinned_at),
        'pinned_by': pinned_by,
        'updated_at': _naive_utc(updated_at),
        'updated_by': updated_by,
    }
    statement: Executable
    if expected_revision == 0:
        statement = (
            pg_insert(DbMatchDashboardAnnotation)
            .values(
                match_id=match_id,
                tournament_id=tournament_id,
                revision=1,
                **values,
            )
            .on_conflict_do_nothing(index_elements=['match_id'])
            .returning(DbMatchDashboardAnnotation)
        )
    else:
        statement = (
            update(DbMatchDashboardAnnotation)
            .where(
                DbMatchDashboardAnnotation.match_id == match_id,
                DbMatchDashboardAnnotation.revision == expected_revision,
            )
            .values(revision=DbMatchDashboardAnnotation.revision + 1, **values)
            .returning(DbMatchDashboardAnnotation)
        )
    row = db.session.scalars(
        statement, execution_options={'populate_existing': True}
    ).one_or_none()
    db.session.flush()
    return _db_pin_to_pin_state(row) if row is not None else None


def _db_thresholds_to_thresholds(
    row: DbDashboardPartyThresholds,
) -> PartyDashboardThresholds:
    return PartyDashboardThresholds(
        party_id=row.party_id,
        yellow_minutes=row.yellow_minutes,
        red_minutes=row.red_minutes,
        revision=row.revision,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def find_party_thresholds(
    party_id: PartyID,
) -> PartyDashboardThresholds | None:
    """Return the freshly loaded party override, or `None` if unset."""
    row = db.session.scalars(
        select(DbDashboardPartyThresholds)
        .filter_by(party_id=party_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    return _db_thresholds_to_thresholds(row) if row is not None else None


def set_party_thresholds_flush(
    party_id: PartyID,
    *,
    yellow_minutes: int,
    red_minutes: int,
    expected_revision: int,
    expected_updated_at: datetime | None,
    updated_at: datetime,
    updated_by: UserID,
) -> PartyDashboardThresholds | None:
    """Store the party override if it is the one that was read.

    The override is identified by its revision and its `updated_at`: a
    reset and a new save restart the revision, the time tells them
    apart. Revision 0 means no override yet, inserts and takes no
    `expected_updated_at`. Return the new row, or `None` if another
    writer got there first (flush only). The caller validates the
    values; the database checks are the backstop.
    """
    values = {
        'yellow_minutes': yellow_minutes,
        'red_minutes': red_minutes,
        'updated_at': _naive_utc(updated_at),
        'updated_by': updated_by,
    }
    statement: Executable
    if expected_revision == 0:
        statement = (
            pg_insert(DbDashboardPartyThresholds)
            .values(party_id=party_id, revision=1, **values)
            .on_conflict_do_nothing(index_elements=['party_id'])
            .returning(DbDashboardPartyThresholds)
        )
    else:
        statement = (
            update(DbDashboardPartyThresholds)
            .where(
                DbDashboardPartyThresholds.party_id == party_id,
                DbDashboardPartyThresholds.revision == expected_revision,
                DbDashboardPartyThresholds.updated_at
                == _naive_utc_or_none(expected_updated_at),
            )
            .values(revision=DbDashboardPartyThresholds.revision + 1, **values)
            .returning(DbDashboardPartyThresholds)
        )
    row = db.session.scalars(
        statement, execution_options={'populate_existing': True}
    ).one_or_none()
    db.session.flush()
    return _db_thresholds_to_thresholds(row) if row is not None else None


def delete_party_thresholds_flush(
    party_id: PartyID, *, expected_revision: int, expected_updated_at: datetime
) -> bool:
    """Delete the party override if it is the one that was read.

    The override is identified by its revision and its `updated_at`, as
    in `set_party_thresholds_flush`. Return whether a row was deleted
    (flush only).
    """
    deleted = db.session.execute(
        delete(DbDashboardPartyThresholds)
        .where(
            DbDashboardPartyThresholds.party_id == party_id,
            DbDashboardPartyThresholds.revision == expected_revision,
            DbDashboardPartyThresholds.updated_at
            == _naive_utc(expected_updated_at),
        )
        .returning(DbDashboardPartyThresholds.party_id)
    ).one_or_none()
    db.session.flush()
    return deleted is not None
