"""
tests.unit.services.lan_tournament.test_match_due_policy
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime
from typing import Any

import pytest

from byceps.services.lan_tournament import tournament_match_service
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (  # noqa: E501
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
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    _winners_target,
    derive_due_match_ids,
    pairing_key,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


ONE_V_ONE = GameFormat.ONE_V_ONE
FREE_FOR_ALL = GameFormat.FREE_FOR_ALL
HIGHSCORE = GameFormat.HIGHSCORE
SINGLE = EliminationMode.SINGLE_ELIMINATION
DOUBLE = EliminationMode.DOUBLE_ELIMINATION
ROUND_ROBIN = EliminationMode.ROUND_ROBIN
WB = Bracket.WINNERS
LB = Bracket.LOSERS
GF = Bracket.GRAND_FINAL

NOW = datetime(2026, 10, 3, 18, 0)


def make_tournament(
    game_format: GameFormat,
    elimination_mode: EliminationMode,
    *,
    status: TournamentStatus | None = TournamentStatus.ONGOING,
    **overrides,
) -> Tournament:
    values: dict[str, Any] = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('party-1'),
        'name': 'Test Cup',
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
        'contestant_type': ContestantType.SOLO,
        'tournament_status': status,
        'game_format': game_format,
        'elimination_mode': elimination_mode,
    }
    values.update(overrides)
    return Tournament(**values)


class Board:
    """The matches of one tournament, with their contestants."""

    def __init__(self, tournament: Tournament) -> None:
        self.tournament = tournament
        self.matches: list[TournamentMatch] = []
        self.contestants: dict[
            TournamentMatchID, list[TournamentMatchToContestant]
        ] = {}
        self.completed: set[TournamentMatchID] = set()

    def add(
        self,
        contestants: int = 2,
        *,
        complete: bool = False,
        confirmed: bool = False,
        round: int | None = None,
        group_order: int | None = None,
        bracket: Bracket | None = None,
        phase: int = 1,
        seeding_target: str | None = None,
    ) -> TournamentMatchID:
        match = TournamentMatch(
            id=TournamentMatchID(generate_uuid()),
            tournament_id=self.tournament.id,
            group_order=group_order,
            match_order=0,
            round=round,
            next_match_id=None,
            confirmed_by=UserID(generate_uuid()) if confirmed else None,
            created_at=NOW,
            bracket=bracket,
            phase=phase,
            seeding_target=seeding_target,
        )
        self.matches.append(match)
        self.contestants[match.id] = [
            make_contestant(match.id) for _ in range(contestants)
        ]
        if complete:
            self.completed.add(match.id)
        return match.id

    def add_many(self, count: int, *args, **kwargs) -> set[TournamentMatchID]:
        return {self.add(*args, **kwargs) for _ in range(count)}

    def due(self) -> frozenset[TournamentMatchID]:
        return derive_due_match_ids(
            self.tournament,
            self.matches,
            self.contestants,
            frozenset(self.completed),
        )


def make_contestant(
    match_id: TournamentMatchID,
    *,
    participant_id: TournamentParticipantID | None = None,
    team_id: TournamentTeamID | None = None,
) -> TournamentMatchToContestant:
    if participant_id is None and team_id is None:
        participant_id = TournamentParticipantID(generate_uuid())
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )


def make_ffa_lobby(
    board: Board, round: int, bracket: Bracket | None = None, **kwargs
) -> TournamentMatchID:
    kwargs.setdefault('complete', True)
    return board.add(4, round=round, bracket=bracket, **kwargs)


def test_rr_frontier_is_independent_per_phase_and_group():
    tournament = make_tournament(
        ONE_V_ONE,
        ROUND_ROBIN,
        playoff_game_format=ONE_V_ONE,
        playoff_elimination_mode=SINGLE,
    )
    board = Board(tournament)

    # Group 0 finished round 0, so round 1 is its frontier.
    board.add_many(2, group_order=0, round=0, confirmed=True)
    group_0_frontier = board.add_many(2, group_order=0, round=1)
    board.add_many(2, group_order=0, round=2)

    # Group 1 has played nothing yet, so round 0 is its frontier.
    group_1_frontier = board.add_many(2, group_order=1, round=0)
    board.add_many(2, group_order=1, round=1)

    # Group 2 is finished and has no frontier.
    for round in range(3):
        board.add(group_order=2, round=round, confirmed=True)

    # The playoffs follow knockout rules, whatever the groups did.
    playoff_matches = {
        board.add(phase=2, round=0, bracket=WB),
        board.add(phase=2, round=1, bracket=WB),
        board.add(phase=2, round=1, bracket=LB),
    }
    board.add(0, phase=2, round=2, bracket=WB)

    assert board.due() == group_0_frontier | group_1_frontier | playoff_matches


def test_plain_round_robin_is_a_single_group():
    board = Board(make_tournament(ONE_V_ONE, ROUND_ROBIN))
    board.add_many(3, round=0, confirmed=True)
    frontier = board.add_many(3, round=1)
    board.add_many(3, round=2)

    assert board.due() == frontier


def test_rr_round_stays_open_until_every_match_is_confirmed():
    board = Board(make_tournament(ONE_V_ONE, ROUND_ROBIN))
    confirmed = board.add(round=0, confirmed=True)
    still_open = board.add(round=0)
    board.add(round=1)

    assert board.due() == {still_open}
    assert confirmed not in board.due()


def test_rr_frontier_is_computed_before_the_playable_filter():
    board = Board(make_tournament(ONE_V_ONE, ROUND_ROBIN))
    board.add(1, round=0, group_order=0)
    board.add(round=1, group_order=0)
    board.add(round=1, group_order=0)

    assert board.due() == frozenset()


# fmt: off
@pytest.mark.parametrize(
    'scenario',
    [
        'single_track_earlier_round',
        'winners_round_replaced_by_the_merged_losers_round',
        'both_pools_with_their_companion_losers_round',
        'grand_final_consumes_both_pools',
        'each_pool_follows_its_own_latest_round',
        'unrelated_target_does_not_consume_the_round',
    ],
)
# fmt: on
def test_ffa_current_pool_round_excludes_consumed_sources(scenario):
    if scenario == 'single_track_earlier_round':
        board = Board(make_tournament(FREE_FOR_ALL, SINGLE))
        make_ffa_lobby(board, 0)
        make_ffa_lobby(board, 0)
        current = {make_ffa_lobby(board, 1), make_ffa_lobby(board, 1)}

    elif scenario == 'winners_round_replaced_by_the_merged_losers_round':
        board = Board(make_tournament(FREE_FOR_ALL, DOUBLE))
        make_ffa_lobby(board, 0, WB)
        make_ffa_lobby(board, 0, WB)
        current = {make_ffa_lobby(board, 0, LB, seeding_target='ffa:WB:1')}

    elif scenario == 'both_pools_with_their_companion_losers_round':
        board = Board(make_tournament(FREE_FOR_ALL, DOUBLE))
        make_ffa_lobby(board, 0, WB)
        make_ffa_lobby(board, 0, LB, seeding_target='ffa:WB:0')
        current = {
            make_ffa_lobby(board, 1, WB, seeding_target='ffa:WB:1'),
            make_ffa_lobby(board, 1, LB, seeding_target='ffa:WB:1'),
        }

    elif scenario == 'grand_final_consumes_both_pools':
        board = Board(make_tournament(FREE_FOR_ALL, DOUBLE))
        make_ffa_lobby(board, 2, WB)
        make_ffa_lobby(board, 2, LB)
        current = {make_ffa_lobby(board, 3, GF)}

    elif scenario == 'each_pool_follows_its_own_latest_round':
        board = Board(make_tournament(FREE_FOR_ALL, DOUBLE))
        make_ffa_lobby(board, 0, WB, confirmed=True)
        make_ffa_lobby(board, 1, LB, confirmed=True)
        current = {
            make_ffa_lobby(board, 1, WB),
            make_ffa_lobby(board, 3, LB),
        }

    else:
        board = Board(make_tournament(FREE_FOR_ALL, DOUBLE))
        current = {
            make_ffa_lobby(board, 0, WB),
            make_ffa_lobby(board, 0, LB, seeding_target='ffa:WB:2'),
            make_ffa_lobby(board, 0, WB, seeding_target='ffa:WB:1'),
        }

    assert board.due() == current


def test_ffa_lobby_must_be_complete_and_have_a_roster():
    board = Board(make_tournament(FREE_FOR_ALL, SINGLE))
    complete = make_ffa_lobby(board, 0)
    # An undersized lobby is legitimate once its roster is assembled.
    undersized = board.add(2, round=0, complete=True)
    board.add(4, round=0, complete=False)
    board.add(1, round=0, complete=True)
    board.add(0, round=0, complete=True)
    board.add(4, round=0, complete=True, confirmed=True)

    assert board.due() == {complete, undersized}


def test_ffa_waiting_target_matches_the_engine():
    for round_number in range(5):
        assert _winners_target(
            round_number
        ) == tournament_match_service.ffa_round_seeding_target(
            WB, round_number
        )


def _knockout_scenario():
    board = Board(make_tournament(ONE_V_ONE, SINGLE))
    due = {board.add(round=0), board.add(round=1)}
    board.add(1, round=0)
    board.add(0, round=0)
    board.add(2, round=0, confirmed=True)
    board.add(1, round=0, confirmed=True)
    board.add(1, round=2)
    board.add(0, round=3)
    return board, due


def _round_robin_scenario():
    board = Board(make_tournament(ONE_V_ONE, ROUND_ROBIN))
    # An unconfirmed walkover holds its group in that round, so group 1
    # has no demand although the next round there is fully assigned.
    board.add(1, round=0, group_order=1)
    board.add(round=1, group_order=1)
    # Group 0 has a playable sibling of the walkover and a future round.
    due = {board.add(round=0, group_order=0)}
    board.add(1, round=0, group_order=0)
    board.add(round=1, group_order=0)
    # Group 2 has an empty match and so no demand either.
    board.add(0, round=0, group_order=2)
    return board, due


def _free_for_all_scenario():
    board = Board(make_tournament(FREE_FOR_ALL, SINGLE))
    due = {make_ffa_lobby(board, 0)}
    # A lone contestant has no lobby, and a lobby may still be filling.
    board.add(1, round=0, complete=True)
    board.add(4, round=0, complete=False)
    # Rounds that are not generated yet have no lobbies at all.
    return board, due


# fmt: off
@pytest.mark.parametrize(
    'scenario',
    [_knockout_scenario, _round_robin_scenario, _free_for_all_scenario],
    ids=['knockout', 'round_robin', 'free_for_all'],
)
# fmt: on
def test_walkovers_partial_and_future_fixtures_are_not_due(scenario):
    board, expected = scenario()

    assert board.due() == expected


def test_matches_without_contestant_rows_are_not_due():
    board = Board(make_tournament(ONE_V_ONE, SINGLE))
    match_id = board.add(round=0)
    del board.contestants[match_id]

    assert board.due() == frozenset()


def test_an_open_slot_is_not_a_contestant():
    board = Board(make_tournament(ONE_V_ONE, SINGLE))
    match_id = board.add(1, round=0)
    board.contestants[match_id].append(
        TournamentMatchToContestant(
            id=TournamentMatchToContestantID(generate_uuid()),
            tournament_match_id=match_id,
            team_id=None,
            participant_id=None,
            score=None,
            created_at=NOW,
        )
    )

    assert board.due() == frozenset()


def test_knockout_demand_ignores_rounds_and_brackets():
    board = Board(make_tournament(ONE_V_ONE, DOUBLE))
    expected = {
        board.add(round=0, bracket=WB),
        board.add(round=3, bracket=WB),
        board.add(round=1, bracket=LB),
        board.add(round=0, bracket=GF),
    }

    assert board.due() == expected


# fmt: off
@pytest.mark.parametrize(
    ('status', 'has_demand'),
    [
        (None, False),
        (TournamentStatus.DRAFT, False),
        (TournamentStatus.REGISTRATION_OPEN, False),
        (TournamentStatus.REGISTRATION_CLOSED, False),
        (TournamentStatus.ONGOING, True),
        (TournamentStatus.PAUSED, True),
        (TournamentStatus.COMPLETED, False),
        (TournamentStatus.CANCELLED, False),
    ],
)
# fmt: on
def test_only_a_running_or_paused_tournament_has_demand(status, has_demand):
    board = Board(make_tournament(ONE_V_ONE, SINGLE, status=status))
    match_id = board.add(round=0)

    assert board.due() == ({match_id} if has_demand else frozenset())


def _highscore_with_playoffs_scenario():
    board = Board(
        make_tournament(
            HIGHSCORE,
            EliminationMode.NONE,
            playoff_game_format=FREE_FOR_ALL,
            playoff_elimination_mode=SINGLE,
        )
    )
    # A highscore phase has no matches; stray rows never count.
    board.add(2, round=0, complete=True)
    board.add(2, phase=1, complete=True)
    # The playoffs run lobbies, not pairs: four contestants are due.
    due = {
        board.add(4, phase=2, round=1, complete=True),
        board.add(3, phase=2, round=1, complete=True),
    }
    board.add(4, phase=2, round=0, complete=True)
    return board, due


def _highscore_without_playoffs_scenario():
    board = Board(make_tournament(HIGHSCORE, EliminationMode.NONE))
    board.add(2, phase=1, complete=True)
    board.add(2, phase=2, complete=True)
    return board, set()


def _playoff_phase_without_playoffs_scenario():
    board = Board(make_tournament(ONE_V_ONE, SINGLE))
    due = {board.add(round=0)}
    board.add(round=0, phase=2)
    board.add(round=0, phase=3)
    return board, due


def _round_robin_playoffs_scenario():
    board = Board(
        make_tournament(
            ONE_V_ONE,
            ROUND_ROBIN,
            playoff_game_format=ONE_V_ONE,
            playoff_elimination_mode=DOUBLE,
        )
    )
    due = {
        board.add(round=0, group_order=0),
        board.add(round=0, phase=2, bracket=WB),
        board.add(round=2, phase=2, bracket=LB),
    }
    board.add(round=1, group_order=0)
    return board, due


# fmt: off
@pytest.mark.parametrize(
    'scenario',
    [
        _highscore_with_playoffs_scenario,
        _highscore_without_playoffs_scenario,
        _playoff_phase_without_playoffs_scenario,
        _round_robin_playoffs_scenario,
    ],
    ids=[
        'highscore_with_ffa_playoffs',
        'highscore_without_playoffs',
        'no_playoff_phase_configured',
        'round_robin_with_knockout_playoffs',
    ],
)
# fmt: on
def test_highscore_uses_only_effective_playoff_matches(scenario):
    board, expected = scenario()

    assert board.due() == expected


def test_pairing_key_ignores_row_order_and_row_ids():
    match_id = TournamentMatchID(generate_uuid())
    first = make_contestant(match_id)
    second = make_contestant(match_id)
    same_pair_again = [
        make_contestant(match_id, participant_id=second.participant_id),
        make_contestant(match_id, participant_id=first.participant_id),
    ]

    key = pairing_key([first, second])

    assert key == pairing_key([second, first])
    assert key == pairing_key(same_pair_again)


def test_pairing_key_separates_other_contestants_and_kinds():
    match_id = TournamentMatchID(generate_uuid())
    first = make_contestant(match_id)
    second = make_contestant(match_id)
    third = make_contestant(match_id)
    team = make_contestant(match_id, team_id=TournamentTeamID(generate_uuid()))

    assert pairing_key([first, second]) != pairing_key([first, third])
    assert pairing_key([first, second]) != pairing_key([first])
    assert pairing_key([first, team]).count('team:') == 1
    assert pairing_key([first, team]).count('participant:') == 1


def test_pairing_key_of_a_match_without_real_contestants_is_empty():
    match_id = TournamentMatchID(generate_uuid())
    open_slot = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=None,
        score=None,
        created_at=NOW,
    )

    assert pairing_key([]) == ''
    assert pairing_key([open_slot]) == ''
