from datetime import datetime, UTC

from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok, Result
from byceps.util.uuid import generate_uuid7

from .events import (
    TournamentCreatedEvent,
    TournamentStatusChangedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.tournament_mode import TournamentMode
from .models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from .models.tournament_status import TournamentStatus


_VALID_STATUS_TRANSITIONS: dict[TournamentStatus, set[TournamentStatus]] = {
    TournamentStatus.DRAFT: {
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.REGISTRATION_OPEN: {
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.REGISTRATION_CLOSED: {
        TournamentStatus.ONGOING,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.ONGOING: {
        TournamentStatus.PAUSED,
        TournamentStatus.COMPLETED,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.PAUSED: {
        TournamentStatus.ONGOING,
        TournamentStatus.CANCELLED,
    },
    TournamentStatus.COMPLETED: set(),
    TournamentStatus.CANCELLED: set(),
}


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
    tournament_mode: TournamentMode | None = None,
) -> tuple[Tournament, TournamentCreatedEvent]:
    """Create a new tournament."""
    tournament_id = TournamentID(generate_uuid7())
    now = datetime.now(UTC)

    tournament = Tournament(
        id=tournament_id,
        party_id=party_id,
        name=name,
        game=game,
        description=description,
        image_url=image_url,
        ruleset=ruleset,
        start_time=start_time,
        created_at=now,
        min_players=min_players,
        max_players=max_players,
        min_teams=min_teams,
        max_teams=max_teams,
        min_players_in_team=min_players_in_team,
        max_players_in_team=max_players_in_team,
        contestant_type=contestant_type,
        tournament_status=tournament_status,
        tournament_mode=tournament_mode,
    )

    event = TournamentCreatedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament_id,
    )

    return tournament, event


def validate_status_transition(
    current_status: TournamentStatus | None,
    new_status: TournamentStatus,
) -> Result[TournamentStatus, str]:
    """Validate that a status transition is allowed."""
    if current_status is None:
        return Ok(new_status)

    allowed = _VALID_STATUS_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        return Err(
            f'Cannot transition from {current_status.name}'
            f' to {new_status.name}.'
        )

    return Ok(new_status)


def change_tournament_status(
    tournament: Tournament,
    new_status: TournamentStatus,
) -> Result[tuple[TournamentStatusChangedEvent], str]:
    """Validate and produce a status change event."""
    result = validate_status_transition(
        tournament.tournament_status, new_status
    )
    if result.is_err():
        return Err(result.unwrap_err())

    now = datetime.now(UTC)

    event = TournamentStatusChangedEvent(
        occurred_at=now,
        initiator=None,
        tournament_id=tournament.id,
        old_status=tournament.tournament_status,
        new_status=new_status,
    )

    return Ok((event,))


def validate_participant_count(
    tournament: Tournament,
    current_count: int,
) -> Result[None, str]:
    """Check if the tournament can accept more participants."""
    if (
        tournament.max_players is not None
        and current_count >= tournament.max_players
    ):
        return Err('Tournament is full.')

    return Ok(None)


def validate_team_count(
    tournament: Tournament,
    current_count: int,
) -> Result[None, str]:
    """Check if the tournament can accept more teams."""
    if (
        tournament.max_teams is not None
        and current_count >= tournament.max_teams
    ):
        return Err('Maximum number of teams reached.')

    return Ok(None)


def determine_match_winner(
    contestants: list[TournamentMatchToContestant],
) -> Result[TournamentMatchToContestant, str]:
    """Determine the winner of a match by highest score."""
    if len(contestants) < 2:
        return Err('Need at least 2 contestants to determine winner.')

    for contestant in contestants:
        if contestant.score is None:
            return Err('All contestants must have scores.')

    sorted_contestants = sorted(
        contestants, key=lambda c: c.score, reverse=True
    )

    if sorted_contestants[0].score == sorted_contestants[1].score:
        return Err('Match is tied; cannot determine winner.')

    return Ok(sorted_contestants[0])


def _standard_seed_order(bracket_size: int) -> list[int]:
    """Return standard seeding order for single elimination.

    For bracket_size=8: [0, 7, 3, 4, 1, 6, 2, 5]
    This produces matchups: 1v8, 4v5, 2v7, 3v6 (1-indexed).
    """
    if bracket_size == 1:
        return [0]
    if bracket_size == 2:
        return [0, 1]

    half = bracket_size // 2
    prev = _standard_seed_order(half)
    result = []
    for seed in prev:
        result.append(seed)
        result.append(bracket_size - 1 - seed)
    return result
