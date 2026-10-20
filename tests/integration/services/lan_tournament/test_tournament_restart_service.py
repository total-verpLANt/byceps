"""
tests.integration.services.lan_tournament.test_tournament_restart_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Restart a running team tournament back to `REGISTRATION_CLOSED`.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    signals,
    tournament_log_service,
    tournament_match_service,
    tournament_qualification_repository,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_restart_service as restart_service,
    tournament_seeding_repository,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
    QualificationDecision,
    QualificationDecisionID,
)
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_seeding import (
    TournamentSeeding,
    TournamentSeedingID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
CLOSED = TournamentStatus.REGISTRATION_CLOSED

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand, PartyID(f'restart-service-{generate_uuid7()}'), 'Restart'
    )


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:12]
    return [make_user(f'RestartSvc{suffix}P{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'RestartSvcAdmin{str(generate_uuid7())[:12]}')


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


def _register(tournament, users, contestant_type):
    """Register the users, each one captaining a team in a team tournament.

    Return the team IDs.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    team_ids = []
    for user in users:
        team_id = None
        if contestant_type is ContestantType.TEAM:
            team_id = TournamentTeamID(generate_uuid7())
            tournament_repository.create_team(
                TournamentTeam(
                    id=team_id,
                    tournament_id=tournament.id,
                    name=f'Restart team {generate_uuid7()}',
                    tag=None,
                    description=None,
                    image_url=None,
                    captain_user_id=user.id,
                    join_code=None,
                    created_at=now,
                )
            )
            db.session.flush()
            team_ids.append(team_id)
        tournament_repository.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=team_id,
                created_at=now,
            )
        )
    db.session.commit()
    return team_ids


@pytest.fixture
def build(party, players, admin):
    """Build a team tournament that is generated and then started.

    `create_args` override the 1v1 single elimination defaults.
    """

    def _build(
        status=PAUSED,
        *,
        team_count=4,
        contestant_type=ContestantType.TEAM,
        start=True,
        **create_args,
    ):
        settings = {
            'game_format': GameFormat.ONE_V_ONE,
            'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
            **create_args,
        }
        result = tournament_service.create_tournament(
            party.id,
            f'Restart service {next(_counter)} {generate_uuid7()}',
            contestant_type=contestant_type,
            tournament_status=CLOSED,
            **settings,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()

        _register(tournament, players[:team_count], contestant_type)

        board = seeding.get_board(tournament.id, initiator_id=admin.id)
        generated = seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.unwrap().version,
            initiator_id=admin.id,
        )
        assert generated.is_ok(), generated.unwrap_err()

        if start:
            _change_status(tournament, ONGOING, admin)
            if status is PAUSED:
                _change_status(tournament, PAUSED, admin)
        return tournament

    return _build


@pytest.fixture
def build_leaderboard(party, players, admin):
    """Build a running HIGHSCORE team tournament with a closed leaderboard."""

    def _build(*, release_mode=PlayoffReleaseMode.MANUAL, close=True):
        result = tournament_service.create_tournament(
            party.id,
            f'Restart leaderboard {next(_counter)} {generate_uuid7()}',
            contestant_type=ContestantType.TEAM,
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
            tournament_status=CLOSED,
            playoff_game_format=GameFormat.FREE_FOR_ALL,
            playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            playoff_qualifier_count=4,
            playoff_release_mode=release_mode,
            point_table=[5, 3, 2, 1],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()

        team_ids = _register(tournament, players[:5], ContestantType.TEAM)
        _change_status(tournament, ONGOING, admin)
        for team_id, score in zip(team_ids, (50, 40, 30, 20, 10), strict=True):
            submitted = tournament_score_service.submit_score(
                tournament.id, score, team_id=team_id
            )
            assert submitted.is_ok(), submitted.unwrap_err()
        if close:
            closed = tournament_score_service.close_leaderboard(
                tournament.id, initiator_id=admin.id
            )
            assert closed.is_ok(), closed.unwrap_err()
        return tournament

    return _build


def _change_status(tournament, status, admin):
    changed = tournament_service.change_status(tournament.id, status, admin.id)
    assert changed.is_ok(), changed.unwrap_err()


def _restart_flush(tournament, admin, *, reason='A new team joins', **extra):
    """Run the flush as its caller does: lock first, read under the lock."""
    locked = tournament_repository.get_tournament_for_update(tournament.id)
    return restart_service.restart_tournament_flush(
        locked, reason=reason, initiator_id=admin.id, **extra
    )


def _restart(tournament, admin, **extra):
    """Restart, commit and dispatch, and return the outcome."""
    result = _restart_flush(tournament, admin, **extra)
    assert result.is_ok(), result.unwrap_err()
    tournament_repository.commit_session()
    outcome = result.unwrap()
    restart_service.dispatch_restart_effects(outcome, admin.id)
    return outcome


def _fresh(tournament):
    db.session.rollback()
    db.session.expire_all()
    return tournament_repository.get_tournament(tournament.id, fresh=True)


def _matches(tournament):
    db.session.rollback()
    return tournament_match_service.get_matches_for_tournament_ordered(
        tournament.id
    )


def _entries(tournament, event_type):
    db.session.rollback()
    return [
        e
        for e in tournament_log_service.get_entries_for_tournament(
            tournament.id
        )
        if e.event_type == event_type
    ]


def _confirm_first_real_match(tournament, admin):
    for match in _matches(tournament):
        contestants = tournament_match_service.get_contestants_for_match(
            match.id
        )
        real = [c for c in contestants if c.team_id or c.participant_id]
        if match.confirmed_by is None and len(real) == 2:
            first, second = real
            scores = {
                (first.team_id or first.participant_id): 3,
                (second.team_id or second.participant_id): 1,
            }
            confirmed = tournament_match_service.admin_set_and_confirm_match(
                match.id, admin.id, scores
            )
            assert confirmed.is_ok(), confirmed.unwrap_err()
            return match
    raise AssertionError('no match with two contestants to confirm')


def _phase_two(tournament):
    return [m for m in _matches(tournament) if m.phase == 2]


def _play_groups(tournament, admin):
    """Play every phase 1 match; the lower team ID always wins.

    Group 0 gets one wide margin, so no two groups tie across the cut.
    """
    contestants = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )
    groups: dict[int, list] = {}
    for match in _matches(tournament):
        if match.phase == 1:
            ids = [c.team_id for c in contestants[match.id]]
            groups.setdefault(match.group_order, []).append((match, ids))
    for group, matches in groups.items():
        members = sorted({i for _, ids in matches for i in ids}, key=str)
        for match, ids in matches:
            low = min(ids, key=str)
            margin = 10 if group == 0 and members.index(low) == 0 else 1
            scores = (margin, 0) if ids[0] == low else (0, margin)
            confirmed = tournament_match_service.admin_set_and_confirm_match(
                match.id, admin.id, dict(zip(ids, scores, strict=True))
            )
            assert confirmed.is_ok(), confirmed.unwrap_err()


class _Capture:
    """Collect the events of one signal while it is connected."""

    def __init__(self, signal):
        self.signal = signal
        self.events = []

    def __enter__(self):
        self.signal.connect(self._receive)
        return self

    def __exit__(self, *exc_info):
        self.signal.disconnect(self._receive)

    def _receive(self, sender, *, event):
        self.events.append(event)


def test_restart_clears_all_matches_and_returns_to_registration_closed(
    build, admin
):
    tournament = build(PAUSED)
    match_count = len(_matches(tournament))
    assert match_count > 0
    assert _fresh(tournament).tournament_status is PAUSED

    with (
        _Capture(signals.match_deleted) as deleted,
        _Capture(signals.tournament_status_changed) as changed,
    ):
        result = _restart_flush(tournament, admin)
        assert result.is_ok(), result.unwrap_err()
        assert deleted.events == []
        assert changed.events == []

        tournament_repository.commit_session()
        outcome = result.unwrap()
        restart_service.dispatch_restart_effects(outcome, admin.id)

    assert _matches(tournament) == []
    assert _fresh(tournament).tournament_status is CLOSED
    assert len(deleted.events) == match_count
    assert [(e.old_status, e.new_status) for e in changed.events] == [
        (PAUSED, CLOSED)
    ]


def test_restart_from_ongoing_freezes_the_clock(build, admin):
    tournament = build(ONGOING)
    running = _fresh(tournament)
    assert running.tournament_status is ONGOING
    assert running.operational_clock_running_since is not None
    assert running.operational_clock_elapsed_us == 0

    _restart(tournament, admin)

    restarted = _fresh(tournament)
    assert restarted.tournament_status is CLOSED
    assert restarted.operational_clock_elapsed_us > 0
    assert restarted.operational_clock_running_since is None
    assert restarted.operational_clock_activated_at is not None


def test_restart_resets_playoff_release_and_decisions(build, admin):
    tournament = build(PAUSED)
    now = datetime.now(UTC).replace(tzinfo=None)
    db_tournament = db.session.get(DbTournament, tournament.id)
    db_tournament.playoff_released_at = now
    db_tournament.playoff_released_by = admin.id
    db_tournament.playoff_auto_release_suspended = True
    tournament_qualification_repository.upsert_decision(
        QualificationDecision(
            id=QualificationDecisionID(generate_uuid7()),
            tournament_id=tournament.id,
            scope='group:0',
            blocks=(
                DecisionBlock(
                    contestant_ids=('a', 'b'),
                    reason='Tie',
                    decided_by=admin.id,
                    decided_at=now,
                ),
            ),
            reason='Tie',
            decided_by=admin.id,
            decided_at=now,
        )
    )
    db.session.commit()
    before = _fresh(tournament)
    assert before.playoff_released_at is not None
    assert before.playoff_auto_release_suspended is True
    assert tournament_qualification_repository.get_decisions_for_tournament(
        tournament.id
    )

    outcome = _restart(tournament, admin)

    after = _fresh(tournament)
    assert after.playoff_released_at is None
    assert after.playoff_released_by is None
    assert after.playoff_auto_release_suspended is False
    assert (
        tournament_qualification_repository.get_decisions_for_tournament(
            tournament.id
        )
        == {}
    )
    assert outcome.impact.playoffs_released is True


def test_restart_keeps_the_initial_draft_and_drops_other_drafts(build, admin):
    tournament = build(PAUSED)
    now = datetime.now(UTC).replace(tzinfo=None)
    for target in ('playoff', 'ffa:SE:2'):
        tournament_seeding_repository.create_seeding(
            TournamentSeeding(
                id=TournamentSeedingID(generate_uuid7()),
                tournament_id=tournament.id,
                target=target,
                seed_code='a b',
                version=1,
                generated_seed_code=None,
                generated_at=None,
                updated_by=admin.id,
                created_at=now,
                updated_at=now,
            )
        )
    db.session.commit()
    before = tournament_seeding_repository.get_seedings_for_tournament(
        tournament.id
    )
    assert {s.target for s in before} == {'initial', 'playoff', 'ffa:SE:2'}
    (initial_before,) = [s for s in before if s.target == 'initial']

    _restart(tournament, admin)

    db.session.rollback()
    after = tournament_seeding_repository.get_seedings_for_tournament(
        tournament.id
    )
    assert [s.target for s in after] == ['initial']
    assert after[0].id == initial_before.id
    assert after[0].seed_code == initial_before.seed_code


def test_restart_logs_reason_and_impact(build, admin):
    tournament = build(ONGOING)
    _confirm_first_real_match(tournament, admin)
    match_count = len(_matches(tournament))

    _restart(
        tournament,
        admin,
        reason='  A new team joins\r\nlate  ',
        log_context={'team_name': 'Latecomers'},
    )

    (entry,) = _entries(tournament, 'tournament-restarted')
    assert entry.initiator_id == admin.id
    assert entry.data == {
        'reason': 'A new team joins\nlate',
        'old_status': 'ONGOING',
        'match_count': match_count,
        'confirmed_result_count': 1,
        'playoffs_were_released': False,
        'leaderboard_was_closed': False,
        'team_name': 'Latecomers',
    }
    (cleared,) = _entries(tournament, 'bracket-cleared')
    assert cleared.data['match_count'] == match_count
    assert len(cleared.data['confirmed_results']) >= 1


def test_restart_reopens_a_closed_leaderboard(build_leaderboard, admin):
    tournament = build_leaderboard()
    before = _fresh(tournament)
    assert before.leaderboard_closed_at is not None
    assert tournament_score_service.scores_locked(before)

    _restart(tournament, admin)

    after = _fresh(tournament)
    assert after.tournament_status is CLOSED
    assert after.leaderboard_closed_at is None
    assert not tournament_score_service.scores_locked(after)
    (entry,) = _entries(tournament, 'tournament-restarted')
    assert entry.data['leaderboard_was_closed'] is True
    assert entry.data['playoffs_were_released'] is False


def test_restart_does_not_release_an_old_leaderboard_on_the_next_start(
    build_leaderboard, admin
):
    tournament = build_leaderboard()
    db_tournament = db.session.get(DbTournament, tournament.id)
    db_tournament.playoff_release_mode = PlayoffReleaseMode.AUTOMATIC
    db.session.commit()
    assert not _phase_two(tournament)

    _restart(tournament, admin)
    _change_status(tournament, ONGOING, admin)

    after = _fresh(tournament)
    assert after.tournament_status is ONGOING
    assert after.playoff_released_at is None
    assert not _phase_two(tournament)


def test_restart_clears_a_double_elimination_bracket(build, admin):
    tournament = build(
        PAUSED, elimination_mode=EliminationMode.DOUBLE_ELIMINATION
    )
    _confirm_first_real_match(tournament, admin)
    matches = _matches(tournament)
    assert any(m.confirmed_by is not None for m in matches)
    assert any(m.next_match_id is not None for m in matches)
    assert any(m.loser_next_match_id is not None for m in matches)

    outcome = _restart(tournament, admin)

    assert _matches(tournament) == []
    assert _fresh(tournament).tournament_status is CLOSED
    assert outcome.impact.match_count == len(matches)
    assert outcome.impact.confirmed_result_count == 1
    assert len(outcome.deleted_events) == len(matches)


@pytest.mark.parametrize(
    ('playoff_mode', 'status'),
    [
        (EliminationMode.SINGLE_ELIMINATION, ONGOING),
        (EliminationMode.DOUBLE_ELIMINATION, PAUSED),
    ],
)
def test_restart_clears_both_phases_of_a_released_playoff(
    build, admin, playoff_mode, status
):
    tournament = build(
        ONGOING,
        team_count=8,
        elimination_mode=EliminationMode.ROUND_ROBIN,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=playoff_mode,
        playoff_group_count=2,
        playoff_qualifiers_per_group=2,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    _play_groups(tournament, admin)
    board = seeding.ensure_playoff_draft(tournament.id).unwrap()
    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )
    assert released.is_ok(), released.unwrap_err()
    if status is PAUSED:
        _change_status(tournament, PAUSED, admin)

    matches = _matches(tournament)
    phase_two = [m for m in matches if m.phase == 2]
    assert phase_two
    assert len(phase_two) < len(matches)
    assert any(m.next_match_id is not None for m in phase_two)
    assert any(m.loser_next_match_id is not None for m in phase_two)
    before = _fresh(tournament)
    assert before.tournament_status is status
    assert before.playoff_released_at is not None

    outcome = _restart(tournament, admin)

    assert _matches(tournament) == []
    assert len(outcome.deleted_events) == len(matches)
    assert outcome.impact.playoffs_released is True
    after = _fresh(tournament)
    assert after.tournament_status is CLOSED
    assert after.playoff_released_at is None
    assert after.playoff_released_by is None
    (entry,) = _entries(tournament, 'tournament-restarted')
    assert entry.data['playoffs_were_released'] is True
    assert entry.data['match_count'] == len(matches)


def test_restart_refuses_a_registration_closed_tournament(build, admin):
    tournament = build(start=False)
    match_count = len(_matches(tournament))
    assert match_count > 0
    assert _fresh(tournament).tournament_status is CLOSED

    result = _restart_flush(tournament, admin)

    assert result.unwrap_err() == restart_service.RESTART_NOT_RUNNING_ERROR
    tournament_repository.rollback_session()
    assert len(_matches(tournament)) == match_count
    assert _entries(tournament, 'tournament-restarted') == []
    assert _entries(tournament, 'bracket-cleared') == []


def test_restart_refuses_a_solo_tournament(build, admin):
    tournament = build(PAUSED, contestant_type=ContestantType.SOLO)
    match_count = len(_matches(tournament))
    assert match_count > 0

    result = _restart_flush(tournament, admin)

    assert result.unwrap_err() == restart_service.RESTART_NOT_TEAM_ERROR
    tournament_repository.rollback_session()
    assert len(_matches(tournament)) == match_count
    assert _fresh(tournament).tournament_status is PAUSED
    assert _entries(tournament, 'tournament-restarted') == []


def test_restart_refuses_a_blank_reason(build, admin):
    tournament = build(PAUSED)
    match_count = len(_matches(tournament))

    for reason in ('', '   ', '\n\t'):
        result = _restart_flush(tournament, admin, reason=reason)

        assert (
            result.unwrap_err() == restart_service.RESTART_REASON_MISSING_ERROR
        )
        tournament_repository.rollback_session()

    assert len(_matches(tournament)) == match_count
    assert _fresh(tournament).tournament_status is PAUSED
    assert _entries(tournament, 'tournament-restarted') == []
    assert _entries(tournament, 'bracket-cleared') == []


def test_restart_impact_counts_confirmed_results_but_not_byes(build, admin):
    tournament = build(ONGOING, team_count=3)
    matches = _matches(tournament)
    by_match = tournament_repository.get_contestants_for_matches(
        [m.id for m in matches]
    )
    byes = [
        m
        for m in matches
        if m.confirmed_by is not None and len(by_match.get(m.id, ())) < 2
    ]
    assert byes, 'a bracket of three teams has a confirmed bye'
    _confirm_first_real_match(tournament, admin)

    impact = restart_service.get_restart_impact(tournament.id)

    confirmed = [m for m in _matches(tournament) if m.confirmed_by]
    assert len(confirmed) == len(byes) + 1
    assert impact.match_count == len(matches)
    assert impact.confirmed_result_count == 1
    assert impact.playoffs_released is False


def test_generic_change_status_still_refuses_paused_to_registration_closed(
    build, admin
):
    paused = build(PAUSED)
    ongoing = build(ONGOING)

    for tournament, status in ((paused, PAUSED), (ongoing, ONGOING)):
        result = tournament_service.change_status(
            tournament.id, CLOSED, admin.id
        )

        assert result.is_err()
        assert _fresh(tournament).tournament_status is status
        assert len(_matches(tournament)) > 0
