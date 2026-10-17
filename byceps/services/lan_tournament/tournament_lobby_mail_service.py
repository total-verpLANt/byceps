"""
byceps.services.lan_tournament.tournament_lobby_mail_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Announce free-for-all lobbies once, while the tournament is ongoing.
"""

import structlog

from . import (
    tournament_notification_service,
    tournament_repository as repository,
)
from .models.game_format import GameFormat
from .models.tournament import Tournament, TournamentID
from .models.tournament_match import TournamentMatchID
from .models.tournament_match_to_contestant import TournamentMatchToContestant
from .models.tournament_status import TournamentStatus
from .tournament_domain_service import game_format_for_phase
from .tournament_log_service import create_log_entry


log = structlog.get_logger()

LOBBY_ANNOUNCED_EVENT = 'match-lobby-announced'


def announce_ffa_lobbies(tournament_id: TournamentID) -> None:
    """Mail every not yet announced FFA lobby once.

    Call only after the commit that made the lobbies current. The audit log
    is the ledger: a lobby is recorded and committed before it is mailed, so
    a repeat call finds nothing to do. Never raises.
    """
    try:
        repository.lock_tournament_for_update(tournament_id)
        tournament = repository.get_tournament(tournament_id, fresh=True)
        if tournament.tournament_status is not TournamentStatus.ONGOING:
            repository.commit_session()
            return
        announced = _record_unannounced_lobbies_flush(tournament)
        repository.commit_session()
    except Exception:
        repository.rollback_session()
        log.exception(
            'FFA lobby announcement failed',
            tournament_id=str(tournament_id),
        )
        return

    for match_id in announced:
        tournament_notification_service.send_ffa_lobby_emails(
            tournament_id, match_id
        )


def _record_unannounced_lobbies_flush(
    tournament: Tournament,
) -> list[TournamentMatchID]:
    """Stage an audit entry for each lobby not announced yet.

    Return the lobbies' IDs; the caller owns the commit.
    """
    announced = []
    for match in repository.get_matches_for_tournament_ordered_fresh(
        tournament.id
    ):
        if (
            match.confirmed_by is not None
            or game_format_for_phase(tournament, match.phase)
            is not GameFormat.FREE_FOR_ALL
        ):
            continue
        contestants = repository.get_contestants_for_match(match.id)
        if (
            len(contestants) < 2
            or _has_result(contestants)
            or repository.count_log_entries_with_data(
                tournament.id,
                LOBBY_ANNOUNCED_EVENT,
                {'match_id': str(match.id)},
            )
            > 0
        ):
            continue
        create_log_entry(
            LOBBY_ANNOUNCED_EVENT,
            tournament.id,
            None,
            data={
                'match_id': str(match.id),
                'phase': match.phase,
                'round': match.round,
            },
            commit=False,
        )
        announced.append(match.id)
    return announced


def _has_result(contestants: list[TournamentMatchToContestant]) -> bool:
    """Return whether a result was entered, so the lobby is already playing."""
    return any(
        contestant.placement is not None
        or contestant.points is not None
        or contestant.score is not None
        for contestant in contestants
    )
