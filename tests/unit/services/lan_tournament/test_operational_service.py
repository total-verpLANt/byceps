"""
tests.unit.services.lan_tournament.test_operational_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Any

import pytest

from byceps.services.lan_tournament import tournament_operational_service
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
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (  # noqa: E501
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
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
from byceps.services.lan_tournament.tournament_operational_service import (
    invalidate_due_matches_flush,
    LOBBY_NOT_FREE_FOR_ALL_ERROR,
    LOBBY_ROSTER_INCOMPLETE_ERROR,
    mark_completed_lobbies_occupied_flush,
    reconcile_due_matches_flush,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


SERVICE = tournament_operational_service

MINUTE = timedelta(minutes=1)
MINUTE_US = 60 * 1_000_000
PLUS_TWO = timezone(timedelta(hours=2))

T0 = datetime(2026, 10, 7, 12, 0)

ONE_V_ONE = GameFormat.ONE_V_ONE
FREE_FOR_ALL = GameFormat.FREE_FOR_ALL
SINGLE = EliminationMode.SINGLE_ELIMINATION


def make_tournament(
    *,
    status: TournamentStatus = TournamentStatus.ONGOING,
    game_format: GameFormat = ONE_V_ONE,
    elapsed_us: int = 0,
    running_since: datetime | None = T0,
    activated_at: datetime | None = T0,
    **overrides: Any,
) -> Tournament:
    values: dict[str, Any] = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('party-1'),
        'name': 'Test Cup',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': T0,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': ContestantType.SOLO,
        'tournament_status': status,
        'game_format': game_format,
        'elimination_mode': SINGLE,
        'operational_clock_elapsed_us': elapsed_us,
        'operational_clock_running_since': running_since,
        'operational_clock_activated_at': activated_at,
    }
    values.update(overrides)
    return Tournament(**values)


def make_contestant(
    match_id: TournamentMatchID,
    participant_id: TournamentParticipantID | None = None,
) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=participant_id
        or TournamentParticipantID(generate_uuid()),
        score=None,
        created_at=T0,
    )


class FakeRepository:
    """The repository functions the operational service may use.

    Any other attribute fails the test. The service is flush-only, so a
    commit, a rollback or a writer outside the episode ones is a defect.
    """

    def __init__(self) -> None:
        self.tournaments: dict[TournamentID, Tournament] = {}
        self.matches: dict[TournamentMatchID, TournamentMatch] = {}
        self.contestants: dict[
            TournamentMatchID, list[TournamentMatchToContestant]
        ] = {}
        self.episodes: list[MatchDueEpisode] = []
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f'the service must not use repository.{name}')

    # setup

    def add_tournament(self, tournament: Tournament) -> Tournament:
        self.tournaments[tournament.id] = tournament
        return tournament

    def add_match(
        self,
        tournament: Tournament,
        *,
        contestants: int = 2,
        confirmed: bool = False,
        occupied: bool = False,
        round: int | None = 0,
        phase: int = 1,
    ) -> TournamentMatchID:
        match_id = TournamentMatchID(generate_uuid())
        self.matches[match_id] = TournamentMatch(
            id=match_id,
            tournament_id=tournament.id,
            group_order=None,
            match_order=len(self.matches),
            round=round,
            next_match_id=None,
            confirmed_by=UserID(generate_uuid()) if confirmed else None,
            created_at=T0,
            phase=phase,
            occupied_since=T0 if occupied else None,
        )
        self.contestants[match_id] = [
            make_contestant(match_id) for _ in range(contestants)
        ]
        return match_id

    def add_open_episode(
        self,
        match_id: TournamentMatchID,
        *,
        opened_clock_us: int = 0,
        ack_revision: int = 0,
        key: str | None = None,
    ) -> MatchDueEpisode:
        episode = MatchDueEpisode(
            id=MatchDueEpisodeID(generate_uuid()),
            tournament_id=self.matches[match_id].tournament_id,
            match_id=match_id,
            pairing_key=(
                key
                if key is not None
                else pairing_key(self.contestants[match_id])
            ),
            opened_at=T0,
            opened_clock_us=opened_clock_us,
            ack_revision=ack_revision,
        )
        self.episodes.append(episode)
        return episode

    # inspection

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def open_episodes(self) -> list[MatchDueEpisode]:
        return [e for e in self.episodes if e.closed_at is None]

    def episodes_of(self, match_id: TournamentMatchID) -> list[MatchDueEpisode]:
        return [e for e in self.episodes if e.match_id == match_id]

    # the repository functions the service uses

    def lock_tournament_for_update(self, tournament_id) -> None:
        self.calls.append(('lock_tournament_for_update', tournament_id))

    def lock_matches_for_update(self, match_ids) -> None:
        self.calls.append(('lock_matches_for_update', list(match_ids)))

    def get_tournament(self, tournament_id, *, fresh: bool = False):
        self.calls.append(('get_tournament', fresh))
        assert fresh, 'the service must re-read the tournament after locking'
        if tournament_id not in self.tournaments:
            raise ValueError(f'Unknown tournament ID "{tournament_id}"')
        return self.tournaments[tournament_id]

    def get_matches_by_ids(self, match_ids):
        self.calls.append(('get_matches_by_ids', list(match_ids)))
        return [self.matches[i] for i in match_ids if i in self.matches]

    def get_matches_for_tournament_ordered_fresh(self, tournament_id):
        self.calls.append(('get_matches_for_tournament_ordered_fresh', None))
        return [
            m for m in self.matches.values() if m.tournament_id == tournament_id
        ]

    def get_contestants_for_matches(self, match_ids):
        self.calls.append(('get_contestants_for_matches', list(match_ids)))
        return {
            i: list(self.contestants[i])
            for i in match_ids
            if self.contestants.get(i)
        }

    def set_ffa_lobby_occupied_since_if_unset_flush(
        self, match_id, occupied_since
    ) -> bool:
        self.calls.append(
            ('set_ffa_lobby_occupied_since_if_unset_flush', match_id)
        )
        match = self.matches[match_id]
        if match.occupied_since is not None:
            return False
        self.matches[match_id] = replace(match, occupied_since=occupied_since)
        return True

    def list_open_due_episodes(self, tournament_id):
        self.calls.append(('list_open_due_episodes', tournament_id))
        return [
            e for e in self.open_episodes() if e.tournament_id == tournament_id
        ]

    def open_due_episode_flush(self, episode: MatchDueEpisode) -> None:
        self.calls.append(('open_due_episode_flush', episode))
        assert not any(
            e.match_id == episode.match_id for e in self.open_episodes()
        ), 'uq_lan_tournament_due_episodes_open_match'
        self.episodes.append(episode)

    def close_due_episodes_flush(
        self, match_ids, *, occurred_at: datetime, clock_us: int
    ) -> None:
        self.calls.append(
            (
                'close_due_episodes_flush',
                (list(match_ids), occurred_at, clock_us),
            )
        )
        wanted = set(match_ids)
        self.episodes = [
            (
                replace(e, closed_at=occurred_at, closed_clock_us=clock_us)
                if e.match_id in wanted and e.closed_at is None
                else e
            )
            for e in self.episodes
        ]


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepository()
    monkeypatch.setattr(SERVICE, 'tournament_repository', fake)
    return fake


# -------------------------------------------------------------------- #
# flush-only, no read-side activation
# -------------------------------------------------------------------- #


def test_reconcile_is_flush_only_and_not_get_initialized(repo):
    started = repo.add_tournament(make_tournament())
    due = repo.add_match(started)
    legacy = repo.add_tournament(
        make_tournament(running_since=None, activated_at=None)
    )
    repo.add_match(legacy)
    at = T0 + 5 * MINUTE

    # The strict fake refuses a commit, a rollback and any other writer.
    assert reconcile_due_matches_flush(started.id, occurred_at=at).is_ok()
    assert invalidate_due_matches_flush([due], occurred_at=at).is_ok()
    lobby = repo.add_match(
        repo.add_tournament(
            make_tournament(game_format=FREE_FOR_ALL, group_size_min=2)
        ),
        occupied=True,
    )
    assert mark_completed_lobbies_occupied_flush(
        [lobby], occurred_at=at
    ).is_ok()
    assert not {
        'commit_session',
        'rollback_session',
    } & set(repo.names())

    # A tournament without known clock history is neither read for
    # matches nor given an episode, however due its matches look.
    repo.calls.clear()
    before = list(repo.episodes)
    assert reconcile_due_matches_flush(legacy.id, occurred_at=at).is_ok()
    assert repo.episodes == before
    assert repo.names() == ['lock_tournament_for_update', 'get_tournament']

    source = Path(SERVICE.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    imported = {
        alias.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom | ast.Import)
        for alias in n.names
    } | {
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not used & {
        'commit',
        'commit_session',
        'rollback',
        'rollback_session',
        'send',
        'session',
    }
    assert not any(
        ('signals' in name or 'events' in name or name.startswith('flask'))
        for name in imported
    )


def test_unknown_clock_history_is_never_initialized(repo):
    legacy = repo.add_tournament(
        make_tournament(running_since=None, activated_at=None)
    )
    match_id = repo.add_match(legacy)
    repo.add_open_episode(match_id)
    before = list(repo.episodes)

    result = reconcile_due_matches_flush(legacy.id, occurred_at=T0 + MINUTE)

    assert result.is_ok()
    assert repo.episodes == before
    assert legacy.operational_clock_activated_at is None
    assert 'open_due_episode_flush' not in repo.names()
    assert 'close_due_episodes_flush' not in repo.names()


def test_reconcile_refuses_an_unknown_tournament(repo):
    result = reconcile_due_matches_flush(
        TournamentID(generate_uuid()), occurred_at=T0
    )

    assert result.unwrap_err() == 'tournament_not_found'
    assert repo.episodes == []
    assert 'open_due_episode_flush' not in repo.names()


def test_reconcile_locks_tournament_before_matches(repo):
    tournament = repo.add_tournament(make_tournament())
    repo.add_match(tournament)

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + MINUTE)

    names = repo.names()
    assert names[0] == 'lock_tournament_for_update'
    assert names.index('lock_tournament_for_update') < names.index(
        'lock_matches_for_update'
    )
    assert names.index('lock_matches_for_update') < names.index(
        'open_due_episode_flush'
    )
    assert repo.calls[names.index('get_tournament')][1] is True


# -------------------------------------------------------------------- #
# episode lifecycle
# -------------------------------------------------------------------- #


def test_reconcile_opens_episodes_at_the_current_clock(repo):
    tournament = repo.add_tournament(
        make_tournament(elapsed_us=10 * MINUTE_US, running_since=T0)
    )
    first = repo.add_match(tournament)
    second = repo.add_match(tournament)
    repo.add_match(tournament, confirmed=True)
    repo.add_match(tournament, contestants=1)

    result = reconcile_due_matches_flush(
        tournament.id, occurred_at=T0 + 5 * MINUTE
    )

    assert result.is_ok()
    opened = repo.open_episodes()
    assert [e.match_id for e in opened] == [first, second]
    for episode in opened:
        assert episode.tournament_id == tournament.id
        assert episode.opened_at == T0 + 5 * MINUTE
        assert episode.opened_clock_us == 15 * MINUTE_US
        assert episode.ack_revision == 0
        assert episode.pairing_key == pairing_key(
            repo.contestants[episode.match_id]
        )
    assert len({e.id for e in opened}) == 2


def test_reconcile_stores_naive_utc_for_an_aware_operation_time(repo):
    tournament = repo.add_tournament(make_tournament(running_since=T0))
    match_id = repo.add_match(tournament)
    at = (T0 + 5 * MINUTE).replace(tzinfo=UTC).astimezone(PLUS_TWO)
    assert at.tzinfo is not None

    reconcile_due_matches_flush(tournament.id, occurred_at=at)

    (episode,) = repo.episodes_of(match_id)
    assert episode.opened_at == T0 + 5 * MINUTE
    assert episode.opened_at.tzinfo is None
    assert episode.opened_clock_us == 5 * MINUTE_US


def test_noop_reconcile_keeps_episode_and_ack(repo):
    tournament = repo.add_tournament(make_tournament())
    match_id = repo.add_match(tournament)
    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + MINUTE)
    (episode,) = repo.episodes_of(match_id)
    # Two acknowledgements were recorded against this episode.
    repo.episodes = [replace(episode, ack_revision=2)]
    acknowledged = repo.episodes[0]
    repo.calls.clear()

    for minutes in (20, 21, 90):
        result = reconcile_due_matches_flush(
            tournament.id, occurred_at=T0 + minutes * MINUTE
        )
        assert result.is_ok()

    assert repo.episodes == [acknowledged]
    assert acknowledged.ack_revision == 2
    assert acknowledged.closed_at is None
    assert 'open_due_episode_flush' not in repo.names()
    assert 'close_due_episodes_flush' not in repo.names()


def test_reconcile_closes_an_episode_that_is_no_longer_due(repo):
    tournament = repo.add_tournament(
        make_tournament(elapsed_us=10 * MINUTE_US, running_since=T0)
    )
    match_id = repo.add_match(tournament)
    still_due = repo.add_match(tournament)
    repo.add_open_episode(match_id, opened_clock_us=10 * MINUTE_US)
    repo.add_open_episode(still_due, opened_clock_us=10 * MINUTE_US)
    repo.matches[match_id] = replace(
        repo.matches[match_id], confirmed_by=UserID(generate_uuid())
    )

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + 7 * MINUTE)

    (closed,) = repo.episodes_of(match_id)
    assert closed.closed_at == T0 + 7 * MINUTE
    assert closed.closed_clock_us == 17 * MINUTE_US
    (kept,) = repo.episodes_of(still_due)
    assert kept.closed_at is None
    assert [e.match_id for e in repo.open_episodes()] == [still_due]


def test_reconcile_replaces_an_episode_when_the_pairing_changes(repo):
    tournament = repo.add_tournament(make_tournament())
    match_id = repo.add_match(tournament)
    old = repo.add_open_episode(match_id)
    repo.contestants[match_id] = [
        repo.contestants[match_id][0],
        make_contestant(match_id),
    ]

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + 5 * MINUTE)

    closed, opened = repo.episodes_of(match_id)
    assert closed.id == old.id
    assert closed.closed_clock_us == 5 * MINUTE_US
    assert opened.id != old.id
    assert opened.closed_at is None
    assert opened.pairing_key == pairing_key(repo.contestants[match_id])
    assert opened.pairing_key != old.pairing_key
    assert opened.opened_clock_us == 5 * MINUTE_US
    # The close comes first: the database allows one open episode per match.
    assert repo.names().index('close_due_episodes_flush') < repo.names().index(
        'open_due_episode_flush'
    )


@pytest.mark.parametrize('invalidate', [True, False])
def test_destructive_restore_creates_new_episode(repo, invalidate):
    tournament = repo.add_tournament(make_tournament())
    match_id = repo.add_match(tournament)
    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + MINUTE)
    (first,) = repo.episodes_of(match_id)
    repo.episodes = [replace(first, ack_revision=1)]
    first = repo.episodes[0]
    final_pairing = list(repo.contestants[match_id])

    # A correction replaces the matchup and restores the same one.
    if invalidate:
        result = invalidate_due_matches_flush(
            [match_id], occurred_at=T0 + 30 * MINUTE
        )
        assert result.is_ok()
    repo.contestants[match_id] = [
        make_contestant(match_id),
        make_contestant(match_id),
    ]
    repo.contestants[match_id] = final_pairing
    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + 31 * MINUTE)

    episodes = repo.episodes_of(match_id)
    if invalidate:
        old, new = episodes
        assert old.id == first.id
        assert old.closed_at == T0 + 30 * MINUTE
        assert old.closed_clock_us == 30 * MINUTE_US
        assert old.ack_revision == 1
        assert new.id != first.id
        assert new.pairing_key == first.pairing_key
        assert new.ack_revision == 0
        assert new.opened_at == T0 + 31 * MINUTE
        assert new.opened_clock_us == 31 * MINUTE_US
        assert new.closed_at is None
    else:
        # Before and after equality alone cannot see the replacement.
        assert episodes == [first]


def test_reconcile_keeps_demand_open_while_paused(repo):
    tournament = repo.add_tournament(
        make_tournament(
            status=TournamentStatus.PAUSED,
            elapsed_us=20 * MINUTE_US,
            running_since=None,
        )
    )
    old = repo.add_match(tournament)
    repo.add_open_episode(old, opened_clock_us=5 * MINUTE_US)
    fresh = repo.add_match(tournament)

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + 600 * MINUTE)

    assert repo.episodes_of(old)[0].closed_at is None
    (opened,) = repo.episodes_of(fresh)
    assert opened.opened_clock_us == 20 * MINUTE_US
    assert [e for e in repo.episodes if e.closed_at is not None] == []


@pytest.mark.parametrize(
    'status', [TournamentStatus.COMPLETED, TournamentStatus.CANCELLED]
)
def test_reconcile_closes_every_episode_of_a_terminal_tournament(repo, status):
    tournament = repo.add_tournament(
        make_tournament(
            status=status, elapsed_us=40 * MINUTE_US, running_since=None
        )
    )
    matches = [repo.add_match(tournament) for _ in range(2)]
    for match_id in matches:
        repo.add_open_episode(match_id)

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + 600 * MINUTE)

    assert repo.open_episodes() == []
    assert {e.closed_clock_us for e in repo.episodes} == {40 * MINUTE_US}


def test_reconcile_closes_the_episode_of_a_deleted_match(repo):
    tournament = repo.add_tournament(make_tournament())
    gone = TournamentMatchID(generate_uuid())
    repo.episodes.append(
        MatchDueEpisode(
            id=MatchDueEpisodeID(generate_uuid()),
            tournament_id=tournament.id,
            match_id=gone,
            pairing_key='participant:a|participant:b',
            opened_at=T0,
            opened_clock_us=0,
        )
    )

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + 3 * MINUTE)

    assert repo.open_episodes() == []
    assert repo.episodes[0].closed_clock_us == 3 * MINUTE_US


def test_ffa_lobby_is_due_only_after_its_occupancy_is_recorded(repo):
    tournament = repo.add_tournament(make_tournament(game_format=FREE_FOR_ALL))
    waiting = repo.add_match(tournament, contestants=4, occupied=False)
    seated = repo.add_match(tournament, contestants=4, occupied=True)

    reconcile_due_matches_flush(tournament.id, occurred_at=T0 + MINUTE)

    assert repo.episodes_of(waiting) == []
    assert len(repo.episodes_of(seated)) == 1


# -------------------------------------------------------------------- #
# invalidation
# -------------------------------------------------------------------- #


def test_invalidate_closes_open_episodes_at_the_current_clock(repo):
    tournament = repo.add_tournament(
        make_tournament(elapsed_us=10 * MINUTE_US, running_since=T0)
    )
    corrected = repo.add_match(tournament)
    untouched = repo.add_match(tournament)
    repo.add_open_episode(corrected)
    repo.add_open_episode(untouched)

    result = invalidate_due_matches_flush(
        [corrected], occurred_at=T0 + 4 * MINUTE
    )

    assert result.is_ok()
    (closed,) = repo.episodes_of(corrected)
    assert closed.closed_at == T0 + 4 * MINUTE
    assert closed.closed_clock_us == 14 * MINUTE_US
    assert repo.episodes_of(untouched)[0].closed_at is None


def test_invalidate_stores_naive_utc_for_an_aware_operation_time(repo):
    tournament = repo.add_tournament(make_tournament(running_since=T0))
    match_id = repo.add_match(tournament)
    repo.add_open_episode(match_id)
    aware = (T0 + 4 * MINUTE).replace(tzinfo=UTC).astimezone(PLUS_TWO)

    invalidate_due_matches_flush([match_id], occurred_at=aware)

    (closed,) = repo.episodes_of(match_id)
    assert closed.closed_at == T0 + 4 * MINUTE
    assert closed.closed_at.tzinfo is None
    assert closed.closed_clock_us == 4 * MINUTE_US


def test_invalidate_without_known_clock_or_open_episode_changes_nothing(repo):
    legacy = repo.add_tournament(
        make_tournament(running_since=None, activated_at=None)
    )
    legacy_match = repo.add_match(legacy)
    started = repo.add_tournament(make_tournament())
    no_episode = repo.add_match(started)

    result = invalidate_due_matches_flush(
        [legacy_match, no_episode], occurred_at=T0 + MINUTE
    )

    assert result.is_ok()
    assert repo.episodes == []
    closes = [c for n, c in repo.calls if n == 'close_due_episodes_flush']
    assert [ids for ids, _, _ in closes] == [[no_episode]]


def test_invalidate_refuses_an_unknown_match_and_writes_nothing(repo):
    tournament = repo.add_tournament(make_tournament())
    known = repo.add_match(tournament)
    repo.add_open_episode(known)

    result = invalidate_due_matches_flush(
        [known, TournamentMatchID(generate_uuid())], occurred_at=T0 + MINUTE
    )

    assert result.unwrap_err() == 'match_not_found'
    assert repo.open_episodes() != []
    assert 'close_due_episodes_flush' not in repo.names()
    assert 'lock_tournament_for_update' not in repo.names()


def test_invalidate_without_matches_does_nothing(repo):
    assert invalidate_due_matches_flush([], occurred_at=T0).is_ok()
    assert repo.calls == []


def test_match_ids_may_arrive_as_strings(repo):
    tournament = repo.add_tournament(make_tournament())
    match_id = repo.add_match(tournament)
    repo.add_open_episode(match_id)

    result = invalidate_due_matches_flush(
        [str(match_id)],  # type: ignore[list-item]
        occurred_at=T0 + MINUTE,
    )

    assert result.is_ok()
    assert repo.open_episodes() == []


def test_invalidate_locks_every_tournament_before_any_match(repo):
    first = repo.add_tournament(make_tournament())
    second = repo.add_tournament(make_tournament())
    ids = [repo.add_match(first), repo.add_match(second)]

    invalidate_due_matches_flush(ids, occurred_at=T0 + MINUTE)

    names = repo.names()
    tournament_locks = [
        c for n, c in repo.calls if n == 'lock_tournament_for_update'
    ]
    assert tournament_locks == sorted([first.id, second.id], key=str), (
        'tournaments are locked in ID order'
    )
    last_tournament_lock = max(
        i for i, n in enumerate(names) if n == 'lock_tournament_for_update'
    )
    assert last_tournament_lock < names.index('lock_matches_for_update')


# -------------------------------------------------------------------- #
# completed-lobby marker
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    ('count', 'group_size_min', 'allow_undersized', 'expected'),
    [
        # A full lobby is a complete roster.
        (4, 4, False, None),
        (5, 4, False, None),
        (2, None, False, None),
        # Two rows are no shortcut: they fill a lobby only if two seats were
        # planned, or if the generator planned the shortfall.
        (2, 4, False, LOBBY_ROSTER_INCOMPLETE_ERROR),
        (3, 4, False, LOBBY_ROSTER_INCOMPLETE_ERROR),
        (2, 4, True, None),
        (3, 4, True, None),
        # A lone contestant and an empty lobby never make a lobby.
        (1, None, False, LOBBY_ROSTER_INCOMPLETE_ERROR),
        (1, 4, True, LOBBY_ROSTER_INCOMPLETE_ERROR),
        (0, None, True, LOBBY_ROSTER_INCOMPLETE_ERROR),
    ],
)
# fmt: on
def test_lobby_marker_requires_complete_roster(
    repo, count, group_size_min, allow_undersized, expected
):
    tournament = repo.add_tournament(
        make_tournament(
            game_format=FREE_FOR_ALL, group_size_min=group_size_min
        )
    )
    lobby = repo.add_match(tournament, contestants=count)

    result = mark_completed_lobbies_occupied_flush(
        [lobby], occurred_at=T0 + MINUTE, allow_undersized=allow_undersized
    )

    if expected is None:
        assert result.is_ok()
    else:
        assert result.unwrap_err() == expected


def test_lobby_marker_refuses_the_whole_call_for_one_incomplete_lobby(repo):
    tournament = repo.add_tournament(
        make_tournament(game_format=FREE_FOR_ALL, group_size_min=4)
    )
    full = repo.add_match(tournament, contestants=4)
    short = repo.add_match(tournament, contestants=2)

    result = mark_completed_lobbies_occupied_flush(
        [full, short], occurred_at=T0
    )

    assert result.unwrap_err() == LOBBY_ROSTER_INCOMPLETE_ERROR


# fmt: off
@pytest.mark.parametrize(
    ('options', 'confirmed', 'expected'),
    [
        ({'game_format': ONE_V_ONE}, False, LOBBY_NOT_FREE_FOR_ALL_ERROR),
        ({'game_format': GameFormat.HIGHSCORE}, False, LOBBY_NOT_FREE_FOR_ALL_ERROR),
        ({'game_format': FREE_FOR_ALL}, True, 'match_confirmed'),
    ],
)
# fmt: on
def test_lobby_marker_refuses_other_formats_and_confirmed_lobbies(
    repo, options, confirmed, expected
):
    tournament = repo.add_tournament(make_tournament(**options))
    lobby = repo.add_match(tournament, contestants=4, confirmed=confirmed)

    result = mark_completed_lobbies_occupied_flush([lobby], occurred_at=T0)

    assert result.unwrap_err() == expected


def test_lobby_marker_follows_the_format_of_the_lobbys_phase(repo):
    tournament = repo.add_tournament(
        make_tournament(
            game_format=ONE_V_ONE,
            playoff_game_format=FREE_FOR_ALL,
            playoff_elimination_mode=SINGLE,
        )
    )
    group_match = repo.add_match(tournament, contestants=4, phase=1)
    playoff_lobby = repo.add_match(tournament, contestants=4, phase=2)

    assert (
        mark_completed_lobbies_occupied_flush([group_match], occurred_at=T0)
        .unwrap_err()
        == LOBBY_NOT_FREE_FOR_ALL_ERROR
    )
    assert mark_completed_lobbies_occupied_flush(
        [playoff_lobby], occurred_at=T0
    ).is_ok()


def test_lobby_marker_refuses_an_unknown_match(repo):
    result = mark_completed_lobbies_occupied_flush(
        [TournamentMatchID(generate_uuid())], occurred_at=T0
    )

    assert result.unwrap_err() == 'match_not_found'
    assert 'lock_tournament_for_update' not in repo.names()


def test_lobby_marker_without_lobbies_does_nothing(repo):
    assert mark_completed_lobbies_occupied_flush([], occurred_at=T0).is_ok()
    assert repo.calls == []


def test_lobby_marker_locks_tournament_before_matches_and_reads_fresh(repo):
    tournament = repo.add_tournament(make_tournament(game_format=FREE_FOR_ALL))
    lobby = repo.add_match(tournament, contestants=3)

    mark_completed_lobbies_occupied_flush([str(lobby)], occurred_at=T0)  # type: ignore[list-item]

    names = repo.names()
    assert names.index('lock_tournament_for_update') < names.index(
        'lock_matches_for_update'
    )
    assert names.index('lock_matches_for_update') < names.index(
        'get_matches_for_tournament_ordered_fresh'
    )
    assert repo.calls[names.index('get_tournament')][1] is True
