"""
tests.integration.services.lan_tournament.test_qualification_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import select

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-qualification')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('qualificationbrand', 'Qualification Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Qualification')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'QualificationUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(*, participants=4, qualifiers_per_group=1):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Qualification Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_group_count=2,
            playoff_qualifiers_per_group=qualifiers_per_group,
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:participants]:
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
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _groups(tournament):
    """Return the matches of each group, with their contestant ids."""
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    groups: dict[int, list] = {}
    for match in matches:
        ids = [c.participant_id for c in contestants[match.id]]
        groups.setdefault(match.group_order, []).append((match, ids))
    return groups


def _confirm(match, ids, scores, admin):
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, dict(zip(ids, scores, strict=True))
    )
    assert result.is_ok(), result.unwrap_err()


def _play_all_draws(tournament, admin):
    for matches in _groups(tournament).values():
        for match, ids in matches:
            _confirm(match, ids, (1, 1), admin)


def _log_entries(tournament, event_type):
    return db.session.scalars(
        select(DbTournamentLogEntry).filter_by(
            tournament_id=tournament.id, event_type=event_type
        )
    ).all()


def _group_one_tie(tournament, admin):
    """Play every match as a draw and return the group 0 tie scope/ids."""
    _play_all_draws(tournament, admin)
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert state.blockers, 'all-draw groups must tie'
    block = next(b for b in state.blockers if b.scope == 'group:0')
    return state, block


def test_state_reports_open_matches_before_results(make_tournament):
    tournament = make_tournament()

    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()

    assert state.source == 'groups'
    assert state.total_match_count == 2
    assert state.open_match_count == state.total_match_count
    assert not state.ready
    assert state.qualifiers is None
    assert state.released_at is None
    assert not state.can_unrelease
    assert state.release_mode is PlayoffReleaseMode.MANUAL


def test_state_blocks_on_cut_tie(make_tournament, users):
    tournament = make_tournament()

    state, block = _group_one_tie(tournament, users[0])

    assert state.open_match_count == 0
    assert not state.ready
    assert state.qualifiers is None
    assert {b.scope for b in state.blockers} == {'group:0', 'group:1'}
    assert len(block.contestant_ids) == 2
    assert block.kind.name == 'CUT'
    assert not block.decided


def test_state_ready_once_ties_are_decided(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    for scope in ('group:0', 'group:1'):
        tie = next(
            b
            for b in tournament_qualification_service.get_qualification(
                tournament.id
            )
            .unwrap()
            .blockers
            if b.scope == scope
        )
        result = tournament_qualification_service.save_decision(
            tournament.id,
            scope,
            list(tie.contestant_ids),
            reason='coin toss by the orga',
            initiator_id=users[0].id,
        )
        assert result.is_ok(), result.unwrap_err()

    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()

    assert state.ready
    assert state.blockers == ()
    assert state.qualifiers is not None
    assert len(state.qualifiers) == 2
    decided_first = {
        q.contestant_id for q in state.qualifiers if q.scope == 'group:0'
    }
    assert decided_first == {block.contestant_ids[0]}


def test_save_decision_requires_reason(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='   ',
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'group:0'
        )
        is None
    )


@pytest.mark.parametrize(
    'reason',
    [
        '',
        ' \t\r\n ',
        '\u200e\u2028\u2029',
        'nul\x00byte',
        'tab\there',
        'bidi \u202e override',
        'bidi \u2066 isolate',
        'x' * 501,
    ],
)
def test_save_decision_refuses_unusable_reason_in_the_service(
    make_tournament, users, reason
):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason=reason,
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'group:0'
        )
        is None
    )
    assert not _log_entries(tournament, 'qualification-tie-decided')


def test_save_decision_keeps_line_breaks_in_the_reason(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='first line\r\nsecond line',
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    stored = tournament_qualification_repository.find_decision(
        tournament.id, 'group:0'
    )
    assert stored.reason == 'first line\nsecond line'


def test_save_decision_keeps_zwj_and_trims_the_reason(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    reason = 'family \U0001f468\u200d\U0001f469 drew the lot'

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason=f'  {reason} \n',
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    stored = tournament_qualification_repository.find_decision(
        tournament.id, 'group:0'
    )
    assert stored.reason == reason


def test_save_decision_ids_must_match_tie(make_tournament, users):
    tournament = make_tournament()
    state, block = _group_one_tie(tournament, users[0])
    other = next(b for b in state.blockers if b.scope == 'group:1')
    ids = list(block.contestant_ids)

    def attempt(scope, ordered):
        return tournament_qualification_service.save_decision(
            tournament.id,
            scope,
            ordered,
            reason='orga decision',
            initiator_id=users[0].id,
        )

    assert attempt('group:0', ids[:1]).is_err()
    assert attempt('group:0', ids + [str(generate_uuid7())]).is_err()
    assert attempt('group:0', [ids[0], ids[0]]).is_err()
    assert attempt('group:0', list(other.contestant_ids)).is_err()
    assert attempt('group:7', ids).is_err()
    assert attempt('winner', ids).is_err()
    assert (
        tournament_qualification_repository.get_decisions_for_tournament(
            tournament.id
        )
        == {}
    )

    # IDs arrive as strings from a form; UUID objects are coerced alike.
    assert attempt('group:0', list(reversed(ids))).is_ok()
    stored = tournament_qualification_repository.find_decision(
        tournament.id, 'group:0'
    )
    assert stored.orders == (tuple(str(i) for i in reversed(ids)),)


def test_save_decision_accepts_uuid_ids(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        [UUID(i) for i in block.contestant_ids],
        reason='orga decision',
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()


def test_save_decision_refuses_a_tie_that_is_already_decided(
    make_tournament, users
):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    ids = list(block.contestant_ids)
    assert tournament_qualification_service.save_decision(
        tournament.id, 'group:0', ids, reason='first', initiator_id=users[0].id
    ).is_ok()

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(reversed(ids)),
        reason='second',
        initiator_id=users[0].id,
    )

    assert result.unwrap_err() == (
        'This tie is already decided. Withdraw the decision first.'
    )
    stored = tournament_qualification_repository.find_decision(
        tournament.id, 'group:0'
    )
    assert stored.reason == 'first'


def test_save_decision_is_audited_with_the_reason(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[1])

    assert tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='decided by the judges',
        initiator_id=users[1].id,
    ).is_ok()

    (entry,) = _log_entries(tournament, 'qualification-tie-decided')
    assert entry.initiator_id == users[1].id
    assert entry.data['scope'] == 'group:0'
    assert entry.data['reason'] == 'decided by the judges'
    assert entry.data['contestant_ids'] == list(block.contestant_ids)
    assert entry.data['rank_from'] == block.rank_from
    assert entry.data['rank_to'] == block.rank_to


def test_withdraw_decision_logged(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    assert tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='to be withdrawn',
        initiator_id=users[0].id,
    ).is_ok()

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=list(block.contestant_ids),
        reason='the judges changed their mind',
        initiator_id=users[2].id,
    )

    assert result.is_ok(), result.unwrap_err()
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'group:0'
        )
        is None
    )
    (entry,) = _log_entries(tournament, 'qualification-tie-withdrawn')
    assert entry.initiator_id == users[2].id
    assert entry.data['scope'] == 'group:0'
    assert entry.data['reason'] == 'the judges changed their mind'
    assert entry.data['decision_reason'] == 'to be withdrawn'
    assert entry.data['rank_from'] == block.rank_from
    assert entry.data['rank_to'] == block.rank_to
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert any(b.scope == 'group:0' for b in state.blockers)


def test_withdraw_decision_without_decision_is_refused(make_tournament, users):
    tournament = make_tournament()

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=['a', 'b'],
        reason='nothing to withdraw',
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert not _log_entries(tournament, 'qualification-tie-withdrawn')


@pytest.mark.parametrize('reason', ['', '   ', 'x' * 501, 'bad\x00reason'])
def test_withdraw_decision_requires_a_valid_reason(
    make_tournament, users, reason
):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    assert tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='kept decision',
        initiator_id=users[0].id,
    ).is_ok()

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=list(block.contestant_ids),
        reason=reason,
        initiator_id=users[0].id,
    )

    assert result.is_err()
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'group:0'
        )
        is not None
    )
    assert not _log_entries(tournament, 'qualification-tie-withdrawn')


def test_decisions_are_refused_after_release(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    assert tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='before release',
        initiator_id=users[0].id,
    ).is_ok()
    tournament_repository.set_playoff_release(
        tournament.id, released_at=datetime.now(UTC), released_by=users[0].id
    )
    db.session.commit()

    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=list(block.contestant_ids),
        reason='after release',
        initiator_id=users[0].id,
    )
    saved = tournament_qualification_service.save_decision(
        tournament.id,
        'group:1',
        list(
            next(
                b
                for b in tournament_qualification_service.get_qualification(
                    tournament.id
                )
                .unwrap()
                .blockers
                if b.scope == 'group:1'
            ).contestant_ids
        ),
        reason='after release',
        initiator_id=users[0].id,
    )

    assert withdrawn.is_err()
    assert saved.is_err()
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'group:0'
        )
        is not None
    )


def test_delete_tournament_removes_decisions(make_tournament, users):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])
    assert tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='gone with the tournament',
        initiator_id=users[0].id,
    ).is_ok()

    tournament_service.delete_tournament(tournament.id)

    assert (
        tournament_qualification_repository.get_decisions_for_tournament(
            tournament.id
        )
        == {}
    )


def test_tournament_without_playoffs_has_only_a_winner_qualification(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Qualification Plain {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    tournament, _ = result.unwrap()
    try:
        state = tournament_qualification_service.get_qualification(
            tournament.id
        ).unwrap()
        assert state.source == 'winner'
        assert state.qualifiers is None
        assert not state.ready
        released = tournament_qualification_service.release_playoffs(
            tournament.id, expected_version=0, initiator_id=generate_uuid7()
        )
        assert released.is_err()
    finally:
        tournament_service.delete_tournament(tournament.id)


def test_two_ties_in_one_scope_are_decided_one_after_the_other(
    make_tournament, users
):
    tournament = make_tournament(participants=8, qualifiers_per_group=3)
    groups = _groups(tournament)
    members = sorted({i for _, ids in groups[0] for i in ids}, key=str)
    top, bottom = set(members[:2]), set(members[2:])
    for match, ids in groups[0]:
        a, b = ids
        if {a, b} in (top, bottom):
            scores = (1, 1) if top == {a, b} else (0, 0)
        else:
            scores = (1, 0) if a in top else (0, 1)
        _confirm(match, ids, scores, users[0])
    for match, ids in groups[1]:
        _confirm(match, ids, (1, 1), users[0])

    blockers = (
        tournament_qualification_service.get_qualification(tournament.id)
        .unwrap()
        .blockers
    )
    group_zero = [b for b in blockers if b.scope == 'group:0']
    assert {frozenset(b.contestant_ids) for b in group_zero} == {
        frozenset(str(i) for i in top),
        frozenset(str(i) for i in bottom),
    }

    for block in group_zero:
        result = tournament_qualification_service.save_decision(
            tournament.id,
            'group:0',
            list(block.contestant_ids),
            reason='one tie at a time',
            initiator_id=users[0].id,
        )
        assert result.is_ok(), result.unwrap_err()

    after = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert not [b for b in after.blockers if b.scope == 'group:0']
    stored = tournament_qualification_repository.find_decision(
        tournament.id, 'group:0'
    )
    assert len(stored.blocks) == 2


@pytest.fixture
def make_plain_round_robin(party, users):
    created = []

    def _make(*, participants=3):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Plain Round Robin {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:participants]:
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
        generated = tournament_match_service.generate_round_robin_bracket(
            tournament.id
        )
        assert generated.is_ok(), generated.unwrap_err()
        tournament_repository.set_tournament_status_flush(
            tournament.id, TournamentStatus.ONGOING
        )
        tournament_repository.commit_session()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _all_matches(tournament):
    return [
        match_and_ids
        for matches in _groups(tournament).values()
        for match_and_ids in matches
    ]


def _status(tournament):
    return tournament_repository.get_tournament(tournament.id)


def test_plain_rr_with_a_winner_tie_stays_ongoing_until_decided(
    make_plain_round_robin, users
):
    tournament = make_plain_round_robin()
    _play_all_draws(tournament, users[0])

    current = _status(tournament)
    assert current.tournament_status == TournamentStatus.ONGOING
    assert current.winner_participant_id is None
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert [b.scope for b in state.blockers] == ['winner']
    assert state.open_match_count == 0
    assert not state.ready
    tied = list(state.blockers[0].contestant_ids)
    assert len(tied) == 3

    decided = list(reversed(tied))
    result = tournament_qualification_service.save_decision(
        tournament.id,
        'winner',
        decided,
        reason='coin flip by the orga',
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    current = _status(tournament)
    assert current.tournament_status == TournamentStatus.COMPLETED
    assert str(current.winner_participant_id) == decided[0]
    assert len(_log_entries(tournament, 'qualification-tie-decided')) == 1

    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'winner',
        contestant_ids=decided,
        reason='take it back',
        initiator_id=users[0].id,
    )
    assert withdrawn.is_err()
    assert _status(tournament).tournament_status == TournamentStatus.COMPLETED


def test_plain_rr_unconfirm_reopens_a_decided_tournament(
    make_plain_round_robin, users
):
    tournament = make_plain_round_robin()
    _play_all_draws(tournament, users[0])
    tied = list(
        tournament_qualification_service.get_qualification(tournament.id)
        .unwrap()
        .blockers[0]
        .contestant_ids
    )
    tournament_qualification_service.save_decision(
        tournament.id,
        'winner',
        tied,
        reason='orga decision',
        initiator_id=users[0].id,
    )
    assert _status(tournament).tournament_status == TournamentStatus.COMPLETED

    match, _ = _all_matches(tournament)[0]
    result = tournament_match_service.unconfirm_match(match.id, users[0].id)

    assert result.is_ok(), result.unwrap_err()
    current = _status(tournament)
    assert current.tournament_status == TournamentStatus.ONGOING
    assert current.winner_participant_id is None


def test_plain_rr_with_a_clear_winner_completes_with_a_shared_second_place(
    make_plain_round_robin, users
):
    tournament = make_plain_round_robin()
    winner_id = None
    for match, ids in _all_matches(tournament):
        if winner_id is None:
            winner_id = ids[0]
        if winner_id in ids:
            scores = (2, 0) if ids[0] == winner_id else (0, 2)
        else:
            scores = (1, 1)
        _confirm(match, ids, scores, users[0])

    current = _status(tournament)
    assert current.tournament_status == TournamentStatus.COMPLETED
    assert current.winner_participant_id == winner_id
    podium = tournament_service.resolve_podium_display_names(current)
    others = {u.screen_name for u in users[:3] if u.id != _user_id(winner_id)}
    assert set(podium['runner_up'].split(' / ')) == others
    assert podium['bronze'] is None


def _user_id(participant_id):
    return tournament_repository.find_participant(participant_id).user_id


# -------------------------------------------------------------------- #
# one block per tie


def _play_named(matches, plan, admin):
    """Play a group; `plan` maps a pair of names to `{name: score}`."""
    members = sorted({i for _, ids in matches for i in ids}, key=str)
    by_name = dict(zip('ABCD', members, strict=True))
    for match, ids in matches:
        pair = frozenset(n for n, i in by_name.items() if i in ids)
        _confirm(
            match,
            ids,
            [
                plan[pair][next(n for n, i in by_name.items() if i == x)]
                for x in ids
            ],
            admin,
        )
    return by_name


def _match_between(matches, a, b):
    return next(m for m, ids in matches if set(ids) == {a, b})


@pytest.fixture
def two_decided_ties(make_tournament, users):
    """Group 0 holds the ties {A,C} (seeding) and {B,D} (cut), both decided.

    The orga then corrects A-B, so that A and D tie across the cut.
    """
    admin = users[0]
    tournament = make_tournament(participants=8, qualifiers_per_group=3)
    started = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )
    assert started.is_ok(), started.unwrap_err()
    groups = _groups(tournament)
    for matches in groups.values():
        if matches is groups[0]:
            continue
        for match, ids in matches:
            _confirm(match, ids, (1, 0), admin)
    plan = {
        frozenset('AB'): {'A': 1, 'B': 0},
        frozenset('AC'): {'A': 1, 'C': 1},
        frozenset('AD'): {'A': 1, 'D': 1},
        frozenset('BC'): {'B': 1, 'C': 1},
        frozenset('BD'): {'B': 1, 'D': 1},
        frozenset('CD'): {'C': 1, 'D': 0},
    }
    names = _play_named(groups[0], plan, admin)
    ids = {n: str(i) for n, i in names.items()}
    for order, by, reason in (
        ('AC', users[1], 'first reason'),
        ('BD', users[2], 'second reason'),
    ):
        result = tournament_qualification_service.save_decision(
            tournament.id,
            'group:0',
            [ids[n] for n in order],
            reason=reason,
            initiator_id=by.id,
        )
        assert result.is_ok(), result.unwrap_err()

    def correct():
        corrected = tournament_match_service.correct_match_result(
            _match_between(groups[0], names['A'], names['B']).id,
            admin.id,
            reason='wrong result reported',
            corrected_scores={names['A']: 0, names['B']: 1},
        )
        assert corrected.is_ok(), corrected.unwrap_err()

    return tournament, ids, correct


def _ranking(tournament, scope='group:0'):
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    return state, next(r for r in state.rankings if r.scope == scope)


def _stored(tournament, scope='group:0'):
    return tournament_qualification_repository.find_decision(
        tournament.id, scope
    )


def test_two_ties_of_a_scope_are_stored_as_two_blocks(two_decided_ties, users):
    tournament, ids, _ = two_decided_ties

    stored = _stored(tournament)

    assert stored.orders == ((ids['A'], ids['C']), (ids['B'], ids['D']))
    assert [b.reason for b in stored.blocks] == [
        'first reason',
        'second reason',
    ]
    assert [b.decided_by for b in stored.blocks] == [users[1].id, users[2].id]
    assert (stored.reason, stored.decided_by) == (
        'second reason',
        users[2].id,
    )
    _, ranking = _ranking(tournament)
    assert ranking.outdated == ()


def test_merged_decisions_never_decide_a_new_tie(two_decided_ties):
    tournament, ids, correct = two_decided_ties
    stored_orders = _stored(tournament).orders

    correct()

    state, ranking = _ranking(tournament)
    tie_ad = next(
        t for t in ranking.ties if set(t.contestant_ids) == {ids['A'], ids['D']}
    )
    assert tie_ad.kind.name == 'CUT'
    assert not tie_ad.decided
    assert state.ready is False
    assert tie_ad in state.blockers
    assert set(ranking.outdated) == set(stored_orders)
    assert len(ranking.outdated) == 2


def test_saving_a_new_tie_supersedes_overlapping_outdated_blocks(
    two_decided_ties, users
):
    tournament, ids, correct = two_decided_ties
    old_orders = _stored(tournament).orders
    correct()

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        [ids['A'], ids['D']],
        reason='new tie decided',
        initiator_id=users[3].id,
    )

    assert result.is_ok(), result.unwrap_err()
    stored = _stored(tournament)
    assert stored.orders == ((ids['A'], ids['D']),)
    assert stored.reason == 'new tie decided'
    (saved,) = [
        e
        for e in _log_entries(tournament, 'qualification-tie-decided')
        if 'superseded' in e.data
    ]
    assert saved.data['contestant_ids'] == [ids['A'], ids['D']]
    assert [tuple(o) for o in saved.data['superseded']] == list(old_orders)
    _, ranking = _ranking(tournament)
    assert ranking.outdated == ()


def test_a_save_that_supersedes_nothing_logs_no_superseded_key(
    make_tournament, users
):
    tournament = make_tournament()
    _, block = _group_one_tie(tournament, users[0])

    assert tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='plain save',
        initiator_id=users[0].id,
    ).is_ok()

    (entry,) = _log_entries(tournament, 'qualification-tie-decided')
    assert 'superseded' not in entry.data


def test_withdraw_removes_only_the_named_block(two_decided_ties, users):
    tournament, ids, _ = two_decided_ties

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=[ids['B'], ids['D']],
        reason='second tie reopened',
        initiator_id=users[3].id,
    )

    assert result.is_ok(), result.unwrap_err()
    stored = _stored(tournament)
    assert stored.orders == ((ids['A'], ids['C']),)
    assert (stored.reason, stored.decided_by) == (
        'first reason',
        users[1].id,
    )
    state, ranking = _ranking(tournament)
    assert [t.decided for t in ranking.ties] == [True, False]
    assert not state.ready
    (entry,) = _log_entries(tournament, 'qualification-tie-withdrawn')
    assert entry.data['contestant_ids'] == [ids['B'], ids['D']]
    assert entry.data['decision_reason'] == 'second reason'
    assert 'outdated' not in entry.data
    assert 'rank_from' in entry.data


def test_withdrawing_the_last_block_deletes_the_row(two_decided_ties, users):
    tournament, ids, _ = two_decided_ties
    for order in ('AC', 'BD'):
        result = tournament_qualification_service.withdraw_decision(
            tournament.id,
            'group:0',
            contestant_ids=[ids[n] for n in order],
            reason='all reopened',
            initiator_id=users[3].id,
        )
        assert result.is_ok(), result.unwrap_err()

    assert _stored(tournament) is None


def test_withdraw_of_an_outdated_block_is_audited_as_outdated(
    two_decided_ties, users
):
    tournament, ids, correct = two_decided_ties
    correct()

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=[ids['A'], ids['C']],
        reason='no longer matches',
        initiator_id=users[3].id,
    )

    assert result.is_ok(), result.unwrap_err()
    assert _stored(tournament).orders == ((ids['B'], ids['D']),)
    (entry,) = _log_entries(tournament, 'qualification-tie-withdrawn')
    assert entry.data['outdated'] is True
    assert 'rank_from' not in entry.data
    assert entry.data['contestant_ids'] == [ids['A'], ids['C']]


@pytest.mark.parametrize('named', [[], ['x', 'y'], 'one-member'])
def test_withdraw_without_contestant_ids_is_refused(
    two_decided_ties, users, named
):
    tournament, ids, _ = two_decided_ties
    if named == 'one-member':
        named = [ids['A']]
    before = _stored(tournament)

    result = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'group:0',
        contestant_ids=named,
        reason='names no block',
        initiator_id=users[3].id,
    )

    assert result.is_err()
    assert result.unwrap_err() == 'There is no decision to withdraw.'
    assert _stored(tournament) == before
    assert not _log_entries(tournament, 'qualification-tie-withdrawn')


def _play_groups_by_margin(tournament, admin, margin):
    """Play every group match, the first-listed contestant wins by `margin`."""
    for group, matches in _groups(tournament).items():
        for match, ids in matches:
            _confirm(match, ids, (margin(group), 0), admin)


def _crossover_ties(tournament):
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    return state, [b for b in state.blockers if b.scope == 'crossover']


def _decide_crossover(tournament, ties, admin, *, reverse=False):
    for tie in ties:
        ordered = list(tie.contestant_ids)
        result = tournament_qualification_service.save_decision(
            tournament.id,
            'crossover',
            ordered[::-1] if reverse else ordered,
            reason='seeding by the orga',
            initiator_id=admin.id,
        )
        assert result.is_ok(), result.unwrap_err()


def test_equal_group_winners_block_until_the_orga_orders_them(
    make_tournament, users
):
    tournament = make_tournament(qualifiers_per_group=2)
    _play_groups_by_margin(tournament, users[0], lambda group: 1)

    state, ties = _crossover_ties(tournament)

    assert state.open_match_count == 0
    assert not state.ready
    assert state.qualifiers is not None
    assert len(state.qualifiers) == 4
    assert state.seed_order is None
    assert state.crossover is not None
    assert [(t.rank_from, t.rank_to, t.kind.name) for t in ties] == [
        (1, 2, 'SEEDING'),
        (3, 4, 'SEEDING'),
    ]
    assert {b.scope for b in state.blockers} == {'crossover'}

    _decide_crossover(tournament, ties[:1], users[0], reverse=True)
    state, ties_left = _crossover_ties(tournament)
    assert not state.ready
    assert len(ties_left) == 1
    assert ties_left[0].rank_from == 3

    _decide_crossover(tournament, ties_left, users[0], reverse=True)
    state, _ = _crossover_ties(tournament)

    assert state.ready
    assert state.blockers == ()
    expected = [
        *reversed(ties[0].contestant_ids),
        *reversed(ties[1].contestant_ids),
    ]
    assert [q.contestant_id for q in state.seed_order] == expected
    (first, _second) = _log_entries(tournament, 'qualification-tie-decided')
    assert first.data['scope'] == 'crossover'


def test_distinct_group_margins_seed_without_a_decision(make_tournament, users):
    tournament = make_tournament(qualifiers_per_group=2)
    _play_groups_by_margin(tournament, users[0], lambda group: group + 1)

    state, ties = _crossover_ties(tournament)

    assert state.ready
    assert ties == []
    assert state.blockers == ()
    scopes = [q.scope for q in state.seed_order]
    # the bigger margin leads the winners, the smaller one the runners-up
    assert scopes == ['group:1', 'group:0', 'group:0', 'group:1']
    assert [q.rank for q in state.seed_order] == [1, 1, 2, 2]


def test_exactly_two_qualifiers_need_no_crossover_decision(
    make_tournament, users
):
    tournament = make_tournament(qualifiers_per_group=1)
    _play_groups_by_margin(tournament, users[0], lambda group: 1)

    state, ties = _crossover_ties(tournament)

    assert state.ready
    assert ties == []
    assert state.crossover is None
    assert len(state.seed_order) == 2
    refused = tournament_qualification_service.save_decision(
        tournament.id,
        'crossover',
        [q.contestant_id for q in state.seed_order],
        reason='nothing to decide',
        initiator_id=users[0].id,
    )
    assert refused.is_err()


def test_crossover_decision_is_locked_after_the_release(make_tournament, users):
    tournament = make_tournament(qualifiers_per_group=2)
    _play_groups_by_margin(tournament, users[0], lambda group: 1)
    _, ties = _crossover_ties(tournament)
    _decide_crossover(tournament, ties, users[0])
    tournament_repository.set_playoff_release(
        tournament.id, released_at=datetime.now(UTC), released_by=users[0].id
    )
    db.session.commit()
    stored = tournament_qualification_repository.find_decision(
        tournament.id, 'crossover'
    )

    saved = tournament_qualification_service.save_decision(
        tournament.id,
        'crossover',
        list(reversed(ties[0].contestant_ids)),
        reason='after release',
        initiator_id=users[0].id,
    )
    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        'crossover',
        contestant_ids=list(ties[0].contestant_ids),
        reason='after release',
        initiator_id=users[0].id,
    )

    assert saved.is_err()
    assert withdrawn.is_err()
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'crossover'
        )
        == stored
    )


def _force_status(tournament, status):
    tournament_repository.set_tournament_status_flush(tournament.id, status)
    tournament_repository.commit_session()


def test_save_decision_on_a_cancelled_plain_round_robin_is_refused(
    make_plain_round_robin, users
):
    tournament = make_plain_round_robin()
    _play_all_draws(tournament, users[0])
    tied = list(
        tournament_qualification_service.get_qualification(tournament.id)
        .unwrap()
        .blockers[0]
        .contestant_ids
    )
    cancelled = tournament_service.change_status(
        tournament.id, TournamentStatus.CANCELLED, users[0].id
    )
    assert cancelled.is_ok(), cancelled.unwrap_err()

    result = tournament_qualification_service.save_decision(
        tournament.id,
        'winner',
        tied,
        reason='late decision',
        initiator_id=users[0].id,
    )

    assert result == Err(
        tournament_qualification_service.ERR_DECISION_CANCELLED
    )
    current = _status(tournament)
    assert current.tournament_status is TournamentStatus.CANCELLED
    assert current.winner_participant_id is None
    assert (
        tournament_qualification_repository.find_decision(
            tournament.id, 'winner'
        )
        is None
    )
    assert not _log_entries(tournament, 'qualification-tie-decided')


@pytest.mark.parametrize('scope', ['groups', 'ffa', 'winner'])
def test_decisions_are_refused_once_completed(
    scope, make_tournament, make_plain_round_robin, users
):
    admin = users[0]
    if scope == 'winner':
        tournament = make_plain_round_robin()
        _play_all_draws(tournament, admin)
        tied = list(
            tournament_qualification_service.get_qualification(tournament.id)
            .unwrap()
            .blockers[0]
            .contestant_ids
        )
        decided = list(reversed(tied))
        assert tournament_qualification_service.save_decision(
            tournament.id,
            'winner',
            decided,
            reason='coin flip by the orga',
            initiator_id=admin.id,
        ).is_ok()
        assert _status(tournament).tournament_status is (
            TournamentStatus.COMPLETED
        )
        scope_id, ids = 'winner', decided
    elif scope == 'groups':
        tournament = make_tournament()
        _, block = _group_one_tie(tournament, admin)
        ids = list(block.contestant_ids)
        assert tournament_qualification_service.save_decision(
            tournament.id,
            'group:0',
            ids,
            reason='decided while running',
            initiator_id=admin.id,
        ).is_ok()
        other = next(
            b
            for b in tournament_qualification_service.get_qualification(
                tournament.id
            )
            .unwrap()
            .blockers
            if b.scope == 'group:1'
        )
        _force_status(tournament, TournamentStatus.ONGOING)
        _force_status(tournament, TournamentStatus.COMPLETED)
        refused = tournament_qualification_service.save_decision(
            tournament.id,
            'group:1',
            list(other.contestant_ids),
            reason='too late',
            initiator_id=admin.id,
        )
        assert refused == Err(
            tournament_qualification_service.ERR_DECISION_COMPLETED
        )
        scope_id = 'group:0'
    else:
        tournament = make_tournament()
        ids = ['a', 'b']
        scope_id = 'ffa:SE:0:1'
        _force_status(tournament, TournamentStatus.ONGOING)
        _force_status(tournament, TournamentStatus.COMPLETED)
        saved = tournament_qualification_service.save_decision(
            tournament.id,
            scope_id,
            ids,
            reason='too late',
            initiator_id=admin.id,
        )
        assert saved == Err(
            tournament_qualification_service.ERR_DECISION_COMPLETED
        )

    withdrawn = tournament_qualification_service.withdraw_decision(
        tournament.id,
        scope_id,
        contestant_ids=ids,
        reason='take it back',
        initiator_id=admin.id,
    )

    assert withdrawn == Err(
        tournament_qualification_service.ERR_DECISION_COMPLETED
    )
    assert not _log_entries(tournament, 'qualification-tie-withdrawn')
    if scope != 'ffa':
        assert (
            tournament_qualification_repository.find_decision(
                tournament.id, scope_id
            )
            is not None
        )
