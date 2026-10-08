from datetime import datetime, timedelta, timezone, UTC
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CheckConstraint, UniqueConstraint

from byceps.services.lan_tournament import tournament_repository as repository
from byceps.services.lan_tournament.dbmodels.dashboard import (
    DbDashboardPartyThresholds,
    DbMatchDashboardAnnotation,
    DbMatchDueEpisode,
    DbMatchEscalationAck,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
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
    normalize_utc,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


NOW = datetime(2026, 10, 7, 12, 0, 0)
THREE_DAYS_US = 3 * 24 * 60 * 60 * 1_000_000
assert THREE_DAYS_US == 259_200_000_000

TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())
ACTOR_ID = UserID(generate_uuid())
PARTY_ID = PartyID('lan-party-unit')


@pytest.fixture
def session():
    mock = MagicMock()
    with patch.object(repository.db, 'session', mock):
        yield mock


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={'literal_binds': False},
        )
    )


def _executed(session):
    """Return the one statement the repository handed to the session."""
    calls = session.execute.call_args_list + session.scalars.call_args_list
    assert len(calls) == 1
    return calls[0].args[0]


def _bound(statement) -> dict:
    return statement.compile(dialect=postgresql.dialect()).params


def _check_sql(model, name: str) -> str:
    [constraint] = [
        c
        for c in model.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == name
    ]
    return str(constraint.sqltext)


def _episode(**overrides) -> MatchDueEpisode:
    values = dict(
        id=MatchDueEpisodeID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        match_id=MATCH_ID,
        pairing_key='participant:a|participant:b',
        opened_at=NOW,
        opened_clock_us=1_000,
    )
    return MatchDueEpisode(**{**values, **overrides})


def _ack(**overrides) -> MatchEscalationAcknowledgement:
    values = dict(
        id=MatchEscalationAcknowledgementID(generate_uuid()),
        episode_id=MatchDueEpisodeID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        match_id=MATCH_ID,
        revision=1,
        occurred_at=NOW,
        clock_us=2_000,
        actor_id=ACTOR_ID,
        comment='Contacted both captains.',
    )
    return MatchEscalationAcknowledgement(**{**values, **overrides})


def _db_tournament(**attributes) -> DbTournament:
    row = DbTournament(TOURNAMENT_ID, PARTY_ID, 'Cup', NOW)
    for name, value in attributes.items():
        setattr(row, name, value)
    return row


# -------------------------------------------------------------------- #
# mapper
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'elapsed_us, running_since, activated_at',
    [
        (0, None, None),
        (45 * 60 * 1_000_000, None, NOW),
        (THREE_DAYS_US, NOW, NOW - timedelta(days=3)),
    ],
    ids=['unknown-history', 'frozen', 'running'],
)
# fmt: on
def test_mapper_roundtrips_timing_and_nullable_history(
    elapsed_us, running_since, activated_at
):
    row = _db_tournament(
        operational_clock_elapsed_us=elapsed_us,
        operational_clock_running_since=running_since,
        operational_clock_activated_at=activated_at,
    )

    tournament = repository._db_tournament_to_tournament(row)

    assert tournament.operational_clock_elapsed_us == elapsed_us
    assert tournament.operational_clock_running_since == running_since
    assert tournament.operational_clock_activated_at == activated_at


def test_a_new_tournament_row_maps_to_an_unknown_clock():
    tournament = repository._db_tournament_to_tournament(_db_tournament())

    assert tournament.operational_clock_elapsed_us == 0
    assert tournament.operational_clock_running_since is None
    assert tournament.operational_clock_activated_at is None


# fmt: off
@pytest.mark.parametrize('last_changed_at', [None, NOW], ids=['unknown', 'known'])
# fmt: on
def test_match_mapper_keeps_unknown_last_change_unknown(last_changed_at):
    row = DbTournamentMatch(
        MATCH_ID, TOURNAMENT_ID, NOW, last_changed_at=last_changed_at
    )

    match = repository._db_match_to_match(row)

    assert match.last_changed_at == last_changed_at


# fmt: off
@pytest.mark.parametrize(
    'closed_at, closed_clock_us, ack_revision',
    [(None, None, 0), (NOW, THREE_DAYS_US, 3)],
    ids=['open', 'closed'],
)
# fmt: on
def test_episode_mapper_roundtrips_every_fact(
    closed_at, closed_clock_us, ack_revision
):
    episode = _episode(
        closed_at=closed_at,
        closed_clock_us=closed_clock_us,
        ack_revision=ack_revision,
    )
    row = DbMatchDueEpisode(
        episode.id,
        episode.tournament_id,
        episode.match_id,
        episode.pairing_key,
        episode.opened_at,
        episode.opened_clock_us,
        closed_at=closed_at,
        closed_clock_us=closed_clock_us,
        ack_revision=ack_revision,
    )

    assert repository._db_episode_to_episode(row) == episode


# fmt: off
@pytest.mark.parametrize('comment', [None, 'Waiting for the captain.'])
# fmt: on
def test_ack_mapper_roundtrips_every_fact(comment):
    ack = _ack(comment=comment, clock_us=THREE_DAYS_US)
    row = DbMatchEscalationAck(
        ack.id,
        ack.episode_id,
        ack.tournament_id,
        ack.match_id,
        ack.actor_id,
        ack.revision,
        ack.occurred_at,
        ack.clock_us,
        comment=comment,
    )

    assert repository._db_ack_to_ack(row) == ack


# fmt: off
@pytest.mark.parametrize(
    'pinned_at, pinned_by',
    [(None, None), (NOW, ACTOR_ID)],
    ids=['unpinned', 'pinned'],
)
# fmt: on
def test_pin_mapper_roundtrips_every_fact(pinned_at, pinned_by):
    row = DbMatchDashboardAnnotation(
        MATCH_ID,
        TOURNAMENT_ID,
        NOW,
        ACTOR_ID,
        revision=4,
        pinned_at=pinned_at,
        pinned_by=pinned_by,
    )

    state = repository._db_pin_to_pin_state(row)

    assert (state.match_id, state.tournament_id) == (MATCH_ID, TOURNAMENT_ID)
    assert state.revision == 4
    assert (state.pinned_at, state.pinned_by) == (pinned_at, pinned_by)
    assert (state.updated_at, state.updated_by) == (NOW, ACTOR_ID)


def test_threshold_mapper_roundtrips_every_fact():
    row = DbDashboardPartyThresholds(
        PARTY_ID, 20, 60, NOW, ACTOR_ID, revision=7
    )

    thresholds = repository._db_thresholds_to_thresholds(row)

    assert thresholds.party_id == PARTY_ID
    assert (thresholds.yellow_minutes, thresholds.red_minutes) == (20, 60)
    assert thresholds.revision == 7
    assert (thresholds.updated_at, thresholds.updated_by) == (NOW, ACTOR_ID)


# -------------------------------------------------------------------- #
# creation mapping
# -------------------------------------------------------------------- #


def _tournament_dto(**overrides) -> Tournament:
    values = dict(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID,
        name='Cup',
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
        tournament_status=TournamentStatus.DRAFT,
        game_format=None,
        elimination_mode=None,
    )
    return Tournament(**{**values, **overrides})


def test_create_tournament_persists_the_clock_facts(session):
    repository.create_tournament(
        _tournament_dto(
            operational_clock_elapsed_us=THREE_DAYS_US,
            operational_clock_running_since=NOW,
            operational_clock_activated_at=NOW - timedelta(days=3),
        ),
        commit=False,
    )

    [row] = [call.args[0] for call in session.add.call_args_list]
    assert row.operational_clock_elapsed_us == THREE_DAYS_US
    assert row.operational_clock_running_since == NOW
    assert row.operational_clock_activated_at == NOW - timedelta(days=3)
    session.commit.assert_not_called()


def test_create_tournament_defaults_to_an_unknown_clock(session):
    repository.create_tournament(_tournament_dto(), commit=False)

    [row] = [call.args[0] for call in session.add.call_args_list]
    assert row.operational_clock_elapsed_us == 0
    assert row.operational_clock_running_since is None
    assert row.operational_clock_activated_at is None


def test_create_tournament_stores_aware_clock_times_as_naive_utc(session):
    plus_two = timezone(timedelta(hours=2))

    repository.create_tournament(
        _tournament_dto(
            operational_clock_running_since=datetime(
                2026, 10, 7, 14, 0, tzinfo=plus_two
            ),
        ),
        commit=False,
    )

    [row] = [call.args[0] for call in session.add.call_args_list]
    assert row.operational_clock_running_since == datetime(2026, 10, 7, 12, 0)
    assert row.operational_clock_running_since.tzinfo is None


def _match_dto(**overrides) -> TournamentMatch:
    values = dict(
        id=MATCH_ID,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )
    return TournamentMatch(**{**values, **overrides})


def _created_match(session) -> DbTournamentMatch:
    [row] = [call.args[0] for call in session.add.call_args_list]
    return row


def test_create_match_keeps_the_last_change_the_match_carries(session):
    carried = NOW + timedelta(minutes=1)

    repository.create_match(
        _match_dto(last_changed_at=carried),
        changed_at=NOW + timedelta(minutes=2),
    )

    assert _created_match(session).last_changed_at == carried
    session.execute.assert_not_called()


def test_create_match_takes_the_shared_operation_time_when_given(session):
    repository.create_match(_match_dto(), changed_at=NOW)

    assert _created_match(session).last_changed_at == NOW
    session.execute.assert_not_called()


def test_create_match_initializes_with_the_server_operation_time(session):
    session.execute.return_value.scalar_one.return_value = NOW

    repository.create_match(_match_dto())

    assert _created_match(session).last_changed_at == NOW
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


# -------------------------------------------------------------------- #
# operation time and last change
# -------------------------------------------------------------------- #


def test_operation_time_is_the_server_clock_in_utc(session):
    session.execute.return_value.scalar_one.return_value = NOW

    assert repository.get_operation_time() == NOW

    sql = _sql(_executed(session)).lower()
    assert 'clock_timestamp()' in sql
    assert 'timezone(' in sql
    assert 'now()' not in sql


def test_touch_is_monotonic_and_fills_unknown_history(session):
    other = NOW + timedelta(seconds=1)

    repository.touch_matches_last_changed_flush(
        {MATCH_ID, TournamentMatchID(generate_uuid())}, changed_at=other
    )

    statement = _executed(session)
    sql = _sql(statement).lower()
    assert 'greatest(lan_tournament_matches.last_changed_at' in sql
    assert 'where lan_tournament_matches.id in' in sql
    assert other in _bound(statement).values()
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


# fmt: off
@pytest.mark.parametrize('empty', [[], (), set(), frozenset()])
# fmt: on
def test_empty_collections_touch_nothing(session, empty):
    repository.touch_matches_last_changed_flush(empty, changed_at=NOW)
    repository.close_due_episodes_flush(empty, occurred_at=NOW, clock_us=1)
    repository.retire_dashboard_matches_flush(empty, occurred_at=NOW)

    session.execute.assert_not_called()
    session.flush.assert_not_called()


def test_touch_normalizes_aware_times_to_naive_utc(session):
    aware = datetime(2026, 10, 7, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    repository.touch_matches_last_changed_flush([MATCH_ID], changed_at=aware)

    stored = [
        value
        for value in _bound(_executed(session)).values()
        if isinstance(value, datetime)
    ]
    assert stored == [datetime(2026, 10, 7, 12, 0)]


# fmt: off
@pytest.mark.parametrize(
    'value',
    [
        datetime(2026, 10, 7, 12, 0),
        datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 7, 14, 0, tzinfo=timezone(timedelta(hours=2))),
        datetime(2026, 10, 7, 7, 0, tzinfo=timezone(timedelta(hours=-5))),
        datetime(2026, 10, 7, 12, 0, 0, 123_456, tzinfo=UTC),
    ],
    ids=['naive', 'utc', 'plus-2', 'minus-5', 'microseconds'],
)
# fmt: on
def test_repository_normalizer_agrees_with_the_domain_normalizer(value):
    assert repository._naive_utc(value) == normalize_utc(value)
    assert repository._naive_utc(value).tzinfo is None


# -------------------------------------------------------------------- #
# episodes
# -------------------------------------------------------------------- #


def test_only_one_open_episode_per_match():
    [index] = [
        i
        for i in DbMatchDueEpisode.__table__.indexes
        if i.name == 'uq_lan_tournament_due_episodes_open_match'
    ]
    assert index.unique
    assert [c.name for c in index.columns] == ['match_id']
    where = index.dialect_options['postgresql']['where']
    assert str(where) == 'closed_at IS NULL'


def test_open_due_episode_is_stored_whole_and_flush_only(session):
    episode = _episode()

    repository.open_due_episode_flush(episode)

    [row] = [call.args[0] for call in session.add.call_args_list]
    assert repository._db_episode_to_episode(row) == episode
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_list_open_due_episodes_reads_fresh_and_open_only(session):
    row = DbMatchDueEpisode(
        MatchDueEpisodeID(generate_uuid()),
        TOURNAMENT_ID,
        MATCH_ID,
        'participant:a|participant:b',
        NOW,
        1_000,
    )
    session.scalars.return_value.all.return_value = [row]

    [episode] = repository.list_open_due_episodes(TOURNAMENT_ID)

    statement = _executed(session)
    assert statement.get_execution_options()['populate_existing'] is True
    assert 'closed_at IS NULL' in _sql(statement)
    assert episode.match_id == MATCH_ID and episode.closed_at is None


def test_close_pair_checks_reject_partial_nulls(session):
    check = _check_sql(
        DbMatchDueEpisode, 'ck_lan_tournament_due_episodes_close_pair'
    )
    assert check == (
        '(closed_at IS NULL AND closed_clock_us IS NULL)'
        ' OR (closed_at IS NOT NULL AND closed_clock_us IS NOT NULL)'
    )

    repository.close_due_episodes_flush(
        [MATCH_ID], occurred_at=NOW, clock_us=THREE_DAYS_US
    )

    statement = _executed(session)
    assert 'closed_at IS NULL' in _sql(statement).split('WHERE')[1]
    assigned = _sql(statement).split('WHERE')[0]
    assert 'closed_at=' in assigned.replace(' ', '')
    assert 'closed_clock_us=' in assigned.replace(' ', '')
    bound = _bound(statement)
    assert NOW in bound.values() and THREE_DAYS_US in bound.values()
    session.commit.assert_not_called()


def test_close_due_episodes_stores_aware_times_as_naive_utc(session):
    aware = datetime(2026, 10, 7, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    repository.close_due_episodes_flush(
        [MATCH_ID], occurred_at=aware, clock_us=5
    )

    stored = [
        v for v in _bound(_executed(session)).values() if isinstance(v, datetime)
    ]
    assert stored == [datetime(2026, 10, 7, 12, 0)]


def test_retire_closes_open_episodes_and_deletes_the_live_pins(session):
    repository.retire_dashboard_matches_flush([MATCH_ID], occurred_at=NOW)

    update_statement, delete_statement = [
        call.args[0] for call in session.execute.call_args_list
    ]
    update_sql = _sql(update_statement)
    assert update_sql.startswith('UPDATE lan_tournament_match_due_episodes')
    assert 'closed_at IS NULL' in update_sql
    assert 'lan_tournaments.operational_clock_elapsed_us' in update_sql
    assert _sql(delete_statement).startswith(
        'DELETE FROM lan_tournament_match_dashboard_annotations'
    )
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


# -------------------------------------------------------------------- #
# acknowledgements
# -------------------------------------------------------------------- #


def test_acks_are_episode_revision_unique(session):
    [constraint] = [
        c
        for c in DbMatchEscalationAck.__table__.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert constraint.name == 'uq_lan_tournament_escalation_ack_episode_revision'
    assert [c.name for c in constraint.columns] == ['episode_id', 'revision']
    assert _check_sql(
        DbMatchEscalationAck, 'ck_lan_tournament_escalation_ack_revision'
    ) == 'revision >= 1'

    ack = _ack()
    repository.create_escalation_ack_flush(ack)

    [row] = [call.args[0] for call in session.add.call_args_list]
    assert repository._db_ack_to_ack(row) == ack
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


# -------------------------------------------------------------------- #
# clock columns
# -------------------------------------------------------------------- #


def test_multi_day_clock_values_persist(session):
    columns = [
        DbTournament.__table__.c.operational_clock_elapsed_us,
        DbMatchDueEpisode.__table__.c.opened_clock_us,
        DbMatchDueEpisode.__table__.c.closed_clock_us,
        DbMatchEscalationAck.__table__.c.clock_us,
    ]
    for column in columns:
        assert column.type.compile(postgresql.dialect()) == 'BIGINT', column
    assert THREE_DAYS_US > 2**31 - 1

    episode = _episode(
        opened_clock_us=THREE_DAYS_US,
        closed_at=NOW,
        closed_clock_us=THREE_DAYS_US + 1,
    )
    repository.open_due_episode_flush(episode)
    [row] = [call.args[0] for call in session.add.call_args_list]
    assert repository._db_episode_to_episode(row) == episode

    ack = _ack(clock_us=THREE_DAYS_US)
    repository.create_escalation_ack_flush(ack)
    row = session.add.call_args_list[-1].args[0]
    assert repository._db_ack_to_ack(row).clock_us == THREE_DAYS_US

    tournament = repository._db_tournament_to_tournament(
        _db_tournament(operational_clock_elapsed_us=THREE_DAYS_US)
    )
    assert tournament.operational_clock_elapsed_us == THREE_DAYS_US


# -------------------------------------------------------------------- #
# pins
# -------------------------------------------------------------------- #


def _pin_kwargs(**overrides) -> dict:
    values = dict(
        pinned_at=NOW,
        pinned_by=ACTOR_ID,
        updated_at=NOW,
        updated_by=ACTOR_ID,
        expected_revision=0,
    )
    return {**values, **overrides}


def test_first_pin_inserts_and_loses_a_race_without_error(session):
    session.scalars.return_value.one_or_none.return_value = None

    result = repository.save_match_pin_flush(
        MATCH_ID, TOURNAMENT_ID, **_pin_kwargs()
    )

    assert result is None
    sql = _sql(_executed(session))
    assert sql.startswith('INSERT INTO lan_tournament_match_dashboard_annotations')
    assert 'ON CONFLICT (match_id) DO NOTHING' in sql
    session.commit.assert_not_called()


def test_later_pin_writes_compare_and_set_on_the_revision(session):
    row = DbMatchDashboardAnnotation(
        MATCH_ID, TOURNAMENT_ID, NOW, ACTOR_ID, revision=4, pinned_at=NOW,
        pinned_by=ACTOR_ID,
    )
    session.scalars.return_value.one_or_none.return_value = row

    state = repository.save_match_pin_flush(
        MATCH_ID, TOURNAMENT_ID, **_pin_kwargs(expected_revision=3)
    )

    statement = _executed(session)
    sql = _sql(statement)
    assert sql.startswith('UPDATE lan_tournament_match_dashboard_annotations')
    assert 'SET revision=(lan_tournament_match_dashboard_annotations.revision +' in sql
    where = sql.split(' WHERE ')[1].split(' RETURNING ')[0]
    assert 'lan_tournament_match_dashboard_annotations.revision = ' in where
    assert 3 in _bound(statement).values()
    assert state.revision == 4


def test_find_match_pin_state_reads_fresh(session):
    session.scalars.return_value.one_or_none.return_value = None

    assert repository.find_match_pin_state(MATCH_ID) is None

    statement = _executed(session)
    assert statement.get_execution_options()['populate_existing'] is True


# -------------------------------------------------------------------- #
# party thresholds
# -------------------------------------------------------------------- #


def _threshold_row(revision: int) -> DbDashboardPartyThresholds:
    return DbDashboardPartyThresholds(
        PARTY_ID, 20, 60, NOW, ACTOR_ID, revision=revision
    )


# A time that differs from `NOW`, so a bound value names its parameter.
EXPECTED_AT = NOW - timedelta(hours=1)


def _set_thresholds(expected_revision: int, expected_updated_at=EXPECTED_AT):
    return repository.set_party_thresholds_flush(
        PARTY_ID,
        yellow_minutes=20,
        red_minutes=60,
        expected_revision=expected_revision,
        expected_updated_at=expected_updated_at,
        updated_at=NOW,
        updated_by=ACTOR_ID,
    )


def test_party_threshold_row_checks_and_cas(session):
    table = DbDashboardPartyThresholds.__table__
    checks = {
        c.name: str(c.sqltext)
        for c in table.constraints
        if isinstance(c, CheckConstraint)
    }
    assert checks == {
        'ck_lan_tournament_dashboard_party_thresholds_yellow_min': (
            'yellow_minutes >= 1'
        ),
        'ck_lan_tournament_dashboard_party_thresholds_order': (
            'yellow_minutes < red_minutes'
        ),
        'ck_lan_tournament_dashboard_party_thresholds_red_max': (
            'red_minutes <= 1440'
        ),
        'ck_lan_tournament_dashboard_party_thresholds_revision': (
            'revision >= 1'
        ),
    }

    # No row yet: insert, and a concurrent insert changes nothing.
    session.scalars.return_value.one_or_none.return_value = _threshold_row(1)
    created = _set_thresholds(0, None)
    insert = session.scalars.call_args_list[-1].args[0]
    assert _sql(insert).startswith(
        'INSERT INTO lan_tournament_dashboard_party_thresholds'
    )
    assert 'ON CONFLICT (party_id) DO NOTHING' in _sql(insert)
    assert created.revision == 1

    # Revision 0 names no stored override, so it has no time to compare.
    _set_thresholds(0)
    insert = session.scalars.call_args_list[-1].args[0]
    assert EXPECTED_AT not in _bound(insert).values()

    # A newer revision exists: the update matches no row.
    session.scalars.return_value.one_or_none.return_value = None
    assert _set_thresholds(1) is None
    update = session.scalars.call_args_list[-1].args[0]
    update_sql = _sql(update)
    assert update_sql.startswith(
        'UPDATE lan_tournament_dashboard_party_thresholds'
    )
    where = update_sql.split('WHERE')[1]
    assert 'revision' in where
    assert 1 in _bound(update).values()
    # The override is the one that was read: revision and time.
    assert 'lan_tournament_dashboard_party_thresholds.updated_at = ' in where
    assert EXPECTED_AT in _bound(update).values()
    session.commit.assert_not_called()


def test_party_threshold_update_compares_the_time_as_naive_utc(session):
    session.scalars.return_value.one_or_none.return_value = None
    aware = EXPECTED_AT.replace(tzinfo=UTC)
    offset = (EXPECTED_AT + timedelta(hours=2)).replace(
        tzinfo=timezone(timedelta(hours=2))
    )

    _set_thresholds(1, aware)
    aware_update = session.scalars.call_args_list[-1].args[0]
    _set_thresholds(1, offset)
    offset_update = session.scalars.call_args_list[-1].args[0]

    for statement in (aware_update, offset_update):
        assert EXPECTED_AT in _bound(statement).values()


def test_party_threshold_update_without_a_time_matches_no_row(session):
    session.scalars.return_value.one_or_none.return_value = None

    assert _set_thresholds(1, None) is None

    update = session.scalars.call_args_list[-1].args[0]
    where = _sql(update).split('WHERE')[1]
    assert 'lan_tournament_dashboard_party_thresholds.updated_at IS NULL' in where


def test_party_threshold_delete_is_compare_and_set(session):
    session.execute.return_value.one_or_none.return_value = None
    assert not repository.delete_party_thresholds_flush(
        PARTY_ID, expected_revision=2, expected_updated_at=EXPECTED_AT
    )

    session.execute.return_value.one_or_none.return_value = (PARTY_ID,)
    assert repository.delete_party_thresholds_flush(
        PARTY_ID,
        expected_revision=2,
        expected_updated_at=EXPECTED_AT.replace(tzinfo=UTC),
    )

    statement = session.execute.call_args_list[-1].args[0]
    sql = _sql(statement)
    assert sql.startswith('DELETE FROM lan_tournament_dashboard_party_thresholds')
    where = sql.split('WHERE')[1]
    assert 'revision' in where
    assert 2 in _bound(statement).values()
    # The override is the one that was read: revision and time (naive UTC).
    assert 'lan_tournament_dashboard_party_thresholds.updated_at = ' in where
    assert EXPECTED_AT in _bound(statement).values()
    session.commit.assert_not_called()


def test_find_party_thresholds_reads_fresh(session):
    session.scalars.return_value.one_or_none.return_value = None

    assert repository.find_party_thresholds(PARTY_ID) is None

    assert _executed(session).get_execution_options()['populate_existing']

    session.scalars.reset_mock()
    session.scalars.return_value.one_or_none.return_value = _threshold_row(5)
    assert repository.find_party_thresholds(PARTY_ID).revision == 5
