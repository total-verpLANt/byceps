from datetime import datetime, UTC
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_match_service as matches,
    tournament_participant_service as participants,
    tournament_qualification_service as qualification,
    tournament_repository as repo,
    tournament_score_service as scores,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7

from tests.integration.services.lan_tournament.test_ffa_waiting_winner import (
    entries,
    lobbies,
    members,
    play,
)


@pytest.fixture(scope='module')
def correction_party(make_party, make_brand):
    suffix = str(generate_uuid7())
    return make_party(
        make_brand(f'b2correction{suffix}', f'B2 Correction {suffix}'),
        PartyID(f'b2correction-{suffix}'),
        f'B2 Correction {suffix}',
    )


@pytest.fixture(scope='module')
def correction_users(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'B2Correction{suffix}{i}') for i in range(13)]


@pytest.fixture(autouse=True)
def rollback_after_test():
    yield
    repo.rollback_session()


def setup_tournament(kind, party, users, *, size=8, point_table=None):
    admin = users[-1]
    args = dict(
        contestant_type=ContestantType.SOLO,
        point_table=point_table or [5, 3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
    )
    if kind == 'plain':
        args.update(
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
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
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_qualifier_count=size,
            playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
        )
    tournament, _ = tournament_service.create_tournament(
        party.id, f'B2 Correction {generate_uuid7()}', **args
    ).unwrap()
    for user in users[:size]:
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
        board = seeding.get_board(tournament.id, initiator_id=admin.id).unwrap()
        seeding.generate_from_seeding(
            tournament.id, expected_version=board.version, initiator_id=admin.id
        ).unwrap()
        tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, admin.id
        ).unwrap()
    else:
        for i, participant in enumerate(
            repo.get_participants_for_tournament(tournament.id), start=1
        ):
            scores.submit_score(
                tournament.id, i * 10, participant_id=participant.id
            ).unwrap()
        scores.close_leaderboard(tournament.id, initiator_id=admin.id).unwrap()
    return tournament, admin


def persisted(tournament):
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                'SELECT tournament_status, winner_participant_id '
                'FROM lan_tournaments WHERE id = :id'
            ),
            {'id': tournament.id},
        ).one()


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
@pytest.mark.parametrize(
    'entrypoint', ['direct', 'preparation', 'existing-draft']
)
@pytest.mark.parametrize('correct_empty', [False, True])
def test_corrected_source_retracts_and_automatically_recompletes(
    kind,
    entrypoint,
    correct_empty,
    correction_party,
    correction_users,
    monkeypatch,
):
    tournament, admin = setup_tournament(
        kind, correction_party, correction_users
    )
    first = lobbies(tournament, None, 0)
    assert len(first) == 2
    source = first[0]
    for match in first:
        play(match, admin)
    old, new, *rest = members(source)
    if entrypoint == 'existing-draft':
        target = seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        board = seeding.get_board(
            tournament.id, target=target, initiator_id=admin.id
        ).unwrap()
    for cid in members(first[1]):
        participants.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=admin
        ).unwrap()
    assert (
        repo.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )
    if entrypoint == 'direct':
        completion = matches.advance_ffa_round(
            tournament.id, initiator_id=admin.id
        )
    elif entrypoint == 'preparation':
        completion = seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        )
    else:
        completion = seeding.generate_from_seeding(
            tournament.id,
            target=target,
            expected_version=board.version,
            initiator_id=admin.id,
        )
    assert completion.unwrap() == 'completed'
    assert persisted(tournament) == ('COMPLETED', UUID(old))
    assert len(entries(tournament, 'bracket-single-survivor')) == 1
    received = []
    retracted = []
    commits = []
    locks = []
    real_commit = repo.commit_session
    real_tournament_lock = repo.lock_tournament_for_update
    real_match_lock = repo.get_match_for_update

    def committed():
        real_commit()
        commits.append(True)

    def tournament_lock(tid):
        locks.append('tournament')
        return real_tournament_lock(tid)

    def match_lock(mid):
        assert locks and locks[0] == 'tournament'
        locks.append('match')
        return real_match_lock(mid)

    def completed(sender, *, event):
        if event.tournament_id == tournament.id:
            assert commits == [True]
            assert persisted(tournament) == (
                'COMPLETED',
                event.winner_participant_id,
            )
            received.append(event)

    def uncompleted(sender, *, event):
        if event.tournament_id == tournament.id:
            assert commits == [True]
            assert persisted(tournament) == ('ONGOING', None)
            retracted.append(event)

    monkeypatch.setattr(repo, 'commit_session', committed)
    monkeypatch.setattr(repo, 'lock_tournament_for_update', tournament_lock)
    monkeypatch.setattr(repo, 'get_match_for_update', match_lock)
    order = [new, old, *rest]
    correction = first[1] if correct_empty else source
    signals.tournament_completed.connect(completed)
    signals.tournament_uncompleted.connect(uncompleted)
    try:
        for cycle in range(2):
            commits.clear()
            locks.clear()
            result = matches.unconfirm_match(
                correction.id, admin.id, reason='Correct B2 survivor'
            )
            assert result.is_ok(), result.unwrap_err()
            assert commits == [True]
            assert persisted(tournament) == ('ONGOING', None)
            assert len(retracted) == cycle + 1
            audit = entries(tournament, 'match-result-retracted')[-1]
            assert audit.data['reason'] == 'Correct B2 survivor'
            assert audit.data['retracted_placements']
            if correct_empty:
                # The empty lobby is a confirmation dependency, not the winner source.
                matches.unconfirm_match(
                    source.id, admin.id, reason='Change survivor'
                ).unwrap()
            if cycle:
                order = [old, new, *rest]
            matches.set_ffa_placements(
                source.id, {cid: i + 1 for i, cid in enumerate(order)}
            ).unwrap()
            commits.clear()
            locks.clear()
            matches.confirm_ffa_match(source.id, admin.id).unwrap()
            if correct_empty:
                assert persisted(tournament) == ('ONGOING', None)
                assert len(received) == cycle
                matches.set_ffa_placements(
                    correction.id,
                    {cid: i + 1 for i, cid in enumerate(members(correction))},
                ).unwrap()
                commits.clear()
                locks.clear()
                matches.confirm_ffa_match(correction.id, admin.id).unwrap()
            assert commits == [True]
            assert persisted(tournament) == ('COMPLETED', UUID(order[0]))
            assert len(received) == cycle + 1
            assert received[-1].winner_participant_id == UUID(order[0])
            before_audit = len(entries(tournament, 'ffa-match-confirmed'))
            assert matches.confirm_ffa_match(correction.id, admin.id).is_err()
            assert (
                len(entries(tournament, 'ffa-match-confirmed')) == before_audit
            )
            assert len(received) == cycle + 1
            assert (
                len(entries(tournament, 'bracket-single-survivor')) == cycle + 2
            )
            repo.rollback_session()
    finally:
        signals.tournament_completed.disconnect(completed)
        signals.tournament_uncompleted.disconnect(uncompleted)
    assert len(repo.get_matches_for_tournament(tournament.id)) == 2
    placements = {
        str(c.participant_id): c.placement
        for c in matches.get_contestants_for_match(source.id)
    }
    assert placements == {cid: i + 1 for i, cid in enumerate(order)}


def completed_b2(party, users, *, size=8, kind='plain'):
    tournament, admin = setup_tournament(kind, party, users, size=size)
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, admin)
    for match in first[1:]:
        for cid in members(match):
            participants.admin_remove_participant(
                tournament.id, TournamentParticipantID(cid), initiator=admin
            ).unwrap()
    assert (
        matches.advance_ffa_round(tournament.id, initiator_id=admin.id).unwrap()
        == 'completed'
    )
    return tournament, admin, first


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_three_source_lobbies_wait_for_every_correction_confirmation(
    correction_party, correction_users, kind
):
    tournament, admin, first = completed_b2(
        correction_party, correction_users, size=12, kind=kind
    )
    for match in first:
        matches.unconfirm_match(
            match.id, admin.id, reason='Correct all sources'
        ).unwrap()
    assert persisted(tournament) == ('ONGOING', None)
    order = members(first[0])
    order[0], order[1] = order[1], order[0]
    for i, match in enumerate(first):
        current = order if i == 0 else members(match)
        matches.set_ffa_placements(
            match.id, {cid: p + 1 for p, cid in enumerate(current)}
        ).unwrap()
        matches.confirm_ffa_match(match.id, admin.id).unwrap()
        assert persisted(tournament) == (
            ('COMPLETED', UUID(order[0])) if i == 2 else ('ONGOING', None)
        )
    assert len(repo.get_matches_for_tournament(tournament.id)) == 3
    assert len(entries(tournament, 'bracket-single-survivor')) == 2


@pytest.mark.parametrize('boundary', ['tie', 'zero', 'invalid'])
def test_correction_boundaries_never_keep_stale_winner(
    correction_party, correction_users, boundary
):
    tournament, admin, first = completed_b2(correction_party, correction_users)
    source = first[0]
    matches.unconfirm_match(
        source.id, admin.id, reason='Boundary correction'
    ).unwrap()
    order = members(source)
    if boundary == 'zero':
        repo.soft_delete_participants_by_ids(
            [TournamentParticipantID(UUID(cid)) for cid in order],
            datetime.now(UTC),
        )
        repo.commit_session()
    if boundary == 'invalid':
        result = matches.set_ffa_placements(source.id, {order[0]: 1})
        assert result.is_err()
        repo.rollback_session()
        assert repo.get_match(source.id).confirmed_by is None
    else:
        placements = {cid: i + 1 for i, cid in enumerate(order)}
        matches.set_ffa_placements(source.id, placements).unwrap()
        if boundary == 'tie':
            # Sequential placements can still tie in points at the cut.
            repo.update_contestant_placement_and_points(
                {
                    c.id: (c.placement, 5 if c.placement <= 2 else c.points)
                    for c in matches.get_contestants_for_match(source.id)
                }
            )
            repo.commit_session()
        matches.confirm_ffa_match(source.id, admin.id).unwrap()
    assert persisted(tournament) == ('ONGOING', None)
    assert len(entries(tournament, 'bracket-single-survivor')) == 1
    assert len(repo.get_matches_for_tournament(tournament.id)) == 2
    if boundary == 'tie':
        matches.unconfirm_match(
            source.id, admin.id, reason='Resolve cut tie'
        ).unwrap()
        matches.set_ffa_placements(
            source.id, {cid: i + 1 for i, cid in enumerate(order)}
        ).unwrap()
        matches.confirm_ffa_match(source.id, admin.id).unwrap()
        assert persisted(tournament) == ('COMPLETED', UUID(order[0]))


def test_flush_retraction_and_recompletion_remain_caller_rollback_owned(
    correction_party, correction_users, monkeypatch
):
    tournament, admin, first = completed_b2(correction_party, correction_users)
    source = first[0]
    old = UUID(members(source)[0])
    original = [
        (c.placement, c.points)
        for c in matches.get_contestants_for_match(source.id)
    ]
    completed = Mock()
    uncompleted = Mock()
    monkeypatch.setattr(signals.tournament_completed, 'send', completed)
    monkeypatch.setattr(signals.tournament_uncompleted, 'send', uncompleted)
    matches._lock_reachable_matches(source.id)
    result = matches._unconfirm_match_flush(
        source.id, admin.id, reason='Rollback correction'
    )
    assert result.is_ok()
    assert result.unwrap()[2] is True
    assert (
        repo.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )
    assert persisted(tournament) == ('COMPLETED', old)
    completed.assert_not_called()
    uncompleted.assert_not_called()
    repo.rollback_session()
    assert repo.get_match(source.id).confirmed_by is not None
    assert [
        (c.placement, c.points)
        for c in matches.get_contestants_for_match(source.id)
    ] == original
    assert not entries(tournament, 'match-result-retracted')
    matches.unconfirm_match(
        source.id, admin.id, reason='Real correction'
    ).unwrap()
    matches.set_ffa_placements(
        source.id, {cid: i + 1 for i, cid in enumerate(members(source))}
    ).unwrap()
    uncompleted.reset_mock()
    repo.lock_tournament_for_update(tournament.id)
    repo.confirm_match(source.id, admin.id)
    current = repo.get_tournament(tournament.id)
    plan = matches._single_survivor_source_plan(
        repo.get_match(source.id), current
    )
    assert plan is not None
    result = matches.complete_ffa_single_survivor(current, plan, admin.id)
    assert result.is_ok()
    assert persisted(tournament) == ('ONGOING', None)
    completed.assert_not_called()
    uncompleted.assert_not_called()
    repo.rollback_session()
    assert persisted(tournament) == ('ONGOING', None)
    assert repo.get_match(source.id).confirmed_by is None
    assert len(entries(tournament, 'bracket-single-survivor')) == 1


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_multiple_source_survivors_do_not_automatically_complete(
    correction_party, correction_users, kind
):
    tournament, admin = setup_tournament(
        kind, correction_party, correction_users
    )
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, admin)
    assert persisted(tournament) == ('ONGOING', None)
    matches.unconfirm_match(
        first[0].id, admin.id, reason='Nondeciding source'
    ).unwrap()
    order = members(first[0])
    order[0], order[1] = order[1], order[0]
    matches.set_ffa_placements(
        first[0].id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(first[0].id, admin.id).unwrap()
    assert persisted(tournament) == ('ONGOING', None)
    assert not entries(tournament, 'bracket-single-survivor')
    assert len(repo.get_matches_for_tournament(tournament.id)) == 2


def test_commit_failure_leaves_recompletion_rollback_owned_without_dispatch(
    correction_party, correction_users, monkeypatch
):
    tournament, admin, first = completed_b2(correction_party, correction_users)
    source = first[0]
    matches.unconfirm_match(
        source.id, admin.id, reason='Commit failure'
    ).unwrap()
    matches.set_ffa_placements(
        source.id, {cid: i + 1 for i, cid in enumerate(members(source))}
    ).unwrap()
    completed = Mock()
    monkeypatch.setattr(signals.tournament_completed, 'send', completed)
    before = len(entries(tournament, 'ffa-match-confirmed'))

    def fail_commit():
        assert persisted(tournament) == ('ONGOING', None)
        raise RuntimeError('caller_commit_failed')

    monkeypatch.setattr(repo, 'commit_session', fail_commit)
    with pytest.raises(RuntimeError, match='caller_commit_failed'):
        matches.confirm_ffa_match(source.id, admin.id)
    completed.assert_not_called()
    repo.rollback_session()
    assert persisted(tournament) == ('ONGOING', None)
    assert repo.get_match(source.id).confirmed_by is None
    assert len(entries(tournament, 'ffa-match-confirmed')) == before
    assert len(entries(tournament, 'bracket-single-survivor')) == 1


@pytest.mark.parametrize('kind', ['plain', 'highscore'])
def test_public_zero_removal_fk_begins_only_after_terminal_transition(
    correction_party, correction_users, kind
):
    tournament, admin, first = completed_b2(
        correction_party, correction_users, kind=kind
    )
    source = first[0]
    matches.unconfirm_match(
        source.id, admin.id, reason='Classify removal'
    ).unwrap()
    assert persisted(tournament) == ('ONGOING', None)
    trace = []
    for cid in members(source):
        before = persisted(tournament)
        try:
            participants.admin_remove_participant(
                tournament.id, TournamentParticipantID(cid), initiator=admin
            ).unwrap()
        except IntegrityError as error:
            assert before[0] == 'COMPLETED'
            assert error.orig.diag.constraint_name == (
                'lan_tournament_match_contestants_participant_id_fkey'
            )
            repo.rollback_session()
            assert persisted(tournament) == before
            trace.append((before[0], 'terminal_fk'))
            break
        trace.append((before[0], persisted(tournament)[0]))
    assert trace == [
        ('ONGOING', 'ONGOING'),
        ('ONGOING', 'ONGOING'),
        ('ONGOING', 'COMPLETED'),
        ('COMPLETED', 'terminal_fk'),
    ]
    print('Public zero-removal classification:', kind, trace)


@pytest.mark.parametrize(
    'writer', ['set_tournament_winner', 'set_tournament_status_flush']
)
def test_failed_recompletion_rolls_back_match_and_atomic_audit(
    correction_party, correction_users, monkeypatch, writer
):
    tournament, admin, first = completed_b2(correction_party, correction_users)
    source = first[0]
    matches.unconfirm_match(source.id, admin.id, reason='Fail write').unwrap()
    matches.set_ffa_placements(
        source.id, {cid: i + 1 for i, cid in enumerate(members(source))}
    ).unwrap()
    before = len(entries(tournament, 'ffa-match-confirmed'))
    monkeypatch.setattr(
        repo, writer, lambda *args, **kwargs: Err('write_failed')
    )
    result = matches.confirm_ffa_match(source.id, admin.id)
    assert result.is_err() and result.unwrap_err() == 'write_failed'
    assert persisted(tournament) == ('ONGOING', None)
    assert repo.get_match(source.id).confirmed_by is None
    assert len(entries(tournament, 'ffa-match-confirmed')) == before
    assert len(entries(tournament, 'bracket-single-survivor')) == 1


def test_a_sole_survivor_completion_locks_its_source_decisions(
    correction_party, correction_users
):
    tournament, admin = setup_tournament(
        'plain', correction_party, correction_users, point_table=[1, 1, 1, 1]
    )
    first = lobbies(tournament, None, 0)
    for match in first:
        play(match, admin)
    for cid in members(first[1]):
        participants.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=admin
        ).unwrap()
    scope = matches.ffa_lobby_scope(first[0])
    tied = members(first[0])
    qualification.save_decision(
        tournament.id, scope, tied, reason='x', initiator_id=admin.id
    ).unwrap()
    assert (
        matches.advance_ffa_round(tournament.id, initiator_id=admin.id).unwrap()
        == 'completed'
    )
    before = persisted(tournament)
    assert before == ('COMPLETED', UUID(tied[0]))

    withdrawn = qualification.withdraw_decision(
        tournament.id,
        scope,
        contestant_ids=tied,
        reason='x',
        initiator_id=admin.id,
    )
    saved = qualification.save_decision(
        tournament.id,
        scope,
        list(reversed(tied)),
        reason='x',
        initiator_id=admin.id,
    )
    assert withdrawn.is_err()
    assert saved.is_err()
    assert persisted(tournament) == before

    completed = repo.get_tournament(tournament.id)
    for source in first:
        assert matches.ffa_round_consumed(source, completed) is True
        assert matches.ffa_round_already_advanced(source, completed) is False
