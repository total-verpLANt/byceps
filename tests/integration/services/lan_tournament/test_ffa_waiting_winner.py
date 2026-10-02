"""Exercise a lone WB winner while the merged LB pool still needs a round."""

from dataclasses import replace
from datetime import datetime, UTC

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service as matches,
    tournament_participant_service,
    tournament_qualification_service as qualification,
    tournament_repository as repo,
    tournament_score_service,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err, Ok
from byceps.util.uuid import generate_uuid7


@pytest.fixture(scope='module')
def engine_party(make_party, make_brand):
    suffix = str(generate_uuid7())
    brand = make_brand(f'ffaclose{suffix}', f'FFA Close Engine {suffix}')
    return make_party(
        brand, PartyID(f'ffa-close-{suffix}'), f'FFA Close Engine {suffix}'
    )


@pytest.fixture(scope='module')
def engine_players(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'Ffa{suffix}P{i}') for i in range(16)]


@pytest.fixture(scope='module')
def engine_admin(make_user):
    return make_user(f'FfaAdmin{str(generate_uuid7())[:18]}')


@pytest.fixture
def make_engine(engine_party, engine_players, engine_admin):
    def make(kind, *, double=True, size=16, point_table=None, minimum=2, cut=1):
        mode = (
            EliminationMode.DOUBLE_ELIMINATION
            if double
            else EliminationMode.SINGLE_ELIMINATION
        )
        args = dict(
            contestant_type=ContestantType.SOLO,
            point_table=point_table or [5, 3, 2, 1],
            group_size_min=minimum,
            group_size_max=4,
            advancement_count=cut,
        )
        if kind == 'plain':
            args.update(
                game_format=GameFormat.FREE_FOR_ALL,
                elimination_mode=mode,
                tournament_status=TournamentStatus.REGISTRATION_CLOSED,
                max_players=16,
            )
        else:
            args.update(
                game_format=GameFormat.HIGHSCORE,
                elimination_mode=EliminationMode.NONE,
                score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
                tournament_status=TournamentStatus.ONGOING,
                playoff_game_format=GameFormat.FREE_FOR_ALL,
                playoff_elimination_mode=mode,
                playoff_qualifier_count=size,
                playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
            )
        tournament, _ = tournament_service.create_tournament(
            engine_party.id, f'FFA Close {generate_uuid7()}', **args
        ).unwrap()
        for user in engine_players[:size]:
            repo.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=user.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()
        if kind == 'plain':
            board = seeding.get_board(
                tournament.id, initiator_id=engine_admin.id
            ).unwrap()
            seeding.generate_from_seeding(
                tournament.id,
                expected_version=board.version,
                initiator_id=engine_admin.id,
            ).unwrap()
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, engine_admin.id
            ).unwrap()
        else:
            for i, participant in enumerate(
                repo.get_participants_for_tournament(tournament.id), start=1
            ):
                tournament_score_service.submit_score(
                    tournament.id, i * 10, participant_id=participant.id
                ).unwrap()
            tournament_score_service.close_leaderboard(
                tournament.id, initiator_id=engine_admin.id
            ).unwrap()
        return tournament

    yield make
    db.session.rollback()


def lobbies(tournament, bracket, round_number):
    return sorted(
        repo.get_matches_for_round(
            tournament.id, round_number, bracket=bracket
        ),
        key=lambda m: m.group_order or 0,
    )


def members(match):
    return sorted(
        str(c.participant_id)
        for c in matches.get_contestants_for_match(match.id)
    )


def play(match, admin):
    order = members(match)
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()
    return order[0]


def entries(tournament, event):
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == event
    ]


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize('entrypoint', ['direct', 'draft'])
def test_wb_winner_waits_while_merged_lb_plays(
    make_engine, engine_admin, kind, entrypoint
):
    tournament = make_engine(kind)
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    matches.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
    ).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        play(match, engine_admin)
    (wb,) = lobbies(tournament, Bracket.WINNERS, 1)
    winner = play(wb, engine_admin)
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
        assert len(board.state.roster) == 6
        assert winner not in board.state.roster
        assert len(board.waiting_winners) == 1
        assert not board.byes
        result = seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=board.version,
            initiator_id=engine_admin.id,
        )
        assert (
            seeding.get_board(tournament.id, target).unwrap().waiting_winners
            == board.waiting_winners
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
    assert not lobbies(tournament, Bracket.WINNERS, 2)
    lb = lobbies(tournament, Bracket.LOSERS, 1)
    assert sorted(len(members(m)) for m in lb) == [3, 3]
    assert winner not in {cid for m in lb for cid in members(m)}
    audits = entries(tournament, 'bracket-lobby-bye')
    assert len(audits) == 1
    (audit,) = audits
    assert audit.data == {'pool': 'WB', 'round': 2, 'contestant': winner}
    for method in ('direct', 'draft'):
        repeat = (
            matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS)
            if method == 'direct'
            else seeding.prepare_ffa_round_draft(
                tournament.id,
                pool=Bracket.WINNERS,
                initiator_id=engine_admin.id,
            )
        )
        assert repeat.is_err()
        assert (
            repeat.unwrap_err()
            == 'The winners bracket is finished. Continue the losers bracket.'
        )
    assert len(entries(tournament, 'bracket-lobby-bye')) == 1
    for match in lb:
        play(match, engine_admin)
    eligible = matches.advance_ffa_round(tournament.id, pool=Bracket.LOSERS)
    assert eligible.is_ok() and eligible.unwrap() == 'grand_final_eligible'
    matches.generate_ffa_grand_final(
        tournament.id, initiator_id=engine_admin.id
    ).unwrap()
    finals = [
        m
        for m in repo.get_matches_for_tournament(tournament.id)
        if m.bracket is Bracket.GRAND_FINAL
    ]
    assert len(finals) == 1
    assert len(members(finals[0])) == 3
    assert members(finals[0]).count(winner) == 1


@pytest.mark.parametrize('unconfirmed_pool', [Bracket.WINNERS, Bracket.LOSERS])
def test_waiting_wb_requires_both_source_pools_confirmed(
    make_engine, engine_admin, unconfirmed_pool
):
    tournament = make_engine('plain')
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    wb = lobbies(tournament, Bracket.WINNERS, 1)
    lb = lobbies(tournament, Bracket.LOSERS, 0)
    for match in lb if unconfirmed_pool is Bracket.WINNERS else wb:
        play(match, engine_admin)
    before = len(repo.get_matches_for_tournament(tournament.id))
    for entrypoint in ('direct', 'draft'):
        result = (
            matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS)
            if entrypoint == 'direct'
            else seeding.prepare_ffa_round_draft(
                tournament.id,
                pool=Bracket.WINNERS,
                initiator_id=engine_admin.id,
            )
        )
        assert result.is_err()
    assert len(repo.get_matches_for_tournament(tournament.id)) == before
    assert not entries(tournament, 'bracket-lobby-bye')


def test_removed_wb_drops_leave_a_new_grand_final_eligible_plan(
    make_engine, engine_admin
):
    tournament = make_engine('plain')
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        play(match, engine_admin)
    (wb,) = lobbies(tournament, Bracket.WINNERS, 1)
    winner = play(wb, engine_admin)
    for cid in members(wb):
        if cid != winner:
            tournament_participant_service.admin_remove_participant(
                tournament.id,
                TournamentParticipantID(cid),
                initiator=engine_admin,
            ).unwrap()
    result = matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS)
    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == 'grand_final_eligible'
    assert not entries(tournament, 'bracket-lobby-bye')


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_grand_final_gate_refuses_premature_and_unconfirmed_pools(
    make_engine, engine_admin, kind, monkeypatch
):
    tournament = make_engine(kind)
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        play(match, engine_admin)
    (wb,) = lobbies(tournament, Bracket.WINNERS, 1)
    winner = play(wb, engine_admin)
    writes = []
    commits = []
    original_create = repo.create_match
    original_commit = repo.commit_session

    def created(match):
        writes.append(match)
        return original_create(match)

    def committed():
        commits.append(True)
        return original_commit()

    monkeypatch.setattr(repo, 'create_match', created)
    monkeypatch.setattr(repo, 'commit_session', committed)
    before = len(
        tournament_log_service.get_entries_for_tournament(tournament.id)
    )
    premature = matches.generate_ffa_grand_final(tournament.id)
    assert premature.is_err()
    assert (
        premature.unwrap_err()
        == 'The Grand Final is not eligible yet. Continue the bracket rounds.'
    )
    assert not writes and not commits
    assert (
        len(tournament_log_service.get_entries_for_tournament(tournament.id))
        == before
    )
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    writes.clear()
    commits.clear()
    unconfirmed = matches.generate_ffa_grand_final(tournament.id)
    assert unconfirmed.is_err()
    assert not writes and not commits
    for match in lobbies(tournament, Bracket.LOSERS, 1):
        play(match, engine_admin)
    # Three survivors: one above a configured maximum of two is refused;
    # exactly the maximum of three is legal. Simulate persisted legacy config.
    current = repo.get_tournament(tournament.id)
    repo.update_tournament(replace(current, group_size_max=2))
    original_commit()
    writes.clear()
    commits.clear()
    too_large = matches.generate_ffa_grand_final(tournament.id)
    assert (
        too_large.is_err() and too_large.unwrap_err() == premature.unwrap_err()
    )
    assert not writes and not commits
    repo.update_tournament(
        replace(repo.get_tournament(tournament.id), group_size_max=3)
    )
    original_commit()
    legal = matches.generate_ffa_grand_final(tournament.id)
    assert legal.is_ok() and legal.unwrap() == 1
    (final,) = writes
    assert final.phase == (2 if kind == 'highscore' else 1)
    assert len(members(final)) == 3
    assert members(final).count(winner) == 1


def test_grand_final_generation_is_audited_and_signalled(
    make_engine, engine_admin
):
    from byceps.services.lan_tournament import signals

    created, ready = [], []

    def on_created(_sender, event):
        created.append(event)

    def on_ready(_sender, event):
        ready.append(event)

    tournament = make_engine('plain')
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        play(match, engine_admin)
    (wb,) = lobbies(tournament, Bracket.WINNERS, 1)
    play(wb, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 1):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.LOSERS).unwrap()
    before = len(entries(tournament, 'bracket-generated'))

    signals.match_created.connect(on_created)
    signals.match_ready.connect(on_ready)
    try:
        matches.generate_ffa_grand_final(
            tournament.id, initiator_id=engine_admin.id
        ).unwrap()
        (final,) = [
            m
            for m in repo.get_matches_for_tournament(tournament.id)
            if m.bracket is Bracket.GRAND_FINAL
        ]
        added = entries(tournament, 'bracket-generated')[before:]
        assert len(added) == 1
        assert added[0].data == {
            'target': 'ffa:GF',
            'contestants': members(final),
        }
        assert len(added[0].data['contestants']) == 3
        assert [e.match_id for e in created] == [final.id]
        assert [e.match_id for e in ready] == [final.id]

        refused = matches.generate_ffa_grand_final(
            tournament.id, initiator_id=engine_admin.id
        )
        assert refused.is_err()
        assert len(entries(tournament, 'bracket-generated')) == before + 1
        assert len(created) == 1 and len(ready) == 1
    finally:
        signals.match_created.disconnect(on_created)
        signals.match_ready.disconnect(on_ready)


def test_grand_final_gate_is_read_only_and_matches_the_generator(
    make_engine, engine_admin, monkeypatch
):
    tournament = make_engine('plain')
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        play(match, engine_admin)
    (wb,) = lobbies(tournament, Bracket.WINNERS, 1)
    play(wb, engine_admin)
    locks = []
    commits = []
    original_lock = repo.lock_tournament_for_update
    original_commit = repo.commit_session
    monkeypatch.setattr(
        repo,
        'lock_tournament_for_update',
        lambda tid: locks.append(tid) or original_lock(tid),
    )
    monkeypatch.setattr(
        repo,
        'commit_session',
        lambda: commits.append(True) or original_commit(),
    )
    premature = matches.ffa_grand_final_gate(tournament.id)
    assert premature == Err(matches.FFA_GRAND_FINAL_NOT_ELIGIBLE_ERROR)
    assert not locks and not commits
    matches.advance_ffa_round(tournament.id, pool=Bracket.WINNERS).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 1):
        play(match, engine_admin)
    matches.advance_ffa_round(tournament.id, pool=Bracket.LOSERS).unwrap()
    locks.clear()
    commits.clear()
    assert matches.ffa_grand_final_gate(tournament.id) == Ok(3)
    assert not locks and not commits
    assert not [
        m
        for m in repo.get_matches_for_tournament(tournament.id)
        if m.bracket is Bracket.GRAND_FINAL
    ]
    matches.generate_ffa_grand_final(tournament.id).unwrap()
    assert matches.ffa_grand_final_gate(tournament.id) == Err(
        'Grand Final has already been generated.'
    )


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_grand_final_requires_confirmation_even_when_every_entrant_fits(
    make_engine, kind, monkeypatch
):
    tournament = make_engine(kind, size=4)
    writes = []
    rollbacks = []
    original_rollback = repo.rollback_session
    original_create = repo.create_match

    def created(match):
        writes.append(match)
        return original_create(match)

    monkeypatch.setattr(repo, 'create_match', created)

    def rolled_back():
        rollbacks.append(True)
        original_rollback()

    monkeypatch.setattr(repo, 'rollback_session', rolled_back)
    result = matches.generate_ffa_grand_final(tournament.id)
    assert result.is_err()
    assert result.unwrap_err() == 'Bracket matches are not confirmed.'
    assert rollbacks == [True]
    assert not writes


def test_grand_final_requires_resolved_cut_ties(make_engine, engine_admin):
    tournament = make_engine('plain', point_table=[1, 1, 1, 1])
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, engine_admin)
    before = len(repo.get_matches_for_tournament(tournament.id))
    result = matches.generate_ffa_grand_final(tournament.id)
    assert (
        result.is_err()
        and result.unwrap_err() == matches.QUALIFICATION_TIE_ERROR
    )
    assert len(repo.get_matches_for_tournament(tournament.id)) == before


def _wait_for_merged_lb(tournament, admin, entrypoint, *, decide=False):
    def finish(match):
        return (
            play_decide(tournament, match, admin)
            if decide
            else play(match, admin)
        )

    for match in lobbies(tournament, Bracket.WINNERS, 0):
        finish(match)
    matches.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    ).unwrap()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        finish(match)
    (wb,) = lobbies(tournament, Bracket.WINNERS, 1)
    order = finish(wb)
    if entrypoint == 'direct':
        matches.advance_ffa_round(
            tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
        ).unwrap()
    else:
        target = seeding.prepare_ffa_round_draft(
            tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
        ).unwrap()
        board = seeding.get_board(tournament.id, target).unwrap()
        seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=board.version,
            initiator_id=admin.id,
        ).unwrap()
    assert not lobbies(tournament, Bracket.WINNERS, 2)
    assert lobbies(tournament, Bracket.LOSERS, 1)
    return wb, order


def play_decide(tournament, match, admin):
    order = members(match)
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()
    scope = matches.ffa_lobby_scope(match)
    for tie in qualification.get_ffa_cut_ties(tournament.id):
        if tie.scope != scope or tie.decided:
            continue
        ids = sorted(
            (c.contestant_id for c in tie.contestants), key=order.index
        )
        qualification.save_decision(
            tournament.id, scope, ids, reason='x', initiator_id=admin.id
        ).unwrap()
    return order


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize('entrypoint', ['direct', 'draft'])
def test_a_waiting_advance_locks_the_winners_source(
    make_engine, engine_admin, kind, entrypoint
):
    tournament = make_engine(kind)
    wb, _ = _wait_for_merged_lb(tournament, engine_admin, entrypoint)
    current = repo.get_tournament(tournament.id)
    assert matches.ffa_round_already_advanced(wb, current) is True
    result = matches.unconfirm_match(wb.id, engine_admin.id, reason='x')
    assert result.is_err()
    assert result.unwrap_err().startswith(
        'A later round has already been built from this result'
    )


def test_a_waiting_advance_locks_the_winners_tie_decision(
    make_engine, engine_admin
):
    tournament = make_engine('plain', point_table=[1, 1, 0, 0])
    wb, order = _wait_for_merged_lb(
        tournament, engine_admin, 'direct', decide=True
    )
    scope = matches.ffa_lobby_scope(wb)
    ties = [t for t in qualification.get_ffa_cut_ties(tournament.id)]
    assert any(t.scope == scope and t.locked is True for t in ties)
    withdrawn = qualification.withdraw_decision(
        tournament.id,
        scope,
        contestant_ids=order[:2],
        reason='x',
        initiator_id=engine_admin.id,
    )
    assert withdrawn == Err(qualification.ERR_FFA_ROUND_BUILT)
    saved = qualification.save_decision(
        tournament.id,
        scope,
        [order[1], order[0]],
        reason='x',
        initiator_id=engine_admin.id,
    )
    assert saved == Err(qualification.ERR_FFA_ROUND_BUILT)


def _open_losers_round(tournament, admin):
    for match in lobbies(tournament, Bracket.WINNERS, 0):
        play(match, admin)
    matches.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
    ).unwrap()
    for match in lobbies(tournament, Bracket.WINNERS, 1):
        play(match, admin)
    assert lobbies(tournament, Bracket.LOSERS, 0)
    assert all(
        m.confirmed_by is None for m in lobbies(tournament, Bracket.LOSERS, 0)
    )


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize('entrypoint', ['direct', 'draft'])
def test_a_winners_advance_refuses_an_open_losers_round(
    make_engine, engine_admin, kind, entrypoint
):
    tournament = make_engine(kind, cut=2, minimum=3)
    _open_losers_round(tournament, engine_admin)
    before = len(repo.get_matches_for_tournament(tournament.id))
    if entrypoint == 'direct':
        result = matches.advance_ffa_round(
            tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
        )
    else:
        result = seeding.prepare_ffa_round_draft(
            tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
        )
    assert result == Err(matches.FFA_LOSERS_UNCONFIRMED_ERROR)
    assert not lobbies(tournament, Bracket.LOSERS, 1)
    assert len(repo.get_matches_for_tournament(tournament.id)) == before


def test_a_winners_advance_merges_the_confirmed_losers_round(
    make_engine, engine_admin
):
    tournament = make_engine('plain', cut=2, minimum=3)
    _open_losers_round(tournament, engine_admin)
    lb0_losers = set()
    for match in lobbies(tournament, Bracket.LOSERS, 0):
        order = members(match)
        play(match, engine_admin)
        lb0_losers.update(order[2:])
    matches.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS, initiator_id=engine_admin.id
    ).unwrap()
    lb1 = lobbies(tournament, Bracket.LOSERS, 1)
    assert len(lb1) == 2
    assert sum(len(members(m)) for m in lb1) == 8
    assert lb0_losers.isdisjoint({cid for m in lb1 for cid in members(m)})


def _remove(tournament, admin, contestant_id):
    tournament_participant_service.admin_remove_participant(
        tournament.id,
        TournamentParticipantID(contestant_id),
        initiator=admin,
    ).unwrap()


def _play_in_order(match, admin, order):
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_a_removed_waiting_winner_keeps_a_harmless_tie_harmless(
    make_engine, engine_admin, kind
):
    tournament = make_engine(kind, point_table=[3, 2, 2, 0])
    wb, winner = _wait_for_merged_lb(tournament, engine_admin, 'direct')
    _remove(tournament, engine_admin, winner)
    current = repo.get_tournament(tournament.id)
    all_matches = repo.get_matches_for_tournament_ordered(tournament.id)
    assert matches._collect_wb_survivors(current, all_matches) == Ok([])
    scope = matches.ffa_lobby_scope(wb)
    assert not [
        t
        for t in qualification.get_ffa_cut_ties(tournament.id)
        if t.scope == scope
    ]
    for match in lobbies(tournament, Bracket.LOSERS, 1):
        play(match, engine_admin)
    advanced = matches.advance_ffa_round(tournament.id, pool=Bracket.LOSERS)
    assert advanced.is_ok(), advanced.unwrap_err()


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_a_removed_waiting_winner_is_not_replaced(
    make_engine, engine_admin, kind
):
    tournament = make_engine(kind)
    wb, winner = _wait_for_merged_lb(tournament, engine_admin, 'direct')
    runner_up = members(wb)[1]
    _remove(tournament, engine_admin, winner)
    current = repo.get_tournament(tournament.id)
    all_matches = repo.get_matches_for_tournament_ordered(tournament.id)
    assert matches._collect_wb_survivors(current, all_matches).unwrap() == []
    for match in lobbies(tournament, Bracket.LOSERS, 1):
        seated = members(match)
        if runner_up in seated:
            seated = [c for c in seated if c != runner_up] + [runner_up]
        _play_in_order(match, engine_admin, seated)
    advanced = matches.advance_ffa_round(tournament.id, pool=Bracket.LOSERS)
    assert advanced == Ok('grand_final_eligible')
    matches.generate_ffa_grand_final(
        tournament.id, initiator_id=engine_admin.id
    ).unwrap()
    (final,) = [
        m
        for m in repo.get_matches_for_tournament(tournament.id)
        if m.bracket is Bracket.GRAND_FINAL
    ]
    assert runner_up not in members(final)
