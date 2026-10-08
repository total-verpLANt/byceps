from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
import random
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text, update

from byceps.database import db
from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament import (
    permissions as _permissions,  # noqa: F401 -- registers the permissions
    tournament_dashboard_repository as repository,
    tournament_dashboard_service as service,
    tournament_dashboard_settings_service as settings_service,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.dashboard import (
    DbMatchDashboardAnnotation,
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
from byceps.services.lan_tournament.dbmodels.team import DbTournamentTeam
from byceps.services.lan_tournament.dbmodels.tournament_orga import (
    DbTournamentOrga,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    derive_match_readiness,
)
from byceps.services.lan_tournament.models.operational_timing import (
    MatchDueEpisode,
    MatchDueEpisodeID,
    MatchEscalationAcknowledgement,
    MatchEscalationAcknowledgementID,
    TrafficTier,
)
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_dashboard import (
    AckUnavailableReason,
    DASHBOARD_STATES,
    DashboardNonActionableCounts,
    DashboardQuery,
    DashboardRowState,
    DashboardScope,
    DashboardSettings,
    DashboardTierCounts,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_orga import (
    TournamentOrgaID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeamID,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    derive_due_match_ids,
    derive_traffic_tier,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 7, 12, 0, 0)
MINUTE_US = 60_000_000
CLOCK_START = NOW - timedelta(hours=3)
CLOCK_AT_NOW_US = 180 * MINUTE_US

SETTINGS = DashboardSettings(
    yellow_minutes=15,
    red_minutes=45,
    poll_seconds=30,
    page_size=50,
    threshold_source='deployment',
)

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
ONE_V_ONE = GameFormat.ONE_V_ONE
FREE_FOR_ALL = GameFormat.FREE_FOR_ALL
HIGHSCORE = GameFormat.HIGHSCORE
SINGLE = EliminationMode.SINGLE_ELIMINATION
DOUBLE = EliminationMode.DOUBLE_ELIMINATION
ROUND_ROBIN = EliminationMode.ROUND_ROBIN

DUE = DashboardRowState.DUE
DEMAND_STATES = {
    DashboardRowState.DUE,
    DashboardRowState.UNKNOWN,
    DashboardRowState.PAUSED,
}


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03Scope{i}') for i in range(6)]


@pytest.fixture(scope='module')
def confirmer(make_user):
    return make_user('F03ScopeConfirmer')


@pytest.fixture(scope='module')
def admin(make_admin):
    return make_admin(
        {'lan_tournament.administrate'}, screen_name='F03ScopeAdmin'
    )


@pytest.fixture(autouse=True)
def _context(admin_app):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def party_id(make_party, brand) -> PartyID:
    party_id = PartyID(f'f03s-{uuid4().hex[:12]}')
    return make_party(brand, party_id, f'F03 scope {party_id}').id


def _viewer(user, *permissions: str) -> CurrentUser:
    return CurrentUser.create_authenticated(user, None, frozenset(permissions))


class World:
    """Builds committed tournaments of one party for the dashboard."""

    def __init__(self, party_id: PartyID, users, confirmer) -> None:
        self.party_id = party_id
        self.users = users
        self.confirmer = confirmer
        self._participants: dict[TournamentID, list] = {}

    def tournament(
        self,
        *,
        name: str | None = None,
        status: TournamentStatus | None = ONGOING,
        game_format: GameFormat | None = ONE_V_ONE,
        mode: EliminationMode | None = SINGLE,
        clock: str = 'known',
        elapsed_us: int | None = None,
        playoff: dict | None = None,
        group_size_max: int | None = None,
        game: str | None = None,
        party_id: PartyID | None = None,
    ) -> TournamentID:
        tournament_id = TournamentID(uuid7())
        if clock == 'known':
            running = status is ONGOING
            elapsed = (
                0
                if running
                else (CLOCK_AT_NOW_US if elapsed_us is None else elapsed_us)
            )
            running_since = CLOCK_START if running else None
            activated_at = CLOCK_START
        else:
            elapsed, running_since, activated_at = 0, None, None
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=party_id or self.party_id,
                name=name or f'Scope {tournament_id}',
                game=game,
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
                elimination_mode=mode,
                group_size_max=group_size_max,
                operational_clock_elapsed_us=elapsed,
                operational_clock_running_since=running_since,
                operational_clock_activated_at=activated_at,
                **(playoff or {}),
            )
        )
        participants = [
            DbTournamentParticipant(
                TournamentParticipantID(uuid7()), user.id, tournament_id, NOW
            )
            for user in self.users
        ]
        db.session.add_all(participants)
        db.session.commit()
        self._participants[tournament_id] = [p.id for p in participants]
        return tournament_id

    def participants(
        self, tournament_id: TournamentID, count: int | None = None
    ):
        ids = self._participants[tournament_id]
        return ids if count is None else ids[:count]

    def team(self, tournament_id, name, captain=None) -> TournamentTeamID:
        team_id = TournamentTeamID(uuid7())
        db.session.add(
            DbTournamentTeam(
                team_id,
                tournament_id,
                name,
                (captain or self.users[0]).id,
                NOW,
            )
        )
        db.session.commit()
        return team_id

    def match(
        self,
        tournament_id: TournamentID,
        *,
        contestants: int | list = 2,
        teams: list | None = None,
        phase: int = 1,
        round: int | None = None,
        group_order: int | None = None,
        match_order: int | None = None,
        bracket: str | None = None,
        confirmed: bool = False,
        occupied: bool = False,
        seeding_target: str | None = None,
        last_changed_at: datetime | None = None,
        scores: list | None = None,
        pairing: bool = False,
    ) -> TournamentMatchID:
        match_id = TournamentMatchID(uuid7())
        db.session.add(
            DbTournamentMatch(
                match_id,
                tournament_id,
                NOW - timedelta(hours=1),
                group_order=group_order,
                match_order=match_order,
                round=round,
                bracket=bracket,
                confirmed_by=self.confirmer.id if confirmed else None,
                phase=phase,
                seeding_target=seeding_target,
                occupied_since=NOW - timedelta(minutes=30)
                if occupied
                else None,
                last_changed_at=last_changed_at,
            )
        )
        db.session.flush()
        participant_ids = (
            self.participants(tournament_id, contestants)
            if isinstance(contestants, int)
            else contestants
        )
        entries = [{'participant_id': c} for c in participant_ids]
        entries += [{'team_id': t} for t in teams or []]
        for index, kwargs in enumerate(entries):
            if scores is not None:
                kwargs['score'] = scores[index]
            db.session.add(
                DbTournamentMatchToContestant(
                    TournamentMatchToContestantID(uuid7()),
                    match_id,
                    NOW - timedelta(hours=1),
                    **kwargs,
                )
            )
        db.session.flush()
        if pairing:
            repo.refresh_match_pairing_flush(
                match_id, occurred_at=NOW - timedelta(minutes=20)
            )
        db.session.commit()
        return match_id

    def episode(
        self,
        tournament_id,
        match_id,
        *,
        wait_minutes: float = 10,
        wait_us: int | None = None,
        opened_at: datetime | None = None,
        pairing_key: str = 'key',
        closed_wait_minutes: float | None = None,
    ) -> MatchDueEpisodeID:
        episode_id = MatchDueEpisodeID(uuid7())
        if wait_us is None:
            wait_us = int(wait_minutes * MINUTE_US)
        opened_clock = CLOCK_AT_NOW_US - wait_us
        closed: dict[str, Any] = {}
        if closed_wait_minutes is not None:
            closed = {
                'closed_at': NOW - timedelta(minutes=1),
                'closed_clock_us': opened_clock
                + int(closed_wait_minutes * MINUTE_US),
            }
        repo.open_due_episode_flush(
            MatchDueEpisode(
                id=episode_id,
                tournament_id=tournament_id,
                match_id=match_id,
                pairing_key=pairing_key,
                opened_at=opened_at or NOW - timedelta(minutes=wait_minutes),
                opened_clock_us=opened_clock,
                **closed,
            )
        )
        db.session.commit()
        return episode_id

    def due_match(
        self,
        tournament_id,
        *,
        wait_minutes: float = 10,
        wait_us: int | None = None,
        **match_fields,
    ) -> TournamentMatchID:
        """Create a playable knock-out match with an open episode."""
        match_id = self.match(tournament_id, **match_fields)
        self.episode(
            tournament_id,
            match_id,
            wait_minutes=wait_minutes,
            wait_us=wait_us,
        )
        return match_id

    def ack(
        self,
        tournament_id,
        match_id,
        episode_id,
        *,
        revision: int = 1,
        minutes_ago: float = 5,
        actor=None,
        comment: str | None = None,
    ) -> MatchEscalationAcknowledgementID:
        ack_id = MatchEscalationAcknowledgementID(uuid7())
        repo.create_escalation_ack_flush(
            MatchEscalationAcknowledgement(
                id=ack_id,
                episode_id=episode_id,
                tournament_id=tournament_id,
                match_id=match_id,
                revision=revision,
                occurred_at=NOW - timedelta(minutes=minutes_ago),
                clock_us=CLOCK_AT_NOW_US - int(minutes_ago * MINUTE_US),
                actor_id=(actor or self.users[0]).id,
                comment=comment,
            )
        )
        db.session.execute(
            update(DbMatchDueEpisode)
            .where(DbMatchDueEpisode.id == episode_id)
            .values(ack_revision=revision)
        )
        db.session.commit()
        return ack_id

    def pin(self, tournament_id, match_id, user) -> None:
        repo.save_match_pin_flush(
            match_id,
            tournament_id,
            pinned_at=NOW - timedelta(minutes=7),
            pinned_by=user.id,
            updated_at=NOW - timedelta(minutes=7),
            updated_by=user.id,
            expected_revision=0,
        )
        db.session.commit()

    def ready(self, match_id, side: MatchSide, user) -> None:
        repo.set_side_ready_flush(
            match_id, side, NOW - timedelta(minutes=9), user.id
        )
        db.session.commit()

    def orga(self, tournament_id, user) -> None:
        db.session.add(
            DbTournamentOrga(
                TournamentOrgaID(uuid7()), tournament_id, user.id, NOW
            )
        )
        db.session.commit()


@pytest.fixture
def world(party_id, users, confirmer):
    return World(party_id, users, confirmer)


def _query(**fields) -> DashboardQuery:
    fields.setdefault('per_page', 50)
    return DashboardQuery(**fields)


def _page(viewer, party_id, *, settings=SETTINGS, now=NOW, **fields):
    result = service.get_dashboard_page(
        viewer, party_id, _query(**fields), settings=settings, now=now
    )
    return result.unwrap()


def _ids(page):
    return [row.match_id for row in page.rows]


def _admin(admin) -> CurrentUser:
    return _viewer(admin, 'lan_tournament.administrate')


def _all_rows(viewer, party_id, *, per_page=100, **fields):
    """Read every page of a listing."""
    rows = []
    number = 1
    while True:
        page = _page(viewer, party_id, per_page=per_page, page=number, **fields)
        rows += page.rows
        if number >= page.total_pages:
            return rows
        number += 1


def _oracle_due_ids(tournament_id) -> frozenset:
    """Apply the pure policy to the stored facts, as the reconciler does."""
    tournament = repo.get_tournament(tournament_id, fresh=True)
    matches = repo.get_matches_for_tournament(tournament_id)
    contestants = repo.get_contestants_for_tournament(tournament_id)
    completed = frozenset(
        match.id for match in matches if match.occupied_since is not None
    )
    return derive_due_match_ids(tournament, matches, contestants, completed)


def _sql_due_ids(viewer, party_id) -> set:
    return {
        row.match_id
        for row in _all_rows(viewer, party_id, view='all', scope='all')
        if row.state in DEMAND_STATES
    }


# -- scope --


def test_scoped_orga_cannot_widen_party_or_assignments(
    world, party_id, users, admin, make_party, brand
):
    mine = world.tournament(name='Mine')
    other = world.tournament(name='Other')
    foreign_party = PartyID(f'f03s-{uuid4().hex[:12]}')
    make_party(brand, foreign_party, f'F03 foreign {foreign_party}')
    foreign = world.tournament(name='Foreign', party_id=foreign_party)
    orga = users[1]
    world.orga(mine, orga)
    world.orga(foreign, orga)

    my_match = world.due_match(mine, wait_minutes=20)
    other_match = world.due_match(other, wait_minutes=50)
    foreign_match = world.due_match(foreign, wait_minutes=50)
    # Matches that are not due leak just as well, into the other lists.
    hidden = [
        world.match(other, contestants=1, round=2),
        world.match(other, confirmed=True, round=3),
        world.match(foreign, contestants=1, round=2),
        world.match(foreign, confirmed=True, round=3),
    ]

    viewer = _viewer(orga)

    # Neither the requested scope nor a tournament filter widens it, and
    # what is not theirs is not counted either.
    for requested in ('assigned', 'all'):
        scope = service.resolve_dashboard_scope(
            viewer, party_id, requested
        ).unwrap()
        assert scope.kind == 'assigned'
        assert scope.tournament_ids == (mine,)
        assert not scope.is_global_admin

        page = _page(viewer, party_id, scope=requested)
        assert _ids(page) == [my_match]
        assert page.total_count == 1
        assert page.tier_counts == DashboardTierCounts(red=0, yellow=1, green=0)
        assert [c.tournament_id for c in page.tournament_choices] == [mine]
        for view in ('upcoming', 'all'):
            listed = _page(viewer, party_id, scope=requested, view=view)
            assert set(_ids(listed)) <= {my_match}, view
            assert listed.total_count == len(listed.rows)
            assert (
                listed.non_actionable_counts == DashboardNonActionableCounts()
            )
        everything = _page(viewer, party_id, scope=requested, view='all')
        assert not set(_ids(everything)) & {other_match, foreign_match, *hidden}

    for wanted in (other, foreign):
        page = _page(viewer, party_id, scope='all', tournament_id=wanted)
        assert page.rows == ()
        assert page.total_count == 0
        assert page.empty_reason == 'no_filter_matches'
        assert page.tier_counts.yellow == 1

    # A tournament filter that arrives as a string is not a bypass.
    page = _page(viewer, party_id, tournament_id=str(other))
    assert page.rows == ()

    # The assignment in another party only counts there.
    page = _page(viewer, foreign_party, scope='all')
    assert _ids(page) == [foreign_match]

    # A claim of the viewer object is not the permission of the user.
    forged = _viewer(orga, 'lan_tournament.administrate')
    assert _ids(_page(forged, party_id, scope='all')) == [my_match]

    # Only a global administrator widens it.
    admin_viewer = _admin(admin)
    scope = service.resolve_dashboard_scope(
        admin_viewer, party_id, 'all'
    ).unwrap()
    assert scope.kind == 'all'
    assert scope.is_global_admin
    assert set(scope.tournament_ids) == {mine, other}
    page = _page(admin_viewer, party_id, scope='all')
    assert set(_ids(page)) == {my_match, other_match}
    assert page.tier_counts == DashboardTierCounts(red=1, yellow=1, green=0)
    own = _page(admin_viewer, party_id, scope='assigned')
    assert own.rows == ()
    assert own.empty_reason == 'no_assignment'

    # The repository bounds a forged scope to its party as well.
    forged_scope = DashboardScope(
        user_id=orga.id,
        party_id=party_id,
        kind='all',
        tournament_ids=(mine, other, foreign),
        is_global_admin=True,
    )
    data = repository.query_dashboard_matches(
        forged_scope, _query(), now=NOW, settings=SETTINGS
    )
    assert {r.tournament_id for r in data.records} == {mine, other}

    # Whoever may not see the dashboard gets no part of it.
    stranger = users[2]
    for permissions in ((), ('lan_tournament.view',)):
        for requested in ('assigned', 'all'):
            result = service.get_dashboard_page(
                _viewer(stranger, *permissions),
                party_id,
                _query(scope=requested),
                settings=SETTINGS,
                now=NOW,
            )
            assert result.unwrap_err() == service.DASHBOARD_FORBIDDEN_ERROR
    anonymous = CurrentUser.create_anonymous(None)
    result = service.get_dashboard_page(
        anonymous, party_id, _query(), settings=SETTINGS, now=NOW
    )
    assert result.unwrap_err() == service.DASHBOARD_UNAUTHENTICATED_ERROR

    # A revoked assignment loses the next request.
    db.session.execute(
        delete(DbTournamentOrga).where(
            DbTournamentOrga.tournament_id == mine,
            DbTournamentOrga.user_id == orga.id,
        )
    )
    db.session.commit()
    result = service.get_dashboard_page(
        viewer, party_id, _query(), settings=SETTINGS, now=NOW
    )
    assert result.unwrap_err() == service.DASHBOARD_FORBIDDEN_ERROR


# -- the SQL due set equals the pure policy --

_PLAYOFF_RR_TO_KNOCKOUT = {
    'playoff_game_format': ONE_V_ONE,
    'playoff_elimination_mode': SINGLE,
    'playoff_group_count': 2,
    'playoff_qualifiers_per_group': 1,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}
_PLAYOFF_HIGHSCORE_TO_FFA = {
    'playoff_game_format': FREE_FOR_ALL,
    'playoff_elimination_mode': DOUBLE,
    'playoff_qualifier_count': 4,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}

# fmt: off
_FAMILIES: list[dict[str, Any]] = [
    {'game_format': ONE_V_ONE, 'mode': SINGLE},
    {'game_format': ONE_V_ONE, 'mode': DOUBLE},
    {'game_format': ONE_V_ONE, 'mode': ROUND_ROBIN},
    {'game_format': FREE_FOR_ALL, 'mode': SINGLE},
    {'game_format': FREE_FOR_ALL, 'mode': DOUBLE},
    {'game_format': FREE_FOR_ALL, 'mode': DOUBLE},
    {'game_format': FREE_FOR_ALL, 'mode': SINGLE},
    {'game_format': HIGHSCORE, 'mode': EliminationMode.NONE},
    {'game_format': None, 'mode': None},
    {'game_format': ONE_V_ONE, 'mode': ROUND_ROBIN,
     'playoff': _PLAYOFF_RR_TO_KNOCKOUT},
    {'game_format': HIGHSCORE, 'mode': EliminationMode.NONE,
     'playoff': _PLAYOFF_HIGHSCORE_TO_FFA},
]
_STATUSES = [
    ONGOING, ONGOING, ONGOING, PAUSED, PAUSED,
    TournamentStatus.DRAFT, TournamentStatus.REGISTRATION_OPEN,
    TournamentStatus.REGISTRATION_CLOSED, TournamentStatus.COMPLETED,
    TournamentStatus.CANCELLED, None,
]
# fmt: on


def _random_tournament(rng: random.Random, world: World) -> TournamentID:
    tournament_id = world.tournament(
        status=rng.choice(_STATUSES),
        clock=rng.choice(['known', 'unknown']),
        **rng.choice(_FAMILIES),
    )
    for _ in range(rng.randint(3, 12)):
        world.match(
            tournament_id,
            contestants=rng.choice([0, 1, 2, 2, 2, 3, 4]),
            phase=rng.choice([1, 1, 2]),
            round=rng.choice([None, 1, 1, 2, 2, 3]),
            group_order=rng.choice([None, 0, 1]),
            bracket=rng.choice([None, 'WB', 'WB', 'LB', 'GF', 'P3', 'XX']),
            confirmed=rng.random() < 0.3,
            occupied=rng.random() < 0.5,
            seeding_target=rng.choice(
                [None, 'ffa:WB:2', 'ffa:WB:3', 'ffa:LB:1']
            ),
        )
    return tournament_id


@pytest.mark.parametrize('seed', range(16))
def test_sql_due_ids_equal_pure_policy(world, party_id, admin, seed):
    rng = random.Random(seed)  # noqa: S311 -- seeded fixture generator
    tournament_ids = [_random_tournament(rng, world) for _ in range(9)]

    viewer = _admin(admin)
    oracle = set().union(*(_oracle_due_ids(t) for t in tournament_ids))
    assert _sql_due_ids(viewer, party_id) == oracle

    # Each match is in exactly one state, and the lists are cut from them.
    everything = _all_rows(viewer, party_id, view='all', scope='all')
    by_state: dict = {}
    for row in everything:
        by_state.setdefault(row.state, set()).add(row.match_id)
    assert sum(len(ids) for ids in by_state.values()) == len(everything)
    assert len({row.match_id for row in everything}) == len(everything)
    due_states = (DashboardRowState.DUE, DashboardRowState.UNKNOWN)
    waiting_states = (
        DashboardRowState.UPCOMING,
        DashboardRowState.PARTIAL,
        DashboardRowState.AWAITING_LOBBY,
    )
    for view, states in (('due', due_states), ('upcoming', waiting_states)):
        listed = {
            row.match_id
            for row in _all_rows(viewer, party_id, view=view, scope='all')
        }
        assert listed == set().union(*(by_state.get(s, set()) for s in states))


# fmt: off
_SCENARIOS = {
    'knockout_needs_two_sides': (
        {},
        [
            ('due', {'contestants': 2}),
            ('not', {'contestants': 1}),
            ('not', {'contestants': 0}),
            ('not', {'contestants': 2, 'confirmed': True}),
            ('not', {'contestants': 3}),
        ],
    ),
    'round_robin_frontier_per_group': (
        {'mode': ROUND_ROBIN},
        [
            # Group 0: a one-sided match holds round 1 open.
            ('due', {'round': 1, 'group_order': 0}),
            ('not', {'round': 1, 'group_order': 0, 'contestants': 1}),
            ('not', {'round': 2, 'group_order': 0}),
            # Group 1 is judged on its own: round 1 is settled.
            ('not', {'round': 1, 'group_order': 1, 'confirmed': True}),
            ('due', {'round': 2, 'group_order': 1}),
            ('due', {'round': 2, 'group_order': 1}),
            ('not', {'round': 3, 'group_order': 1}),
            # The matches of one group without a number form one group.
            ('due', {'round': 1, 'group_order': None}),
            ('not', {'round': 2, 'group_order': None}),
            # A match without a round is never due.
            ('not', {'round': None, 'group_order': 2}),
        ],
    ),
    'phases_follow_their_own_format': (
        {'mode': ROUND_ROBIN, 'playoff': _PLAYOFF_RR_TO_KNOCKOUT},
        [
            ('due', {'round': 1, 'group_order': 0}),
            ('not', {'round': 2, 'group_order': 0}),
            # Phase 2 is a knock-out: no rounds, only a playable pairing.
            ('due', {'phase': 2, 'round': 3, 'bracket': 'WB'}),
            ('due', {'phase': 2, 'round': 1, 'bracket': 'WB'}),
            ('not', {'phase': 2, 'contestants': 1}),
        ],
    ),
    'highscore_runs_no_matches': (
        {'game_format': HIGHSCORE, 'mode': EliminationMode.NONE},
        [
            ('not', {'contestants': 2}),
            ('not', {'phase': 2, 'contestants': 2}),
        ],
    ),
    'highscore_playoffs_are_free_for_all': (
        {
            'game_format': HIGHSCORE,
            'mode': EliminationMode.NONE,
            'playoff': _PLAYOFF_HIGHSCORE_TO_FFA,
        },
        [
            ('not', {'contestants': 2, 'occupied': True}),
            ('due', {'phase': 2, 'contestants': 3, 'occupied': True,
                     'round': 1, 'bracket': 'WB'}),
            ('not', {'phase': 2, 'contestants': 3, 'round': 1,
                     'bracket': 'WB'}),
        ],
    ),
    'free_for_all_current_round_of_complete_lobbies': (
        {'game_format': FREE_FOR_ALL, 'mode': SINGLE},
        [
            ('not', {'contestants': 3, 'occupied': True, 'round': 1,
                     'confirmed': True}),
            ('not', {'contestants': 3, 'occupied': True, 'round': 1}),
            ('due', {'contestants': 2, 'occupied': True, 'round': 2}),
            ('due', {'contestants': 4, 'occupied': True, 'round': 2}),
            # A lobby that is not assembled yet, or has a lone survivor.
            ('not', {'contestants': 3, 'occupied': False, 'round': 2}),
            ('not', {'contestants': 1, 'occupied': True, 'round': 2}),
        ],
    ),
    'free_for_all_merged_and_final_rounds': (
        {'game_format': FREE_FOR_ALL, 'mode': DOUBLE},
        [
            # The merged losers round consumed winners round 2.
            ('not', {'contestants': 3, 'occupied': True, 'round': 2,
                     'bracket': 'WB'}),
            ('due', {'contestants': 3, 'occupied': True, 'round': 1,
                     'bracket': 'LB', 'seeding_target': 'ffa:WB:3'}),
            # A pool without such a consumer keeps its latest round.
            ('due', {'contestants': 3, 'occupied': True, 'round': 2,
                     'bracket': None}),
        ],
    ),
    'free_for_all_grand_final_consumes_both_pools': (
        {'game_format': FREE_FOR_ALL, 'mode': DOUBLE},
        [
            ('not', {'contestants': 3, 'occupied': True, 'round': 2,
                     'bracket': 'WB'}),
            ('not', {'contestants': 3, 'occupied': True, 'round': 2,
                     'bracket': 'LB'}),
            ('due', {'contestants': 2, 'occupied': True, 'round': None,
                     'bracket': 'GF'}),
            ('not', {'contestants': 2, 'occupied': True, 'round': None,
                     'bracket': 'GF', 'confirmed': True}),
        ],
    ),
}
# fmt: on


@pytest.mark.parametrize('name', list(_SCENARIOS))
def test_sql_due_ids_equal_pure_policy_in_named_scenarios(
    world, party_id, admin, name
):
    fields, matches = _SCENARIOS[name]
    tournament_id = world.tournament(**fields)
    expected_due = set()
    ids = []
    for kind, match_fields in matches:
        match_id = world.match(tournament_id, **match_fields)
        ids.append(match_id)
        if kind == 'due':
            expected_due.add(match_id)

    assert _oracle_due_ids(tournament_id) == expected_due
    assert _sql_due_ids(_admin(admin), party_id) == expected_due


@pytest.mark.parametrize('status', list(TournamentStatus) + [None])
def test_only_running_and_paused_tournaments_have_due_matches(
    world, party_id, admin, status
):
    tournament_id = world.tournament(status=status)
    match_id = world.match(tournament_id)

    expected = {match_id} if status in (ONGOING, PAUSED) else set()
    assert _oracle_due_ids(tournament_id) == expected
    assert _sql_due_ids(_admin(admin), party_id) == expected


# -- order, counts and paging --

_BRACKET_RANK = {None: 0, Bracket.WINNERS: 1, Bracket.LOSERS: 2}


def _label_key(row):
    location = row.location
    return (
        location.phase,
        -1 if location.group_order is None else location.group_order,
        _BRACKET_RANK[location.bracket],
        location.round if location.round is not None else 10**9,
        location.match_order if location.match_order is not None else 10**9,
    )


def _tournament_key(row):
    return (row.tournament_name.lower(), row.tournament_id, *_label_key(row))


def _tier_rank(row):
    if row.tier is not None:
        return {
            TrafficTier.RED: 0,
            TrafficTier.YELLOW: 1,
            TrafficTier.GREEN: 2,
        }[row.tier]
    return (
        3
        if row.state in (DashboardRowState.PAUSED, DashboardRowState.UNKNOWN)
        else 4
    )


def _urgency_key(row):
    return (
        _tier_rank(row),
        -(row.alert_interval_us if row.alert_interval_us is not None else -1),
        -(
            row.total_active_wait_us
            if row.total_active_wait_us is not None
            else -1
        ),
        *_tournament_key(row),
        row.match_id,
    )


def _wait_key(row):
    return (
        -(
            row.total_active_wait_us
            if row.total_active_wait_us is not None
            else -1
        ),
        *_tournament_key(row),
        row.match_id,
    )


_SORT_KEYS = {
    'urgency': _urgency_key,
    'wait': _wait_key,
    'tournament': lambda row: (*_tournament_key(row), row.match_id),
}


def test_sort_and_counts_precede_stable_pagination(world, party_id, admin):
    alpha = world.tournament(name='Alpha')
    bravo = world.tournament(name='Bravo')
    # Equal waits and equal intervals make the tie-breakers matter.
    for order, wait in enumerate([50, 30, 30, 20, 20, 5, 5, 2]):
        world.due_match(alpha, wait_minutes=wait, round=1, match_order=order)
    for order, wait in enumerate([50, 30, 20, 5]):
        world.due_match(bravo, wait_minutes=wait, round=1, match_order=order)
    # A checked delay lowers the alert interval, not the total wait.
    acked = world.due_match(alpha, wait_minutes=55, round=2, match_order=0)
    episode_id = db.session.scalar(
        select(DbMatchDueEpisode.id).where(DbMatchDueEpisode.match_id == acked)
    )
    world.ack(alpha, acked, episode_id, minutes_ago=10)
    total = 13

    viewer = _admin(admin)
    for sort, key in _SORT_KEYS.items():
        everything = _page(
            viewer, party_id, scope='all', sort=sort, per_page=100
        )
        assert everything.total_count == total
        assert [key(row) for row in everything.rows] == sorted(
            key(row) for row in everything.rows
        )
        expected = _ids(everything)
        assert len(set(expected)) == total

        for per_page in (1, 2, 5, 13, 50):
            collected = []
            counts = set()
            for number in range(1, -(-total // per_page) + 1):
                page = _page(
                    viewer,
                    party_id,
                    scope='all',
                    sort=sort,
                    per_page=per_page,
                    page=number,
                )
                collected += _ids(page)
                counts.add(
                    (
                        page.total_count,
                        page.total_pages,
                        page.tier_counts,
                        page.non_actionable_counts,
                    )
                )
                assert page.page == number
                assert page.per_page == per_page
            assert collected == expected, (sort, per_page)
            assert counts == {
                (
                    total,
                    -(-total // per_page),
                    everything.tier_counts,
                    everything.non_actionable_counts,
                )
            }

    # The urgency order reads the alert interval, the wait order the total.
    urgency = _page(viewer, party_id, scope='all', per_page=100)
    wait = _page(viewer, party_id, scope='all', sort='wait', per_page=100)
    assert [r.total_active_wait_us for r in wait.rows][:3] == [
        55 * MINUTE_US,
        50 * MINUTE_US,
        50 * MINUTE_US,
    ]
    by_id = {r.match_id: r for r in urgency.rows}
    assert by_id[acked].tier is TrafficTier.GREEN
    assert by_id[acked].alert_interval_us == 10 * MINUTE_US
    assert by_id[acked].total_active_wait_us == 55 * MINUTE_US
    greens = [r for r in urgency.rows if r.tier is TrafficTier.GREEN]
    assert [r.alert_interval_us for r in greens] == sorted(
        (r.alert_interval_us for r in greens), reverse=True
    )
    assert urgency.rows.index(by_id[acked]) < urgency.rows.index(greens[1])


def test_unknown_history_has_no_fake_green_tier(world, party_id, admin):
    # A tournament that ran before the feature has no known clock, and
    # a tracked one can lack the episode of a match that is due.
    legacy = world.tournament(name='Legacy', clock='unknown')
    legacy_match = world.match(legacy, round=1, match_order=1)
    tracked = world.tournament(name='Tracked')
    no_episode = world.match(tracked, round=1, match_order=1)
    tracked_ok = world.due_match(
        tracked, wait_minutes=7, round=1, match_order=2
    )

    def snapshot() -> list:
        return [
            db.session.scalar(select(func.count()).select_from(model))
            for model in (
                DbMatchDueEpisode,
                DbMatchEscalationAck,
                DbMatchDashboardAnnotation,
            )
        ] + [
            tuple(row)
            for row in db.session.execute(
                text(
                    'SELECT id, operational_clock_elapsed_us,'
                    ' operational_clock_running_since,'
                    ' operational_clock_activated_at'
                    ' FROM lan_tournaments ORDER BY id'
                )
            )
        ]

    before = snapshot()
    viewer = _admin(admin)
    due = _page(viewer, party_id, scope='all')
    everything = _page(viewer, party_id, scope='all', view='all')
    green = _page(viewer, party_id, scope='all', state='tier-green')
    assert snapshot() == before

    by_id = {row.match_id: row for row in due.rows}
    assert set(by_id) == {legacy_match, no_episode, tracked_ok}
    for match_id in (legacy_match, no_episode):
        row = by_id[match_id]
        assert row.state is DashboardRowState.UNKNOWN
        assert row.tier is None
        assert row.total_active_wait_us is None
        assert row.alert_interval_us is None
        assert row.episode_id is None
        assert row.episode_opened_at is None
        assert row.ack_unavailable_reason is AckUnavailableReason.CLOCK_UNKNOWN
        assert row.latest_acknowledgement is None
    assert by_id[tracked_ok].tier is TrafficTier.GREEN
    assert by_id[tracked_ok].total_active_wait_us == 7 * MINUTE_US

    # Nothing unknown is counted into a tier, or found by one.
    assert due.tier_counts == DashboardTierCounts(red=0, yellow=0, green=1)
    assert everything.tier_counts == due.tier_counts
    assert _ids(green) == [tracked_ok]
    assert green.total_count == 1

    # Unknown rows sort behind every tier.
    assert _ids(due)[0] == tracked_ok

    # A paused tournament without history shows no duration either.
    paused = world.tournament(
        name='Paused legacy', status=PAUSED, clock='unknown'
    )
    paused_match = world.match(paused, round=1, match_order=1)
    page = _page(viewer, party_id, scope='all', view='all')
    row = next(r for r in page.rows if r.match_id == paused_match)
    assert row.state is DashboardRowState.PAUSED
    assert row.tier is None
    assert row.total_active_wait_us is None
    assert row.alert_interval_us is None


def _expected_tiers(waits: Iterable[int], settings: DashboardSettings):
    counts = {tier: 0 for tier in TrafficTier}
    for wait in waits:
        counts[derive_traffic_tier(wait, settings)] += 1
    return DashboardTierCounts(
        red=counts[TrafficTier.RED],
        yellow=counts[TrafficTier.YELLOW],
        green=counts[TrafficTier.GREEN],
    )


@pytest.mark.parametrize('thresholds', [(15, 45), (20, 60), (1, 2)])
def test_tier_counts_ignore_filters_and_page(
    world, party_id, admin, thresholds
):
    settings = DashboardSettings(
        yellow_minutes=thresholds[0],
        red_minutes=thresholds[1],
        poll_seconds=30,
        page_size=50,
        threshold_source='party',
    )
    yellow_us = thresholds[0] * MINUTE_US
    red_us = thresholds[1] * MINUTE_US
    # Exactly on, and one microsecond below, each boundary.
    waits = [
        1,
        yellow_us - 1,
        yellow_us,
        red_us - 1,
        red_us,
        red_us + 7 * MINUTE_US,
    ]
    alpha = world.tournament(name='Alpha')
    bravo = world.tournament(name='Bravo')
    for order, wait in enumerate(waits):
        world.due_match(alpha, wait_us=wait, round=1, match_order=order)
    world.due_match(bravo, wait_us=yellow_us + 5, round=1, match_order=0)
    world.due_match(bravo, wait_us=2, round=1, match_order=1)
    all_waits = [*waits, yellow_us + 5, 2]

    # None of these has a tier of its own that is counted.
    paused = world.tournament(name='Paused', status=PAUSED)
    world.due_match(paused, wait_minutes=100, round=1, match_order=0)
    legacy = world.tournament(name='Legacy', clock='unknown')
    world.match(legacy, round=1, match_order=0)
    world.match(alpha, round=2, match_order=0)
    world.match(alpha, round=3, match_order=0, confirmed=True)

    expected = _expected_tiers(all_waits, settings)
    assert expected == DashboardTierCounts(red=2, yellow=3, green=3)

    viewer = _admin(admin)
    seen = set()
    variants = [
        {},
        {'view': 'all'},
        {'view': 'upcoming'},
        {'state': 'tier-red'},
        {'state': 'tier-green', 'view': 'all'},
        {'state': 'pinned'},
        {'state': 'ready-both'},
        {'tournament_id': bravo},
        {'sort': 'wait', 'per_page': 3, 'page': 2},
        {'per_page': 1, 'page': 4},
        {'per_page': 1, 'page': 99},
    ]
    for fields in variants:
        page = _page(viewer, party_id, scope='all', settings=settings, **fields)
        seen.add(page.tier_counts)
    assert seen == {expected}

    # The tier of each row follows the pure policy, and so does its filter.
    rows = _page(
        viewer, party_id, scope='all', settings=settings, per_page=100
    ).rows
    rows = [row for row in rows if row.tier is not None]
    assert sorted(r.alert_interval_us for r in rows) == sorted(all_waits)
    for tier in TrafficTier:
        filtered = _page(
            viewer,
            party_id,
            scope='all',
            settings=settings,
            state=f'tier-{tier.value}',
            per_page=100,
        )
        assert set(_ids(filtered)) == {
            r.match_id
            for r in rows
            if derive_traffic_tier(r.alert_interval_us, settings) is tier
        }
        assert all(r.tier is tier for r in filtered.rows)


@pytest.fixture
def make_world(make_party, brand, users, confirmer):
    """Provide a factory of worlds, each in a party of its own."""

    def _make() -> World:
        new_party_id = PartyID(f'f03s-{uuid4().hex[:12]}')
        make_party(brand, new_party_id, f'F03 scope {new_party_id}')
        return World(new_party_id, users, confirmer)

    return _make


# -- state filters and the urgency order --


def test_state_filters_and_urgency_sort_precede_pagination(
    world, party_id, admin, users
):
    first = world.tournament(name='T1')
    ffa = world.tournament(name='T2', game_format=FREE_FOR_ALL, mode=SINGLE)
    paused = world.tournament(name='T3', status=PAUSED)
    legacy = world.tournament(name='T4', clock='unknown')
    round_robin = world.tournament(name='T5', mode=ROUND_ROBIN)

    def due(order, wait, **kwargs):
        return world.due_match(
            first,
            wait_minutes=wait,
            round=1,
            match_order=order,
            pairing=True,
            **kwargs,
        )

    red = due(1, 60)
    world.ready(red, MatchSide.A, users[0])
    world.ready(red, MatchSide.B, users[1])
    yellow_30 = due(2, 30)
    world.pin(first, yellow_30, users[2])
    yellow_20 = due(3, 20)
    world.ready(yellow_20, MatchSide.A, users[0])
    green_5 = due(4, 5)
    # Checked 2 minutes ago: green, though it has waited for 50.
    green_acked = due(5, 50)
    world.ready(green_acked, MatchSide.B, users[1])
    episode_id = db.session.scalar(
        select(DbMatchDueEpisode.id).where(
            DbMatchDueEpisode.match_id == green_acked
        )
    )
    world.ack(first, green_acked, episode_id, minutes_ago=2)
    partial = world.match(first, contestants=1, round=2, match_order=6)
    done = world.match(first, confirmed=True, round=2, match_order=7)
    lobby = world.due_match(
        ffa, wait_minutes=3, contestants=3, occupied=True, round=1
    )
    frozen = world.due_match(paused, wait_minutes=5, round=1, match_order=1)
    unknown = world.match(legacy, round=1, match_order=1)
    round_one = world.due_match(
        round_robin, wait_minutes=2, round=1, match_order=1
    )
    round_two = world.match(round_robin, round=2, match_order=1)

    # `unknown` is due and shares its players with the other due matches,
    # which is a conflict; the paused `frozen` is nobody's demand. Within
    # their tier the conflict goes first, whatever the tournament names.
    # fmt: off
    full_order = [
        red, yellow_30, yellow_20, green_5, lobby, green_acked, round_one,
        unknown, frozen,
        partial, done, round_two,
    ]
    # fmt: on
    viewer = _admin(admin)

    def listing(per_page, number, **fields):
        return _page(
            viewer,
            party_id,
            scope='all',
            view='all',
            per_page=per_page,
            page=number,
            **fields,
        )

    assert _ids(listing(100, 1)) == full_order

    # The order is cut into pages, not the other way round.
    assert _ids(listing(5, 1)) == full_order[:5]
    assert _ids(listing(5, 2)) == full_order[5:10]
    assert _ids(listing(5, 3)) == full_order[10:]

    paired = {red, yellow_30, yellow_20, green_5, green_acked}
    expected = {
        'tier-red': {red},
        'tier-yellow': {yellow_30, yellow_20},
        'tier-green': {green_5, green_acked, lobby, round_one},
        'ready-both': {red},
        'ready-one': {yellow_20, green_acked},
        'ready-none': {yellow_30, green_5},
        'ready-unavailable': set(full_order) - paired,
        'pinned': {yellow_30},
        'review-open': set(),
    }
    for state, wanted in expected.items():
        page = listing(2, 1, state=state)
        assert page.total_count == len(wanted), state
        collected = []
        for number in range(1, page.total_pages + 1):
            part = listing(2, number, state=state)
            assert part.total_count == len(wanted), (state, number)
            collected += _ids(part)
        assert collected == [m for m in full_order if m in wanted], state

    # A filter is applied before the cut.
    green = listing(2, 2, state='tier-green')
    assert _ids(green) == [green_acked, round_one]
    assert green.total_pages == 2
    beyond = listing(2, 3, state='tier-green')
    assert beyond.rows == ()
    assert beyond.total_count == 4

    # Every state of the allowlist is a valid, consistent request.
    for state in DASHBOARD_STATES:
        page = listing(100, 1, state=state)
        assert page.total_count == len(page.rows)

    # The wait order reads the total, the interval order the alert.
    by_wait = _ids(listing(100, 1, sort='wait'))
    assert by_wait[:2] == [red, green_acked]
    by_tournament = _ids(listing(100, 1, sort='tournament'))
    assert by_tournament[: len(paired) + 2] == [
        red,
        yellow_30,
        yellow_20,
        green_5,
        green_acked,
        partial,
        done,
    ]


# -- empty and out-of-range pages --


def test_page_beyond_last_is_not_clamped_and_empty_reason_is_distinct(
    make_world, admin
):
    viewer = _admin(admin)

    # No assignment: nothing in the party, in either scope.
    nothing = make_world()
    for scope in ('assigned', 'all'):
        page = _page(viewer, nothing.party_id, scope=scope)
        assert page.rows == ()
        assert (page.total_count, page.total_pages) == (0, 0)
        assert page.empty_reason == 'no_assignment'
        assert page.tournament_choices == ()

    # A tournament without any match has no current demand.
    quiet = make_world()
    quiet.tournament()
    for view in ('due', 'upcoming', 'all'):
        page = _page(viewer, quiet.party_id, scope='all', view=view)
        assert page.empty_reason == 'no_current_demand', view

    # Fixtures that are no demand, told apart by the reason.
    waiting = make_world()
    running = waiting.tournament(name='Running', mode=ROUND_ROBIN)
    waiting.match(running, round=1, group_order=0, contestants=1)
    waiting.match(running, round=2, group_order=0)
    stopped = waiting.tournament(name='Stopped', status=PAUSED)
    waiting.due_match(stopped, round=1, match_order=1)
    waiting.match(stopped, round=1, match_order=2, contestants=1)
    before = waiting.tournament(
        name='Before', status=TournamentStatus.REGISTRATION_CLOSED
    )
    waiting.match(before, round=1, match_order=1)
    waiting.match(before, round=1, match_order=2, contestants=1)
    over = waiting.tournament(name='Over', status=TournamentStatus.COMPLETED)
    waiting.match(over, round=1, match_order=1)
    page = _page(viewer, waiting.party_id, scope='all')
    assert page.rows == ()
    assert page.empty_reason == 'no_actionable_fixtures'
    assert page.non_actionable_counts == DashboardNonActionableCounts(
        paused=1, pre_start=2, partial=3
    )
    # The view of what comes next is not empty, so it has no reason.
    upcoming = _page(viewer, waiting.party_id, scope='all', view='upcoming')
    assert upcoming.total_count == 5
    assert upcoming.empty_reason is None

    # Everything settled: nothing to do, and nothing to explain.
    settled = make_world()
    done = settled.tournament()
    settled.match(done, confirmed=True)
    page = _page(viewer, settled.party_id, scope='all')
    assert page.empty_reason == 'no_current_demand'
    assert page.non_actionable_counts == DashboardNonActionableCounts()

    # A filter that empties a view that is not empty.
    busy = make_world()
    first = busy.tournament(name='First')
    second = busy.tournament(name='Second')
    busy.due_match(first, wait_minutes=20, round=1, match_order=1)
    for fields in ({'state': 'tier-red'}, {'tournament_id': second}):
        page = _page(viewer, busy.party_id, scope='all', **fields)
        assert page.rows == ()
        assert page.empty_reason == 'no_filter_matches', fields
        assert page.tier_counts.yellow == 1
    # A filter on a view that is empty anyway is no reason.
    page = _page(
        viewer, busy.party_id, scope='all', view='upcoming', state='pinned'
    )
    assert page.empty_reason == 'no_current_demand'

    # The page is the requested one, whether or not it exists.
    paged = make_world()
    tournament_id = paged.tournament()
    ids = [
        paged.due_match(
            tournament_id, wait_minutes=10 + order, round=1, match_order=order
        )
        for order in range(5)
    ]
    last = _page(viewer, paged.party_id, scope='all', per_page=2, page=3)
    assert len(last.rows) == 1
    assert (last.page, last.total_pages, last.total_count) == (3, 3, 5)
    assert last.empty_reason is None
    for number in (4, 99, 10**6):
        page = _page(
            viewer, paged.party_id, scope='all', per_page=2, page=number
        )
        assert page.rows == ()
        assert page.page == number
        assert page.per_page == 2
        assert page.total_pages == 3
        assert page.total_count == 5
        assert page.empty_reason is None
        assert page.tier_counts == DashboardTierCounts(green=5)
    assert set(ids) == {
        row.match_id for row in _all_rows(viewer, paged.party_id, scope='all')
    }

    # A page that is not a page at all is refused, not clamped.
    for bad in (0, -1, 10**6 + 1):
        result = service.get_dashboard_page(
            viewer,
            paged.party_id,
            _query(scope='all', page=bad),
            settings=SETTINGS,
            now=NOW,
        )
        assert result.unwrap_err() == service.DASHBOARD_QUERY_INVALID_ERROR


def test_non_actionable_counts_and_leaderboard_cards_are_scoped(
    world, party_id, admin, users
):
    mine = world.tournament(name='Mine', mode=ROUND_ROBIN)
    world.match(mine, round=1, group_order=0, contestants=1)
    world.match(mine, round=2, group_order=0)
    my_board = world.tournament(
        name='My board',
        game='Comet Run',
        game_format=HIGHSCORE,
        mode=EliminationMode.NONE,
    )
    other = world.tournament(name='Other', status=PAUSED)
    world.due_match(other, round=1, match_order=1)
    other_board = world.tournament(
        name='Other board',
        game_format=HIGHSCORE,
        mode=EliminationMode.NONE,
    )
    # A leaderboard-only tournament has no match to list.
    world.match(my_board, round=1)
    scoped = users[3]
    world.orga(mine, scoped)
    world.orga(my_board, scoped)

    viewer = _viewer(scoped)
    page = _page(viewer, party_id, view='all')
    assert page.non_actionable_counts == DashboardNonActionableCounts(partial=2)
    assert [c.tournament_id for c in page.tournament_choices] == [
        mine,
        my_board,
    ]
    assert [t.tournament_id for t in page.leaderboard_only_tournaments] == [
        my_board
    ]
    assert page.leaderboard_only_tournaments[0].name == 'My board'
    assert page.leaderboard_only_tournaments[0].game == 'Comet Run'
    # The cards are not rows, and do not count.
    assert page.total_count == 2
    assert len(page.rows) == 2

    # Only the whole list of every match carries them.
    for fields in (
        {'view': 'due'},
        {'view': 'upcoming'},
        {'view': 'all', 'state': 'pinned'},
        {'view': 'all', 'tournament_id': mine},
    ):
        assert (
            _page(viewer, party_id, **fields).leaderboard_only_tournaments == ()
        ), fields
    filtered = _page(viewer, party_id, view='all', tournament_id=my_board)
    assert [t.tournament_id for t in filtered.leaderboard_only_tournaments] == [
        my_board
    ]

    # The same for what is out of reach.
    admin_page = _page(_admin(admin), party_id, scope='all', view='all')
    assert admin_page.non_actionable_counts == DashboardNonActionableCounts(
        paused=1, partial=2
    )
    assert {
        t.tournament_id for t in admin_page.leaderboard_only_tournaments
    } == {
        my_board,
        other_board,
    }
    assert {c.tournament_id for c in admin_page.tournament_choices} == {
        mine,
        my_board,
        other,
        other_board,
    }


# -- one consistent snapshot, and a read that writes nothing --

_CONNECTION_SETTINGS_SQL = text(
    "SELECT current_setting('transaction_isolation'),"
    " current_setting('transaction_read_only'),"
    " current_setting('default_transaction_isolation'),"
    " current_setting('default_transaction_read_only')"
)


def _settings_of_pooled_connection(pid: int) -> tuple:
    """Read the settings of the pooled connection that has that backend."""
    held = []
    try:
        for _ in range(12):
            connection = db.engine.connect()
            held.append(connection)
            if (
                connection.execute(text('SELECT pg_backend_pid()')).scalar()
                == pid
            ):
                return tuple(connection.execute(_CONNECTION_SETTINGS_SQL).one())
    finally:
        for connection in held:
            connection.close()
    pytest.fail('the connection of the snapshot is not in the pool')


@contextmanager
def _plain_reads():
    """Stand in for `read_snapshot` without any isolation."""
    db.session.rollback()
    try:
        yield
    finally:
        db.session.rollback()


def _rename_from_another_connection(tournament_id, name) -> None:
    with db.engine.begin() as connection:
        connection.execute(
            text('UPDATE lan_tournaments SET name = :name WHERE id = :id'),
            {'name': name, 'id': tournament_id},
        )


def test_snapshot_reads_are_consistent_across_statements(
    make_world, admin, monkeypatch
):
    # The counts and the rows are one statement. What the snapshot still
    # keeps together is that statement and the reads that follow it: the
    # tournament choices must name what the rows were read from.
    viewer = _admin(admin)
    original = repository._query_tournaments
    inside: dict = {}

    def commit_after_the_rows(scope, query):
        # The page statement has run. Another session now renames the
        # tournament, before the choices are read.
        inside['settings'] = tuple(
            db.session.execute(_CONNECTION_SETTINGS_SQL).one()
        )
        inside['pid'] = db.session.scalar(text('SELECT pg_backend_pid()'))
        _rename_from_another_connection(inside['tournament'], inside['renamed'])
        return original(scope, query)

    monkeypatch.setattr(repository, '_query_tournaments', commit_after_the_rows)

    world = make_world()
    tournament_id = world.tournament(name='Seam original')
    inside['tournament'] = tournament_id
    inside['renamed'] = 'Seam renamed'
    ids = [
        world.due_match(
            tournament_id, wait_minutes=10 + order, round=1, match_order=order
        )
        for order in range(3)
    ]

    page = _page(viewer, world.party_id, scope='all')

    # The rows and the choices show the same moment, before the commit.
    assert page.total_count == 3
    assert set(_ids(page)) == set(ids)
    assert page.tier_counts == DashboardTierCounts(green=3)
    assert {row.tournament_name for row in page.rows} == {'Seam original'}
    assert [c.name for c in page.tournament_choices] == ['Seam original']
    # The commit is real: the next snapshot sees it, in rows and choices.
    later = _page(viewer, world.party_id, scope='all')
    assert {row.tournament_name for row in later.rows} == {'Seam renamed'}
    assert [c.name for c in later.tournament_choices] == ['Seam renamed']

    # Isolation is scoped to the transaction. Nothing stays on the
    # pooled connection, which is back in the pool and usable.
    assert inside['settings'] == (
        'repeatable read',
        'on',
        'read committed',
        'off',
    )
    assert _settings_of_pooled_connection(inside['pid']) == (
        'read committed',
        'off',
        'read committed',
        'off',
    )
    assert not db.session().in_transaction()

    # Control: without the snapshot, the very same commit splits them.
    control = make_world()
    control_tournament = control.tournament(name='Control original')
    inside['tournament'] = control_tournament
    inside['renamed'] = 'Control renamed'
    for order in range(3):
        control.due_match(
            control_tournament,
            wait_minutes=10 + order,
            round=1,
            match_order=order,
        )
    monkeypatch.setattr(repository, 'read_snapshot', _plain_reads)

    split = _page(viewer, control.party_id, scope='all')

    assert {row.tournament_name for row in split.rows} == {'Control original'}
    assert [c.name for c in split.tournament_choices] == ['Control renamed']


def test_the_snapshot_refuses_to_drop_pending_changes(world, users):
    tournament_id = world.tournament()
    pending = DbTournamentOrga(
        TournamentOrgaID(uuid7()), tournament_id, users[4].id, NOW
    )
    db.session.add(pending)

    with pytest.raises(RuntimeError):
        with repository.read_snapshot():
            pytest.fail('the snapshot must not start')

    assert pending in db.session.new
    db.session.rollback()


@contextmanager
def _recorded_statements():
    statements: list[str] = []

    def on_execute(connection, cursor, statement, *args):
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', on_execute)
    try:
        yield statements
    finally:
        event.remove(db.engine, 'before_cursor_execute', on_execute)


def test_a_read_writes_nothing_and_takes_a_bounded_number_of_statements(
    make_world, admin, users
):
    world = make_world()
    tournament_id = world.tournament(name='Budget')
    world.orga(tournament_id, users[1])
    ids = []
    for order in range(40):
        ids.append(
            world.due_match(
                tournament_id,
                wait_minutes=1 + order,
                round=1,
                match_order=order,
                pairing=True,
            )
        )
    world.pin(tournament_id, ids[3], users[2])
    episode_id = db.session.scalar(
        select(DbMatchDueEpisode.id).where(DbMatchDueEpisode.match_id == ids[4])
    )
    world.ack(tournament_id, ids[4], episode_id, actor=users[3])
    viewer = _admin(admin)

    counts = {}
    for per_page in (5, 40):
        with _recorded_statements() as statements:
            # No `now`: the snapshot reads the server clock itself.
            result = service.get_dashboard_page(
                viewer,
                world.party_id,
                _query(scope='all', per_page=per_page),
                settings=SETTINGS,
            )
        page = result.unwrap()
        assert len(page.rows) == per_page
        counts[per_page] = len(statements)

        for statement in statements:
            head = statement.lstrip().split(None, 1)[0].upper()
            assert head in {'SELECT', 'WITH'}, statement
            assert 'FOR UPDATE' not in statement.upper()
            assert 'FOR SHARE' not in statement.upper()
        assert 'clock_timestamp' in statements[0]
        assert abs(
            page.as_of - datetime.now(UTC).replace(tzinfo=None)
        ) < timedelta(minutes=5)

    # Growth of the page adds no statement, and the budget holds.
    assert counts[5] == counts[40]
    assert counts[5] <= 12


# -- the facts of a row --


def _row_of(page, match_id):
    return next(row for row in page.rows if row.match_id == match_id)


def _rows_by_id(viewer, party_id, **fields):
    rows = _all_rows(viewer, party_id, scope='all', view='all', **fields)
    return {row.match_id: row for row in rows}


def test_done_and_bye_rows_keep_the_closed_wait_and_the_result(
    world, party_id, admin
):
    tournament_id = world.tournament()
    settled = world.match(
        tournament_id,
        confirmed=True,
        scores=[2, 1],
        pairing=True,
        round=1,
        match_order=1,
    )
    world.episode(
        tournament_id, settled, wait_minutes=20, closed_wait_minutes=14
    )
    bye = world.match(
        tournament_id, confirmed=True, contestants=1, round=1, match_order=2
    )
    no_scores = world.match(
        tournament_id, confirmed=True, round=1, match_order=3
    )
    over = world.tournament(name='Over', status=TournamentStatus.COMPLETED)
    unplayed = world.match(over, round=1, match_order=1)

    rows = _rows_by_id(_admin(admin), party_id)

    done = rows[settled]
    assert done.state is DashboardRowState.DONE
    assert done.tier is None
    assert done.total_active_wait_us is None
    assert done.alert_interval_us is None
    assert done.closed_episode_wait_us == 14 * MINUTE_US
    assert done.episode_id is None
    assert done.ack_unavailable_reason is AckUnavailableReason.TERMINAL
    assert done.status_note.code == service.STATUS_NOTE_RESULT_CONFIRMED
    assert dict(done.status_note.params) == {'score_a': 2, 'score_b': 1}
    assert done.readiness_available

    assert rows[bye].state is DashboardRowState.BYE
    assert rows[bye].status_note.code == service.STATUS_NOTE_BYE_ADVANCE
    assert len(rows[bye].contestant_names) == 1
    assert rows[bye].ack_unavailable_reason is AckUnavailableReason.TERMINAL

    assert rows[no_scores].state is DashboardRowState.DONE
    assert rows[no_scores].status_note is None
    assert rows[no_scores].closed_episode_wait_us is None

    # A match of a finished tournament is done, whatever its result.
    assert rows[unplayed].state is DashboardRowState.DONE
    assert rows[unplayed].status_note is None
    assert (
        rows[unplayed].ack_unavailable_reason is AckUnavailableReason.TERMINAL
    )


def test_a_paused_row_keeps_its_frozen_waits_and_its_record(
    world, party_id, admin, users
):
    tournament_id = world.tournament(status=PAUSED)
    match_id = world.due_match(tournament_id, wait_minutes=25, round=1)
    episode_id = db.session.scalar(
        select(DbMatchDueEpisode.id).where(
            DbMatchDueEpisode.match_id == match_id
        )
    )
    world.ack(tournament_id, match_id, episode_id, minutes_ago=5, comment='ok')

    viewer = _admin(admin)
    assert _page(viewer, party_id, scope='all').rows == ()
    assert _page(viewer, party_id, scope='all', view='upcoming').rows == ()

    row = _rows_by_id(viewer, party_id)[match_id]
    assert row.state is DashboardRowState.PAUSED
    assert row.tier is None
    assert row.total_active_wait_us == 25 * MINUTE_US
    assert row.alert_interval_us == 5 * MINUTE_US
    assert row.ack_unavailable_reason is AckUnavailableReason.PAUSED
    assert row.latest_acknowledgement.comment == 'ok'
    assert row.episode_id == episode_id

    # The wait is frozen: a later snapshot reads the same values.
    later = _rows_by_id(viewer, party_id)[match_id]
    assert later.total_active_wait_us == row.total_active_wait_us


def test_episode_and_acknowledgement_facts_of_a_row(
    world, party_id, admin, users, monkeypatch
):
    monkeypatch.setattr(service, 'gettext', lambda text: f'T[{text}]')
    tournament_id = world.tournament()
    reopened = world.match(tournament_id, round=1, match_order=1)
    world.episode(
        tournament_id,
        reopened,
        wait_minutes=60,
        closed_wait_minutes=30,
        pairing_key='same',
    )
    open_id = world.episode(
        tournament_id, reopened, wait_minutes=40, pairing_key='same'
    )
    repaired = world.match(tournament_id, round=1, match_order=2)
    world.episode(
        tournament_id,
        repaired,
        wait_minutes=60,
        closed_wait_minutes=30,
        pairing_key='old',
    )
    world.episode(tournament_id, repaired, wait_minutes=3, pairing_key='new')
    first = world.due_match(
        tournament_id, wait_minutes=40, round=1, match_order=3
    )

    # Four checks of the current episode, one of them by a gone user, and
    # one check that belongs to the closed one.
    closed_id = db.session.scalar(
        select(DbMatchDueEpisode.id).where(
            DbMatchDueEpisode.match_id == reopened,
            DbMatchDueEpisode.closed_at.is_not(None),
        )
    )
    world.ack(tournament_id, reopened, closed_id, minutes_ago=50, comment='old')
    for revision, (actor, minutes_ago) in enumerate(
        [(users[0], 30), (users[1], 20), (None, 12), (users[2], 4)], start=1
    ):
        ack_id = MatchEscalationAcknowledgementID(uuid7())
        repo.create_escalation_ack_flush(
            MatchEscalationAcknowledgement(
                id=ack_id,
                episode_id=open_id,
                tournament_id=tournament_id,
                match_id=reopened,
                revision=revision,
                occurred_at=NOW - timedelta(minutes=minutes_ago),
                clock_us=CLOCK_AT_NOW_US - minutes_ago * MINUTE_US,
                actor_id=actor.id if actor else uuid7(),
                comment=f'check {revision}',
            )
        )
    db.session.execute(
        update(DbMatchDueEpisode)
        .where(DbMatchDueEpisode.id == open_id)
        .values(ack_revision=4)
    )
    db.session.commit()

    rows = _rows_by_id(_admin(admin), party_id)

    row = rows[reopened]
    assert row.has_prior_episode
    assert row.status_note.code == service.STATUS_NOTE_CORRECTED_REOPENED
    assert row.episode_id == open_id
    assert row.episode_opened_at == NOW - timedelta(minutes=40)
    assert row.ack_revision == 4
    assert row.acknowledgement_count == 4
    assert [a.revision for a in row.recent_acknowledgements] == [4, 3, 2]
    assert row.latest_acknowledgement == row.recent_acknowledgements[0]
    assert [a.comment for a in row.recent_acknowledgements] == [
        'check 4',
        'check 3',
        'check 2',
    ]
    assert [a.actor_display_name for a in row.recent_acknowledgements] == [
        users[2].screen_name,
        'T[Deleted orga]',
        users[1].screen_name,
    ]
    # The wait is the episode's, the interval runs from the latest check.
    assert row.total_active_wait_us == 40 * MINUTE_US
    assert row.alert_interval_us == 4 * MINUTE_US

    other = rows[repaired]
    assert other.has_prior_episode
    assert other.status_note is None
    assert other.acknowledgement_count == 0
    assert other.recent_acknowledgements == ()
    assert other.latest_acknowledgement is None

    plain = rows[first]
    assert not plain.has_prior_episode
    assert plain.status_note is None


def _derived_readiness(match_id, *, one_v_one: bool):
    pairing = repo.get_match_pairings_for_matches([match_id]).get(match_id)
    return derive_match_readiness(
        repo.get_match(match_id),
        repo.get_contestants_for_match(match_id),
        pairing=pairing,
        supports_readiness=one_v_one,
    )


def test_the_ready_projection_equals_the_pure_derivation(
    world, party_id, admin, users
):
    tournament_id = world.tournament()
    first, second, third, fourth = world.participants(tournament_id, 4)
    team_a = world.team(tournament_id, 'Team A')
    team_b = world.team(tournament_id, 'Team B')
    cases = {}

    def paired(name, **fields):
        match_id = world.match(
            tournament_id,
            pairing=True,
            round=1,
            match_order=len(cases),
            **fields,
        )
        cases[name] = match_id
        return match_id

    # A valid pair without a claim, with one claim and with both.
    paired('none')
    one = paired('one')
    world.ready(one, MatchSide.A, users[0])
    both = paired('both')
    world.ready(both, MatchSide.A, users[0])
    world.ready(both, MatchSide.B, users[1])
    only_b = paired('only_b')
    world.ready(only_b, MatchSide.B, users[1])
    teams = paired('teams', contestants=[], teams=[team_a, team_b])
    world.ready(teams, MatchSide.B, users[1])
    confirmed = paired('confirmed', confirmed=True)
    world.ready(confirmed, MatchSide.A, users[0])

    # A pair that no longer stands: a claim stays behind, and must not show.
    stale = paired('stale')
    world.ready(stale, MatchSide.A, users[0])
    db.session.execute(
        update(DbTournamentMatchToContestant)
        .where(
            DbTournamentMatchToContestant.tournament_match_id == stale,
            DbTournamentMatchToContestant.participant_id == second,
        )
        .values(participant_id=third)
    )
    ended = paired('ended')
    world.ready(ended, MatchSide.A, users[0])
    db.session.execute(
        text(
            'UPDATE lan_tournament_match_pairings SET ended_at = :at'
            ' WHERE match_id = :id'
        ),
        {'at': NOW, 'id': ended},
    )
    old_generation = paired('old_generation')
    world.ready(old_generation, MatchSide.B, users[1])
    db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id == old_generation)
        .values(pairing_generation=DbTournamentMatch.pairing_generation + 1)
    )
    db.session.commit()

    # No pair was ever made, mixed sides, a lobby, a lone side.
    cases['no_pairing'] = world.match(tournament_id, round=2, match_order=1)
    cases['mixed'] = world.match(
        tournament_id,
        contestants=[fourth],
        teams=[team_a],
        round=2,
        match_order=2,
        pairing=True,
    )
    cases['lone'] = world.match(
        tournament_id, contestants=1, round=2, match_order=3, pairing=True
    )
    ffa = world.tournament(name='Lobby', game_format=FREE_FOR_ALL, mode=SINGLE)
    cases['lobby'] = world.match(ffa, contestants=3, occupied=True, round=1)
    world.ready(cases['no_pairing'], MatchSide.A, users[0])

    rows = _rows_by_id(_admin(admin), party_id)
    expected = {
        name: _derived_readiness(match_id, one_v_one=name != 'lobby')
        for name, match_id in cases.items()
    }
    assert {expected[n].pairing_valid for n in cases} == {True, False}
    for name, match_id in cases.items():
        row, derived = rows[match_id], expected[name]
        assert row.readiness_available == derived.pairing_valid, name
        assert row.ready_at_a == derived.ready_at_a, name
        assert row.ready_at_b == derived.ready_at_b, name

    # The stale and the ended pair claim nothing, though columns are set.
    for name in ('stale', 'ended', 'old_generation', 'no_pairing'):
        assert not rows[cases[name]].readiness_available, name
        assert rows[cases[name]].ready_at_a is None, name
        assert rows[cases[name]].ready_at_b is None, name

    # The filters make the same distinctions.
    buckets = {
        'ready-none': lambda d: d.pairing_valid and not d.ready_sides,
        'ready-one': lambda d: d.pairing_valid and len(d.ready_sides) == 1,
        'ready-both': lambda d: d.pairing_valid and len(d.ready_sides) == 2,
        'ready-unavailable': lambda d: not d.pairing_valid,
    }
    for state, belongs in buckets.items():
        page = _page(
            _admin(admin),
            party_id,
            scope='all',
            view='all',
            state=state,
            per_page=100,
        )
        assert set(_ids(page)) == {
            cases[name] for name, d in expected.items() if belongs(d)
        }, state


def test_row_identity_names_and_labels(
    world, party_id, admin, users, confirmer, make_user, monkeypatch
):
    monkeypatch.setattr(service, 'gettext', lambda text: f'T[{text}]')
    gone = make_user('F03ScopeGone', deleted=True)
    members = World(party_id, [users[0], users[1], gone], confirmer)
    tournament_id = members.tournament(
        name='Arena Cup',
        game='Arena Five',
        mode=ROUND_ROBIN,
        playoff=_PLAYOFF_RR_TO_KNOCKOUT,
    )
    p0, p1, p_gone = members.participants(tournament_id)
    kupfer = members.team(tournament_id, 'Kupferfüchse')
    nachtbus = members.team(tournament_id, 'Nachtbus')

    # Side A is the contestant the pairing says, not the first row.
    match = members.match(
        tournament_id,
        contestants=[p1, p0],
        pairing=True,
        round=2,
        group_order=1,
        match_order=3,
        occupied=True,
    )
    members.ready(match, MatchSide.A, users[0])
    members.episode(tournament_id, match, wait_minutes=4)
    # A Ready claim is a domain change, so the time is set afterwards.
    db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id == match)
        .values(last_changed_at=NOW - timedelta(minutes=2))
    )
    db.session.commit()

    def names() -> tuple:
        page = _page(
            _admin(admin), party_id, scope='all', view='all', per_page=100
        )
        return _row_of(page, match).contestant_names

    assert names() == (users[1].screen_name, users[0].screen_name)
    db.session.execute(
        text(
            'UPDATE lan_tournament_match_pairings'
            ' SET side_a_id = side_b_id, side_b_id = side_a_id'
            ' WHERE match_id = :id'
        ),
        {'id': match},
    )
    db.session.commit()
    assert names() == (users[0].screen_name, users[1].screen_name)

    teams = members.match(
        tournament_id,
        contestants=[],
        teams=[kupfer, nachtbus],
        pairing=True,
        round=2,
        group_order=1,
        match_order=4,
    )
    with_gone = members.match(
        tournament_id,
        contestants=[p0, p_gone],
        round=2,
        group_order=1,
        match_order=5,
    )
    final = members.match(
        tournament_id,
        phase=2,
        round=3,
        bracket='LB',
        match_order=1,
    )
    members.episode(tournament_id, final, wait_minutes=1)
    for minutes, user in enumerate([users[2], users[0], gone]):
        db.session.add(
            DbTournamentOrga(
                uuid7(),
                tournament_id,
                user.id,
                NOW + timedelta(minutes=minutes),
            )
        )
    db.session.commit()
    members.pin(tournament_id, teams, gone)
    members.pin(tournament_id, with_gone, users[1])

    rows = _rows_by_id(_admin(admin), party_id)

    row = rows[match]
    assert row.tournament_name == 'Arena Cup'
    assert row.game == 'Arena Five'
    assert row.game_format is ONE_V_ONE
    assert row.elimination_mode is ROUND_ROBIN
    assert row.location.phase == 1
    assert (row.location.round, row.location.group_order) == (2, 1)
    assert row.location.match_order == 3
    assert row.location.bracket is None
    assert row.created_at == NOW - timedelta(hours=1)
    assert row.last_changed_at == NOW - timedelta(minutes=2)
    assert row.occupied_since == NOW - timedelta(minutes=30)
    assert row.ready_at_a == NOW - timedelta(minutes=9)
    assert row.ready_at_b is None
    # Orgas read in the order of their assignment, the gone one is left out.
    assert row.orga_names == (users[2].screen_name, users[0].screen_name)
    assert row.pinned_at is None
    assert row.pinned_by_name is None
    assert row.pin_revision == 0

    assert rows[teams].contestant_names == ('Kupferfüchse', 'Nachtbus')
    assert rows[teams].pinned_by_name == 'T[Deleted orga]'
    assert rows[teams].pin_revision == 1
    assert rows[with_gone].contestant_names == (
        users[0].screen_name,
        'T[Deleted user]',
    )
    assert rows[with_gone].pinned_by_name == users[1].screen_name

    # The playoff phase is judged by its own format and place.
    assert rows[final].game_format is ONE_V_ONE
    assert rows[final].elimination_mode is SINGLE
    assert rows[final].location.phase == 2
    assert rows[final].location.bracket is Bracket.LOSERS
    assert rows[final].location.round == 3


def test_notes_explain_rows_that_are_not_due(world, party_id, admin, users):
    # A round that is not the group's earliest open round.
    robin = world.tournament(name='Robin', mode=ROUND_ROBIN)
    open_round = world.match(
        robin, contestants=1, round=1, group_order=0, match_order=1
    )
    later = world.match(robin, round=3, group_order=0, match_order=2)
    # A lobby that is not complete.
    lobbies = world.tournament(
        name='Lobbies',
        game_format=FREE_FOR_ALL,
        mode=SINGLE,
        group_size_max=4,
    )
    waiting = world.match(
        lobbies, contestants=2, round=2, bracket='WB', match_order=1
    )
    # FFA rounds are zero-based: round 0 is the first, round 1 waits for it.
    first_round = world.match(
        lobbies, contestants=1, round=0, bracket='WB', match_order=2
    )
    second_round = world.match(
        lobbies, contestants=3, round=1, bracket='WB', match_order=3
    )
    sized = world.tournament(
        name='Unsized', game_format=FREE_FOR_ALL, mode=SINGLE
    )
    unsized = world.match(sized, contestants=3, round=1)
    full = world.due_match(
        lobbies,
        wait_minutes=2,
        contestants=4,
        occupied=True,
        round=2,
        bracket='LB',
        match_order=1,
    )

    rows = _rows_by_id(_admin(admin), party_id)

    assert rows[open_round].state is DashboardRowState.PARTIAL
    assert rows[open_round].status_note is None
    assert rows[later].state is DashboardRowState.UPCOMING
    assert (
        rows[later].status_note.code == service.STATUS_NOTE_EARLIER_ROUND_OPEN
    )
    assert dict(rows[later].status_note.params) == {'round': 1}
    assert rows[later].ack_unavailable_reason is AckUnavailableReason.NOT_DUE

    assert rows[waiting].state is DashboardRowState.AWAITING_LOBBY
    assert rows[waiting].status_note.code == service.STATUS_NOTE_LOBBY_WAITING
    assert dict(rows[waiting].status_note.params) == {
        'filled': 2,
        'size': 4,
        'after_round': 1,
    }
    assert len(rows[waiting].contestant_names) == 2
    assert dict(rows[first_round].status_note.params) == {
        'filled': 1,
        'size': 4,
    }
    assert dict(rows[second_round].status_note.params) == {
        'filled': 3,
        'size': 4,
        'after_round': 0,
    }
    assert dict(rows[unsized].status_note.params) == {
        'filled': 3,
        'after_round': 0,
    }
    assert rows[waiting].readiness_available is False
    assert rows[full].state is DashboardRowState.DUE
    assert len(rows[full].contestant_names) == 4
    assert rows[full].status_note is None


def test_tiers_follow_the_effective_thresholds_of_the_party(
    world, party_id, admin, users
):
    tournament_id = world.tournament()
    world.due_match(tournament_id, wait_minutes=20, round=1, match_order=1)
    world.due_match(tournament_id, wait_minutes=50, round=1, match_order=2)
    viewer = _admin(admin)

    def tiers(settings):
        page = _page(viewer, party_id, scope='all', settings=settings)
        return [row.tier for row in page.rows], page.tier_counts

    default = settings_service.get_effective_dashboard_settings(
        party_id
    ).unwrap()
    assert default.threshold_source == 'deployment'
    assert tiers(default) == (
        [TrafficTier.RED, TrafficTier.YELLOW],
        DashboardTierCounts(red=1, yellow=1),
    )

    settings_service.set_party_thresholds(
        party_id,
        yellow_minutes=30,
        red_minutes=90,
        expected_revision=0,
        expected_updated_at=None,
        initiator_id=users[0].id,
    ).unwrap()
    party = settings_service.get_effective_dashboard_settings(party_id).unwrap()

    assert party.threshold_source == 'party'
    assert tiers(party) == (
        [TrafficTier.YELLOW, TrafficTier.GREEN],
        DashboardTierCounts(yellow=1, green=1),
    )
