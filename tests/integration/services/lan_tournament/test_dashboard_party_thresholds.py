from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
import logging
import threading
import time

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from byceps.database import db
from byceps.services.lan_tournament import (
    dashboard_config,
    tournament_dashboard_settings_service as service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.dashboard import (
    DbDashboardPartyThresholds,
)
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
    TrafficTier,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    PartyDashboardThresholds,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_dashboard_settings_service import (
    THRESHOLDS_STALE_ERROR,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (
    derive_traffic_tier,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok
from byceps.util.uuid import uuid7


TOURNAMENT_PARTY_ID = PartyID('f03-party-thresholds')

NOW = datetime(2026, 10, 7, 12, 0, 0)
MINUTE_US = 60 * 1_000_000

WAIT_TIMEOUT = 20

_local = threading.local()


@pytest.fixture(scope='module')
def tournament_party(make_party, brand):
    return make_party(brand, TOURNAMENT_PARTY_ID, 'F03 party thresholds')


@pytest.fixture(autouse=True)
def _context(admin_app, tournament_party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def party_ids():
    """Provide unique party IDs, and remove their overrides afterwards.

    A party ID is a snapshot without a foreign key, so no party exists.
    """
    created: list[PartyID] = []

    def _make() -> PartyID:
        party_id = PartyID(f'thresholds-{uuid7()}')
        created.append(party_id)
        return party_id

    yield _make

    db.session.rollback()
    db.session.execute(
        delete(DbDashboardPartyThresholds).where(
            DbDashboardPartyThresholds.party_id.in_(created)
        )
    )
    db.session.commit()


@pytest.fixture
def party_id(party_ids) -> PartyID:
    return party_ids()


def _orga() -> UserID:
    return UserID(uuid7())


def _version(thresholds: PartyDashboardThresholds) -> dict:
    """Return what a form carries to name the override it was loaded with."""
    return {
        'expected_revision': thresholds.revision,
        'expected_updated_at': thresholds.updated_at,
    }


NO_OVERRIDE = {'expected_revision': 0, 'expected_updated_at': None}


def _set(party_id, *, yellow=20, red=60, version=NO_OVERRIDE, initiator=None):
    return service.set_party_thresholds(
        party_id,
        yellow_minutes=yellow,
        red_minutes=red,
        initiator_id=initiator or _orga(),
        **version,
    )


def _reset(party_id, *, version, initiator=None):
    return service.reset_party_thresholds(
        party_id, initiator_id=initiator or _orga(), **version
    )


def _committed_rows(party_id) -> list[tuple]:
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    'SELECT yellow_minutes, red_minutes, revision, updated_by'
                    ' FROM lan_tournament_dashboard_party_thresholds'
                    ' WHERE party_id = :party_id'
                ),
                {'party_id': party_id},
            )
        ]


def _deployment():
    return dashboard_config.get_dashboard_settings().unwrap()


# -------------------------------------------------------------------- #
# round trip and validation against the database
# -------------------------------------------------------------------- #


def test_party_override_and_reset_round_trip_in_the_database(party_ids):
    party, other = party_ids(), party_ids()
    deployment = _deployment()
    initiator = _orga()

    assert service.get_effective_dashboard_settings(party).unwrap() == (
        deployment
    )

    saved = _set(party, yellow=20, red=61, initiator=initiator).unwrap()

    assert _committed_rows(party) == [(20, 61, 1, initiator)]
    assert saved.updated_by == initiator
    assert abs(saved.updated_at - datetime.now(UTC).replace(tzinfo=None)) < (
        timedelta(minutes=5)
    )
    effective = service.get_effective_dashboard_settings(party).unwrap()
    assert effective.yellow_minutes == 20
    assert effective.red_minutes == 61
    assert effective.threshold_source == 'party'
    assert effective.poll_seconds == deployment.poll_seconds
    assert effective.page_size == deployment.page_size
    assert service.get_party_thresholds(party) == saved

    # Another party keeps the deployment default.
    assert service.get_effective_dashboard_settings(other).unwrap() == (
        deployment
    )
    assert service.get_party_thresholds(other) is None

    assert _reset(party, version=_version(saved)).is_ok()

    assert _committed_rows(party) == []
    assert service.get_party_thresholds(party) is None
    assert service.get_effective_dashboard_settings(party).unwrap() == (
        deployment
    )


# fmt: off
@pytest.mark.parametrize(
    ('yellow', 'red'),
    [
        (0, 10),
        (15, 15),
        (45, 15),
        (15, 1441),
        (900, 2700),
        (15.0, 45),
        (True, 45),
        ('15', '45'),
        (-1, 45),
    ],
)
# fmt: on
def test_invalid_thresholds_write_no_row_in_the_database(
    party_id, yellow, red
):
    result = _set(party_id, yellow=yellow, red=red)

    assert result.is_err()
    assert result != Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(party_id) == []
    assert db.session.scalar(
        select(DbDashboardPartyThresholds).filter_by(party_id=party_id)
    ) is None
    assert not db.session.new
    assert not db.session.dirty


def test_database_check_violation_rolls_back_and_raises(
    party_id, monkeypatch
):
    # A value the validation missed hits the named CHECK constraint.
    monkeypatch.setattr(
        service, 'validate_thresholds', lambda yellow, red: Ok((yellow, red))
    )

    with pytest.raises(IntegrityError) as raised:
        _set(party_id, yellow=30, red=10)

    assert (
        raised.value.orig.diag.constraint_name  # type: ignore[union-attr]
        == 'ck_lan_tournament_dashboard_party_thresholds_order'
    )
    # The session is usable again and holds nothing.
    assert db.session.scalar(text('SELECT 1')) == 1
    assert _committed_rows(party_id) == []


# -------------------------------------------------------------------- #
# compare and set
# -------------------------------------------------------------------- #


@contextmanager
def _threads():
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        yield executor
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _in_app_context(app, call):
    """Run `call` with an app context, so with a session of its own."""
    with app.app_context():
        try:
            db.session.execute(text("SET LOCAL lock_timeout = '10s'"))
            return call()
        finally:
            db.session.rollback()
            db.session.remove()


def _backend_pid() -> int:
    return db.session.scalar(text('SELECT pg_backend_pid()'))


def _wait_until_blocked_by(waiter_pid: int, holder_pid: int, future) -> bool:
    """Return whether the waiter waits for the holder.

    A waiter that finished without waiting returns `False`, so the caller
    can still assert the business outcome.
    """
    with db.engine.connect().execution_options(
        isolation_level='AUTOCOMMIT'
    ) as connection:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            blockers = connection.scalar(
                text('SELECT pg_blocking_pids(:pid)'), {'pid': waiter_pid}
            )
            if holder_pid in blockers:
                return True
            if future.done():
                return False
            time.sleep(0.02)
    pytest.fail(f'backend {waiter_pid} never waited for {holder_pid}')


def _race_a_save_against_an_uncommitted_write(
    admin_app,
    party_id,
    *,
    loser_revision: int,
    loser_updated_at: datetime | None,
    loser,
    holder_values,
):
    """Let `loser` run while another transaction holds an uncommitted write.

    The holder writes through the repository and commits only after the
    loser has reached its statement and waits for the holder. The loser
    has read the stored state before the holder commits.
    """
    holding, release = threading.Event(), threading.Event()
    holder_state: dict = {}
    loser_state: dict = {}

    def hold():
        holder_state['pid'] = _backend_pid()
        yellow, red = holder_values
        saved = repo.set_party_thresholds_flush(
            party_id,
            yellow_minutes=yellow,
            red_minutes=red,
            expected_revision=loser_revision,
            expected_updated_at=loser_updated_at,
            updated_at=NOW,
            updated_by=holder_state['user'],
        )
        assert saved is not None
        holding.set()
        assert release.wait(WAIT_TIMEOUT)
        repo.commit_session()

    def lose():
        loser_state['pid'] = _backend_pid()
        loser_state['started'].set()
        return loser()

    holder_state['user'] = _orga()
    loser_state['started'] = threading.Event()
    with _threads() as executor:
        holder = executor.submit(_in_app_context, admin_app, hold)
        try:
            assert holding.wait(WAIT_TIMEOUT), holder.result(timeout=1)
            future = executor.submit(_in_app_context, admin_app, lose)
            assert loser_state['started'].wait(WAIT_TIMEOUT)
            blocked = _wait_until_blocked_by(
                loser_state['pid'], holder_state['pid'], future
            )
        finally:
            release.set()
        holder.result(timeout=WAIT_TIMEOUT)
        outcome = future.result(timeout=WAIT_TIMEOUT)
    assert blocked, 'the save never waited for the uncommitted write'
    return outcome, holder_state['user']


def test_threshold_cas_refuses_stale_revision(admin_app, party_ids):
    party = party_ids()
    first_orga, second_orga = _orga(), _orga()

    # Two orgas open the form of a party without an override.
    first = _set(party, yellow=20, red=60, initiator=first_orga).unwrap()
    assert first.revision == 1
    second = _set(party, yellow=30, red=90, initiator=second_orga)
    assert second == Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(party) == [(20, 60, 1, first_orga)]

    # Two orgas open the form of revision 1.
    newer = _set(party, yellow=25, red=70, version=_version(first)).unwrap()
    assert newer.revision == 2
    stale = _set(
        party, yellow=30, red=90, version=_version(first), initiator=second_orga
    )
    assert stale == Err(THRESHOLDS_STALE_ERROR)
    assert _reset(party, version=_version(first)) == Err(THRESHOLDS_STALE_ERROR)
    assert [row[:3] for row in _committed_rows(party)] == [(25, 70, 2)]

    # A revision from the future is stale, too.
    future = {**_version(newer), 'expected_revision': 3}
    assert _set(party, version=future) == Err(THRESHOLDS_STALE_ERROR)
    assert _reset(party, version=future) == Err(THRESHOLDS_STALE_ERROR)
    assert [row[:3] for row in _committed_rows(party)] == [(25, 70, 2)]

    # Concurrent: the holder commits while the second save waits.
    concurrent = party_ids()
    saved = _set(concurrent, yellow=20, red=60).unwrap()
    outcome, winner = _race_a_save_against_an_uncommitted_write(
        admin_app,
        concurrent,
        loser_revision=saved.revision,
        loser_updated_at=saved.updated_at,
        loser=lambda: _set(
            concurrent, yellow=40, red=100, version=_version(saved)
        ),
        holder_values=(25, 70),
    )
    assert outcome == Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(concurrent) == [(25, 70, 2, winner)]

    # Concurrent: both start from a party without an override.
    inserting = party_ids()
    outcome, winner = _race_a_save_against_an_uncommitted_write(
        admin_app,
        inserting,
        loser_revision=0,
        loser_updated_at=None,
        loser=lambda: _set(inserting, yellow=40, red=100),
        holder_values=(25, 70),
    )
    assert outcome == Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(inserting) == [(25, 70, 1, winner)]


def test_a_reset_cannot_delete_a_newer_override(admin_app, party_ids):
    party = party_ids()
    saved = _set(party, yellow=20, red=60).unwrap()

    outcome, winner = _race_a_save_against_an_uncommitted_write(
        admin_app,
        party,
        loser_revision=saved.revision,
        loser_updated_at=saved.updated_at,
        loser=lambda: _reset(party, version=_version(saved)),
        holder_values=(25, 70),
    )

    assert outcome == Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(party) == [(25, 70, 2, winner)]
    effective = service.get_effective_dashboard_settings(party).unwrap()
    assert (effective.yellow_minutes, effective.red_minutes) == (25, 70)


@pytest.mark.parametrize('late_operation', ['set', 'reset'])
def test_threshold_cas_refuses_a_save_after_a_reset_and_resave(
    party_ids, late_operation
):
    party = party_ids()
    first = _set(party, yellow=20, red=60).unwrap()
    loaded_form = _version(first)

    # Another orga resets and saves a new override before the form is sent.
    assert _reset(party, version=_version(first)).is_ok()
    second = _set(party, yellow=25, red=70).unwrap()

    # The revision restarts, only the time tells the overrides apart.
    assert second.revision == first.revision
    assert second.updated_at != first.updated_at

    if late_operation == 'set':
        late = _set(party, yellow=30, red=90, version=loaded_form)
    else:
        late = _reset(party, version=loaded_form)

    assert late == Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(party) == [(25, 70, 1, second.updated_by)]


def _pause_the_loser_before(monkeypatch, name, reached, proceed):
    """Hold the loser thread right before the repository write `name`."""
    real = getattr(repo, name)

    def paused(*args, **kwargs):
        if getattr(_local, 'label', None) == 'loser':
            reached.set()
            assert proceed.wait(WAIT_TIMEOUT)
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, name, paused)


@pytest.mark.parametrize(
    ('late_operation', 'write'),
    [
        ('set', 'set_party_thresholds_flush'),
        ('reset', 'delete_party_thresholds_flush'),
    ],
)
def test_threshold_cas_refuses_a_reset_and_resave_between_read_and_write(
    admin_app, party_ids, monkeypatch, late_operation, write
):
    party = party_ids()
    first = _set(party, yellow=20, red=60).unwrap()
    loaded_form = _version(first)
    reached, proceed = threading.Event(), threading.Event()
    _pause_the_loser_before(monkeypatch, write, reached, proceed)

    def lose():
        _local.label = 'loser'
        try:
            if late_operation == 'set':
                return _set(party, yellow=30, red=90, version=loaded_form)
            return _reset(party, version=loaded_form)
        finally:
            _local.label = None

    with _threads() as executor:
        future = executor.submit(_in_app_context, admin_app, lose)
        try:
            # The late save has read the override it was loaded with.
            assert reached.wait(WAIT_TIMEOUT), future.result(timeout=1)
            assert _reset(party, version=_version(first)).is_ok()
            second = _set(party, yellow=25, red=70).unwrap()
        finally:
            proceed.set()
        outcome = future.result(timeout=WAIT_TIMEOUT)

    assert outcome == Err(THRESHOLDS_STALE_ERROR)
    assert _committed_rows(party) == [(25, 70, 1, second.updated_by)]


# -------------------------------------------------------------------- #
# derived tiers
# -------------------------------------------------------------------- #


@pytest.fixture
def make_tournament(tournament_party):
    def _make() -> TournamentID:
        tournament_id = TournamentID(uuid7())
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=tournament_party.id,
                name=f'Thresholds {tournament_id}',
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
                operational_clock_elapsed_us=30 * MINUTE_US,
                operational_clock_activated_at=NOW,
            )
        )
        return tournament_id

    return _make


def _dashboard_facts(tournament_id, match_id) -> dict[str, list[tuple]]:
    """Return every stored fact of the match the dashboard shows."""
    queries = {
        'episodes': (
            'SELECT * FROM lan_tournament_match_due_episodes'
            ' WHERE match_id = :match_id ORDER BY id'
        ),
        'acknowledgements': (
            'SELECT * FROM lan_tournament_match_escalation_acks'
            ' WHERE match_id = :match_id ORDER BY id'
        ),
        'pins': (
            'SELECT * FROM lan_tournament_match_dashboard_annotations'
            ' WHERE match_id = :match_id'
        ),
        'match': 'SELECT * FROM lan_tournament_matches WHERE id = :match_id',
        'clock': (
            'SELECT operational_clock_elapsed_us,'
            ' operational_clock_running_since, operational_clock_activated_at'
            ' FROM lan_tournaments WHERE id = :tournament_id'
        ),
    }
    with db.engine.connect() as connection:
        return {
            name: [
                tuple(row)
                for row in connection.execute(
                    text(sql),
                    {'match_id': match_id, 'tournament_id': tournament_id},
                )
            ]
            for name, sql in queries.items()
        }


def test_threshold_change_recolours_without_touching_episodes_or_acks(
    make_tournament, tournament_party
):
    party = tournament_party.id
    tournament_id = make_tournament()
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
        changed_at=NOW,
    )
    episode = MatchDueEpisode(
        id=MatchDueEpisodeID(uuid7()),
        tournament_id=tournament_id,
        match_id=match_id,
        pairing_key='participant:a|participant:b',
        opened_at=NOW,
        opened_clock_us=0,
    )
    repo.open_due_episode_flush(episode)
    repo.create_escalation_ack_flush(
        MatchEscalationAcknowledgement(
            id=MatchEscalationAcknowledgementID(uuid7()),
            episode_id=episode.id,
            tournament_id=tournament_id,
            match_id=match_id,
            revision=1,
            occurred_at=NOW,
            clock_us=20 * MINUTE_US,
            actor_id=_orga(),
            comment='checked',
        )
    )
    orga = _orga()
    repo.save_match_pin_flush(
        match_id,
        tournament_id,
        pinned_at=NOW,
        pinned_by=orga,
        updated_at=NOW,
        updated_by=orga,
        expected_revision=0,
    )
    repo.commit_session()

    before = _dashboard_facts(tournament_id, match_id)
    assert [len(rows) for rows in before.values()] == [1, 1, 1, 1, 1]
    last_changed_at = repo.find_match_fresh(match_id).last_changed_at
    assert last_changed_at == NOW

    def tiers() -> tuple[str, TrafficTier, TrafficTier]:
        settings = service.get_effective_dashboard_settings(party).unwrap()
        return (
            settings.threshold_source,
            derive_traffic_tier(20 * MINUTE_US, settings),
            derive_traffic_tier(50 * MINUTE_US, settings),
        )

    try:
        assert tiers() == (
            'deployment',
            TrafficTier.YELLOW,
            TrafficTier.RED,
        )

        saved = _set(party, yellow=30, red=60).unwrap()
        assert tiers() == ('party', TrafficTier.GREEN, TrafficTier.YELLOW)
        assert _dashboard_facts(tournament_id, match_id) == before

        saved = _set(party, yellow=5, red=10, version=_version(saved)).unwrap()
        assert tiers() == ('party', TrafficTier.RED, TrafficTier.RED)
        assert _dashboard_facts(tournament_id, match_id) == before

        assert _reset(party, version=_version(saved)).is_ok()
        assert tiers() == (
            'deployment',
            TrafficTier.YELLOW,
            TrafficTier.RED,
        )
        assert _dashboard_facts(tournament_id, match_id) == before
        assert repo.find_match_fresh(match_id).last_changed_at == (
            last_changed_at
        )
    finally:
        db.session.rollback()
        current = service.get_party_thresholds(party)
        if current is not None:
            _reset(party, version=_version(current))


def test_the_override_is_logged_through_the_app_logger(party_id, caplog):
    with caplog.at_level(logging.INFO, logger=service.__name__):
        saved = _set(party_id, yellow=20, red=60).unwrap()
        _reset(party_id, version=_version(saved))

    messages = [
        r.getMessage() for r in caplog.records if r.name == service.__name__
    ]
    assert len(messages) == 2
    assert all(repr(party_id) in message for message in messages)
    assert 'new yellow=20 red=60' in messages[0]
    assert 'old yellow=20 red=60' in messages[1]
