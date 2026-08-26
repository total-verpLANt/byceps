"""Pure/mocked surface policy; SQL evidence lives in the integration file."""

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from byceps.services.lan_tournament import lan_tournament_view_helpers as helpers
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity, MatchPairing, READINESS_FILTER_BUCKETS, ReadinessDisplayStatus,
    derive_match_readiness,
)
from byceps.services.lan_tournament.models.tournament import Tournament
from byceps.services.lan_tournament.models.tournament_match import MatchSide, TournamentMatch
from byceps.services.lan_tournament.models.tournament_match_to_contestant import TournamentMatchToContestant
from byceps.services.lan_tournament.models.tournament_status import TournamentStatus


NOW = datetime(2026, 10, 5, 12)


def _tournament(**changes):
    defaults = dict(
        id=uuid4(), party_id=uuid4(), name='Projection', game=None,
        description=None, image_url=None, ruleset=None, start_time=None,
        created_at=NOW, min_players=None, max_players=None, min_teams=None,
        max_teams=None, min_players_in_team=None, max_players_in_team=None,
        contestant_type=None, tournament_status=TournamentStatus.ONGOING,
        game_format=GameFormat.ONE_V_ONE, elimination_mode=None,
    )
    defaults.update(changes)
    return Tournament(**defaults)


def _row(tournament, *, count=2, phase=1, claims=0):
    match = TournamentMatch(
        id=uuid4(), tournament_id=tournament.id, group_order=None,
        match_order=1, round=1, next_match_id=None, confirmed_by=None,
        created_at=NOW, phase=phase, occupied_since=NOW - timedelta(days=1),
        pairing_id=uuid4(), pairing_generation=4, readiness_revision=7,
        ready_at_a=NOW if claims >= 1 else None,
        ready_at_b=NOW if claims == 2 else None,
        ready_by_a=uuid4() if claims >= 1 else None,
        ready_by_b=uuid4() if claims == 2 else None,
    )
    contestants = [TournamentMatchToContestant(
        id=uuid4(), tournament_match_id=match.id, team_id=None,
        participant_id=uuid4(), score=None, created_at=NOW,
    ) for _ in range(count)]
    pairing = None
    if count == 2:
        pairing = MatchPairing(
            id=match.pairing_id, match_id=match.id, tournament_id=tournament.id,
            generation=4, started_at=NOW,
            side_a=ContestantIdentity(kind='participant', id=contestants[0].participant_id),
            side_b=ContestantIdentity(kind='participant', id=contestants[1].participant_id),
        )
    return match, contestants, pairing


def _project(tournament, rows):
    pairs = {m.id: p for m, _, p in rows if p is not None}
    with patch.object(helpers.tournament_repository, 'get_match_pairings_for_matches', return_value=pairs):
        return helpers.build_match_readiness_projections(
            tournament, [m for m, _, _ in rows], {m.id: c for m, c, _ in rows},
        )


# fmt: off
@pytest.mark.parametrize('base,playoff,phase,expected', [
    (GameFormat.ONE_V_ONE, None, 1, True),
    (GameFormat.ONE_V_ONE, GameFormat.ONE_V_ONE, 2, True),
    (GameFormat.HIGHSCORE, GameFormat.ONE_V_ONE, 2, True),
    (GameFormat.FREE_FOR_ALL, GameFormat.ONE_V_ONE, 2, True),
    (GameFormat.HIGHSCORE, GameFormat.FREE_FOR_ALL, 2, False),
    (GameFormat.FREE_FOR_ALL, None, 1, False),
    (GameFormat.HIGHSCORE, None, 1, False),
    (GameFormat.ONE_V_ONE, None, 2, False),
    (GameFormat.ONE_V_ONE, None, 3, False),
])
# fmt: on
def test_effective_playoff_format_controls_capability(base, playoff, phase, expected):
    tournament = _tournament(game_format=base, playoff_game_format=playoff)
    match, contestants, pairing = _row(tournament, phase=phase, claims=2)
    projection = _project(tournament, [(match, contestants, pairing)])[match.id]
    assert projection.supports_readiness is expected
    assert projection.mutation_available is expected
    assert projection.assignment_complete is expected
    assert projection.ready_sides == ((MatchSide.A, MatchSide.B) if expected else ())
    if not expected:
        assert projection.ready_at_a is projection.ready_by_a is None
        assert projection.status is ReadinessDisplayStatus.NOT_YET_OCCUPIED


# fmt: off
@pytest.mark.parametrize('count,terminal,confirmed,outcome', [
    (2, None, True, 'confirmed'),
    (1, None, True, 'defwin'),
    (0, None, True, 'defwin'),
    (2, TournamentStatus.COMPLETED, False, 'completed'),
    (2, TournamentStatus.CANCELLED, False, 'cancelled'),
    (2, TournamentStatus.COMPLETED, True, 'confirmed'),
])
# fmt: on
def test_outcomes_precede_ready(count, terminal, confirmed, outcome):
    tournament = _tournament(tournament_status=terminal or TournamentStatus.ONGOING)
    match, contestants, pairing = _row(tournament, count=count, claims=2)
    if confirmed:
        match = replace(match, confirmed_by=uuid4())
        contestants = [replace(c, score=1) for c in contestants]
    projection = _project(tournament, [(match, contestants, pairing)])[match.id]
    assert projection.display_status == outcome
    assert not projection.mutation_available
    assert projection.filter_bucket == 'finished'
    for only in READINESS_FILTER_BUCKETS:
        expected = [projection] if only == 'finished' else []
        assert helpers.filter_match_projections([projection], only=only) == expected
    assert helpers.filter_match_projections([projection], only='all') == [projection]


def test_filter_buckets_partition_every_projection():
    tournament = _tournament()
    rows = [_row(tournament, count=count, claims=claims) for count, claims in (
        (0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (2, 2),
    )]
    match, contestants, pairing = rows[-1]
    rows[-1] = replace(match, confirmed_by=uuid4()), contestants, pairing
    projections = list(_project(tournament, rows).values())
    assert [p.status for p in projections[:5]] == [
        ReadinessDisplayStatus.NOT_YET_OCCUPIED,
        ReadinessDisplayStatus.NOT_YET_OCCUPIED,
        ReadinessDisplayStatus.OPEN,
        ReadinessDisplayStatus.PARTIALLY_READY,
        ReadinessDisplayStatus.BOTH_READY,
    ]
    assert projections[0].assigned_contestant_count == 0
    assert projections[1].assigned_contestant_count == 1
    assert projections[2].assignment_complete and projections[2].ready_sides == ()
    assert projections[3].ready_sides == (MatchSide.A,)
    ffa = _tournament(game_format=GameFormat.FREE_FOR_ALL)
    completed = _tournament(tournament_status=TournamentStatus.COMPLETED)
    projections += _project(ffa, [_row(ffa, count=3)]).values()
    projections += _project(completed, [_row(completed, claims=2)]).values()
    assert [p.filter_bucket for p in projections] == [
        'waiting', 'waiting', 'not_ready', 'partially_ready', 'both_ready',
        'finished', 'no_readiness', 'finished',
    ]
    counts = helpers.count_match_projections(projections)
    assert counts == {
        'all': 8, 'waiting': 2, 'not_ready': 1, 'partially_ready': 1,
        'both_ready': 1, 'no_readiness': 1, 'finished': 2,
    }
    assert sum(counts[bucket] for bucket in READINESS_FILTER_BUCKETS) == counts['all']
    seen = []
    for bucket in READINESS_FILTER_BUCKETS:
        in_bucket = helpers.filter_match_projections(projections, only=bucket)
        assert len(in_bucket) == counts[bucket]
        assert all(p.filter_bucket == bucket for p in in_bucket)
        seen.extend(p.match_id for p in in_bucket)
    assert len(seen) == len(set(seen)) == len(projections)
    assert set(seen) == {p.match_id for p in projections}
    assert helpers.filter_match_projections(projections, only='all') == projections
    # Callers scope FIRST; every badge/count and filtered row sees that same scope.
    scoped = projections[1:4]
    counts = helpers.count_match_projections(scoped)
    assert counts == {
        'all': 3, 'waiting': 1, 'not_ready': 1, 'partially_ready': 1,
        'both_ready': 0, 'no_readiness': 0, 'finished': 0,
    }
    for only, count in counts.items():
        assert len(helpers.filter_match_projections(scoped, only=only)) == count
    assert set(helpers.count_match_projections([]).values()) == {0}


def test_legacy_filter_aliases_fall_back_to_all():
    tournament = _tournament()
    rows = [_row(tournament, count=count, claims=claims) for count, claims in (
        (0, 0), (2, 0), (2, 1), (2, 2),
    )]
    projections = list(_project(tournament, rows).values())
    for only in ('ready', 'playable', 'open', 'unknown', ''):
        assert helpers.normalize_match_projection_filter(only) == 'all'
        assert helpers.filter_match_projections(projections, only=only) == projections
    for only in (*READINESS_FILTER_BUCKETS, 'all'):
        assert helpers.normalize_match_projection_filter(only) == only


# fmt: off
@pytest.mark.parametrize('base,playoff,phase,count,expected', [
    (GameFormat.FREE_FOR_ALL, None, 1, 3, 'no_readiness'),
    (GameFormat.FREE_FOR_ALL, None, 1, 2, 'no_readiness'),
    (GameFormat.FREE_FOR_ALL, None, 1, 1, 'waiting'),
    (GameFormat.FREE_FOR_ALL, None, 1, 0, 'waiting'),
    (GameFormat.HIGHSCORE, None, 1, 4, 'no_readiness'),
    (GameFormat.HIGHSCORE, GameFormat.FREE_FOR_ALL, 2, 3, 'no_readiness'),
    (GameFormat.ONE_V_ONE, None, 2, 2, 'no_readiness'),
    (GameFormat.ONE_V_ONE, None, 1, 2, 'not_ready'),
    (GameFormat.ONE_V_ONE, None, 1, 1, 'waiting'),
    (GameFormat.ONE_V_ONE, None, 1, 0, 'waiting'),
    (GameFormat.ONE_V_ONE, GameFormat.ONE_V_ONE, 2, 2, 'not_ready'),
    (GameFormat.HIGHSCORE, GameFormat.ONE_V_ONE, 2, 2, 'not_ready'),
])
# fmt: on
def test_no_readiness_bucket_only_for_unsupported_formats(base, playoff, phase, count, expected):
    tournament = _tournament(game_format=base, playoff_game_format=playoff)
    match, contestants, pairing = _row(tournament, count=count, phase=phase)
    projection = _project(tournament, [(match, contestants, pairing)])[match.id]
    assert projection.filter_bucket == expected
    assert (projection.filter_bucket == 'no_readiness') is (
        not projection.supports_readiness and count >= 2
    )


def test_match_filter_options_follow_design_order():
    quantities = {
        'all': 21, 'waiting': 1, 'not_ready': 2, 'partially_ready': 3,
        'both_ready': 4, 'no_readiness': 5, 'finished': 6,
    }
    with patch.object(helpers, 'gettext', side_effect=lambda text: f'de:{text}'):
        options = helpers.match_filter_options(quantities)
        hidden = helpers.match_filter_options({**quantities, 'no_readiness': 0})
        empty = helpers.match_filter_options(helpers.count_match_projections([]))
    assert options == [
        ('waiting', 'de:Waiting for opponent', 1),
        ('not_ready', 'de:Not ready', 2),
        ('partially_ready', 'de:Partially ready', 3),
        ('both_ready', 'de:Both ready', 4),
        ('no_readiness', 'de:Open (no readiness)', 5),
        ('finished', 'de:Finished', 6),
        ('all', 'de:All', 21),
    ]
    assert [(key, count) for key, _, count in hidden] == [
        ('waiting', 1), ('not_ready', 2), ('partially_ready', 3),
        ('both_ready', 4), ('finished', 6), ('all', 21),
    ]
    assert [(key, count) for key, _, count in empty] == [
        ('waiting', 0), ('not_ready', 0), ('partially_ready', 0),
        ('both_ready', 0), ('finished', 0), ('all', 0),
    ]


def test_active_match_filter_ignores_buckets_the_bar_hides():
    quantities = {
        'all': 7,
        'waiting': 1,
        'not_ready': 2,
        'partially_ready': 0,
        'both_ready': 0,
        'no_readiness': 0,
        'finished': 4,
    }
    with patch.object(helpers, 'gettext', side_effect=lambda text: text):
        hidden = helpers.match_filter_options(quantities)
        shown = helpers.match_filter_options({**quantities, 'no_readiness': 3})
    assert helpers.active_match_filter('no_readiness', hidden) == 'all'
    assert helpers.active_match_filter('no_readiness', shown) == 'no_readiness'
    # Buckets the bar always lists stay selectable at count 0.
    for only in (
        'waiting',
        'not_ready',
        'partially_ready',
        'both_ready',
        'finished',
        'all',
    ):
        assert helpers.active_match_filter(only, hidden) == only
    for only in ('ready', 'playable', 'open', 'unknown', ''):
        assert helpers.active_match_filter(only, shown) == 'all'


def test_projection_batch_query_growth():
    """Mock policy check ONLY: real SQL growth is independently instrumented."""
    tournament = _tournament()
    for quantity in (10, 100):
        rows = [_row(tournament, claims=2) for _ in range(quantity)]
        pairs = {m.id: p for m, _, p in rows}
        with patch.object(helpers.tournament_repository, 'get_match_pairings_for_matches', return_value=pairs) as batch:
            result = helpers.build_match_readiness_projections(
                tournament, [m for m, _, _ in rows], {m.id: c for m, c, _ in rows},
            )
        batch.assert_called_once_with([m.id for m, _, _ in rows])
        assert len(result) == quantity
        assert all(p.ready_by_a == m.ready_by_a for m, _, _ in rows for p in [result[m.id]])


def test_projection_is_clock_independent_and_keeps_original_occupancy():
    tournament = _tournament(tournament_status=TournamentStatus.PAUSED)
    match, contestants, pairing = _row(tournament, claims=1)
    first = _project(tournament, [(match, contestants, pairing)])[match.id]
    second = derive_match_readiness(match, contestants, pairing=pairing, supports_readiness=True)
    assert first == replace(second, mutation_available=False)
    assert first.original_occupied_since == NOW - timedelta(days=1)
    assert first.pairing_started_at == NOW
    assert first.pairing_generation == 4 and first.readiness_revision == 7
    assert first.ready_at_a == NOW and first.ready_at_b is None
    assert first.ready_sides == (MatchSide.A,)
    assert 'no_show' not in first.display_status


# fmt: off
@pytest.mark.parametrize('real_count', [0, 1, 2])
# fmt: on
def test_bye_and_administrative_outcomes_precede_ready(real_count):
    """Byes/DEFWIN/admin results use confirmed facts, not a new status column."""
    tournament = _tournament()
    match, contestants, pairing = _row(tournament, claims=2)
    match = replace(match, confirmed_by=uuid4())
    contestants = [
        replace(c, participant_id=None, score=None) if index >= real_count
        else replace(c, score=0)
        for index, c in enumerate(contestants)
    ]
    projection = _project(tournament, [(match, contestants, pairing)])[match.id]
    assert projection.display_status == ('confirmed' if real_count == 2 else 'defwin')
    assert not projection.mutation_available
    assert projection.filter_bucket == 'finished'
    assert helpers.count_match_projections([projection])['both_ready'] == 0


def test_ffa_is_no_readiness_without_two_side_claims():
    tournament = _tournament(game_format=GameFormat.FREE_FOR_ALL)
    rows = [_row(tournament, count=3, claims=2)]
    projection = next(iter(_project(tournament, rows).values()))
    assert not projection.supports_readiness and not projection.assignment_complete
    assert projection.assigned_contestant_count == 3 and projection.ready_sides == ()
    assert projection.filter_bucket == 'no_readiness'
    assert helpers.filter_match_projections([projection], only='no_readiness') == [projection]
    assert helpers.filter_match_projections([projection], only='ready') == [projection]
    assert helpers.filter_match_projections([projection], only='both_ready') == []
