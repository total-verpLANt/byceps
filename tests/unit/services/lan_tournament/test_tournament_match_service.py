"""
tests.unit.services.lan_tournament.test_tournament_match_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime, UTC
from unittest.mock import Mock, patch

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.lan_tournament.events import (
    MatchConfirmedEvent,
    TournamentUncompletedEvent,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament import tournament_match_service
from byceps.services.party.models import PartyID
from byceps.services.user.models.user import UserID
from byceps.util.result import Ok

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
TOURNAMENT_ID = TournamentID(generate_uuid())
PARTY_ID = PartyID('lan-2025')
MATCH_ID = TournamentMatchID(generate_uuid())
USER_ID = UserID(generate_uuid())


# -------------------------------------------------------------------- #
# generate_single_elimination_bracket
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_4_players_total_matches(mock_repo):
    """4 players => bracket_size 4 => 3 total matches."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 3  # 4-1 = 3 matches


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_8_teams_total_matches(mock_repo):
    """8 teams => bracket_size 8 => 7 total matches."""
    tournament = _create_tournament(contestant_type=ContestantType.TEAM)
    teams = [
        _create_mock_team(TournamentTeamID(generate_uuid())) for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = teams
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 7  # 8-1 = 7 matches


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_all_rounds_created(mock_repo):
    """4 players => 2 rounds (round 0 + final)."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    # Check that create_match was called 3 times (3 matches)
    assert mock_repo.create_match.call_count == 3

    # Verify round assignments in created matches
    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]
    rounds = sorted(m.round for m in created_matches)
    # 2 round-0 matches + 1 final (round 1)
    assert rounds == [0, 0, 1]


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_linkage_correct(mock_repo):
    """Round 0 matches should link to the final match."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    # Final match (created first due to reverse order) has
    # no next_match_id
    final_matches = [m for m in created_matches if m.round == 1]
    assert len(final_matches) == 1
    assert final_matches[0].next_match_id is None

    # Round 0 matches should point to the final
    round0_matches = [m for m in created_matches if m.round == 0]
    assert len(round0_matches) == 2
    for m in round0_matches:
        assert m.next_match_id == final_matches[0].id


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_bye_handling(mock_repo):
    """5 players => 3 BYEs; auto-advance solo contestants."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(5)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    # Track contestants created per match to simulate BYE
    # detection. For BYE matches, return only 1 contestant.
    contestant_by_match: dict[
        TournamentMatchID, list[TournamentMatchToContestant]
    ] = {}

    def track_contestant(contestant):
        mid = contestant.tournament_match_id
        if mid not in contestant_by_match:
            contestant_by_match[mid] = []
        contestant_by_match[mid].append(contestant)

    mock_repo.create_match_contestant.side_effect = track_contestant

    def get_contestants(match_id):
        return contestant_by_match.get(match_id, [])

    mock_repo.get_contestants_for_match.side_effect = get_contestants

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 7  # 8-1 = 7 matches total


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_without_contestant_type(mock_repo):
    """Bracket generation fails without contestant type set."""
    tournament = _create_tournament(contestant_type=None)

    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert 'contestant type' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_bracket_with_one_contestant(mock_repo):
    """Bracket generation fails with only 1 contestant."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_single_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert '2 contestants' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# generate_double_elimination_bracket
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_4_players_total_matches(
    mock_repo,
):
    """4 players => 6 matches (WB:3 + LB:2 + GF:1)."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 6  # 2*4 - 2 = 6


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_8_players_total_matches(
    mock_repo,
):
    """8 players => 14 matches (2*8 - 2)."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 14  # 2*8 - 2 = 14


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_5_players_total_matches(
    mock_repo,
):
    """5 players => bracket_size 8 => 14 matches (2*8 - 2).
    Non-power-of-2 verifies BYE handling in DE.
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(5)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 14  # 2*8 - 2

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]
    brackets = [m.bracket for m in created_matches]
    assert Bracket.WINNERS in brackets
    assert Bracket.LOSERS in brackets
    assert Bracket.GRAND_FINAL in brackets


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_wb_lb_gf_structure(
    mock_repo,
):
    """Verify bracket labels ('WB', 'LB', 'GF') on
    created matches.
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    brackets = [m.bracket for m in created_matches]
    assert Bracket.WINNERS in brackets
    assert Bracket.LOSERS in brackets
    assert Bracket.GRAND_FINAL in brackets

    wb_count = brackets.count(Bracket.WINNERS)
    lb_count = brackets.count(Bracket.LOSERS)
    gf_count = brackets.count(Bracket.GRAND_FINAL)

    # 4 players: WB=3, LB=2, GF=1 (no reset)
    assert wb_count == 3
    assert lb_count == 2
    assert gf_count == 1


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_de_bracket_gf_is_terminal(mock_repo):
    """GF match has next_match_id=None and no bracket reset
    match exists.
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    gf_matches = [
        m for m in created_matches if m.bracket == Bracket.GRAND_FINAL
    ]
    assert len(gf_matches) == 1

    gf = gf_matches[0]
    assert gf.next_match_id is None, 'GF must be the terminal match'

    # No match with round=1 in GF bracket (no bracket reset).
    reset_matches = [
        m
        for m in created_matches
        if m.bracket == Bracket.GRAND_FINAL and m.round == 1
    ]
    assert len(reset_matches) == 0, 'Bracket reset match must not exist'


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_loser_routing_wired(
    mock_repo,
):
    """WB matches (except WBR0) have loser_next_match_id
    set.
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    wb_matches = [m for m in created_matches if m.bracket == Bracket.WINNERS]

    # All WB matches should have loser_next_match_id set.
    for m in wb_matches:
        assert m.loser_next_match_id is not None, (
            f'WB match round={m.round} order={m.match_order}'
            f' missing loser_next_match_id'
        )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_seeding_correct(mock_repo):
    """Round 0 WB uses standard seed order."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    # Verify contestants were placed into WBR0 matches.
    assert mock_repo.create_match_contestant.call_count >= 4

    # All seeded contestants should be placed into WB
    # round 0 matches.
    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]
    wb_r0_ids = {
        m.id
        for m in created_matches
        if m.bracket == Bracket.WINNERS and m.round == 0
    }

    seeded = [
        call.args[0]
        for call in (mock_repo.create_match_contestant.call_args_list)
    ]
    # All initial contestants go into WBR0 matches.
    for c in seeded[:4]:
        assert c.tournament_match_id in wb_r0_ids


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_4p_wb_final_loser_routes_to_lbr2(
    mock_repo,
):
    """4 players: WB final (wb_r=1) loser routes to LBR2,
    not LBR1.
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    # Identify WB final (bracket=WINNERS, round=1 for p=2).
    wb_final = [
        m
        for m in created_matches
        if m.bracket == Bracket.WINNERS and m.round == 1
    ]
    assert len(wb_final) == 1

    # Identify LB matches by round.
    lb_by_round: dict[int, list] = {}
    for m in created_matches:
        if m.bracket == Bracket.LOSERS:
            lb_by_round.setdefault(m.round, []).append(m)

    # LBR2 must exist and the WB final loser must
    # route there, not LBR1.
    assert 2 in lb_by_round, 'LBR2 missing from bracket'
    lbr2_ids = {m.id for m in lb_by_round[2]}

    assert wb_final[0].loser_next_match_id in lbr2_ids, (
        f'WB final loser routes to'
        f' {wb_final[0].loser_next_match_id},'
        f' expected one of LBR2 ids {lbr2_ids}'
    )


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_8p_loser_routing_correctness(
    mock_repo,
):
    """8 players: verify all WB loser routing targets the
    correct LB round.

    Expected mapping (p=3, wb_rounds=3, lb_rounds=4):
      wb_r=0 -> LBR1
      wb_r=1 -> LBR2
      wb_r=2 -> LBR4
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    mock_repo.get_contestants_for_match.return_value = []

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]

    # Build lookup: match_id -> match
    by_id = {m.id: m for m in created_matches}

    # Build LB round -> set of match IDs.
    lb_round_ids: dict[int, set] = {}
    for m in created_matches:
        if m.bracket == Bracket.LOSERS:
            lb_round_ids.setdefault(m.round, set()).add(m.id)

    expected_lb_round = {0: 1, 1: 2, 2: 4}

    for m in created_matches:
        if m.bracket != Bracket.WINNERS:
            continue
        wb_r = m.round
        loser_mid = m.loser_next_match_id
        assert loser_mid is not None, (
            f'WB round {wb_r} match missing loser_next_match_id'
        )

        target = by_id[loser_mid]
        assert target.bracket == Bracket.LOSERS, (
            f'Loser target for WB round {wb_r} is not in LB'
        )
        assert target.round == expected_lb_round[wb_r], (
            f'WB round {wb_r} loser routed to'
            f' LBR{target.round}, expected'
            f' LBR{expected_lb_round[wb_r]}'
        )


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_2_players_rejected(
    mock_repo,
):
    """2 players must be rejected for DE brackets."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(2)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert '4 contestants' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_3_players_rejected(
    mock_repo,
):
    """3 players must be rejected for DE brackets."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(3)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert '4 contestants' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_defwin_nullifies_loser_link(
    mock_repo,
):
    """5 players: WBR0 DEFWIN matches have loser_next_match_id
    cleared via clear_loser_next_match_id.
    """
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(5)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    # Simulate DEFWIN detection: some WBR0 matches will
    # have only 1 contestant.
    # For bracket_size=8, WBR0 has 4 matches.
    # 5 players means 3 DEFWIN matches (1 contestant each).
    # Return 1 contestant for most, 2 for one match.
    single = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
        ),
    ]
    pair = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
        ),
    ]
    # get_contestants_for_match is called once per WBR0
    # match in the auto-advance loop, then again per match
    # in the DEFWIN nullification loop.
    mock_repo.get_contestants_for_match.side_effect = [
        single,  # WBR0 match 0: DEFWIN (auto-advance)
        pair,  # WBR0 match 1: real match (skip)
        single,  # WBR0 match 2: DEFWIN (auto-advance)
        single,  # WBR0 match 3: DEFWIN (auto-advance)
        single,  # WBR0 match 0: DEFWIN (nullify loop)
        pair,  # WBR0 match 1: real (nullify loop skips)
        single,  # WBR0 match 2: DEFWIN (nullify loop)
        single,  # WBR0 match 3: DEFWIN (nullify loop)
    ]

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    # clear_loser_next_match_id called for each DEFWIN
    # match (3 out of 4 WBR0 matches).
    assert mock_repo.clear_loser_next_match_id.call_count == 3


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_generate_de_bracket_defwin_still_advances_to_wb1(
    mock_repo,
):
    """DEFWIN contestants still advance to next WB round."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(5)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    sole_pid = TournamentParticipantID(generate_uuid())
    single = [
        _create_match_contestant(participant_id=sole_pid),
    ]
    pair = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
        ),
    ]
    # auto-advance loop + nullification loop
    mock_repo.get_contestants_for_match.side_effect = [
        single,
        pair,
        single,
        single,
        single,
        pair,
        single,
        single,
    ]

    result = tournament_match_service.generate_double_elimination_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    # create_match_contestant called for seeding (5 calls)
    # + auto-advance (3 DEFWIN contestants).
    # Verify auto-advance calls happened.
    seeded_calls = [
        call.args[0]
        for call in (mock_repo.create_match_contestant.call_args_list)
    ]
    # At least 5 seeding + 3 auto-advance = 8 calls.
    assert len(seeded_calls) >= 8


# -------------------------------------------------------------------- #
# _determine_loser
# -------------------------------------------------------------------- #


def test_determine_loser_returns_err_on_wrong_count():
    """_determine_loser returns Err when contestant count != 2."""
    a = _create_match_contestant(
        participant_id=TournamentParticipantID(generate_uuid()),
        score=10,
    )
    b = _create_match_contestant(
        participant_id=TournamentParticipantID(generate_uuid()),
        score=5,
    )
    c = _create_match_contestant(
        participant_id=TournamentParticipantID(generate_uuid()),
        score=3,
    )

    result = tournament_match_service._determine_loser([a, b, c], a)

    assert result.is_err()
    assert 'Expected 2' in result.unwrap_err()


def test_determine_loser_returns_ok():
    """_determine_loser returns Ok(loser) for a valid 2-contestant
    match.
    """
    a = _create_match_contestant(
        participant_id=TournamentParticipantID(generate_uuid()),
        score=10,
    )
    b = _create_match_contestant(
        participant_id=TournamentParticipantID(generate_uuid()),
        score=5,
    )

    result = tournament_match_service._determine_loser([a, b], a)

    assert result.is_ok()
    assert result.unwrap().id == b.id


# -------------------------------------------------------------------- #
# generate_round_robin_bracket
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_4_players_total_matches(
    mock_repo,
):
    """4 players => 6 matches (N*(N-1)/2 = 4*3/2)."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 6  # 4*3/2 = 6


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_8_teams_total_matches(
    mock_repo,
):
    """8 teams => 28 matches (N*(N-1)/2 = 8*7/2)."""
    tournament = _create_tournament(contestant_type=ContestantType.TEAM)
    teams = [
        _create_mock_team(TournamentTeamID(generate_uuid())) for _ in range(8)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_teams_for_tournament.return_value = teams

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()
    assert result.unwrap() == 28  # 8*7/2 = 28


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_creates_all_rounds(
    mock_repo,
):
    """4 players => 3 rounds (N-1 for even N)."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]
    rounds = sorted({m.round for m in created_matches})
    assert rounds == [0, 1, 2]


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_no_next_match_linking(
    mock_repo,
):
    """All RR matches have next_match_id=None."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    created_matches = [
        call.args[0] for call in mock_repo.create_match.call_args_list
    ]
    for m in created_matches:
        assert m.next_match_id is None


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_contestants_placed(
    mock_repo,
):
    """Each RR match has 2 contestants created."""
    tournament = _create_tournament(contestant_type=ContestantType.SOLO)
    participants = [
        _create_mock_participant(TournamentParticipantID(generate_uuid()))
        for _ in range(4)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_ok()

    # 6 matches * 2 contestants each = 12 contestant entries
    assert mock_repo.create_match_contestant.call_count == 12


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_with_force_regenerate(
    mock_repo,
):
    """force_regenerate=True clears existing bracket first."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
        for _ in range(3)
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants
    # Simulate existing matches on the first call,
    # then none after clearing.
    mock_repo.get_matches_for_tournament.side_effect = [
        [_create_match()],  # has_matches -> True
        [_create_match()],  # has_matches in clear_bracket
        [],  # delete_match -> get_match
    ]

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID, force_regenerate=True
    )

    assert result.is_ok()
    assert result.unwrap() == 3  # C(3,2) = 3


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_less_than_2_contestants(
    mock_repo,
):
    """Fewer than 2 contestants returns Err."""
    tournament = _create_tournament(
        contestant_type=ContestantType.SOLO,
    )
    participants = [
        _create_mock_participant(
            TournamentParticipantID(generate_uuid()),
        )
    ]

    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_participants_for_tournament.return_value = participants

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert '2 contestants' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_generate_round_robin_no_contestant_type(
    mock_repo,
):
    """No contestant_type set returns Err."""
    tournament = _create_tournament(contestant_type=None)

    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.generate_round_robin_bracket(
        TOURNAMENT_ID
    )

    assert result.is_err()
    assert 'contestant type' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# set_score
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_with_valid_score(mock_repo):
    """Test setting a valid score."""
    contestant_id = TournamentParticipantID(generate_uuid())
    contestant = _create_match_contestant(
        participant_id=contestant_id, score=None
    )

    mock_repo.find_contestant_for_match.return_value = contestant

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, 10)

    assert result.is_ok()
    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 10)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_with_zero(mock_repo):
    """Test setting score to zero is valid."""
    contestant_id = TournamentParticipantID(generate_uuid())
    contestant = _create_match_contestant(
        participant_id=contestant_id, score=None
    )

    mock_repo.find_contestant_for_match.return_value = contestant

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, 0)

    assert result.is_ok()
    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 0)


def test_set_score_with_negative_score():
    """Test that negative scores are rejected."""
    contestant_id = TournamentParticipantID(generate_uuid())

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, -1)

    assert result.is_err()
    assert 'negative' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_for_team(mock_repo):
    """Test setting score for a team contestant."""
    team_id = TournamentTeamID(generate_uuid())
    contestant = _create_match_contestant(team_id=team_id, score=None)

    # First lookup (as participant) returns None, second (as team)
    # succeeds
    mock_repo.find_contestant_for_match.side_effect = [
        None,
        contestant,
    ]

    result = tournament_match_service.set_score(MATCH_ID, team_id, 5)

    assert result.is_ok()
    mock_repo.update_contestant_score.assert_called_once_with(contestant.id, 5)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_set_score_for_nonexistent_contestant(mock_repo):
    """Test that setting score for nonexistent contestant fails."""
    contestant_id = TournamentParticipantID(generate_uuid())

    # Both lookups return None
    mock_repo.find_contestant_for_match.side_effect = [None, None]

    result = tournament_match_service.set_score(MATCH_ID, contestant_id, 10)

    assert result.is_err()
    assert 'not found' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# confirm_match
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_with_scores(mock_repo):
    """Test confirming a match with scores set."""
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID)


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_confirm_match_uses_row_lock(mock_repo):
    """confirm_match acquires a row lock via
    get_match_for_update (SELECT FOR UPDATE) to prevent
    TOCTOU races on concurrent confirmation.
    """
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(
                generate_uuid()
            ),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(
                generate_uuid()
            ),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = (
        contestants
    )

    result = tournament_match_service.confirm_match(
        MATCH_ID, USER_ID
    )

    assert result.is_ok()
    mock_repo.get_match_for_update.assert_called_once_with(
        MATCH_ID,
    )
    mock_repo.get_match.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_without_scores(mock_repo):
    """Test that confirming fails if contestants lack scores."""
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=None,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'scores' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_already_confirmed(mock_repo):
    """Test that double-confirmation is rejected."""
    match = _create_match(confirmed_by=UserID(generate_uuid()))

    mock_repo.get_match_for_update.return_value = match

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'already confirmed' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_with_less_than_two_contestants(mock_repo):
    """Test that confirming fails with less than 2 contestants."""
    match = _create_match(confirmed_by=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        )
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert '2 contestants' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_nonexistent_match(mock_repo):
    """Test that confirming nonexistent match raises."""
    mock_repo.get_match_for_update.side_effect = ValueError(
        f'Unknown match ID "{MATCH_ID}"'
    )

    with pytest.raises(ValueError, match='Unknown match ID'):
        tournament_match_service.confirm_match(MATCH_ID, USER_ID)


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_advances_winner_to_next_match(mock_repo):
    """Test that confirming advances the winner to the next match."""
    next_match_id = TournamentMatchID(generate_uuid())
    match = _create_match(confirmed_by=None, next_match_id=next_match_id)

    winner_participant_id = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(
            participant_id=winner_participant_id, score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID)

    # Verify contestant was created in the next match
    mock_repo.create_match_contestant.assert_called_once()
    created = mock_repo.create_match_contestant.call_args[0][0]
    assert created.tournament_match_id == next_match_id
    assert created.participant_id == winner_participant_id


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_no_advancement_without_next_match(mock_repo):
    """Test that confirming without next_match_id does not advance."""
    match = _create_match(confirmed_by=None, next_match_id=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.create_match_contestant.assert_not_called()


# -------------------------------------------------------------------- #
# confirm_match — draw handling
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_draw_ok_in_round_robin(mock_repo):
    """Draw in round-robin mode confirms successfully."""
    tournament = _create_tournament(
        tournament_mode=TournamentMode.ROUND_ROBIN,
    )
    match = _create_match(confirmed_by=None, next_match_id=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.confirm_match.assert_called_once_with(MATCH_ID, USER_ID)
    mock_repo.commit_session.assert_called()
    # No advancement should occur for a draw.
    mock_repo.create_match_contestant.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_draw_blocked_in_single_elimination(
    mock_repo,
):
    """Draw in single-elimination mode returns Err."""
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )
    match = _create_match(confirmed_by=None, next_match_id=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_err()
    assert 'draw' in result.unwrap_err().lower()
    mock_repo.confirm_match.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.match_confirmed'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_draw_event_has_no_winner(mock_repo, mock_signal):
    """MatchConfirmedEvent for a draw has no winner IDs."""
    tournament = _create_tournament(
        tournament_mode=TournamentMode.ROUND_ROBIN,
    )
    match = _create_match(confirmed_by=None, next_match_id=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Verify the signal was sent with a MatchConfirmedEvent.
    mock_signal.send.assert_called_once()
    event = mock_signal.send.call_args[1]['event']
    assert isinstance(event, MatchConfirmedEvent)
    assert event.winner_team_id is None
    assert event.winner_participant_id is None
    assert event.match_id == MATCH_ID
    assert event.tournament_id == TOURNAMENT_ID


# -------------------------------------------------------------------- #
# unconfirm_match
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_success(mock_repo):
    """Test unconfirming a confirmed match."""
    confirmed_by = UserID(generate_uuid())
    match = _create_match(confirmed_by=confirmed_by)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.unconfirm_match.assert_called_once_with(MATCH_ID)
    mock_repo.commit_session.assert_called()


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.db'
)
@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_not_confirmed_returns_err(
    mock_repo, _mock_db
):
    """Test that unconfirming an unconfirmed match returns Err."""
    match = _create_match(confirmed_by=None)

    mock_repo.get_match_for_update.return_value = match

    result = tournament_match_service.unconfirm_match(
        MATCH_ID, USER_ID
    )

    assert result.is_err()
    assert 'not confirmed' in result.unwrap_err().lower()


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_not_found_raises(mock_repo):
    """Test that unconfirming a nonexistent match raises ValueError."""
    mock_repo.get_match_for_update.side_effect = ValueError(
        'Unknown match ID "..."'
    )

    with pytest.raises(ValueError, match='Unknown match ID'):
        tournament_match_service.unconfirm_match(
            MATCH_ID, USER_ID
        )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_retracts_advanced_contestant(mock_repo):
    """Test that unconfirming retracts the advanced contestant."""
    next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by, next_match_id=next_match_id
    )

    winner_participant_id = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(
            participant_id=winner_participant_id, score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    # Next match is not confirmed, so no cascade needed
    next_match = _create_match(match_id=next_match_id, confirmed_by=None)

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_match.return_value = next_match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Verify contestant was removed from the next match
    mock_repo.delete_contestant_from_match.assert_called_once_with(
        next_match_id,
        team_id=None,
        participant_id=winner_participant_id,
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_draw_skips_cascade(mock_repo):
    """Unconfirming a drawn round-robin match skips cascade retraction."""
    next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by, next_match_id=next_match_id
    )

    # Equal scores => draw => determine_match_winner returns Ok(None)
    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.unconfirm_match.assert_called_once_with(MATCH_ID)
    # Draw: no winner => no cascade retraction
    mock_repo.delete_contestant_from_match.assert_not_called()


# -------------------------------------------------------------------- #
# confirm_match / unconfirm_match — loser routing (DE)
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_advances_loser_in_de(mock_repo):
    """Confirming a WB match with loser_next_match_id advances
    the loser to the losers bracket.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    loser_next_match_id = TournamentMatchID(generate_uuid())
    match = _create_match(
        confirmed_by=None,
        next_match_id=next_match_id,
        loser_next_match_id=loser_next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    loser_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(participant_id=loser_pid, score=5),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Two create_match_contestant calls: winner + loser
    assert mock_repo.create_match_contestant.call_count == 2

    created_calls = [
        call.args[0]
        for call in (mock_repo.create_match_contestant.call_args_list)
    ]

    # First call: winner advanced to next_match_id
    assert created_calls[0].tournament_match_id == next_match_id
    assert created_calls[0].participant_id == winner_pid

    # Second call: loser advanced to loser_next_match_id
    assert created_calls[1].tournament_match_id == loser_next_match_id
    assert created_calls[1].participant_id == loser_pid


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_no_loser_advance_without_loser_next_match(
    mock_repo,
):
    """SE match (no loser_next_match_id) does not advance
    loser.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    match = _create_match(
        confirmed_by=None,
        next_match_id=next_match_id,
        loser_next_match_id=None,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Only 1 create_match_contestant call (winner only)
    assert mock_repo.create_match_contestant.call_count == 1
    created = mock_repo.create_match_contestant.call_args[0][0]
    assert created.tournament_match_id == next_match_id


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_match_de_auto_advances_lb_defwin(mock_repo):
    """When a loser is routed to an LB match that has only
    1 incoming feed (structural DEFWIN due to WBR0 DEFWIN
    nullification), the lone contestant auto-advances to
    the next LB round.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    loser_next_match_id = TournamentMatchID(generate_uuid())
    lb_next_match_id = TournamentMatchID(generate_uuid())

    match = _create_match(
        confirmed_by=None,
        next_match_id=next_match_id,
        loser_next_match_id=loser_next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    loser_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(participant_id=loser_pid, score=5),
    ]

    # The LB match that loser is sent to.
    lb_match = _create_match(
        match_id=loser_next_match_id,
        confirmed_by=None,
        bracket=Bracket.LOSERS,
        next_match_id=lb_next_match_id,
    )

    mock_repo.get_match_for_update.return_value = match
    mock_repo.find_match.return_value = lb_match

    # After the loser is created in the LB match, it has
    # exactly 1 contestant (the newly routed loser).
    lb_sole_contestant = _create_match_contestant(
        participant_id=loser_pid,
    )

    # get_contestants_for_match calls:
    # 1) main match contestants (confirm_match validation)
    # 2) lb match contestants (DEFWIN auto-advance check)
    mock_repo.get_contestants_for_match.side_effect = [
        contestants,
        [lb_sole_contestant],
    ]

    # Only 1 feed into this LB match (structural DEFWIN).
    mock_repo.count_incoming_feeds.return_value = 1

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # 3 create_match_contestant calls:
    # 1) winner to next_match
    # 2) loser to loser_next_match
    # 3) auto-advance from LB DEFWIN to lb_next_match
    assert mock_repo.create_match_contestant.call_count == 3

    calls = [
        call.args[0]
        for call in (mock_repo.create_match_contestant.call_args_list)
    ]

    # Third call: auto-advanced to lb_next_match_id.
    assert calls[2].tournament_match_id == lb_next_match_id
    assert calls[2].participant_id == loser_pid


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_retracts_loser_from_lb(mock_repo):
    """Unconfirming a WB match removes the loser from the LB
    match.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    loser_next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=next_match_id,
        loser_next_match_id=loser_next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    loser_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(participant_id=loser_pid, score=5),
    ]

    # next_match and loser_next_match are both unconfirmed
    next_match = _create_match(match_id=next_match_id, confirmed_by=None)
    loser_next_match = _create_match(
        match_id=loser_next_match_id, confirmed_by=None
    )

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_match.side_effect = [next_match, loser_next_match]
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Two delete_contestant_from_match calls:
    # winner retracted + loser retracted
    assert mock_repo.delete_contestant_from_match.call_count == 2

    calls = mock_repo.delete_contestant_from_match.call_args_list

    # First call: winner retracted from next_match
    assert calls[0].args[0] == next_match_id
    assert calls[0].kwargs['participant_id'] == winner_pid

    # Second call: loser retracted from loser_next_match
    assert calls[1].args[0] == loser_next_match_id
    assert calls[1].kwargs['participant_id'] == loser_pid


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_cascades_through_loser_bracket(
    mock_repo,
):
    """Recursive unconfirm of downstream LB match before
    loser retraction.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    loser_next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=next_match_id,
        loser_next_match_id=loser_next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    loser_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(participant_id=loser_pid, score=5),
    ]

    # next_match is unconfirmed (no WB cascade needed)
    next_match = _create_match(match_id=next_match_id, confirmed_by=None)

    # loser_next_match IS confirmed — triggers LB cascade
    lb_confirmed_by = UserID(generate_uuid())
    loser_next_match = _create_match(
        match_id=loser_next_match_id,
        confirmed_by=lb_confirmed_by,
    )

    # LB cascade contestants (draw => no further cascade)
    lb_contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    # get_match_for_update calls:
    # 0) unconfirm_match top-level lock (passed as _match)
    # 1) loser_next_match (recursive _unconfirm_match_impl)
    mock_repo.get_match_for_update.side_effect = [
        match,
        loser_next_match,
    ]
    # get_match calls (downstream lookups):
    # 1) next_match (winner cascade check)
    # 2) loser_next_match (loser cascade check)
    mock_repo.get_match.side_effect = [
        next_match,
        loser_next_match,
    ]

    # get_contestants_for_match calls:
    # 1) main match contestants
    # 2) LB match contestants (recursive unconfirm)
    mock_repo.get_contestants_for_match.side_effect = [
        contestants,
        lb_contestants,
    ]

    # Terminal match reversion (LB match has next_match_id=None)
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # unconfirm_match called for both main match and
    # cascaded LB match
    assert mock_repo.unconfirm_match.call_count == 2
    unconfirm_calls = mock_repo.unconfirm_match.call_args_list
    # LB match unconfirmed first (cascade), then main
    assert unconfirm_calls[0].args[0] == loser_next_match_id
    assert unconfirm_calls[1].args[0] == MATCH_ID


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_unconfirm_match_no_loser_retract_without_loser_next_match(
    mock_repo,
):
    """SE/RR unconfirm (no loser_next_match_id) does not
    retract loser.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=next_match_id,
        loser_next_match_id=None,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    next_match = _create_match(match_id=next_match_id, confirmed_by=None)

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_match.return_value = next_match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # Only 1 delete call (winner only, no loser retraction)
    assert mock_repo.delete_contestant_from_match.call_count == 1
    call = mock_repo.delete_contestant_from_match.call_args
    assert call.args[0] == next_match_id
    assert call.kwargs['participant_id'] == winner_pid


@patch(
    'byceps.services.lan_tournament.tournament_match_service.match_unconfirmed'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_match_single_commit_for_cascade(
    mock_repo,
    mock_signal,
):
    """Cascade unconfirm uses exactly one commit, not one
    per recursion level.
    """
    # Build a two-level chain: match_a -> match_b (confirmed).
    next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())

    match_a = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    contestants_a = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    # match_b is also confirmed — triggers cascade.
    match_b_confirmed_by = UserID(generate_uuid())
    match_b = _create_match(
        match_id=next_match_id,
        confirmed_by=match_b_confirmed_by,
    )

    # match_b contestants (draw => no further cascade).
    contestants_b = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    # get_match_for_update calls:
    # 0) unconfirm_match top-level lock (passed as _match)
    # 1) match_b (recursive _unconfirm_match_impl)
    mock_repo.get_match_for_update.side_effect = [match_a, match_b]
    # get_match calls (downstream cascade check):
    # 1) match_b (check winner cascade — confirmed)
    mock_repo.get_match.return_value = match_b

    # get_contestants_for_match calls:
    # 1) match_a contestants
    # 2) match_b contestants (recursive call)
    mock_repo.get_contestants_for_match.side_effect = [
        contestants_a,
        contestants_b,
    ]

    # Terminal match reversion (match_b has next_match_id=None)
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # The critical assertion: exactly ONE commit for
    # the entire cascade.
    assert mock_repo.commit_session.call_count == 1

    # Both matches were unconfirmed via the repo.
    assert mock_repo.unconfirm_match.call_count == 2

    # Events dispatched after the single commit.
    assert mock_signal.send.call_count == 2


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.db'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_detects_circular_reference(
    mock_repo, _mock_db
):
    """Circular next_match_id linkage returns Err
    instead of recursing forever.
    """
    # Two matches that point to each other:
    # match_a.next_match_id -> match_b
    # match_b.next_match_id -> match_a
    match_b_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())

    match_a = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=match_b_id,
    )
    match_b = _create_match(
        match_id=match_b_id,
        confirmed_by=confirmed_by,
        next_match_id=MATCH_ID,  # points back to match_a
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    contestants_with_winner = [
        _create_match_contestant(
            participant_id=winner_pid, score=10
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(
                generate_uuid()
            ),
            score=5,
        ),
    ]

    # get_match_for_update calls:
    # 0) unconfirm_match top-level lock (passed as _match)
    # 1) match_b (recursive _unconfirm_match_impl)
    mock_repo.get_match_for_update.side_effect = [
        match_a,
        match_b,
    ]
    # get_match: match_b (cascade check for match_a)
    mock_repo.get_match.return_value = match_b
    mock_repo.get_contestants_for_match.side_effect = [
        contestants_with_winner,
        contestants_with_winner,
    ]

    result = tournament_match_service.unconfirm_match(
        MATCH_ID, USER_ID
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Circular match reference detected.'
    )


# -------------------------------------------------------------------- #
# add_comment
# -------------------------------------------------------------------- #


@patch('byceps.util.uuid.generate_uuid7')
@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_add_comment_valid(mock_repo, mock_uuid):
    """Test adding a valid comment."""
    mock_uuid.return_value = generate_uuid()

    result = tournament_match_service.add_comment(
        MATCH_ID, USER_ID, 'Great match!'
    )

    assert result.is_ok()
    mock_repo.create_match_comment.assert_called_once()
    call_args = mock_repo.create_match_comment.call_args[0][0]
    assert call_args.tournament_match_id == MATCH_ID
    assert call_args.created_by == USER_ID
    assert call_args.comment == 'Great match!'


def test_add_comment_too_long():
    """Test that comments exceeding 1000 chars are rejected."""
    long_comment = 'x' * 1001

    result = tournament_match_service.add_comment(
        MATCH_ID, USER_ID, long_comment
    )

    assert result.is_err()
    assert '1000' in result.unwrap_err()


def test_add_comment_at_limit():
    """Test that exactly 1000 char comment is accepted."""
    limit_comment = 'x' * 1000

    with (
        patch(
            'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
        ) as mock_repo,
        patch('byceps.util.uuid.generate_uuid7'),
    ):
        result = tournament_match_service.add_comment(
            MATCH_ID, USER_ID, limit_comment
        )

        assert result.is_ok()
        mock_repo.create_match_comment.assert_called_once()


# -------------------------------------------------------------------- #
# update_comment
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_update_comment_valid(mock_repo):
    """Test updating a comment with valid text."""
    comment_id = TournamentMatchCommentID(generate_uuid())

    result = tournament_match_service.update_comment(comment_id, 'New comment')

    assert result.is_ok()
    mock_repo.update_match_comment.assert_called_once_with(
        comment_id, 'New comment'
    )


def test_update_comment_too_long():
    """Test that updating with too long text fails."""
    comment_id = TournamentMatchCommentID(generate_uuid())
    long_comment = 'x' * 1001

    result = tournament_match_service.update_comment(comment_id, long_comment)

    assert result.is_err()
    assert '1000' in result.unwrap_err()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_update_nonexistent_comment(mock_repo):
    """Test that updating nonexistent comment raises."""
    comment_id = TournamentMatchCommentID(generate_uuid())

    mock_repo.update_match_comment.side_effect = ValueError(
        f'Unknown comment ID "{comment_id}"'
    )

    with pytest.raises(ValueError, match='Unknown comment ID'):
        tournament_match_service.update_comment(comment_id, 'New text')


# -------------------------------------------------------------------- #
# confirm_match — terminal match auto-complete
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_terminal_match_sets_winner(mock_repo):
    """Confirming a terminal match stores the winner on the tournament."""
    winner_pid = TournamentParticipantID(generate_uuid())
    match = _create_match(confirmed_by=None, next_match_id=None)
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.set_tournament_winner.assert_called_once_with(
        TOURNAMENT_ID,
        winner_team_id=None,
        winner_participant_id=winner_pid,
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_terminal_match_completes_tournament(mock_repo):
    """Confirming a terminal match sets status to COMPLETED."""
    match = _create_match(confirmed_by=None, next_match_id=None)
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.set_tournament_status_flush.assert_called_once_with(
        TOURNAMENT_ID,
        TournamentStatus.COMPLETED,
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_non_terminal_match_does_not_complete(mock_repo):
    """Non-terminal match does not trigger winner/status updates."""
    next_match_id = TournamentMatchID(generate_uuid())
    match = _create_match(confirmed_by=None, next_match_id=next_match_id)
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.set_tournament_winner.assert_not_called()
    mock_repo.set_tournament_status_flush.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service.tournament_repository'
)
def test_confirm_draw_does_not_complete_tournament(mock_repo):
    """Draw result on terminal match does not trigger completion."""
    tournament = _create_tournament(
        tournament_mode=TournamentMode.ROUND_ROBIN,
    )
    match = _create_match(confirmed_by=None, next_match_id=None)

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.get_tournament.return_value = tournament

    result = tournament_match_service.confirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.set_tournament_winner.assert_not_called()
    mock_repo.set_tournament_status_flush.assert_not_called()


# -------------------------------------------------------------------- #
# unconfirm_match — LB auto-advance retraction
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_retracts_lb_auto_advance(mock_repo):
    """Unconfirming a WB match retracts the loser's
    auto-advance from the LB match's next match.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    loser_next_match_id = TournamentMatchID(generate_uuid())
    lb_next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())

    match = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=next_match_id,
        loser_next_match_id=loser_next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    loser_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(participant_id=loser_pid, score=5),
    ]

    # next_match is unconfirmed
    next_match = _create_match(match_id=next_match_id, confirmed_by=None)
    # loser_next_match has a downstream match (lb_next_match)
    loser_next_match = _create_match(
        match_id=loser_next_match_id,
        confirmed_by=None,
        next_match_id=lb_next_match_id,
    )

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_match.side_effect = [
        next_match,
        loser_next_match,
    ]
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    # 3 delete_contestant_from_match calls:
    # 1) winner retracted from next_match
    # 2) loser retracted from loser_next_match
    # 3) loser auto-advance retracted from lb_next_match
    assert mock_repo.delete_contestant_from_match.call_count == 3

    calls = mock_repo.delete_contestant_from_match.call_args_list

    # Winner retracted from next_match
    assert calls[0].args[0] == next_match_id
    assert calls[0].kwargs['participant_id'] == winner_pid

    # Loser retracted from loser_next_match
    assert calls[1].args[0] == loser_next_match_id
    assert calls[1].kwargs['participant_id'] == loser_pid

    # Loser auto-advance retracted from lb_next_match
    assert calls[2].args[0] == lb_next_match_id
    assert calls[2].kwargs['participant_id'] == loser_pid


# -------------------------------------------------------------------- #
# unconfirm_match — terminal match tournament state reversion
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_terminal_match_clears_winner(mock_repo):
    """Unconfirming a terminal match clears the tournament
    winner.
    """
    confirmed_by = UserID(generate_uuid())
    match = _create_match(confirmed_by=confirmed_by, next_match_id=None)
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.set_tournament_winner.assert_called_once_with(
        TOURNAMENT_ID,
        winner_team_id=None,
        winner_participant_id=None,
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_terminal_match_reverts_status_to_ongoing(
    mock_repo,
):
    """Unconfirming a terminal match reverts status to
    ONGOING.
    """
    confirmed_by = UserID(generate_uuid())
    match = _create_match(confirmed_by=confirmed_by, next_match_id=None)
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_repo.set_tournament_status_flush.assert_called_once_with(
        TOURNAMENT_ID,
        TournamentStatus.ONGOING,
    )


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_uncompleted'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_terminal_match_dispatches_uncompleted_event(
    mock_repo,
    mock_signal,
):
    """Unconfirming a terminal match dispatches
    TournamentUncompletedEvent.
    """
    confirmed_by = UserID(generate_uuid())
    match = _create_match(confirmed_by=confirmed_by, next_match_id=None)
    tournament = _create_tournament(
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = contestants
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(None)

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()

    mock_signal.send.assert_called_once()
    event = mock_signal.send.call_args[1]['event']
    assert isinstance(event, TournamentUncompletedEvent)
    assert event.tournament_id == TOURNAMENT_ID


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_uncompleted'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_non_terminal_cascading_to_gf_dispatches_uncompleted_event(
    mock_repo,
    mock_signal,
):
    """Unconfirming a non-terminal match that cascades to
    a confirmed GF (terminal) still dispatches
    TournamentUncompletedEvent.

    Scenario: WB semi → GF (terminal, both confirmed).
    Unconfirm WB semi.  The cascade unconfirms GF which
    reverts tournament state.  The uncompleted flag must
    propagate back up to the top-level caller.
    """
    gf_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())

    # WB semifinal: next_match_id → GF
    wb_semi = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=gf_id,
    )

    # GF: terminal (next_match_id=None), confirmed
    gf_confirmed_by = UserID(generate_uuid())
    gf_match = _create_match(
        match_id=gf_id,
        confirmed_by=gf_confirmed_by,
        next_match_id=None,
    )

    tournament = _create_tournament(
        tournament_mode=TournamentMode.DOUBLE_ELIMINATION,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    contestants_with_winner = [
        _create_match_contestant(
            participant_id=winner_pid, score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(
                generate_uuid(),
            ),
            score=5,
        ),
    ]

    # get_match_for_update calls:
    # 0) unconfirm_match top-level lock (passed as _match)
    # 1) gf_match (recursive _unconfirm_match_impl)
    mock_repo.get_match_for_update.side_effect = [
        wb_semi,
        gf_match,
    ]
    # get_match: GF lookup during cascade check
    mock_repo.get_match.return_value = gf_match
    # Contestants for wb_semi, then for GF
    mock_repo.get_contestants_for_match.side_effect = [
        contestants_with_winner,
        contestants_with_winner,
    ]
    mock_repo.get_tournament.return_value = tournament
    mock_repo.set_tournament_winner.return_value = Ok(None)
    mock_repo.set_tournament_status_flush.return_value = Ok(
        None,
    )

    result = tournament_match_service.unconfirm_match(
        MATCH_ID, USER_ID,
    )

    assert result.is_ok()

    # The critical assertion: TournamentUncompletedEvent
    # must be dispatched even though we unconfirmed a
    # non-terminal match.
    mock_signal.send.assert_called_once()
    event = mock_signal.send.call_args[1]['event']
    assert isinstance(event, TournamentUncompletedEvent)
    assert event.tournament_id == TOURNAMENT_ID


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_uncompleted'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_non_terminal_match_no_uncompleted_event(
    mock_repo,
    mock_signal,
):
    """Unconfirming a non-terminal match does NOT dispatch
    TournamentUncompletedEvent.
    """
    next_match_id = TournamentMatchID(generate_uuid())
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=next_match_id,
    )

    winner_pid = TournamentParticipantID(generate_uuid())
    contestants = [
        _create_match_contestant(participant_id=winner_pid, score=10),
        _create_match_contestant(
            participant_id=TournamentParticipantID(generate_uuid()),
            score=5,
        ),
    ]

    next_match = _create_match(match_id=next_match_id, confirmed_by=None)

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_match.return_value = next_match
    mock_repo.get_contestants_for_match.return_value = contestants

    result = tournament_match_service.unconfirm_match(MATCH_ID, USER_ID)

    assert result.is_ok()
    mock_signal.send.assert_not_called()


@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_uncompleted'
)
@patch(
    'byceps.services.lan_tournament.tournament_match_service'
    '.tournament_repository'
)
def test_unconfirm_rr_terminal_match_no_uncompleted_event(
    mock_repo,
    mock_signal,
):
    """Unconfirming a terminal match in round-robin mode
    does NOT dispatch TournamentUncompletedEvent.

    Round-robin matches always have next_match_id=None, but
    RR tournaments don't auto-complete, so reverting tournament
    state is not applicable.
    """
    confirmed_by = UserID(generate_uuid())
    match = _create_match(
        confirmed_by=confirmed_by,
        next_match_id=None,
    )
    tournament = _create_tournament(
        tournament_mode=TournamentMode.ROUND_ROBIN,
    )

    contestants = [
        _create_match_contestant(
            participant_id=TournamentParticipantID(
                generate_uuid()
            ),
            score=10,
        ),
        _create_match_contestant(
            participant_id=TournamentParticipantID(
                generate_uuid()
            ),
            score=5,
        ),
    ]

    mock_repo.get_match_for_update.return_value = match
    mock_repo.get_tournament.return_value = tournament
    mock_repo.get_contestants_for_match.return_value = (
        contestants
    )

    result = tournament_match_service.unconfirm_match(
        MATCH_ID, USER_ID
    )

    assert result.is_ok()
    mock_signal.send.assert_not_called()


# -------------------------------------------------------------------- #
# _propagate_dead_lb_matches
# -------------------------------------------------------------------- #


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_propagate_dead_lb_clears_zero_feed_matches(
    mock_repo,
):
    """LB matches with 0 incoming feeds get next_match_id
    cleared.
    """
    lb_ids: list[list[TournamentMatchID]] = [
        [],  # index 0 unused
        [
            TournamentMatchID(generate_uuid()),
            TournamentMatchID(generate_uuid()),
        ],
        [TournamentMatchID(generate_uuid())],
    ]

    # LBR1 match 0: 0 feeds (dead), match 1: 1 feed (alive)
    # LBR2 match 0: 0 feeds (dead, cascaded from LBR1)
    mock_repo.count_incoming_feeds.side_effect = [
        0,
        1,
        0,
    ]

    tournament_match_service._propagate_dead_lb_matches(
        lb_ids, lb_rounds=2
    )

    assert mock_repo.clear_next_match_id.call_count == 2
    cleared_ids = [
        call.args[0]
        for call in mock_repo.clear_next_match_id.call_args_list
    ]
    assert cleared_ids == [lb_ids[1][0], lb_ids[2][0]]


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_propagate_dead_lb_no_op_when_all_alive(
    mock_repo,
):
    """No LB matches cleared when all have incoming feeds."""
    lb_ids: list[list[TournamentMatchID]] = [
        [],
        [TournamentMatchID(generate_uuid())],
        [TournamentMatchID(generate_uuid())],
    ]

    mock_repo.count_incoming_feeds.return_value = 1

    tournament_match_service._propagate_dead_lb_matches(
        lb_ids, lb_rounds=2
    )

    mock_repo.clear_next_match_id.assert_not_called()


@patch(
    'byceps.services.lan_tournament'
    '.tournament_match_service.tournament_repository'
)
def test_propagate_dead_lb_all_dead(mock_repo):
    """All LB matches dead when every feed count is 0."""
    lb_ids: list[list[TournamentMatchID]] = [
        [],
        [
            TournamentMatchID(generate_uuid()),
            TournamentMatchID(generate_uuid()),
        ],
        [TournamentMatchID(generate_uuid())],
        [TournamentMatchID(generate_uuid())],
    ]

    mock_repo.count_incoming_feeds.return_value = 0

    tournament_match_service._propagate_dead_lb_matches(
        lb_ids, lb_rounds=3
    )

    # All 4 LB matches should be cleared.
    assert mock_repo.clear_next_match_id.call_count == 4


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


def _create_tournament(**kwargs) -> Tournament:
    """Create a tournament for testing."""
    defaults = {
        'id': TOURNAMENT_ID,
        'party_id': PARTY_ID,
        'name': 'Test Tournament',
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
        'contestant_type': None,
        'tournament_status': None,
        'tournament_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _create_match(
    *,
    match_id: TournamentMatchID | None = None,
    confirmed_by: UserID | None = None,
    round: int | None = None,
    next_match_id: TournamentMatchID | None = None,
    bracket: Bracket | None = None,
    loser_next_match_id: TournamentMatchID | None = None,
) -> TournamentMatch:
    """Create a tournament match for testing."""
    if match_id is None:
        match_id = MATCH_ID
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=round,
        next_match_id=next_match_id,
        confirmed_by=confirmed_by,
        created_at=NOW,
        bracket=bracket,
        loser_next_match_id=loser_next_match_id,
    )


def _create_mock_participant(participant_id):
    """Create a mock participant for testing."""
    mock = Mock()
    mock.id = participant_id
    return mock


def _create_mock_team(team_id):
    """Create a mock team for testing."""
    mock = Mock()
    mock.id = team_id
    return mock


def _create_match_contestant(
    contestant_id=None,
    participant_id=None,
    team_id=None,
    score=None,
):
    """Create a match contestant for testing."""
    if contestant_id is None:
        contestant_id = TournamentMatchToContestantID(generate_uuid())

    return TournamentMatchToContestant(
        id=contestant_id,
        tournament_match_id=MATCH_ID,
        team_id=team_id,
        participant_id=participant_id,
        score=score,
        created_at=NOW,
    )
