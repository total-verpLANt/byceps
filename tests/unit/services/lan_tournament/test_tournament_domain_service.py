"""
tests.unit.services.lan_tournament.test_tournament_domain_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from datetime import datetime

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.tournament_domain_service import (
    _standard_seed_order,
    create_tournament,
    determine_match_winner,
    validate_participant_count,
    validate_team_count,
)
from byceps.services.party.models import PartyID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


# -------------------------------------------------------------------- #
# create_tournament


def test_create_tournament_with_minimal_args():
    party_id = PartyID('lan-2025')

    tournament, event = create_tournament(party_id, 'My Tournament')

    assert tournament.id is not None
    assert tournament.party_id == party_id
    assert tournament.name == 'My Tournament'
    assert tournament.game is None
    assert tournament.description is None
    assert tournament.image_url is None
    assert tournament.ruleset is None
    assert tournament.start_time is None
    assert tournament.created_at is not None
    assert tournament.min_players is None
    assert tournament.max_players is None
    assert tournament.min_teams is None
    assert tournament.max_teams is None
    assert tournament.min_players_in_team is None
    assert tournament.max_players_in_team is None
    assert tournament.contestant_type is None
    assert tournament.tournament_status is None
    assert tournament.tournament_mode is None

    assert event.tournament_id == tournament.id
    assert event.occurred_at == tournament.created_at
    assert event.initiator is None


def test_create_tournament_with_all_args():
    party_id = PartyID('lan-2025')
    start = datetime(2025, 7, 1, 18, 0)

    tournament, event = create_tournament(
        party_id,
        'CS2 Championship',
        game='Counter-Strike 2',
        description='Big tournament',
        image_url='https://example.com/img.png',
        ruleset='Standard rules',
        start_time=start,
        min_players=10,
        max_players=64,
        min_teams=4,
        max_teams=16,
        min_players_in_team=5,
        max_players_in_team=5,
        contestant_type=ContestantType.TEAM,
        tournament_status=TournamentStatus.DRAFT,
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    assert tournament.name == 'CS2 Championship'
    assert tournament.game == 'Counter-Strike 2'
    assert tournament.description == 'Big tournament'
    assert tournament.start_time == start
    assert tournament.min_players == 10
    assert tournament.max_players == 64
    assert tournament.min_teams == 4
    assert tournament.max_teams == 16
    assert tournament.min_players_in_team == 5
    assert tournament.max_players_in_team == 5
    assert tournament.contestant_type == ContestantType.TEAM
    assert tournament.tournament_status == TournamentStatus.DRAFT
    assert tournament.tournament_mode == TournamentMode.SINGLE_ELIMINATION

    assert event.tournament_id == tournament.id


def test_create_tournament_generates_unique_ids():
    party_id = PartyID('lan-2025')

    tournament1, _ = create_tournament(party_id, 'Tournament 1')
    tournament2, _ = create_tournament(party_id, 'Tournament 2')

    assert tournament1.id != tournament2.id


# -------------------------------------------------------------------- #
# validate_participant_count


@pytest.mark.parametrize(
    ('max_players', 'current_count', 'expected_ok'),
    [
        (None, 0, True),  # no limit, zero players
        (None, 100, True),  # no limit, many players
        (10, 0, True),  # under limit
        (10, 9, True),  # one below limit
        (10, 10, False),  # at limit
        (10, 11, False),  # over limit
        (1, 0, True),  # single-slot tournament, empty
        (1, 1, False),  # single-slot tournament, full
    ],
)
def test_validate_participant_count(max_players, current_count, expected_ok):
    tournament = _create_tournament(max_players=max_players)

    result = validate_participant_count(tournament, current_count)

    assert result.is_ok() == expected_ok
    if not expected_ok:
        assert 'full' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# validate_team_count


@pytest.mark.parametrize(
    ('max_teams', 'current_count', 'expected_ok'),
    [
        (None, 0, True),  # no limit, zero teams
        (None, 50, True),  # no limit, many teams
        (8, 0, True),  # under limit
        (8, 7, True),  # one below limit
        (8, 8, False),  # at limit
        (8, 9, False),  # over limit
        (1, 0, True),  # single team allowed, empty
        (1, 1, False),  # single team allowed, full
    ],
)
def test_validate_team_count(max_teams, current_count, expected_ok):
    tournament = _create_tournament(max_teams=max_teams)

    result = validate_team_count(tournament, current_count)

    assert result.is_ok() == expected_ok
    if not expected_ok:
        assert 'team' in result.unwrap_err().lower()


# -------------------------------------------------------------------- #
# determine_match_winner


def test_determine_match_winner_clear_win():
    match_id = TournamentMatchID(generate_uuid())
    winner = _create_contestant(match_id=match_id, score=10)
    loser = _create_contestant(match_id=match_id, score=5)

    result = determine_match_winner([winner, loser])

    assert result.is_ok()
    assert result.unwrap() == winner


def test_determine_match_winner_clear_win_reversed_order():
    match_id = TournamentMatchID(generate_uuid())
    winner = _create_contestant(match_id=match_id, score=10)
    loser = _create_contestant(match_id=match_id, score=5)

    result = determine_match_winner([loser, winner])

    assert result.is_ok()
    assert result.unwrap() == winner


def test_determine_match_winner_tie_error():
    match_id = TournamentMatchID(generate_uuid())
    c1 = _create_contestant(match_id=match_id, score=7)
    c2 = _create_contestant(match_id=match_id, score=7)

    result = determine_match_winner([c1, c2])

    assert result.is_err()
    assert 'tied' in result.unwrap_err().lower()


def test_determine_match_winner_missing_scores_error():
    match_id = TournamentMatchID(generate_uuid())
    c1 = _create_contestant(match_id=match_id, score=10)
    c2 = _create_contestant(match_id=match_id, score=None)

    result = determine_match_winner([c1, c2])

    assert result.is_err()
    assert 'scores' in result.unwrap_err().lower()


def test_determine_match_winner_too_few_contestants():
    match_id = TournamentMatchID(generate_uuid())
    c1 = _create_contestant(match_id=match_id, score=10)

    result = determine_match_winner([c1])

    assert result.is_err()
    assert '2 contestants' in result.unwrap_err().lower()


def test_determine_match_winner_empty_list():
    result = determine_match_winner([])

    assert result.is_err()


# -------------------------------------------------------------------- #
# _standard_seed_order


def test_standard_seed_order_4_players():
    order = _standard_seed_order(4)

    assert len(order) == 4
    # For bracket_size=4: [0, 3, 1, 2]
    # matchup 0: seeds 0 vs 3 (1v4)
    # matchup 1: seeds 1 vs 2 (2v3)
    assert order == [0, 3, 1, 2]


def test_standard_seed_order_8_players():
    order = _standard_seed_order(8)

    assert len(order) == 8
    # For bracket_size=8: [0, 7, 3, 4, 1, 6, 2, 5]
    # matchup 0: seeds 0 vs 7 (1v8)
    # matchup 1: seeds 3 vs 4 (4v5)
    # matchup 2: seeds 1 vs 6 (2v7)
    # matchup 3: seeds 2 vs 5 (3v6)
    assert order == [0, 7, 3, 4, 1, 6, 2, 5]


def test_standard_seed_order_16_players():
    order = _standard_seed_order(16)

    assert len(order) == 16
    # Top seed (0) faces bottom seed (15)
    assert order[0] == 0
    assert order[1] == 15
    # Every seed appears exactly once
    assert sorted(order) == list(range(16))


def test_standard_seed_order_2_players():
    order = _standard_seed_order(2)

    assert order == [0, 1]


def test_standard_seed_order_1_player():
    order = _standard_seed_order(1)

    assert order == [0]


# -------------------------------------------------------------------- #
# helpers


def _create_contestant(
    *,
    match_id: TournamentMatchID | None = None,
    score: int | None = None,
) -> TournamentMatchToContestant:
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=None,
        score=score,
        created_at=NOW,
    )


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
