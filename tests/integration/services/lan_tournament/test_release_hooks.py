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
from byceps.services.lan_tournament.tournament_qualification_domain_service import (
    TieKind,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-release-hooks')

_counter = count(1)

MODES = [PlayoffReleaseMode.MANUAL, PlayoffReleaseMode.AUTOMATIC]


def _release_failure(*args, **kwargs):
    raise RuntimeError('injected release failure')


def test_update_survives_a_failing_auto_release(make_tournament, users, monkeypatch, caplog):
    tournament = _make_groups_tournament(make_tournament, users, PlayoffReleaseMode.AUTOMATIC)
    monkeypatch.setattr(tournament_qualification_service, 'try_auto_release', _release_failure)
    result = tournament_service.update_tournament(
        tournament.id, name=tournament.name,
        game_format=tournament.game_format, elimination_mode=tournament.elimination_mode,
        playoff_qualifiers_per_group=1, initiator_id=users[0].id,
    )
    assert result.is_ok(), result.unwrap_err()
    db.session.expire_all()
    assert tournament_repository.get_tournament(tournament.id).playoff_qualifiers_per_group == 1
    assert 'Automatic playoff release failed' in caplog.text


def test_auto_release_and_rollback_failures_after_commit_are_logged_not_raised(
    make_tournament, users, monkeypatch, caplog
):
    tournament = _make_groups_tournament(make_tournament, users, PlayoffReleaseMode.AUTOMATIC)
    with monkeypatch.context() as scoped:
        scoped.setattr(tournament_qualification_service, 'try_auto_release', _release_failure)
        scoped.setattr(tournament_repository, 'rollback_session', _release_failure)
        tournament_qualification_service.auto_release_after_commit(
            tournament.id, triggered_by=users[0].id
        )
    db.session.expire_all()
    assert tournament_repository.get_tournament(tournament.id).tournament_status is TournamentStatus.ONGOING
    assert 'Automatic playoff release failed' in caplog.text
    assert 'Rollback after automatic playoff release failed' in caplog.text


def test_removal_signals_fire_before_the_auto_release(make_tournament, users, monkeypatch):
    from byceps.services.lan_tournament import signals

    tournament = _make_groups_tournament(make_tournament, users, PlayoffReleaseMode.AUTOMATIC)
    participant = tournament_repository.get_participants_for_tournament(tournament.id)[-1]
    order = []
    def receiver(sender, **kwargs):
        order.append('participant_left')
    monkeypatch.setattr(tournament_qualification_service, 'try_auto_release', lambda *a, **kw: order.append('release'))
    with signals.participant_left.connected_to(receiver):
        result = tournament_participant_service.admin_remove_participant(
            tournament.id, participant.id, initiator=users[0]
        )
    assert result.is_ok(), result.unwrap_err()
    assert order == ['participant_left', 'release']


@pytest.mark.parametrize('removal', ['admin', 'ticketless', 'last-member'])
def test_all_team_removal_signals_precede_release(make_tournament, users, monkeypatch, removal):
    from byceps.services.lan_tournament import signals, tournament_team_service
    from tests.integration.services.lan_tournament import test_removed_contestants as removed

    tournament, teams = removed._make_team_groups(
        make_tournament, users, PlayoffReleaseMode.AUTOMATIC
    )
    team = teams[0]
    if removal != 'ticketless':
        captain = tournament_repository.find_participant_by_user(tournament.id, team.captain_user_id)
        tournament_participant_service.admin_remove_participant(
            tournament.id, captain.id, initiator=users[0]
        ).unwrap()
        if removal == 'last-member':
            # Legacy data: only a team orphaned before the handover reaches
            # the empty-team branch of remove_team_member.
            removed._orphan_the_captaincy(team, captain)
    members = tournament_repository.get_participants_for_team(team.id)
    order = []
    def member_left(sender, **kwargs):
        order.append('member_left')
    def participant_left(sender, **kwargs):
        order.append('participant_left')
    def team_deleted(sender, **kwargs):
        order.append('team_deleted')
    monkeypatch.setattr(tournament_qualification_service, 'try_auto_release',
                        lambda *a, **kw: order.append('release'))
    with (signals.team_member_left.connected_to(member_left),
          signals.participant_left.connected_to(participant_left),
          signals.team_deleted.connected_to(team_deleted)):
        if removal == 'admin':
            result = tournament_participant_service.admin_remove_participant(
                tournament.id, members[0].id, initiator=users[0]
            )
        elif removal == 'last-member':
            result = tournament_team_service.remove_team_member(
                team.id, members[0].user_id, initiator_id=users[0].id
            )
        else:
            removed_users = {member.user_id for member in members}
            monkeypatch.setattr(tournament_participant_service.ticket_service,
                                'select_ticket_users_for_party',
                                lambda user_ids, party_id: user_ids - removed_users)
            result = tournament_participant_service.remove_participants_without_tickets(
                tournament.id, PARTY_ID, initiator_id=users[0].id
            )
    assert result.is_ok(), result.unwrap_err()
    assert order[-1] == 'release'
    assert order.index('team_deleted') < order.index('release')
    assert order.count('release') == 1
    assert order.count('member_left') == len(members)
    if removal != 'last-member':
        assert order.count('participant_left') == len(members)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('releasehooksbrand', 'Release Hooks Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Release Hooks')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'ReleaseHooksUser{i}') for i in range(16)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(*, contestant_type=ContestantType.SOLO, participants=8, **args):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Release Hooks Tournament {next(_counter)}',
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


def _open_match_with_weakest(tournament, *, group=0):
    """Return the match of a group's strongest and weakest contestant.

    The strongest ID wins every match, so the weakest is no qualifier.
    Group 0's scoreless walkover leaves distinct crossover metrics. In
    group 1 it instead ties the two winners (9 points, 6–0, 3 played).
    Match retrieval is unordered; never infer this scenario from its first row.
    """
    matches = _group_matches(tournament)
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
def test_removal_settling_last_match_preserves_unresolved_crossover_tie(
    make_tournament, users, mode
):
    """Settling all matches is not enough when playoff seeds still tie."""
    admin = users[0]
    tournament = _make_groups_tournament(make_tournament, users, mode)
    open_match, weakest = _open_match_with_weakest(tournament, group=1)
    for match in _group_matches(tournament):
        if match.id != open_match.id:
            _confirm(match, admin)
    before = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert before.open_match_count == 1
    assert not before.ready
    assert not _has_draft(tournament)
    assert not _released(tournament)

    removed = tournament_participant_service.admin_remove_participant(
        tournament.id, TournamentParticipantID(UUID(weakest)), initiator=admin
    )
    assert removed.is_ok(), removed.unwrap_err()
    assert all(m.confirmed_by for m in _group_matches(tournament))
    state = tournament_qualification_service.get_qualification(
        tournament.id
    ).unwrap()
    assert state.open_match_count == 0
    assert not state.ready
    assert state.seed_order is None
    (tie,) = state.blockers
    assert tie.scope == 'crossover'
    assert tie.kind is TieKind.SEEDING
    assert not tie.decided
    winners = tuple(ranking.entries[0] for ranking in state.rankings)
    assert set(tie.contestant_ids) == {entry.contestant_id for entry in winners}
    assert all(
        (entry.row.played, entry.row.points, entry.row.score_for, entry.row.score_against)
        == (3, 9, 6, 0)
        for entry in winners
    )
    assert not _has_draft(tournament)
    assert not _released(tournament)
    assert not any(
        match.phase == 2
        for match in tournament_repository.get_matches_for_tournament(tournament.id)
    )
    retried = tournament_qualification_service.try_auto_release(
        tournament.id, triggered_by=admin.id
    )
    assert retried.is_ok(), retried.unwrap_err()
    assert retried.unwrap() is False
    assert not _has_draft(tournament)
    assert not _released(tournament)


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
