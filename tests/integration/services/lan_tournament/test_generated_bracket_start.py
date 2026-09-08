"""
tests.integration.services.lan_tournament.test_generated_bracket_start
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Feed REALLY generated brackets to ``validate_bracket_for_start`` and
then actually start the tournament.

Why this exists alongside the unit suite: every case in
``tests/unit/.../test_bracket_start_validation.py`` patches the
repository and hand-builds the match graph, so it asserts that the
validator agrees with a *hand-drawn* bracket -- never that it agrees
with what ``generate_*_bracket`` really writes. The gate it guards is
a hard block with no administrative override: a validator that drifts
away from its generator does not degrade, it makes the tournament
unstartable in the middle of an event. Only an end-to-end pass over
real generator output can catch that drift.

Sizes cover the shapes that differ structurally: powers of two, odd
counts, and counts that force byes (SE), WBR0 DEFWIN nullification and
dead-LB propagation (DE), and the odd/even circle-method split (RR).
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-bracket-start')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Bracket Start')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'Bracket Start Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'BracketStart{i:02d}') for i in range(16)]


@pytest.fixture(scope='module')
def grant_ticket(ticket_category):
    def _grant(user):
        return ticket_creation_service.create_ticket(
            ticket_category, user, user=user
        )

    return _grant


@pytest.fixture(scope='module')
def ticketed(users, grant_ticket):
    """Grant every user one ticket, once for the whole module."""
    for user in users:
        grant_ticket(user)
    return users


def _build_closed_tournament(name, elimination_mode, num_players, users):
    """Create a tournament, fill it, and close registration."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=elimination_mode,
        contestant_type=ContestantType.SOLO,
        max_players=64,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    open_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    )
    assert open_result.is_ok()

    for user in users[:num_players]:
        join_result = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join_result.is_ok(), join_result.unwrap_err()

    close_result = tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    )
    assert close_result.is_ok()

    return tournament


def _assert_validates_and_starts(tournament, label):
    violations = tournament_match_service.validate_bracket_for_start(
        tournament.id
    )
    assert violations == [], f'{label}: {violations}'

    start_result = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    )
    assert start_result.is_ok(), (
        f'{label}: start refused -- {start_result.unwrap_err()}'
    )


# 2 and 16 are exact bracket sizes; the rest force byes, and the odd
# counts put a DEFWIN in WBR0.
@pytest.mark.parametrize(
    'num_players', [2, 3, 4, 5, 6, 7, 8, 9, 11, 13, 16]
)
def test_generated_se_bracket_validates_and_starts(
    num_players, party, ticketed
):
    tournament = _build_closed_tournament(
        f'SE start {num_players}',
        EliminationMode.SINGLE_ELIMINATION,
        num_players,
        ticketed,
    )

    generate_result = (
        tournament_match_service.generate_single_elimination_bracket(
            tournament.id
        )
    )
    assert generate_result.is_ok(), generate_result.unwrap_err()

    _assert_validates_and_starts(tournament, f'SE n={num_players}')


# Below 4 the generator refuses outright, so 4 is the floor here.
# 5-7 and 9-12 exercise WBR0 DEFWIN nullification and the dead-LB
# propagation that follows it -- the parts of the DE graph the
# validator's feed-count rule reasons about.
@pytest.mark.parametrize(
    'num_players', [4, 5, 6, 7, 8, 9, 11, 12, 16]
)
def test_generated_de_bracket_validates_and_starts(
    num_players, party, ticketed
):
    tournament = _build_closed_tournament(
        f'DE start {num_players}',
        EliminationMode.DOUBLE_ELIMINATION,
        num_players,
        ticketed,
    )

    generate_result = (
        tournament_match_service.generate_double_elimination_bracket(
            tournament.id
        )
    )
    assert generate_result.is_ok(), generate_result.unwrap_err()

    _assert_validates_and_starts(tournament, f'DE n={num_players}')


# The validator derives the expected match count as n*(n-1)/2, so an
# odd roster -- where the circle method carries a bye round -- is the
# case that would expose a miscount.
@pytest.mark.parametrize('num_players', [2, 3, 4, 5, 6, 7, 8])
def test_generated_round_robin_bracket_validates_and_starts(
    num_players, party, ticketed
):
    tournament = _build_closed_tournament(
        f'RR start {num_players}',
        EliminationMode.ROUND_ROBIN,
        num_players,
        ticketed,
    )

    generate_result = tournament_match_service.generate_round_robin_bracket(
        tournament.id
    )
    assert generate_result.is_ok(), generate_result.unwrap_err()

    _assert_validates_and_starts(tournament, f'RR n={num_players}')
