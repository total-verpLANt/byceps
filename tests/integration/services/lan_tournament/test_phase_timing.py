from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text, update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as matches,
    tournament_operational_service as operational,
    tournament_participant_service,
    tournament_qualification_service as qualification,
    tournament_repository as repo,
    tournament_score_service,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.dashboard import DbMatchDueEpisode
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
)
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


START = datetime(2031, 5, 6, 18, 0, 0)
SCHEDULED = datetime(2031, 5, 6, 15, 0, 0)
STARTED_AT = START + timedelta(hours=1)
RELEASE_AT = START + timedelta(hours=5)
UNRELEASE_AT = START + timedelta(hours=6)
RERELEASE_AT = START + timedelta(hours=7)
HOUR_US = 3_600 * 1_000_000

CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING

SE = EliminationMode.SINGLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN

MANUAL = PlayoffReleaseMode.MANUAL
AUTOMATIC = PlayoffReleaseMode.AUTOMATIC


class _Clock:
    """Stand in for the server operation time, one reading per call."""

    def __init__(self, now: datetime, step: timedelta = timedelta(seconds=1)):
        self.now = now
        self.step = step
        self.readings = 0

    def __call__(self) -> datetime:
        self.readings += 1
        value = self.now
        self.now += self.step
        return value


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    suffix = str(generate_uuid7())
    brand = make_brand(f'phasetiming{suffix}', f'Phase timing {suffix}')
    return make_party(brand, PartyID(f'phase-timing-{suffix}'), 'Phase timing')


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'PhaseTiming{suffix}P{i}') for i in range(12)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'PhaseTimingAdmin{str(generate_uuid7())[:18]}')


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(START)
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


@pytest.fixture
def reconciles(monkeypatch):
    """Record every reconcile as `(tournament_id, occurred_at)`."""
    calls = []
    real = operational.reconcile_due_matches_flush

    def spy(tournament_id, *, occurred_at):
        calls.append((tournament_id, occurred_at))
        return real(tournament_id, occurred_at=occurred_at)

    monkeypatch.setattr(operational, 'reconcile_due_matches_flush', spy)
    return calls


def _add_participants(tournament, users, created_at=SCHEDULED):
    for user in users:
        repo.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=created_at,
            )
        )
    db.session.commit()


def _start(tournament, admin, clock):
    """Start the tournament at `STARTED_AT`, then move well past it."""
    clock.now = STARTED_AT
    result = tournament_service.change_status(tournament.id, ONGOING, admin.id)
    assert result.is_ok(), result.unwrap_err()
    clock.now = START + timedelta(hours=2)


def _cleanup(created):
    db.session.rollback()
    for tournament in created:
        if repo.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def make_groups(party, players, admin, clock):
    """Return a factory for a started group stage with a 1v1 playoff."""
    created = []

    def make(*, release=MANUAL):
        result = tournament_service.create_tournament(
            party.id,
            f'Phase timing groups {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=RR,
            tournament_status=CLOSED,
            start_time=SCHEDULED,
            max_players=16,
            playoff_game_format=GameFormat.ONE_V_ONE,
            playoff_elimination_mode=SE,
            playoff_group_count=2,
            playoff_qualifiers_per_group=2,
            playoff_release_mode=release,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _add_participants(tournament, players[:8])
        generated = matches.generate_round_robin_bracket(tournament.id)
        assert generated.is_ok(), generated.unwrap_err()
        _start(tournament, admin, clock)
        return tournament

    yield make
    _cleanup(created)


@pytest.fixture
def make_highscore(party, players, admin, clock):
    """Return a factory for a started leaderboard with an FFA playoff."""
    created = []

    def make():
        result = tournament_service.create_tournament(
            party.id,
            f'Phase timing highscore {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.HIGHSCORE,
            elimination_mode=EliminationMode.NONE,
            score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
            tournament_status=CLOSED,
            start_time=SCHEDULED,
            max_players=16,
            playoff_game_format=GameFormat.FREE_FOR_ALL,
            playoff_elimination_mode=SE,
            playoff_qualifier_count=4,
            playoff_release_mode=MANUAL,
            point_table=[5, 3, 2, 1],
            group_size_min=3,
            group_size_max=4,
            advancement_count=2,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _add_participants(tournament, players[:5])
        _start(tournament, admin, clock)
        return tournament

    yield make
    _cleanup(created)


@pytest.fixture
def make_ffa(party, players, admin, clock):
    """Return a factory for a started single-track FFA, round 0 drafted."""
    created = []

    def make(*, size=12, maximum=4, cut=2):
        result = tournament_service.create_tournament(
            party.id,
            f'Phase timing FFA {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=SE,
            tournament_status=CLOSED,
            start_time=SCHEDULED,
            max_players=16,
            point_table=[5, 3, 2, 1],
            group_size_min=2,
            group_size_max=maximum,
            advancement_count=cut,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _add_participants(tournament, players[:size])
        board = seeding.get_board(tournament.id, initiator_id=admin.id)
        seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.unwrap().version,
            initiator_id=admin.id,
        ).unwrap()
        _start(tournament, admin, clock)
        return tournament

    yield make
    _cleanup(created)


# -- reads: only committed data, through a separate connection --


def _committed(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _facts(tournament) -> tuple[list, list]:
    """Return the committed matches and episodes, to compare for equality."""
    found = _committed(
        'SELECT id, phase, last_changed_at, occupied_since, confirmed_by'
        ' FROM lan_tournament_matches WHERE tournament_id = :id ORDER BY id',
        id=tournament.id,
    )
    episodes = _committed(
        'SELECT id, match_id, pairing_key, opened_at, opened_clock_us,'
        ' closed_at, closed_clock_us, ack_revision'
        ' FROM lan_tournament_match_due_episodes'
        ' WHERE tournament_id = :id ORDER BY id',
        id=tournament.id,
    )
    return [tuple(r) for r in found], [tuple(r) for r in episodes]


def _episodes(tournament) -> list[DbMatchDueEpisode]:
    db.session.rollback()
    return list(
        db.session.scalars(
            select(DbMatchDueEpisode)
            .where(DbMatchDueEpisode.tournament_id == tournament.id)
            .order_by(DbMatchDueEpisode.opened_at, DbMatchDueEpisode.id)
            .execution_options(populate_existing=True)
        )
    )


def _open(tournament) -> dict[UUID, DbMatchDueEpisode]:
    found = [e for e in _episodes(tournament) if e.closed_at is None]
    assert len({e.match_id for e in found}) == len(found)
    return {e.match_id: e for e in found}


def _phase_two(tournament):
    db.session.rollback()
    return [
        m
        for m in repo.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _occupancy(tournament) -> dict[UUID, datetime | None]:
    return {
        row[0]: row[1]
        for row in _committed(
            'SELECT id, occupied_since FROM lan_tournament_matches'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _tournament(tournament):
    db.session.rollback()
    return repo.get_tournament(tournament.id, fresh=True)


def _release_state(tournament):
    (row,) = _committed(
        'SELECT playoff_released_at FROM lan_tournaments WHERE id = :id',
        id=tournament.id,
    )
    return row[0]


def _log_count(tournament, event_type: str) -> int:
    (row,) = _committed(
        'SELECT count(*) FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id AND event_type = :event',
        id=tournament.id,
        event=event_type,
    )
    return row[0]


def _annotations(tournament) -> set[UUID]:
    return {
        row[0]
        for row in _committed(
            'SELECT match_id FROM lan_tournament_match_dashboard_annotations'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _acks(tournament) -> dict[UUID, list]:
    found: dict[UUID, list] = {}
    for row in _committed(
        'SELECT id, episode_id, revision FROM'
        ' lan_tournament_match_escalation_acks WHERE tournament_id = :id',
        id=tournament.id,
    ):
        found.setdefault(row[1], []).append((row[0], row[2]))
    return found


# -- the group stage and its playoff --


def _groups(tournament):
    """Return the matches of each group, with their contestant IDs."""
    db.session.rollback()
    found = repo.get_matches_for_tournament(tournament.id)
    contestants = repo.get_contestants_for_tournament(tournament.id)
    groups: dict[int, list] = {}
    for match in found:
        if match.phase != 1:
            continue
        ids = [c.participant_id for c in contestants[match.id]]
        groups.setdefault(match.group_order, []).append((match, ids))
    return groups


def _confirm_group_match(match, ids, scores, admin):
    result = matches.admin_set_and_confirm_match(
        match.id, admin.id, dict(zip(ids, scores, strict=True))
    )
    assert result.is_ok(), result.unwrap_err()


def _play_groups(tournament, admin, *, skip_last=False):
    """Play every group match; the lower ID always wins.

    The margin is wide for the winner of group 0 and narrow elsewhere, so
    the standings never tie across the groups. With `skip_last`, return
    the match left open.
    """
    plan = []
    for group, group_matches in _groups(tournament).items():
        members = sorted({i for _, ids in group_matches for i in ids}, key=str)
        for match, ids in group_matches:
            low, high = sorted(ids, key=str)
            margin = 10 if group == 0 and members.index(low) == 0 else 1
            scores = (margin, 0) if ids[0] == low else (0, margin)
            plan.append((match, ids, scores))
    open_match = plan.pop() if skip_last else None
    for match, ids, scores in plan:
        _confirm_group_match(match, ids, scores, admin)
    return open_match


def _release(tournament, admin):
    board = seeding.ensure_playoff_draft(tournament.id).unwrap()
    return qualification.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )


def _acknowledge(episode: DbMatchDueEpisode, actor_id, at: datetime) -> None:
    """Record one acknowledgement, as the acknowledgement service will."""
    repo.create_escalation_ack_flush(
        MatchEscalationAcknowledgement(
            id=MatchEscalationAcknowledgementID(generate_uuid7()),
            episode_id=episode.id,
            tournament_id=episode.tournament_id,
            match_id=episode.match_id,
            revision=1,
            occurred_at=at,
            clock_us=0,
            actor_id=actor_id,
            comment='checked',
        )
    )
    db.session.execute(
        update(DbMatchDueEpisode)
        .where(DbMatchDueEpisode.id == episode.id)
        .values(ack_revision=1)
    )
    db.session.commit()


def _pin(match, tournament, actor_id, at: datetime) -> None:
    saved = repo.save_match_pin_flush(
        match.id,
        tournament.id,
        pinned_at=at,
        pinned_by=actor_id,
        updated_at=at,
        updated_by=actor_id,
        expected_revision=0,
    )
    assert saved is not None
    db.session.commit()


# -- the highscore leaderboard and its FFA playoff --


def _scored_participants(tournament, users) -> list[str]:
    by_user = {
        p.user_id: str(p.id)
        for p in repo.get_participants_for_tournament(tournament.id)
    }
    return [by_user[u.id] for u in users if u.id in by_user]


def _close_leaderboard(tournament, users, admin):
    for participant_id, score in zip(
        _scored_participants(tournament, users),
        [90, 80, 70, 60, 50],
        strict=True,
    ):
        result = tournament_score_service.submit_score(
            tournament.id,
            score,
            participant_id=TournamentParticipantID(participant_id),
        )
        assert result.is_ok(), result.unwrap_err()
    closed = tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    )
    assert closed.is_ok(), closed.unwrap_err()


# -- a single-track FFA --


def _round(tournament, round_number):
    db.session.rollback()
    return sorted(
        repo.get_matches_for_round(tournament.id, round_number, bracket=None),
        key=lambda m: m.group_order or 0,
    )


def _members(match) -> list[str]:
    db.session.rollback()
    return sorted(
        str(c.participant_id)
        for c in matches.get_contestants_for_match(match.id)
    )


def _play(match, admin) -> str:
    """Place the lobby by contestant ID and confirm it; return the winner."""
    order = _members(match)
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()
    return order[0]


def _play_round(tournament, admin, round_number):
    for lobby in _round(tournament, round_number):
        _play(lobby, admin)


def _draft(tournament, admin) -> tuple[str, int]:
    target = seeding.prepare_ffa_round_draft(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    return target, seeding.get_board(tournament.id, target).unwrap().version


def _generate(tournament, admin, target, version):
    return seeding.generate_from_seeding(
        tournament.id, target, expected_version=version, initiator_id=admin.id
    )


def _baseline_us(at: datetime) -> int:
    """Return the active time of a tournament that ran since the start."""
    return int((at - STARTED_AT) / timedelta(microseconds=1))


# -- the four plan tests --


# fmt: off
@pytest.mark.parametrize('kind', ['ffa_round', 'playoff'])
# fmt: on
def test_draft_only_and_unchanged_generation_do_not_touch_matches(
    kind, make_ffa, make_groups, admin, clock, reconciles
):
    if kind == 'ffa_round':
        tournament = make_ffa()
        _play_round(tournament, admin, 0)
        target, version = _draft(tournament, admin)

        def keep():
            return seeding.prepare_ffa_round_draft(
                tournament.id, initiator_id=admin.id
            )

        def generate(version):
            return _generate(tournament, admin, target, version)

    else:
        tournament = make_groups(release=MANUAL)
        _play_groups(tournament, admin)
        board = seeding.ensure_playoff_draft(tournament.id).unwrap()
        target, version = seeding.PLAYOFF_TARGET, board.version

        def keep():
            return seeding.ensure_playoff_draft(tournament.id)

        def generate(version):
            return qualification.release_playoffs(
                tournament.id, expected_version=version, initiator_id=admin.id
            )

    def swap(version):
        return seeding.apply_action(
            tournament.id,
            target,
            seeding.Swap(0, 3),
            expected_version=version,
            initiator_id=admin.id,
        )

    def untouched(facts):
        assert _facts(tournament) == facts
        assert clock.readings == readings
        assert len(reconciles) == reconciled

    # A draft that is kept or edited writes no match and no episode.
    before = _facts(tournament)
    readings, reconciled = clock.readings, len(reconciles)
    assert keep().is_ok()
    edited = swap(version)
    assert edited.is_ok(), edited.unwrap_err()
    untouched(before)

    generated = generate(edited.unwrap().version)
    assert generated.is_ok(), generated.unwrap_err()
    assert len(_open(tournament)) == 2
    after_generation = _facts(tournament)
    version = seeding.get_board(tournament.id, target).unwrap().version
    readings, reconciled = clock.readings, len(reconciles)

    # The code the matches already follow generates nothing.
    if kind == 'ffa_round':
        again = generate(version)
    else:
        again = seeding.generate_from_seeding(
            tournament.id,
            target,
            expected_version=version,
            initiator_id=admin.id,
        )
    assert again == Ok(seeding.GENERATION_UNCHANGED)
    untouched(after_generation)

    # Editing the draft afterwards leaves the generated matches as they are.
    edited = swap(version)
    assert edited.is_ok(), edited.unwrap_err()
    untouched(after_generation)


# fmt: off
@pytest.mark.parametrize('kind', ['one_v_one', 'free_for_all'])
# fmt: on
def test_release_uses_effective_format_clock(
    kind, make_groups, make_highscore, players, admin, clock, reconciles
):
    if kind == 'one_v_one':
        tournament = make_groups(release=MANUAL)
        _play_groups(tournament, admin)
    else:
        tournament = make_highscore()
        _close_leaderboard(tournament, players, admin)
    assert _open(tournament) == {}
    clock.now = RELEASE_AT
    reconciles.clear()

    released = _release(tournament, admin)

    assert released.is_ok(), released.unwrap_err()
    # One operation time, the first reading of the release.
    assert [at for _, at in reconciles] == [RELEASE_AT]
    phase_two = _phase_two(tournament)
    opened = _open(tournament)
    assert all(m.phase == 2 for m in phase_two)
    if kind == 'one_v_one':
        # Four qualifiers meet in two first-round matches; the final waits.
        first_round = {m.id for m in phase_two if m.round == 0}
        assert len(phase_two) == 4 and len(first_round) == 2
        assert set(opened) == first_round
    else:
        # Five contestants, four qualify: one lobby of four.
        (lobby,) = phase_two
        assert set(opened) == {lobby.id}
        assert _occupancy(tournament)[lobby.id] == RELEASE_AT
    # The wait starts now, on the clock of the running tournament.
    assert {e.opened_at for e in opened.values()} == {RELEASE_AT}
    assert {e.opened_clock_us for e in opened.values()} == {
        _baseline_us(RELEASE_AT)
    }
    assert _baseline_us(RELEASE_AT) == 4 * HOUR_US
    assert {e.tournament_id for e in opened.values()} == {tournament.id}
    # Nothing of the finished first phase is due again.
    assert all(
        e.closed_at is not None
        for e in _episodes(tournament)
        if e.match_id not in opened
    )


def test_unrelease_retains_history_and_removes_live_annotations(
    make_groups, admin, clock, reconciles
):
    tournament = make_groups(release=MANUAL)
    _play_groups(tournament, admin)
    first_phase = next(iter(_groups(tournament).values()))[0][0]
    clock.now = RELEASE_AT
    assert _release(tournament, admin).is_ok()
    old = _open(tournament)
    old_matches = {m.id for m in _phase_two(tournament)}
    assert len(old) == 2
    shown, hidden = sorted(old.values(), key=lambda e: str(e.id))
    _acknowledge(shown, admin.id, RELEASE_AT + timedelta(minutes=20))
    for match_id in sorted(old, key=str)[:1]:
        _pin(repo.get_match(match_id), tournament, admin.id, RELEASE_AT)
    _pin(first_phase, tournament, admin.id, RELEASE_AT)
    assert _annotations(tournament) == {
        first_phase.id,
        *sorted(old, key=str)[:1],
    }
    clock.now = UNRELEASE_AT
    reconciles.clear()

    result = qualification.unrelease_playoffs(
        tournament.id, reason='Reset the playoffs', initiator_id=admin.id
    )

    assert result.is_ok(), result.unwrap_err()
    assert [at for _, at in reconciles] == [UNRELEASE_AT]
    assert _phase_two(tournament) == []
    assert _release_state(tournament) is None
    # The history stays: closed at the clock of the un-release.
    by_id = {e.id: e for e in _episodes(tournament)}
    for episode in old.values():
        kept = by_id[episode.id]
        assert kept.match_id == episode.match_id
        assert kept.closed_at == UNRELEASE_AT
        assert kept.closed_clock_us == _baseline_us(UNRELEASE_AT)
    assert by_id[shown.id].ack_revision == 1
    assert by_id[hidden.id].ack_revision == 0
    assert [e for e in by_id.values() if e.closed_at is None] == []
    acks = _acks(tournament)
    assert [revision for _, revision in acks[shown.id]] == [1]
    # The live pin of a deleted match goes, the first phase keeps its own.
    assert _annotations(tournament) == {first_phase.id}

    # The same pairing again is a new match with a new, unacknowledged wait.
    clock.now = RERELEASE_AT
    assert _release(tournament, admin).is_ok()
    new = _open(tournament)
    assert len(new) == 2
    assert not old_matches & set(new)
    assert not {e.id for e in old.values()} & {e.id for e in new.values()}
    assert {e.ack_revision for e in new.values()} == {0}
    assert {e.opened_at for e in new.values()} == {RERELEASE_AT}
    assert not set(_acks(tournament)) & {e.id for e in new.values()}
    assert [r for _, r in _acks(tournament)[shown.id]] == [1]
    assert _annotations(tournament) == {first_phase.id}


def _boom(*args, **kwargs):
    return Err('boom')


def _late_failure(*args, **kwargs):
    raise RuntimeError('late failure')


# fmt: off
@pytest.mark.parametrize('case', [
    'release_reconcile_fails',
    'release_fails_after_timing',
    'ffa_round_reconcile_fails',
    'unrelease_reconcile_fails',
])
# fmt: on
def test_phase_failure_rolls_back_timing_with_generation(
    case, make_groups, make_ffa, admin, clock, monkeypatch
):
    if case == 'ffa_round_reconcile_fails':
        tournament = make_ffa()
        _play_round(tournament, admin, 0)
        target, version = _draft(tournament, admin)
        before = _facts(tournament)
        monkeypatch.setattr(operational, 'reconcile_due_matches_flush', _boom)

        result = _generate(tournament, admin, target, version)

        assert result == Err('boom')
        assert _facts(tournament) == before
        (row,) = _committed(
            'SELECT generated_seed_code, version FROM'
            ' lan_tournament_seedings WHERE tournament_id = :id'
            ' AND target = :target',
            id=tournament.id,
            target=target,
        )
        assert row[0] is None and row[1] == version
        assert _round(tournament, 1) == []
        monkeypatch.undo()
        retried = _generate(tournament, admin, target, version)
        assert retried.is_ok(), retried.unwrap_err()
        assert len(_open(tournament)) == 2
        return

    tournament = make_groups(release=MANUAL)
    _play_groups(tournament, admin)
    clock.now = RELEASE_AT

    if case == 'unrelease_reconcile_fails':
        assert _release(tournament, admin).is_ok()
        before = _facts(tournament)
        pinned = next(iter(_open(tournament)))
        _pin(repo.get_match(pinned), tournament, admin.id, RELEASE_AT)
        monkeypatch.setattr(operational, 'reconcile_due_matches_flush', _boom)

        result = qualification.unrelease_playoffs(
            tournament.id, reason='Reset the playoffs', initiator_id=admin.id
        )

        assert result == Err('boom')
        assert _facts(tournament) == before
        assert _release_state(tournament) is not None
        assert _annotations(tournament) == {pinned}
        assert _log_count(tournament, 'playoffs-unreleased') == 0
        assert len(_open(tournament)) == 2
        monkeypatch.undo()
        retried = qualification.unrelease_playoffs(
            tournament.id, reason='Reset the playoffs', initiator_id=admin.id
        )
        assert retried.is_ok(), retried.unwrap_err()
        assert _annotations(tournament) == set()
        assert _open(tournament) == {}
        return

    board = seeding.ensure_playoff_draft(tournament.id).unwrap()
    before = _facts(tournament)
    log_entries = _log_count(tournament, 'bracket-generated')
    if case == 'release_reconcile_fails':
        monkeypatch.setattr(operational, 'reconcile_due_matches_flush', _boom)
    else:
        # The timing is flushed by then: the failure must take it with it.
        monkeypatch.setattr(repo, 'set_playoff_release', _late_failure)

    def release():
        return qualification.release_playoffs(
            tournament.id,
            expected_version=board.version,
            initiator_id=admin.id,
        )

    if case == 'release_reconcile_fails':
        assert release() == Err('boom')
    else:
        with pytest.raises(RuntimeError, match='late failure'):
            release()

    assert _facts(tournament) == before
    assert _release_state(tournament) is None
    assert _phase_two(tournament) == []
    assert _log_count(tournament, 'bracket-generated') == log_entries
    (row,) = _committed(
        'SELECT generated_seed_code FROM lan_tournament_seedings'
        ' WHERE tournament_id = :id AND target = :target',
        id=tournament.id,
        target=seeding.PLAYOFF_TARGET,
    )
    assert row[0] is None
    monkeypatch.undo()
    clock.now = RERELEASE_AT
    retried = release()
    assert retried.is_ok(), retried.unwrap_err()
    assert len(_open(tournament)) == 2


# -- the seeding owner for a tournament that already runs --


def test_ffa_round_from_the_draft_opens_its_episodes_at_generation(
    make_ffa, admin, clock, reconciles
):
    tournament = make_ffa()
    _play_round(tournament, admin, 0)
    target, version = _draft(tournament, admin)
    assert _open(tournament) == {}
    clock.now = RELEASE_AT
    reconciles.clear()

    generated = _generate(tournament, admin, target, version)

    assert generated == Ok(2)
    assert [at for _, at in reconciles] == [RELEASE_AT]
    lobbies = _round(tournament, 1)
    opened = _open(tournament)
    assert set(opened) == {m.id for m in lobbies}
    assert {e.opened_at for e in opened.values()} == {RELEASE_AT}
    assert {e.opened_clock_us for e in opened.values()} == {
        _baseline_us(RELEASE_AT)
    }
    # The lobbies are occupied and stamped at that same time.
    assert {_occupancy(tournament)[m.id] for m in lobbies} == {RELEASE_AT}
    assert {m.last_changed_at for m in lobbies} == {RELEASE_AT}


def test_regeneration_replaces_the_episodes_of_the_old_round(
    make_ffa, admin, clock, reconciles
):
    tournament = make_ffa()
    _play_round(tournament, admin, 0)
    target, version = _draft(tournament, admin)
    assert _generate(tournament, admin, target, version).is_ok()
    old = _open(tournament)
    board = seeding.get_board(tournament.id, target).unwrap()
    edited = seeding.apply_action(
        tournament.id,
        target,
        seeding.Swap(0, 5),
        expected_version=board.version,
        initiator_id=admin.id,
    ).unwrap()
    clock.now = UNRELEASE_AT
    reconciles.clear()

    regenerated = _generate(tournament, admin, target, edited.version)

    assert regenerated == Ok(2)
    assert [at for _, at in reconciles] == [UNRELEASE_AT]
    new = _open(tournament)
    assert len(new) == 2 and not set(old) & set(new)
    assert {e.opened_at for e in new.values()} == {UNRELEASE_AT}
    # The replaced lobbies keep their wait as closed history.
    by_id = {e.id: e for e in _episodes(tournament)}
    for episode in old.values():
        assert by_id[episode.id].closed_at == UNRELEASE_AT
        assert by_id[episode.id].closed_clock_us == _baseline_us(UNRELEASE_AT)


# fmt: off
@pytest.mark.parametrize('entry', ['draft', 'generate'])
# fmt: on
def test_single_survivor_completion_closes_the_demand_at_the_freeze(
    entry, make_ffa, admin, clock, reconciles
):
    tournament = make_ffa(size=8, cut=1)
    first, second = _round(tournament, 0)
    _play(first, admin)
    _play(second, admin)
    target = version = None
    if entry == 'generate':
        target, version = _draft(tournament, admin)
    for cid in _members(second):
        tournament_participant_service.admin_remove_participant(
            tournament.id, TournamentParticipantID(UUID(cid)), initiator=admin
        ).unwrap()
    # A demand the completion has to close, left behind on a played lobby.
    repo.open_due_episode_flush(
        MatchDueEpisode(
            id=MatchDueEpisodeID(generate_uuid7()),
            tournament_id=tournament.id,
            match_id=first.id,
            pairing_key='stale',
            opened_at=START + timedelta(hours=3),
            opened_clock_us=0,
        )
    )
    db.session.commit()
    clock.now = RELEASE_AT
    reconciles.clear()

    if entry == 'draft':
        result = seeding.prepare_ffa_round_draft(
            tournament.id, initiator_id=admin.id
        )
    else:
        result = _generate(tournament, admin, target, version)

    assert result == Ok('completed')
    ((_, frozen_at),) = reconciles
    assert frozen_at == RELEASE_AT
    found = _tournament(tournament)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert found.operational_clock_running_since is None
    assert found.operational_clock_elapsed_us == _baseline_us(RELEASE_AT)
    (episode,) = [
        e for e in _episodes(tournament) if e.pairing_key == 'stale'
    ]
    assert episode.closed_at == RELEASE_AT
    assert episode.closed_clock_us == _baseline_us(RELEASE_AT)
    assert _open(tournament) == {}


# -- the automatic release is a transaction of its own --


def test_automatic_release_runs_in_its_own_timing_transaction(
    make_groups, admin, clock, reconciles
):
    tournament = make_groups(release=AUTOMATIC)
    last, ids, scores = _play_groups(tournament, admin, skip_last=True)
    assert _phase_two(tournament) == []
    reconciles.clear()
    clock.now = RELEASE_AT

    _confirm_group_match(last, ids, scores, admin)

    # The result and the release are two transactions with two times.
    (result_at, release_at) = [at for _, at in reconciles]
    assert result_at == RELEASE_AT
    assert release_at > result_at
    final_group_match = [e for e in _episodes(tournament) if e.match_id == last.id]
    assert [e.closed_at for e in final_group_match] == [result_at]
    phase_two = {m.id for m in _phase_two(tournament)}
    assert len(phase_two) == 4
    opened = _open(tournament)
    assert len(opened) == 2 and set(opened) <= phase_two
    assert {e.opened_at for e in opened.values()} == {release_at}
    assert _release_state(tournament) is not None
