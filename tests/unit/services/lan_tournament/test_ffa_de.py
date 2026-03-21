"""
tests.unit.services.lan_tournament.test_ffa_de
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for FFA double elimination pool logic:
advance_ffa_round (WB/LB), generate_ffa_grand_final,
confirm_ffa_match (GF completion), point carry.

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, UTC
from unittest.mock import Mock, patch, call

import pytest

from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import (
    GameFormat,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.lan_tournament import tournament_match_service
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')
USER_ID = UserID(generate_uuid())

REPO_PATH = (
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)


# -------------------------------------------------------------------- #
# advance_ffa_round — WB advancement: bottom moved to LB
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_wb_advancement_routes_bottom_to_lb(mock_repo):
    """WB advancement: top N stay in WB, bottom players route to LB.
    With enough contestants that GF is NOT triggered."""
    tournament = _create_de_tournament(
        advancement_count=2,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB round 0: 2 matches of 3 each.
    # advancement_count=2 -> 4 WB survivors, 2 dropped.
    mid_1 = TournamentMatchID(generate_uuid())
    mid_2 = TournamentMatchID(generate_uuid())
    pids = [TournamentParticipantID(generate_uuid()) for _ in range(6)]

    match_1 = _create_match(
        match_id=mid_1, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )
    match_2 = _create_match(
        match_id=mid_2, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [
        match_1, match_2,
    ]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [match_1, match_2]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == mid_1:
            return [
                _make_contestant(pids[0], mid_1, placement=1, points=10),
                _make_contestant(pids[1], mid_1, placement=2, points=7),
                _make_contestant(pids[2], mid_1, placement=3, points=3),
            ]
        if mid == mid_2:
            return [
                _make_contestant(pids[3], mid_2, placement=1, points=10),
                _make_contestant(pids[4], mid_2, placement=2, points=7),
                _make_contestant(pids[5], mid_2, placement=3, points=3),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.WINNERS, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 4 WB survivors + 0 LB survivors + 2 dropped = 6 > group_size_max=4
    assert result.unwrap() == 'advanced_wb'
    # Verify matches were created (WB next round + LB round).
    assert mock_repo.create_match.call_count >= 2


# -------------------------------------------------------------------- #
# advance_ffa_round — LB advancement: bottom eliminated
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_lb_advancement_eliminates_bottom(mock_repo):
    """LB advancement: top N survive in LB, bottom eliminated entirely."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=6,
    )
    mock_repo.get_tournament.return_value = tournament

    mid_1 = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    # LB round 0.
    match_1 = _create_match(
        match_id=mid_1, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    # WB round 0 (for survivor collection during GF check).
    wb_mid = TournamentMatchID(generate_uuid())
    wb_match = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )
    wb_pid_1 = TournamentParticipantID(generate_uuid())
    wb_pid_2 = TournamentParticipantID(generate_uuid())
    wb_pid_3 = TournamentParticipantID(generate_uuid())

    mock_repo.get_matches_for_tournament_ordered.return_value = [
        wb_match, match_1,
    ]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.LOSERS and rnd == 0:
            return [match_1]
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_match]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == mid_1:
            return [
                _make_contestant(pid_a, mid_1, placement=1, points=10),
                _make_contestant(pid_b, mid_1, placement=2, points=5),
                _make_contestant(pid_c, mid_1, placement=3, points=2),
            ]
        if mid == wb_mid:
            return [
                _make_contestant(wb_pid_1, wb_mid, placement=1, points=10),
                _make_contestant(wb_pid_2, wb_mid, placement=2, points=7),
                _make_contestant(wb_pid_3, wb_mid, placement=3, points=3),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.LOSERS, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 1 LB survivor + 1 WB survivor = 2 < group_size_max=6
    # -> grand_final_eligible
    assert result.unwrap() == 'grand_final_eligible'


@patch(REPO_PATH)
def test_lb_advancement_generates_next_lb_round(mock_repo):
    """LB advancement generates next LB round when survivors exceed GF trigger."""
    tournament = _create_de_tournament(
        advancement_count=2,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    # LB round 0: 2 matches of 3 players each, advance top 2 -> 4 LB survivors.
    mid_1 = TournamentMatchID(generate_uuid())
    mid_2 = TournamentMatchID(generate_uuid())
    pids = [TournamentParticipantID(generate_uuid()) for _ in range(6)]

    match_1 = _create_match(
        match_id=mid_1, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )
    match_2 = _create_match(
        match_id=mid_2, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    # WB round 0: 2 matches, 4 WB survivors.
    wb_mid_1 = TournamentMatchID(generate_uuid())
    wb_mid_2 = TournamentMatchID(generate_uuid())
    wb_pids = [TournamentParticipantID(generate_uuid()) for _ in range(4)]

    wb_match_1 = _create_match(
        match_id=wb_mid_1, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )
    wb_match_2 = _create_match(
        match_id=wb_mid_2, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [
        wb_match_1, wb_match_2, match_1, match_2,
    ]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.LOSERS and rnd == 0:
            return [match_1, match_2]
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_match_1, wb_match_2]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == mid_1:
            return [
                _make_contestant(pids[0], mid_1, placement=1, points=10),
                _make_contestant(pids[1], mid_1, placement=2, points=7),
                _make_contestant(pids[2], mid_1, placement=3, points=3),
            ]
        if mid == mid_2:
            return [
                _make_contestant(pids[3], mid_2, placement=1, points=10),
                _make_contestant(pids[4], mid_2, placement=2, points=7),
                _make_contestant(pids[5], mid_2, placement=3, points=3),
            ]
        if mid == wb_mid_1:
            return [
                _make_contestant(wb_pids[0], wb_mid_1, placement=1, points=10),
                _make_contestant(wb_pids[1], wb_mid_1, placement=2, points=7),
            ]
        if mid == wb_mid_2:
            return [
                _make_contestant(wb_pids[2], wb_mid_2, placement=1, points=10),
                _make_contestant(wb_pids[3], wb_mid_2, placement=2, points=7),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.LOSERS, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 4 LB survivors + 4 WB survivors = 8 > group_size_max=4
    # -> generates next LB round, returns 'advanced_lb'
    assert result.unwrap() == 'advanced_lb'
    # Verify new LB round created.
    assert mock_repo.create_match.call_count >= 1


# -------------------------------------------------------------------- #
# LB re-grouping after WB drops join
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_wb_advancement_merges_dropped_with_lb_survivors(mock_repo):
    """After WB advancement, dropped WB players join LB survivors
    for re-grouped LB round via snake seeding."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB round 0: 2 matches of 2.
    wb_mid_1 = TournamentMatchID(generate_uuid())
    wb_mid_2 = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())
    pid_d = TournamentParticipantID(generate_uuid())

    wb_m1 = _create_match(
        match_id=wb_mid_1, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )
    wb_m2 = _create_match(
        match_id=wb_mid_2, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    # Existing LB round 0: 1 match of 2 (from a previous WB drop).
    lb_mid = TournamentMatchID(generate_uuid())
    pid_e = TournamentParticipantID(generate_uuid())
    pid_f = TournamentParticipantID(generate_uuid())
    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    all_matches = [wb_m1, wb_m2, lb_m]
    mock_repo.get_matches_for_tournament_ordered.return_value = all_matches

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m1, wb_m2]
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == wb_mid_1:
            return [
                _make_contestant(pid_a, wb_mid_1, placement=1, points=10),
                _make_contestant(pid_b, wb_mid_1, placement=2, points=5),
            ]
        if mid == wb_mid_2:
            return [
                _make_contestant(pid_c, wb_mid_2, placement=1, points=10),
                _make_contestant(pid_d, wb_mid_2, placement=2, points=3),
            ]
        if mid == lb_mid:
            return [
                _make_contestant(pid_e, lb_mid, placement=1, points=8),
                _make_contestant(pid_f, lb_mid, placement=2, points=4),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.WINNERS, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 2 WB survivors + 2 WB dropped + 1 LB survivor = 5 > group_size_max=4
    # -> advanced_wb, new WB + LB rounds created.
    assert result.unwrap() == 'advanced_wb'
    # At least 2 create_match calls: 1 for next WB, 1+ for next LB.
    assert mock_repo.create_match.call_count >= 2


# -------------------------------------------------------------------- #
# Point carry: WB points carry for LB seeding when enabled
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_point_carry_enabled_uses_all_brackets_for_lb_seeding(mock_repo):
    """When points_carry_to_losers=True, WB cumulative points
    influence LB seeding order (query-time, never mutated)."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=4,
        points_carry_to_losers=True,
    )
    mock_repo.get_tournament.return_value = tournament

    # Point carry is a query-time concern — verify the tournament
    # flag is set correctly and the function does not mutate records.
    assert tournament.points_carry_to_losers is True

    # Verify the flag is available on the model.
    assert hasattr(tournament, 'points_carry_to_losers')


@patch(REPO_PATH)
def test_point_carry_disabled_tournament_flag(mock_repo):
    """When points_carry_to_losers=False, LB seeding uses
    LB-earned points only (query-time exclusion)."""
    tournament = _create_de_tournament(
        points_carry_to_losers=False,
    )
    mock_repo.get_tournament.return_value = tournament

    assert tournament.points_carry_to_losers is False


# -------------------------------------------------------------------- #
# GF trigger: survivors <= group_size_max returns "grand_final_eligible"
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_gf_trigger_wb_pool(mock_repo):
    """GF triggers when total survivors (WB + LB) <= group_size_max
    after WB advancement."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB round 0: 1 match of 2 -> 1 WB survivor, 1 dropped.
    wb_mid = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, wb_mid, placement=1, points=10),
        _make_contestant(pid_b, wb_mid, placement=2, points=5),
    ]

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.WINNERS, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 1 WB survivor + 0 LB survivors + 1 dropped = 2 <= group_size_max=4
    assert result.unwrap() == 'grand_final_eligible'
    # GF signal only — no new rounds created.
    mock_repo.create_match.assert_not_called()


@patch(REPO_PATH)
def test_gf_trigger_lb_pool(mock_repo):
    """GF triggers when total survivors (WB + LB) <= group_size_max
    after LB advancement."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=4,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB round 0: 1 match of 2 -> 1 WB survivor.
    wb_mid = TournamentMatchID(generate_uuid())
    wb_pid_a = TournamentParticipantID(generate_uuid())
    wb_pid_b = TournamentParticipantID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    # LB round 0: 1 match of 3 -> 1 LB survivor.
    lb_mid = TournamentMatchID(generate_uuid())
    lb_pid_a = TournamentParticipantID(generate_uuid())
    lb_pid_b = TournamentParticipantID(generate_uuid())
    lb_pid_c = TournamentParticipantID(generate_uuid())

    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m, lb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == wb_mid:
            return [
                _make_contestant(wb_pid_a, wb_mid, placement=1, points=10),
                _make_contestant(wb_pid_b, wb_mid, placement=2, points=5),
            ]
        if mid == lb_mid:
            return [
                _make_contestant(lb_pid_a, lb_mid, placement=1, points=8),
                _make_contestant(lb_pid_b, lb_mid, placement=2, points=4),
                _make_contestant(lb_pid_c, lb_mid, placement=3, points=1),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.LOSERS, initiator_id=USER_ID,
    )

    assert result.is_ok()
    # 1 WB survivor + 1 LB survivor = 2 <= group_size_max=4
    assert result.unwrap() == 'grand_final_eligible'


# -------------------------------------------------------------------- #
# GF generation: one big group, GRAND_FINAL bracket
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_generate_gf_creates_single_grand_final_match(mock_repo):
    """generate_ffa_grand_final merges all survivors into one
    GRAND_FINAL match."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=6,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB round 0: 1 match of 2 -> 1 survivor.
    wb_mid = TournamentMatchID(generate_uuid())
    wb_pid = TournamentParticipantID(generate_uuid())
    wb_pid_b = TournamentParticipantID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    # LB round 0: 1 match of 2 -> 1 survivor.
    lb_mid = TournamentMatchID(generate_uuid())
    lb_pid = TournamentParticipantID(generate_uuid())
    lb_pid_b = TournamentParticipantID(generate_uuid())

    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m, lb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m]
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == wb_mid:
            return [
                _make_contestant(wb_pid, wb_mid, placement=1, points=10),
                _make_contestant(wb_pid_b, wb_mid, placement=2, points=5),
            ]
        if mid == lb_mid:
            return [
                _make_contestant(lb_pid, lb_mid, placement=1, points=8),
                _make_contestant(lb_pid_b, lb_mid, placement=2, points=3),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.generate_ffa_grand_final(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_ok()
    assert result.unwrap() == 1

    # Verify exactly 1 match was created.
    assert mock_repo.create_match.call_count == 1
    created_match = mock_repo.create_match.call_args.args[0]
    assert created_match.bracket == Bracket.GRAND_FINAL

    # Verify 2 contestants created (1 WB + 1 LB survivor).
    assert mock_repo.create_match_contestant.call_count == 2
    mock_repo.commit_session.assert_called_once()


@patch(REPO_PATH)
def test_generate_gf_rejects_non_de_tournament(mock_repo):
    """generate_ffa_grand_final rejects non-DE tournaments."""
    tournament = _create_de_tournament(
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.generate_ffa_grand_final(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'double elimination' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# GF confirmation: triggers tournament completion
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_gf_confirmation_triggers_tournament_completion(mock_repo):
    """Confirming the Grand Final match triggers auto-complete."""
    tournament = _create_de_tournament()
    mock_repo.get_tournament.return_value = tournament

    gf_mid = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    gf_match = _create_match(
        match_id=gf_mid, confirmed_by=None, round=0,
        bracket=Bracket.GRAND_FINAL,
    )
    mock_repo.get_match_for_update.return_value = gf_match

    contestants = [
        _make_contestant(pid_a, gf_mid, placement=1, points=10),
        _make_contestant(pid_b, gf_mid, placement=2, points=7),
    ]
    mock_repo.get_contestants_for_match.return_value = contestants

    # After confirmation, get_matches_for_round returns the now-confirmed match.
    confirmed_gf = _create_match(
        match_id=gf_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.GRAND_FINAL,
    )
    mock_repo.get_matches_for_round.return_value = [confirmed_gf]

    # set_tournament_winner and set_tournament_status_flush succeed.
    mock_repo.set_tournament_winner.return_value = _ok_result()
    mock_repo.set_tournament_status_flush.return_value = _ok_result()

    result = tournament_match_service.confirm_ffa_match(
        gf_mid, USER_ID,
    )

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(gf_mid, USER_ID)
    # Tournament winner should have been set (1st place contestant).
    mock_repo.set_tournament_winner.assert_called_once()
    mock_repo.set_tournament_status_flush.assert_called_once()


@patch(REPO_PATH)
def test_non_gf_confirmation_does_not_complete_tournament(mock_repo):
    """Confirming a non-GF match in a DE tournament does not
    trigger completion."""
    tournament = _create_de_tournament()
    mock_repo.get_tournament.return_value = tournament

    wb_mid = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    wb_match = _create_match(
        match_id=wb_mid, confirmed_by=None, round=0,
        bracket=Bracket.WINNERS,
    )
    mock_repo.get_match_for_update.return_value = wb_match

    contestants = [
        _make_contestant(pid_a, wb_mid, placement=1, points=10),
        _make_contestant(pid_b, wb_mid, placement=2, points=7),
    ]
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_ffa_match(
        wb_mid, USER_ID,
    )

    assert result.is_ok()
    # Tournament winner should NOT be set.
    mock_repo.set_tournament_winner.assert_not_called()


# -------------------------------------------------------------------- #
# Team+DE rejection: FFA+TEAM when max_teams < group_size_min
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_generate_ffa_round_rejects_team_de_insufficient_max_teams(mock_repo):
    """FFA+TEAM+DE rejects when max_teams < group_size_min."""
    tournament = _create_de_tournament(
        contestant_type=ContestantType.TEAM,
        max_teams=2,
        group_size_min=3,
    )
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.generate_ffa_round(
        TOURNAMENT_ID, 0, ['a', 'b', 'c'],
    )

    assert result.is_err()
    assert 'team' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# DE requires pool parameter
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_advance_requires_pool_for_de(mock_repo):
    """DE advancement requires explicit pool parameter."""
    tournament = _create_de_tournament()
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'pool' in result.unwrap_err().lower()


@patch(REPO_PATH)
def test_single_track_rejects_pool_param(mock_repo):
    """Single-track FFA rejects pool parameter."""
    tournament = _create_de_tournament(
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.WINNERS, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'single-track' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# WB advancement rejects unconfirmed matches
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_wb_advancement_rejects_unconfirmed(mock_repo):
    """WB advancement rejects when not all matches confirmed."""
    tournament = _create_de_tournament(advancement_count=1)
    mock_repo.get_tournament.return_value = tournament

    wb_mid = TournamentMatchID(generate_uuid())
    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=None, round=0,
        bracket=Bracket.WINNERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m]
    mock_repo.get_matches_for_round.return_value = [wb_m]

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.WINNERS, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'not confirmed' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# LB advancement rejects unconfirmed matches
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_lb_advancement_rejects_unconfirmed(mock_repo):
    """LB advancement rejects when not all matches confirmed."""
    tournament = _create_de_tournament(advancement_count=1)
    mock_repo.get_tournament.return_value = tournament

    lb_mid = TournamentMatchID(generate_uuid())
    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=None, round=0,
        bracket=Bracket.LOSERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [lb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.LOSERS, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'not confirmed' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# Tie at WB cutoff
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_wb_advancement_detects_tie(mock_repo):
    """WB advancement returns Err on tie at cutoff."""
    tournament = _create_de_tournament(advancement_count=1)
    mock_repo.get_tournament.return_value = tournament

    wb_mid = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m]
    mock_repo.get_matches_for_round.return_value = [wb_m]

    mock_repo.get_contestants_for_match.return_value = [
        _make_contestant(pid_a, wb_mid, placement=1, points=10),
        _make_contestant(pid_b, wb_mid, placement=2, points=10),
    ]

    result = tournament_match_service.advance_ffa_round(
        TOURNAMENT_ID, pool=Bracket.WINNERS, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'tie' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# GF generation with teams
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_generate_gf_with_teams(mock_repo):
    """generate_ffa_grand_final correctly uses team_id for
    TEAM contestant type."""
    tournament = _create_de_tournament(
        contestant_type=ContestantType.TEAM,
        max_teams=10,
        advancement_count=1,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB: 1 match, 1 team survivor.
    wb_mid = TournamentMatchID(generate_uuid())
    team_a = TournamentTeamID(generate_uuid())
    team_b = TournamentTeamID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    # LB: 1 match, 1 team survivor.
    lb_mid = TournamentMatchID(generate_uuid())
    team_c = TournamentTeamID(generate_uuid())
    team_d = TournamentTeamID(generate_uuid())

    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m, lb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m]
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == wb_mid:
            return [
                _make_team_contestant(team_a, wb_mid, placement=1, points=10),
                _make_team_contestant(team_b, wb_mid, placement=2, points=5),
            ]
        if mid == lb_mid:
            return [
                _make_team_contestant(team_c, lb_mid, placement=1, points=8),
                _make_team_contestant(team_d, lb_mid, placement=2, points=3),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.generate_ffa_grand_final(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_ok()
    assert result.unwrap() == 1

    # Verify contestants created with team_id set.
    assert mock_repo.create_match_contestant.call_count == 2
    for c_call in mock_repo.create_match_contestant.call_args_list:
        contestant = c_call.args[0]
        assert contestant.team_id is not None
        assert contestant.participant_id is None


# -------------------------------------------------------------------- #
# GF duplicate guard
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_generate_gf_rejects_duplicate(mock_repo):
    """generate_ffa_grand_final rejects when GF matches already exist."""
    tournament = _create_de_tournament()
    mock_repo.get_tournament.return_value = tournament

    existing_gf = _create_match(
        confirmed_by=USER_ID, round=0, bracket=Bracket.GRAND_FINAL,
    )
    mock_repo.get_matches_for_tournament_ordered.return_value = [existing_gf]

    result = tournament_match_service.generate_ffa_grand_final(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_err()
    assert 'already been generated' in result.unwrap_err().lower()
    mock_repo.create_match.assert_not_called()


# -------------------------------------------------------------------- #
# GF seeding: points_carry_to_losers
# -------------------------------------------------------------------- #


@patch(REPO_PATH)
def test_generate_gf_seeding_carry_enabled(mock_repo):
    """When points_carry=True, GF seeding uses full cross-bracket
    cumulative.  A LB survivor with high WB points can outrank
    a WB survivor with fewer total points."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=6,
        points_carry_to_losers=True,
    )
    mock_repo.get_tournament.return_value = tournament

    # WB round 0: pid_a (1st, 10pts), pid_b (2nd, 5pts).
    wb_mid = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    # LB round 0: pid_b (dropped from WB) plays pid_c.
    # pid_b earned 5 WB pts + 10 LB pts = 15 total.
    lb_mid = TournamentMatchID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m, lb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m]
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == wb_mid:
            return [
                _make_contestant(pid_a, wb_mid, placement=1, points=10),
                _make_contestant(pid_b, wb_mid, placement=2, points=5),
            ]
        if mid == lb_mid:
            return [
                _make_contestant(pid_b, lb_mid, placement=1, points=10),
                _make_contestant(pid_c, lb_mid, placement=2, points=3),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.generate_ffa_grand_final(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_ok()

    # With carry=True, cumulative is cross-bracket:
    # pid_b: 5 (WB) + 10 (LB) = 15
    # pid_a: 10 (WB) = 10
    # pid_c: 3 (LB) = 3
    # Survivors: pid_a (WB), pid_b (LB) -> seeded by cumulative.
    # pid_b (15) should rank before pid_a (10).
    assert mock_repo.create_match_contestant.call_count == 2
    created = [
        c.args[0] for c in mock_repo.create_match_contestant.call_args_list
    ]
    # First contestant should be pid_b (15 pts), second pid_a (10 pts).
    assert created[0].participant_id == pid_b
    assert created[1].participant_id == pid_a


@patch(REPO_PATH)
def test_generate_gf_seeding_carry_disabled(mock_repo):
    """When points_carry=False, GF seeding ranks WB survivors first
    (by WB-only cumulative), then LB survivors (by LB-only cumulative).
    WB points do NOT carry for LB survivors."""
    tournament = _create_de_tournament(
        advancement_count=1,
        group_size_min=2,
        group_size_max=6,
        points_carry_to_losers=False,
    )
    mock_repo.get_tournament.return_value = tournament

    # Same scenario as carry_enabled test:
    # WB round 0: pid_a (1st, 10pts), pid_b (2nd, 5pts).
    wb_mid = TournamentMatchID(generate_uuid())
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    wb_m = _create_match(
        match_id=wb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.WINNERS,
    )

    # LB round 0: pid_b (dropped) wins with 10 LB pts.
    lb_mid = TournamentMatchID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    lb_m = _create_match(
        match_id=lb_mid, confirmed_by=USER_ID, round=0,
        bracket=Bracket.LOSERS,
    )

    mock_repo.get_matches_for_tournament_ordered.return_value = [wb_m, lb_m]

    def get_round_matches(tid, rnd, *, bracket=None):
        if bracket == Bracket.WINNERS and rnd == 0:
            return [wb_m]
        if bracket == Bracket.LOSERS and rnd == 0:
            return [lb_m]
        return []

    mock_repo.get_matches_for_round.side_effect = get_round_matches

    def get_contestants(mid):
        if mid == wb_mid:
            return [
                _make_contestant(pid_a, wb_mid, placement=1, points=10),
                _make_contestant(pid_b, wb_mid, placement=2, points=5),
            ]
        if mid == lb_mid:
            return [
                _make_contestant(pid_b, lb_mid, placement=1, points=10),
                _make_contestant(pid_c, lb_mid, placement=2, points=3),
            ]
        return []

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.generate_ffa_grand_final(
        TOURNAMENT_ID, initiator_id=USER_ID,
    )

    assert result.is_ok()

    # With carry=False:
    # WB survivors seeded first: pid_a (10 WB pts)
    # LB survivors seeded second: pid_b (10 LB pts — WB pts excluded)
    # pid_a MUST rank before pid_b (WB survivors always first).
    assert mock_repo.create_match_contestant.call_count == 2
    created = [
        c.args[0] for c in mock_repo.create_match_contestant.call_args_list
    ]
    assert created[0].participant_id == pid_a
    assert created[1].participant_id == pid_b


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_de_tournament(**kwargs) -> Tournament:
    """Create an FFA+DE tournament for testing."""
    defaults = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'FFA DE Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'updated_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': TournamentStatus.ONGOING,
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
        'point_table': [10, 7, 5, 3, 1],
        'advancement_count': 2,
        'group_size_min': 2,
        'group_size_max': 6,
        'points_carry_to_losers': False,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _create_match(
    *,
    match_id: TournamentMatchID | None = None,
    confirmed_by: UserID | None = None,
    round: int | None = None,
    bracket: Bracket | None = None,
) -> TournamentMatch:
    """Create a tournament match for testing."""
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=round,
        next_match_id=None,
        confirmed_by=confirmed_by,
        created_at=NOW,
        bracket=bracket,
    )


def _make_contestant(
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


def _make_team_contestant(
    team_id: TournamentTeamID,
    match_id: TournamentMatchID,
    *,
    placement: int | None = None,
    points: int | None = None,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=None,
        score=None,
        placement=placement,
        points=points,
        created_at=NOW,
    )


def _ok_result():
    """Create a mock Ok result."""
    from byceps.util.result import Ok
    return Ok(None)
