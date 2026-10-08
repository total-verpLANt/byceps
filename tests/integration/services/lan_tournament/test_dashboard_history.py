from datetime import datetime, timedelta, UTC
from uuid import UUID

from click.testing import CliRunner
import pytest
from sqlalchemy import text, update

from byceps.cli.commands.purge_lan_tournament_log_entries import (
    purge_lan_tournament_log_entries,
)
from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as matches,
    tournament_qualification_service as qualification,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.dashboard import DbMatchDueEpisode
from byceps.services.lan_tournament.dbmodels.tournament_log_entry import (
    DbTournamentLogEntry,
)
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.operational_timing import (
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
)
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


START = datetime(2031, 5, 6, 18, 0, 0)
SCHEDULED = datetime(2031, 5, 6, 15, 0, 0)
STARTED_AT = START + timedelta(hours=1)
RELEASE_AT = START + timedelta(hours=5)
REGENERATE_AT = START + timedelta(hours=6)
RERELEASE_AT = START + timedelta(hours=7)

CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING
SE = EliminationMode.SINGLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN


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
    brand = make_brand(f'dashhistory{suffix}', f'Dashboard history {suffix}')
    return make_party(brand, PartyID(f'dash-history-{suffix}'), 'History')


@pytest.fixture(scope='module')
def players(make_user):
    suffix = str(generate_uuid7())[:18]
    return [make_user(f'DashHistory{suffix}P{i}') for i in range(12)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user(f'DashHistoryAdmin{str(generate_uuid7())[:18]}')


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(START)
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


def _add_participants(tournament, users):
    for user in users:
        repo.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=SCHEDULED,
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
def make_ffa(party, players, admin, clock):
    """Return a factory for a started single-track FFA with round 0 drafted."""
    created = []

    def make():
        result = tournament_service.create_tournament(
            party.id,
            f'Dashboard history FFA {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=SE,
            tournament_status=CLOSED,
            start_time=SCHEDULED,
            max_players=16,
            point_table=[5, 3, 2, 1],
            group_size_min=2,
            group_size_max=4,
            advancement_count=2,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        _add_participants(tournament, players)
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


@pytest.fixture
def make_groups(party, players, admin, clock):
    """Return a factory for a started group stage with a 1v1 playoff."""
    created = []

    def make():
        result = tournament_service.create_tournament(
            party.id,
            f'Dashboard history groups {generate_uuid7()}',
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
            playoff_release_mode=PlayoffReleaseMode.MANUAL,
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


# -- reads: only committed data, through a separate connection --


def _committed(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _episodes(tournament) -> dict[UUID, tuple]:
    """Return id -> (match_id, pairing_key, opened_at, closed_at, closed_us, revision)."""
    return {
        row[0]: tuple(row[1:])
        for row in _committed(
            'SELECT id, match_id, pairing_key, opened_at, closed_at,'
            ' closed_clock_us, ack_revision'
            ' FROM lan_tournament_match_due_episodes'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _open(tournament) -> dict[UUID, UUID]:
    """Return match_id -> episode_id of the open episodes."""
    found = {
        row[1]: row[0]
        for row in _committed(
            'SELECT id, match_id FROM lan_tournament_match_due_episodes'
            ' WHERE tournament_id = :id AND closed_at IS NULL',
            id=tournament.id,
        )
    }
    return found


def _acks(tournament) -> set[tuple[UUID, UUID, int]]:
    return {
        (row[0], row[1], row[2])
        for row in _committed(
            'SELECT id, episode_id, revision'
            ' FROM lan_tournament_match_escalation_acks'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _pins(tournament) -> set[UUID]:
    return {
        row[0]
        for row in _committed(
            'SELECT match_id FROM lan_tournament_match_dashboard_annotations'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _match_ids(tournament) -> set[UUID]:
    return {
        row[0]
        for row in _committed(
            'SELECT id FROM lan_tournament_matches WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _orphan_pins(tournament) -> set[UUID]:
    return {
        row[0]
        for row in _committed(
            'SELECT a.match_id FROM lan_tournament_match_dashboard_annotations a'
            ' WHERE a.tournament_id = :id AND NOT EXISTS'
            ' (SELECT 1 FROM lan_tournament_matches m WHERE m.id = a.match_id)',
            id=tournament.id,
        )
    }


def _log_ids(tournament) -> set[UUID]:
    return {
        row[0]
        for row in _committed(
            'SELECT id FROM lan_tournament_log_entries'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


def _baseline_us(at: datetime) -> int:
    """Return the active time of a tournament that ran since the start."""
    return int((at - STARTED_AT) / timedelta(microseconds=1))


# -- writes of the coordination tables, as their services do them --


def _pin(match_id, tournament, actor_id, at: datetime) -> None:
    saved = repo.save_match_pin_flush(
        match_id,
        tournament.id,
        pinned_at=at,
        pinned_by=actor_id,
        updated_at=at,
        updated_by=actor_id,
        expected_revision=0,
    )
    assert saved is not None
    db.session.commit()


def _acknowledge(episode_id, tournament, match_id, actor_id, at) -> None:
    repo.create_escalation_ack_flush(
        MatchEscalationAcknowledgement(
            id=MatchEscalationAcknowledgementID(generate_uuid7()),
            episode_id=episode_id,
            tournament_id=tournament.id,
            match_id=match_id,
            revision=1,
            occurred_at=at,
            clock_us=0,
            actor_id=actor_id,
            comment='checked',
        )
    )
    db.session.execute(
        update(DbMatchDueEpisode)
        .where(DbMatchDueEpisode.id == episode_id)
        .values(ack_revision=1)
    )
    db.session.commit()


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


def _play(match, admin) -> None:
    """Place the lobby by contestant ID and confirm it."""
    order = _members(match)
    matches.set_ffa_placements(
        match.id, {cid: i + 1 for i, cid in enumerate(order)}
    ).unwrap()
    matches.confirm_ffa_match(match.id, admin.id).unwrap()


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


def _swap(tournament, admin, target, version, a=0, b=3):
    edited = seeding.apply_action(
        tournament.id,
        target,
        seeding.Swap(a, b),
        expected_version=version,
        initiator_id=admin.id,
    )
    assert edited.is_ok(), edited.unwrap_err()
    return edited.unwrap().version


# -- the group stage and its playoff --


def _groups(tournament):
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


def _play_groups(tournament, admin):
    """Play every group match; the lower ID always wins."""
    for group, group_matches in _groups(tournament).items():
        members = sorted({i for _, ids in group_matches for i in ids}, key=str)
        for match, ids in group_matches:
            low, high = sorted(ids, key=str)
            margin = 10 if group == 0 and members.index(low) == 0 else 1
            scores = (margin, 0) if ids[0] == low else (0, margin)
            result = matches.admin_set_and_confirm_match(
                match.id, admin.id, dict(zip(ids, scores, strict=True))
            )
            assert result.is_ok(), result.unwrap_err()


def _release(tournament, admin):
    board = seeding.ensure_playoff_draft(tournament.id).unwrap()
    return qualification.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    )


def _phase_two(tournament):
    db.session.rollback()
    return [
        m
        for m in repo.get_matches_for_tournament(tournament.id)
        if m.phase == 2
    ]


def _released_playoffs(make_groups, admin, clock):
    tournament = make_groups()
    _play_groups(tournament, admin)
    first_phase = next(iter(_groups(tournament).values()))[0][0]
    clock.now = RELEASE_AT
    assert _release(tournament, admin).is_ok()
    return tournament, first_phase


def _annotate(tournament, admin, pinned, kept_match_id=None):
    """Pin the matches, acknowledge one open episode, pin the kept match."""
    open_episodes = _open(tournament)
    for match_id in sorted(pinned, key=str):
        _pin(match_id, tournament, admin.id, RELEASE_AT)
    shown = sorted(open_episodes, key=str)[0]
    _acknowledge(
        open_episodes[shown],
        tournament,
        shown,
        admin.id,
        RELEASE_AT + timedelta(minutes=20),
    )
    if kept_match_id is not None:
        _pin(kept_match_id, tournament, admin.id, RELEASE_AT)


# -- the four plan tests --


def test_history_survives_audit_purge(make_ffa, admin, clock):
    tournament = make_ffa()
    lobbies = _round(tournament, 0)
    open_before = _open(tournament)
    assert set(open_before) == {m.id for m in lobbies}
    first = lobbies[0]
    _acknowledge(
        open_before[first.id],
        tournament,
        first.id,
        admin.id,
        START + timedelta(hours=3),
    )
    for lobby in lobbies[1:]:
        _pin(lobby.id, tournament, admin.id, START + timedelta(hours=3))
    # The result closes the acknowledged wait: closed history with an ack.
    _play(first, admin)
    assert first.id not in _open(tournament)
    history = _episodes(tournament)
    acks = _acks(tournament)
    pins = _pins(tournament)
    assert len(acks) == 1
    assert pins == {m.id for m in lobbies[1:]}
    assert _log_ids(tournament)

    # Age the audit trail of this tournament past the retention, and purge
    # it with the real command, which cuts at the age it is given.
    db.session.execute(
        update(DbTournamentLogEntry)
        .where(DbTournamentLogEntry.tournament_id == tournament.id)
        .values(occurred_at=datetime.now(UTC) - timedelta(days=500))
    )
    db.session.commit()
    run = CliRunner().invoke(
        purge_lan_tournament_log_entries, ['--older-than-days', '400']
    )

    assert run.exit_code == 0, run.output
    assert not _log_ids(tournament)
    # Neither the clock history nor the acknowledgements depend on the log.
    assert _episodes(tournament) == history
    assert _acks(tournament) == acks
    assert _pins(tournament) == pins


def _world(kind, make_ffa, make_groups, admin, clock):
    """Return a tournament whose second phase or round holds annotated matches.

    The result is (tournament, regenerate, kept match, replaced match IDs,
    open episodes before): `regenerate()` replaces those matches, the kept
    match is one of the first phase or round that stays.
    """
    if kind == 'ffa_regeneration':
        tournament = make_ffa()
        _play_round(tournament, admin, 0)
        target, version = _draft(tournament, admin)
        assert _generate(tournament, admin, target, version).is_ok()
        kept = _round(tournament, 0)[0].id
        replaced = {m.id for m in _round(tournament, 1)}

        def regenerate():
            version = seeding.get_board(tournament.id, target).unwrap().version
            edited = _swap(tournament, admin, target, version, 0, 5)
            return _generate(tournament, admin, target, edited)

    else:
        tournament, first_phase = _released_playoffs(make_groups, admin, clock)
        kept = first_phase.id
        replaced = {m.id for m in _phase_two(tournament)}

        def regenerate():
            if kind == 'unrelease':
                return qualification.unrelease_playoffs(
                    tournament.id,
                    reason='Reset the playoffs',
                    initiator_id=admin.id,
                )

            board = seeding.get_board(
                tournament.id, seeding.PLAYOFF_TARGET
            ).unwrap()
            edited = _swap(
                tournament, admin, seeding.PLAYOFF_TARGET, board.version
            )
            return seeding.generate_from_seeding(
                tournament.id,
                seeding.PLAYOFF_TARGET,
                expected_version=edited,
                initiator_id=admin.id,
            )

    return tournament, regenerate, kept, replaced


# fmt: off
@pytest.mark.parametrize('kind', [
    'ffa_regeneration', 'playoff_regeneration', 'unrelease',
])
# fmt: on
def test_regeneration_and_unrelease_do_not_block_on_annotations(
    kind, make_ffa, make_groups, admin, clock
):
    tournament, regenerate, kept, replaced = _world(
        kind, make_ffa, make_groups, admin, clock
    )
    old_open = _open(tournament)
    # Only the matches that are due have a wait; all of them are pinned.
    assert old_open and set(old_open) <= replaced
    _annotate(tournament, admin, replaced, kept)
    history = _episodes(tournament)
    acks = _acks(tournament)
    assert _pins(tournament) == replaced | {kept}
    clock.now = REGENERATE_AT
    readings = clock.readings

    result = regenerate()

    assert result.is_ok(), result.unwrap_err()
    # One operation time serves the whole transaction: the owner's reading.
    assert clock.readings - readings == 1
    # The replaced matches are gone, and their live pins with them.
    assert not replaced & _match_ids(tournament)
    assert _pins(tournament) == {kept}
    assert _orphan_pins(tournament) == set()
    # Their waits stay as closed history, acknowledgements untouched.
    after = _episodes(tournament)
    assert after.keys() >= history.keys()
    for episode_id in old_open.values():
        before, now = history[episode_id], after[episode_id]
        assert now[4] is not None and now[3] is not None
        # The owner's operation time reaches the episode, exactly.
        assert now[3] == REGENERATE_AT
        assert now[4] == _baseline_us(REGENERATE_AT)
        assert (now[0], now[1], now[2], now[5]) == (
            before[0], before[1], before[2], before[5],
        )
    assert _acks(tournament) == acks
    assert not set(old_open) & set(_open(tournament))


# fmt: off
@pytest.mark.parametrize('kind', ['ffa_regeneration', 'playoff_rerelease'])
# fmt: on
def test_episode_history_not_reused_by_recreated_matches(
    kind, make_ffa, make_groups, admin, clock
):
    world = 'ffa_regeneration' if kind == 'ffa_regeneration' else 'unrelease'
    tournament, regenerate, kept, replaced = _world(
        world, make_ffa, make_groups, admin, clock
    )
    old_open = _open(tournament)
    _annotate(tournament, admin, replaced, kept)
    history = _episodes(tournament)
    acks = _acks(tournament)
    old_keys = {history[e][1] for e in old_open.values()}
    clock.now = REGENERATE_AT
    readings = clock.readings

    assert regenerate().is_ok()
    assert clock.readings - readings == 1
    if kind == 'playoff_rerelease':
        # The same pairing comes back, as new matches, in one operation.
        clock.now = RERELEASE_AT
        readings = clock.readings
        assert _release(tournament, admin).is_ok()
        assert clock.readings - readings == 1

    new_open = _open(tournament)
    created = _match_ids(tournament) - {kept}
    assert new_open and set(new_open) <= created
    assert not set(new_open) & replaced
    assert not set(new_open.values()) & set(history)
    current = _episodes(tournament)
    for match_id, episode_id in new_open.items():
        assert current[episode_id][0] == match_id
        assert current[episode_id][5] == 0
    if kind == 'playoff_rerelease':
        assert {current[e][1] for e in new_open.values()} == old_keys
    # No acknowledgement or pin of the old matches reaches a new one.
    assert _acks(tournament) == acks
    assert not {
        episode_id for _, episode_id, _ in _acks(tournament)
    } & set(new_open.values())
    assert _pins(tournament) == {kept}
    # The old episodes are untouched history.
    for episode_id in old_open.values():
        assert current[episode_id][0] in replaced
        assert current[episode_id][3] is not None


def test_tournament_deletion_retains_snapshot_history(make_ffa, admin, clock):
    tournament = make_ffa()
    lobbies = _round(tournament, 0)
    _annotate(tournament, admin, {m.id for m in lobbies})
    history = _episodes(tournament)
    acks = _acks(tournament)
    assert _pins(tournament) == {m.id for m in lobbies}
    deleted_at = START + timedelta(hours=8)
    clock.now = deleted_at

    tournament_service.delete_tournament(tournament.id, admin.id)

    assert repo.find_tournament(tournament.id) is None
    assert _match_ids(tournament) == set()
    # The live pins are gone, and nothing is left open.
    assert _pins(tournament) == set()
    assert _open(tournament) == {}
    # Every wait stays, closed at the tournament clock of the deletion.
    after = _episodes(tournament)
    assert after.keys() == history.keys()
    for episode_id, row in after.items():
        assert row[3] == deleted_at
        assert row[4] == _baseline_us(deleted_at)
        assert (row[0], row[1], row[2], row[5]) == (
            history[episode_id][0], history[episode_id][1],
            history[episode_id][2], history[episode_id][5],
        )
    assert _acks(tournament) == acks
    # Nothing in the history points at a live match or tournament row.
    constraints = _committed(
        "SELECT conrelid::regclass::text, conname, confrelid::regclass::text"
        " FROM pg_constraint WHERE contype = 'f' AND conrelid::regclass::text"
        " IN ('lan_tournament_match_due_episodes',"
        " 'lan_tournament_match_escalation_acks',"
        " 'lan_tournament_match_dashboard_annotations')"
    )
    assert [tuple(row) for row in constraints] == [
        (
            'lan_tournament_match_escalation_acks',
            'fk_lan_tournament_escalation_acks_episode_id',
            'lan_tournament_match_due_episodes',
        )
    ]

    # The audit trail outlives the tournament, and so does the history
    # when the purge removes it.
    assert _log_ids(tournament)
    db.session.execute(
        update(DbTournamentLogEntry)
        .where(DbTournamentLogEntry.tournament_id == tournament.id)
        .values(occurred_at=datetime.now(UTC) - timedelta(days=500))
    )
    db.session.commit()
    run = CliRunner().invoke(
        purge_lan_tournament_log_entries, ['--older-than-days', '400']
    )
    assert run.exit_code == 0, run.output
    assert not _log_ids(tournament)
    assert _episodes(tournament) == after
    assert _acks(tournament) == acks
