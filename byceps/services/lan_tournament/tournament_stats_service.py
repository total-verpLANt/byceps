from dataclasses import dataclass

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


@dataclass(frozen=True)
class TournamentStatsForParty:
    tournament_count: int
    total_participant_count: int
    ongoing_count: int
    registration_open_count: int
    draft_count: int
    completed_count: int


def get_stats_for_party(
    tournaments: list[Tournament],
    participant_counts: dict[TournamentID, int],
) -> TournamentStatsForParty:
    """Compute aggregate tournament statistics from pre-fetched data."""
    total_participant_count = sum(participant_counts.values())

    def _count_status(status: TournamentStatus) -> int:
        return sum(1 for t in tournaments if t.tournament_status == status)

    return TournamentStatsForParty(
        tournament_count=len(tournaments),
        total_participant_count=total_participant_count,
        ongoing_count=_count_status(TournamentStatus.ONGOING),
        registration_open_count=_count_status(
            TournamentStatus.REGISTRATION_OPEN
        ),
        draft_count=_count_status(TournamentStatus.DRAFT),
        completed_count=_count_status(TournamentStatus.COMPLETED),
    )
