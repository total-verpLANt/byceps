"""Complete single-track FFA without manufacturing a one-player lobby."""

import pytest
from sqlalchemy.exc import IntegrityError

from byceps.services.lan_tournament import (
    signals,
    tournament_match_service as matches,
    tournament_participant_service,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)

from .test_ffa_waiting_winner import (
    engine_admin as engine_admin,
    engine_party as engine_party,
    engine_players as engine_players,
    entries,
    lobbies,
    make_engine as make_engine,
    members,
    play,
)


@pytest.mark.parametrize('entrypoint', ['direct', 'draft'])
def test_single_survivor_cut_tie_is_not_decided_by_completion(
    make_engine, engine_admin, entrypoint
):
    tournament = make_engine(
        'plain', double=False, size=8, point_table=[1, 1, 1, 1]
    )
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, engine_admin)
    for cid in members(first[1]):
        tournament_participant_service.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=engine_admin
        ).unwrap()
    result = (
        matches.advance_ffa_round(tournament.id)
        if entrypoint == 'direct'
        else seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=engine_admin.id
        )
    )
    assert (
        result.is_err()
        and result.unwrap_err() == matches.QUALIFICATION_TIE_ERROR
    )
    assert not entries(tournament, 'bracket-single-survivor')


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_generated_final_removal_completes_before_the_terminal_fk_followup(
    make_engine, engine_admin, kind
):
    tournament = make_engine(kind, double=False, size=8)
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, engine_admin)
    target = seeding.prepare_ffa_round_draft(
        tournament.id, initiator_id=engine_admin.id
    ).unwrap()
    board = seeding.get_board(tournament.id, target).unwrap()
    seeding.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=engine_admin.id,
    ).unwrap()
    removed_winner, second_victim, *_ = members(first[1])
    assert (
        repo.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )
    tournament_participant_service.admin_remove_participant(
        tournament.id,
        TournamentParticipantID(removed_winner),
        initiator=engine_admin,
    ).unwrap()
    persisted = repo.get_tournament(tournament.id)
    assert persisted.tournament_status is TournamentStatus.COMPLETED
    (final,) = lobbies(tournament, None, 1)
    assert final.confirmed_by is not None
    assert final.phase == (2 if kind == 'highscore' else 1)
    assert str(persisted.winner_participant_id) == members(first[0])[0]
    assert not entries(tournament, 'bracket-single-survivor')
    with pytest.raises(
        IntegrityError,
        match='lan_tournament_match_contestants_participant_id_fkey',
    ):
        tournament_participant_service.admin_remove_participant(
            tournament.id,
            TournamentParticipantID(second_victim),
            initiator=engine_admin,
        )
    repo.rollback_session()


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize('entrypoint', ['direct', 'draft', 'existing_draft'])
def test_single_survivor_completes_without_a_lobby(
    make_engine, engine_admin, kind, entrypoint, monkeypatch
):
    tournament = make_engine(kind, double=False, size=8)
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, engine_admin)
    target = None
    if entrypoint == 'existing_draft':
        target = seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=engine_admin.id
        ).unwrap()
        board = seeding.get_board(tournament.id, target).unwrap()
    winner = members(first[0])[0]
    for cid in members(first[1]):
        tournament_participant_service.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=engine_admin
        ).unwrap()
    commits = []
    received = []
    commit = repo.commit_session

    def committed():
        commit()
        commits.append(True)

    def completed(sender, *, event):
        assert commits == [True]
        assert (
            repo.get_tournament(tournament.id).tournament_status
            is TournamentStatus.COMPLETED
        )
        received.append(event)

    monkeypatch.setattr(repo, 'commit_session', committed)
    signals.tournament_completed.connect(completed)
    if entrypoint == 'direct':
        result = matches.advance_ffa_round(
            tournament.id, initiator_id=engine_admin.id
        )
    elif entrypoint == 'draft':
        result = seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=engine_admin.id
        )
    else:
        result = seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=board.version,
            initiator_id=engine_admin.id,
        )
    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == 'completed'
    persisted = repo.get_tournament(tournament.id)
    assert persisted.tournament_status is TournamentStatus.COMPLETED
    assert str(persisted.winner_participant_id) == winner
    assert len(repo.get_matches_for_tournament(tournament.id)) == len(first)
    assert len(received) == 1
    assert str(received[0].winner_participant_id) == winner
    (audit,) = entries(tournament, 'bracket-single-survivor')
    assert audit.data == {'pool': 'SE', 'round': 1, 'contestant': winner}
    repeat = matches.advance_ffa_round(
        tournament.id, initiator_id=engine_admin.id
    )
    assert repeat.is_err()
    assert len(entries(tournament, 'bracket-single-survivor')) == 1
    assert len(received) == 1
    signals.tournament_completed.disconnect(completed)


@pytest.mark.parametrize('entrypoint', ['direct', 'draft', 'existing_draft'])
def test_single_survivor_requires_confirmed_source(
    make_engine, engine_admin, entrypoint
):
    tournament = make_engine('plain', double=False, size=8)
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, engine_admin)
    target = seeding.prepare_ffa_round_draft(
        tournament.id, initiator_id=engine_admin.id
    ).unwrap()
    board = seeding.get_board(tournament.id, target).unwrap()
    for cid in members(first[1]):
        tournament_participant_service.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=engine_admin
        ).unwrap()
    matches.unconfirm_match(first[0].id, engine_admin.id).unwrap()
    if entrypoint == 'direct':
        result = matches.advance_ffa_round(
            tournament.id, initiator_id=engine_admin.id
        )
    elif entrypoint == 'draft':
        result = seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=engine_admin.id
        )
    else:
        result = seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=board.version,
            initiator_id=engine_admin.id,
        )
    assert result.is_err()
    assert not entries(tournament, 'bracket-single-survivor')
    assert (
        repo.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )
