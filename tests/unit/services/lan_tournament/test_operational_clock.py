"""
tests.unit.services.lan_tournament.test_operational_clock
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import dataclasses
from datetime import datetime, timedelta, timezone, UTC
import inspect

import pytest

from byceps.services.lan_tournament.models import (
    operational_timing,
    tournament_dashboard,
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
    MatchPinState,
    OperationalClock,
    TrafficTier,
)
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_dashboard import (
    AckUnavailableReason,
    DASHBOARD_SCOPES,
    DASHBOARD_SORTS,
    DASHBOARD_STATES,
    DASHBOARD_VIEWS,
    DashboardAcknowledgementSummary,
    DashboardConflict,
    DashboardConflictRef,
    DashboardMatchLocation,
    DashboardNonActionableCounts,
    DashboardPage,
    DashboardQuery,
    DashboardRow,
    DashboardRowState,
    DashboardSettings,
    DashboardStatusNote,
    DashboardTierCounts,
    DashboardTournamentRef,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (
    clock_value_us,
    derive_traffic_tier,
    normalize_utc,
    transition_clock,
    UNSUPPORTED_STARTED_CREATION_ERROR,
    UNSUPPORTED_STATUS_TRANSITION_ERROR,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


DRAFT = TournamentStatus.DRAFT
REGISTRATION_OPEN = TournamentStatus.REGISTRATION_OPEN
REGISTRATION_CLOSED = TournamentStatus.REGISTRATION_CLOSED
ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
COMPLETED = TournamentStatus.COMPLETED
CANCELLED = TournamentStatus.CANCELLED

SECOND = 1_000_000
MINUTE = 60 * SECOND

START = datetime(2026, 10, 3, 18, 0)


def at(
    minutes: int = 0, *, seconds: int = 0, microseconds: int = 0
) -> datetime:
    """Return a naive UTC moment relative to the tournament start."""
    return START + timedelta(
        minutes=minutes, seconds=seconds, microseconds=microseconds
    )


def make_settings(yellow_minutes: int = 15, red_minutes: int = 45):
    return DashboardSettings(
        yellow_minutes=yellow_minutes,
        red_minutes=red_minutes,
        poll_seconds=30,
        page_size=50,
        threshold_source='deployment',
    )


def move(clock, old, new, when):
    result = transition_clock(clock, old, new, when)
    assert result.is_ok(), result
    return result.unwrap()


def test_clock_excludes_prestart_and_pauses():
    clock = OperationalClock()

    # Days of registration add no active time.
    for old, new, minutes in [
        (None, DRAFT, -3 * 24 * 60),
        (DRAFT, REGISTRATION_OPEN, -2 * 24 * 60),
        (REGISTRATION_OPEN, REGISTRATION_CLOSED, -60),
        (REGISTRATION_CLOSED, REGISTRATION_OPEN, -50),
        (REGISTRATION_OPEN, REGISTRATION_CLOSED, -40),
    ]:
        clock = move(clock, old, new, at(minutes))
        assert clock == OperationalClock()
    assert clock_value_us(clock, at(-30)) == 0

    clock = move(clock, REGISTRATION_CLOSED, ONGOING, at(0))
    assert clock == OperationalClock(
        elapsed_us=0, running_since=at(0), activated_at=at(0)
    )
    assert clock_value_us(clock, at(15)) == 15 * MINUTE

    clock = move(clock, ONGOING, PAUSED, at(20))
    assert clock == OperationalClock(
        elapsed_us=20 * MINUTE, running_since=None, activated_at=at(0)
    )
    assert clock_value_us(clock, at(50)) == 20 * MINUTE

    clock = move(clock, PAUSED, ONGOING, at(60))
    assert clock.elapsed_us == 20 * MINUTE
    assert clock.running_since == at(60)
    assert clock_value_us(clock, at(65)) == 25 * MINUTE

    clock = move(clock, ONGOING, COMPLETED, at(80))
    assert clock.elapsed_us == 40 * MINUTE
    assert clock.running_since is None
    assert clock_value_us(clock, at(200)) == 40 * MINUTE

    clock = move(clock, COMPLETED, ONGOING, at(300))
    assert clock.activated_at == at(0)
    assert clock_value_us(clock, at(310)) == 50 * MINUTE


def test_cancelling_a_paused_tournament_keeps_the_frozen_time():
    clock = OperationalClock(
        elapsed_us=7 * MINUTE, running_since=None, activated_at=at(0)
    )

    assert move(clock, PAUSED, CANCELLED, at(90)) == clock


def test_cancelling_a_running_tournament_freezes_the_time():
    clock = OperationalClock(
        elapsed_us=0, running_since=at(0), activated_at=at(0)
    )

    assert move(clock, ONGOING, CANCELLED, at(12)) == OperationalClock(
        elapsed_us=12 * MINUTE, running_since=None, activated_at=at(0)
    )


def test_same_status_changes_nothing():
    clock = OperationalClock(
        elapsed_us=3 * MINUTE, running_since=at(5), activated_at=at(0)
    )

    assert move(clock, ONGOING, ONGOING, at(30)) == clock


# fmt: off
@pytest.mark.parametrize(
    ('old', 'new', 'expected_error'),
    [
        (None, ONGOING, UNSUPPORTED_STARTED_CREATION_ERROR),
        (None, PAUSED, UNSUPPORTED_STARTED_CREATION_ERROR),
        (None, COMPLETED, UNSUPPORTED_STARTED_CREATION_ERROR),
        (DRAFT, ONGOING, UNSUPPORTED_STATUS_TRANSITION_ERROR),
        (REGISTRATION_OPEN, ONGOING, UNSUPPORTED_STATUS_TRANSITION_ERROR),
        (CANCELLED, ONGOING, UNSUPPORTED_STATUS_TRANSITION_ERROR),
    ],
)
# fmt: on
def test_a_start_without_a_supported_origin_is_refused(
    old, new, expected_error
):
    result = transition_clock(OperationalClock(), old, new, at(0))

    assert result.is_err()
    assert result.unwrap_err() == expected_error


# fmt: off
@pytest.mark.parametrize(
    'new', [DRAFT, REGISTRATION_OPEN, REGISTRATION_CLOSED, CANCELLED]
)
# fmt: on
def test_creation_before_the_start_leaves_the_clock_alone(new):
    clock = OperationalClock()

    assert move(clock, None, new, at(0)) == clock


def test_unknown_history_is_never_started_or_resumed():
    unknown = OperationalClock()

    # A tournament that ran before the feature has no clock facts.
    assert move(unknown, ONGOING, PAUSED, at(10)) == unknown
    assert move(unknown, PAUSED, ONGOING, at(20)) == unknown
    assert move(unknown, COMPLETED, ONGOING, at(30)) == unknown
    assert clock_value_us(unknown, at(40)) == 0


# fmt: off
@pytest.mark.parametrize(
    ('alert_wait_us', 'expected_tier'),
    [
        (-1, TrafficTier.GREEN),
        (0, TrafficTier.GREEN),
        (14 * MINUTE + 59 * SECOND + 999_999, TrafficTier.GREEN),
        (15 * MINUTE, TrafficTier.YELLOW),
        (15 * MINUTE + 1, TrafficTier.YELLOW),
        (44 * MINUTE + 59 * SECOND + 999_999, TrafficTier.YELLOW),
        (45 * MINUTE, TrafficTier.RED),
        (45 * MINUTE + 1, TrafficTier.RED),
        (3 * 24 * 60 * MINUTE, TrafficTier.RED),
    ],
)
# fmt: on
def test_tier_thresholds_are_exact_at_the_default_minutes(
    alert_wait_us, expected_tier
):
    assert derive_traffic_tier(alert_wait_us, make_settings()) is expected_tier


# fmt: off
@pytest.mark.parametrize(
    ('yellow', 'red', 'alert_wait_us', 'expected_tier'),
    [
        (1, 2, 59 * SECOND + 999_999, TrafficTier.GREEN),
        (1, 2, 60 * SECOND, TrafficTier.YELLOW),
        (1, 2, 119 * SECOND + 999_999, TrafficTier.YELLOW),
        (1, 2, 120 * SECOND, TrafficTier.RED),
        (59, 60, 58 * MINUTE + 59 * SECOND + 999_999, TrafficTier.GREEN),
        (59, 60, 59 * MINUTE, TrafficTier.YELLOW),
        (59, 60, 60 * MINUTE, TrafficTier.RED),
        (1, 1440, 1439 * MINUTE + 59 * SECOND + 999_999, TrafficTier.YELLOW),
        (1, 1440, 1440 * MINUTE, TrafficTier.RED),
    ],
)
# fmt: on
def test_tier_thresholds_follow_non_default_whole_minutes(
    yellow, red, alert_wait_us, expected_tier
):
    settings = make_settings(yellow, red)

    assert derive_traffic_tier(alert_wait_us, settings) is expected_tier


# fmt: off
@pytest.mark.parametrize(
    ('offset_us', 'expected_tier'),
    [
        (0, TrafficTier.GREEN),
        (14 * MINUTE + 59 * SECOND + 999_999, TrafficTier.GREEN),
        (15 * MINUTE, TrafficTier.YELLOW),
        (44 * MINUTE + 59 * SECOND + 999_999, TrafficTier.YELLOW),
        (45 * MINUTE, TrafficTier.RED),
    ],
)
# fmt: on
def test_ack_reescalates_at_exact_15_and_45_minutes(offset_us, expected_tier):
    settings = make_settings()
    clock = move(OperationalClock(), REGISTRATION_CLOSED, ONGOING, at(0))
    # The match became due at the start; an orga acknowledged it at 20 min.
    opened_clock_us = clock_value_us(clock, at(0))
    ack_clock_us = clock_value_us(clock, at(20))
    ack_moment = at(20) + timedelta(microseconds=offset_us)

    wait_since_open = clock_value_us(clock, ack_moment) - opened_clock_us
    alert_wait = clock_value_us(clock, ack_moment) - ack_clock_us

    assert alert_wait == offset_us
    assert wait_since_open == 20 * MINUTE + offset_us
    assert derive_traffic_tier(alert_wait, settings) is expected_tier


def test_a_pause_does_not_consume_the_alert_interval_after_an_ack():
    settings = make_settings()
    clock = move(OperationalClock(), REGISTRATION_CLOSED, ONGOING, at(0))
    ack_clock_us = clock_value_us(clock, at(20))
    clock = move(clock, ONGOING, PAUSED, at(30))
    clock = move(clock, PAUSED, ONGOING, at(90))

    # 10 min counted before the pause, so 5 more minutes make 15.
    just_before = at(94, seconds=59, microseconds=999_999)
    exactly = at(95)

    assert derive_traffic_tier(
        clock_value_us(clock, just_before) - ack_clock_us, settings
    ) is TrafficTier.GREEN
    assert derive_traffic_tier(
        clock_value_us(clock, exactly) - ack_clock_us, settings
    ) is TrafficTier.YELLOW

    # 3 h of wall time since the ack, but only 15 min were active.
    assert at(95) - at(20) > timedelta(hours=1)
    assert clock_value_us(clock, exactly) - ack_clock_us == 15 * MINUTE


# fmt: off
@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (datetime(2026, 10, 3, 18, 0), datetime(2026, 10, 3, 18, 0)),
        (
            datetime(2026, 10, 3, 18, 0, 0, 123_456),
            datetime(2026, 10, 3, 18, 0, 0, 123_456),
        ),
        (
            datetime(2026, 10, 3, 18, 0, tzinfo=UTC),
            datetime(2026, 10, 3, 18, 0),
        ),
        (
            datetime(
                2026, 10, 3, 20, 0, tzinfo=timezone(timedelta(hours=2))
            ),
            datetime(2026, 10, 3, 18, 0),
        ),
        (
            datetime(
                2026, 10, 3, 13, 30, 0, 1,
                tzinfo=timezone(timedelta(hours=-4, minutes=-30)),
            ),
            datetime(2026, 10, 3, 18, 0, 0, 1),
        ),
        (
            datetime(
                2026, 10, 4, 1, 0, tzinfo=timezone(timedelta(hours=7))
            ),
            datetime(2026, 10, 3, 18, 0),
        ),
    ],
)
# fmt: on
def test_clock_normalizes_naive_and_aware_utc(value, expected):
    normalized = normalize_utc(value)

    assert normalized == expected
    assert normalized.tzinfo is None


def test_clock_normalizes_mixed_inputs_without_a_type_error():
    cest = timezone(timedelta(hours=2))
    utc = UTC
    naive_clock = OperationalClock(
        elapsed_us=2 * MINUTE, running_since=at(0), activated_at=at(0)
    )
    aware_clock = OperationalClock(
        elapsed_us=2 * MINUTE,
        running_since=at(0).replace(tzinfo=utc).astimezone(cest),
        activated_at=at(0),
    )
    aware_at = at(15).replace(tzinfo=utc).astimezone(cest)

    expected = 17 * MINUTE
    assert clock_value_us(naive_clock, at(15)) == expected
    assert clock_value_us(naive_clock, aware_at) == expected
    assert clock_value_us(aware_clock, at(15)) == expected
    assert clock_value_us(aware_clock, aware_at) == expected


def test_transitions_store_naive_utc_whatever_the_input():
    cest = timezone(timedelta(hours=2))
    started = at(0).replace(tzinfo=UTC).astimezone(cest)
    paused = at(10).replace(tzinfo=UTC).astimezone(cest)

    clock = move(OperationalClock(), REGISTRATION_CLOSED, ONGOING, started)
    assert clock.running_since == at(0)
    assert clock.running_since.tzinfo is None
    assert clock.activated_at == at(0)
    assert clock.activated_at.tzinfo is None

    clock = move(clock, ONGOING, PAUSED, paused)
    assert clock.elapsed_us == 10 * MINUTE

    clock = move(clock, PAUSED, ONGOING, paused + timedelta(minutes=5))
    assert clock.running_since == at(15)
    assert clock.running_since.tzinfo is None


def test_clock_never_moves_backwards():
    clock = OperationalClock(
        elapsed_us=9 * MINUTE, running_since=at(30), activated_at=at(0)
    )

    # A moment before the run began adds nothing and subtracts nothing.
    assert clock_value_us(clock, at(30) - timedelta(microseconds=1)) == (
        9 * MINUTE
    )
    assert clock_value_us(clock, at(0)) == 9 * MINUTE
    assert clock_value_us(clock, at(-60)) == 9 * MINUTE

    values = [
        clock_value_us(clock, at(minutes))
        for minutes in (-5, 0, 29, 30, 31, 45, 120)
    ]
    assert values == sorted(values)
    assert min(values) == 9 * MINUTE

    # Freezing at an earlier moment keeps the accumulated time.
    frozen = move(clock, ONGOING, PAUSED, at(10))
    assert frozen.elapsed_us == 9 * MINUTE
    assert frozen.running_since is None

    # Neither does a resume at an earlier moment lower the value.
    resumed = move(frozen, PAUSED, ONGOING, at(5))
    assert clock_value_us(resumed, at(5)) == 9 * MINUTE
    assert clock_value_us(resumed, at(-100)) == 9 * MINUTE


# fmt: off
@pytest.mark.parametrize(
    'duration',
    [
        timedelta(microseconds=1),
        timedelta(days=3, microseconds=1),
        # Beyond 2**53 microseconds a float cannot count every tick.
        timedelta(days=200_000, microseconds=1),
    ],
)
# fmt: on
def test_clock_arithmetic_is_exact_integer_microseconds(duration):
    clock = OperationalClock(
        elapsed_us=2**40, running_since=at(0), activated_at=at(0)
    )

    value = clock_value_us(clock, at(0) + duration)

    assert isinstance(value, int)
    assert value == 2**40 + duration // timedelta(microseconds=1)


# fmt: off
@pytest.mark.parametrize('module', [operational_timing, tournament_dashboard])
# fmt: on
def test_contracts_are_frozen_keyword_only_dataclasses(module):
    contracts = [
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if dataclasses.is_dataclass(cls) and cls.__module__ == module.__name__
    ]

    assert contracts
    for cls in contracts:
        assert cls.__dataclass_params__.frozen, cls
        assert all(field.kw_only for field in dataclasses.fields(cls)), cls


def test_dashboard_allowlists_and_defaults_follow_the_design():
    assert DASHBOARD_SCOPES == ('assigned', 'all')
    assert DASHBOARD_VIEWS == ('due', 'upcoming', 'all')
    assert DASHBOARD_SORTS == ('urgency', 'wait', 'tournament')
    assert DASHBOARD_STATES == (
        'all',
        'tier-red',
        'tier-yellow',
        'tier-green',
        'ready-none',
        'ready-one',
        'ready-both',
        'ready-unavailable',
        'conflict',
        'review-open',
        'pinned',
    )

    query = DashboardQuery(per_page=50)

    assert (
        query.scope,
        query.view,
        query.state,
        query.sort,
        query.tournament_id,
        query.page,
    ) == ('assigned', 'due', 'all', 'urgency', None, 1)


def test_the_dashboard_contract_composes_a_page_of_rows():
    tournament_id = TournamentID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    other_match_id = TournamentMatchID(generate_uuid())
    episode_id = MatchDueEpisodeID(generate_uuid())
    actor_id = UserID(generate_uuid())

    episode = MatchDueEpisode(
        id=episode_id,
        tournament_id=tournament_id,
        match_id=match_id,
        pairing_key='participant:a|participant:b',
        opened_at=at(0),
        opened_clock_us=0,
    )
    acknowledgement = MatchEscalationAcknowledgement(
        id=MatchEscalationAcknowledgementID(generate_uuid()),
        episode_id=episode.id,
        tournament_id=tournament_id,
        match_id=match_id,
        revision=1,
        occurred_at=at(20),
        clock_us=20 * MINUTE,
        actor_id=actor_id,
    )
    pin = MatchPinState(
        match_id=match_id,
        tournament_id=tournament_id,
        revision=1,
        pinned_at=at(21),
        pinned_by=actor_id,
        updated_at=at(21),
        updated_by=actor_id,
    )
    summary = DashboardAcknowledgementSummary(
        id=acknowledgement.id,
        revision=acknowledgement.revision,
        actor_display_name='Mara',
        occurred_at=acknowledgement.occurred_at,
    )
    conflict = DashboardConflict(
        user_id=actor_id,
        user_display_name='Nori',
        via_team_name='Kupferfuechse',
        visible_refs=(
            DashboardConflictRef(
                match_id=other_match_id,
                tournament_id=tournament_id,
                tournament_name='Neon Cup',
                location=DashboardMatchLocation(phase=1, round=1),
                list_page=None,
            ),
        ),
        has_external_conflict=True,
    )
    row = DashboardRow(
        match_id=match_id,
        tournament_id=tournament_id,
        tournament_name='Kupfer Cup',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        location=DashboardMatchLocation(phase=1, round=1, match_order=2),
        state=DashboardRowState.DUE,
        tier=TrafficTier.YELLOW,
        created_at=at(-30),
        episode_id=episode.id,
        ack_revision=episode.ack_revision,
        ack_unavailable_reason=AckUnavailableReason.RECENTLY_ACKNOWLEDGED,
        latest_acknowledgement=summary,
        recent_acknowledgements=(summary,),
        acknowledgement_count=1,
        pinned_at=pin.pinned_at,
        pin_revision=pin.revision,
        conflicts=(conflict,),
        status_note=DashboardStatusNote(code='example', params=(('n', 1),)),
    )
    page = DashboardPage(
        rows=(row,),
        as_of=at(35),
        total_count=1,
        page=3,
        per_page=50,
        total_pages=1,
        tier_counts=DashboardTierCounts(yellow=1),
        non_actionable_counts=DashboardNonActionableCounts(paused=2),
        leaderboard_only_tournaments=(
            DashboardTournamentRef(tournament_id=tournament_id, name='HS'),
        ),
        empty_reason='no_current_demand',
    )

    assert page.page == 3 > page.total_pages
    assert page.rows[0].conflicts[0].visible_refs[0].list_page is None
    assert page.rows[0].total_active_wait_us is None
    assert page.tier_counts == DashboardTierCounts(red=0, yellow=1, green=0)

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.tier = TrafficTier.RED
    with pytest.raises(TypeError):
        DashboardTierCounts(1, 2, 3)
