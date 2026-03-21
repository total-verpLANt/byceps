"""
tests.unit.services.lan_tournament.test_ffa_domain_logic
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for FFA domain-level algorithms:
snake_seed_groups, map_placement_to_points,
compute_ffa_round_standings, compute_ffa_cumulative_standings.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime

import pytest

from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    compute_ffa_cumulative_standings,
    compute_ffa_round_standings,
    map_placement_to_points,
    snake_seed_groups,
)

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


# -------------------------------------------------------------------- #
# snake_seed_groups
# -------------------------------------------------------------------- #


def test_snake_seed_groups_even_distribution():
    """16 players, groups of max 8 -> 2 balanced groups of 8."""
    ids = [str(i) for i in range(16)]
    result = snake_seed_groups(ids, group_size_min=4, group_size_max=8)

    assert result.is_ok()
    groups = result.unwrap()
    assert len(groups) == 2
    assert len(groups[0]) == 8
    assert len(groups[1]) == 8

    # Snake order: row 0 (L->R): [0->G0, 1->G1],
    #              row 1 (R->L): [2->G1, 3->G0], ...
    # Top seed (0) in group 0, second seed (1) in group 1.
    assert '0' in groups[0]
    assert '1' in groups[1]
    # Seeds should be interleaved fairly.
    all_ids = groups[0] + groups[1]
    assert sorted(all_ids) == sorted(ids)


def test_snake_seed_groups_uneven_distribution():
    """14 players, groups of max 8 -> 2 groups (7 + 7)."""
    ids = [str(i) for i in range(14)]
    result = snake_seed_groups(ids, group_size_min=4, group_size_max=8)

    assert result.is_ok()
    groups = result.unwrap()
    assert len(groups) == 2
    # ceil(14/8) = 2 groups; snake distributes as evenly as possible.
    sizes = sorted([len(g) for g in groups])
    assert sizes == [7, 7]

    all_ids = groups[0] + groups[1]
    assert sorted(all_ids) == sorted(ids)


def test_snake_seed_groups_single_group():
    """6 players, max 8 -> 1 group with all 6."""
    ids = [str(i) for i in range(6)]
    result = snake_seed_groups(ids, group_size_min=2, group_size_max=8)

    assert result.is_ok()
    groups = result.unwrap()
    assert len(groups) == 1
    assert len(groups[0]) == 6
    assert groups[0] == ids


def test_snake_seed_groups_respects_min_size():
    """Validates the minimum group size constraint."""
    # 5 players, max 3 -> ceil(5/3) = 2 groups -> [3, 2]
    # group_size_min=3 means the group of 2 should fail.
    ids = [str(i) for i in range(5)]
    result = snake_seed_groups(ids, group_size_min=3, group_size_max=3)

    assert result.is_err()
    assert 'below the minimum' in result.unwrap_err()


def test_snake_seed_groups_empty_input():
    """No contestants -> error."""
    result = snake_seed_groups([], group_size_min=2, group_size_max=4)
    assert result.is_err()


def test_snake_seed_groups_three_groups():
    """12 players, max 4 -> 3 groups of 4 with snake ordering."""
    ids = [str(i) for i in range(12)]
    result = snake_seed_groups(ids, group_size_min=3, group_size_max=4)

    assert result.is_ok()
    groups = result.unwrap()
    assert len(groups) == 3
    for g in groups:
        assert len(g) == 4

    # Snake verification: row 0 (L->R): 0->G0, 1->G1, 2->G2
    # row 1 (R->L): 3->G2, 4->G1, 5->G0
    assert groups[0][0] == '0'
    assert groups[1][0] == '1'
    assert groups[2][0] == '2'
    assert groups[2][1] == '3'
    assert groups[1][1] == '4'
    assert groups[0][1] == '5'


# -------------------------------------------------------------------- #
# map_placement_to_points
# -------------------------------------------------------------------- #


def test_map_placement_to_points_within_table():
    """Placement within table length maps correctly."""
    table = [10, 7, 5, 3, 1]
    assert map_placement_to_points(1, table) == 10
    assert map_placement_to_points(2, table) == 7
    assert map_placement_to_points(3, table) == 5
    assert map_placement_to_points(4, table) == 3
    assert map_placement_to_points(5, table) == 1


def test_map_placement_to_points_beyond_table():
    """Placement beyond table length returns 0."""
    table = [10, 7, 5]
    assert map_placement_to_points(4, table) == 0
    assert map_placement_to_points(10, table) == 0
    assert map_placement_to_points(100, table) == 0


def test_map_placement_to_points_empty_table():
    """Empty table always returns 0."""
    assert map_placement_to_points(1, []) == 0


def test_map_placement_to_points_single_entry():
    """Table with single entry."""
    table = [25]
    assert map_placement_to_points(1, table) == 25
    assert map_placement_to_points(2, table) == 0


# -------------------------------------------------------------------- #
# compute_ffa_round_standings
# -------------------------------------------------------------------- #


def test_compute_ffa_round_standings_single_group():
    """Standings from a single FFA match."""
    match_id = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    contestants = [
        _make_ffa_contestant(pid_a, match_id, placement=1, points=10),
        _make_ffa_contestant(pid_b, match_id, placement=2, points=7),
        _make_ffa_contestant(pid_c, match_id, placement=3, points=5),
    ]

    standings = compute_ffa_round_standings([contestants])

    assert len(standings) == 3
    assert standings[0] == (str(pid_a), 10)
    assert standings[1] == (str(pid_b), 7)
    assert standings[2] == (str(pid_c), 5)


def test_compute_ffa_round_standings_multiple_groups():
    """Standings aggregated across two matches in the same round."""
    m1 = TournamentMatchID(generate_uuid())
    m2 = TournamentMatchID(generate_uuid())

    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())
    pid_d = TournamentParticipantID(generate_uuid())

    group1 = [
        _make_ffa_contestant(pid_a, m1, placement=1, points=10),
        _make_ffa_contestant(pid_b, m1, placement=2, points=7),
    ]
    group2 = [
        _make_ffa_contestant(pid_c, m2, placement=1, points=10),
        _make_ffa_contestant(pid_d, m2, placement=2, points=7),
    ]

    standings = compute_ffa_round_standings([group1, group2])

    assert len(standings) == 4
    # Both 1st-place finishers have 10 pts, both 2nd have 7.
    points_list = [pts for _, pts in standings]
    assert points_list == [10, 10, 7, 7]


def test_compute_ffa_round_standings_empty():
    """No matches -> empty standings."""
    standings = compute_ffa_round_standings([])
    assert standings == []


# -------------------------------------------------------------------- #
# compute_ffa_cumulative_standings
# -------------------------------------------------------------------- #


def test_compute_ffa_cumulative_standings():
    """Cumulative standings across two rounds."""
    m1 = TournamentMatchID(generate_uuid())
    m2 = TournamentMatchID(generate_uuid())

    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    round1 = [
        [
            _make_ffa_contestant(pid_a, m1, placement=1, points=10),
            _make_ffa_contestant(pid_b, m1, placement=2, points=7),
            _make_ffa_contestant(pid_c, m1, placement=3, points=5),
        ],
    ]

    round2 = [
        [
            _make_ffa_contestant(pid_a, m2, placement=2, points=7),
            _make_ffa_contestant(pid_b, m2, placement=1, points=10),
        ],
    ]

    standings = compute_ffa_cumulative_standings([round1, round2])

    # A: 10+7=17, B: 7+10=17, C: 5+0=5
    cid_to_pts = dict(standings)
    assert cid_to_pts[str(pid_a)] == 17
    assert cid_to_pts[str(pid_b)] == 17
    assert cid_to_pts[str(pid_c)] == 5

    # A and B tied at 17 should be first (order between them
    # is stable but both before C).
    assert standings[2] == (str(pid_c), 5)


def test_compute_ffa_cumulative_standings_empty():
    """No rounds -> empty standings."""
    standings = compute_ffa_cumulative_standings([])
    assert standings == []


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _make_ffa_contestant(
    participant_id: TournamentParticipantID,
    match_id: TournamentMatchID,
    *,
    placement: int | None = None,
    points: int | None = None,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=participant_id,
        score=None,
        placement=placement,
        points=points,
        created_at=NOW,
    )
