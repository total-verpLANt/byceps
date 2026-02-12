from datetime import UTC, datetime

from byceps.services.user.models.user import UserID
from byceps.util.result import Err, Ok, Result

from . import tournament_repository
from .events import (
    ContestantAdvancedEvent,
    MatchConfirmedEvent,
    MatchUnconfirmedEvent,
)
from .models.tournament import TournamentID
from .models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from .models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from .models.tournament_participant import TournamentParticipantID
from .models.tournament_seed import TournamentSeed
from .models.tournament_team import TournamentTeamID
from .signals import (
    contestant_advanced,
    match_confirmed,
    match_unconfirmed,
)
from .tournament_domain_service import determine_match_winner


def set_seed(
    seed_list: list[TournamentSeed],
    tournament_id: TournamentID,
) -> Result[None, str]:
    """Set seeding for a tournament."""
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
    from .models.contestant_type import ContestantType

    # Get tournament to check contestant type
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        return Err('Tournament contestant type is not set.')

    is_team_tournament = tournament.contestant_type == ContestantType.TEAM

    now = datetime.now(UTC)

    match_events: list[MatchCreatedEvent] = []

    # Create matches and contestants for each seed
    for seed in seed_list:
        # Create the match
        match_id = TournamentMatchID(generate_uuid7())
        match = TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=seed.match_order,
            round=seed.round,
            next_match_id=None,
            confirmed_by=None,
            created_at=now,
        )
        tournament_repository.create_match(match)

        match_event = MatchCreatedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=tournament_id,
            match_id=match_id,
        )
        match_events.append(match_event)

        # Create contestants for entry_a and entry_b
        # Skip if entry is "DEFWIN"
        if seed.entry_a.upper() != 'DEFWIN':
            contestant_a_id = TournamentMatchToContestantID(generate_uuid7())
            if is_team_tournament:
                contestant_a = TournamentMatchToContestant(
                    id=contestant_a_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(seed.entry_a)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                contestant_a = TournamentMatchToContestant(
                    id=contestant_a_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(UUID(seed.entry_a)),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant_a)

        if seed.entry_b.upper() != 'DEFWIN':
            contestant_b_id = TournamentMatchToContestantID(generate_uuid7())
            if is_team_tournament:
                contestant_b = TournamentMatchToContestant(
                    id=contestant_b_id,
                    tournament_match_id=match_id,
                    team_id=TournamentTeamID(UUID(seed.entry_b)),
                    participant_id=None,
                    score=None,
                    created_at=now,
                )
            else:
                contestant_b = TournamentMatchToContestant(
                    id=contestant_b_id,
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=TournamentParticipantID(UUID(seed.entry_b)),
                    score=None,
                    created_at=now,
                )
            tournament_repository.create_match_contestant(contestant_b)

    # Commit entire seeding as a single transaction
    tournament_repository.commit_session()

    # Dispatch events after successful commit
    for match_event in match_events:
        signals.match_created.send(None, event=match_event)

    return Ok(None)


def has_matches(tournament_id: TournamentID) -> bool:
    """Check if tournament already has matches."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    return len(matches) > 0


def clear_bracket(tournament_id: TournamentID) -> Result[None, str]:
    """Clear all matches from a tournament bracket."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    
    for match in matches:
        delete_match(match.id)
    
    tournament_repository.commit_session()
    
    return Ok(None)


def generate_single_elimination_bracket(
    tournament_id: TournamentID,
    force_regenerate: bool = False,
) -> Result[int, str]:
    """Generate single elimination bracket with all rounds."""
    import math
    from uuid import UUID

    from byceps.util.uuid import generate_uuid7

    from . import signals
    from .events import MatchCreatedEvent
    from .models.contestant_type import ContestantType
    from .tournament_domain_service import _standard_seed_order

    # CRITICAL: Lock tournament to prevent race condition (TOCTOU vulnerability)
    # This blocks concurrent bracket generation attempts until this transaction commits
    tournament_repository.lock_tournament_for_update(tournament_id)

    # Check if matches already exist (now atomic with lock)
    if has_matches(tournament_id) and not force_regenerate:
        return Err('Tournament already has matches. Use force regenerate to clear and rebuild.')

    # Clear existing matches if force regenerate
    if force_regenerate and has_matches(tournament_id):
        clear_result = clear_bracket(tournament_id)
        if clear_result.is_err():
            return Err(f'Failed to clear existing bracket: {clear_result.unwrap_err()}')

    # Get tournament to check contestant type
    tournament = tournament_repository.get_tournament(tournament_id)
    if tournament.contestant_type is None:
        return Err('Tournament contestant type is not set.')

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
        return Err('Need at least 2 contestants for bracket.')

    # Calculate bracket geometry
    bracket_size = 2 ** math.ceil(math.log2(num_contestants))
    num_rounds = int(math.log2(bracket_size))
    is_team = tournament.contestant_type == ContestantType.TEAM
    now = datetime.now(UTC)

    # Build rounds from final backwards so we can set next_match_id
    # rounds_matches[r] holds match IDs for round r
    rounds_matches: list[list[TournamentMatchID]] = [
        [] for _ in range(num_rounds)
    ]
    match_events: list[MatchCreatedEvent] = []

    # Create matches round by round, final first
    for r in range(num_rounds - 1, -1, -1):
        num_matches_in_round = 2 ** (num_rounds - 1 - r)
        for m in range(num_matches_in_round):
            match_id = TournamentMatchID(generate_uuid7())

            # Determine next_match_id from the next round
            if r < num_rounds - 1:
                next_match_id = rounds_matches[r + 1][m // 2]
            else:
                next_match_id = None

            match = TournamentMatch(
                id=match_id,
                tournament_id=tournament_id,
                group_order=None,
                match_order=m,
                round=r,
                next_match_id=next_match_id,
                confirmed_by=None,
                created_at=now,
            )
            tournament_repository.create_match(match)
            rounds_matches[r].append(match_id)

            match_events.append(
                MatchCreatedEvent(
                    occurred_at=now,
                    initiator=None,
                    tournament_id=tournament_id,
                    match_id=match_id,
                )
            )

    # Seed round 0 using standard seed order
    seed_order = _standard_seed_order(bracket_size)

    # Pad contestant list with None for DEFWINs
    padded: list[str | None] = list(contestant_ids) + [None] * (
        bracket_size - num_contestants
    )

    # Place contestants into round 0 matches
    for slot_idx, seed_pos in enumerate(seed_order):
        match_idx = slot_idx // 2
        match_id = rounds_matches[0][match_idx]
        cid = padded[seed_pos]

        if cid is None:
            continue  # DEFWIN — no contestant to place

        contestant_id = TournamentMatchToContestantID(generate_uuid7())
        if is_team:
            contestant = TournamentMatchToContestant(
                id=contestant_id,
                tournament_match_id=match_id,
                team_id=TournamentTeamID(UUID(cid)),
                participant_id=None,
                score=None,
                created_at=now,
            )
        else:
            contestant = TournamentMatchToContestant(
                id=contestant_id,
                tournament_match_id=match_id,
                team_id=None,
                participant_id=TournamentParticipantID(UUID(cid)),
                score=None,
                created_at=now,
            )
        tournament_repository.create_match_contestant(contestant)

    # Auto-advance DEFWIN matches: if a round 0 match has only
    # 1 contestant, advance that contestant to the next match
    for match_idx, match_id in enumerate(rounds_matches[0]):
        contestants = tournament_repository.get_contestants_for_match(match_id)
        if len(contestants) == 1 and num_rounds > 1:
            # Advance the sole contestant to the next round
            sole = contestants[0]
            next_mid = rounds_matches[1][match_idx // 2]
            adv_id = TournamentMatchToContestantID(generate_uuid7())
            advanced = TournamentMatchToContestant(
                id=adv_id,
                tournament_match_id=next_mid,
                team_id=sole.team_id,
                participant_id=sole.participant_id,
                score=None,
                created_at=now,
            )
            tournament_repository.create_match_contestant(advanced)

    # Single transaction commit
    tournament_repository.commit_session()

    # Dispatch events after successful commit
    for event in match_events:
        signals.match_created.send(None, event=event)

    total_matches = bracket_size - 1
    return Ok(total_matches)


def reset_match(match_id: TournamentMatchID) -> None:
    """Reset a match (with cascading delete of dependents)."""
    delete_match(match_id)


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
) -> Result[None, str]:
    """Confirm a match result."""
    # Get match via repository
    match = tournament_repository.get_match(match_id)

    # Check if already confirmed
    if match.confirmed_by is not None:
        return Err('Match is already confirmed.')

    # Validate that both contestants have scores
    contestants = tournament_repository.get_contestants_for_match(match_id)
    if len(contestants) < 2:
        return Err('Cannot confirm match with less than 2 contestants.')

    for contestant in contestants:
        if contestant.score is None:
            return Err(
                'Cannot confirm match: all contestants must have scores.'
            )

    # Determine winner.
    winner_result = determine_match_winner(contestants)
    if winner_result.is_err():
        return Err(winner_result.unwrap_err())

    winner = winner_result.unwrap()

    tournament_repository.confirm_match(match_id, confirmed_by_user_id)
    tournament_repository.commit_session()

    now = datetime.now(UTC)
    winner_team_id = winner.team_id
    winner_participant_id = winner.participant_id

    match_confirmed.send(
        MatchConfirmedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match_id,
            winner_team_id=winner_team_id,
            winner_participant_id=winner_participant_id,
        )
    )

    if match.next_match_id is not None:
        from byceps.util.uuid import generate_uuid7

        contestant_id = TournamentMatchToContestantID(generate_uuid7())
        new_contestant = TournamentMatchToContestant(
            id=contestant_id,
            tournament_match_id=match.next_match_id,
            team_id=winner_team_id,
            participant_id=winner_participant_id,
            score=None,
            created_at=now,
        )
        tournament_repository.create_match_contestant(new_contestant)
        tournament_repository.commit_session()

        contestant_advanced.send(
            ContestantAdvancedEvent(
                occurred_at=now,
                initiator=None,
                tournament_id=match.tournament_id,
                match_id=match.next_match_id,
                from_match_id=match_id,
                advanced_team_id=winner_team_id,
                advanced_participant_id=winner_participant_id,
            )
        )

    return Ok(None)


def unconfirm_match(
    match_id: TournamentMatchID,
    initiator_id: UserID,
) -> Result[None, str]:
    """Unconfirm a match and cascade-retract advanced contestants."""
    match = tournament_repository.get_match(match_id)
    if match is None:
        return Err('Match not found.')

    if match.confirmed_by is None:
        return Err('Match is not confirmed.')

    contestants = tournament_repository.get_contestants_for_match(match_id)

    # Determine winner for cascade retraction.
    winner_result = determine_match_winner(contestants)

    if match.next_match_id is not None and winner_result.is_ok():
        winner = winner_result.unwrap()
        next_match = tournament_repository.get_match(match.next_match_id)
        if next_match is not None and next_match.confirmed_by is not None:
            # Recursively unconfirm downstream match first.
            cascade_result = unconfirm_match(match.next_match_id, initiator_id)
            if cascade_result.is_err():
                return cascade_result

        # Remove advanced contestant from next match.
        tournament_repository.delete_contestant_from_match(
            match.next_match_id,
            team_id=winner.team_id,
            participant_id=winner.participant_id,
        )

    tournament_repository.unconfirm_match(match_id)
    tournament_repository.commit_session()

    now = datetime.now(UTC)
    match_unconfirmed.send(
        MatchUnconfirmedEvent(
            occurred_at=now,
            initiator=None,
            tournament_id=match.tournament_id,
            match_id=match_id,
            unconfirmed_by=initiator_id,
        )
    )

    return Ok(None)


def set_score(
    match_id: TournamentMatchID,
    contestant_id: TournamentParticipantID | TournamentTeamID,
    score: int,
) -> Result[None, str]:
    """Set the score for a contestant in a match."""
    if score < 0:
        return Err('Score cannot be negative.')

    # Find the contestant entry for this match.
    # Try as participant first, then as team.
    contestant = tournament_repository.find_contestant_for_match(
        match_id,
        participant_id=contestant_id,  # type: ignore[arg-type]
    )
    if contestant is None:
        contestant = tournament_repository.find_contestant_for_match(
            match_id,
            team_id=contestant_id,  # type: ignore[arg-type]
        )
    if contestant is None:
        return Err(
            f'Contestant "{contestant_id}" not found in match "{match_id}"'
        )

    tournament_repository.update_contestant_score(contestant.id, score)

    return Ok(None)


def add_comment(
    match_id: TournamentMatchID,
    created_by_user_id: UserID,
    comment: str,
) -> Result[None, str]:
    """Add a comment to a match."""
    from byceps.util.uuid import generate_uuid7

    # Validate comment length (max 1000 chars per Task #17)
    if len(comment) > 1000:
        return Err('Comment cannot exceed 1000 characters.')

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

    return Ok(None)


def update_comment(
    comment_id: TournamentMatchCommentID,
    comment: str,
) -> Result[None, str]:
    """Update a match comment."""
    # Validate comment length (max 1000 chars)
    if len(comment) > 1000:
        return Err('Comment cannot exceed 1000 characters.')

    tournament_repository.update_match_comment(comment_id, comment)

    return Ok(None)


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


def get_contestants_for_match(
    match_id: TournamentMatchID,
) -> list[TournamentMatchToContestant]:
    """Return all contestants for that match."""
    return tournament_repository.get_contestants_for_match(match_id)
