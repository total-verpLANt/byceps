"""
byceps.services.lan_tournament.blueprints.site.homepage_cards
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from flask import g

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)


@dataclass(frozen=True, kw_only=True)
class HomepageTournamentCard:
    tournament_id: TournamentID
    name: str
    game: str | None
    status: TournamentStatus
    is_team_based: bool
    filled: int
    capacity: int | None
    starts_at: datetime | None


# Statuses missing here (draft, cancelled) never reach a visitor.
_STATUS_ORDER = {
    TournamentStatus.ONGOING: 0,
    TournamentStatus.PAUSED: 0,
    TournamentStatus.REGISTRATION_OPEN: 1,
    TournamentStatus.REGISTRATION_CLOSED: 2,
    TournamentStatus.COMPLETED: 3,
}


def build_homepage_cards(
    tournaments: Sequence[Tournament],
    participant_counts: Mapping[TournamentID, int],
    team_counts: Mapping[TournamentID, int],
    *,
    limit: int = 4,
) -> list[HomepageTournamentCard]:
    """Return up to `limit` visible cards, live first, then by position."""
    visible = [
        (t, t.tournament_status)
        for t in tournaments
        if t.tournament_status is not None
        and t.tournament_status in _STATUS_ORDER
    ]
    visible.sort(key=lambda entry: (_STATUS_ORDER[entry[1]], entry[0].position))

    return [
        _to_card(t, status, participant_counts, team_counts)
        for t, status in visible[:limit]
    ]


def _to_card(
    tournament: Tournament,
    status: TournamentStatus,
    participant_counts: Mapping[TournamentID, int],
    team_counts: Mapping[TournamentID, int],
) -> HomepageTournamentCard:
    is_team_based = (
        tournament_domain_service.derive_contestant_type(
            tournament.contestant_type,
            tournament.max_players_in_team,
            tournament.min_players_in_team,
        )
        is ContestantType.TEAM
    )

    if is_team_based:
        filled = team_counts.get(tournament.id, 0)
        capacity = tournament.max_teams
    else:
        filled = participant_counts.get(tournament.id, 0)
        capacity = tournament.max_players

    return HomepageTournamentCard(
        tournament_id=tournament.id,
        name=tournament.name,
        game=tournament.game,
        status=status,
        is_team_based=is_team_based,
        filled=filled,
        capacity=capacity,
        starts_at=tournament.start_time,
    )


def get_homepage_tournament_cards(
    limit: int = 4,
) -> list[HomepageTournamentCard]:
    """Return cards for `g.party`, or an empty list without a party."""
    party = getattr(g, 'party', None)
    if party is None:
        return []

    tournaments = tournament_service.get_tournaments_for_party(party.id)
    tournament_ids = [t.id for t in tournaments]
    participant_counts = (
        tournament_service.get_participant_counts_for_tournaments(
            tournament_ids
        )
    )
    team_counts = tournament_team_service.get_team_counts_for_tournaments(
        tournament_ids
    )

    return build_homepage_cards(
        tournaments, participant_counts, team_counts, limit=limit
    )
