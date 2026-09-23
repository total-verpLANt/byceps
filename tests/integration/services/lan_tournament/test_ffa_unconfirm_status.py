"""
tests.integration.services.lan_tournament.test_ffa_unconfirm_status
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unconfirming an FFA match must only undo a completion it caused.
"""

import pytest

from byceps.services.lan_tournament import (
    tournament_log_service,
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
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.party.models import PartyID
from byceps.services.ticketing import ticket_creation_service


PARTY_ID = PartyID('lan-party-2026-ffa-unconfirm')


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 FFA Unconfirm')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'FFA Unconfirm Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'FfaUnconfirm{i:02d}') for i in range(8)]


@pytest.fixture(scope='module')
def ticketed(users, ticket_category):
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaUnconfirmAdmin')


def _started_ffa(name, ticketed, admin):
    """8-player FFA SE: two groups in round 0, one deciding group after."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=16,
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
        join = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join.is_ok(), join.unwrap_err()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()

    generated = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()

    round_0 = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    assert len(round_0) == 2
    assert all(m.next_match_id is None for m in round_0)
    return tournament, round_0


def _place_and_confirm(match, admin):
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    placed = tournament_match_service.set_ffa_placements(
        match.id,
        {str(c.participant_id): i + 1 for i, c in enumerate(contestants)},
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(match.id, admin.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()
    return contestants[0].participant_id


@pytest.mark.parametrize(
    'parked_status', [TournamentStatus.PAUSED, TournamentStatus.CANCELLED]
)
def test_unconfirming_ffa_match_keeps_parked_status(
    parked_status, party, ticketed, admin
):
    """PAUSED stays PAUSED; CANCELLED stays CANCELLED."""
    tournament, (first, _second) = _started_ffa(
        f'FFA unconfirm {parked_status.name}', ticketed, admin
    )
    _place_and_confirm(first, admin)
    assert tournament_service.change_status(
        tournament.id, parked_status
    ).is_ok()

    result = tournament_match_service.unconfirm_match(
        first.id, admin.id, reason='fix a placement'
    )

    assert result.is_ok(), result.unwrap_err()
    after = tournament_service.get_tournament(tournament.id)
    assert after.tournament_status == parked_status


def _completed_ffa(name, ticketed, admin):
    tournament, round_0 = _started_ffa(name, ticketed, admin)
    for match in round_0:
        _place_and_confirm(match, admin)

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=admin.id
    )
    assert advanced.is_ok(), advanced.unwrap_err()

    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    (final_group,) = (m for m in matches if m.round == 1)
    champion = _place_and_confirm(final_group, admin)

    completed = tournament_service.get_tournament(tournament.id)
    assert completed.tournament_status == TournamentStatus.COMPLETED
    assert completed.winner_participant_id == champion
    return tournament, round_0, final_group, champion


ADVANCED_ERROR = (
    'A later round has already been built from this result, so it can '
    'no longer be unconfirmed.'
)


def _assert_refused(result, tournament, match):
    assert result.is_err()
    assert result.unwrap_err() == ADVANCED_ERROR
    assert tournament_match_service.get_match(match.id).confirmed_by
    assert not [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == 'match-result-retracted'
    ]


def test_unconfirming_advanced_ffa_group_is_refused(party, ticketed, admin):
    """Round 1 was built from round 0, so round 0 stays as it is."""
    tournament, round_0, _final_group, champion = _completed_ffa(
        'FFA unconfirm early group', ticketed, admin
    )

    result = tournament_match_service.unconfirm_match(
        round_0[0].id, admin.id, reason='fix a round-0 placement'
    )

    _assert_refused(result, tournament, round_0[0])
    after = tournament_service.get_tournament(tournament.id)
    assert after.tournament_status == TournamentStatus.COMPLETED
    assert after.winner_participant_id == champion


def _started_ffa_de(name, ticketed, admin):
    """8-player FFA DE with both winners-bracket groups confirmed."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        contestant_type=ContestantType.SOLO,
        max_players=16,
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
        join = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join.is_ok(), join.unwrap_err()
    for status in (
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.ONGOING,
    ):
        assert tournament_service.change_status(tournament.id, status).is_ok()

    generated = tournament_match_service.generate_ffa_round(
        tournament.id, bracket=Bracket.WINNERS, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()

    wb_round_0 = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    assert len(wb_round_0) == 2
    for match in wb_round_0:
        _place_and_confirm(match, admin)
    return tournament, wb_round_0


def _advance_winners(tournament, admin):
    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    )
    assert advanced.is_ok(), advanced.unwrap_err()
    assert advanced.unwrap() == 'advanced_wb'
    matches = tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )
    (lb_group,) = (m for m in matches if m.bracket == Bracket.LOSERS)
    return lb_group


def test_unconfirming_advanced_winners_group_is_refused(party, ticketed, admin):
    """Winners round 1 and losers round 0 were built from this group."""
    tournament, wb_round_0 = _started_ffa_de(
        'FFA DE unconfirm advanced WB group', ticketed, admin
    )
    _advance_winners(tournament, admin)

    result = tournament_match_service.unconfirm_match(
        wb_round_0[0].id, admin.id, reason='fix a WB placement'
    )

    _assert_refused(result, tournament, wb_round_0[0])


def test_unconfirming_latest_losers_group_is_allowed(party, ticketed, admin):
    """Nothing has been built from the latest losers round yet."""
    tournament, _wb_round_0 = _started_ffa_de(
        'FFA DE unconfirm latest LB group', ticketed, admin
    )
    lb_group = _advance_winners(tournament, admin)
    _place_and_confirm(lb_group, admin)

    result = tournament_match_service.unconfirm_match(
        lb_group.id, admin.id, reason='fix an LB placement'
    )

    assert result.is_ok(), result.unwrap_err()
    assert tournament_match_service.get_match(lb_group.id).confirmed_by is None


def test_unconfirming_group_seeding_the_grand_final_is_refused(
    party, ticketed, admin
):
    """The grand final was built from the latest winners round."""
    tournament, wb_round_0 = _started_ffa_de(
        'FFA DE unconfirm after grand final', ticketed, admin
    )
    generated = tournament_match_service.generate_ffa_grand_final(
        tournament.id, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()

    result = tournament_match_service.unconfirm_match(
        wb_round_0[0].id, admin.id, reason='fix a WB placement'
    )

    _assert_refused(result, tournament, wb_round_0[0])


def test_unconfirming_deciding_ffa_group_reopens_tournament(
    party, ticketed, admin
):
    """The deciding group completed the tournament, so it reverts it."""
    tournament, _round_0, final_group, _champion = _completed_ffa(
        'FFA unconfirm final group', ticketed, admin
    )

    result = tournament_match_service.unconfirm_match(
        final_group.id, admin.id, reason='fix the final placement'
    )

    assert result.is_ok(), result.unwrap_err()
    after = tournament_service.get_tournament(tournament.id)
    assert after.tournament_status == TournamentStatus.ONGOING
    assert after.winner_participant_id is None
