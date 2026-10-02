"""
tests.unit.services.lan_tournament.test_bracket_start_validation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Verify the structural bracket validation that gates a tournament
start.
"""

from datetime import datetime
from dataclasses import replace
from itertools import combinations
from unittest.mock import patch

import pytest

from byceps.services.lan_tournament.models.bracket import Bracket
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
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


def _make_id() -> TournamentMatchID:
    return TournamentMatchID(generate_uuid())


def _make_match(
    tournament_id: TournamentID,
    *,
    bracket: Bracket | None = None,
    round: int = 0,
    next_match_id: TournamentMatchID | None = None,
    loser_next_match_id: TournamentMatchID | None = None,
) -> TournamentMatch:
    return TournamentMatch(
        id=_make_id(),
        tournament_id=tournament_id,
        group_order=None,
        match_order=0,
        round=round,
        next_match_id=next_match_id,
        loser_next_match_id=loser_next_match_id,
        confirmed_by=None,
        created_at=NOW,
        bracket=bracket,
    )


def _make_contestant(
    match_id: TournamentMatchID,
    participant_number: int,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=TournamentParticipantID(
            generate_uuid()
        )
        if participant_number is not None
        else None,
        score=None,
        created_at=NOW,
    )


def _make_participant(
    tournament_id: TournamentID,
) -> TournamentParticipant:
    return TournamentParticipant(
        id=TournamentParticipantID(generate_uuid()),
        user_id=UserID(generate_uuid()),
        tournament_id=tournament_id,
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )


def _make_tournament(**kwargs) -> Tournament:
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
        'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
        'game_format': GameFormat.ONE_V_ONE,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)


def _contestants_by_match(matches, participant_numbers_by_match):
    contestants = {}
    for match in matches:
        numbers = participant_numbers_by_match.get(match.id, [])
        contestants[match.id] = [
            _make_contestant(match.id, n) for n in numbers
        ]
    return contestants


# -------------------------------------------------------------------- #
# graph builders mimicking the generators' output
# -------------------------------------------------------------------- #


def _build_se_bracket_6_players(tournament_id):
    """Build an 8-slot SE bracket for 6 players, with two DEFWIN slots."""
    final = _make_match(tournament_id, round=2)
    p3 = _make_match(tournament_id, bracket=Bracket.THIRD_PLACE, round=2)
    semis = [
        _make_match(
            tournament_id,
            round=1,
            next_match_id=final.id,
            loser_next_match_id=p3.id,
        )
        for _ in range(2)
    ]
    r0 = [
        _make_match(
            tournament_id, round=0, next_match_id=semis[m // 2].id
        )
        for m in range(4)
    ]

    matches = [final] + semis + [p3] + r0
    # 6 distinct participants across r0; two r0 slots are DEFWIN.
    numbers = {
        r0[0].id: [1, 2],
        r0[1].id: [3],
        r0[2].id: [4],
        r0[3].id: [],
        semis[0].id: [1, 3],
        semis[1].id: [4, 5],
        final.id: [1, 4],
        p3.id: [3, 5],
    }
    return matches, numbers


def _build_de_bracket_8_players(tournament_id):
    """DE with 8 players: WB rounds 4/2/1, LB rounds 2/2/2/1, GF."""
    gf = _make_match(tournament_id, bracket=Bracket.GRAND_FINAL, round=0)

    lb_r4 = [
        _make_match(
            tournament_id,
            bracket=Bracket.LOSERS,
            round=4,
            next_match_id=gf.id,
        )
    ]
    lb_r3 = [
        _make_match(
            tournament_id, bracket=Bracket.LOSERS, round=3, next_match_id=lb_r4[0].id
        )
        for _ in range(2)
    ]
    lb_r2 = [
        _make_match(
            tournament_id,
            bracket=Bracket.LOSERS,
            round=2,
            next_match_id=lb_r3[m // 2].id,
        )
        for m in range(2)
    ]
    lb_r1 = [
        _make_match(
            tournament_id, bracket=Bracket.LOSERS, round=1, next_match_id=lb_r2[m].id
        )
        for m in range(2)
    ]

    wb_final = _make_match(
        tournament_id,
        bracket=Bracket.WINNERS,
        round=2,
        next_match_id=gf.id,
        loser_next_match_id=lb_r4[0].id,
    )
    wb_r1 = [
        _make_match(
            tournament_id,
            bracket=Bracket.WINNERS,
            round=1,
            next_match_id=wb_final.id,
            loser_next_match_id=lb_r2[m % len(lb_r2)].id,
        )
        for m in range(2)
    ]
    wb_r0 = [
        _make_match(
            tournament_id,
            bracket=Bracket.WINNERS,
            round=0,
            next_match_id=wb_r1[m // 2].id,
            loser_next_match_id=lb_r1[m % len(lb_r1)].id,
        )
        for m in range(4)
    ]

    matches = (
        [gf]
        + lb_r4
        + lb_r3
        + lb_r2
        + lb_r1
        + [wb_final]
        + wb_r1
        + wb_r0
    )
    numbers = {
        wb_r0[0].id: [1, 2],
        wb_r0[1].id: [3, 4],
        wb_r0[2].id: [5, 6],
        wb_r0[3].id: [7, 8],
        wb_r1[0].id: [1, 3],
        wb_r1[1].id: [5, 7],
        wb_final.id: [1, 5],
        lb_r1[0].id: [2, 4],
        lb_r1[1].id: [6, 8],
        lb_r2[0].id: [2, 6],
        lb_r2[1].id: [4, 8],
        lb_r3[0].id: [2, 4],
        lb_r3[1].id: [6, 8],
        lb_r4[0].id: [2, 6],
        gf.id: [1, 2],
    }
    return matches, numbers


def _build_round_robin_bracket(tournament_id, contestant_count):
    """Build a full round-robin schedule for `contestant_count` players.

    Each contestant keeps one participant ID across all its matches.
    """
    participant_ids = [
        TournamentParticipantID(generate_uuid())
        for _ in range(contestant_count)
    ]
    matches = []
    contestants_by_match = {}
    for round_num, (a, b) in enumerate(
        combinations(range(contestant_count), 2)
    ):
        match = _make_match(tournament_id, round=round_num, next_match_id=None)
        matches.append(match)
        contestants_by_match[match.id] = [
            TournamentMatchToContestant(
                id=TournamentMatchToContestantID(generate_uuid()),
                tournament_match_id=match.id,
                team_id=None,
                participant_id=participant_ids[i],
                score=None,
                created_at=NOW,
            )
            for i in (a, b)
        ]
    return matches, contestants_by_match


REPO = 'byceps.services.lan_tournament.tournament_match_service.tournament_repository'


# -------------------------------------------------------------------- #
# accepted brackets
# -------------------------------------------------------------------- #


@patch(REPO)
def test_validator_accepts_generated_se_bracket_with_byes(repo):
    """A generated SE bracket for 6 players (two DEFWIN slots) is valid."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament()
    matches, numbers = _build_se_bracket_6_players(tournament.id)

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = (
        _contestants_by_match(matches, numbers)
    )

    violations = validate_bracket_for_start(tournament.id)

    assert violations == []


@patch(REPO)
def test_validator_reports_keyless_contestant_row_instead_of_raising(repo):
    """A contestant row carrying neither participant nor team is skipped."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament()
    matches, numbers = _build_se_bracket_6_players(tournament.id)
    contestants = _contestants_by_match(matches, numbers)

    first_match_id = next(iter(contestants))
    contestants[first_match_id].append(
        _make_contestant(first_match_id, None)
    )

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = contestants

    violations = validate_bracket_for_start(tournament.id)

    assert violations == []


@patch(REPO)
def test_validator_reports_roundless_match_instead_of_raising(repo):
    """A match carrying no round number is reported, not raised on."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament()
    matches, numbers = _build_se_bracket_6_players(tournament.id)
    contestants = _contestants_by_match(matches, numbers)

    victim = next(m for m in matches if m.round == 0)
    matches = [
        replace(m, round=None) if m.id == victim.id else m
        for m in matches
    ]

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = contestants

    violations = validate_bracket_for_start(tournament.id)

    assert f'match {victim.id} has no round number' in violations
    # The rest of the bracket is still checked.
    assert not any('is missing next_match_id' in v for v in violations)


@patch(REPO)
def test_validator_still_finds_terminal_round_around_a_roundless_match(
    repo,
):
    """Validate the remaining matches against the real final round."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament()
    matches, numbers = _build_se_bracket_6_players(tournament.id)
    contestants = _contestants_by_match(matches, numbers)

    final = max(
        (m for m in matches if m.bracket in (None, Bracket.WINNERS)),
        key=lambda m: m.round,
    )
    victim = next(m for m in matches if m.round == 0)

    matches = [
        replace(m, round=None)
        if m.id == victim.id
        else replace(m, next_match_id=victim.id)
        if m.id == final.id
        else m
        for m in matches
    ]

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = contestants

    violations = validate_bracket_for_start(tournament.id)

    assert f'terminal match {final.id} has next_match_id' in violations


@patch(REPO)
def test_validator_accepts_generated_de_bracket(repo):
    """A generated DE bracket for 8 players is valid."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    matches, numbers = _build_de_bracket_8_players(tournament.id)

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = (
        _contestants_by_match(matches, numbers)
    )

    violations = validate_bracket_for_start(tournament.id)

    assert violations == []


@patch(REPO)
def test_validator_accepts_round_robin_schedule_odd_count(repo):
    """RR with 5 players yields C(5,2) == 10 pairings and is valid."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        elimination_mode=EliminationMode.ROUND_ROBIN,
    )
    matches, contestants_by_match = _build_round_robin_bracket(
        tournament.id, 5
    )

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = contestants_by_match

    violations = validate_bracket_for_start(tournament.id)

    assert len(matches) == 10  # expected pairing count
    assert violations == []
    # The live roster must never be consulted for this check.
    repo.get_participants_for_tournament.assert_not_called()


# -------------------------------------------------------------------- #
# skipped formats
# -------------------------------------------------------------------- #


@patch(REPO)
def test_validator_skips_highscore(repo):
    """Highscore has no matches and is valid without repo access."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    cases = [
        (
            GameFormat.HIGHSCORE,
            EliminationMode.NONE,
        ),
    ]  # fmt: skip

    for game_format, elimination_mode in cases:
        repo.reset_mock(return_value=True, side_effect=True)
        tournament = _make_tournament(
            game_format=game_format,
            elimination_mode=elimination_mode,
        )
        repo.get_tournament.return_value = tournament

        violations = validate_bracket_for_start(tournament.id)

        assert violations == []
        repo.get_matches_for_tournament_ordered.assert_not_called()


@pytest.mark.parametrize(
    'elimination_mode',
    [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION],
)
@patch(REPO)
def test_ffa_without_round_one_blocks_start(repo, elimination_mode):
    """An FFA tournament without any generated match cannot start."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=elimination_mode,
    )
    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = []

    assert validate_bracket_for_start(tournament.id) == [
        'no matches generated'
    ]


@pytest.mark.parametrize(
    'elimination_mode',
    [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION],
)
@patch(REPO)
def test_ffa_with_round_one_passes(repo, elimination_mode):
    """An FFA tournament with a generated round 1 may start."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=elimination_mode,
    )
    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = [
        _make_match(tournament.id, round=1)
    ]

    assert validate_bracket_for_start(tournament.id) == []


# -------------------------------------------------------------------- #
# rejected brackets
# -------------------------------------------------------------------- #


@patch(REPO)
def test_validator_rejects_missing_next_match_link(repo):
    """An SE bracket with a severed next_match_id lists a violation."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament()
    matches, numbers = _build_se_bracket_6_players(tournament.id)

    # Sever one semifinal's link to the final.
    severed = next(m for m in matches if m.round == 1)
    severed = replace(severed, next_match_id=None)
    matches = [m for m in matches if m.id != severed.id] + [severed]

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = (
        _contestants_by_match(matches, numbers)
    )

    violations = validate_bracket_for_start(tournament.id)

    assert any('missing next_match_id' in v for v in violations), violations


@patch(REPO)
def test_validator_rejects_orphaned_loser_routing(repo):
    """A DE WB match with a dangling loser_next_match_id is a violation."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    matches, numbers = _build_de_bracket_8_players(tournament.id)

    # Point one WB round-0 match's loser route at an unknown match.
    orphan_target = _make_id()
    orphaned = matches[-1]
    orphaned = replace(orphaned, loser_next_match_id=orphan_target)
    matches = [m for m in matches if m.id != orphaned.id] + [orphaned]

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = (
        _contestants_by_match(matches, numbers)
    )

    violations = validate_bracket_for_start(tournament.id)

    assert any('orphaned loser_next_match_id' in v for v in violations), (
        violations
    )


# -------------------------------------------------------------------- #
# an elimination mode invalid or unset for a bracket format
# -------------------------------------------------------------------- #


@patch(REPO)
def test_validator_rejects_unset_elimination_mode_with_matches(repo):
    """A missing elimination mode is a violation, even with matches."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(elimination_mode=None)
    matches = [_make_match(tournament.id, round=0)]

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = {}

    violations = validate_bracket_for_start(tournament.id)

    assert violations, violations
    assert any('elimination mode' in v for v in violations), violations
    assert not any('no matches generated' in v for v in violations), (
        violations
    )


@patch(REPO)
def test_validator_rejects_unset_elimination_mode_without_matches(repo):
    """Report both the missing elimination mode and the empty bracket."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(elimination_mode=None)

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = []
    repo.get_contestants_for_tournament.return_value = {}

    violations = validate_bracket_for_start(tournament.id)

    assert violations, violations
    assert any('no matches generated' in v for v in violations), violations
    assert any('elimination mode' in v for v in violations), violations


@patch(REPO)
def test_validator_rejects_none_elimination_mode_for_one_v_one(repo):
    """`EliminationMode.NONE` must not pass on a ONE_V_ONE tournament."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.NONE,
    )
    matches = [_make_match(tournament.id, round=0)]

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = {}

    violations = validate_bracket_for_start(tournament.id)

    assert violations, violations
    assert any("'NONE'" in v for v in violations), violations
    assert not any('no matches generated' in v for v in violations), (
        violations
    )


@patch(REPO)
def test_validator_highscore_still_bypasses_fallthrough(repo):
    """A HIGHSCORE tournament still returns `[]`."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
    )

    repo.get_tournament.return_value = tournament

    violations = validate_bracket_for_start(tournament.id)

    assert violations == []
    repo.get_matches_for_tournament_ordered.assert_not_called()


# -------------------------------------------------------------------- #
# round-robin validation ignores a shrinking live roster
# -------------------------------------------------------------------- #


@patch(REPO)
def test_validator_round_robin_ignores_shrunk_live_roster(repo):
    """A round-robin bracket stays valid after the live roster shrinks."""
    from byceps.services.lan_tournament.tournament_match_service import (
        validate_bracket_for_start,
    )

    tournament = _make_tournament(
        elimination_mode=EliminationMode.ROUND_ROBIN,
    )
    matches, contestants_by_match = _build_round_robin_bracket(
        tournament.id, 5
    )

    repo.get_tournament.return_value = tournament
    repo.get_matches_for_tournament_ordered.return_value = matches
    repo.get_contestants_for_tournament.return_value = contestants_by_match
    # The live roster has shrunk to 2.
    repo.get_participants_for_tournament.return_value = [
        _make_participant(tournament.id) for _ in range(2)
    ]
    repo.get_teams_for_tournament.return_value = []

    violations = validate_bracket_for_start(tournament.id)

    assert violations == []
    repo.get_participants_for_tournament.assert_not_called()
    repo.get_teams_for_tournament.assert_not_called()
