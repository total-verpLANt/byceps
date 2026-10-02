"""Advance naturally small WB pools without relaxing the LB or GF."""

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as matches,
    tournament_seeding_service as seeding,
)
from byceps.services.lan_tournament.models.bracket import Bracket

from tests.integration.services.lan_tournament import (
    test_ffa_waiting_winner as base,
)


engine_party = base.engine_party
engine_players = base.engine_players
engine_admin = base.engine_admin
make_engine = base.make_engine


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize('entrypoint', ['direct', 'draft'])
def test_natural_shortfall_does_not_bypass_source_confirmation(
    make_engine, engine_admin, kind, entrypoint, monkeypatch
):
    tournament = make_engine(kind, size=8, minimum=3, cut=2)
    for match in base.lobbies(tournament, Bracket.WINNERS, 0):
        base.play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in base.lobbies(tournament, Bracket.LOSERS, 0):
        base.play(match, engine_admin)
    lock = base.repo.lock_tournament_for_update
    calls = []

    def record_lock(tid):
        calls.append(tid)
        return lock(tid)

    monkeypatch.setattr(base.repo, 'lock_tournament_for_update', record_lock)
    if entrypoint == 'direct':
        result = matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS)
    else:
        result = seeding.prepare_ffa_round_draft(
            tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
        )
    assert result.is_err()
    assert 'not confirmed' in result.unwrap_err()
    assert calls and calls[0] == tournament.id
    assert not base.lobbies(tournament, Bracket.WINNERS, 2)
    assert not base.lobbies(tournament, Bracket.LOSERS, 1)
    assert not base.entries(tournament, 'bracket-lobby-undersized')


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize('entrypoint', ['direct', 'draft'])
def test_natural_wb_shortfall_progresses_to_legal_gf(
    make_engine, engine_admin, kind, entrypoint
):
    tournament = make_engine(kind, size=8, minimum=3, cut=2)
    phase = 1 if kind == 'plain' else 2
    for match in base.lobbies(tournament, Bracket.WINNERS, 0):
        base.play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for pool, rnd in ((Bracket.WINNERS, 1), (Bracket.LOSERS, 0)):
        for match in base.lobbies(tournament, pool, rnd):
            base.play(match, engine_admin)
    wb_source = base.lobbies(tournament, Bracket.WINNERS, 1)[0]
    expected_wb = set(base.members(wb_source)[:2])
    expected_lb = set(base.members(wb_source)[2:]) | set(
        base.members(base.lobbies(tournament, Bracket.LOSERS, 0)[0])[:2]
    )
    removed = matches.removed_in_race(tournament)
    assert removed.count_for(Bracket.WINNERS) == 0
    assert (
        matches.advance_ffa_round(
            tournament.id, pool=Bracket.LOSERS
        ).unwrap_err()
        == matches.FFA_LOBBY_BELOW_MINIMUM_ERROR
    )
    assert (
        matches.generate_ffa_grand_final(tournament.id).unwrap_err()
        == matches.FFA_GRAND_FINAL_NOT_ELIGIBLE_ERROR
    )
    if entrypoint == 'direct':
        result = matches.advance_ffa_round(
            tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
        )
    else:
        prepared = seeding.prepare_ffa_round_draft(
            tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
        )
        assert prepared.is_ok(), prepared.unwrap_err()
        target = prepared.unwrap()
        board = seeding.get_board(tournament.id, target).unwrap()
        assert set(board.state.roster) == expected_wb
        assert not board.waiting_winners and not board.byes
        assert len(board.undersized) == 1
        assert board.undersized[0].natural_shortfall
        result = seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=board.version,
            initiator_id=engine_admin.id,
        )
        assert result.is_ok(), result.unwrap_err()
        assert (
            seeding.get_board(tournament.id, target)
            .unwrap()
            .undersized[0]
            .natural_shortfall
        )
        board = seeding.get_board(tournament.id, target).unwrap()
        edited = seeding.apply_action(
            tournament.id,
            target,
            seeding.Swap(0, 1),
            expected_version=board.version,
            initiator_id=engine_admin.id,
        ).unwrap()
        regenerated = seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=edited.version,
            initiator_id=engine_admin.id,
        )
        assert regenerated.is_ok(), regenerated.unwrap_err()
    assert result.is_ok(), result.unwrap_err()
    (wb,) = base.lobbies(tournament, Bracket.WINNERS, 2)
    (lb,) = base.lobbies(tournament, Bracket.LOSERS, 1)
    assert wb.phase == lb.phase == phase
    assert set(base.members(wb)) == expected_wb
    assert set(base.members(lb)) == expected_lb
    assert len(expected_wb) == 2 and len(expected_lb) == 4
    assert not expected_wb & expected_lb
    audits = base.entries(tournament, 'bracket-lobby-undersized')
    assert len(audits) == 1
    assert audits[0].data == {
        'pool': 'WB',
        'round': 2,
        'count': 2,
        'lobbies': [2],
        'minimum': 3,
        'reason': 'natural_shortfall',
    }
    assert matches.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS
    ).is_err()
    assert len(base.lobbies(tournament, Bracket.LOSERS, 1)) == 1
    base.play(wb, engine_admin)
    ranking = matches.get_contestants_for_match(wb.id)
    assert sorted(c.points for c in ranking) == [3, 5]
    base.play(lb, engine_admin)
    expected_gf = expected_wb | set(base.members(lb)[:2])
    assert (
        matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
        == 'grand_final_eligible'
    )
    matches.generate_ffa_grand_final(
        tournament.id, initiator_id=engine_admin.id
    ).unwrap()
    (gf,) = base.lobbies(tournament, Bracket.GRAND_FINAL, 0)
    assert gf.phase == phase
    assert len(base.members(gf)) == 4
    assert set(base.members(gf)) == expected_gf
    assert len(base.entries(tournament, 'bracket-lobby-undersized')) == 1
