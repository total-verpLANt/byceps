"""
tests.integration.services.lan_tournament.test_removed_contestants
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A contestant removed from a running tournament keeps their confirmed
results for their opponents, but is never a qualifier or a winner.
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_repository,
    tournament_seeding_service,
    tournament_service,
    tournament_team_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    build_round_robin_standings,
)
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


PARTY_ID = PartyID('lan-party-2024-removed-contestants')

_counter = count(1)

MODES = [PlayoffReleaseMode.MANUAL, PlayoffReleaseMode.AUTOMATIC]


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('removedcontestantsbrand', 'Removed Contestants Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Removed Contestants')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'RemovedContestantsUser{i}') for i in range(16)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(*, contestant_type=ContestantType.SOLO, participants=8, **args):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Removed Contestants Tournament {next(_counter)}',
            contestant_type=contestant_type,
            **args,
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _start(tournament, admin):
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()


def _make_groups_tournament(make_tournament, users, mode, **args):
    """Return a started RR tournament: two groups, two qualifiers each."""
    tournament = make_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=mode,
        **args,
    )
    _start(tournament, users[0])
    return tournament


def _matches(tournament, phase=None):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if phase is None or m.phase == phase
    ]


def _members(match):
    return [
        str(c.participant_id or c.team_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    ]


def _group_members(tournament, group):
    return sorted(
        {
            cid
            for m in _matches(tournament, 1)
            if m.group_order == group
            for cid in _members(m)
        }
    )


def _play(match, admin):
    """Let the stronger (lower) ID win by 2 + the group number."""
    stronger, weaker = sorted(_members(match))
    margin = 2 + (match.group_order or 0)
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(stronger): margin, UUID(weaker): 0}
    )
    assert result.is_ok(), result.unwrap_err()


def _play_all_but(tournament, admin, open_match):
    for match in _matches(tournament, 1):
        if match.id != open_match.id:
            _play(match, admin)


def _open_match(tournament, group, a, b):
    (match,) = [
        m
        for m in _matches(tournament, 1)
        if m.group_order == group and set(_members(m)) == {a, b}
    ]
    return match


def _qualifier_ids(tournament):
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    return state, [q.contestant_id for q in (state.qualifiers or ())]


def _remove(tournament, participant_id, admin):
    removed = tournament_participant_service.admin_remove_participant(
        tournament.id,
        TournamentParticipantID(UUID(str(participant_id))),
        initiator=admin,
    )
    assert removed.is_ok(), removed.unwrap_err()


def _released(tournament):
    return (
        tournament_repository.get_tournament(tournament.id).playoff_released_at
        is not None
    )


def _phase_two_contestants(tournament):
    return {cid for m in _matches(tournament, 2) for cid in _members(m)}


# -------------------------------------------------------------------- #
# group qualification


@pytest.mark.parametrize('grouped', [False, True], ids=['plain', 'groups'])
def test_unconfirmed_one_entry_match_counts_no_win(
    make_tournament, users, monkeypatch, grouped
):
    if grouped:
        tournament = _make_groups_tournament(
            make_tournament, users, PlayoffReleaseMode.MANUAL
        )
    else:
        tournament = make_tournament(
            participants=4,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        _start(tournament, users[0])
    matches = _matches(tournament, 1)
    by_match = tournament_repository.get_contestants_for_tournament(tournament.id)
    match = matches[0]
    assert match.confirmed_by is None
    by_match[match.id] = by_match[match.id][:1]
    monkeypatch.setattr(
        tournament_repository, 'get_contestants_for_tournament', lambda _: by_match
    )

    if grouped:
        state, _ = _qualifier_ids(tournament)
        rankings = state.rankings
        assert not state.ready
    else:
        standing = tournament_match_service.plain_round_robin_standing(tournament)
        rankings = [standing.ranking]
        assert standing.open_match_count == len(matches)
    for ranking in rankings:
        for entry in ranking.entries:
            assert (entry.row.played, entry.row.won, entry.row.points) == (0, 0, 0)
    assert build_round_robin_standings([
        {'match': m, 'contestants': by_match[m.id]} for m in matches
    ]) == []


def test_group_withdrawal_gives_every_remaining_opponent_the_walkover(
    make_tournament, users
):
    admin = users[0]
    tournament = _make_groups_tournament(
        make_tournament, users, PlayoffReleaseMode.MANUAL
    )
    leaver, *opponents = _group_members(tournament, 0)
    played = _open_match(tournament, 0, leaver, opponents[0])
    tournament_match_service.admin_set_and_confirm_match(
        played.id, admin.id, {UUID(leaver): 0, UUID(opponents[0]): 5}
    ).unwrap()
    _remove(tournament, leaver, admin)

    state, _ = _qualifier_ids(tournament)
    ranking = next(r for r in state.rankings if r.scope == 'group:0')
    rows = {e.contestant_id: e.row for e in ranking.entries}
    assert set(rows) == set(opponents)
    for cid in opponents:
        row = rows[cid]
        assert (row.played, row.won, row.drawn, row.lost) == (1, 1, 0, 0)
        assert row.points == 3
        assert row.score_for == (5 if cid == opponents[0] else 0)
        assert row.score_against == 0
    assert ranking.open_matches == 3

    public = build_round_robin_standings([
        {
            'match': m,
            'contestants': tournament_repository.get_contestants_for_match(m.id),
        }
        for m in _matches(tournament, 1)
        if m.group_order == 0
    ])
    public_rows = {r.contestant_id: r for r in public}
    for cid, row in rows.items():
        shown = public_rows[cid]
        assert (shown.wins, shown.points) == (row.won, row.points)
        assert (shown.score_for, shown.score_against) == (
            row.score_for, row.score_against
        )


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_removed_group_leader_does_not_qualify(make_tournament, users, mode):
    admin = users[0]
    tournament = _make_groups_tournament(make_tournament, users, mode)
    leader, second, third, fourth = _group_members(tournament, 0)
    open_match = _open_match(tournament, 0, leader, fourth)
    _play_all_but(tournament, admin, open_match)

    # The removal confirms the open match by defwin and settles the group.
    _remove(tournament, leader, admin)

    state, qualifiers = _qualifier_ids(tournament)
    assert state.ready
    assert leader not in qualifiers
    group_zero = {
        q.contestant_id for q in state.qualifiers if q.scope == 'group:0'
    }
    assert group_zero == {second, third}
    ranking = next(r for r in state.rankings if r.scope == 'group:0')
    assert leader not in [e.contestant_id for e in ranking.entries]
    # The results against the removed leader still count.
    assert (
        next(e for e in ranking.entries if e.contestant_id == second).row.points
        == 6
    )

    if mode is PlayoffReleaseMode.AUTOMATIC:
        assert _released(tournament)
        assert _phase_two_contestants(tournament) == set(qualifiers)
        assert leader not in _phase_two_contestants(tournament)
    else:
        assert not _released(tournament)
        assert not _matches(tournament, 2)


def test_a_tie_that_only_existed_with_the_removed_member_disappears(
    make_tournament, users
):
    admin = users[0]
    tournament = _make_groups_tournament(
        make_tournament, users, PlayoffReleaseMode.MANUAL
    )
    leader, second, third, fourth = _group_members(tournament, 0)
    # The first three beat each other in a circle, and all beat the fourth:
    # three on 6 points, a tie across the cut of two.
    winners = {
        frozenset((leader, second)): leader,
        frozenset((second, third)): second,
        frozenset((third, leader)): third,
    }
    for match in _matches(tournament, 1):
        if match.group_order != 0:
            _play(match, admin)
            continue
        pair = frozenset(_members(match))
        winner = winners.get(pair) or next(c for c in pair if c != fourth)
        scores = {UUID(c): int(c == winner) for c in pair}
        tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, scores
        ).unwrap()
    state, _ = _qualifier_ids(tournament)
    (block,) = state.blockers
    assert set(block.contestant_ids) == {leader, second, third}
    assert not state.ready

    _remove(tournament, leader, admin)

    state, qualifiers = _qualifier_ids(tournament)
    assert not state.blockers
    assert state.ready
    assert leader not in qualifiers
    group_zero = [q for q in state.qualifiers if q.scope == 'group:0']
    assert [q.contestant_id for q in group_zero] == [second, third]


# -------------------------------------------------------------------- #
# plain round robin


def test_plain_round_robin_counts_a_walkover_win(make_tournament, users):
    admin = users[0]
    tournament = make_tournament(
        participants=4,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    _start(tournament, admin)
    leaver, winner, second, third = sorted(
        {cid for m in _matches(tournament) for cid in _members(m)}
    )
    for match in _matches(tournament):
        ids = _members(match)
        if leaver in ids and second not in ids:
            continue
        match_winner = winner if winner in ids else second
        scores = {UUID(cid): int(cid == match_winner) for cid in ids}
        tournament_match_service.admin_set_and_confirm_match(
            match.id, admin.id, scores
        ).unwrap()
    _remove(tournament, leaver, admin)
    standing = tournament_match_service.plain_round_robin_standing(tournament)
    rows = {e.contestant_id: e.row for e in standing.ranking.entries}
    assert rows[winner].points == 9
    assert (rows[winner].played, rows[winner].won, rows[winner].lost) == (3, 3, 0)
    assert (rows[winner].score_for, rows[winner].score_against) == (2, 0)
    assert rows[second].points == 6
    assert rows[third].points == 3
    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) == winner
    public = build_round_robin_standings([
        {
            'match': m,
            'contestants': tournament_repository.get_contestants_for_match(m.id),
        }
        for m in _matches(tournament)
    ])
    public_rows = {r.contestant_id: r for r in public}
    assert public[0].contestant_id == str(found.winner_participant_id)
    for cid, row in rows.items():
        shown = public_rows[cid]
        assert (shown.wins, shown.draws, shown.losses) == (
            row.won, row.drawn, row.lost
        )
        assert shown.points == row.points
        assert (shown.score_for, shown.score_against) == (
            row.score_for, row.score_against
        )


def test_removed_leader_does_not_win_a_plain_round_robin(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(
        participants=4,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    _start(tournament, admin)
    leader, second, third, fourth = sorted(
        {cid for m in _matches(tournament) for cid in _members(m)}
    )
    (open_match,) = [
        m for m in _matches(tournament) if set(_members(m)) == {leader, fourth}
    ]
    for match in _matches(tournament):
        if match.id != open_match.id:
            _play(match, admin)
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        is TournamentStatus.ONGOING
    )

    # The removal confirms the last match by defwin and completes the cup.
    _remove(tournament, leader, admin)

    found = tournament_repository.get_tournament(tournament.id)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) == second
    by_user = {
        str(p.id): p.user_id
        for p in tournament_repository.get_participants_for_tournament(
            tournament.id, include_removed=True
        )
    }
    names = {
        cid: next(u for u in users if u.id == user_id).screen_name
        for cid, user_id in by_user.items()
    }
    podium = tournament_service.resolve_podium_display_names(found)
    assert podium == {'runner_up': names[third], 'bronze': names[fourth]}
    assert names[leader] not in podium.values()


# -------------------------------------------------------------------- #
# highscore leaderboard


def test_removed_top_scorer_does_not_qualify_from_the_leaderboard(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=4,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
        point_table=[5, 3, 2, 1],
        group_size_min=3,
        group_size_max=4,
        advancement_count=2,
    )
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    values = [80, 70, 60, 50, 40, 30, 20, 10]
    for participant, value in zip(participants, values, strict=True):
        tournament_score_service.submit_score(
            tournament.id, value, participant_id=participant.id
        ).unwrap()
    top, *rest = [str(p.id) for p in participants]
    _remove(tournament, top, admin)

    state, qualifiers = _qualifier_ids(tournament)
    assert not state.ready
    ranking = state.rankings[0]
    assert top not in [e.contestant_id for e in ranking.entries]
    assert ranking.entries[0].contestant_id == rest[0]
    assert ranking.entries[0].rank == 1

    closed = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    )
    assert closed.is_ok(), closed.unwrap_err()

    state, qualifiers = _qualifier_ids(tournament)
    assert qualifiers == rest[:4]
    assert _released(tournament)
    assert _phase_two_contestants(tournament) == set(rest[:4])


# -------------------------------------------------------------------- #
# release


def test_release_is_refused_for_a_draft_with_a_removed_contestant(
    make_tournament, users
):
    admin = users[0]
    tournament = _make_groups_tournament(
        make_tournament, users, PlayoffReleaseMode.MANUAL
    )
    for match in _matches(tournament, 1):
        _play(match, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    # A touched draft is never rewritten by itself.
    draft = tournament_seeding_service.apply_action(
        tournament.id,
        tournament_seeding_service.PLAYOFF_TARGET,
        tournament_seeding_service.Swap(0, 1),
        expected_version=draft.version,
        initiator_id=admin.id,
    ).unwrap()
    _, drafted = _qualifier_ids(tournament)
    leader = _group_members(tournament, 0)[0]
    assert leader in drafted

    # Nothing is open, so the removal does not re-stage the draft.
    _remove(tournament, leader, admin)

    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=draft.version, initiator_id=admin.id
    )
    assert released.is_err()
    assert released.unwrap_err() == (
        tournament_qualification_service.ERR_REMOVED_QUALIFIER
    )
    assert not _released(tournament)
    assert not _matches(tournament, 2)

    # The board offers the re-seed; the release works from the new draft.
    board = tournament_seeding_service.get_board(
        tournament.id, tournament_seeding_service.PLAYOFF_TARGET
    ).unwrap()
    assert board.stale
    reseeded = tournament_seeding_service.apply_action(
        tournament.id,
        tournament_seeding_service.PLAYOFF_TARGET,
        tournament_seeding_service.ReseedKeepTiers(),
        expected_version=board.version,
        initiator_id=admin.id,
    )
    assert reseeded.is_ok(), reseeded.unwrap_err()
    released = tournament_qualification_service.release_playoffs(
        tournament.id,
        expected_version=reseeded.unwrap().version,
        initiator_id=admin.id,
    )
    assert released.is_ok(), released.unwrap_err()
    _, qualifiers = _qualifier_ids(tournament)
    assert leader not in qualifiers
    assert _phase_two_contestants(tournament) == set(qualifiers)


def test_untouched_draft_drops_a_removed_qualifier_by_itself(
    make_tournament, users
):
    admin = users[0]
    tournament = _make_groups_tournament(
        make_tournament, users, PlayoffReleaseMode.MANUAL
    )
    for match in _matches(tournament, 1):
        _play(match, admin)
    draft = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    leader = _group_members(tournament, 0)[0]
    assert leader in draft.state.roster

    _remove(tournament, leader, admin)

    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()
    assert not board.stale
    assert leader not in board.state.roster
    assert board.version == draft.version + 1
    state, qualifiers = _qualifier_ids(tournament)
    assert board.state.seed_list == tuple(
        q.contestant_id for q in state.seed_order
    )
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()
    assert _phase_two_contestants(tournament) == set(qualifiers)


# -------------------------------------------------------------------- #
# team removal hook


def _make_team_groups(make_tournament, users, mode):
    """Return a started team RR (two groups of four) and its teams.

    Every team has a captain and one more member.
    """
    tournament = make_tournament(
        contestant_type=ContestantType.TEAM,
        participants=16,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=mode,
    )
    teams = []
    for index, captain in enumerate(users[:8]):
        team, _ = tournament_team_service.create_team(
            tournament.id, f'RC Team {index}', captain.id
        ).unwrap()
        tournament_team_service.admin_add_member(
            team.id, users[8 + index].id
        ).unwrap()
        teams.append(team)
    _start(tournament, users[0])
    return tournament, teams


def _empty_the_weakest_team(tournament, teams, admin, initiator_id):
    """Leave the weakest team of group 0 with one open match, then empty it.

    The captain leaves first; removing the last member empties the team.
    Return the open match and the weakest team's ID.
    """
    group = _group_members(tournament, 0)
    leader, weakest = group[0], group[-1]
    team = next(t for t in teams if str(t.id) == weakest)
    open_match = _open_match(tournament, 0, leader, weakest)
    _play_all_but(tournament, admin, open_match)
    captain = tournament_repository.find_participant_by_user(
        tournament.id, team.captain_user_id
    )
    _remove(tournament, captain.id, admin)
    (member,) = tournament_repository.get_participants_for_team(team.id)
    assert tournament_repository.find_match(open_match.id).confirmed_by is None

    removed = tournament_team_service.remove_team_member(
        team.id, member.user_id, initiator_id=initiator_id
    )
    assert removed.is_ok(), removed.unwrap_err()
    return open_match, weakest


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_team_removal_settling_the_last_group_match_makes_release_due(
    make_tournament, users, mode
):
    admin = users[0]
    tournament, teams = _make_team_groups(make_tournament, users, mode)

    open_match, weakest = _empty_the_weakest_team(
        tournament, teams, admin, admin.id
    )

    assert tournament_repository.find_match(open_match.id).confirmed_by
    state, _ = _qualifier_ids(tournament)
    assert state.ready
    if mode is PlayoffReleaseMode.MANUAL:
        assert (
            tournament_seeding_repository.find_seeding(tournament.id, 'playoff')
            is not None
        )
        assert not _released(tournament)
    else:
        assert _released(tournament)
        assert weakest not in _phase_two_contestants(tournament)


def test_team_removal_without_an_initiator_confirms_nothing(
    make_tournament, users
):
    admin = users[0]
    tournament, teams = _make_team_groups(
        make_tournament, users, PlayoffReleaseMode.AUTOMATIC
    )

    open_match, _ = _empty_the_weakest_team(tournament, teams, admin, None)

    assert tournament_repository.find_match(open_match.id).confirmed_by is None
    assert not _released(tournament)


def test_team_removal_dispatches_the_defwin_signals(make_tournament, users):
    admin = users[0]
    tournament, teams = _make_team_groups(
        make_tournament, users, PlayoffReleaseMode.MANUAL
    )
    confirmed = []

    def spy(sender, *, event):
        confirmed.append(event)

    signals.match_confirmed.connect(spy, weak=False)
    try:
        open_match, _ = _empty_the_weakest_team(
            tournament, teams, admin, admin.id
        )
    finally:
        signals.match_confirmed.disconnect(spy)

    assert tournament_repository.find_match(open_match.id).confirmed_by
    assert open_match.id in [e.match_id for e in confirmed]
