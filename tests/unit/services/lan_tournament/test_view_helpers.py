"""
tests.unit.services.lan_tournament.test_view_helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for ``lan_tournament_view_helpers``.
"""

from datetime import datetime

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
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_round_robin_standings,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)
TOURNAMENT_ID = TournamentID(generate_uuid())


def _make_match(
    *,
    confirmed: bool = True,
    match_id: TournamentMatchID | None = None,
) -> TournamentMatch:
    if match_id is None:
        match_id = TournamentMatchID(generate_uuid())
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=UserID(generate_uuid()) if confirmed else None,
        created_at=NOW,
    )


def _make_contestant(
    *,
    match_id: TournamentMatchID,
    participant_id: TournamentParticipantID | None = None,
    score: int | None = None,
) -> TournamentMatchToContestant:
    if participant_id is None:
        participant_id = TournamentParticipantID(generate_uuid())
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=participant_id,
        score=score,
        created_at=NOW,
    )


# -------------------------------------------------------------------- #
# build_round_robin_standings
# -------------------------------------------------------------------- #


def test_build_round_robin_standings_empty():
    """No match data yields empty standings."""
    standings = build_round_robin_standings([])
    assert standings == []


def test_build_round_robin_standings_filters_unconfirmed():
    """Only confirmed matches count towards standings."""
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    confirmed_match = _make_match(confirmed=True)
    unconfirmed_match = _make_match(confirmed=False)

    match_data = [
        {
            'match': confirmed_match,
            'contestants': [
                _make_contestant(
                    match_id=confirmed_match.id,
                    participant_id=pid_a,
                    score=10,
                ),
                _make_contestant(
                    match_id=confirmed_match.id,
                    participant_id=pid_b,
                    score=5,
                ),
            ],
        },
        {
            'match': unconfirmed_match,
            'contestants': [
                _make_contestant(
                    match_id=unconfirmed_match.id,
                    participant_id=pid_a,
                    score=10,
                ),
                _make_contestant(
                    match_id=unconfirmed_match.id,
                    participant_id=pid_b,
                    score=5,
                ),
            ],
        },
    ]

    standings = build_round_robin_standings(match_data)

    # Only 1 confirmed match → A has 1 win, B has 1 loss.
    assert len(standings) == 2
    assert standings[0].contestant_id == str(pid_a)
    assert standings[0].wins == 1
    assert standings[0].losses == 0
    assert standings[1].contestant_id == str(pid_b)
    assert standings[1].wins == 0
    assert standings[1].losses == 1


def test_build_round_robin_standings_all_unconfirmed():
    """All unconfirmed matches yield empty standings."""
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())

    m = _make_match(confirmed=False)

    match_data = [
        {
            'match': m,
            'contestants': [
                _make_contestant(
                    match_id=m.id,
                    participant_id=pid_a,
                    score=10,
                ),
                _make_contestant(
                    match_id=m.id,
                    participant_id=pid_b,
                    score=5,
                ),
            ],
        },
    ]

    standings = build_round_robin_standings(match_data)
    assert standings == []


def test_build_round_robin_standings_multiple_confirmed():
    """Multiple confirmed matches produce correct aggregated
    standings."""
    pid_a = TournamentParticipantID(generate_uuid())
    pid_b = TournamentParticipantID(generate_uuid())
    pid_c = TournamentParticipantID(generate_uuid())

    m1 = _make_match(confirmed=True)
    m2 = _make_match(confirmed=True)
    m3 = _make_match(confirmed=True)

    # A beats B (10-5), A beats C (8-3), B beats C (7-2)
    match_data = [
        {
            'match': m1,
            'contestants': [
                _make_contestant(
                    match_id=m1.id,
                    participant_id=pid_a,
                    score=10,
                ),
                _make_contestant(
                    match_id=m1.id,
                    participant_id=pid_b,
                    score=5,
                ),
            ],
        },
        {
            'match': m2,
            'contestants': [
                _make_contestant(
                    match_id=m2.id,
                    participant_id=pid_a,
                    score=8,
                ),
                _make_contestant(
                    match_id=m2.id,
                    participant_id=pid_c,
                    score=3,
                ),
            ],
        },
        {
            'match': m3,
            'contestants': [
                _make_contestant(
                    match_id=m3.id,
                    participant_id=pid_b,
                    score=7,
                ),
                _make_contestant(
                    match_id=m3.id,
                    participant_id=pid_c,
                    score=2,
                ),
            ],
        },
    ]

    standings = build_round_robin_standings(match_data)

    assert len(standings) == 3
    # A: 6 pts (2 wins), B: 3 pts (1 win), C: 0 pts (0 wins)
    assert standings[0].contestant_id == str(pid_a)
    assert standings[0].points == 6
    assert standings[1].contestant_id == str(pid_b)
    assert standings[1].points == 3
    assert standings[2].contestant_id == str(pid_c)
    assert standings[2].points == 0
