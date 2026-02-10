from byceps.services.user.models.user import UserID

from . import tournament_repository
from .models.tournament import TournamentID
from .models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from .models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_seed import TournamentSeed
from .models.tournament_team import TournamentTeamID
from datetime import UTC


def set_seed(
    seed_list: list[TournamentSeed],
    tournament_id: TournamentID,
) -> None:
    """Set seeding for a tournament."""
    from datetime import datetime
    from uuid import UUID

    from .models.tournament_match import TournamentMatch, TournamentMatchID
    from .models.tournament_match_to_contestant import (
        TournamentMatchToContestant,
        TournamentMatchToContestantID,
    )
    from byceps.util.uuid import generate_uuid7

    # Get tournament to check contestant type
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        raise ValueError('Tournament contestant type is not set.')

    from .models.contestant_type import ContestantType

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    now = datetime.now(UTC)

    # Create matches and contestants for each seed
    for seed in seed_list:
        # Create the match
        match_id = TournamentMatchID(generate_uuid7())
        match = TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=seed.match_order,
            confirmed_by=None,
            created_at=now,
        )
        tournament_repository.create_match(match)

        # Create contestants for entry_a and entry_b
        # Skip if entry is "BYE"
        if seed.entry_a.upper() != 'BYE':
            contestant_a_id = TournamentMatchToContestantID(generate_uuid7())
            if is_team_tournament:
                from .models.tournament_team import TournamentTeamID

                contestant_a = TournamentMatchToContestant(
                    id=contestant_a_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(seed.entry_a)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                from .models.tournament_participant import (
                    TournamentParticipantID,
                )

                contestant_a = TournamentMatchToContestant(
                    id=contestant_a_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(
                        UUID(seed.entry_a)
                    ),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant_a)

        if seed.entry_b.upper() != 'BYE':
            contestant_b_id = TournamentMatchToContestantID(generate_uuid7())
            if is_team_tournament:
                from .models.tournament_team import TournamentTeamID

                contestant_b = TournamentMatchToContestant(
                    id=contestant_b_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(seed.entry_b)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                from .models.tournament_participant import (
                    TournamentParticipantID,
                )

                contestant_b = TournamentMatchToContestant(
                    id=contestant_b_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(
                        UUID(seed.entry_b)
                    ),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant_b)



def generate_single_elimination_bracket(
    tournament_id: TournamentID,
) -> list[TournamentSeed]:
    """Generate single elimination bracket with seeding."""
    import math

    from .models.contestant_type import ContestantType

    # Get tournament to check contestant type
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        raise ValueError('Tournament contestant type is not set.')

    # Get contestants (participants or teams)
    if tournament.contestant_type == ContestantType.TEAM:
        teams = tournament_repository.get_teams_for_tournament(tournament_id)
        contestant_ids = [str(team.id) for team in teams]
    else:
        participants = tournament_repository.get_participants_for_tournament(
            tournament_id
        )
        contestant_ids = [str(p.id) for p in participants]

    num_contestants = len(contestant_ids)
    if num_contestants < 2:
        raise ValueError('Need at least 2 contestants for bracket.')

    # Calculate next power of 2
    bracket_size = 2 ** math.ceil(math.log2(num_contestants))

    # Create seed list
    seeds = []
    match_order = 0

    # Standard single elimination seeding pattern
    # For 8 players: 1v8, 4v5, 2v7, 3v6
    # For 4 players: 1v4, 2v3
    # With BYEs: higher seeds get BYEs

    # Simple approach: pair contestants sequentially, add BYEs at end
    contestant_index = 0
    for _i in range(bracket_size // 2):
        # Get entry_a
        if contestant_index < num_contestants:
            entry_a = contestant_ids[contestant_index]
            contestant_index += 1
        else:
            entry_a = 'BYE'

        # Get entry_b
        if contestant_index < num_contestants:
            entry_b = contestant_ids[contestant_index]
            contestant_index += 1
        else:
            entry_b = 'BYE'

        seed = TournamentSeed(
            match_order=match_order,
            entry_a=entry_a,
            entry_b=entry_b,
        )
        seeds.append(seed)
        match_order += 1

    return seeds


def reset_match(match_id: TournamentMatchID) -> None:
    """Reset a match."""
    tournament_repository.delete_match(match_id)


def get_match(
    match_id: TournamentMatchID,
) -> TournamentMatch:
    """Return the match."""
    return tournament_repository.get_match(match_id)


def get_matches_for_tournament(
    tournament_id: TournamentID,
) -> list[TournamentMatch]:
    """Return all matches for that tournament."""
    return tournament_repository.get_matches_for_tournament(tournament_id)


def confirm_match(
    match_id: TournamentMatchID,
    confirmed_by_user_id: UserID,
) -> None:
    """Confirm a match result."""
    from .dbmodels.match import DbTournamentMatch
    from byceps.database import db

    # Get match and set confirmed_by
    db_match = db.session.get(DbTournamentMatch, match_id)
    if db_match is None:
        raise ValueError(f'Unknown match ID "{match_id}"')

    # Check if already confirmed
    if db_match.confirmed_by is not None:
        raise ValueError('Match is already confirmed.')

    # Validate that both contestants have scores
    contestants = tournament_repository.get_contestants_for_match(match_id)
    if len(contestants) < 2:
        raise ValueError('Cannot confirm match with less than 2 contestants.')

    for contestant in contestants:
        if contestant.score is None:
            raise ValueError(
                'Cannot confirm match: all contestants must have scores.'
            )

    db_match.confirmed_by = confirmed_by_user_id
    db.session.commit()

    # TODO: Bracket advancement logic (Task #4)
    # - Determine winner
    # - Find next match in bracket
    # - Add winner to next match


def set_score(
    match_id: TournamentMatchID,
    contestant_id: TournamentParticipantID | TournamentTeamID,
    score: int,
) -> None:
    """Set the score for a contestant in a match."""
    if score < 0:
        raise ValueError('Score cannot be negative.')

    # Find the contestant entry for this match
    # Try as participant first, then as team
    try:
        contestant = tournament_repository.find_contestant_for_match(
            match_id,
            participant_id=contestant_id,  # type: ignore[arg-type]
        )
    except ValueError:
        # Not a valid participant_id, try as team_id
        contestant = tournament_repository.find_contestant_for_match(
            match_id,
            team_id=contestant_id,  # type: ignore[arg-type]
        )

    if contestant is None:
        raise ValueError(
            f'Contestant "{contestant_id}" not found in match "{match_id}"'
        )

    tournament_repository.update_contestant_score(contestant.id, score)


def add_comment(
    match_id: TournamentMatchID,
    created_by_user_id: UserID,
    comment: str,
) -> None:
    """Add a comment to a match."""
    from datetime import datetime

    from .models.tournament_match_comment import (
        TournamentMatchComment,
        TournamentMatchCommentID,
    )
    from byceps.util.uuid import generate_uuid7

    # Validate comment length (max 1000 chars per Task #17)
    if len(comment) > 1000:
        raise ValueError('Comment cannot exceed 1000 characters.')

    now = datetime.now(UTC)
    comment_id = TournamentMatchCommentID(generate_uuid7())

    match_comment = TournamentMatchComment(
        id=comment_id,
        tournament_match_id=match_id,
        created_by=created_by_user_id,
        comment=comment,
        created_at=now,
    )

    tournament_repository.create_match_comment(match_comment)


def update_comment(
    comment_id: TournamentMatchCommentID,
    comment: str,
) -> None:
    """Update a match comment."""
    # Validate comment length (max 1000 chars per Task #17)
    if len(comment) > 1000:
        raise ValueError('Comment cannot exceed 1000 characters.')

    # Need to add update method to repository
    # For now, implement inline with db access
    from .dbmodels.match_comment import DbTournamentMatchComment
    from byceps.database import db

    db_comment = db.session.get(DbTournamentMatchComment, comment_id)
    if db_comment is None:
        raise ValueError(f'Unknown comment ID "{comment_id}"')

    db_comment.comment = comment
    db.session.commit()


def delete_comment(
    comment_id: TournamentMatchCommentID,
) -> None:
    """Delete a match comment."""
    tournament_repository.delete_match_comment(comment_id)


def delete_match(
    match_id: TournamentMatchID,
) -> None:
    """Delete a match and all dependent entities.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.administrate' permission).

    CASCADE HANDLING: Deletes all dependent entities in correct order:
    1. Match comments
    2. Match contestants
    3. Match itself
    """
    from datetime import datetime

    from . import signals
    from .events import MatchDeletedEvent

    # Get match to retrieve tournament_id before deletion
    match = tournament_repository.get_match(match_id)

    # Delete in dependency order (children first, then parent)
    tournament_repository.delete_comments_for_match(match_id)
    tournament_repository.delete_contestants_for_match(match_id)
    tournament_repository.delete_match(match_id)

    event = MatchDeletedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=match.tournament_id,
        match_id=match_id,
    )
    signals.match_deleted.send(None, event=event)


def get_comments_from_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchComment]:
    """Return all comments for that match."""
    return tournament_repository.get_comments_for_match(match_id)
