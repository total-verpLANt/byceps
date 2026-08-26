"""
tests.integration.services.lan_tournament.test_fc3_same_winner_correction
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A score-only correction that keeps the winner leaves the next matches alone.
"""

from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_invitation_service as invitations,
    tournament_log_service,
    tournament_match_service as engine,
    tournament_readiness_service as readiness,
    tournament_repository as repo,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.match_readiness import (
    DbMatchInvitation,
    DbMatchPairing,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    MatchSide,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    slug = uuid7().hex[-12:]
    brand = make_brand(f'fc3c-{slug}', 'FC3 same winner')
    return make_party(brand, PartyID(f'fc3c-{slug}'), 'FC3 same winner')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'Fc3c-{uuid7().hex[-12:]}') for _ in range(4)]


@pytest.fixture(scope='module')
def admin_user(make_user):
    return make_user(f'Fc3cAdmin-{uuid7().hex[-12:]}')


@pytest.fixture(scope='module')
def corrector_user(make_user):
    return make_user(f'Fc3cCorrector-{uuid7().hex[-12:]}')


@pytest.fixture
def make_world(party, users, admin_user, monkeypatch):
    # Invitations are recorded but never mailed.
    monkeypatch.setattr(invitations.jobqueue, 'enqueue', Mock())
    monkeypatch.setattr(invitations.jobqueue, 'enqueue_at', Mock())

    def make(mode):
        result = tournament_service.create_tournament(
            party.id,
            f'FC3 same winner {uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        user_by_participant = {}
        for user in users:
            participant_id = TournamentParticipantID(uuid7())
            repo.create_participant(
                TournamentParticipant(
                    id=participant_id,
                    tournament_id=tournament.id,
                    user_id=user.id,
                    created_at=datetime.now(UTC),
                    team_id=None,
                    substitute_player=False,
                )
            )
            user_by_participant[participant_id] = user.id
        db.session.commit()

        generate = (
            engine.generate_single_elimination_bracket
            if mode is EliminationMode.SINGLE_ELIMINATION
            else engine.generate_double_elimination_bracket
        )
        generated = generate(tournament.id)
        assert generated.is_ok(), generated.unwrap_err()
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, admin_user.id
        )
        assert started.is_ok(), started.unwrap_err()
        return SimpleNamespace(
            tournament_id=tournament.id,
            user_by_participant=user_by_participant,
        )

    yield make
    db.session.rollback()


def _matches(world):
    return repo.get_matches_for_tournament(world.tournament_id)


def _keys(match_id):
    return [
        c.participant_id
        for c in repo.get_contestants_for_match(match_id)
        if c.participant_id is not None
    ]


def _play(match_id, admin_user, *, winner_first=True, scores=(3, 1)):
    """Confirm the match; the first contestant wins unless told otherwise."""
    keys = _keys(match_id)
    assert len(keys) == 2
    high, low = scores
    first, second = (high, low) if winner_first else (low, high)
    result = engine.admin_set_and_confirm_match(
        match_id, admin_user.id, {keys[0]: first, keys[1]: second}
    )
    assert result.is_ok(), result.unwrap_err()
    return (keys[0], keys[1]) if winner_first else (keys[1], keys[0])


def _claim_both(world, match_id):
    pairing = repo.get_match_pairing(match_id)
    assert pairing is not None
    for side, identity in (
        (MatchSide.A, pairing.side_a),
        (MatchSide.B, pairing.side_b),
    ):
        match = repo.find_match_fresh(match_id)
        claimed = readiness.claim_ready_flush(
            match_id,
            side,
            world.user_by_participant[identity.id],
            expected_pairing_generation=match.pairing_generation,
            expected_readiness_revision=match.readiness_revision,
        )
        assert claimed.is_ok(), claimed.unwrap_err()
        repo.commit_session()


def _facts(match_id):
    """Committed readiness, pairing, invitation and contestant-row facts."""
    with Session(db.engine) as session:
        row = session.get(DbTournamentMatch, match_id)
        pairing = (
            session.get(DbMatchPairing, row.pairing_id)
            if row.pairing_id is not None
            else None
        )
        invites = session.scalars(
            select(DbMatchInvitation)
            .where(DbMatchInvitation.match_id == match_id)
            .order_by(DbMatchInvitation.id)
        ).all()
        contestants = session.scalars(
            select(DbTournamentMatchToContestant)
            .where(
                DbTournamentMatchToContestant.tournament_match_id == match_id
            )
            .order_by(
                DbTournamentMatchToContestant.created_at,
                DbTournamentMatchToContestant.id,
            )
        ).all()
        return SimpleNamespace(
            pairing_id=row.pairing_id,
            pairing_generation=row.pairing_generation,
            readiness_revision=row.readiness_revision,
            ready_at=(row.ready_at_a, row.ready_at_b),
            ready_by=(row.ready_by_a, row.ready_by_b),
            sides=(
                (pairing.side_a_id, pairing.side_b_id)
                if pairing is not None
                else None
            ),
            invitations=[
                (i.id, i.pairing_generation, i.recipient_id, i.status)
                for i in invites
            ],
            contestant_rows=[(c.id, c.participant_id) for c in contestants],
        )


def _scores(match_id):
    """Read the committed scores through an independent connection."""
    with Session(db.engine) as session:
        rows = session.scalars(
            select(DbTournamentMatchToContestant).where(
                DbTournamentMatchToContestant.tournament_match_id == match_id
            )
        ).all()
        return {row.participant_id: row.score for row in rows}


def _corrected_entries(tournament_id, match_id):
    """Read the committed correction entries through an independent connection."""
    with Session(db.engine) as session:
        rows = session.scalars(
            select(DbTournamentLogEntry).where(
                DbTournamentLogEntry.tournament_id == tournament_id,
                DbTournamentLogEntry.event_type == 'match-result-corrected',
            )
        ).all()
        return [
            SimpleNamespace(data=row.data)
            for row in rows
            if row.data['match_id'] == str(match_id)
        ]


def _se_final_world(make_world, admin_user):
    """Both feeders confirmed; the final holds two claimed sides."""
    world = make_world(EliminationMode.SINGLE_ELIMINATION)
    matches = _matches(world)
    final = next(
        m
        for m in matches
        if m.next_match_id is None and m.bracket is not Bracket.THIRD_PLACE
    )
    feeders = sorted(
        (m for m in matches if m.next_match_id == final.id),
        key=lambda m: m.match_order,
    )
    assert len(feeders) == 2
    first_winner, first_loser = _play(feeders[0].id, admin_user)
    _play(feeders[1].id, admin_user)
    assert len(_keys(final.id)) == 2
    return world, final, feeders[0], first_winner, first_loser


def test_same_winner_correction_leaves_the_next_match_untouched(
    make_world, admin_user, monkeypatch, corrector_user
):
    world, final, subject, winner, loser = _se_final_world(
        make_world, admin_user
    )
    _claim_both(world, final.id)

    before = _facts(final.id)
    assert before.pairing_id is not None
    assert all(before.ready_at) and all(before.ready_by)
    assert before.invitations
    pairing_generation = before.pairing_generation

    sends = {}
    for name in (
        'match_unconfirmed',
        'match_confirmed',
        'contestant_advanced',
        'match_created',
        'match_deleted',
        'match_ready',
        'tournament_completed',
        'tournament_uncompleted',
    ):
        sends[name] = Mock()
        monkeypatch.setattr(getattr(signals, name), 'send', sends[name])

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Score typo',
        corrected_scores={winner: 5, loser: 2},
    )
    assert result.is_ok(), result.unwrap_err()

    after = _facts(final.id)
    assert after.pairing_generation == pairing_generation
    assert after == before

    assert _scores(subject.id) == {winner: 5, loser: 2}
    assert repo.find_match_fresh(subject.id).confirmed_by == corrector_user.id

    entries = _corrected_entries(world.tournament_id, subject.id)
    assert len(entries) == 1
    data = entries[0].data
    assert data['scores_applied'] is True
    assert data['previous_scores'] == {str(winner): 3, str(loser): 1}
    assert data['new_scores'] == {str(winner): 5, str(loser): 2}
    assert data['reason'] == 'Score typo'

    case, scores_applied = result.unwrap()
    assert case is CorrectionCase.NO_DOWNSTREAM
    assert scores_applied is True

    for name in (
        'contestant_advanced',
        'match_created',
        'match_deleted',
        'match_ready',
        'tournament_completed',
        'tournament_uncompleted',
    ):
        assert not sends[name].called, name
    (unconfirmed,) = [
        c.kwargs['event'] for c in sends['match_unconfirmed'].call_args_list
    ]
    (confirmed,) = [
        c.kwargs['event'] for c in sends['match_confirmed'].call_args_list
    ]
    assert unconfirmed.match_id == confirmed.match_id == subject.id
    assert unconfirmed.unconfirmed_by == corrector_user.id
    assert confirmed.winner_participant_id == winner


def test_same_winner_correction_keeps_confirmed_downstream_without_acknowledgement(
    make_world, admin_user, corrector_user
):
    world, final, subject, winner, loser = _se_final_world(
        make_world, admin_user
    )
    final_winner, _ = _play(final.id, admin_user)
    final_before = _facts(final.id)
    assert (
        tournament_service.get_tournament(world.tournament_id).tournament_status
        is TournamentStatus.COMPLETED
    )

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Score typo',
        corrected_scores={winner: 9, loser: 0},
        acknowledged_match_ids=[],
    )
    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == (CorrectionCase.NO_DOWNSTREAM, True)

    assert _facts(final.id) == final_before
    assert repo.find_match_fresh(subject.id).confirmed_by == corrector_user.id
    assert repo.find_match_fresh(final.id).confirmed_by == admin_user.id
    tournament = tournament_service.get_tournament(world.tournament_id)
    assert tournament.tournament_status is TournamentStatus.COMPLETED
    assert tournament.winner_participant_id == final_winner
    assert _scores(subject.id) == {winner: 9, loser: 0}


def test_same_winner_correction_keeps_a_lone_advanced_contestant(
    make_world, admin_user, corrector_user
):
    world = make_world(EliminationMode.SINGLE_ELIMINATION)
    matches = _matches(world)
    final = next(
        m
        for m in matches
        if m.next_match_id is None and m.bracket is not Bracket.THIRD_PLACE
    )
    subject = sorted(
        (m for m in matches if m.next_match_id == final.id),
        key=lambda m: m.match_order,
    )[0]
    winner, loser = _play(subject.id, admin_user)
    before = _facts(final.id)
    assert [pid for _, pid in before.contestant_rows] == [winner]

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Score typo',
        corrected_scores={winner: 4, loser: 3},
    )
    assert result.is_ok(), result.unwrap_err()
    assert _facts(final.id) == before
    assert repo.find_match_fresh(subject.id).confirmed_by == corrector_user.id


def test_same_winner_correction_leaves_winner_and_loser_matches_untouched_in_de(
    make_world, admin_user, corrector_user
):
    world = make_world(EliminationMode.DOUBLE_ELIMINATION)
    matches = _matches(world)
    feeders = sorted(
        (
            m
            for m in matches
            if m.bracket is Bracket.WINNERS
            and m.loser_next_match_id is not None
            and len(_keys(m.id)) == 2
        ),
        key=lambda m: m.match_order,
    )
    assert len(feeders) == 2
    subject = feeders[0]
    winner, loser = _play(subject.id, admin_user)
    _play(feeders[1].id, admin_user)

    winners_next = repo.find_match_fresh(subject.id).next_match_id
    losers_next = repo.find_match_fresh(subject.id).loser_next_match_id
    assert winners_next != losers_next
    _claim_both(world, winners_next)
    _claim_both(world, losers_next)
    winners_before = _facts(winners_next)
    losers_before = _facts(losers_next)
    assert winner in [pid for _, pid in winners_before.contestant_rows]
    assert loser in [pid for _, pid in losers_before.contestant_rows]

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Score typo',
        corrected_scores={winner: 6, loser: 5},
    )
    assert result.is_ok(), result.unwrap_err()

    assert _facts(winners_next) == winners_before
    assert _facts(losers_next) == losers_before
    assert _scores(subject.id) == {winner: 6, loser: 5}
    assert repo.find_match_fresh(subject.id).confirmed_by == corrector_user.id
    assert len(_corrected_entries(world.tournament_id, subject.id)) == 1


def test_winner_changing_correction_still_resets_the_next_match(
    make_world, admin_user, corrector_user
):
    world, final, subject, winner, loser = _se_final_world(
        make_world, admin_user
    )
    _claim_both(world, final.id)
    before = _facts(final.id)

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Wrong winner',
        corrected_scores={winner: 1, loser: 3},
        ack_critical=True,
    )
    assert result.is_ok(), result.unwrap_err()

    after = _facts(final.id)
    assert after.ready_at == (None, None)
    assert after.ready_by == (None, None)
    assert after.pairing_generation > before.pairing_generation
    assert after.readiness_revision > before.readiness_revision
    assert loser in [pid for _, pid in after.contestant_rows]
    assert winner not in [pid for _, pid in after.contestant_rows]
    assert _scores(subject.id) == {winner: 1, loser: 3}


def test_same_winner_correction_with_identical_scores_is_still_refused(
    make_world, admin_user, corrector_user
):
    world, final, subject, winner, loser = _se_final_world(
        make_world, admin_user
    )
    before = _facts(final.id)

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Nothing changed',
        corrected_scores={winner: 3, loser: 1},
    )
    assert result.is_err()
    assert _facts(final.id) == before
    assert _scores(subject.id) == {winner: 3, loser: 1}
    assert not _corrected_entries(world.tournament_id, subject.id)


def test_same_winner_correction_of_an_unconfirmed_match_is_refused(
    make_world, admin_user, corrector_user
):
    world = make_world(EliminationMode.SINGLE_ELIMINATION)
    subject = next(
        m
        for m in _matches(world)
        if m.next_match_id is not None and len(_keys(m.id)) == 2
    )
    keys = _keys(subject.id)

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Not played yet',
        corrected_scores={keys[0]: 3, keys[1]: 1},
    )
    assert result.is_err()
    assert repo.find_match_fresh(subject.id).confirmed_by is None
    assert not _corrected_entries(world.tournament_id, subject.id)


def test_same_winner_correction_of_a_reported_unconfirmed_match_is_refused(
    make_world, admin_user, corrector_user
):
    world = make_world(EliminationMode.SINGLE_ELIMINATION)
    subject = next(
        m
        for m in _matches(world)
        if m.next_match_id is not None and len(_keys(m.id)) == 2
    )
    winner, loser = _keys(subject.id)
    for key, score in ((winner, 3), (loser, 1)):
        assert engine.set_score(subject.id, key, score).is_ok()

    result = engine.correct_match_result(
        subject.id,
        corrector_user.id,
        reason='Not confirmed yet',
        corrected_scores={winner: 5, loser: 2},
    )
    assert result.is_err()
    assert repo.find_match_fresh(subject.id).confirmed_by is None
    assert _scores(subject.id) == {winner: 3, loser: 1}
    assert not _corrected_entries(world.tournament_id, subject.id)


def test_same_winner_correction_of_the_deciding_match_takes_the_full_path(
    make_world, admin_user, corrector_user
):
    world, final, _subject, _winner, _loser = _se_final_world(
        make_world, admin_user
    )
    champion, runner_up = _play(final.id, admin_user)

    result = engine.correct_match_result(
        final.id,
        corrector_user.id,
        reason='Score typo',
        corrected_scores={champion: 7, runner_up: 6},
    )
    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == (CorrectionCase.NO_DOWNSTREAM, True)

    assert _scores(final.id) == {champion: 7, runner_up: 6}
    assert repo.find_match_fresh(final.id).confirmed_by == corrector_user.id
    tournament = tournament_service.get_tournament(world.tournament_id)
    assert tournament.tournament_status is TournamentStatus.COMPLETED
    assert tournament.winner_participant_id == champion
    event_types = [
        e.event_type
        for e in tournament_log_service.get_entries_for_tournament(
            world.tournament_id
        )
        if e.data.get('match_id') == str(final.id)
    ]
    assert 'match-result-retracted' in event_types
    assert 'match-result-corrected' in event_types


def test_same_winner_correction_of_the_first_grand_final_keeps_the_reset_match(
    make_world, admin_user, corrector_user
):
    world = make_world(EliminationMode.DOUBLE_ELIMINATION)
    for _ in range(40):
        matches = _matches(world)
        grand_final = next(
            (
                m
                for m in matches
                if m.bracket is Bracket.GRAND_FINAL and m.match_order == 0
            ),
            None,
        )
        playable = [
            m
            for m in matches
            if m.confirmed_by is None
            and (grand_final is None or m.id != grand_final.id)
            and len(_keys(m.id)) == 2
        ]
        if not playable:
            break
        _play(playable[0].id, admin_user)
    assert grand_final is not None

    lb_final = next(
        m
        for m in matches
        if m.bracket is Bracket.LOSERS and m.next_match_id == grand_final.id
    )
    lb_champion = max(
        repo.get_contestants_for_match(lb_final.id),
        key=lambda c: c.score or 0,
    ).participant_id
    wb_champion = next(
        key for key in _keys(grand_final.id) if key != lb_champion
    )
    confirmed = engine.admin_set_and_confirm_match(
        grand_final.id,
        admin_user.id,
        {lb_champion: 10, wb_champion: 1},
    )
    assert confirmed.is_ok(), confirmed.unwrap_err()

    reset_id = repo.find_match_fresh(grand_final.id).next_match_id
    assert reset_id is not None
    _claim_both(world, reset_id)
    reset_before = _facts(reset_id)

    result = engine.correct_match_result(
        grand_final.id,
        corrector_user.id,
        reason='Score typo',
        corrected_scores={lb_champion: 10, wb_champion: 3},
    )
    assert result.is_ok(), result.unwrap_err()
    assert result.unwrap() == (CorrectionCase.NO_DOWNSTREAM, True)

    assert repo.find_match_fresh(grand_final.id).next_match_id == reset_id
    assert (
        repo.find_match_fresh(grand_final.id).confirmed_by == corrector_user.id
    )
    assert _facts(reset_id) == reset_before
    assert _scores(grand_final.id) == {lb_champion: 10, wb_champion: 3}
    assert (
        tournament_service.get_tournament(world.tournament_id).tournament_status
        is TournamentStatus.ONGOING
    )
