"""
tests.integration.services.lan_tournament.test_release_hooks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The playoff draft and the automatic release run after every change
that can make a qualification ready: a removal whose defwin settles
the last group match, and the resume of a paused tournament. The
leaderboard closes only while the tournament is ongoing.
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import select

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_participant_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_repository,
    tournament_seeding_service,
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


PARTY_ID = PartyID('lan-party-2024-release-hooks')

_counter = count(1)

MODES = [PlayoffReleaseMode.MANUAL, PlayoffReleaseMode.AUTOMATIC]


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('releasehooksbrand', 'Release Hooks Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Release Hooks')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'ReleaseHooksUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(**args):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Release Hooks Tournament {next(_counter)}',
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


def _make_groups_tournament(make_tournament, users, mode):
    """Return a started RR tournament: two groups, two qualifiers each."""
    admin = users[0]
    tournament = make_tournament(
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=mode,
    )
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()
    return tournament


def _group_matches(tournament):
    return [
        m
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
        if m.phase == 1
    ]


def _contestants(match):
    return [
        str(c.participant_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    ]


def _open_match_with_weakest(tournament):
    """Return the match of a group's strongest and weakest contestant.

    The strongest ID wins every match, so the weakest is no qualifier.
    """
    matches = _group_matches(tournament)
    group = matches[0].group_order
    members = sorted(
        {
            cid
            for m in matches
            if m.group_order == group
            for cid in _contestants(m)
        }
    )
    strongest, weakest = members[0], members[-1]
    (match,) = [
        m for m in matches if set(_contestants(m)) == {strongest, weakest}
    ]
    return match, weakest


def _confirm(match, admin):
    stronger, weaker = sorted(_contestants(match))
    margin = 2 + (match.group_order or 0)
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(stronger): margin, UUID(weaker): 0}
    )
    assert result.is_ok(), result.unwrap_err()


def _has_draft(tournament):
    return (
        tournament_seeding_repository.find_seeding(tournament.id, 'playoff')
        is not None
    )


def _released(tournament):
    return (
        tournament_repository.get_tournament(tournament.id).playoff_released_at
        is not None
    )


def _assert_due(tournament, mode):
    """Assert what a due qualification leaves behind in `mode`."""
    assert (
        tournament_qualification_service.get_qualification(tournament.id)
        .unwrap()
        .ready
    )
    if mode is PlayoffReleaseMode.MANUAL:
        assert _has_draft(tournament)
        assert not _released(tournament)
    else:
        assert _released(tournament)


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_removal_settling_last_group_match_makes_release_due(
    make_tournament, users, mode
):
    admin = users[0]
    tournament = _make_groups_tournament(make_tournament, users, mode)
    open_match, weakest = _open_match_with_weakest(tournament)
    for match in _group_matches(tournament):
        if match.id != open_match.id:
            _confirm(match, admin)
    assert not _has_draft(tournament)
    assert not _released(tournament)

    participant_id = TournamentParticipantID(UUID(weakest))
    removed = tournament_participant_service.admin_remove_participant(
        tournament.id, participant_id, initiator=admin
    )
    assert removed.is_ok(), removed.unwrap_err()

    assert all(m.confirmed_by for m in _group_matches(tournament))
    _assert_due(tournament, mode)


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_resume_makes_a_ready_qualification_due(make_tournament, users, mode):
    admin = users[0]
    tournament = _make_groups_tournament(make_tournament, users, mode)
    matches = _group_matches(tournament)
    for match in matches[:-1]:
        _confirm(match, admin)
    paused = tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    )
    assert paused.is_ok(), paused.unwrap_err()
    _confirm(matches[-1], admin)
    assert not _has_draft(tournament)
    assert not _released(tournament)

    resumed = tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )
    assert resumed.is_ok(), resumed.unwrap_err()

    _assert_due(tournament, mode)


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_close_while_paused_is_refused_and_writes_nothing(
    make_tournament, users, mode
):
    admin = users[0]
    tournament = make_tournament(
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=8,
        playoff_release_mode=mode,
        point_table=[5, 3, 2, 1],
        group_size_min=3,
        group_size_max=4,
        advancement_count=2,
    )
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for participant, value in zip(
        participants, [80, 70, 60, 50, 40, 30, 20, 10], strict=True
    ):
        tournament_score_service.submit_score(
            tournament.id, value, participant_id=participant.id
        ).unwrap()
    tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    ).unwrap()

    closed = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    )

    assert closed.is_err()
    assert closed.unwrap_err() == (
        tournament_score_service.CLOSE_NOT_ONGOING_ERROR
    )
    found = tournament_repository.get_tournament(tournament.id)
    assert found.leaderboard_closed_at is None
    logged = db.session.scalars(
        select(DbTournamentLogEntry.event_type).filter_by(
            tournament_id=tournament.id
        )
    ).all()
    assert 'qualification-leaderboard-closed' not in logged

    # After the resume the close works and the release follows at once.
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()
    tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    _assert_due(tournament, mode)


def _confirm_with_winner(match, admin, winner):
    loser = next(c for c in _contestants(match) if c != winner)
    margin = 2 + (match.group_order or 0)
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(winner): margin, UUID(loser): 0}
    )
    assert result.is_ok(), result.unwrap_err()


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_removal_dissolving_a_group_tie_makes_release_due(
    make_tournament, users, mode
):
    """A removal alone, with every match confirmed, dissolves the tie."""
    admin = users[0]
    tournament = _make_groups_tournament(make_tournament, users, mode)
    matches = _group_matches(tournament)
    group = matches[0].group_order
    first, second, third, last = sorted(
        {
            cid
            for m in matches
            if m.group_order == group
            for cid in _contestants(m)
        }
    )
    # A three-way cycle across the cut, and `last` loses to everyone.
    cycle = {
        frozenset({first, second}): first,
        frozenset({second, third}): second,
        frozenset({third, first}): third,
    }
    for match in matches:
        pair = frozenset(_contestants(match))
        if pair in cycle:
            winner = cycle[pair]
        elif last in pair:
            winner = next(c for c in pair if c != last)
        else:
            winner = sorted(pair)[0]
        _confirm_with_winner(match, admin, winner)
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert state.open_match_count == 0
    assert state.blockers
    assert not _has_draft(tournament)
    assert not _released(tournament)

    removed = tournament_participant_service.admin_remove_participant(
        tournament.id,
        TournamentParticipantID(UUID(first)),
        initiator=admin,
    )
    assert removed.is_ok(), removed.unwrap_err()

    _assert_due(tournament, mode)


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_removal_after_the_close_dissolving_a_cut_tie_makes_release_due(
    make_tournament, users, mode
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
        playoff_release_mode=mode,
        point_table=[5, 3, 2, 1],
        group_size_min=4,
        group_size_max=4,
        advancement_count=2,
    )
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for participant, value in zip(
        participants, [80, 70, 60, 50, 50, 30, 20, 10], strict=True
    ):
        tournament_score_service.submit_score(
            tournament.id, value, participant_id=participant.id
        ).unwrap()
    tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert state.blockers
    assert not _released(tournament)

    removed = tournament_participant_service.admin_remove_participant(
        tournament.id, participants[4].id, initiator=admin
    )
    assert removed.is_ok(), removed.unwrap_err()

    _assert_due(tournament, mode)


@pytest.mark.parametrize('mode', MODES, ids=['manual', 'automatic'])
def test_correction_dissolving_a_cut_tie_makes_release_due(
    make_tournament, users, mode
):
    admin = users[0]
    tournament = _make_groups_tournament(make_tournament, users, mode)
    matches = _group_matches(tournament)
    group = matches[0].group_order
    first, second, third, last = sorted(
        {
            cid
            for m in matches
            if m.group_order == group
            for cid in _contestants(m)
        }
    )
    # A three-way cycle across the cut, and `last` loses to everyone.
    cycle = {
        frozenset({first, second}): first,
        frozenset({second, third}): second,
        frozenset({third, first}): third,
    }
    for match in matches:
        pair = frozenset(_contestants(match))
        if pair in cycle:
            winner = cycle[pair]
        elif last in pair:
            winner = next(c for c in pair if c != last)
        else:
            winner = sorted(pair)[0]
        _confirm_with_winner(match, admin, winner)
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert state.open_match_count == 0
    assert state.blockers
    assert not _has_draft(tournament)
    assert not _released(tournament)

    # Third beats first becomes first beats third: the tie is gone.
    (subject,) = [
        m for m in matches if frozenset(_contestants(m)) == {first, third}
    ]
    corrected = tournament_match_service.correct_match_result(
        subject.id,
        admin.id,
        reason='typo',
        corrected_scores={UUID(first): 2, UUID(third): 0},
    )
    assert corrected.is_ok(), corrected.unwrap_err()

    _assert_due(tournament, mode)
