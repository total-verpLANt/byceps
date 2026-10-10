"""
tests.integration.services.lan_tournament.test_one_group_playoffs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Round robins with a single group feeding the playoffs, driven through
the services only.
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_domain_service as qualification_domain,
    tournament_qualification_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
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
from byceps.util.uuid import generate_uuid7


_SUFFIX = uuid4().hex[:8]

PARTY_ID = PartyID(f'lan-party-one-group-{_SUFFIX}')

SINGLE = EliminationMode.SINGLE_ELIMINATION
DOUBLE = EliminationMode.DOUBLE_ELIMINATION
MANUAL = PlayoffReleaseMode.MANUAL
AUTOMATIC = PlayoffReleaseMode.AUTOMATIC

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand(f'onegroup{_SUFFIX}', 'One Group Playoffs Brand')
    return make_party(brand, PARTY_ID, 'LAN Party One Group Playoffs')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'OneGroupUser{i}{_SUFFIX}') for i in range(6)]


def _create(**args):
    return tournament_service.create_tournament(
        PARTY_ID,
        f'One Group Playoffs {next(_counter)} {_SUFFIX}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_group_count=1,
        **args,
    )


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(players, *, qualifiers, playoff_mode=SINGLE, release_mode=MANUAL):
        result = _create(
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            min_players=players,
            playoff_elimination_mode=playoff_mode,
            playoff_qualifiers_per_group=qualifiers,
            playoff_release_mode=release_mode,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:players]:
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _participant_ids(tournament, users):
    """Return the participant IDs as strings, in the order of `users`."""
    by_user = {
        p.user_id: str(p.id)
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id
        )
    }
    return [by_user[u.id] for u in users]


def _matches(tournament, phase):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == phase
    ]


def _contestants(match):
    return [
        str(c.participant_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    ]


def _log_types(tournament):
    return list(
        db.session.scalars(
            select(DbTournamentLogEntry.event_type)
            .filter_by(tournament_id=tournament.id)
            .order_by(DbTournamentLogEntry.occurred_at)
        )
    )


def _qualification(tournament):
    result = tournament_qualification_service.get_qualification(tournament.id)
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def _stronger_wins(ids):
    """Return a result table in which the earlier ID always wins 2-0."""
    rank = {cid: n for n, cid in enumerate(ids)}

    def scores_of(a, b):
        return (2, 0) if rank[a] < rank[b] else (0, 2)

    return scores_of


def _start(tournament, admin):
    """Make the group from its draft, start the tournament; count matches."""
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert generated.is_ok(), generated.unwrap_err()
    started = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )
    assert started.is_ok(), started.unwrap_err()
    return generated.unwrap()


def _confirm(match, admin, scores_of):
    a, b = _contestants(match)
    score_a, score_b = scores_of(a, b)
    confirmed = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(a): score_a, UUID(b): score_b}
    )
    assert confirmed.is_ok(), confirmed.unwrap_err()


def _confirm_group(tournament, admin, scores_of):
    for match in _matches(tournament, 1):
        _confirm(match, admin, scores_of)


def _play_phase_two(tournament, admin, scores_of):
    """Play each phase 2 match as soon as both sides are known."""
    for _ in range(8):
        ready = [
            m
            for m in _matches(tournament, 2)
            if m.confirmed_by is None and len(_contestants(m)) == 2
        ]
        if not ready:
            return
        for match in ready:
            _confirm(match, admin, scores_of)


def _release(tournament, admin):
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=draft.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()
    return released.unwrap()


def _group_ranking(tournament, ids):
    """Rank the group from its stored results, apart from the service."""
    results = []
    for match in _matches(tournament, 1):
        a, b = tournament_repository.get_contestants_for_match(match.id)
        results.append(
            qualification_domain.MatchResult(
                a=str(a.participant_id),
                b=str(b.participant_id),
                score_a=a.score,
                score_b=b.score,
                confirmed=match.confirmed_by is not None,
            )
        )
    return qualification_domain.rank_round_robin('group:0', ids, results)


def _assert_completed(tournament, winner):
    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) == winner


def test_one_group_playoff_config_persists(party):
    result = _create(
        min_players=4,
        playoff_elimination_mode=SINGLE,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=MANUAL,
    )

    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    try:
        found = tournament_repository.get_tournament(tournament.id)
        assert found.has_playoffs
        assert found.playoff_group_count == 1
        assert found.playoff_qualifiers_per_group == 2
    finally:
        tournament_service.delete_tournament(tournament.id)


def test_one_group_four_players_two_qualifiers_to_single_elimination(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(4, qualifiers=2)
    ids = _participant_ids(tournament, users[:4])

    assert _start(tournament, admin) == 6
    assert {m.group_order for m in _matches(tournament, 1)} == {0}
    _confirm_group(tournament, admin, _stronger_wins(ids))

    state = _qualification(tournament)
    assert state.ready
    assert not state.de_fallback
    ranking = _group_ranking(tournament, ids)
    top_two = {e.contestant_id for e in ranking.entries if e.rank <= 2}
    assert top_two == {ids[0], ids[1]}
    assert {q.contestant_id for q in state.qualifiers} == top_two
    assert {q.scope for q in state.qualifiers} == {'group:0'}
    assert not _matches(tournament, 2)

    assert _release(tournament, admin) == 1
    (final,) = _matches(tournament, 2)
    assert set(_contestants(final)) == top_two
    assert final.next_match_id is None
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )

    _play_phase_two(tournament, admin, _stronger_wins(ids))

    _assert_completed(tournament, ids[0])
    assert _log_types(tournament).count('playoffs-released') == 1


def test_one_group_tie_at_the_cut_needs_a_decision(make_tournament, users):
    admin = users[0]
    tournament = make_tournament(4, qualifiers=2)
    best, *tied = _participant_ids(tournament, users[:4])
    _start(tournament, admin)

    # The best beats everybody; the rest beat each other in a circle, so
    # that points, head-to-head, difference and scored goals all tie.
    circle = {
        frozenset((tied[0], tied[1])): tied[0],
        frozenset((tied[1], tied[2])): tied[1],
        frozenset((tied[2], tied[0])): tied[2],
    }

    def scores_of(a, b):
        pair = frozenset((a, b))
        winner = best if best in pair else circle[pair]
        return (1, 0) if winner == a else (0, 1)

    _confirm_group(tournament, admin, scores_of)

    state = _qualification(tournament)
    assert state.open_match_count == 0
    assert not state.ready
    (block,) = state.blockers
    assert block.scope == 'group:0'
    assert set(block.contestant_ids) == set(tied)
    assert (block.rank_from, block.rank_to) == (2, 4)
    assert block.kind is qualification_domain.TieKind.CUT

    blocked = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=0, initiator_id=admin.id
    )
    assert (
        blocked.unwrap_err() == tournament_seeding_service.ERR_PLAYOFF_NOT_READY
    )
    assert not _matches(tournament, 2)

    decided = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        [tied[1], tied[2], tied[0]],
        reason='Sudden death in the lobby',
        initiator_id=admin.id,
    )
    assert decided.is_ok(), decided.unwrap_err()
    state = _qualification(tournament)
    assert state.ready
    assert {q.contestant_id for q in state.qualifiers} == {best, tied[1]}

    assert _release(tournament, admin) == 1
    (final,) = _matches(tournament, 2)
    assert set(_contestants(final)) == {best, tied[1]}
    types = _log_types(tournament)
    assert types.index('qualification-tie-decided') < types.index(
        'playoffs-released'
    )


def test_one_group_six_players_four_qualifiers_to_double_elimination(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(6, qualifiers=4, playoff_mode=DOUBLE)
    ids = _participant_ids(tournament, users[:6])

    assert _start(tournament, admin) == 15
    _confirm_group(tournament, admin, _stronger_wins(ids))

    state = _qualification(tournament)
    assert state.ready
    assert not state.de_fallback
    assert {q.contestant_id for q in state.qualifiers} == set(ids[:4])

    released = _release(tournament, admin)
    phase_two = _matches(tournament, 2)
    assert released == len(phase_two) > 0
    assert {m.bracket.value for m in phase_two} == {'WB', 'LB', 'GF'}
    assert {cid for m in phase_two for cid in _contestants(m)} == set(ids[:4])
    assert 'playoffs-de-fallback' not in _log_types(tournament)

    _play_phase_two(tournament, admin, _stronger_wins(ids))

    assert all(m.confirmed_by is not None for m in _matches(tournament, 2))
    _assert_completed(tournament, ids[0])
    assert {m.bracket for m in _matches(tournament, 2)} >= {
        Bracket.WINNERS,
        Bracket.LOSERS,
        Bracket.GRAND_FINAL,
    }


def test_one_group_double_elimination_with_two_qualifiers_falls_back(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(4, qualifiers=2, playoff_mode=DOUBLE)
    ids = _participant_ids(tournament, users[:4])
    _start(tournament, admin)
    _confirm_group(tournament, admin, _stronger_wins(ids))

    state = _qualification(tournament)
    assert state.ready
    assert state.de_fallback is True
    assert not _matches(tournament, 2)

    assert _release(tournament, admin) == 1
    (final,) = _matches(tournament, 2)
    assert set(_contestants(final)) == {ids[0], ids[1]}
    assert final.next_match_id is None
    assert final.bracket not in (Bracket.LOSERS, Bracket.GRAND_FINAL)
    assert 'playoffs-de-fallback' in _log_types(tournament)
    assert (
        tournament_repository.get_tournament(
            tournament.id
        ).playoff_elimination_mode
        is SINGLE
    )

    _play_phase_two(tournament, admin, _stronger_wins(ids))

    _assert_completed(tournament, ids[0])


def test_one_group_double_elimination_falls_back_with_automatic_release(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(
        4, qualifiers=2, playoff_mode=DOUBLE, release_mode=AUTOMATIC
    )
    ids = _participant_ids(tournament, users[:4])
    scores_of = _stronger_wins(ids)
    _start(tournament, admin)

    # Nothing is released before the last group result is in.
    *early, last = _matches(tournament, 1)
    for match in early:
        _confirm(match, admin, scores_of)
    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is None
    assert not _matches(tournament, 2)

    _confirm(last, admin, scores_of)

    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is not None
    assert found.playoff_released_by is None
    (final,) = _matches(tournament, 2)
    assert set(_contestants(final)) == {ids[0], ids[1]}
    assert final.bracket not in (Bracket.LOSERS, Bracket.GRAND_FINAL)
    assert found.playoff_elimination_mode is SINGLE
    types = _log_types(tournament)
    assert 'playoffs-de-fallback' in types
    assert types.count('playoffs-released') == 1

    _play_phase_two(tournament, admin, scores_of)

    _assert_completed(tournament, ids[0])


def test_one_group_seeding_board_shows_no_same_group_warning(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(6, qualifiers=4)
    ids = _participant_ids(tournament, users[:6])
    _start(tournament, admin)
    _confirm_group(tournament, admin, _stronger_wins(ids))
    assert _qualification(tournament).ready

    drawn = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    board = tournament_seeding_service.get_board(
        tournament.id,
        tournament_seeding_service.PLAYOFF_TARGET,
        initiator_id=admin.id,
    ).unwrap()

    assert drawn.origin_labels
    assert board.origin_labels
    assert drawn.same_group_matches == ()
    assert board.same_group_matches == ()
