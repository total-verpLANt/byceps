"""
byceps.services.lan_tournament.tournament_metrics_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Iterator

from byceps.services.metrics.models import Label, Metric
from byceps.services.party import party_service
from byceps.services.party.models import PartyID

from . import tournament_repository
from .models.tournament_status import TournamentStatus


_UNKNOWN_STATUS = 'UNKNOWN'


def collect_metrics() -> Iterator[Metric]:
    """Yield aggregate tournament metrics for active parties."""
    party_ids = [party.id for party in party_service.get_active_parties()]
    if not party_ids:
        return

    tournament_counts = (
        tournament_repository.count_tournaments_by_party_and_status(party_ids)
    )
    match_counts = (
        tournament_repository.count_matches_by_party_and_confirmation(party_ids)
    )
    participant_counts = (
        tournament_repository.count_active_participants_by_party(party_ids)
    )
    team_counts = tournament_repository.count_active_teams_by_party(party_ids)

    for party_id in party_ids:
        labels = [Label('party', party_id)]

        for status in TournamentStatus:
            yield Metric(
                'lan_tournament_tournaments_count',
                tournament_counts.get((party_id, status.name), 0),
                labels=[*labels, Label('status', status.name)],
            )

        unknown_count = _count_unknown_status(tournament_counts, party_id)
        if unknown_count > 0:
            yield Metric(
                'lan_tournament_tournaments_count',
                unknown_count,
                labels=[*labels, Label('status', _UNKNOWN_STATUS)],
            )

        confirmed_count, unconfirmed_count = match_counts.get(party_id, (0, 0))
        yield Metric(
            'lan_tournament_matches_confirmed_count',
            confirmed_count,
            labels=labels,
        )
        yield Metric(
            'lan_tournament_matches_unconfirmed_count',
            unconfirmed_count,
            labels=labels,
        )

        yield Metric(
            'lan_tournament_participants_count',
            participant_counts.get(party_id, 0),
            labels=labels,
        )
        yield Metric(
            'lan_tournament_teams_count',
            team_counts.get(party_id, 0),
            labels=labels,
        )


def _count_unknown_status(
    tournament_counts: dict[tuple[PartyID, str | None], int], party_id: PartyID
) -> int:
    """Return the number of tournaments without a known status."""
    return sum(
        count
        for (counted_party_id, status), count in tournament_counts.items()
        if counted_party_id == party_id
        and (status is None or status not in TournamentStatus.__members__)
    )
