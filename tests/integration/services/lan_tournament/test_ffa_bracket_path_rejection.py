"""
tests.integration.services.lan_tournament.test_ffa_bracket_path_rejection

A free-for-all match is decided by placements. Every bracket score
path refuses it, including by direct call -- the blueprints' own
per-route checks only cover the forms they render.
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


PARTY_ID = PartyID('lan-party-2026-ffa-bracket-path')

CONFIRM_ERROR = 'Free-for-all matches are decided by placements, not by scores.'
CORRECTION_ERROR = (
    'Free-for-all matches are corrected by unconfirming '
    'them and re-entering the placements.'
)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 FFA Bracket Path')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'FFA Bracket Path Entry')


@pytest.fixture(scope='module')
def ticketed(make_user, ticket_category):
    users = [make_user(f'FfaBracketPath{i:02d}') for i in range(4)]
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaBracketPathAdmin')


@pytest.fixture(scope='module')
def ffa_group(party, ticketed, admin):
    """A confirmable single-group FFA round, i.e. the auto-completing one."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        'FFA bracket path rejection',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[10, 6, 3, 1],
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed:
        assert tournament_participant_service.join_tournament(
            tournament.id, user.id
        ).is_ok()
    for status in (
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    ):
        assert tournament_service.change_status(tournament.id, status).is_ok()

    assert tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=admin.id
    ).is_ok()
    (group,) = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    return tournament, group


def _scores(match_id):
    contestants = tournament_match_service.get_contestants_for_match(match_id)
    return {c.participant_id: 10 - i for i, c in enumerate(contestants)}


def _state(tournament_id, match_id):
    match = tournament_match_service.get_match(match_id)
    tournament = tournament_service.get_tournament(tournament_id)
    contestants = tournament_match_service.get_contestants_for_match(match_id)
    return (
        match.confirmed_by,
        tournament.tournament_status,
        tournament.winner_participant_id,
        [c.score for c in contestants],
    )


def _losing_user(tournament_id, match_id, users):
    contestants = tournament_match_service.get_contestants_for_match(match_id)
    lowest = contestants[-1]
    participants = (
        tournament_participant_service.get_participants_for_tournament(
            tournament_id
        )
    )
    (participant,) = [p for p in participants if p.id == lowest.participant_id]
    (user,) = [u for u in users if u.id == participant.user_id]
    return user


def test_participant_score_submission_cannot_confirm_an_ffa_match(
    ffa_group, ticketed
):
    """The participant path needs no permission beyond being in the match."""
    tournament, group = ffa_group
    before = _state(tournament.id, group.id)
    submitter = _losing_user(tournament.id, group.id, ticketed)

    result = tournament_match_service.set_match_scores(
        group.id, submitter.id, _scores(group.id)
    )

    assert result.is_err()
    assert result.unwrap_err() == CONFIRM_ERROR
    # Unconfirmed, no scores written, and above all no tournament
    # winner declared off a first-round group.
    assert _state(tournament.id, group.id) == before


def test_admin_confirm_with_scores_cannot_confirm_an_ffa_match(
    ffa_group, admin
):
    tournament, group = ffa_group
    before = _state(tournament.id, group.id)

    result = tournament_match_service.admin_set_and_confirm_match(
        group.id, admin.id, _scores(group.id)
    )

    assert result.is_err()
    assert result.unwrap_err() == CONFIRM_ERROR
    assert _state(tournament.id, group.id) == before


def test_result_correction_cannot_retract_an_ffa_match(ffa_group, admin):
    tournament, group = ffa_group
    before = _state(tournament.id, group.id)

    result = tournament_match_service.correct_match_result(
        group.id, admin.id, reason='posted straight at the route'
    )

    assert result.is_err()
    assert result.unwrap_err() == CORRECTION_ERROR
    assert _state(tournament.id, group.id) == before


def test_the_placement_path_still_confirms_and_completes(
    ffa_group, admin, ticketed
):
    """The rejections above must not have closed the real path."""
    tournament, group = ffa_group
    contestants = tournament_match_service.get_contestants_for_match(group.id)

    assert tournament_match_service.set_ffa_placements(
        group.id,
        {str(c.participant_id): i + 1 for i, c in enumerate(contestants)},
    ).is_ok()
    assert tournament_match_service.confirm_ffa_match(group.id, admin.id).is_ok()

    confirmed_by, status, winner_id, scores = _state(tournament.id, group.id)
    assert confirmed_by == admin.id
    assert status == TournamentStatus.COMPLETED
    assert winner_id is not None
    # Decided by placement, so the score columns stay empty.
    assert scores == [None] * len(contestants)
    assert [
        (c.placement, c.points)
        for c in tournament_match_service.get_contestants_for_match(group.id)
    ] == [(1, 10), (2, 6), (3, 3), (4, 1)]


@pytest.fixture(scope='module')
def bracket_match(party, ticketed, admin):
    """An unconfirmed single-elimination match, i.e. the other format."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        'Bracket placement rejection',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=8,
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed:
        assert tournament_participant_service.join_tournament(
            tournament.id, user.id
        ).is_ok()
    # The bracket has to exist before the start, not after:
    # change_status() validates its structure on the way to ONGOING.
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()
    assert tournament_match_service.generate_single_elimination_bracket(
        tournament.id
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    # A first-round match: it has a next_match_id to stall.
    (playable,) = [
        m
        for m in matches
        if m.next_match_id is not None
        and len(
            [
                c
                for c in tournament_match_service.get_contestants_for_match(
                    m.id
                )
                if c.participant_id or c.team_id
            ]
        )
        == 2
    ][:1]
    return tournament, playable


def test_placements_cannot_be_written_to_a_bracket_match(
    bracket_match, admin
):
    """First half of the bracket stall: no placements on a bracket match."""
    tournament, match = bracket_match
    contestants = tournament_match_service.get_contestants_for_match(match.id)

    result = tournament_match_service.set_ffa_placements(
        match.id,
        {str(c.participant_id): i + 1 for i, c in enumerate(contestants)},
    )

    assert result.is_err()
    assert result.unwrap_err() == (
        'Placements apply only to free-for-all matches.'
    )
    assert all(
        c.placement is None
        for c in tournament_match_service.get_contestants_for_match(match.id)
    )


def test_the_ffa_confirm_route_cannot_confirm_a_bracket_match(
    bracket_match, admin
):
    """Second half: confirm_ffa_match never advances a winner.

    Letting it take a bracket match would mark the match confirmed
    without feeding its next_match_id, and the normal confirm path
    refuses a confirmed match from then on -- the bracket stalls.
    """
    tournament, match = bracket_match

    result = tournament_match_service.confirm_ffa_match(match.id, admin.id)

    assert result.is_err()
    assert result.unwrap_err() == (
        'Placements apply only to free-for-all matches.'
    )
    assert tournament_match_service.get_match(match.id).confirmed_by is None
