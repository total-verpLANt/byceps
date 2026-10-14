from types import SimpleNamespace
from unittest.mock import patch

from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_metrics_service import (
    collect_metrics,
)
from byceps.services.metrics.models import Label, Metric
from byceps.services.party.models import PartyID


_M = 'byceps.services.lan_tournament.tournament_metrics_service'

PARTY_ID = PartyID('lan-2026')
OTHER_PARTY_ID = PartyID('lan-2027')


def _party(party_id):
    return SimpleNamespace(id=party_id)


def _collect(
    parties, *, tournaments=None, matches=None, participants=None, teams=None
):
    with (
        patch(f'{_M}.party_service') as mock_party_service,
        patch(f'{_M}.tournament_repository') as mock_repo,
    ):
        mock_party_service.get_active_parties.return_value = parties
        mock_repo.count_tournaments_by_party_and_status.return_value = (
            tournaments or {}
        )
        mock_repo.count_matches_by_party_and_confirmation.return_value = (
            matches or {}
        )
        mock_repo.count_active_participants_by_party.return_value = (
            participants or {}
        )
        mock_repo.count_active_teams_by_party.return_value = teams or {}

        metrics = list(collect_metrics())

    return metrics, mock_repo


def _named(metrics, name):
    return [metric for metric in metrics if metric.name == name]


def _status_label(metric):
    return next(
        label.value for label in metric.labels if label.name == 'status'
    )


def test_collect_metrics_yields_nothing_without_active_parties():
    metrics, mock_repo = _collect([])

    assert metrics == []
    assert mock_repo.mock_calls == []


def test_collect_metrics_zero_fills_every_status():
    metrics, _ = _collect([_party(PARTY_ID)])

    tournament_metrics = _named(metrics, 'lan_tournament_tournaments_count')

    assert len(tournament_metrics) == 7
    assert [metric.value for metric in tournament_metrics] == [0] * 7
    assert [_status_label(metric) for metric in tournament_metrics] == [
        status.name for status in TournamentStatus
    ]


def test_collect_metrics_counts_null_and_unknown_status_as_unknown():
    metrics, _ = _collect(
        [_party(PARTY_ID)],
        tournaments={(PARTY_ID, None): 2, (PARTY_ID, 'LEGACY'): 1},
    )

    unknown_metrics = [
        metric
        for metric in _named(metrics, 'lan_tournament_tournaments_count')
        if _status_label(metric) == 'UNKNOWN'
    ]

    assert unknown_metrics == [
        Metric(
            'lan_tournament_tournaments_count',
            3,
            labels=[Label('party', PARTY_ID), Label('status', 'UNKNOWN')],
        )
    ]


def test_collect_metrics_omits_unknown_when_zero():
    metrics, _ = _collect(
        [_party(PARTY_ID)],
        tournaments={(PARTY_ID, 'DRAFT'): 1, (PARTY_ID, 'ONGOING'): 2},
    )

    tournament_metrics = _named(metrics, 'lan_tournament_tournaments_count')
    values_by_status = {
        _status_label(metric): metric.value for metric in tournament_metrics
    }

    assert 'UNKNOWN' not in values_by_status
    assert values_by_status['DRAFT'] == 1
    assert values_by_status['ONGOING'] == 2
    assert len(tournament_metrics) == 7


def test_collect_metrics_emits_match_participant_team_counts():
    metrics, mock_repo = _collect(
        [_party(PARTY_ID), _party(OTHER_PARTY_ID)],
        matches={PARTY_ID: (5, 3)},
        participants={PARTY_ID: 12},
        teams={PARTY_ID: 4},
    )

    def per_party(name):
        return {
            metric.labels[0].value: metric.value
            for metric in _named(metrics, name)
        }

    assert per_party('lan_tournament_matches_confirmed_count') == {
        PARTY_ID: 5,
        OTHER_PARTY_ID: 0,
    }
    assert per_party('lan_tournament_matches_unconfirmed_count') == {
        PARTY_ID: 3,
        OTHER_PARTY_ID: 0,
    }
    assert per_party('lan_tournament_participants_count') == {
        PARTY_ID: 12,
        OTHER_PARTY_ID: 0,
    }
    assert per_party('lan_tournament_teams_count') == {
        PARTY_ID: 4,
        OTHER_PARTY_ID: 0,
    }

    party_ids = [PARTY_ID, OTHER_PARTY_ID]
    mock_repo.count_tournaments_by_party_and_status.assert_called_once_with(
        party_ids
    )
    mock_repo.count_matches_by_party_and_confirmation.assert_called_once_with(
        party_ids
    )
    mock_repo.count_active_participants_by_party.assert_called_once_with(
        party_ids
    )
    mock_repo.count_active_teams_by_party.assert_called_once_with(party_ids)
