"""
tests.integration.services.lan_tournament.test_playoff_flows
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two complete tournaments, driven through the services only: RR groups
to single elimination with a manual release, and highscore to FFA with
an automatic release.
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import select

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
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
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-playoff-flows')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('playoffflowsbrand', 'Playoff Flows Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Playoff Flows')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PlayoffFlowsUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(**args):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Playoff Flows Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            **args,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users:
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


def _log_types(tournament):
    return list(
        db.session.scalars(
            select(DbTournamentLogEntry.event_type)
            .filter_by(tournament_id=tournament.id)
            .order_by(DbTournamentLogEntry.occurred_at)
        )
    )


def _status(tournament):
    return tournament_repository.get_tournament(tournament.id).tournament_status


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


def _qualification(tournament):
    result = tournament_qualification_service.get_qualification(tournament.id)
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def test_rr_groups_to_se_manual_end_to_end(make_tournament, users):
    admin = users[0]
    tournament = make_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    user_of = dict(zip(_participant_ids(tournament, users), users, strict=True))

    # Seeding: the groups are made from the draft.
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    generated = tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert generated.unwrap() == 12
    phase_one = _matches(tournament, 1)
    assert len(phase_one) == 12
    assert {m.group_order for m in phase_one} == {0, 1}
    assert not _matches(tournament, 2)
    started = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )
    assert started.is_ok(), started.unwrap_err()

    # Results: the stronger (lower) ID wins every match, except that in
    # group 0 the second and third strongest draw. That tie spans the
    # cut of two, so the orga has to decide it.
    groups: dict[int, list] = {}
    for match in phase_one:
        groups.setdefault(match.group_order, []).append(match)
    strength = {}
    for group, matches in groups.items():
        members = sorted({cid for m in matches for cid in _contestants(m)})
        assert len(members) == 4
        strength[group] = members
    drawn_pair = {strength[0][1], strength[0][2]}
    for group, matches in sorted(groups.items()):
        for match in matches:
            stronger, weaker = sorted(_contestants(match))
            draw = group == 0 and {stronger, weaker} == drawn_pair
            margin = 2 + group
            scores = {
                stronger: 1 if draw else margin,
                weaker: 1 if draw else 0,
            }
            if draw or match is matches[0]:
                # The participant path: the weaker side, or either on a draw.
                result = tournament_match_service.set_match_scores(
                    match.id,
                    user_of[weaker].id,
                    {UUID(cid): score for cid, score in scores.items()},
                )
            else:
                result = tournament_match_service.admin_set_and_confirm_match(
                    match.id,
                    admin.id,
                    {UUID(cid): score for cid, score in scores.items()},
                )
            assert result.is_ok(), result.unwrap_err()

    assert _status(tournament) is TournamentStatus.ONGOING
    state = _qualification(tournament)
    assert state.open_match_count == 0
    assert not state.ready
    (block,) = state.blockers
    assert block.scope == 'group:0'
    assert set(block.contestant_ids) == drawn_pair
    assert not _matches(tournament, 2)

    # The tie is decided by the orga, the release stays manual.
    decided = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        [strength[0][2], strength[0][1]],
        reason='Sudden death in the lobby',
        initiator_id=admin.id,
    )
    assert decided.is_ok(), decided.unwrap_err()
    state = _qualification(tournament)
    assert state.ready
    qualified = {q.contestant_id for q in state.qualifiers}
    assert qualified == {
        strength[0][0],
        strength[0][2],
        strength[1][0],
        strength[1][1],
    }
    assert state.released_at is None
    assert not _matches(tournament, 2)

    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=draft.version, initiator_id=admin.id
    )
    assert released.unwrap() == len(_matches(tournament, 2)) > 0
    assert _status(tournament) is TournamentStatus.ONGOING
    assert {
        cid for m in _matches(tournament, 2) for cid in _contestants(m)
    } == (qualified)
    assert len(_matches(tournament, 1)) == 12

    # Phase 1 and its tie decision are locked from now on.
    locked = tournament_match_service.unconfirm_match(
        phase_one[0].id, admin.id, reason='too late'
    )
    assert locked.unwrap_err() == tournament_match_service.PHASE1_LOCKED_ERROR
    redecided = tournament_qualification_service.save_decision(
        tournament.id,
        'group:0',
        list(block.contestant_ids),
        reason='changed my mind',
        initiator_id=admin.id,
    )
    assert redecided.unwrap_err() == (
        'Tie decisions are locked after the playoffs are released.'
    )

    # Phase 2: play every match that has two contestants until done. The
    # weaker side wins, so no phase 1 table can predict the podium.
    winners = {}
    losers = {}
    for _ in range(4):
        ready = [
            m
            for m in _matches(tournament, 2)
            if m.confirmed_by is None and len(_contestants(m)) == 2
        ]
        if not ready:
            break
        for match in ready:
            stronger, weaker = sorted(_contestants(match))
            confirmed = tournament_match_service.admin_set_and_confirm_match(
                match.id,
                admin.id,
                {UUID(weaker): 3, UUID(stronger): 1},
            )
            assert confirmed.is_ok(), confirmed.unwrap_err()
            winners[match.id] = weaker
            losers[match.id] = stronger

    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    final = [
        m
        for m in _matches(tournament, 2)
        if m.next_match_id is None and m.bracket is not Bracket.THIRD_PLACE
    ]
    assert len(final) == 1
    assert all(m.confirmed_by is not None for m in _matches(tournament, 2))
    assert str(found.winner_participant_id) == winners[final[0].id]
    assert str(found.winner_participant_id) in qualified
    assert not _qualification(tournament).can_unrelease

    # The podium comes from phase 2 alone: the final loser is the
    # runner-up and the third-place winner is the bronze, not a table
    # over the group matches.
    (third_place,) = [
        m
        for m in _matches(tournament, 2)
        if m.bracket is Bracket.THIRD_PLACE
    ]
    podium = tournament_service.resolve_podium_display_names(found)
    assert podium == {
        'runner_up': user_of[losers[final[0].id]].screen_name,
        'bronze': user_of[winners[third_place.id]].screen_name,
    }
    assert podium['runner_up'] != user_of[winners[final[0].id]].screen_name

    types = _log_types(tournament)
    order = [
        types.index(t)
        for t in (
            'bracket-generated',
            'qualification-tie-decided',
            'playoffs-released',
        )
    ]
    assert order == sorted(order)
    assert types.count('playoffs-released') == 1


@pytest.mark.parametrize(
    'playoff_mode',
    [EliminationMode.SINGLE_ELIMINATION, EliminationMode.DOUBLE_ELIMINATION],
    ids=['se', 'de'],
)
def test_highscore_to_ffa_auto_end_to_end(make_tournament, users, playoff_mode):
    admin = users[0]
    tournament = make_tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=playoff_mode,
        playoff_qualifier_count=8,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
        point_table=[5, 3, 2, 1],
        group_size_min=3,
        # The DE fixture's asserted six-player GF must fit its maximum.
        group_size_max=(
            6 if playoff_mode is EliminationMode.DOUBLE_ELIMINATION else 4
        ),
        advancement_count=2,
    )
    ids = _participant_ids(tournament, users)
    rank = {cid: n for n, cid in enumerate(ids)}

    # Phase 1: the leaderboard. Nothing is released before the close.
    for participant_id, value in zip(
        ids, [80, 70, 60, 50, 40, 30, 20, 10], strict=True
    ):
        submitted = tournament_score_service.submit_score(
            tournament.id,
            value,
            participant_id=TournamentParticipantID(participant_id),
        )
        assert submitted.is_ok(), submitted.unwrap_err()
    assert not _qualification(tournament).ready
    assert not _matches(tournament, 2)

    closed = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    )
    assert closed.is_ok(), closed.unwrap_err()

    # The close released the playoffs by itself: two lobbies of four.
    found = tournament_repository.get_tournament(tournament.id)
    assert found.playoff_released_at is not None
    assert found.playoff_released_by is None
    lobbies = _matches(tournament, 2)
    assert len(lobbies) == 2
    assert all(
        m.phase == 2
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
    )
    assert {cid for m in lobbies for cid in _contestants(m)} == set(ids)
    expected_bracket = (
        Bracket.WINNERS
        if playoff_mode is EliminationMode.DOUBLE_ELIMINATION
        else None
    )
    assert {m.bracket for m in lobbies} == {expected_bracket}

    def play(match, reverse=False):
        order = sorted(
            _contestants(match), key=rank.__getitem__, reverse=reverse
        )
        placed = tournament_match_service.set_ffa_placements(
            match.id, {cid: place for place, cid in enumerate(order, start=1)}
        )
        assert placed.is_ok(), placed.unwrap_err()
        confirmed = tournament_match_service.confirm_ffa_match(
            match.id, admin.id
        )
        assert confirmed.is_ok(), confirmed.unwrap_err()

    for lobby in lobbies:
        play(lobby)
    assert _status(tournament) is TournamentStatus.ONGOING

    if playoff_mode is EliminationMode.SINGLE_ELIMINATION:
        # The next round is made from its draft.
        target = tournament_seeding_service.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        board = tournament_seeding_service.get_board(
            tournament.id, target, initiator_id=admin.id
        ).unwrap()
        made = tournament_seeding_service.generate_from_seeding(
            tournament.id,
            target,
            expected_version=board.version,
            initiator_id=admin.id,
        )
        assert made.unwrap() == 1
        (final,) = [m for m in _matches(tournament, 2) if m.round == 1]
        assert final.phase == 2
        assert len(_contestants(final)) == 4
        assert _status(tournament) is TournamentStatus.ONGOING
        play(final, reverse=True)
        decisive = final
    else:
        # Winners round 1 and losers round 0 come from one advance.
        advanced = tournament_match_service.advance_ffa_round(
            tournament.id, pool=Bracket.WINNERS, initiator_id=admin.id
        )
        assert advanced.unwrap() == 'advanced_wb'
        pending = [m for m in _matches(tournament, 2) if m.confirmed_by is None]
        assert {(m.bracket, m.round) for m in pending} == {
            (Bracket.WINNERS, 1),
            (Bracket.LOSERS, 0),
        }
        for match in pending:
            play(match)
        assert _status(tournament) is TournamentStatus.ONGOING
        assert not [
            m
            for m in tournament_repository.get_matches_for_tournament(
                tournament.id
            )
            if m.bracket is Bracket.GRAND_FINAL
        ]

        # Everybody left is in the grand final, which is a phase 2 match.
        generated = tournament_match_service.generate_ffa_grand_final(
            tournament.id, initiator_id=admin.id
        )
        assert generated.unwrap() == 1
        (decisive,) = [
            m
            for m in tournament_repository.get_matches_for_tournament(
                tournament.id
            )
            if m.bracket is Bracket.GRAND_FINAL
        ]
        assert decisive.phase == 2
        assert len(_contestants(decisive)) == 6
        assert _status(tournament) is TournamentStatus.ONGOING
        play(decisive, reverse=True)

    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert ids[0] in _contestants(decisive)

    # The podium is the placements of the deciding lobby (played in
    # reverse strength), not the leaderboard ranks of phase 1.
    by_place = {
        c.placement: str(c.participant_id)
        for c in tournament_repository.get_contestants_for_match(decisive.id)
    }
    user_of = dict(zip(ids, users, strict=True))
    assert str(found.winner_participant_id) == by_place[1] != ids[0]
    assert (by_place[2], by_place[3]) != (ids[1], ids[2])
    assert tournament_service.resolve_podium_display_names(found) == {
        'runner_up': user_of[by_place[2]].screen_name,
        'bronze': user_of[by_place[3]].screen_name,
    }
    assert all(
        m.phase == 2
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
    )
    types = _log_types(tournament)
    assert types.index('qualification-leaderboard-closed') < types.index(
        'playoffs-released'
    )
    assert types.count('playoffs-released') == 1
