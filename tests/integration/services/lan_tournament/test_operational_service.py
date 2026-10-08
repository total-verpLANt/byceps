"""
tests.integration.services.lan_tournament.test_operational_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, UTC

from blinker import Signal
import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import OperationalError

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_operational_service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.dashboard import (
    DbMatchDueEpisode,
    DbMatchEscalationAck,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
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
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    pairing_key,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


service = tournament_operational_service

PARTY_ID = PartyID('f03-operational-service')

NOW = datetime(2026, 10, 7, 12, 0)
MINUTE = timedelta(minutes=1)
MINUTE_US = 60 * 1_000_000
PLUS_TWO = timezone(timedelta(hours=2))

ONE_V_ONE = GameFormat.ONE_V_ONE
FREE_FOR_ALL = GameFormat.FREE_FOR_ALL


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 operational service')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03OperationalService{i}') for i in range(4)]


@pytest.fixture(scope='module')
def confirmer(users):
    return users[0].id


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def make_tournament(party, users):
    """Create a committed tournament with participants for every user."""

    def _make(
        *,
        status: TournamentStatus = TournamentStatus.ONGOING,
        game_format: GameFormat = ONE_V_ONE,
        known_clock: bool = True,
        elapsed_us: int = 0,
        **fields,
    ) -> TournamentID:
        tournament_id = TournamentID(uuid7())
        running = known_clock and status is TournamentStatus.ONGOING
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=party.id,
                name=f'Operational service {tournament_id}',
                game=None,
                description=None,
                image_url=None,
                ruleset=None,
                start_time=None,
                created_at=NOW,
                min_players=None,
                max_players=None,
                min_teams=None,
                max_teams=None,
                min_players_in_team=None,
                max_players_in_team=None,
                contestant_type=None,
                tournament_status=status,
                game_format=game_format,
                elimination_mode=EliminationMode.SINGLE_ELIMINATION,
                operational_clock_elapsed_us=elapsed_us,
                operational_clock_running_since=NOW if running else None,
                operational_clock_activated_at=NOW if known_clock else None,
                **fields,
            )
        )
        db.session.add_all(
            DbTournamentParticipant(
                TournamentParticipantID(uuid7()), user.id, tournament_id, NOW
            )
            for user in users
        )
        db.session.commit()
        return tournament_id

    return _make


def _participant_ids(tournament_id, count=4):
    rows = db.session.scalars(
        select(DbTournamentParticipant.id)
        .where(DbTournamentParticipant.tournament_id == tournament_id)
        .order_by(DbTournamentParticipant.id)
    ).all()
    return list(rows[:count])


def _add_contestants(match_id, participant_ids):
    db.session.add_all(
        DbTournamentMatchToContestant(
            uuid7(), match_id, NOW, participant_id=participant_id
        )
        for participant_id in participant_ids
    )
    db.session.flush()


def _add_match(
    tournament_id,
    contestants=2,
    *,
    occupied=False,
    confirmed_by=None,
    phase=1,
) -> TournamentMatchID:
    match_id = TournamentMatchID(uuid7())
    repo.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=0,
            round=0,
            next_match_id=None,
            confirmed_by=confirmed_by,
            created_at=NOW,
            phase=phase,
        )
    )
    _add_contestants(match_id, _participant_ids(tournament_id, contestants))
    if occupied:
        db.session.execute(
            update(DbTournamentMatch)
            .where(DbTournamentMatch.id == match_id)
            .values(occupied_since=NOW)
        )
    db.session.commit()
    return match_id


def _scalar(sql: str, **params):
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _committed_episode_count(tournament_id) -> int:
    return _scalar(
        'SELECT count(*) FROM lan_tournament_match_due_episodes'
        ' WHERE tournament_id = :id',
        id=tournament_id,
    )


def _episodes(match_id) -> list[DbMatchDueEpisode]:
    db.session.expire_all()
    return list(
        db.session.scalars(
            select(DbMatchDueEpisode)
            .where(DbMatchDueEpisode.match_id == match_id)
            .order_by(DbMatchDueEpisode.opened_at, DbMatchDueEpisode.id)
            .execution_options(populate_existing=True)
        )
    )


def _acknowledge(episode: DbMatchDueEpisode, actor_id) -> None:
    """Record one acknowledgement, as the acknowledgement service will."""
    repo.create_escalation_ack_flush(
        MatchEscalationAcknowledgement(
            id=MatchEscalationAcknowledgementID(uuid7()),
            episode_id=episode.id,
            tournament_id=episode.tournament_id,
            match_id=episode.match_id,
            revision=1,
            occurred_at=NOW + 20 * MINUTE,
            clock_us=20 * MINUTE_US,
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


@contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        yield seen
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)


def _writes(statements: list[str]) -> list[str]:
    return [
        s
        for s in statements
        if s.lstrip().split(None, 1)[0].upper()
        in {'INSERT', 'UPDATE', 'DELETE'}
    ]


def _forbid_commit_and_signals(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError('a flush-only operation committed or signalled')

    monkeypatch.setattr(db.session, 'commit', refuse)
    monkeypatch.setattr(repo, 'commit_session', refuse)
    monkeypatch.setattr(Signal, 'send', refuse)


# -------------------------------------------------------------------- #
# flush-only, no read-side activation
# -------------------------------------------------------------------- #


def test_reconcile_is_flush_only_and_not_get_initialized(
    make_tournament, monkeypatch
):
    started = make_tournament()
    due = _add_match(started)
    legacy = make_tournament(known_clock=False)
    _add_match(legacy)

    _forbid_commit_and_signals(monkeypatch)
    assert service.reconcile_due_matches_flush(
        started, occurred_at=NOW + 5 * MINUTE
    ).is_ok()

    # The episode exists inside the owner's transaction, and nowhere else.
    assert [e.match_id for e in repo.list_open_due_episodes(started)] == [due]
    assert _committed_episode_count(started) == 0

    # A tournament that began before the feature gets nothing written.
    with _statements() as statements:
        assert service.reconcile_due_matches_flush(
            legacy, occurred_at=NOW + 5 * MINUTE
        ).is_ok()
    assert _writes(statements) == []
    assert repo.list_open_due_episodes(legacy) == []
    assert (
        repo.get_tournament(legacy, fresh=True).operational_clock_activated_at
        is None
    )

    # The owner rolls the whole operation back.
    repo.rollback_session()
    assert repo.list_open_due_episodes(started) == []
    assert _committed_episode_count(started) == 0
    monkeypatch.undo()

    # Reading never creates an episode, however due the match is.
    with _statements() as statements:
        repo.get_tournament(started, fresh=True)
        repo.get_matches_for_tournament(started)
        repo.get_contestants_for_tournament(started)
        repo.list_open_due_episodes(started)
    assert _writes(statements) == []
    db.session.commit()
    assert _committed_episode_count(started) == 0


# fmt: off
@pytest.mark.parametrize('sql', [
    'SELECT id FROM lan_tournaments WHERE id = :id FOR UPDATE NOWAIT',
    'SELECT id FROM lan_tournament_matches WHERE id = :id FOR UPDATE NOWAIT',
])
# fmt: on
def test_reconcile_holds_the_tournament_and_match_locks(make_tournament, sql):
    tournament_id = make_tournament()
    match_id = _add_match(tournament_id)

    assert service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    ).is_ok()

    row_id = tournament_id if 'lan_tournaments' in sql else match_id
    with db.engine.connect() as other:
        with pytest.raises(OperationalError) as refused:
            other.execute(text(sql), {'id': row_id})
    assert 'could not obtain lock' in str(refused.value)


# -------------------------------------------------------------------- #
# episode lifecycle
# -------------------------------------------------------------------- #


def test_reconcile_opens_episodes_at_the_current_clock(
    make_tournament, confirmer
):
    tournament_id = make_tournament(elapsed_us=10 * MINUTE_US)
    due = _add_match(tournament_id)
    confirmed = _add_match(tournament_id, confirmed_by=confirmer)
    _add_match(tournament_id, contestants=1)

    result = service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + 5 * MINUTE
    )

    assert result.is_ok()
    (episode,) = _episodes(due)
    assert _episodes(confirmed) == []
    assert episode.tournament_id == tournament_id
    assert episode.opened_at == NOW + 5 * MINUTE
    assert episode.opened_clock_us == 15 * MINUTE_US
    assert episode.closed_at is None
    assert episode.closed_clock_us is None
    assert episode.ack_revision == 0
    assert episode.pairing_key == pairing_key(
        repo.get_contestants_for_match(due)
    )


def test_reconcile_stores_naive_utc_for_an_aware_operation_time(
    make_tournament,
):
    tournament_id = make_tournament()
    match_id = _add_match(tournament_id)
    aware = (NOW + 5 * MINUTE).replace(tzinfo=UTC).astimezone(PLUS_TWO)
    assert aware.utcoffset() == timedelta(hours=2)

    service.reconcile_due_matches_flush(tournament_id, occurred_at=aware)

    (episode,) = _episodes(match_id)
    assert episode.opened_at == NOW + 5 * MINUTE
    assert episode.opened_clock_us == 5 * MINUTE_US


def test_noop_reconcile_keeps_episode_and_ack(make_tournament, users):
    tournament_id = make_tournament()
    match_id = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()
    (episode,) = _episodes(match_id)
    _acknowledge(episode, users[0].id)
    episode_id, opened_at = episode.id, episode.opened_at

    with _statements() as statements:
        for minutes in (20, 21, 90):
            result = service.reconcile_due_matches_flush(
                tournament_id, occurred_at=NOW + minutes * MINUTE
            )
            assert result.is_ok()
    db.session.commit()

    assert _writes(statements) == []
    (kept,) = _episodes(match_id)
    assert kept.id == episode_id
    assert kept.opened_at == opened_at
    assert kept.closed_at is None
    assert kept.ack_revision == 1
    acks = db.session.scalars(
        select(DbMatchEscalationAck).where(
            DbMatchEscalationAck.episode_id == episode_id
        )
    ).all()
    assert [a.revision for a in acks] == [1]


@pytest.mark.parametrize('invalidate', [True, False])
def test_destructive_restore_creates_new_episode(
    make_tournament, users, invalidate
):
    tournament_id = make_tournament()
    match_id = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()
    (first,) = _episodes(match_id)
    _acknowledge(first, users[0].id)
    first_id = first.id
    final_pairing = _participant_ids(tournament_id, 2)

    # A correction replaces the matchup, then restores the very same one.
    if invalidate:
        assert service.invalidate_due_matches_flush(
            [match_id], occurred_at=NOW + 30 * MINUTE
        ).is_ok()
    db.session.execute(
        delete(DbTournamentMatchToContestant).where(
            DbTournamentMatchToContestant.tournament_match_id == match_id
        )
    )
    _add_contestants(match_id, _participant_ids(tournament_id, 4)[2:])
    db.session.execute(
        delete(DbTournamentMatchToContestant).where(
            DbTournamentMatchToContestant.tournament_match_id == match_id
        )
    )
    _add_contestants(match_id, final_pairing)
    assert service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + 31 * MINUTE
    ).is_ok()
    db.session.commit()

    episodes = _episodes(match_id)
    open_ones = [e for e in episodes if e.closed_at is None]
    assert len(open_ones) == 1

    if not invalidate:
        # Equal before and after: nothing can tell the pairing was replaced.
        assert [e.id for e in episodes] == [first_id]
        assert episodes[0].ack_revision == 1
        return

    old, new = episodes
    assert old.id == first_id
    assert old.closed_at == NOW + 30 * MINUTE
    assert old.closed_clock_us == 30 * MINUTE_US
    assert old.ack_revision == 1
    assert new.id != first_id
    assert new.pairing_key == old.pairing_key
    assert new.ack_revision == 0
    assert new.opened_at == NOW + 31 * MINUTE
    assert new.opened_clock_us == 31 * MINUTE_US
    # The old acknowledgement stays with the old episode as history.
    acknowledged = db.session.scalars(
        select(DbMatchEscalationAck.episode_id).where(
            DbMatchEscalationAck.match_id == match_id
        )
    ).all()
    assert acknowledged == [first_id]


def test_reconcile_replaces_an_episode_when_the_pairing_changes(
    make_tournament,
):
    tournament_id = make_tournament()
    match_id = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()
    (old,) = _episodes(match_id)
    db.session.execute(
        delete(DbTournamentMatchToContestant).where(
            DbTournamentMatchToContestant.tournament_match_id == match_id
        )
    )
    _add_contestants(match_id, _participant_ids(tournament_id, 4)[2:])

    # The database allows one open episode per match, so the close of the
    # old episode has to reach it before the new one is inserted.
    assert service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + 5 * MINUTE
    ).is_ok()
    db.session.commit()

    closed, opened = _episodes(match_id)
    assert closed.id == old.id
    assert closed.closed_at == NOW + 5 * MINUTE
    assert closed.closed_clock_us == 5 * MINUTE_US
    assert opened.id != old.id
    assert opened.closed_at is None
    assert opened.pairing_key != old.pairing_key


def test_reconcile_closes_the_episode_of_a_confirmed_match(
    make_tournament, confirmer
):
    tournament_id = make_tournament(elapsed_us=10 * MINUTE_US)
    match_id = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()
    db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id == match_id)
        .values(confirmed_by=confirmer)
    )

    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + 7 * MINUTE
    )
    db.session.commit()

    (closed,) = _episodes(match_id)
    assert closed.closed_at == NOW + 7 * MINUTE
    assert closed.closed_clock_us == 17 * MINUTE_US


@pytest.mark.parametrize(
    'status', [TournamentStatus.COMPLETED, TournamentStatus.CANCELLED]
)
def test_reconcile_closes_the_episodes_of_a_terminal_tournament(
    make_tournament, status
):
    tournament_id = make_tournament(elapsed_us=40 * MINUTE_US)
    match_id = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()
    # The status edge froze the clock at 45 minutes before it reconciles.
    db.session.execute(
        text(
            'UPDATE lan_tournaments SET tournament_status = :status,'
            ' operational_clock_elapsed_us = :frozen,'
            ' operational_clock_running_since = NULL WHERE id = :id'
        ),
        {'status': status.name, 'frozen': 45 * MINUTE_US, 'id': tournament_id},
    )
    db.session.commit()

    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + 600 * MINUTE
    )
    db.session.commit()

    (closed,) = _episodes(match_id)
    assert closed.closed_clock_us == 45 * MINUTE_US


def test_paused_tournament_keeps_its_episodes(make_tournament):
    tournament_id = make_tournament(elapsed_us=20 * MINUTE_US)
    match_id = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE lan_tournaments SET tournament_status = 'PAUSED',"
            ' operational_clock_elapsed_us = :frozen,'
            ' operational_clock_running_since = NULL WHERE id = :id'
        ),
        {'frozen': 25 * MINUTE_US, 'id': tournament_id},
    )
    db.session.commit()
    (before,) = _episodes(match_id)

    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + 600 * MINUTE
    )
    db.session.commit()

    assert [e.id for e in _episodes(match_id)] == [before.id]
    assert _episodes(match_id)[0].closed_at is None


def test_ffa_lobby_episode_follows_the_recorded_occupancy(make_tournament):
    tournament_id = make_tournament(game_format=FREE_FOR_ALL)
    waiting = _add_match(tournament_id, 4, occupied=False)
    seated = _add_match(tournament_id, 4, occupied=True)

    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )

    assert _episodes(waiting) == []
    (episode,) = _episodes(seated)
    assert episode.opened_clock_us == MINUTE_US


# -------------------------------------------------------------------- #
# invalidation
# -------------------------------------------------------------------- #


def test_invalidate_closes_open_episodes_at_the_current_clock(
    make_tournament,
):
    tournament_id = make_tournament(elapsed_us=10 * MINUTE_US)
    corrected = _add_match(tournament_id)
    untouched = _add_match(tournament_id)
    service.reconcile_due_matches_flush(
        tournament_id, occurred_at=NOW + MINUTE
    )
    db.session.commit()

    result = service.invalidate_due_matches_flush(
        [corrected], occurred_at=NOW + 4 * MINUTE
    )

    assert result.is_ok()
    (closed,) = _episodes(corrected)
    assert closed.closed_at == NOW + 4 * MINUTE
    assert closed.closed_clock_us == 14 * MINUTE_US
    assert _episodes(untouched)[0].closed_at is None


def test_invalidate_takes_the_tournament_lock_and_refuses_unknown_matches(
    make_tournament,
):
    tournament_id = make_tournament()
    match_id = _add_match(tournament_id)

    refused = service.invalidate_due_matches_flush(
        [match_id, TournamentMatchID(uuid7())], occurred_at=NOW
    )
    assert refused.unwrap_err() == 'match_not_found'

    assert service.invalidate_due_matches_flush(
        [str(match_id)],  # type: ignore[list-item]
        occurred_at=NOW,
    ).is_ok()
    with db.engine.connect() as other:
        with pytest.raises(OperationalError):
            other.execute(
                text(
                    'SELECT id FROM lan_tournaments'
                    ' WHERE id = :id FOR UPDATE NOWAIT'
                ),
                {'id': tournament_id},
            )


# -------------------------------------------------------------------- #
# completed-lobby marker
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    ('count', 'group_size_min', 'allow_undersized', 'expected'),
    [
        (4, 4, False, None),
        (2, None, False, None),
        # Two inserted rows are not a roster of four seats.
        (2, 4, False, service.LOBBY_ROSTER_INCOMPLETE_ERROR),
        (3, 4, False, service.LOBBY_ROSTER_INCOMPLETE_ERROR),
        (2, 4, True, None),
        (1, None, True, service.LOBBY_ROSTER_INCOMPLETE_ERROR),
    ],
)
# fmt: on
def test_lobby_marker_requires_complete_roster(
    make_tournament, count, group_size_min, allow_undersized, expected
):
    tournament_id = make_tournament(
        game_format=FREE_FOR_ALL, group_size_min=group_size_min
    )
    # The two-row shortcut of the F-04 marker has already set the
    # occupancy of this lobby. It must not make the roster complete.
    lobby = _add_match(tournament_id, count, occupied=True)

    result = service.mark_completed_lobbies_occupied_flush(
        [lobby], occurred_at=NOW + MINUTE, allow_undersized=allow_undersized
    )

    if expected is None:
        assert result.is_ok()
    else:
        assert result.unwrap_err() == expected


def test_lobby_marker_refuses_one_incomplete_lobby_for_the_whole_call(
    make_tournament,
):
    tournament_id = make_tournament(
        game_format=FREE_FOR_ALL, group_size_min=4
    )
    full = _add_match(tournament_id, 4)
    short = _add_match(tournament_id, 2)

    result = service.mark_completed_lobbies_occupied_flush(
        [full, short], occurred_at=NOW
    )

    assert result.unwrap_err() == service.LOBBY_ROSTER_INCOMPLETE_ERROR


def test_lobby_marker_refuses_other_formats_confirmed_and_unknown_lobbies(
    make_tournament, confirmer
):
    one_v_one = make_tournament(game_format=ONE_V_ONE)
    group_match = _add_match(one_v_one, 4)
    free_for_all = make_tournament(game_format=FREE_FOR_ALL)
    confirmed = _add_match(free_for_all, 4, confirmed_by=confirmer)

    assert (
        service.mark_completed_lobbies_occupied_flush(
            [group_match], occurred_at=NOW
        ).unwrap_err()
        == service.LOBBY_NOT_FREE_FOR_ALL_ERROR
    )
    assert (
        service.mark_completed_lobbies_occupied_flush(
            [confirmed], occurred_at=NOW
        ).unwrap_err()
        == 'match_confirmed'
    )
    assert (
        service.mark_completed_lobbies_occupied_flush(
            [TournamentMatchID(uuid7())], occurred_at=NOW
        ).unwrap_err()
        == 'match_not_found'
    )


def test_lobby_marker_follows_the_format_of_the_lobbys_phase(
    make_tournament,
):
    tournament_id = make_tournament(
        game_format=GameFormat.HIGHSCORE,
        playoff_game_format=FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=4,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    group_match = _add_match(tournament_id, 4, phase=1)
    playoff_lobby = _add_match(tournament_id, 4, phase=2)

    assert (
        service.mark_completed_lobbies_occupied_flush(
            [group_match], occurred_at=NOW
        ).unwrap_err()
        == service.LOBBY_NOT_FREE_FOR_ALL_ERROR
    )
    assert service.mark_completed_lobbies_occupied_flush(
        [playoff_lobby], occurred_at=NOW
    ).is_ok()
