"""
tests.unit.services.lan_tournament.test_dashboard_settings_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, timezone, UTC
import logging

from flask import Flask
import pytest

from byceps.services.lan_tournament import (
    dashboard_config,
    tournament_dashboard_settings_service as service,
)
from byceps.services.lan_tournament.dashboard_config import (
    INVALID_RED_MINUTES_ERROR,
    INVALID_THRESHOLD_ORDER_ERROR,
    INVALID_YELLOW_MINUTES_ERROR,
    PAGE_SIZE_KEY,
    POLL_SECONDS_KEY,
    RED_MINUTES_KEY,
    YELLOW_MINUTES_KEY,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    DashboardSettings,
    PartyDashboardThresholds,
)
from byceps.services.lan_tournament.tournament_dashboard_settings_service import (
    THRESHOLDS_STALE_ERROR,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


PARTY = PartyID('gv-36-settings')
OTHER_PARTY = PartyID('gv-37-settings')
ORGA = UserID(generate_uuid())
OTHER_ORGA = UserID(generate_uuid())
OPERATION_TIME = datetime(2026, 10, 7, 12, 30, 0)

DEPLOYMENT_DEFAULTS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

LOGGER_NAME = service.__name__

_ALL_KEYS = (
    YELLOW_MINUTES_KEY,
    RED_MINUTES_KEY,
    POLL_SECONDS_KEY,
    PAGE_SIZE_KEY,
)


class FakeRepository:
    """A repository with the compare-and-set semantics of the real one.

    Writes are staged and become visible to other readers on commit only.
    """

    def __init__(self):
        self.committed: dict[PartyID, PartyDashboardThresholds] = {}
        self.staged: dict[PartyID, PartyDashboardThresholds | None] = {}
        self.calls: list[str] = []
        self.flush_kwargs: list[dict] = []
        self.delete_kwargs: list[dict] = []
        self.commit_error: Exception | None = None
        self.flush_error: Exception | None = None
        self.operation_time = OPERATION_TIME

    def find_party_thresholds(self, party_id):
        self.calls.append('find')
        if party_id in self.staged:
            return self.staged[party_id]
        return self.committed.get(party_id)

    def get_operation_time(self):
        self.calls.append('get_operation_time')
        # Like the server clock, never twice the same moment.
        moment = self.operation_time
        self.operation_time += timedelta(seconds=1)
        return moment

    def set_party_thresholds_flush(
        self,
        party_id,
        *,
        yellow_minutes,
        red_minutes,
        expected_revision,
        expected_updated_at,
        updated_at,
        updated_by,
    ):
        self.calls.append('set_flush')
        self.flush_kwargs.append(
            {
                'yellow_minutes': yellow_minutes,
                'red_minutes': red_minutes,
                'expected_revision': expected_revision,
                'expected_updated_at': expected_updated_at,
                'updated_at': updated_at,
                'updated_by': updated_by,
            }
        )
        if self.flush_error is not None:
            raise self.flush_error

        current = self.committed.get(party_id)
        if expected_revision == 0:
            if current is not None:
                return None
            revision = 1
        else:
            if not self._is_version(
                current, expected_revision, expected_updated_at
            ):
                return None
            revision = current.revision + 1

        row = PartyDashboardThresholds(
            party_id=party_id,
            yellow_minutes=yellow_minutes,
            red_minutes=red_minutes,
            revision=revision,
            updated_at=updated_at,
            updated_by=updated_by,
        )
        self.staged[party_id] = row
        return row

    def delete_party_thresholds_flush(
        self, party_id, *, expected_revision, expected_updated_at
    ):
        self.calls.append('delete_flush')
        self.delete_kwargs.append(
            {
                'expected_revision': expected_revision,
                'expected_updated_at': expected_updated_at,
            }
        )
        if self.flush_error is not None:
            raise self.flush_error

        current = self.committed.get(party_id)
        if not self._is_version(
            current, expected_revision, expected_updated_at
        ):
            return False

        self.staged[party_id] = None
        return True

    @staticmethod
    def _is_version(current, revision, updated_at):
        # The compare-and-set predicate: revision and time, as in SQL.
        return (
            current is not None
            and current.revision == revision
            and current.updated_at == updated_at
        )

    def commit_session(self):
        self.calls.append('commit')
        if self.commit_error is not None:
            raise self.commit_error

        for party_id, row in self.staged.items():
            if row is None:
                self.committed.pop(party_id, None)
            else:
                self.committed[party_id] = row
        self.staged.clear()

    def rollback_session(self):
        self.calls.append('rollback')
        self.staged.clear()

    def seed(self, party_id, yellow_minutes, red_minutes, revision):
        self.committed[party_id] = PartyDashboardThresholds(
            party_id=party_id,
            yellow_minutes=yellow_minutes,
            red_minutes=red_minutes,
            revision=revision,
            updated_at=OPERATION_TIME,
            updated_by=OTHER_ORGA,
        )


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def repository(monkeypatch):
    fake = FakeRepository()
    monkeypatch.setattr(service, 'tournament_repository', fake)
    return fake


@pytest.fixture
def app():
    app = Flask(__name__)
    with app.app_context():
        yield app


def _set(party_id=PARTY, **overrides):
    arguments = {
        'yellow_minutes': 20,
        'red_minutes': 60,
        'expected_revision': 0,
        'expected_updated_at': None,
        'initiator_id': ORGA,
    }
    return service.set_party_thresholds(party_id, **(arguments | overrides))


def _reset(party_id=PARTY, **overrides):
    # The default is the override that `seed` stores at revision 1.
    arguments = {
        'expected_revision': 1,
        'expected_updated_at': OPERATION_TIME,
        'initiator_id': ORGA,
    }
    return service.reset_party_thresholds(party_id, **(arguments | overrides))


def _version(thresholds: PartyDashboardThresholds) -> dict:
    """Return what a form carries to name the override it was loaded with."""
    return {
        'expected_revision': thresholds.revision,
        'expected_updated_at': thresholds.updated_at,
    }


def _version_at(revision: int) -> dict:
    """Return the version of a seeded override, or of no override at 0."""
    return {
        'expected_revision': revision,
        'expected_updated_at': OPERATION_TIME if revision else None,
    }


def _records(caplog):
    return [r for r in caplog.records if r.name == LOGGER_NAME]


# fmt: off
@pytest.mark.parametrize(
    ('config', 'deployment'),
    [
        (
            {},
            DEPLOYMENT_DEFAULTS,
        ),
        (
            {
                YELLOW_MINUTES_KEY: 10,
                RED_MINUTES_KEY: 90,
                POLL_SECONDS_KEY: 120,
                PAGE_SIZE_KEY: 25,
            },
            DashboardSettings(
                yellow_minutes=10,
                red_minutes=90,
                poll_seconds=120,
                page_size=25,
                threshold_source='deployment',
            ),
        ),
    ],
    ids=['plain default', 'configured deployment'],
)
# fmt: on
def test_party_override_beats_deployment_default_and_reset_restores_it(
    app, repository, config, deployment
):
    app.config.update(config)

    assert service.get_effective_dashboard_settings(PARTY) == Ok(deployment)
    assert service.get_party_thresholds(PARTY) is None

    saved = _set(yellow_minutes=20, red_minutes=61).unwrap()

    assert service.get_party_thresholds(PARTY) == saved
    effective = service.get_effective_dashboard_settings(PARTY).unwrap()
    assert effective.yellow_minutes == 20
    assert effective.red_minutes == 61
    assert effective.threshold_source == 'party'
    # Only the thresholds come from the party.
    assert effective.poll_seconds == deployment.poll_seconds
    assert effective.page_size == deployment.page_size

    # Other parties keep the deployment default.
    assert service.get_effective_dashboard_settings(OTHER_PARTY) == Ok(
        deployment
    )
    assert service.get_party_thresholds(OTHER_PARTY) is None

    assert _reset(**_version(saved)) == Ok(None)

    assert service.get_party_thresholds(PARTY) is None
    assert service.get_effective_dashboard_settings(PARTY) == Ok(deployment)


def test_party_override_is_replaced_by_the_next_save(app, repository):
    first = _set(yellow_minutes=20, red_minutes=60).unwrap()
    second = _set(yellow_minutes=25, red_minutes=70, **_version(first)).unwrap()

    assert second.revision == first.revision + 1
    effective = service.get_effective_dashboard_settings(PARTY).unwrap()
    assert (effective.yellow_minutes, effective.red_minutes) == (25, 70)


# fmt: off
@pytest.mark.parametrize(
    'config',
    [
        {YELLOW_MINUTES_KEY: 0},
        {POLL_SECONDS_KEY: 4},
        {PAGE_SIZE_KEY: 101},
        {RED_MINUTES_KEY: True},
    ],
    ids=['yellow', 'poll', 'page size', 'bool red'],
)
# fmt: on
def test_invalid_deployment_config_is_an_error_even_with_an_override(
    app, repository, config
):
    repository.seed(PARTY, 20, 60, 1)
    app.config.update(config)

    for party_id in (PARTY, OTHER_PARTY):
        result = service.get_effective_dashboard_settings(party_id)

        assert result.is_err()
        assert result == dashboard_config.get_dashboard_settings()


# fmt: off
@pytest.mark.parametrize(
    ('yellow', 'red', 'error'),
    [
        # The default pair given in seconds, not minutes.
        (900, 2700, INVALID_RED_MINUTES_ERROR),
        (2700, 5400, INVALID_YELLOW_MINUTES_ERROR),
        (901.0, 45, INVALID_YELLOW_MINUTES_ERROR),
        (15.5, 45, INVALID_YELLOW_MINUTES_ERROR),
        (15, 45.5, INVALID_RED_MINUTES_ERROR),
        ('15', 45, INVALID_YELLOW_MINUTES_ERROR),
        (15, '45', INVALID_RED_MINUTES_ERROR),
        (None, 45, INVALID_YELLOW_MINUTES_ERROR),
        (15, None, INVALID_RED_MINUTES_ERROR),
        (True, 45, INVALID_YELLOW_MINUTES_ERROR),
        (15, True, INVALID_RED_MINUTES_ERROR),
        ([15], 45, INVALID_YELLOW_MINUTES_ERROR),
        (15, {'a': 1}, INVALID_RED_MINUTES_ERROR),
        # Bounds.
        (0, 45, INVALID_YELLOW_MINUTES_ERROR),
        (-5, 45, INVALID_YELLOW_MINUTES_ERROR),
        (1441, 1442, INVALID_YELLOW_MINUTES_ERROR),
        (15, 0, INVALID_RED_MINUTES_ERROR),
        (15, 1441, INVALID_RED_MINUTES_ERROR),
        (15, 10**30, INVALID_RED_MINUTES_ERROR),
        # Order.
        (45, 45, INVALID_THRESHOLD_ORDER_ERROR),
        (60, 45, INVALID_THRESHOLD_ORDER_ERROR),
        (1440, 1440, INVALID_THRESHOLD_ORDER_ERROR),
    ],
)
# fmt: on
def test_set_thresholds_validates_bounds_before_storage(
    repository, yellow, red, error
):
    result = _set(yellow_minutes=yellow, red_minutes=red)

    assert result == Err(error)
    # Not even a read: nothing was stored, flushed or committed.
    assert repository.calls == []
    assert repository.committed == {}
    assert repository.staged == {}


# fmt: off
@pytest.mark.parametrize(
    ('yellow', 'red'),
    [(1, 2), (1, 1440), (1439, 1440), (15, 45)],
)
# fmt: on
def test_set_thresholds_accepts_whole_minutes_in_bounds(
    repository, yellow, red
):
    saved = _set(yellow_minutes=yellow, red_minutes=red).unwrap()

    assert (saved.yellow_minutes, saved.red_minutes) == (yellow, red)
    assert repository.committed[PARTY] == saved


# fmt: off
@pytest.mark.parametrize(
    'revision',
    [-1, 2**31, 10**30, True, False, 1.0, '1', None, [1]],
    ids=repr,
)
# fmt: on
def test_unusable_revisions_are_stale_without_touching_storage(
    repository, revision
):
    repository.seed(PARTY, 20, 60, 1)

    version = {
        'expected_revision': revision,
        'expected_updated_at': OPERATION_TIME,
    }

    assert _set(**version) == Err(THRESHOLDS_STALE_ERROR)
    assert _reset(**version) == Err(THRESHOLDS_STALE_ERROR)

    assert repository.calls == []
    assert repository.committed[PARTY].yellow_minutes == 20


# fmt: off
@pytest.mark.parametrize(
    ('stored_revision', 'expected_revision'),
    [
        (None, 1),
        (None, 2),
        (1, 0),
        (2, 1),
        (2, 3),
    ],
)
# fmt: on
def test_set_refuses_a_stale_revision_without_writing(
    repository, stored_revision, expected_revision
):
    if stored_revision is not None:
        repository.seed(PARTY, 20, 60, stored_revision)
    before = dict(repository.committed)

    result = _set(
        yellow_minutes=30, red_minutes=90, **_version_at(expected_revision)
    )

    assert result == Err(THRESHOLDS_STALE_ERROR)
    assert 'set_flush' not in repository.calls
    assert 'commit' not in repository.calls
    assert repository.committed == before


# fmt: off
@pytest.mark.parametrize(
    ('revision', 'updated_at'),
    [
        (0, OPERATION_TIME),
        (0, 0),
        (1, None),
        (7, None),
        (1, OPERATION_TIME.isoformat()),
        (1, OPERATION_TIME.timestamp()),
        (1, int(OPERATION_TIME.timestamp())),
        (1, OPERATION_TIME.date()),
        (1, [OPERATION_TIME]),
        (1, True),
    ],
    ids=repr,
)
# fmt: on
def test_unusable_versions_are_stale_without_touching_storage(
    repository, revision, updated_at
):
    repository.seed(PARTY, 20, 60, 1)
    version = {'expected_revision': revision, 'expected_updated_at': updated_at}

    assert _set(**version) == Err(THRESHOLDS_STALE_ERROR)
    assert _reset(**version) == Err(THRESHOLDS_STALE_ERROR)

    assert repository.calls == []
    assert repository.committed[PARTY].yellow_minutes == 20


# fmt: off
@pytest.mark.parametrize(
    'other_time',
    [
        OPERATION_TIME + timedelta(microseconds=1),
        OPERATION_TIME - timedelta(microseconds=1),
        OPERATION_TIME + timedelta(days=1),
        # The same wall clock, read as an hour off.
        OPERATION_TIME.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
    ids=repr,
)
# fmt: on
def test_a_different_updated_at_is_stale_even_at_the_same_revision(
    repository, other_time
):
    repository.seed(PARTY, 20, 60, 2)
    version = {'expected_revision': 2, 'expected_updated_at': other_time}

    assert _set(yellow_minutes=30, red_minutes=90, **version) == Err(
        THRESHOLDS_STALE_ERROR
    )
    assert _reset(**version) == Err(THRESHOLDS_STALE_ERROR)

    assert 'set_flush' not in repository.calls
    assert 'delete_flush' not in repository.calls
    assert 'commit' not in repository.calls
    assert repository.committed[PARTY].yellow_minutes == 20


# fmt: off
@pytest.mark.parametrize(
    'form_time',
    [
        OPERATION_TIME,
        OPERATION_TIME.replace(tzinfo=UTC),
        (OPERATION_TIME + timedelta(hours=2)).replace(
            tzinfo=timezone(timedelta(hours=2))
        ),
    ],
    ids=['naive utc', 'aware utc', 'aware offset, same moment'],
)
# fmt: on
def test_updated_at_is_compared_as_the_same_moment(repository, form_time):
    repository.seed(PARTY, 20, 60, 2)

    saved = _set(
        yellow_minutes=30,
        red_minutes=90,
        expected_revision=2,
        expected_updated_at=form_time,
    ).unwrap()

    assert (saved.yellow_minutes, saved.revision) == (30, 3)


@pytest.mark.parametrize('late_operation', ['set', 'reset'])
def test_a_reset_and_resave_after_the_form_was_loaded_is_stale(
    repository, late_operation
):
    first = _set(yellow_minutes=20, red_minutes=60).unwrap()
    loaded_form = _version(first)

    # Another orga resets and saves a new override before the form is sent.
    assert _reset(**_version(first)) == Ok(None)
    second = _set(yellow_minutes=25, red_minutes=70).unwrap()

    # The revision restarts, only the time tells the overrides apart.
    assert second.revision == first.revision
    assert second.updated_at != first.updated_at

    if late_operation == 'set':
        late = _set(yellow_minutes=30, red_minutes=90, **loaded_form)
    else:
        late = _reset(**loaded_form)

    assert late == Err(THRESHOLDS_STALE_ERROR)
    assert repository.committed[PARTY] == second
    assert repository.calls[-1] == 'find'


def test_the_expected_version_reaches_the_repository(repository):
    repository.seed(PARTY, 20, 60, 4)
    repository.seed(OTHER_PARTY, 20, 60, 2)

    _set(yellow_minutes=30, red_minutes=90, **_version_at(4)).unwrap()
    assert _reset(OTHER_PARTY, **_version_at(2)) == Ok(None)

    (flushed,) = repository.flush_kwargs
    assert flushed['expected_revision'] == 4
    assert flushed['expected_updated_at'] == OPERATION_TIME
    assert repository.delete_kwargs == [
        {'expected_revision': 2, 'expected_updated_at': OPERATION_TIME}
    ]


@pytest.mark.parametrize('late_operation', ['set', 'reset'])
def test_a_reset_and_resave_between_read_and_write_loses_on_the_time(
    repository, late_operation
):
    repository.seed(PARTY, 20, 60, 1)
    real_find = repository.find_party_thresholds

    def find_then_reset_and_resave(party_id):
        row = real_find(party_id)
        # Two other saves commit between the read and the write: the
        # revision is 1 again, only the time differs.
        repository.committed[party_id] = PartyDashboardThresholds(
            party_id=party_id,
            yellow_minutes=25,
            red_minutes=70,
            revision=1,
            updated_at=OPERATION_TIME + timedelta(minutes=5),
            updated_by=OTHER_ORGA,
        )
        return row

    repository.find_party_thresholds = find_then_reset_and_resave

    if late_operation == 'set':
        result = _set(yellow_minutes=30, red_minutes=90, **_version_at(1))
    else:
        result = _reset(**_version_at(1))

    assert result == Err(THRESHOLDS_STALE_ERROR)
    assert repository.calls[-1] == 'rollback'
    assert 'commit' not in repository.calls
    assert repository.committed[PARTY].yellow_minutes == 25


def test_set_that_loses_the_compare_and_set_rolls_back(repository):
    repository.seed(PARTY, 20, 60, 1)
    real_find = repository.find_party_thresholds

    def find_then_lose_the_race(party_id):
        row = real_find(party_id)
        # Another save commits between the read and the write.
        repository.seed(party_id, 25, 70, 2)
        return row

    repository.find_party_thresholds = find_then_lose_the_race

    result = _set(yellow_minutes=30, red_minutes=90, **_version_at(1))

    assert result == Err(THRESHOLDS_STALE_ERROR)
    assert repository.calls[-2:] == ['set_flush', 'rollback']
    assert 'commit' not in repository.calls
    newer = repository.committed[PARTY]
    assert (newer.yellow_minutes, newer.red_minutes, newer.revision) == (
        25,
        70,
        2,
    )


def test_set_flushes_once_commits_once_and_stores_actor_and_time(repository):
    saved = _set(
        yellow_minutes=20, red_minutes=60, initiator_id=OTHER_ORGA
    ).unwrap()

    assert repository.calls == [
        'find',
        'get_operation_time',
        'set_flush',
        'commit',
    ]
    assert repository.flush_kwargs == [
        {
            'yellow_minutes': 20,
            'red_minutes': 60,
            'expected_revision': 0,
            'expected_updated_at': None,
            'updated_at': OPERATION_TIME,
            'updated_by': OTHER_ORGA,
        }
    ]
    assert saved.updated_at == OPERATION_TIME
    assert saved.updated_by == OTHER_ORGA
    assert saved.revision == 1


@pytest.mark.parametrize('failing_step', ['flush', 'commit'])
def test_failed_set_rolls_back_stores_nothing_and_logs_nothing(
    repository, caplog, failing_step
):
    repository.seed(PARTY, 20, 60, 1)
    failure = RuntimeError(failing_step)
    if failing_step == 'flush':
        repository.flush_error = failure
    else:
        repository.commit_error = failure

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(RuntimeError, match=failing_step):
            _set(yellow_minutes=30, red_minutes=90, **_version_at(1))

    assert repository.calls[-1] == 'rollback'
    assert repository.staged == {}
    assert repository.committed[PARTY].yellow_minutes == 20
    assert _records(caplog) == []


def test_reset_removes_the_override_and_commits_once(repository):
    repository.seed(PARTY, 20, 60, 3)

    assert _reset(**_version_at(3)) == Ok(None)

    assert repository.calls == ['find', 'delete_flush', 'commit']
    assert PARTY not in repository.committed


# fmt: off
@pytest.mark.parametrize(
    ('stored_revision', 'expected_revision'),
    [
        (None, 1),
        (3, 0),
        (3, 2),
        (3, 4),
    ],
)
# fmt: on
def test_reset_refuses_a_stale_revision_without_writing(
    repository, stored_revision, expected_revision
):
    if stored_revision is not None:
        repository.seed(PARTY, 20, 60, stored_revision)
    before = dict(repository.committed)

    result = _reset(**_version_at(expected_revision))

    assert result == Err(THRESHOLDS_STALE_ERROR)
    assert 'delete_flush' not in repository.calls
    assert 'commit' not in repository.calls
    assert repository.committed == before


def test_reset_without_an_override_is_already_done(repository, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert _reset(**_version_at(0)) == Ok(None)

    assert repository.calls == ['find']
    assert _records(caplog) == []


def test_reset_that_loses_the_compare_and_set_rolls_back(repository):
    repository.seed(PARTY, 20, 60, 1)
    real_find = repository.find_party_thresholds

    def find_then_lose_the_race(party_id):
        row = real_find(party_id)
        repository.seed(party_id, 25, 70, 2)
        return row

    repository.find_party_thresholds = find_then_lose_the_race

    assert _reset(**_version_at(1)) == Err(THRESHOLDS_STALE_ERROR)

    assert repository.calls[-2:] == ['delete_flush', 'rollback']
    assert repository.committed[PARTY].revision == 2


@pytest.mark.parametrize('failing_step', ['flush', 'commit'])
def test_failed_reset_rolls_back_keeps_the_override_and_logs_nothing(
    repository, caplog, failing_step
):
    repository.seed(PARTY, 20, 60, 1)
    failure = RuntimeError(failing_step)
    if failing_step == 'flush':
        repository.flush_error = failure
    else:
        repository.commit_error = failure

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(RuntimeError, match=failing_step):
            _reset(**_version_at(1))

    assert repository.calls[-1] == 'rollback'
    assert PARTY in repository.committed
    assert _records(caplog) == []


def test_threshold_changes_are_app_logged(repository, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        first = _set(
            yellow_minutes=20, red_minutes=60, initiator_id=ORGA
        ).unwrap()
        second = _set(
            yellow_minutes=25,
            red_minutes=70,
            initiator_id=OTHER_ORGA,
            **_version(first),
        ).unwrap()
        _reset(initiator_id=ORGA, **_version(second))

    records = _records(caplog)
    assert [r.levelno for r in records] == [logging.INFO] * 3
    created, changed, removed = (r.getMessage() for r in records)

    # The first override replaces the deployment default.
    assert repr(PARTY) in created
    assert str(ORGA) in created
    assert 'old deployment default, new yellow=20 red=60' in created

    assert repr(PARTY) in changed
    assert str(OTHER_ORGA) in changed
    assert str(ORGA) not in changed
    assert 'old yellow=20 red=60, new yellow=25 red=70' in changed

    assert repr(PARTY) in removed
    assert str(ORGA) in removed
    assert 'old yellow=25 red=70, new deployment default' in removed


def test_a_party_id_cannot_forge_a_log_line(repository, caplog):
    forged = PartyID('gv-36\nWARNING forged entry')

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        _set(party_id=forged)

    (record,) = _records(caplog)
    assert '\n' not in record.getMessage()


def test_refused_changes_are_not_logged(repository, caplog):
    repository.seed(PARTY, 20, 60, 2)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert _set(yellow_minutes=0).is_err()
        assert _set(**_version_at(1)).is_err()
        assert _reset(**_version_at(1)).is_err()

    assert _records(caplog) == []
