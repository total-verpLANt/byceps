"""
byceps.services.lan_tournament.tournament_restart_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Mapping
import dataclasses
from dataclasses import dataclass
from datetime import datetime, UTC

from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result

from . import (
    signals,
    tournament_domain_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_repository,
    tournament_repository,
    tournament_seeding_repository,
    tournament_service,
    tournament_team_service,
)
from .events import (
    MatchDeletedEvent,
    ParticipantJoinedEvent,
    TeamCreatedEvent,
    TournamentStatusChangedEvent,
)
from .models.contestant_type import ContestantType
from .models.tournament import Tournament, TournamentID
from .models.tournament_status import TournamentStatus
from .models.tournament_team import TournamentTeam
from .tournament_log_service import create_log_entry
from .tournament_qualification_service import validate_reason
from .tournament_seeding_service import INITIAL_TARGET


RESTART_REASON_MISSING_ERROR = 'Please give a reason for the restart.'
RESTART_NOT_CONFIRMED_ERROR = 'Confirm that the tournament will be restarted.'
RESTART_NOT_RUNNING_ERROR = (
    'Only a running or paused tournament can be restarted.'
)
RESTART_NOT_TEAM_ERROR = (
    'Only a team tournament can be restarted for a new team.'
)

RESTARTABLE_STATUSES = frozenset(
    {TournamentStatus.ONGOING, TournamentStatus.PAUSED}
)


@dataclass(frozen=True, kw_only=True)
class RestartImpact:
    """What a restart deletes."""

    match_count: int
    confirmed_result_count: int
    playoffs_released: bool


@dataclass(frozen=True, kw_only=True)
class RestartOutcome:
    """What a flushed restart leaves for its owner to dispatch."""

    impact: RestartImpact
    old_tournament: Tournament
    status_event: TournamentStatusChangedEvent
    deleted_events: list[MatchDeletedEvent]


def _is_team_tournament(tournament: Tournament) -> bool:
    return (
        tournament_domain_service.derive_contestant_type(
            tournament.contestant_type,
            tournament.max_players_in_team,
            tournament.min_players_in_team,
        )
        is ContestantType.TEAM
    )


def is_restart_required(tournament: Tournament) -> bool:
    """Tell whether a new team in that tournament needs a restart."""
    return (
        _is_team_tournament(tournament)
        and tournament.tournament_status in RESTARTABLE_STATUSES
    )


def get_restart_impact(tournament_id: TournamentID) -> RestartImpact:
    """Return what a restart of that tournament would delete.

    A confirmed match counts as a result only with two contestants, so a
    confirmed bye does not.
    """
    tournament = tournament_repository.get_tournament(tournament_id, fresh=True)
    matches = tournament_repository.get_matches_for_tournament(tournament_id)
    confirmed_ids = [m.id for m in matches if m.confirmed_by is not None]
    contestants = tournament_repository.get_contestants_for_matches(
        confirmed_ids
    )
    return RestartImpact(
        match_count=len(matches),
        confirmed_result_count=sum(
            1
            for match_id in confirmed_ids
            if len(contestants.get(match_id, ())) >= 2
        ),
        playoffs_released=tournament.playoff_released_at is not None,
    )


def restart_tournament_flush(
    tournament: Tournament,
    *,
    reason: str,
    initiator_id: UserID,
    log_context: Mapping[str, object] | None = None,
) -> Result[RestartOutcome, str]:
    """Take a running tournament back to `REGISTRATION_CLOSED` (flush only).

    Delete every match, the playoff release, the leaderboard close and the
    decisions, and every seeding draft but the initial one. The caller
    holds the tournament
    lock, passes the tournament it read under it, rolls back on `Err` and
    owns the commit. Call `dispatch_restart_effects` after the commit.
    """
    match validate_reason(reason, missing=RESTART_REASON_MISSING_ERROR):
        case Err(e):
            return Err(e)
        case Ok(clean_reason):
            pass

    if tournament.tournament_status not in RESTARTABLE_STATUSES:
        return Err(RESTART_NOT_RUNNING_ERROR)
    if not _is_team_tournament(tournament):
        return Err(RESTART_NOT_TEAM_ERROR)

    impact = get_restart_impact(tournament.id)

    changed_at = tournament_match_service._operation_time_of(tournament)
    match_ids = [
        m.id
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
    ]
    tournament_repository.lock_matches_for_update(match_ids)
    if changed_at is not None and match_ids:
        tournament_repository.retire_dashboard_matches_flush(
            match_ids, occurred_at=changed_at
        )

    deleted_events = tournament_match_service.clear_bracket(
        tournament.id,
        phase=None,
        initiator_id=initiator_id,
        **tournament_match_service._changed(changed_at),
    )
    tournament_repository.clear_playoff_release(
        tournament.id, suspend_auto=False
    )
    leaderboard_was_closed = tournament.leaderboard_closed_at is not None
    tournament_repository.set_leaderboard_closed(tournament.id, None)
    tournament_qualification_repository.delete_decisions_for_tournament(
        tournament.id
    )
    tournament_seeding_repository.delete_seedings_for_tournament_except(
        tournament.id, keep_target=INITIAL_TARGET
    )

    event = TournamentStatusChangedEvent(
        occurred_at=datetime.now(UTC),
        initiator=None,
        tournament_id=tournament.id,
        old_status=tournament.tournament_status,
        new_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    updated = dataclasses.replace(
        tournament, tournament_status=TournamentStatus.REGISTRATION_CLOSED
    )
    persisted = tournament_service._persist_status_change_flush(
        tournament, updated, event, initiator_id
    )
    if persisted.is_err():
        return Err(persisted.unwrap_err())

    create_log_entry(
        'tournament-restarted',
        tournament.id,
        initiator_id,
        data={
            'reason': clean_reason,
            'old_status': tournament.tournament_status.name,
            'match_count': impact.match_count,
            'confirmed_result_count': impact.confirmed_result_count,
            'playoffs_were_released': impact.playoffs_released,
            'leaderboard_was_closed': leaderboard_was_closed,
            **(log_context or {}),
        },
        commit=False,
    )

    timing = tournament_match_service._reconcile_timing_flush(
        tournament.id, changed_at
    )
    if timing.is_err():
        return Err(timing.unwrap_err())

    return Ok(
        RestartOutcome(
            impact=impact,
            old_tournament=tournament,
            status_event=event,
            deleted_events=deleted_events,
        )
    )


def dispatch_restart_effects(
    outcome: RestartOutcome, initiator_id: UserID
) -> None:
    """Send the signals of a committed restart."""
    for deleted_event in outcome.deleted_events:
        signals.match_deleted.send(None, event=deleted_event)
    tournament_service._dispatch_status_change_effects(
        outcome.old_tournament, outcome.status_event, initiator_id
    )


@tournament_team_service._rollback_roster_on_failure
def create_team_with_restart(
    tournament_id: TournamentID,
    name: str,
    captain_user_id: UserID,
    *,
    reason: str,
    confirm_restart: bool,
    initiator_id: UserID,
    tag: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    join_code: str | None = None,
) -> Result[tuple[TournamentTeam, TeamCreatedEvent], str]:
    """Create a team in a running tournament by restarting it.

    Every check that can refuse runs before the first deletion. The
    restart, the captain's registration and the team commit together, and
    the signals follow the commit.
    """
    if not confirm_restart:
        return Err(RESTART_NOT_CONFIRMED_ERROR)
    match validate_reason(reason, missing=RESTART_REASON_MISSING_ERROR):
        case Err(e):
            return Err(e)
        case Ok(clean_reason):
            pass

    tournament = tournament_repository.get_tournament_for_update(tournament_id)
    if tournament.tournament_status not in RESTARTABLE_STATUSES:
        return Err(RESTART_NOT_RUNNING_ERROR)
    if not _is_team_tournament(tournament):
        return Err(RESTART_NOT_TEAM_ERROR)

    validated = tournament_team_service._validate_new_team(
        tournament, name, tag, image_url, captain_user_id
    )
    if validated.is_err():
        return Err(validated.unwrap_err())
    tag, captain = validated.unwrap()

    if captain is None:
        count_result = tournament_domain_service.validate_participant_count(
            tournament,
            tournament_repository.get_participant_count(tournament_id),
        )
        if count_result.is_err():
            return Err(count_result.unwrap_err())

    restarted = restart_tournament_flush(
        tournament,
        reason=clean_reason,
        initiator_id=initiator_id,
        log_context={
            'team_name': name,
            'captain_user_id': str(captain_user_id),
        },
    )
    if restarted.is_err():
        return Err(restarted.unwrap_err())
    outcome = restarted.unwrap()

    newly_registered = captain is None
    if captain is None:
        captain = (
            tournament_participant_service._create_or_reactivate_participant(
                tournament_id,
                captain_user_id,
                substitute_player=False,
                team_id=None,
            )
        )

    created = tournament_team_service._create_team_flush(
        tournament_id,
        name,
        captain,
        tag=tag,
        description=description,
        image_url=image_url,
        join_code=join_code,
    )
    if created.is_err():
        return Err(created.unwrap_err())
    team = created.unwrap()

    tournament_repository.commit_session()

    dispatch_restart_effects(outcome, initiator_id)
    if newly_registered:
        signals.participant_joined.send(
            None,
            event=ParticipantJoinedEvent(
                occurred_at=captain.created_at,
                initiator=None,
                tournament_id=tournament_id,
                participant_id=captain.id,
            ),
        )
    event = TeamCreatedEvent(
        occurred_at=team.created_at,
        initiator=None,
        tournament_id=tournament_id,
        team_id=team.id,
    )
    signals.team_created.send(None, event=event)

    return Ok((team, event))
