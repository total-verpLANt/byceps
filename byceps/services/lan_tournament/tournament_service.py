import dataclasses
from datetime import datetime, UTC
from urllib.parse import urlparse

from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result

from . import (
    signals,
    tournament_domain_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_repository,
    tournament_score_service,
    tournament_team_service,
)
from .models.bracket import Bracket
from .events import (
    TournamentCreatedEvent,
    TournamentDeletedEvent,
    TournamentStatusChangedEvent,
    TournamentUpdatedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.score_ordering import ScoreOrdering
from .models.game_format import GameFormat, is_valid_combination
from .models.elimination_mode import EliminationMode
from .models.tournament_status import TournamentStatus


# Statuses where only cosmetic fields may be edited.
EDIT_LOCKED_STATUSES: frozenset[TournamentStatus] = frozenset({
    TournamentStatus.ONGOING,
    TournamentStatus.PAUSED,
})


def _validate_ffa_config(
    game_format: GameFormat | None,
    point_table: list[int] | None,
    group_size_max: int | None,
    contestant_type: ContestantType | None,
    max_teams: int | None,
    group_size_min: int | None,
) -> Result[None, str]:
    """Validate FFA-specific configuration fields.

    Returns ``Ok(None)`` when the format is not FFA or when all
    required FFA fields are present and consistent.
    """
    if game_format != GameFormat.FREE_FOR_ALL:
        return Ok(None)

    if point_table is None:
        return Err('FFA tournaments require a point_table.')
    if group_size_max is None:
        return Err('FFA tournaments require group_size_max.')
    if group_size_min is not None and group_size_min > group_size_max:
        return Err(
            f'group_size_min ({group_size_min}) must not exceed '
            f'group_size_max ({group_size_max}).'
        )
    # FFA+TEAM cross-validation
    if (
        contestant_type == ContestantType.TEAM
        and max_teams is not None
        and group_size_min is not None
        and max_teams < group_size_min
    ):
        return Err(
            'Cannot create FFA team tournament: '
            f'max_teams ({max_teams}) < group_size_min '
            f'({group_size_min}). Impossible to form valid groups.'
        )
    return Ok(None)


def _validate_image_url(image_url: str | None) -> Result[None, str]:
    """Validate image URL to prevent XSS/SSRF attacks."""
    if image_url is None or image_url == '':
        return Ok(None)

    try:
        parsed = urlparse(image_url)
        if parsed.scheme not in ('http', 'https'):
            return Err('Image URL must use http or https scheme.')
        if not parsed.netloc:
            return Err('Image URL must have a valid domain.')
    except Exception:
        return Err('Invalid image URL format.')

    return Ok(None)


def create_tournament(
    party_id: PartyID,
    name: str,
    *,
    game: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    ruleset: str | None = None,
    start_time: datetime | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    min_teams: int | None = None,
    max_teams: int | None = None,
    min_players_in_team: int | None = None,
    max_players_in_team: int | None = None,
    contestant_type: ContestantType | None = None,
    tournament_status: TournamentStatus | None = None,
    game_format: GameFormat | None = None,
    elimination_mode: EliminationMode | None = None,
    score_ordering: ScoreOrdering | None = None,
    point_table: list[int] | None = None,
    advancement_count: int | None = None,
    group_size_min: int | None = None,
    group_size_max: int | None = None,
    points_carry_to_losers: bool | None = None,
) -> Result[tuple[Tournament, TournamentCreatedEvent], str]:
    """Create a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.create' permission).
    """
    # Validate name length
    if len(name.strip()) > 80:
        return Err('Tournament name must not exceed 80 characters.')

    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    # Validate game_format + elimination_mode combination
    if game_format is not None and elimination_mode is not None:
        if not is_valid_combination(game_format, elimination_mode):
            return Err(
                f'Invalid combination: {game_format.name} + '
                f'{elimination_mode.name}.'
            )

    # FFA-specific validation
    ffa_result = _validate_ffa_config(
        game_format, point_table, group_size_max,
        contestant_type, max_teams, group_size_min,
    )
    if ffa_result.is_err():
        return Err(ffa_result.unwrap_err())

    tournament, event = tournament_domain_service.create_tournament(
        party_id,
        name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_status=tournament_status,
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        point_table=point_table,
        advancement_count=advancement_count,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        points_carry_to_losers=points_carry_to_losers,
    )

    tournament_repository.create_tournament(tournament)

    signals.tournament_created.send(None, event=event)

    return Ok((tournament, event))


def update_tournament(
    tournament_id: TournamentID,
    *,
    name: str,
    game: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    ruleset: str | None = None,
    start_time: datetime | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    min_teams: int | None = None,
    max_teams: int | None = None,
    min_players_in_team: int | None = None,
    max_players_in_team: int | None = None,
    contestant_type: ContestantType | None = None,
    game_format: GameFormat | None = None,
    elimination_mode: EliminationMode | None = None,
    score_ordering: ScoreOrdering | None = None,
    point_table: list[int] | None = None,
    advancement_count: int | None = None,
    group_size_min: int | None = None,
    group_size_max: int | None = None,
    points_carry_to_losers: bool | None = None,
) -> Result[Tournament, str]:
    """Update a tournament.

    SECURITY NOTE: Authorization must be checked at blueprint layer before
    calling this function (requires 'lan_tournament.update' permission).

    Note: tournament_status is not accepted here.  Status
    changes must go through ``change_status`` to enforce the
    state machine.
    """
    # Validate name length
    if len(name.strip()) > 80:
        return Err('Tournament name must not exceed 80 characters.')

    # Validate image URL
    validation_result = _validate_image_url(image_url)
    if validation_result.is_err():
        return Err(validation_result.unwrap_err())

    # Validate game_format + elimination_mode combination
    if game_format is not None and elimination_mode is not None:
        if not is_valid_combination(game_format, elimination_mode):
            return Err(
                f'Invalid combination: {game_format.name} + '
                f'{elimination_mode.name}.'
            )

    # FFA-specific validation
    ffa_result = _validate_ffa_config(
        game_format, point_table, group_size_max,
        contestant_type, max_teams, group_size_min,
    )
    if ffa_result.is_err():
        return Err(ffa_result.unwrap_err())

    tournament = tournament_repository.get_tournament(tournament_id)

    # Reject structural changes while the tournament is in play.
    if tournament.tournament_status in EDIT_LOCKED_STATUSES:
        locked_changes: list[str] = []
        if name != tournament.name:
            locked_changes.append('name')
        if game != tournament.game:
            locked_changes.append('game')
        if start_time != tournament.start_time:
            locked_changes.append('start_time')
        if contestant_type != tournament.contestant_type:
            locked_changes.append('contestant_type')
        if game_format != tournament.game_format:
            locked_changes.append('game_format')
        if elimination_mode != tournament.elimination_mode:
            locked_changes.append('elimination_mode')
        if score_ordering != tournament.score_ordering:
            locked_changes.append('score_ordering')
        if min_players != tournament.min_players:
            locked_changes.append('min_players')
        if max_players != tournament.max_players:
            locked_changes.append('max_players')
        if min_teams != tournament.min_teams:
            locked_changes.append('min_teams')
        if max_teams != tournament.max_teams:
            locked_changes.append('max_teams')
        if min_players_in_team != tournament.min_players_in_team:
            locked_changes.append('min_players_in_team')
        if max_players_in_team != tournament.max_players_in_team:
            locked_changes.append('max_players_in_team')
        if point_table != tournament.point_table:
            locked_changes.append('point_table')
        if advancement_count != tournament.advancement_count:
            locked_changes.append('advancement_count')
        if group_size_min != tournament.group_size_min:
            locked_changes.append('group_size_min')
        if group_size_max != tournament.group_size_max:
            locked_changes.append('group_size_max')
        if points_carry_to_losers != tournament.points_carry_to_losers:
            locked_changes.append('points_carry_to_losers')
        if locked_changes:
            fields_str = ', '.join(locked_changes)
            return Err(
                f'Tournament is {tournament.tournament_status.name.lower()}. '
                f'Only description, image, and ruleset can be changed. '
                f'Attempted to change: {fields_str}.'
            )

    updated = dataclasses.replace(
        tournament,
        name=name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        updated_at=datetime.now(UTC),
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        game_format=game_format,
        elimination_mode=elimination_mode,
        score_ordering=score_ordering,
        point_table=point_table,
        advancement_count=advancement_count,
        group_size_min=group_size_min,
        group_size_max=group_size_max,
        points_carry_to_losers=points_carry_to_losers,
    )

    tournament_repository.update_tournament(updated)

    event = TournamentUpdatedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
    )
    signals.tournament_updated.send(None, event=event)

    return Ok(updated)


def delete_tournament(
    tournament_id: TournamentID,
) -> None:
    """Delete a tournament and all dependent entities.

    SECURITY NOTE: Authorization must be checked at blueprint layer
    before calling this function (requires
    'lan_tournament.administrate' permission).

    CASCADE HANDLING: Deletes all dependent entities in correct
    order:
    1. Score submissions (FK to participants/teams)
    2. Match comments
    3. Match contestants
    4. Matches
    5. Winner references (FK back to teams/participants)
    6. Participants
    7. Teams
    8. Tournament itself
    """
    # Delete in dependency order (children first, then parent).
    # All repo calls use commit=False so the entire cascade is a
    # single atomic transaction committed once at the end.
    # Wrapped in try/except to rollback on partial flush failure,
    # preventing session poisoning if a caller catches the exception.
    try:
        tournament_repository.delete_submissions_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_comments_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_contestants_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_matches_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.clear_winner_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_participants_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_teams_for_tournament(
            tournament_id, commit=False
        )
        tournament_repository.delete_tournament(tournament_id, commit=False)

        tournament_repository.commit_session()
    except Exception:
        tournament_repository.rollback_session()
        raise

    event = TournamentDeletedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament_id,
    )
    signals.tournament_deleted.send(None, event=event)


def get_tournament(
    tournament_id: TournamentID,
) -> Tournament:
    """Return the tournament with that ID."""
    return tournament_repository.get_tournament(tournament_id)


def find_tournament(
    tournament_id: TournamentID,
) -> Tournament | None:
    """Return the tournament, or `None` if not found."""
    return tournament_repository.find_tournament(tournament_id)


def get_tournaments_for_party(
    party_id: PartyID,
) -> list[Tournament]:
    """Return all tournaments for that party."""
    return tournament_repository.get_tournaments_for_party(party_id)


def get_participant_count(
    tournament_id: TournamentID,
) -> int:
    """Return the number of participants."""
    return tournament_repository.get_participant_count(tournament_id)


def get_participant_counts_for_tournaments(
    tournament_ids: list[TournamentID],
) -> dict[TournamentID, int]:
    """Return participant counts keyed by tournament ID."""
    return tournament_repository.get_participant_counts_for_tournaments(
        tournament_ids
    )


def _has_bracket_generated(
    tournament_id: TournamentID,
) -> bool:
    """Check if brackets have been generated for the tournament."""
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    return len(matches) > 0


def change_status(
    tournament_id: TournamentID,
    new_status: TournamentStatus,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Change the tournament status."""
    tournament = tournament_repository.get_tournament(tournament_id)

    # Validate state machine transition first
    result = tournament_domain_service.change_tournament_status(
        tournament, new_status
    )
    if result.is_err():
        return Err(result.unwrap_err())

    # Only after a valid transition: check bracket exists when starting
    if new_status == TournamentStatus.ONGOING:
        if tournament.game_format and tournament.game_format.requires_bracket_generation:
            if not _has_bracket_generated(tournament_id):
                return Err(
                    'Cannot start tournament without generated brackets. '
                    'Generate brackets first.'
                )

    (event,) = result.unwrap()

    updated = dataclasses.replace(tournament, tournament_status=new_status)
    tournament_repository.update_tournament(updated)

    signals.tournament_status_changed.send(None, event=event)

    return Ok((updated, event))


def start_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Start a tournament."""
    return change_status(tournament_id, TournamentStatus.ONGOING)


def pause_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Pause a tournament."""
    return change_status(tournament_id, TournamentStatus.PAUSED)


def resume_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """Resume a paused tournament."""
    return change_status(tournament_id, TournamentStatus.ONGOING)


def end_tournament(
    tournament_id: TournamentID,
) -> Result[tuple[Tournament, TournamentStatusChangedEvent], str]:
    """End a tournament."""
    return change_status(tournament_id, TournamentStatus.COMPLETED)


def resolve_winner_display_name(tournament: Tournament) -> str | None:
    """Return a human-readable winner name for a completed tournament,
    or ``None`` if no winner is recorded or the tournament is not
    completed.
    """
    if tournament.tournament_status != TournamentStatus.COMPLETED:
        return None

    if tournament.winner_team_id:
        team = tournament_team_service.find_team(tournament.winner_team_id)
        if team is not None:
            return team.name
    elif tournament.winner_participant_id:
        from byceps.services.user import user_service

        participant = tournament_participant_service.find_participant(
            tournament.winner_participant_id
        )
        if participant is not None:
            user = user_service.get_user(participant.user_id)
            return user.screen_name

    return None


def resolve_podium_display_names(
    tournament: Tournament,
) -> dict[str, str | None]:
    """Return runner-up and bronze display names for a completed tournament.

    Returns a dict with keys ``'runner_up'`` and ``'bronze'``.  Values
    are ``None`` when the tournament is not completed or the position
    cannot be determined.

    The champion is intentionally **not** included — callers should use
    :func:`resolve_winner_display_name` for that to avoid redundant
    look-ups.
    """
    empty: dict[str, str | None] = {
        'runner_up': None,
        'bronze': None,
    }

    if tournament.tournament_status != TournamentStatus.COMPLETED:
        return empty

    gf = tournament.game_format
    em = tournament.elimination_mode
    if gf == GameFormat.ONE_V_ONE and em in (
        EliminationMode.SINGLE_ELIMINATION,
        EliminationMode.DOUBLE_ELIMINATION,
    ):
        runner_up, bronze = _resolve_bracket_podium(tournament)
    elif em == EliminationMode.ROUND_ROBIN:
        runner_up, bronze = _resolve_rr_podium(tournament)
    elif gf == GameFormat.HIGHSCORE:
        runner_up, bronze = _resolve_hs_podium(tournament)
    else:
        runner_up, bronze = None, None

    return {
        'runner_up': runner_up,
        'bronze': bronze,
    }


# -- Private helpers for podium resolution -----------------------------------


def _resolve_bracket_podium(
    tournament: Tournament,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from SE or DE bracket matches."""
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    if not matches:
        return None, None

    em = tournament.elimination_mode
    runner_up_name: str | None = None
    bronze_name: str | None = None

    if em == EliminationMode.DOUBLE_ELIMINATION:
        # 2nd place: loser of the LAST Grand Final match (highest round
        # handles bracket-reset scenario).
        gf_matches = [m for m in matches if m.bracket == Bracket.GRAND_FINAL]
        if gf_matches:
            last_gf = max(gf_matches, key=lambda m: (m.round or 0))
            loser_id = _get_match_loser_contestant_id(last_gf)
            if loser_id is not None:
                runner_up_name = _resolve_contestant_name(tournament, loser_id)

        # 3rd place: loser of the LB Final (highest-round LB match).
        lb_matches = [m for m in matches if m.bracket == Bracket.LOSERS]
        if lb_matches:
            lb_final = max(lb_matches, key=lambda m: (m.round or 0))
            loser_id = _get_match_loser_contestant_id(lb_final)
            if loser_id is not None:
                bronze_name = _resolve_contestant_name(tournament, loser_id)

    else:
        # Single Elimination
        # 2nd place: loser of the final (highest-round WB/None match).
        wb_matches = [
            m for m in matches
            if m.bracket in (None, Bracket.WINNERS)
        ]
        if wb_matches:
            final = max(wb_matches, key=lambda m: (m.round or 0))
            loser_id = _get_match_loser_contestant_id(final)
            if loser_id is not None:
                runner_up_name = _resolve_contestant_name(tournament, loser_id)

        # 3rd place: winner of the P3 match.
        p3_matches = [m for m in matches if m.bracket == Bracket.THIRD_PLACE]
        if p3_matches:
            p3_match = p3_matches[0]
            winner_id = _get_match_winner_contestant_id(p3_match)
            if winner_id is not None:
                bronze_name = _resolve_contestant_name(tournament, winner_id)

    return runner_up_name, bronze_name


def _resolve_rr_podium(
    tournament: Tournament,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from round-robin standings."""
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    contestants_by_match = tournament_match_service.get_contestants_for_tournament(
        tournament.id
    )

    # Build confirmed match pairs for standings computation.
    confirmed_pairs: list[list] = []
    for match in matches:
        if match.confirmed_by is None:
            continue
        pair = contestants_by_match.get(match.id, [])
        if len(pair) == 2:
            confirmed_pairs.append(pair)

    standings = tournament_domain_service.compute_round_robin_standings(
        confirmed_pairs
    )

    runner_up: str | None = None
    bronze: str | None = None

    if len(standings) > 1:
        runner_up = _resolve_contestant_name(
            tournament, standings[1].contestant_id
        )
    if len(standings) > 2:
        bronze = _resolve_contestant_name(
            tournament, standings[2].contestant_id
        )

    return runner_up, bronze


def _resolve_hs_podium(
    tournament: Tournament,
) -> tuple[str | None, str | None]:
    """Derive 2nd and 3rd place from highscore leaderboard."""
    result = tournament_score_service.get_leaderboard(tournament.id)
    if result.is_err():
        return None, None

    leaderboard = result.unwrap()

    runner_up: str | None = None
    bronze: str | None = None

    if len(leaderboard) > 1:
        runner_up = _resolve_score_entry_name(tournament, leaderboard[1])
    if len(leaderboard) > 2:
        bronze = _resolve_score_entry_name(tournament, leaderboard[2])

    return runner_up, bronze


def _get_match_loser_contestant_id(
    match: 'TournamentMatch',
) -> str | None:
    """Return the loser's contestant ID for a confirmed match.

    Uses score comparison: the contestant with the lower score is the
    loser.  Returns ``None`` if the match has no contestants, is
    unconfirmed, or is a draw.
    """
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    if len(contestants) < 2:
        return None

    # Both must have scores.
    if any(c.score is None for c in contestants):
        return None

    sorted_by_score = sorted(contestants, key=lambda c: c.score, reverse=True)
    # Draw — no loser.
    if sorted_by_score[0].score == sorted_by_score[1].score:
        return None

    loser = sorted_by_score[-1]
    if loser.participant_id is None and loser.team_id is None:
        return None
    return str(loser.participant_id if loser.participant_id is not None else loser.team_id)


def _get_match_winner_contestant_id(
    match: 'TournamentMatch',
) -> str | None:
    """Return the winner's contestant ID for a confirmed match.

    Returns ``None`` if the match has no contestants, is unconfirmed,
    or is a draw.
    """
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    if len(contestants) < 2:
        return None

    if any(c.score is None for c in contestants):
        return None

    sorted_by_score = sorted(contestants, key=lambda c: c.score, reverse=True)
    if sorted_by_score[0].score == sorted_by_score[1].score:
        return None

    winner = sorted_by_score[0]
    if winner.participant_id is None and winner.team_id is None:
        return None
    return str(winner.participant_id if winner.participant_id is not None else winner.team_id)


def _resolve_contestant_name(
    tournament: Tournament,
    contestant_id: str,
) -> str | None:
    """Resolve a contestant UUID string to a display name.

    Works for both team-based and participant-based tournaments.
    """
    from uuid import UUID

    if tournament.contestant_type == ContestantType.TEAM:
        from .models.tournament_team import TournamentTeamID

        team = tournament_team_service.find_team(
            TournamentTeamID(UUID(contestant_id))
        )
        return team.name if team is not None else None
    else:
        from byceps.services.user import user_service
        from .models.tournament_participant import TournamentParticipantID

        participant = tournament_participant_service.find_participant(
            TournamentParticipantID(UUID(contestant_id))
        )
        if participant is None:
            return None
        user = user_service.get_user(participant.user_id)
        return user.screen_name


def _resolve_score_entry_name(
    tournament: Tournament,
    entry: 'ScoreSubmission',
) -> str | None:
    """Resolve a highscore leaderboard entry to a display name."""
    contestant_id = entry.team_id if entry.team_id is not None else entry.participant_id
    if contestant_id is None:
        return None
    return _resolve_contestant_name(tournament, str(contestant_id))
