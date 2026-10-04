"""
tests.integration.services.lan_tournament.test_ffa_cut_tie_decisions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The read model of FFA lobby cut ties, and the rule that a decision
stays put once the next round was built from it.
"""

from contextlib import contextmanager
from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_log_service,
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
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

from tests.integration.services.lan_tournament import (
    test_ffa_natural_undersized_wb as natural,
)


engine_party = natural.engine_party
engine_players = natural.engine_players
engine_admin = natural.engine_admin
make_engine = natural.make_engine


PARTY_ID = PartyID('lan-party-ffa-cut-tie-decisions')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ffacuttiedecbrand', 'FFA Cut Tie Decisions Brand')
    return make_party(brand, PARTY_ID, 'LAN Party FFA Cut Tie Decisions')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaCutTieDecPlayer{i}') for i in range(16)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaCutTieDecAdmin')


@pytest.fixture
def make_ffa(party, players, admin):
    created = []

    def _make(*, point_table, advancement_count=3, play=True, player_count=8, lobby_size=6):
        result = tournament_service.create_tournament(
            party.id,
            f'FFA Cut Tie Decisions {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            max_players=16,
            group_size_min=2,
            group_size_max=lobby_size,
            advancement_count=advancement_count,
            point_table=point_table,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in players[:player_count]:
            tournament_repository.create_participant(
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
        generated = tournament_match_service.generate_ffa_round(
            tournament.id, initiator_id=admin.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        started = tournament_service.change_status(
            tournament.id, TournamentStatus.ONGOING, admin.id
        )
        assert started.is_ok(), started.unwrap_err()
        if play:
            for match in _round(tournament, 0):
                _play(match, admin)
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _round(tournament, round_number):
    return sorted(
        tournament_repository.get_matches_for_round(
            tournament.id, round_number
        ),
        key=lambda m: m.group_order or 0,
    )


def _members(match):
    """Return the lobby's contestants by recorded placement, then ID."""
    contestants = tournament_match_service.get_contestants_for_match(match.id)
    return [
        str(c.participant_id)
        for c in sorted(
            contestants,
            key=lambda c: (
                c.placement is None,
                c.placement or 0,
                str(c.participant_id),
            ),
        )
    ]


def _play(match, admin):
    """Place the lobby in its natural order and confirm it."""
    placed = tournament_match_service.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(_members(match))}
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(match.id, admin.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()


def _tied_in(match):
    """Return the contestants of places 3 and 4, in placement order."""
    return _members(match)[2:]


def _decide_all(tournament, admin):
    for match in _round(tournament, 0):
        scope = tournament_match_service.ffa_lobby_scope(match)
        saved = tournament_qualification_service.save_decision(
            tournament.id,
            scope,
            sorted(_tied_in(match)),
            reason='Decided at the table.',
            initiator_id=admin.id,
        )
        assert saved.is_ok(), saved.unwrap_err()


def _advance(tournament, admin):
    """Draft the next round and build its lobbies."""
    target = tournament_seeding_service.prepare_ffa_round_draft(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    board = tournament_seeding_service.get_board(tournament.id, target).unwrap()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id,
        target,
        expected_version=board.version,
        initiator_id=admin.id,
    )
    assert generated.is_ok(), generated.unwrap_err()


def _decisions(tournament):
    return tournament_qualification_repository.get_decisions_for_tournament(
        tournament.id
    )


TIED_TABLE = [3, 2, 1, 1]
CLEAN_TABLE = [4, 3, 2, 1]
SEEDING_TIE_TABLE = [2, 2, 1, 0]


def _count_report_queries(tournament):
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    with event_listener(db.engine, record):
        report = tournament_qualification_service.get_ffa_decision_report(tournament.id)
    print(f'FFA report tournament={tournament.id}: {len(statements)} statements')
    return report, len(statements)


@contextmanager
def event_listener(engine, record):
    from sqlalchemy import event

    event.listen(engine, 'before_cursor_execute', record)
    try:
        yield
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def test_cut_tie_report_query_count_is_constant(make_ffa):
    counts = []
    for size in (8, 16):
        tournament = make_ffa(point_table=[3, 2, 2, 1], advancement_count=2,
                              player_count=size, lobby_size=4)
        report, count = _count_report_queries(tournament)
        assert len(report.ties) == size // 4
        assert all(not tie.locked and not tie.decided for tie in report.ties)
        counts.append(count)
    assert counts[0] == counts[1] <= 10, counts


def test_completed_b2_report_query_count_is_constant(make_ffa, admin):
    counts = []
    for size in (8, 16):
        tournament = make_ffa(point_table=[1, 1, 1, 1], advancement_count=1,
                              player_count=size, lobby_size=4)
        lobbies = _round(tournament, 0)
        _scope = tournament_match_service.ffa_lobby_scope
        # Store decisions in all lobbies, then empty all but one: those
        # stored blocks are outdated while the survivor lobby stays decided.
        for lobby in lobbies:
            tournament_qualification_service.save_decision(
                tournament.id, _scope(lobby), sorted(_members(lobby)),
                reason='Decided at the table.', initiator_id=admin.id,
            ).unwrap()
        for lobby in lobbies[1:]:
            for cid in _members(lobby):
                tournament_participant_service.admin_remove_participant(
                    tournament.id, TournamentParticipantID(cid), initiator=admin
                ).unwrap()
        assert tournament_match_service.advance_ffa_round(
            tournament.id, initiator_id=admin.id
        ).unwrap() == 'completed'
        done = tournament_repository.get_tournament(tournament.id)
        assert all(tournament_match_service.ffa_round_consumed(m, done) for m in lobbies)
        expected = tournament_qualification_service.get_ffa_decision_report(tournament.id)
        report, count = _count_report_queries(tournament)
        assert report == expected
        assert len(report.ties) == 1
        assert all(tie.decided and tie.locked for tie in report.ties)
        assert len(report.outdated) == len(lobbies) - 1
        assert all(block.locked for block in report.outdated)
        counts.append(count)
    assert counts[0] == counts[1] <= 10, counts


def test_auto_release_failure_after_commit_is_logged_not_raised(make_ffa, admin, monkeypatch, caplog):
    tournament = make_ffa(point_table=TIED_TABLE)
    lobby = _round(tournament, 0)[0]
    scope = tournament_match_service.ffa_lobby_scope(lobby)
    def fail(*args, **kwargs):
        raise RuntimeError('injected release failure')
    monkeypatch.setattr(tournament_qualification_service, 'try_auto_release', fail)
    result = tournament_qualification_service.save_decision(
        tournament.id, scope, sorted(_tied_in(lobby)),
        reason='Committed despite release failure.', initiator_id=admin.id,
    )
    assert result.is_ok(), result.unwrap_err()
    db.session.expire_all()
    assert scope in _decisions(tournament)
    assert 'Automatic playoff release failed' in caplog.text


def test_a_tie_in_a_lone_winners_lobby_within_the_cut_is_decidable(
    make_engine, engine_admin
):
    tournament = make_engine(
        'plain', size=16, minimum=3, cut=2, point_table=[1, 1, 0, 0]
    )
    wb = natural.reach_lone_wb_round_three(tournament, engine_admin)
    natural.base.play(wb, engine_admin)
    scope = tournament_match_service.ffa_lobby_scope(wb)
    ties = tournament_qualification_service.get_ffa_cut_ties(tournament.id)
    assert scope in [tie.scope for tie in ties]
    result = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS
    )
    assert result == Err(tournament_match_service.QUALIFICATION_TIE_ERROR)
    saved = tournament_qualification_service.save_decision(
        tournament.id,
        scope,
        natural.base.members(wb),
        reason='Decided at the table.',
        initiator_id=engine_admin.id,
    )
    assert saved.is_ok(), saved.unwrap_err()
    for match in natural.base.repo.get_matches_for_tournament(tournament.id):
        if match.confirmed_by is None:
            natural.base.play(match, engine_admin)
    result = tournament_match_service.advance_ffa_round(
        tournament.id, pool=Bracket.WINNERS
    )
    if result == Err(tournament_match_service.FFA_LOBBY_BELOW_MINIMUM_ERROR):
        tournament_match_service.advance_ffa_round(
            tournament.id, pool=Bracket.LOSERS
        ).unwrap()
        for match in natural.base.repo.get_matches_for_tournament(tournament.id):
            if match.confirmed_by is None:
                natural.base.play(match, engine_admin)
        result = tournament_match_service.advance_ffa_round(
            tournament.id, pool=Bracket.WINNERS
        )
    assert result.is_ok(), result.unwrap_err()


# -------------------------------------------------------------------- #
# read model


def test_cut_ties_lists_the_open_tie_of_every_blocking_lobby(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    lobbies = _round(tournament, 0)

    ties = tournament_qualification_service.get_ffa_cut_ties(tournament.id)

    assert [t.scope for t in ties] == [
        tournament_match_service.ffa_lobby_scope(m) for m in lobbies
    ]
    for tie, lobby in zip(ties, lobbies, strict=True):
        assert tie.pool == 'SE'
        assert tie.round_number == 0
        assert tie.lobby == (lobby.group_order or 0)
        assert (tie.rank_from, tie.rank_to) == (3, 4)
        assert tie.decided is False
        assert tie.decision is None
        assert tie.locked is False
        assert {(c.contestant_id, c.points) for c in tie.contestants} == {
            (cid, 1) for cid in _tied_in(lobby)
        }


def test_cut_ties_is_empty_for_a_lobby_without_a_tie(make_ffa):
    tournament = make_ffa(point_table=CLEAN_TABLE, advancement_count=2)

    assert (
        tournament_qualification_service.get_ffa_cut_ties(tournament.id) == ()
    )


def test_cut_ties_ignores_a_lobby_that_is_not_confirmed(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE, play=False)
    first, _second = _round(tournament, 0)
    _play(first, admin)

    ties = tournament_qualification_service.get_ffa_cut_ties(tournament.id)

    assert [t.scope for t in ties] == [
        tournament_match_service.ffa_lobby_scope(first)
    ]


def test_cut_ties_shows_the_saved_decision(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    first = _round(tournament, 0)[0]
    scope = tournament_match_service.ffa_lobby_scope(first)
    order = list(reversed(_tied_in(first)))
    saved = tournament_qualification_service.save_decision(
        tournament.id, scope, order, reason='Coin toss.', initiator_id=admin.id
    )
    assert saved.is_ok(), saved.unwrap_err()

    ties = {
        t.scope: t
        for t in tournament_qualification_service.get_ffa_cut_ties(
            tournament.id
        )
    }

    decided = ties[scope]
    assert decided.decided is True
    assert [c.contestant_id for c in decided.contestants] == order
    assert decided.decision is not None
    assert decided.decision.reason == 'Coin toss.'
    assert decided.decision.contestant_ids == tuple(order)
    assert decided.decision.decided_by == admin.id
    other = next(t for s, t in ties.items() if s != scope)
    assert other.decided is False


def test_reentered_lobby_reports_the_old_block_as_outdated(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    first = _round(tournament, 0)[0]
    scope = tournament_match_service.ffa_lobby_scope(first)
    members = _members(first)
    old_tie = _tied_in(first)
    saved = tournament_qualification_service.save_decision(
        tournament.id,
        scope,
        old_tie,
        reason='Coin toss.',
        initiator_id=admin.id,
    )
    assert saved.is_ok(), saved.unwrap_err()

    unconfirmed = tournament_match_service.unconfirm_match(first.id, admin.id)
    assert unconfirmed.is_ok(), unconfirmed.unwrap_err()
    replaced = [members[0], members[2], members[1], members[3]]
    placed = tournament_match_service.set_ffa_placements(
        first.id, {cid: i + 1 for i, cid in enumerate(replaced)}
    )
    assert placed.is_ok(), placed.unwrap_err()
    confirmed = tournament_match_service.confirm_ffa_match(first.id, admin.id)
    assert confirmed.is_ok(), confirmed.unwrap_err()

    report = tournament_qualification_service.get_ffa_decision_report(
        tournament.id
    )

    (outdated,) = report.outdated
    assert outdated.scope == scope
    assert outdated.block.contestant_ids == tuple(old_tie)
    assert outdated.locked is False
    (new_tie,) = [t for t in report.ties if t.scope == scope]
    assert new_tie.decided is False
    assert new_tie.decision is None
    assert {c.contestant_id for c in new_tie.contestants} == {
        members[1],
        members[3],
    }


def test_cut_ties_writes_nothing(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    log_before = len(
        tournament_log_service.get_entries_for_tournament(tournament.id)
    )

    tournament_qualification_service.get_ffa_cut_ties(tournament.id)

    assert not _decisions(tournament)
    assert (
        len(tournament_log_service.get_entries_for_tournament(tournament.id))
        == log_before
    )


def test_cut_ties_follow_the_current_round(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    _decide_all(tournament, admin)
    _advance(tournament, admin)

    assert (
        tournament_qualification_service.get_ffa_cut_ties(tournament.id) == ()
    )


def test_cut_ties_of_a_plain_tournament_without_ffa_are_empty(party, players):
    result = tournament_service.create_tournament(
        party.id,
        f'FFA Cut Tie Decisions {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    tournament, _ = result.unwrap()
    try:
        assert (
            tournament_qualification_service.get_ffa_cut_ties(tournament.id)
            == ()
        )
    finally:
        tournament_service.delete_tournament(tournament.id)


# -------------------------------------------------------------------- #
# decisions after the next round (A3)


def test_decisions_are_allowed_before_the_next_round(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    first = _round(tournament, 0)[0]
    scope = tournament_match_service.ffa_lobby_scope(first)
    tied = sorted(_tied_in(first))

    saved = tournament_qualification_service.save_decision(
        tournament.id, scope, tied, reason='First.', initiator_id=admin.id
    )
    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        scope,
        contestant_ids=tied,
        reason='Changed my mind.',
        initiator_id=admin.id,
    )
    resaved = tournament_qualification_service.save_decision(
        tournament.id,
        scope,
        tied[::-1],
        reason='Second.',
        initiator_id=admin.id,
    )

    assert saved == Ok(None)
    assert withdrawn == Ok(None)
    assert resaved == Ok(None)


def test_withdraw_is_refused_once_the_next_round_exists(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    _decide_all(tournament, admin)
    _advance(tournament, admin)
    scope = tournament_match_service.ffa_lobby_scope(_round(tournament, 0)[0])
    before = _decisions(tournament)[scope]

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        scope,
        contestant_ids=before.blocks[0].contestant_ids,
        reason='Too late.',
        initiator_id=admin.id,
    )

    assert result == Err(tournament_qualification_service.ERR_FFA_ROUND_BUILT)
    assert _decisions(tournament)[scope] == before


def test_save_is_refused_once_the_next_round_exists(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    _decide_all(tournament, admin)
    _advance(tournament, admin)
    first = _round(tournament, 0)[0]
    scope = tournament_match_service.ffa_lobby_scope(first)
    before = _decisions(tournament)[scope]

    result = tournament_qualification_service.save_decision(
        tournament.id,
        scope,
        _tied_in(first),
        reason='Too late.',
        initiator_id=admin.id,
    )

    assert result == Err(tournament_qualification_service.ERR_FFA_ROUND_BUILT)
    assert _decisions(tournament)[scope] == before


def test_decision_is_refused_after_a_sole_survivor_completion(make_ffa, admin):
    tournament = make_ffa(point_table=[1, 1, 1, 1], advancement_count=1)
    first = _round(tournament, 0)
    for cid in _members(first[1]):
        tournament_participant_service.admin_remove_participant(
            tournament.id, TournamentParticipantID(cid), initiator=admin
        ).unwrap()
    scope = tournament_match_service.ffa_lobby_scope(first[0])
    tied = _members(first[0])
    saved = tournament_qualification_service.save_decision(
        tournament.id,
        scope,
        sorted(tied),
        reason='Decided at the table.',
        initiator_id=admin.id,
    )
    assert saved.is_ok(), saved.unwrap_err()
    advanced = tournament_match_service.advance_ffa_round(
        tournament.id, initiator_id=admin.id
    )
    assert advanced.unwrap() == 'completed'
    done = tournament_repository.get_tournament(tournament.id)
    assert done.tournament_status is TournamentStatus.COMPLETED
    before = _decisions(tournament)[scope]

    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        scope,
        contestant_ids=before.blocks[0].contestant_ids,
        reason='Flip it.',
        initiator_id=admin.id,
    )
    resaved = tournament_qualification_service.save_decision(
        tournament.id,
        scope,
        list(reversed(sorted(tied))),
        reason='Flip it.',
        initiator_id=admin.id,
    )

    assert withdrawn == Err(
        tournament_qualification_service.ERR_DECISION_COMPLETED
    )
    assert resaved == Err(
        tournament_qualification_service.ERR_DECISION_COMPLETED
    )
    assert _decisions(tournament)[scope] == before
    after = tournament_repository.get_tournament(tournament.id)
    assert after.winner_participant_id == done.winner_participant_id


def test_refused_decisions_leave_no_audit_entry(make_ffa, admin):
    tournament = make_ffa(point_table=TIED_TABLE)
    _decide_all(tournament, admin)
    _advance(tournament, admin)
    scope = tournament_match_service.ffa_lobby_scope(_round(tournament, 0)[0])
    count_before = len(
        tournament_log_service.get_entries_for_tournament(tournament.id)
    )

    tournament_qualification_service.withdraw_decision(
        tournament.id,
        scope,
        contestant_ids=_decisions(tournament)[scope].blocks[0].contestant_ids,
        reason='Too late.',
        initiator_id=admin.id,
    )

    assert (
        len(tournament_log_service.get_entries_for_tournament(tournament.id))
        == count_before
    )


# -------------------------------------------------------------------- #
# highscore to FFA: the phase-2 lobbies


def test_highscore_to_ffa_lobby_tie_across_the_cut_needs_a_decision(
    party, players, admin
):
    result = tournament_service.create_tournament(
        party.id,
        f'FFA Cut Tie Decisions {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=8,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
        point_table=TIED_TABLE,
        group_size_min=4,
        group_size_max=6,
        advancement_count=3,
    )
    tournament, _ = result.unwrap()
    try:
        for player in players:
            tournament_repository.create_participant(
                TournamentParticipant(
                    id=TournamentParticipantID(generate_uuid7()),
                    user_id=player.id,
                    tournament_id=tournament.id,
                    substitute_player=False,
                    team_id=None,
                    created_at=datetime.now(UTC),
                )
            )
        db.session.commit()
        participants = tournament_repository.get_participants_for_tournament(
            tournament.id
        )
        for value, participant in enumerate(participants, start=1):
            tournament_score_service.submit_score(
                tournament.id, value * 10, participant_id=participant.id
            ).unwrap()
        tournament_score_service.close_leaderboard(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        lobbies = sorted(
            (
                m
                for m in tournament_repository.get_matches_for_tournament(
                    tournament.id
                )
                if m.phase == 2
            ),
            key=lambda m: m.group_order or 0,
        )
        assert len(lobbies) == 2
        assert (
            tournament_qualification_service.get_ffa_cut_ties(tournament.id)
            == ()
        )

        first = lobbies[0]
        _play(first, admin)

        ties = tournament_qualification_service.get_ffa_cut_ties(tournament.id)
        (tie,) = ties
        scope = tournament_match_service.ffa_lobby_scope(first)
        assert tie.scope == scope
        assert (tie.pool, tie.round_number) == ('SE', first.round)
        assert (tie.rank_from, tie.rank_to) == (3, 4)
        assert tie.decided is False
        assert {c.contestant_id for c in tie.contestants} == set(
            _tied_in(first)
        )

        _play(lobbies[1], admin)
        refused = tournament_seeding_service.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        )
        assert refused.is_err()

        saved = tournament_qualification_service.save_decision(
            tournament.id,
            scope,
            sorted(_tied_in(first)),
            reason='Decided at the table.',
            initiator_id=admin.id,
        )
        assert saved.is_ok(), saved.unwrap_err()
        still_open = tournament_qualification_service.get_ffa_cut_ties(
            tournament.id
        )
        assert [t.decided for t in still_open] == [True, False]
        second_scope = tournament_match_service.ffa_lobby_scope(lobbies[1])
        tournament_qualification_service.save_decision(
            tournament.id,
            second_scope,
            sorted(_tied_in(lobbies[1])),
            reason='Decided at the table.',
            initiator_id=admin.id,
        ).unwrap()
        prepared = tournament_seeding_service.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        )
        assert prepared.is_ok(), prepared.unwrap_err()
    finally:
        db.session.rollback()
        tournament_service.delete_tournament(tournament.id)


# -------------------------------------------------------------------- #
# fetch order independence


def _survivors(tournament):
    fresh = tournament_repository.get_tournament(tournament.id)
    plan = tournament_match_service.plan_ffa_advance(fresh, None).unwrap()
    return list(plan.survivors)


def _fetch_order(monkeypatch, reorder):
    """Return the contestants of every read in the order *reorder* gives."""
    one = tournament_repository.get_contestants_for_match
    many = tournament_repository.get_contestants_for_matches
    monkeypatch.setattr(
        tournament_repository,
        'get_contestants_for_match',
        lambda *a, **k: reorder(list(one(*a, **k))),
    )
    monkeypatch.setattr(
        tournament_repository,
        'get_contestants_for_matches',
        lambda *a, **k: {
            key: reorder(list(value)) for key, value in many(*a, **k).items()
        },
    )


@pytest.mark.parametrize('shift', range(4))
@pytest.mark.parametrize('reverse', [False, True])
def test_seeding_tie_order_does_not_depend_on_the_fetch_order(
    make_ffa, monkeypatch, shift, reverse
):
    tournament = make_ffa(point_table=SEEDING_TIE_TABLE)
    expected = _survivors(tournament)

    def reorder(items):
        rotated = items[shift:] + items[:shift]
        return rotated[::-1] if reverse else rotated

    _fetch_order(monkeypatch, reorder)

    assert _survivors(tournament) == expected


@pytest.mark.parametrize('reverse', [False, True])
def test_seeding_tie_is_ordered_by_recorded_placement(
    make_ffa, monkeypatch, reverse
):
    tournament = make_ffa(point_table=SEEDING_TIE_TABLE)
    lobbies = _round(tournament, 0)
    if reverse:
        _fetch_order(monkeypatch, lambda items: items[::-1])

    survivors = _survivors(tournament)

    # Places 1 and 2 tie on 2 points in each lobby: place 1 first.
    firsts = [_members(m)[:2] for m in lobbies]
    assert survivors[:4] == [*firsts[0], *firsts[1]]
