from datetime import datetime

from sqlalchemy import delete, select

from byceps.database import db
from byceps.services.party.models import PartyID
from byceps.services.user.models.user import UserID

from .dbmodels.match import DbTournamentMatch
from .dbmodels.match_comment import DbTournamentMatchComment
from .dbmodels.match_contestant import DbTournamentMatchToContestant
from .dbmodels.participant import DbTournamentParticipant
from .dbmodels.team import DbTournamentTeam
from .dbmodels.tournament import DbTournament
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.tournament_match import TournamentMatch, TournamentMatchID
from .models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from .models.tournament_mode import TournamentMode
from .models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeam, TournamentTeamID


# -- tournament --


def _safe_enum_lookup(enum_class, value: str | None, default=None):
    """Safely look up enum by name, returning default if invalid.

    Protects against database corruption or invalid enum values.
    """
    if value is None:
        return None
    try:
        return enum_class[value]
    except KeyError:
        # Log warning about invalid enum value in production
        return default


def create_tournament(tournament: Tournament) -> None:
    """Persist a tournament."""
    db_tournament = DbTournament(
        tournament.id,
        tournament.party_id,
        tournament.name,
        tournament.created_at,
        game=tournament.game,
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
        tournament_mode=(
            tournament.tournament_mode.name
            if tournament.tournament_mode
            else None
        ),
    )

    db.session.add(db_tournament)
    db.session.commit()


def update_tournament(tournament: Tournament) -> None:
    """Update a tournament in place (no delete/recreate)."""
    db_tournament = db.session.get(DbTournament, tournament.id)
    if db_tournament is None:
        raise ValueError(f'Unknown tournament ID "{tournament.id}"')

    db_tournament.name = tournament.name
    db_tournament.game = tournament.game
    db_tournament.description = tournament.description
    db_tournament.image_url = tournament.image_url
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
    db_tournament.tournament_status = (
        tournament.tournament_status.name
        if tournament.tournament_status
        else None
    )
    db_tournament.tournament_mode = (
        tournament.tournament_mode.name if tournament.tournament_mode else None
    )
    db_tournament.updated_at = tournament.updated_at

    db.session.commit()


def delete_tournament(tournament_id: TournamentID) -> None:
    """Delete a tournament."""
    db.session.execute(delete(DbTournament).filter_by(id=tournament_id))
    db.session.commit()


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
) -> Tournament:
    """Return the tournament.

    Raise an exception if not found.
    """
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
        select(DbTournament).filter_by(id=tournament_id).with_for_update()
    ).scalar_one_or_none()
    if db_tournament is None:
        raise ValueError(f'Unknown tournament ID "{tournament_id}"')
    return _db_tournament_to_tournament(db_tournament)


def get_tournaments_for_party(
    party_id: PartyID,
) -> list[Tournament]:
    """Return all tournaments for that party."""
    db_tournaments = (
        db.session.execute(select(DbTournament).filter_by(party_id=party_id))
        .scalars()
        .all()
    )
    return [_db_tournament_to_tournament(t) for t in db_tournaments]


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
        contestant_type=_safe_enum_lookup(
            ContestantType, db_tournament.contestant_type
        ),
        tournament_status=_safe_enum_lookup(
            TournamentStatus, db_tournament.tournament_status
        ),
        tournament_mode=_safe_enum_lookup(
            TournamentMode, db_tournament.tournament_mode
        ),
    )


# -- team --


def create_team(team: TournamentTeam) -> None:
    """Persist a team."""
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
    db.session.commit()


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
    """Update the captain of a team (flush only, caller commits)."""
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


def delete_teams_for_tournament(tournament_id: TournamentID) -> None:
    """Delete all teams for a tournament."""
    db.session.execute(
        delete(DbTournamentTeam).filter_by(tournament_id=tournament_id)
    )
    db.session.commit()


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
        select(DbTournamentTeam).filter_by(id=team_id).with_for_update()
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
    db_participant = db.session.get(DbTournamentParticipant, participant.id)
    if db_participant is None:
        raise ValueError(f'Unknown participant ID "{participant.id}"')

    db_participant.substitute_player = participant.substitute_player
    db_participant.team_id = participant.team_id

    db.session.commit()


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


def delete_participants_for_tournament(tournament_id: TournamentID) -> None:
    """Delete all participants for a tournament."""
    db.session.execute(
        delete(DbTournamentParticipant).filter_by(tournament_id=tournament_id)
    )
    db.session.commit()


def remove_team_from_participants(team_id: TournamentTeamID) -> None:
    """Set team_id to NULL for all participants in this team."""
    db.session.execute(
        db.update(DbTournamentParticipant)
        .filter_by(team_id=team_id)
        .values(team_id=None)
    )
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
    db_participants = db.session.execute(stmt).scalars().all()
    return [_db_participant_to_participant(p) for p in db_participants]


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


def create_match(match: TournamentMatch) -> None:
    """Persist a match."""
    db_match = DbTournamentMatch(
        match.id,
        match.tournament_id,
        match.created_at,
        group_order=match.group_order,
        match_order=match.match_order,
        round=match.round,
        next_match_id=match.next_match_id,
        confirmed_by=match.confirmed_by,
    )

    db.session.add(db_match)
    db.session.flush()


def delete_match(match_id: TournamentMatchID) -> None:
    """Delete a match."""
    db.session.execute(delete(DbTournamentMatch).filter_by(id=match_id))
    db.session.commit()


def delete_matches_for_tournament(tournament_id: TournamentID) -> None:
    """Delete all matches for a tournament."""
    # NULL out self-referential FKs first
    db.session.execute(
        db.update(DbTournamentMatch)
        .filter_by(tournament_id=tournament_id)
        .values(next_match_id=None)
    )
    db.session.execute(
        delete(DbTournamentMatch).filter_by(tournament_id=tournament_id)
    )
    db.session.commit()


def find_match(
    match_id: TournamentMatchID,
) -> TournamentMatch | None:
    """Return the match, or `None` if not found."""
    db_match = db.session.get(DbTournamentMatch, match_id)
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


def confirm_match(
    match_id: TournamentMatchID,
    confirmed_by: UserID,
) -> None:
    """Set the confirmed_by field on a match."""
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')

    db_match.confirmed_by = confirmed_by
    db.session.commit()


def unconfirm_match(match_id: TournamentMatchID) -> None:
    """Reset the confirmed_by field on a match."""
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')

    db_match.confirmed_by = None
    db.session.flush()


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
    )


# -- match comment --


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
    )

    db.session.add(db_comment)
    db.session.commit()


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


def delete_comments_for_tournament(tournament_id: TournamentID) -> None:
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
    )


# -- match contestant --


def create_match_contestant(
    contestant: TournamentMatchToContestant,
) -> None:
    """Persist a match contestant."""
    db_contestant = DbTournamentMatchToContestant(
        contestant.id,
        contestant.tournament_match_id,
        contestant.created_at,
        team_id=contestant.team_id,
        participant_id=contestant.participant_id,
        score=contestant.score,
    )

    db.session.add(db_contestant)
    db.session.flush()


def update_contestant_score(
    contestant_id: TournamentMatchToContestantID,
    score: int,
) -> None:
    """Update a contestant's score."""
    db_contestant = db.session.get(DbTournamentMatchToContestant, contestant_id)
    if db_contestant is None:
        raise ValueError(f'Unknown contestant ID "{contestant_id}"')

    db_contestant.score = score
    db.session.commit()


def get_contestants_for_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchToContestant]:
    """Return all contestants for that match."""
    db_contestants = (
        db.session.execute(
            select(DbTournamentMatchToContestant).filter_by(
                tournament_match_id=match_id
            )
        )
        .scalars()
        .all()
    )
    return [_db_contestant_to_contestant(c) for c in db_contestants]


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
    )


def delete_match_contestant(
    contestant_id: TournamentMatchToContestantID,
) -> None:
    """Delete a match contestant."""
    db.session.execute(
        delete(DbTournamentMatchToContestant).filter_by(id=contestant_id)
    )
    db.session.commit()


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
    db.session.execute(query)
    db.session.flush()


def delete_contestants_for_match(match_id: TournamentMatchID) -> None:
    """Delete all contestants for a match."""
    db.session.execute(
        delete(DbTournamentMatchToContestant).filter_by(
            tournament_match_id=match_id
        )
    )
    db.session.commit()


def delete_contestants_for_tournament(tournament_id: TournamentID) -> None:
    """Delete all contestants for all matches in a tournament."""
    db.session.execute(
        delete(DbTournamentMatchToContestant).where(
            DbTournamentMatchToContestant.tournament_match_id.in_(
                select(DbTournamentMatch.id).filter_by(
                    tournament_id=tournament_id
                )
            )
        )
    )
    db.session.commit()


def remove_team_from_contestants(team_id: TournamentTeamID) -> None:
    """Delete all match contestants referencing this team."""
    db.session.execute(
        db.delete(DbTournamentMatchToContestant).filter_by(team_id=team_id)
    )
    db.session.commit()
