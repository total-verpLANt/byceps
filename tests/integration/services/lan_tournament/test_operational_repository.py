from dataclasses import replace
from datetime import datetime, timedelta, timezone, UTC
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import tournament_repository as repo
from byceps.services.lan_tournament.dashboard_config import (
    MAX_THRESHOLD_MINUTES,
    MIN_THRESHOLD_MINUTES,
)
from byceps.services.lan_tournament.dbmodels.dashboard import (
    DbDashboardPartyThresholds,
    DbMatchDashboardAnnotation,
    DbMatchDueEpisode,
    DbMatchEscalationAck,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
    OperationalClock,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (
    clock_value_us,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.uuid import uuid7


PARTY_ID = PartyID('f03-operational-repository')

NOW = datetime(2026, 10, 7, 12, 0, 0)
MINUTE_US = 60 * 1_000_000
THREE_DAYS_US = 3 * 24 * 60 * MINUTE_US
assert THREE_DAYS_US == 259_200_000_000


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 operational repository')


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def make_tournament(party):
    def _make(**clock) -> TournamentID:
        tournament_id = TournamentID(uuid7())
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=party.id,
                name=f'Operational {tournament_id}',
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
                tournament_status=TournamentStatus.ONGOING,
                game_format=None,
                elimination_mode=None,
                **clock,
            )
        )
        return tournament_id

    return _make


def _make_match(tournament_id, **kwargs) -> TournamentMatchID:
    match_id = TournamentMatchID(uuid7())
    repo.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=NOW,
        ),
        **kwargs,
    )
    return match_id


def _episode(match_id, tournament_id=None, **overrides) -> MatchDueEpisode:
    values = dict(
        id=MatchDueEpisodeID(uuid7()),
        tournament_id=tournament_id or TournamentID(uuid7()),
        match_id=match_id,
        pairing_key='participant:a|participant:b',
        opened_at=NOW,
        opened_clock_us=1_000,
    )
    return MatchDueEpisode(**{**values, **overrides})


def _ack(episode, **overrides) -> MatchEscalationAcknowledgement:
    values = dict(
        id=MatchEscalationAcknowledgementID(uuid7()),
        episode_id=episode.id,
        tournament_id=episode.tournament_id,
        match_id=episode.match_id,
        revision=1,
        occurred_at=NOW,
        clock_us=2_000,
        actor_id=UserID(uuid7()),
        comment=None,
    )
    return MatchEscalationAcknowledgement(**{**values, **overrides})


def _stored(model, primary_key):
    """Return the row as the database holds it, bypassing the session."""
    db.session.expire_all()
    return db.session.get(model, primary_key, populate_existing=True)


def _scalar(sql: str, **params):
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _constraint(error: IntegrityError) -> str:
    return cast(Any, error.orig).diag.constraint_name


# -------------------------------------------------------------------- #
# mapper
# -------------------------------------------------------------------- #


def test_mapper_roundtrips_timing_and_nullable_history(make_tournament):
    activated_at = NOW - timedelta(days=3)
    running = make_tournament(
        operational_clock_elapsed_us=45 * MINUTE_US,
        operational_clock_running_since=NOW,
        operational_clock_activated_at=activated_at,
    )
    unknown = make_tournament()
    legacy_id = uuid7()
    db.session.execute(
        text(
            'INSERT INTO lan_tournament_matches (id, tournament_id, created_at)'
            ' VALUES (:id, :tournament_id, :created_at)'
        ),
        {'id': legacy_id, 'tournament_id': unknown, 'created_at': NOW},
    )
    stamped = _make_match(running, changed_at=NOW)
    db.session.commit()
    db.session.expire_all()

    tournament = repo.get_tournament(running, fresh=True)
    assert tournament.operational_clock_elapsed_us == 45 * MINUTE_US
    assert tournament.operational_clock_running_since == NOW
    assert tournament.operational_clock_activated_at == activated_at
    assert repo.get_tournament_for_update(running) == tournament

    never_started = repo.get_tournament(unknown, fresh=True)
    assert never_started.operational_clock_elapsed_us == 0
    assert never_started.operational_clock_running_since is None
    assert never_started.operational_clock_activated_at is None

    assert repo.find_match(TournamentMatchID(legacy_id)).last_changed_at is None
    assert repo.find_match_fresh(stamped).last_changed_at == NOW
    assert repo.get_match_for_update(stamped).last_changed_at == NOW


def test_episode_ack_and_pin_rows_roundtrip(make_tournament):
    tournament_id = make_tournament()
    match_id = _make_match(tournament_id)
    actor_id = UserID(uuid7())

    open_episode = _episode(match_id, tournament_id)
    repo.open_due_episode_flush(open_episode)
    closed_episode = _episode(
        _make_match(tournament_id),
        tournament_id,
        opened_clock_us=THREE_DAYS_US,
        ack_revision=2,
    )
    repo.open_due_episode_flush(closed_episode)
    repo.close_due_episodes_flush(
        [closed_episode.match_id],
        occurred_at=NOW + timedelta(minutes=5),
        clock_us=THREE_DAYS_US + 1,
    )
    commented = _ack(open_episode, comment='Contacted both captains.')
    plain = _ack(open_episode, revision=2)
    repo.create_escalation_ack_flush(commented)
    repo.create_escalation_ack_flush(plain)
    pinned = repo.save_match_pin_flush(
        match_id,
        tournament_id,
        pinned_at=NOW,
        pinned_by=actor_id,
        updated_at=NOW,
        updated_by=actor_id,
        expected_revision=0,
    )
    db.session.commit()

    assert repo.list_open_due_episodes(tournament_id) == [open_episode]
    assert repo._db_episode_to_episode(
        _stored(DbMatchDueEpisode, closed_episode.id)
    ) == replace(
        closed_episode,
        closed_at=NOW + timedelta(minutes=5),
        closed_clock_us=THREE_DAYS_US + 1,
    )
    assert (
        repo._db_ack_to_ack(_stored(DbMatchEscalationAck, commented.id))
        == commented
    )
    assert repo._db_ack_to_ack(_stored(DbMatchEscalationAck, plain.id)) == plain
    assert pinned == repo.find_match_pin_state(match_id)
    assert (pinned.revision, pinned.pinned_at, pinned.pinned_by) == (
        1,
        NOW,
        actor_id,
    )

    unpinned = repo.save_match_pin_flush(
        match_id,
        tournament_id,
        pinned_at=None,
        pinned_by=None,
        updated_at=NOW + timedelta(minutes=1),
        updated_by=actor_id,
        expected_revision=1,
    )
    db.session.commit()
    assert (unpinned.revision, unpinned.pinned_at, unpinned.pinned_by) == (
        2,
        None,
        None,
    )
    assert repo.find_match_pin_state(match_id) == unpinned


# -------------------------------------------------------------------- #
# database backstops
# -------------------------------------------------------------------- #


def test_only_one_open_episode_per_match(make_tournament):
    tournament_id = make_tournament()
    match_id, other_match_id = (
        _make_match(tournament_id),
        _make_match(tournament_id),
    )
    first = _episode(match_id, tournament_id)
    other = _episode(other_match_id, tournament_id)
    repo.open_due_episode_flush(first)
    repo.open_due_episode_flush(other)
    db.session.commit()

    with pytest.raises(IntegrityError) as raised:
        repo.open_due_episode_flush(_episode(match_id, tournament_id))
    db.session.rollback()
    assert _constraint(raised.value) == (
        'uq_lan_tournament_due_episodes_open_match'
    )

    # Once closed, the match can open a new episode: the history stays.
    repo.close_due_episodes_flush(
        [match_id], occurred_at=NOW + timedelta(minutes=1), clock_us=2_000
    )
    second = _episode(match_id, tournament_id, opened_clock_us=2_000)
    repo.open_due_episode_flush(second)
    db.session.commit()

    assert {e.id for e in repo.list_open_due_episodes(tournament_id)} == {
        second.id,
        other.id,
    }
    assert (
        _scalar(
            'SELECT count(*) FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :match_id',
            match_id=match_id,
        )
        == 2
    )


def test_acks_are_episode_revision_unique(make_tournament):
    tournament_id = make_tournament()
    episode = _episode(_make_match(tournament_id), tournament_id)
    other = _episode(_make_match(tournament_id), tournament_id)
    repo.open_due_episode_flush(episode)
    repo.open_due_episode_flush(other)
    repo.create_escalation_ack_flush(_ack(episode, revision=1))
    db.session.commit()

    with pytest.raises(IntegrityError) as duplicate:
        repo.create_escalation_ack_flush(_ack(episode, revision=1))
    db.session.rollback()
    assert _constraint(duplicate.value) == (
        'uq_lan_tournament_escalation_ack_episode_revision'
    )

    # Another episode may use the revision, the next revision is free.
    repo.create_escalation_ack_flush(_ack(other, revision=1))
    repo.create_escalation_ack_flush(_ack(episode, revision=2))
    db.session.commit()

    with pytest.raises(IntegrityError) as first_revision:
        repo.create_escalation_ack_flush(_ack(episode, revision=0))
    db.session.rollback()
    assert _constraint(first_revision.value) == (
        'ck_lan_tournament_escalation_ack_revision'
    )

    with pytest.raises(IntegrityError) as dangling:
        repo.create_escalation_ack_flush(
            _ack(_episode(TournamentMatchID(uuid7())), revision=1)
        )
    db.session.rollback()
    assert _constraint(dangling.value) == (
        'fk_lan_tournament_escalation_acks_episode_id'
    )


# fmt: off
@pytest.mark.parametrize(
    'closed_at, closed_clock_us',
    [
        (NOW, None),
        (None, 5_000),
    ],
    ids=['time-without-clock', 'clock-without-time'],
)
# fmt: on
def test_close_pair_checks_reject_partial_nulls(closed_at, closed_clock_us):
    with pytest.raises(IntegrityError) as raised:
        repo.open_due_episode_flush(
            _episode(
                TournamentMatchID(uuid7()),
                opened_clock_us=1_000,
                closed_at=closed_at,
                closed_clock_us=closed_clock_us,
            )
        )
    db.session.rollback()

    assert _constraint(raised.value) == (
        'ck_lan_tournament_due_episodes_close_pair'
    )


def test_close_pair_accepts_open_and_closed_rows(make_tournament):
    tournament_id = make_tournament()
    open_episode = _episode(TournamentMatchID(uuid7()), tournament_id)
    closed_episode = _episode(
        TournamentMatchID(uuid7()),
        tournament_id,
        closed_at=NOW,
        closed_clock_us=5_000,
    )

    repo.open_due_episode_flush(open_episode)
    repo.open_due_episode_flush(closed_episode)
    db.session.commit()

    assert repo.list_open_due_episodes(tournament_id) == [open_episode]


def test_close_writes_the_time_and_the_clock_together(make_tournament):
    tournament_id = make_tournament()
    match_id = TournamentMatchID(uuid7())
    episode = _episode(match_id, tournament_id)
    repo.open_due_episode_flush(episode)
    repo.close_due_episodes_flush(
        [match_id], occurred_at=NOW + timedelta(minutes=2), clock_us=9_000
    )
    db.session.commit()

    row = _stored(DbMatchDueEpisode, episode.id)
    assert (row.closed_at, row.closed_clock_us) == (
        NOW + timedelta(minutes=2),
        9_000,
    )
    assert repo.list_open_due_episodes(tournament_id) == []

    # A closed episode is history: closing again never rewrites it.
    repo.close_due_episodes_flush(
        [match_id], occurred_at=NOW + timedelta(minutes=9), clock_us=99_000
    )
    db.session.commit()

    row = _stored(DbMatchDueEpisode, episode.id)
    assert (row.closed_at, row.closed_clock_us) == (
        NOW + timedelta(minutes=2),
        9_000,
    )


def test_open_episode_read_is_fresh_after_a_concurrent_commit(
    make_tournament,
):
    tournament_id = make_tournament()
    episode = _episode(TournamentMatchID(uuid7()), tournament_id)
    repo.open_due_episode_flush(episode)
    db.session.commit()
    # Hold the instance: the identity map only keeps weak references.
    cached = db.session.get(DbMatchDueEpisode, episode.id)
    assert cached.ack_revision == 0

    with Session(db.engine) as other:
        other.execute(
            text(
                'UPDATE lan_tournament_match_due_episodes'
                ' SET ack_revision = 3 WHERE id = :id'
            ),
            {'id': episode.id},
        )
        other.commit()

    assert db.session.get(DbMatchDueEpisode, episode.id).ack_revision == 0
    [fresh] = repo.list_open_due_episodes(tournament_id)
    assert fresh.ack_revision == 3
    assert cached.ack_revision == 3


# -------------------------------------------------------------------- #
# clock columns
# -------------------------------------------------------------------- #


@pytest.mark.parametrize('microseconds', [THREE_DAYS_US, 2**40, 2**31])
def test_multi_day_clock_values_persist(make_tournament, microseconds):
    tournament_id = make_tournament(operational_clock_elapsed_us=microseconds)
    match_id = TournamentMatchID(uuid7())
    episode = _episode(
        match_id, tournament_id, opened_clock_us=microseconds
    )
    repo.open_due_episode_flush(episode)
    repo.create_escalation_ack_flush(_ack(episode, clock_us=microseconds))
    repo.close_due_episodes_flush(
        [match_id], occurred_at=NOW, clock_us=microseconds + 7
    )
    db.session.commit()

    assert _scalar(
        'SELECT operational_clock_elapsed_us FROM lan_tournaments'
        ' WHERE id = :id',
        id=tournament_id,
    ) == microseconds
    assert _scalar(
        'SELECT opened_clock_us FROM lan_tournament_match_due_episodes'
        ' WHERE id = :id',
        id=episode.id,
    ) == microseconds
    assert _scalar(
        'SELECT closed_clock_us FROM lan_tournament_match_due_episodes'
        ' WHERE id = :id',
        id=episode.id,
    ) == microseconds + 7
    assert _scalar(
        'SELECT clock_us FROM lan_tournament_match_escalation_acks'
        ' WHERE episode_id = :id',
        id=episode.id,
    ) == microseconds
    db.session.expire_all()
    assert (
        repo.get_tournament(tournament_id, fresh=True)
        .operational_clock_elapsed_us
        == microseconds
    )


@pytest.mark.parametrize(
    'column, table',
    [
        ('operational_clock_elapsed_us', 'lan_tournaments'),
        ('opened_clock_us', 'lan_tournament_match_due_episodes'),
        ('closed_clock_us', 'lan_tournament_match_due_episodes'),
        ('clock_us', 'lan_tournament_match_escalation_acks'),
    ],
)
def test_clock_columns_are_bigint(column, table):
    assert _scalar(
        'SELECT data_type FROM information_schema.columns'
        ' WHERE table_name = :table AND column_name = :column',
        table=table,
        column=column,
    ) == 'bigint'


def test_a_negative_clock_is_refused(make_tournament):
    tournament_id = make_tournament()
    with pytest.raises(IntegrityError) as raised:
        repo.open_due_episode_flush(
            _episode(
                TournamentMatchID(uuid7()), tournament_id, opened_clock_us=-1
            )
        )
    db.session.rollback()

    assert _constraint(raised.value) == (
        'ck_lan_tournament_due_episodes_opened_clock_us'
    )


def test_a_full_row_update_leaves_the_clock_alone(make_tournament):
    tournament_id = make_tournament(
        operational_clock_elapsed_us=THREE_DAYS_US,
        operational_clock_running_since=NOW,
        operational_clock_activated_at=NOW,
    )
    db.session.commit()
    stale = replace(
        repo.get_tournament(tournament_id, fresh=True),
        name='Renamed',
        operational_clock_elapsed_us=0,
        operational_clock_running_since=None,
        operational_clock_activated_at=None,
    )

    repo.update_tournament(stale)

    stored = repo.get_tournament(tournament_id, fresh=True)
    assert stored.name == 'Renamed'
    assert stored.operational_clock_elapsed_us == THREE_DAYS_US
    assert stored.operational_clock_running_since == NOW
    assert stored.operational_clock_activated_at == NOW


# -------------------------------------------------------------------- #
# operation time and last change
# -------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def test_operation_time_is_naive_utc_whatever_the_session_zone(
    make_tournament,
):
    db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))

    sampled = repo.get_operation_time()

    assert sampled.tzinfo is None
    assert abs(sampled - _utc_now()) < timedelta(seconds=10)
    db.session.rollback()


def test_operation_time_moves_within_one_transaction():
    first = repo.get_operation_time()
    db.session.execute(text('SELECT pg_sleep(0.01)'))
    second = repo.get_operation_time()
    db.session.rollback()

    assert second > first


def test_create_match_initializes_the_last_change(make_tournament):
    tournament_id = make_tournament()
    before = _utc_now()
    sampled = _make_match(tournament_id)
    given = _make_match(tournament_id, changed_at=NOW)
    carried_id = TournamentMatchID(uuid7())
    repo.create_match(
        TournamentMatch(
            id=carried_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=1,
            round=1,
            next_match_id=None,
            confirmed_by=None,
            created_at=NOW,
            last_changed_at=NOW + timedelta(minutes=9),
        ),
        changed_at=NOW,
    )
    db.session.commit()
    db.session.expire_all()

    assert before - timedelta(seconds=1) <= (
        repo.find_match(sampled).last_changed_at
    ) <= _utc_now() + timedelta(seconds=1)
    assert repo.find_match(given).last_changed_at == NOW
    assert repo.find_match(carried_id).last_changed_at == NOW + timedelta(
        minutes=9
    )


def test_touch_is_monotonic_and_fills_unknown_history(make_tournament):
    tournament_id = make_tournament()
    unknown_id = uuid7()
    db.session.execute(
        text(
            'INSERT INTO lan_tournament_matches (id, tournament_id, created_at)'
            ' VALUES (:id, :tournament_id, :created_at)'
        ),
        {'id': unknown_id, 'tournament_id': tournament_id, 'created_at': NOW},
    )
    known = _make_match(tournament_id, changed_at=NOW)
    untouched = _make_match(tournament_id, changed_at=NOW)
    unknown = TournamentMatchID(unknown_id)
    later = NOW + timedelta(minutes=3)

    repo.touch_matches_last_changed_flush({unknown, known}, changed_at=later)
    repo.touch_matches_last_changed_flush(
        {unknown, known}, changed_at=NOW + timedelta(minutes=1)
    )
    db.session.commit()
    db.session.expire_all()

    assert repo.find_match(unknown).last_changed_at == later
    assert repo.find_match(known).last_changed_at == later
    assert repo.find_match(untouched).last_changed_at == NOW

    repo.touch_matches_last_changed_flush(
        [known], changed_at=later + timedelta(microseconds=1)
    )
    db.session.commit()
    db.session.expire_all()
    assert repo.find_match(known).last_changed_at == later + timedelta(
        microseconds=1
    )


def test_touch_stores_naive_utc_under_a_foreign_session_zone(make_tournament):
    tournament_id = make_tournament()
    match_id = _make_match(tournament_id, changed_at=NOW - timedelta(days=1))
    aware = datetime.fromisoformat('2026-10-07T14:00:00+02:00')
    db.session.commit()

    db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
    repo.touch_matches_last_changed_flush([match_id], changed_at=aware)
    db.session.commit()

    assert _scalar(
        'SELECT last_changed_at FROM lan_tournament_matches WHERE id = :id',
        id=match_id,
    ) == datetime(2026, 10, 7, 12, 0)


def test_touch_failure_rolls_back_with_the_owner(make_tournament):
    tournament_id = make_tournament()
    match_id = _make_match(tournament_id, changed_at=NOW)
    db.session.commit()

    repo.touch_matches_last_changed_flush(
        [match_id], changed_at=NOW + timedelta(hours=1)
    )
    repo.rollback_session()

    assert _scalar(
        'SELECT last_changed_at FROM lan_tournament_matches WHERE id = :id',
        id=match_id,
    ) == NOW


# -------------------------------------------------------------------- #
# retirement
# -------------------------------------------------------------------- #


# fmt: off
CLOCK_CASES = [
    # id, elapsed_us, running_since offset (s) or None, at offset (s)
    ('frozen',               7 * MINUTE_US,       None,    90),
    ('running',              7 * MINUTE_US,       0,       90),
    ('at-the-start',         7 * MINUTE_US,       0,       0),
    ('before-the-start',     7 * MINUTE_US,       30,      0),
    ('three-days',           THREE_DAYS_US,       0,       3 * 24 * 3600 + 5),
    ('beyond-int32',         2**40,               0,       90),
    ('a-microsecond',        0,                   0,       None),
]
# fmt: on


@pytest.mark.parametrize(
    'elapsed_us, since_offset, at_offset',
    [case[1:] for case in CLOCK_CASES],
    ids=[case[0] for case in CLOCK_CASES],
)
def test_clock_sql_matches_the_pure_clock(
    make_tournament, elapsed_us, since_offset, at_offset
):
    running_since = (
        NOW + timedelta(seconds=since_offset)
        if since_offset is not None
        else None
    )
    at = (
        NOW + timedelta(seconds=at_offset)
        if at_offset is not None
        else NOW + timedelta(microseconds=1)
    )
    tournament_id = make_tournament(
        operational_clock_elapsed_us=elapsed_us,
        operational_clock_running_since=running_since,
        operational_clock_activated_at=NOW,
    )

    in_sql = db.session.scalar(
        select(repo._tournament_clock_us_at(at)).where(
            DbTournament.id == tournament_id
        )
    )

    assert in_sql == clock_value_us(
        OperationalClock(
            elapsed_us=elapsed_us,
            running_since=running_since,
            activated_at=NOW,
        ),
        at,
    )


def test_retire_closes_episodes_at_the_tournament_clock_and_drops_pins(
    make_tournament,
):
    at = NOW + timedelta(seconds=90)
    running = make_tournament(
        operational_clock_elapsed_us=10 * MINUTE_US,
        operational_clock_running_since=NOW,
        operational_clock_activated_at=NOW - timedelta(hours=1),
    )
    frozen = make_tournament(
        operational_clock_elapsed_us=3 * MINUTE_US,
        operational_clock_activated_at=NOW - timedelta(hours=1),
    )
    retired = _make_match(running)
    retired_frozen = _make_match(frozen)
    kept = _make_match(running)
    orphan = TournamentMatchID(uuid7())

    episodes = {
        'running': _episode(retired, running, opened_clock_us=5 * MINUTE_US),
        'frozen': _episode(
            retired_frozen, frozen, opened_clock_us=2 * MINUTE_US
        ),
        'kept': _episode(kept, running, opened_clock_us=6 * MINUTE_US),
        'orphan': _episode(orphan, opened_clock_us=4 * MINUTE_US),
        'closed': _episode(
            retired,
            running,
            opened_clock_us=1 * MINUTE_US,
            closed_at=NOW - timedelta(minutes=30),
            closed_clock_us=2 * MINUTE_US,
        ),
    }
    for episode in episodes.values():
        repo.open_due_episode_flush(episode)
    actor_id = UserID(uuid7())
    for match_id, tournament_id in ((retired, running), (kept, running)):
        repo.save_match_pin_flush(
            match_id,
            tournament_id,
            pinned_at=NOW,
            pinned_by=actor_id,
            updated_at=NOW,
            updated_by=actor_id,
            expected_revision=0,
        )
    db.session.commit()

    repo.retire_dashboard_matches_flush(
        [retired, retired_frozen, orphan], occurred_at=at
    )
    db.session.commit()

    def closed(name):
        row = _stored(DbMatchDueEpisode, episodes[name].id)
        return row.closed_at, row.closed_clock_us

    assert closed('running') == (at, 10 * MINUTE_US + 90 * 1_000_000)
    assert closed('frozen') == (at, 3 * MINUTE_US)
    # No tournament row to read the clock from: no time is invented.
    assert closed('orphan') == (at, 4 * MINUTE_US)
    assert closed('kept') == (None, None)
    assert closed('closed') == (NOW - timedelta(minutes=30), 2 * MINUTE_US)
    assert repo.find_match_pin_state(retired) is None
    assert repo.find_match_pin_state(kept) is not None
    assert [e.id for e in repo.list_open_due_episodes(running)] == [
        episodes['kept'].id
    ]


def test_retired_matches_can_be_deleted_afterwards(make_tournament):
    tournament_id = make_tournament(operational_clock_activated_at=NOW)
    match_id = _make_match(tournament_id)
    episode = _episode(match_id, tournament_id)
    repo.open_due_episode_flush(episode)
    repo.create_escalation_ack_flush(_ack(episode))
    db.session.commit()

    repo.retire_dashboard_matches_flush([match_id], occurred_at=NOW)
    repo.delete_match_flush(match_id)
    db.session.commit()

    assert repo.find_match(match_id) is None
    row = _stored(DbMatchDueEpisode, episode.id)
    assert row.match_id == match_id and row.closed_at == NOW
    assert (
        _scalar(
            'SELECT count(*) FROM lan_tournament_match_escalation_acks'
            ' WHERE episode_id = :id',
            id=episode.id,
        )
        == 1
    )


# -------------------------------------------------------------------- #
# pins
# -------------------------------------------------------------------- #


def _pin(match_id, tournament_id, expected_revision, *, pinned=True):
    actor_id = UserID(uuid7())
    return repo.save_match_pin_flush(
        match_id,
        tournament_id,
        pinned_at=NOW if pinned else None,
        pinned_by=actor_id if pinned else None,
        updated_at=NOW,
        updated_by=actor_id,
        expected_revision=expected_revision,
    )


def test_pin_storage_is_compare_and_set_on_the_revision():
    tournament_id, match_id = TournamentID(uuid7()), TournamentMatchID(uuid7())

    first = _pin(match_id, tournament_id, 0)
    assert first.revision == 1

    # A second insert of the "never annotated" revision changes nothing.
    assert _pin(match_id, tournament_id, 0, pinned=False) is None
    # A stale or foreign revision changes nothing either.
    assert _pin(match_id, tournament_id, 5) is None
    assert _pin(match_id, tournament_id, -1) is None
    assert repo.find_match_pin_state(match_id) == first

    second = _pin(match_id, tournament_id, 1, pinned=False)
    assert second.revision == 2 and second.pinned_at is None
    assert _pin(match_id, tournament_id, 1) is None
    assert repo.find_match_pin_state(match_id) == second
    db.session.rollback()


def test_pin_pair_check_rejects_a_pin_without_an_actor():
    with pytest.raises(IntegrityError) as raised:
        repo.save_match_pin_flush(
            TournamentMatchID(uuid7()),
            TournamentID(uuid7()),
            pinned_at=NOW,
            pinned_by=None,
            updated_at=NOW,
            updated_by=UserID(uuid7()),
            expected_revision=0,
        )
    db.session.rollback()

    assert _constraint(raised.value) == (
        'ck_lan_tournament_match_dashboard_annotations_pin_pair'
    )


def test_pin_read_is_fresh_after_a_concurrent_commit():
    tournament_id, match_id = TournamentID(uuid7()), TournamentMatchID(uuid7())
    _pin(match_id, tournament_id, 0)
    db.session.commit()
    # Hold the instance: the identity map only keeps weak references.
    cached = db.session.get(DbMatchDashboardAnnotation, match_id)
    assert cached.revision == 1

    with Session(db.engine) as other:
        other.execute(
            text(
                'UPDATE lan_tournament_match_dashboard_annotations'
                ' SET revision = 9 WHERE match_id = :id'
            ),
            {'id': match_id},
        )
        other.commit()

    assert db.session.get(DbMatchDashboardAnnotation, match_id).revision == 1
    assert repo.find_match_pin_state(match_id).revision == 9
    assert cached.revision == 9


# -------------------------------------------------------------------- #
# party thresholds
# -------------------------------------------------------------------- #


def _set(
    party_id,
    expected_revision,
    yellow=20,
    red=60,
    expected_updated_at=None,
    updated_at=NOW,
):
    return repo.set_party_thresholds_flush(
        party_id,
        yellow_minutes=yellow,
        red_minutes=red,
        expected_revision=expected_revision,
        expected_updated_at=expected_updated_at,
        updated_at=updated_at,
        updated_by=UserID(uuid7()),
    )


def test_party_threshold_row_checks_and_cas():
    party_id = PartyID(f'thresholds-{uuid7()}')

    # Absent row: the "no override yet" revision inserts.
    assert repo.find_party_thresholds(party_id) is None
    created = _set(party_id, 0)
    assert (created.yellow_minutes, created.red_minutes) == (20, 60)
    assert created.revision == 1
    assert created.updated_at == NOW
    db.session.commit()

    # A second insert, a stale revision and a missing row change nothing.
    assert _set(party_id, 0, yellow=5, red=10) is None
    assert _set(party_id, 7, yellow=5, red=10, expected_updated_at=NOW) is None
    missing = PartyID(f'thresholds-{uuid7()}')
    assert _set(missing, 1, expected_updated_at=NOW) is None
    assert repo.find_party_thresholds(party_id) == created

    # The right revision with another time, or without one, is not the
    # override that was read.
    other_time = NOW + timedelta(microseconds=1)
    assert _set(party_id, 1, expected_updated_at=other_time) is None
    assert _set(party_id, 1, expected_updated_at=None) is None
    assert repo.find_party_thresholds(party_id) == created

    later = NOW + timedelta(minutes=5)
    updated = _set(
        party_id,
        1,
        yellow=10,
        red=30,
        expected_updated_at=created.updated_at,
        updated_at=later,
    )
    assert updated.revision == 2
    assert updated.updated_at == later
    assert (updated.yellow_minutes, updated.red_minutes) == (10, 30)
    assert _set(party_id, 1, yellow=11, red=31, expected_updated_at=NOW) is None
    db.session.commit()
    assert repo.find_party_thresholds(party_id) == updated

    # An aware time is the same moment as the naive UTC one it converts to.
    zone = timezone(timedelta(hours=2))
    assert _set(
        party_id,
        2,
        yellow=10,
        red=30,
        expected_updated_at=(later + timedelta(hours=2)).replace(tzinfo=zone),
        updated_at=later,
    )
    db.session.commit()
    updated = repo.find_party_thresholds(party_id)
    assert updated.revision == 3

    # Deleting needs the current revision and time, then the default applies again.
    assert not repo.delete_party_thresholds_flush(
        party_id, expected_revision=1, expected_updated_at=updated.updated_at
    )
    assert not repo.delete_party_thresholds_flush(
        party_id, expected_revision=3, expected_updated_at=NOW
    )
    assert repo.find_party_thresholds(party_id) == updated
    assert repo.delete_party_thresholds_flush(
        party_id, expected_revision=3, expected_updated_at=updated.updated_at
    )
    db.session.commit()
    assert repo.find_party_thresholds(party_id) is None
    assert not repo.delete_party_thresholds_flush(
        party_id, expected_revision=3, expected_updated_at=updated.updated_at
    )

    # The database refuses what the service would already have refused.
    bad = [
        (
            MIN_THRESHOLD_MINUTES - 1,
            MIN_THRESHOLD_MINUTES + 4,
            'ck_lan_tournament_dashboard_party_thresholds_yellow_min',
        ),
        (
            MAX_THRESHOLD_MINUTES - 1,
            MAX_THRESHOLD_MINUTES - 1,
            'ck_lan_tournament_dashboard_party_thresholds_order',
        ),
        (
            MAX_THRESHOLD_MINUTES,
            MAX_THRESHOLD_MINUTES - 1,
            'ck_lan_tournament_dashboard_party_thresholds_order',
        ),
        (
            MIN_THRESHOLD_MINUTES,
            MAX_THRESHOLD_MINUTES + 1,
            'ck_lan_tournament_dashboard_party_thresholds_red_max',
        ),
    ]
    for yellow, red, name in bad:
        with pytest.raises(IntegrityError) as raised:
            _set(PartyID(f'thresholds-{uuid7()}'), 0, yellow=yellow, red=red)
        db.session.rollback()
        assert _constraint(raised.value) == name, (yellow, red)

    # The same checks guard the update path.
    kept = _set(party_id, 0)
    db.session.commit()
    with pytest.raises(IntegrityError) as raised:
        _set(
            party_id,
            kept.revision,
            yellow=30,
            red=30,
            expected_updated_at=kept.updated_at,
        )
    db.session.rollback()
    assert _constraint(raised.value) == (
        'ck_lan_tournament_dashboard_party_thresholds_order'
    )
    assert repo.find_party_thresholds(party_id) == kept


def test_party_threshold_cas_tells_a_reset_and_resave_apart():
    party_id = PartyID(f'thresholds-{uuid7()}')
    first = _set(party_id, 0)
    db.session.commit()

    # Another orga resets and saves again: the revision is 1 once more.
    assert repo.delete_party_thresholds_flush(
        party_id, expected_revision=1, expected_updated_at=first.updated_at
    )
    second = _set(
        party_id, 0, yellow=25, red=70, updated_at=NOW + timedelta(minutes=5)
    )
    db.session.commit()
    assert second.revision == first.revision == 1
    assert second.updated_at != first.updated_at

    # A writer that still holds the first override loses, on the time alone.
    assert _set(party_id, 1, yellow=30, red=90, expected_updated_at=NOW) is None
    assert not repo.delete_party_thresholds_flush(
        party_id, expected_revision=1, expected_updated_at=first.updated_at
    )
    assert repo.find_party_thresholds(party_id) == second

    assert _set(
        party_id, 1, yellow=30, red=90, expected_updated_at=second.updated_at
    )
    db.session.commit()
    assert repo.find_party_thresholds(party_id).revision == 2


def test_party_threshold_revision_check_rejects_zero():
    row = DbDashboardPartyThresholds(
        PartyID(f'thresholds-{uuid7()}'),
        20,
        60,
        NOW,
        UserID(uuid7()),
        revision=0,
    )
    db.session.add(row)
    with pytest.raises(IntegrityError) as raised:
        db.session.flush()
    db.session.rollback()

    assert _constraint(raised.value) == (
        'ck_lan_tournament_dashboard_party_thresholds_revision'
    )


def test_party_threshold_read_is_fresh_after_a_concurrent_commit():
    party_id = PartyID(f'thresholds-{uuid7()}')
    _set(party_id, 0)
    db.session.commit()
    # Hold the instance: the identity map only keeps weak references.
    cached = db.session.get(DbDashboardPartyThresholds, party_id)
    assert cached.yellow_minutes == 20

    with Session(db.engine) as other:
        other.execute(
            text(
                'UPDATE lan_tournament_dashboard_party_thresholds'
                ' SET yellow_minutes = 12, revision = 2 WHERE party_id = :id'
            ),
            {'id': party_id},
        )
        other.commit()

    assert db.session.get(DbDashboardPartyThresholds, party_id).revision == 1
    fresh = repo.find_party_thresholds(party_id)
    assert (fresh.yellow_minutes, fresh.revision) == (12, 2)
    assert cached.yellow_minutes == 12
