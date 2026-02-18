from datetime import datetime

from byceps.services.lan_tournament import tournament_stats_service
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


def _create_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('test-party'),
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': None,
        'tournament_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def test_stats_with_mixed_statuses():
    tournaments = [
        _create_tournament(
            tournament_status=TournamentStatus.DRAFT,
        ),
        _create_tournament(
            tournament_status=TournamentStatus.DRAFT,
        ),
        _create_tournament(
            tournament_status=TournamentStatus.REGISTRATION_OPEN,
        ),
        _create_tournament(
            tournament_status=TournamentStatus.ONGOING,
        ),
        _create_tournament(
            tournament_status=TournamentStatus.COMPLETED,
        ),
        _create_tournament(
            tournament_status=TournamentStatus.COMPLETED,
        ),
    ]
    participant_counts = {t.id: 5 for t in tournaments}

    stats = tournament_stats_service.get_stats_for_party(
        tournaments, participant_counts
    )

    assert stats.tournament_count == 6
    assert stats.total_participant_count == 30
    assert stats.draft_count == 2
    assert stats.registration_open_count == 1
    assert stats.ongoing_count == 1
    assert stats.completed_count == 2


def test_stats_with_no_tournaments():
    stats = tournament_stats_service.get_stats_for_party([], {})

    assert stats.tournament_count == 0
    assert stats.total_participant_count == 0
    assert stats.draft_count == 0
    assert stats.registration_open_count == 0
    assert stats.ongoing_count == 0
    assert stats.completed_count == 0


def test_stats_with_single_tournament_zero_participants():
    tournament = _create_tournament(
        tournament_status=TournamentStatus.ONGOING,
    )

    stats = tournament_stats_service.get_stats_for_party(
        [tournament], {}
    )

    assert stats.tournament_count == 1
    assert stats.total_participant_count == 0
    assert stats.ongoing_count == 1
    assert stats.draft_count == 0
    assert stats.registration_open_count == 0
    assert stats.completed_count == 0
