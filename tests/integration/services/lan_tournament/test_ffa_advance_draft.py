"""
tests.integration.services.lan_tournament.test_ffa_advance_draft
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Later FFA rounds: the qualification cut, the draft and its window.
"""

from dataclasses import replace
from datetime import datetime, UTC

import pytest

from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_seeding_repository,
    tournament_seeding_service,
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
from byceps.util.result import Err, Ok


PARTY_ID = PartyID('lan-party-2026-ffa-advance-draft')
SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION
TIE_TABLE = [10, 5, 5, 1]


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party 2026 FFA Advance Draft')


@pytest.fixture(scope='module')
def ticket_category(make_ticket_category, party):
    return make_ticket_category(party.id, 'FFA Advance Draft Entry')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'FfaAdvDraft{i:02d}') for i in range(12)]


@pytest.fixture(scope='module')
def ticketed(users, ticket_category):
    for user in users:
        ticket_creation_service.create_ticket(ticket_category, user, user=user)
    return users


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaAdvDraftAdmin')


def _started(
    name, ticketed, admin, *, players, size_max, advancing, mode=SE,
    table=(10, 6, 3, 1),
):  # fmt: skip
    """Start an FFA tournament with round 0 generated."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        name,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=mode,
        contestant_type=ContestantType.SOLO,
        max_players=16,
        group_size_min=2,
        group_size_max=size_max,
        advancement_count=advancing,
        point_table=list(table),
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()

    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_OPEN
    ).is_ok()
    for user in ticketed[:players]:
        join = tournament_participant_service.join_tournament(
            tournament.id, user.id
        )
        assert join.is_ok(), join.unwrap_err()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED
    ).is_ok()

    generated = tournament_match_service.generate_ffa_round(
        tournament.id,
        bracket=Bracket.WINNERS if mode is DE else None,
        initiator_id=admin.id,
    )
    assert generated.is_ok(), generated.unwrap_err()
    assert tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING
    ).is_ok()
    return tournament


def _round(tournament, round_number, bracket=None):
    return tournament_repository.get_matches_for_round(
        tournament.id, round_number, bracket=bracket
    )


def _members(match):
    return [
        str(c.participant_id)
        for c in tournament_match_service.get_contestants_for_match(match.id)
    ]


def _play(match, admin, order=None):
    """Place the lobby in `order` (best first) and confirm it."""
    order = order or _members(match)
    placed = tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(match.id, admin.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()
    return order


def _play_round(tournament, admin, round_number, bracket=None):
    """Play every lobby in its natural order; return them by group order."""
    return {
        m.group_order: _play(m, admin)
        for m in _round(tournament, round_number, bracket)
    }


def _lobbies(tournament, round_number, bracket=None):
    return {
        m.group_order: _members(m)
        for m in _round(tournament, round_number, bracket)
    }


def _prepare(tournament, admin, pool=None):
    return tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, pool=pool, initiator_id=admin.id
    )


# -------------------------------------------------------------------- #
# the draft


def test_prepare_ffa_round_draft_tiers_from_standings(ticketed, admin):
    """The tiers are the rank bands; the seed order is the standing order."""
    tournament = _started(
        'Advance tiers', ticketed, admin, players=12, size_max=4, advancing=2
    )
    played = _play_round(tournament, admin, 0)

    prepared = _prepare(tournament, admin)

    assert prepared == Ok('ffa:SE:1')
    board = tournament_seeding_service.get_board(
        tournament.id, 'ffa:SE:1'
    ).unwrap()
    band_of = {
        cid: place for order in played.values() for place, cid in enumerate(order[:2])
    }  # fmt: skip
    assert set(board.state.roster) == set(band_of)
    assert board.state.tier_count == 2
    tier_of = dict(zip(board.state.roster, board.state.tiers, strict=True))
    assert tier_of == band_of
    assert [band_of[cid] for cid in board.state.seed_list] == (
        [0] * 3 + [1] * 3
    )
    assert board.stale is False
    assert board.locked_reason is None


def test_prepare_keeps_an_edited_draft_while_survivors_stay(ticketed, admin):
    tournament = _started(
        'Advance keeps edits', ticketed, admin,
        players=12, size_max=6, advancing=3,
    )  # fmt: skip
    _play_round(tournament, admin, 0)
    target = _prepare(tournament, admin).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    edited = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 5),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip

    assert _prepare(tournament, admin) == Ok(target)

    again = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert again.version == edited.version
    assert again.code == edited.code


def test_prepare_rebuilds_the_draft_after_a_result_changed(ticketed, admin):
    tournament = _started(
        'Advance rebuilds', ticketed, admin,
        players=8, size_max=4, advancing=2,
    )  # fmt: skip
    first, second = _round(tournament, 0)
    _play(second, admin)
    order = _play(first, admin)
    target = _prepare(tournament, admin).unwrap()
    before = tournament_seeding_service.get_board(
        tournament.id, target
    ).unwrap()
    assert order[0] in before.state.roster

    assert tournament_match_service.unconfirm_match(
        first.id, admin.id, reason='wrong placements'
    ).is_ok()
    _play(first, admin, list(reversed(order)))
    assert _prepare(tournament, admin) == Ok(target)

    after = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert after.stale is False
    assert order[-1] in after.state.roster
    assert order[0] not in after.state.roster
    assert after.version == before.version + 1


def test_a_rebuilt_draft_refuses_a_stale_tab(ticketed, admin):
    tournament = _started(
        'Advance stale tab', ticketed, admin,
        players=8, size_max=4, advancing=2,
    )  # fmt: skip
    first, second = _round(tournament, 0)
    _play(second, admin)
    order = _play(first, admin)
    target = _prepare(tournament, admin).unwrap()
    stale = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    stale_id = tournament_seeding_repository.find_seeding(
        tournament.id, target
    ).id
    assert tournament_match_service.unconfirm_match(
        first.id, admin.id, reason='wrong placements'
    ).is_ok()
    _play(first, admin, list(reversed(order)))
    assert _prepare(tournament, admin) == Ok(target)
    fresh = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    fresh_id = tournament_seeding_repository.find_seeding(
        tournament.id, target
    ).id

    result = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 3),
        expected_version=stale.version, initiator_id=admin.id,
    )  # fmt: skip

    assert result.unwrap_err() == tournament_seeding_service.ERR_CONFLICT
    assert fresh.version == stale.version + 1
    assert fresh_id == stale_id


def test_prepare_refuses_while_the_round_is_open(ticketed, admin):
    tournament = _started(
        'Advance open', ticketed, admin, players=8, size_max=4, advancing=2
    )
    first, _second = _round(tournament, 0)
    _play(first, admin)

    result = _prepare(tournament, admin)

    assert isinstance(result, Err)
    assert 'not confirmed' in result.unwrap_err()


def test_generate_ffa_round_from_draft(ticketed, admin):
    """The orga's layout, not the derived one, becomes the lobbies."""
    tournament = _started(
        'Advance generate', ticketed, admin,
        players=12, size_max=4, advancing=2,
    )  # fmt: skip
    _play_round(tournament, admin, 0)
    target = _prepare(tournament, admin).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    derived = board.state.layout
    edited = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 5),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip
    assert edited.state.layout != derived

    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=edited.version, initiator_id=admin.id,
    )  # fmt: skip

    assert generated == Ok(2)
    lobbies = _lobbies(tournament, 1)
    layout = edited.state.layout
    assert {k: set(v) for k, v in lobbies.items()} == {
        0: set(layout[0:3]),
        1: set(layout[3:6]),
    }
    after = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert (
        after.generation is tournament_seeding_service.GenerationStatus.MATCHES
    )
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert 'bracket-generated' in events


def test_ffa_round_draft_regenerates_until_the_first_result(ticketed, admin):
    tournament = _started(
        'Advance regenerate', ticketed, admin,
        players=12, size_max=4, advancing=2,
    )  # fmt: skip
    _play_round(tournament, admin, 0)
    target = _prepare(tournament, admin).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    ).is_ok()  # fmt: skip
    old_ids = {m.id for m in _round(tournament, 1)}
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()

    edited = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 5),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip
    regenerated = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=edited.version, initiator_id=admin.id,
    )  # fmt: skip

    assert regenerated == Ok(2)
    new_matches = _round(tournament, 1)
    assert len(new_matches) == 2
    assert not old_ids & {m.id for m in new_matches}
    assert {k: set(v) for k, v in _lobbies(tournament, 1).items()}[0] == set(
        edited.state.layout[0:3]
    )

    _play(new_matches[0], admin)
    current = tournament_seeding_service.get_board(
        tournament.id, target
    ).unwrap()

    locked = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 1),
        expected_version=current.version, initiator_id=admin.id,
    )  # fmt: skip
    assert locked == Err(tournament_match_service.FFA_ROUND_LOCKED_ERROR)
    blocked = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=current.version, initiator_id=admin.id,
    )  # fmt: skip
    assert blocked == Err(tournament_match_service.FFA_ROUND_LOCKED_ERROR)
    locked_board = tournament_seeding_service.get_board(
        tournament.id, target
    ).unwrap()
    assert (
        locked_board.generation
        is tournament_seeding_service.GenerationStatus.LOCKED
    )
    assert locked_board.locked_reason == (
        tournament_match_service.FFA_ROUND_LOCKED_ERROR
    )
    assert {m.id for m in _round(tournament, 1)} == {m.id for m in new_matches}


def test_apply_action_opens_for_draft_targets_only(ticketed, admin):
    tournament = _started(
        'Advance targets', ticketed, admin,
        players=12, size_max=6, advancing=3,
    )  # fmt: skip
    _play_round(tournament, admin, 0)
    target = _prepare(tournament, admin).unwrap()
    swap = tournament_seeding_service.Swap(0, 1)

    applied = tournament_seeding_service.apply_action(
        tournament.id, target, swap, expected_version=1, initiator_id=admin.id
    )
    stale = tournament_seeding_service.apply_action(
        tournament.id, target, swap, expected_version=1, initiator_id=admin.id
    )
    playoff = tournament_seeding_service.apply_action(
        tournament.id, 'playoff', swap, expected_version=1,
        initiator_id=admin.id,
    )  # fmt: skip

    assert applied.is_ok(), applied.unwrap_err()
    assert applied.unwrap().version == 2
    assert stale == Err(tournament_seeding_service.ERR_CONFLICT)
    assert playoff == Err(tournament_seeding_service.ERR_NO_SEEDING)
    assert tournament_seeding_service.get_board(
        tournament.id, 'playoff'
    ) == Err(tournament_seeding_service.ERR_NO_SEEDING)


# -------------------------------------------------------------------- #
# the advance


def test_next_round_balanced_by_standing(ticketed, admin):
    """Every lobby of the next round mixes both bands of survivors."""
    tournament = _started(
        'Advance balanced', ticketed, admin,
        players=12, size_max=4, advancing=2,
    )  # fmt: skip
    played = _play_round(tournament, admin, 0)
    band_of = {
        cid: place for order in played.values() for place, cid in enumerate(order)
    }  # fmt: skip

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=admin.id
    )

    assert advanced == Ok(2)
    lobbies = _lobbies(tournament, 1)
    assert len(lobbies) == 2
    assert sorted(
        sorted(band_of[c] for c in ids) for ids in lobbies.values()
    ) == [[0, 0, 1], [0, 1, 1]]


def test_ffa_cut_tie_decision_unblocks_advance(ticketed, admin):
    tournament = _started(
        'Advance tie', ticketed, admin,
        players=8, size_max=4, advancing=2, table=TIE_TABLE,
    )  # fmt: skip
    played = _play_round(tournament, admin, 0)

    blocked = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=admin.id
    )
    assert blocked == Err(tournament_match_service.QUALIFICATION_TIE_ERROR)
    assert _prepare(tournament, admin) == Err(
        tournament_match_service.QUALIFICATION_TIE_ERROR
    )

    for group in (0, 1):
        decided = tournament_qualification_service.save_decision(
            tournament.id,
            f'ffa:SE:0:{group}',
            [played[group][2], played[group][1]],
            reason='Sudden death in the lobby',
            initiator_id=admin.id,
        )
        assert decided.is_ok(), decided.unwrap_err()

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=admin.id
    )

    assert advanced.is_ok(), advanced.unwrap_err()
    survivors = {c for ids in _lobbies(tournament, 1).values() for c in ids}
    assert survivors == {played[0][0], played[0][2], played[1][0], played[1][2]}
    events = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
    ]
    assert events.count('qualification-tie-decided') == 2


def test_ffa_tie_decision_needs_the_exact_tied_contestants(ticketed, admin):
    tournament = _started(
        'Advance tie guard', ticketed, admin,
        players=8, size_max=4, advancing=2, table=TIE_TABLE,
    )  # fmt: skip
    played = _play_round(tournament, admin, 0)
    save = tournament_qualification_service.save_decision

    wrong_members = save(
        tournament.id, 'ffa:SE:0:0', [played[0][0], played[0][1]],
        reason='x', initiator_id=admin.id,
    )  # fmt: skip
    no_such_lobby = save(
        tournament.id, 'ffa:SE:0:7', [played[0][2], played[0][1]],
        reason='x', initiator_id=admin.id,
    )  # fmt: skip
    open_lobby = save(
        tournament.id, 'ffa:SE:5:0', [played[0][2], played[0][1]],
        reason='x', initiator_id=admin.id,
    )  # fmt: skip

    assert wrong_members.is_err()
    assert no_such_lobby == Err('There is no tie to decide in this scope.')
    assert open_lobby == Err('There is no tie to decide in this scope.')


def test_withdrawn_ffa_decision_blocks_the_advance_again(ticketed, admin):
    tournament = _started(
        'Advance withdraw', ticketed, admin,
        players=8, size_max=4, advancing=2, table=TIE_TABLE,
    )  # fmt: skip
    played = _play_round(tournament, admin, 0)
    for group in (0, 1):
        assert tournament_qualification_service.save_decision(
            tournament.id, f'ffa:SE:0:{group}',
            [played[group][1], played[group][2]],
            reason='Coin-free decision', initiator_id=admin.id,
        ).is_ok()  # fmt: skip

    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'ffa:SE:0:1',
        contestant_ids=[played[1][1], played[1][2]],
        reason='Withdrawn for the test',
        initiator_id=admin.id,
    )

    assert withdrawn.is_ok()
    assert tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=admin.id
    ) == Err(tournament_match_service.QUALIFICATION_TIE_ERROR)


# -------------------------------------------------------------------- #
# double elimination


def _de_with_pending_wb_drop(ticketed, admin, name):
    """8 players, 4 per lobby, 2 advance: WB1 and LB0 confirmed.

    The WB1 lobby dropped two players that sit in no LB round yet.
    """
    tournament = _started(
        name, ticketed, admin,
        players=8, size_max=4, advancing=2, mode=DE,
    )  # fmt: skip
    _play_round(tournament, admin, 0, Bracket.WINNERS)
    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    )
    assert advanced == Ok('advanced_wb')
    _play_round(tournament, admin, 1, Bracket.WINNERS)
    _play_round(tournament, admin, 0, Bracket.LOSERS)
    return tournament


def test_lb_advance_counts_pending_wb_dropped_for_the_grand_final(
    ticketed, admin
):
    """The Grand Final would hold six players, so it is not eligible yet."""
    tournament = _de_with_pending_wb_drop(ticketed, admin, 'Advance GF count')

    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.LOSERS, initiator_id=admin.id
    )

    assert advanced == Ok('advanced_lb')
    assert len(_round(tournament, 1, Bracket.LOSERS)) == 1


def test_lb_draft_generates_and_regenerates(ticketed, admin):
    tournament = _de_with_pending_wb_drop(ticketed, admin, 'Advance LB draft')

    target = _prepare(tournament, admin, Bracket.LOSERS).unwrap()

    assert target == 'ffa:LB:1'
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    first = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    )  # fmt: skip
    old_ids = {m.id for m in _round(tournament, 1, Bracket.LOSERS)}
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    edited = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 1),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip
    second = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=edited.version, initiator_id=admin.id,
    )  # fmt: skip

    assert first == Ok(1)
    assert second == Ok(1)
    new_ids = {m.id for m in _round(tournament, 1, Bracket.LOSERS)}
    assert len(new_ids) == 1
    assert not old_ids & new_ids


def test_regenerate_wb_round_replaces_wb_and_companion_lb(ticketed, admin):
    tournament = _started(
        'Advance WB draft', ticketed, admin,
        players=8, size_max=4, advancing=2, mode=DE,
    )  # fmt: skip
    _play_round(tournament, admin, 0, Bracket.WINNERS)

    target = _prepare(tournament, admin, Bracket.WINNERS).unwrap()

    assert target == 'ffa:WB:1'
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    )  # fmt: skip
    assert generated == Ok(2)
    assert len(_round(tournament, 1, Bracket.WINNERS)) == 1
    assert len(_round(tournament, 0, Bracket.LOSERS)) == 1

    old_wb = {m.id for m in _round(tournament, 1, Bracket.WINNERS)}
    old_lb = {m.id for m in _round(tournament, 0, Bracket.LOSERS)}
    assert {
        m.seeding_target
        for m in _round(tournament, 1, Bracket.WINNERS)
        + _round(tournament, 0, Bracket.LOSERS)
    } == {target}

    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    edited = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Redraw(),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip
    again = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=edited.version, initiator_id=admin.id,
    )  # fmt: skip

    assert again == Ok(2)
    new_wb = _round(tournament, 1, Bracket.WINNERS)
    new_lb = _round(tournament, 0, Bracket.LOSERS)
    assert len(new_wb) == 1
    assert len(new_lb) == 1
    assert not old_wb & {m.id for m in new_wb}
    assert not old_lb & {m.id for m in new_lb}
    assert {m.seeding_target for m in new_wb + new_lb} == {target}
    assert {
        m.seeding_target
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.round == 0 and m.bracket == Bracket.WINNERS
    } == {None}

    _play(new_lb[0], admin)
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    locked = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    )  # fmt: skip
    assert locked == Err(tournament_match_service.FFA_ROUND_LOCKED_ERROR)
    assert {m.id for m in _round(tournament, 1, Bracket.WINNERS)} == {
        m.id for m in new_wb
    }


def test_winners_round_made_without_a_seeding_stays_locked(ticketed, admin):
    tournament = _started(
        'Advance WB untagged', ticketed, admin,
        players=8, size_max=4, advancing=2, mode=DE,
    )  # fmt: skip
    _play_round(tournament, admin, 0, Bracket.WINNERS)
    assert tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    ).is_ok()
    built = {m.id for m in _round(tournament, 1, Bracket.WINNERS)}
    assert {
        m.seeding_target for m in _round(tournament, 1, Bracket.WINNERS)
    } == {None}

    result = tournament_match_service._generate_ffa_advance_impl(
        tournament.id, Bracket.WINNERS, 1,
        groups=list(_lobbies(tournament, 1, Bracket.WINNERS).values()),
        initiator_id=admin.id,
    )  # fmt: skip
    tournament_repository.rollback_session()

    assert result == Err(tournament_match_service.FFA_UNTAGGED_WB_ERROR)
    assert {m.id for m in _round(tournament, 1, Bracket.WINNERS)} == built


def test_initial_ffa_round_carries_the_initial_target(ticketed, admin):
    tournament = _started(
        'Advance initial target', ticketed, admin,
        players=4, size_max=4, advancing=2,
    )  # fmt: skip

    outcome = tournament_match_service._generate_ffa_initial_impl(
        tournament.id, True, initiator_id=admin.id, seeding_target='initial'
    )
    targets = {
        m.seeding_target
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
    }
    tournament_repository.rollback_session()

    assert outcome.is_ok()
    assert targets == {'initial'}


def test_grand_final_eligible_round_has_no_draft(ticketed, admin):
    tournament = _started(
        'Advance GF eligible', ticketed, admin,
        players=4, size_max=4, advancing=2, mode=DE,
    )  # fmt: skip
    _play_round(tournament, admin, 0, Bracket.WINNERS)

    result = _prepare(tournament, admin, Bracket.WINNERS)

    assert result == Err(tournament_match_service.FFA_GRAND_FINAL_ERROR)


# -------------------------------------------------------------------- #
# the generator's own guards, behind the window check


def _no_window(monkeypatch):
    """Let the window check pass, so the generator's guards show."""
    monkeypatch.setattr(
        tournament_seeding_service, '_check_ffa_window', lambda *a: Ok(None)
    )


def test_generate_impl_refuses_a_round_with_a_result(
    ticketed, admin, monkeypatch
):
    tournament = _started(
        'Advance impl result', ticketed, admin,
        players=12, size_max=4, advancing=2,
    )  # fmt: skip
    _play_round(tournament, admin, 0)
    target = _prepare(tournament, admin).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    ).is_ok()  # fmt: skip
    built = {m.id for m in _round(tournament, 1)}
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    board = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 1),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip
    _play(_round(tournament, 1)[0], admin)
    _no_window(monkeypatch)

    result = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    )  # fmt: skip

    assert result == Err(tournament_match_service.FFA_ROUND_LOCKED_ERROR)
    assert {m.id for m in _round(tournament, 1)} == built


def test_generate_impl_refuses_a_winners_round_whose_losers_round_has_a_result(
    ticketed, admin, monkeypatch
):
    tournament = _started(
        'Advance impl WB', ticketed, admin,
        players=8, size_max=4, advancing=2, mode=DE,
    )  # fmt: skip
    _play_round(tournament, admin, 0, Bracket.WINNERS)
    target = _prepare(tournament, admin, Bracket.WINNERS).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    ).is_ok()  # fmt: skip
    built = {m.id for m in _round(tournament, 1, Bracket.WINNERS)}
    built_lb = {m.id for m in _round(tournament, 0, Bracket.LOSERS)}
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    board = tournament_seeding_service.apply_action(
        tournament.id, target, tournament_seeding_service.Swap(0, 1),
        expected_version=board.version, initiator_id=admin.id,
    ).unwrap()  # fmt: skip
    _play(_round(tournament, 0, Bracket.LOSERS)[0], admin)
    _no_window(monkeypatch)

    result = tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    )  # fmt: skip

    assert result == Err(tournament_match_service.FFA_ROUND_LOCKED_ERROR)
    assert {m.id for m in _round(tournament, 1, Bracket.WINNERS)} == built
    assert {m.id for m in _round(tournament, 0, Bracket.LOSERS)} == built_lb


def test_generate_impl_refuses_a_round_made_for_other_survivors(
    ticketed, admin, monkeypatch
):
    """The round is only replaced while its survivors are still the same."""
    tournament = _de_with_pending_wb_drop(ticketed, admin, 'Advance impl LB')
    target = _prepare(tournament, admin, Bracket.LOSERS).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    assert tournament_seeding_service.generate_from_seeding(
        tournament.id, target,
        expected_version=board.version, initiator_id=admin.id,
    ).is_ok()  # fmt: skip
    built = {m.id for m in _round(tournament, 1, Bracket.LOSERS)}

    plan = tournament_match_service.plan_ffa_advance(
        tournament_service.get_tournament(tournament.id),
        Bracket.LOSERS,
        next_round=1,
    ).unwrap()
    outsider = next(
        cid
        for cid in _members(_round(tournament, 1, Bracket.WINNERS)[0])
        if cid not in plan.survivors
    )
    monkeypatch.setattr(
        tournament_match_service,
        'plan_ffa_advance',
        lambda *a, **k: Ok(
            replace(plan, survivors=(*plan.survivors[:-1], outsider))
        ),
    )

    result = tournament_match_service._generate_ffa_advance_impl(
        tournament.id, Bracket.LOSERS, 1,
        groups=[list(plan.survivors[:-1]) + [outsider]],
        initiator_id=admin.id,
    )  # fmt: skip
    tournament_repository.rollback_session()

    assert result == Err(tournament_match_service.LAYOUT_ROSTER_ERROR)
    assert {m.id for m in _round(tournament, 1, Bracket.LOSERS)} == built


def test_ffa_tie_decisions_survive_a_playoff_release(ticketed, admin):
    """Only qualification decisions lock with the release, not lobby ties."""
    tournament = _started(
        'Advance release scope', ticketed, admin,
        players=8, size_max=4, advancing=2, table=TIE_TABLE,
    )  # fmt: skip
    played = _play_round(tournament, admin, 0)
    tournament_repository.set_playoff_release(
        tournament.id,
        released_at=datetime.now(UTC),
        released_by=admin.id,
    )
    tournament_repository.commit_session()

    decided = tournament_qualification_service.save_decision(
        tournament.id, 'ffa:SE:0:0', [played[0][2], played[0][1]],
        reason='Sudden death in the lobby', initiator_id=admin.id,
    )  # fmt: skip

    assert decided.is_ok(), decided.unwrap_err()
    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'ffa:SE:0:0',
        contestant_ids=[played[0][2], played[0][1]],
        reason='Withdrawn for the test',
        initiator_id=admin.id,
    )
    assert withdrawn.is_ok(), withdrawn.unwrap_err()
