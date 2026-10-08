"""
tests.integration.services.lan_tournament.test_result_timing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as matches,
    tournament_operational_service,
    tournament_repository as repo,
    tournament_seeding_service as seeding,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.dashboard import (
    DbMatchDueEpisode,
    DbMatchEscalationAck,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models.bracket import Bracket
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
    OperationalClock,
)
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_operational_domain_service import (  # noqa: E501
    clock_value_us,
)
from byceps.services.party.models import PartyID
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('f03-result-timing')

START = datetime(2031, 3, 4, 18, 0, 0)
SCHEDULED = datetime(2031, 3, 4, 15, 0, 0)
MINUTE_US = 60 * 1_000_000

ONGOING = TournamentStatus.ONGOING
PAUSED = TournamentStatus.PAUSED
COMPLETED = TournamentStatus.COMPLETED
CLOSED = TournamentStatus.REGISTRATION_CLOSED

SE = EliminationMode.SINGLE_ELIMINATION
DE = EliminationMode.DOUBLE_ELIMINATION
RR = EliminationMode.ROUND_ROBIN

WINNERS = Bracket.WINNERS
LOSERS = Bracket.LOSERS
GRAND_FINAL = Bracket.GRAND_FINAL
THIRD_PLACE = Bracket.THIRD_PLACE


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
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'F03 result timing')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03ResultTiming{i}') for i in range(9)]


@pytest.fixture(scope='module')
def admin(users):
    return users[-1]


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock(START)
    monkeypatch.setattr(repo, 'get_operation_time', clock)
    return clock


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(*, players=4, mode=SE, **fields):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Result timing {generate_uuid7()}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=mode,
            tournament_status=CLOSED,
            start_time=SCHEDULED,
            max_players=16,
            **fields,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:players]:
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
        board = seeding.get_board(
            tournament.id, initiator_id=users[-1].id
        ).unwrap()
        seeding.generate_from_seeding(
            tournament.id,
            expected_version=board.version,
            initiator_id=users[-1].id,
        ).unwrap()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if repo.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.fixture
def started(make_tournament, admin, clock):
    """Return a factory for tournaments that run on a known clock."""

    def _start(**kwargs):
        tournament = make_tournament(**kwargs)
        # Past every time the generation sampled.
        clock.now = START + timedelta(hours=1)
        result = tournament_service.change_status(
            tournament.id, ONGOING, admin.id
        )
        assert result.is_ok(), result.unwrap_err()
        clock.now = START + timedelta(hours=2)
        return tournament

    return _start


# -- reads: only committed data, through a separate connection --


def _committed_rows(sql: str, **params):
    with db.engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _stamps(tournament) -> dict[UUID, datetime]:
    return {
        row[0]: row[1]
        for row in _committed_rows(
            'SELECT id, last_changed_at FROM lan_tournament_matches'
            ' WHERE tournament_id = :id',
            id=tournament.id,
        )
    }


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


def _of_match(tournament, match) -> list[DbMatchDueEpisode]:
    return [e for e in _episodes(tournament) if e.match_id == match.id]


def _tournament(tournament):
    db.session.rollback()
    return repo.get_tournament(tournament.id, fresh=True)


def _clock_of(tournament) -> OperationalClock:
    found = _tournament(tournament)
    return OperationalClock(
        elapsed_us=found.operational_clock_elapsed_us,
        running_since=found.operational_clock_running_since,
        activated_at=found.operational_clock_activated_at,
    )


def _match(tournament, *, bracket=None, round=0, order=0):
    db.session.rollback()
    found = [
        m
        for m in repo.get_matches_for_tournament(tournament.id)
        if m.bracket == bracket and m.round == round and m.match_order == order
    ]
    assert len(found) == 1, (bracket, round, order, found)
    return found[0]


def _fresh(match):
    db.session.rollback()
    return repo.get_match(match.id)


def _members(match) -> list[str]:
    db.session.rollback()
    return sorted(
        str(c.participant_id or c.team_id)
        for c in repo.get_contestants_for_match(match.id)
    )


def _scores(match, winner: str | None = None, margin: int = 2):
    members = _members(match)
    winner = winner or members[0]
    loser = next(m for m in members if m != winner)
    return {UUID(winner): margin, UUID(loser): 0}


def _play(match, admin, winner: str | None = None):
    result = matches.admin_set_and_confirm_match(
        match.id, admin.id, _scores(match, winner)
    )
    assert result.is_ok(), result.unwrap_err()


def _acknowledge(episode: DbMatchDueEpisode, actor_id) -> None:
    """Record one acknowledgement, as the acknowledgement service will."""
    repo.create_escalation_ack_flush(
        MatchEscalationAcknowledgement(
            id=MatchEscalationAcknowledgementID(generate_uuid7()),
            episode_id=episode.id,
            tournament_id=episode.tournament_id,
            match_id=episode.match_id,
            revision=1,
            occurred_at=START + timedelta(hours=2, minutes=20),
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


def _change(tournament, status, admin, **kwargs):
    result = tournament_service.change_status(
        tournament.id, status, admin.id, **kwargs
    )
    assert result.is_ok(), result.unwrap_err()


def _clock_at(tournament, at: datetime) -> int:
    return clock_value_us(_clock_of(tournament), at)


# -- one operation time for the source and every destination --


def _first_result(mode, tournament):
    """Return the matches one first-round result changes, and their wait."""
    first = _match(tournament, bracket=WINNERS if mode is DE else None)
    if mode is DE:
        return (
            first,
            {
                'source': first,
                'winner': _match(tournament, bracket=WINNERS, round=1),
                'loser': _match(tournament, bracket=LOSERS, round=1),
            },
            _match(tournament, bracket=WINNERS, order=1),
        )
    return (
        first,
        {
            'source': first,
            'winner': _match(tournament, round=1),
            'loser': _match(tournament, bracket=THIRD_PLACE, round=1),
        },
        _match(tournament, order=1),
    )


@pytest.mark.parametrize('mode', [SE, DE])
def test_ko_cascade_stamps_all_actual_changes(started, admin, clock, mode):
    tournament = started(mode=mode)
    first, changed, other = _first_result(mode, tournament)
    before = _stamps(tournament)
    open_before = _open(tournament)
    assert set(open_before) == {first.id, other.id}
    at = clock.now
    clock.readings = 0

    _play(first, admin)

    # One server reading served the result, the advances and the episodes.
    assert clock.readings == 1
    after = _stamps(tournament)
    for name, match in changed.items():
        assert after[match.id] == at, name
    # What the result did not change keeps its stamp.
    for match in repo.get_matches_for_tournament(tournament.id):
        if match.id not in {m.id for m in changed.values()}:
            assert after[match.id] == before[match.id], match

    # The played match leaves demand, its sibling keeps its episode, and a
    # destination with one contestant is no demand yet.
    now_open = _open(tournament)
    assert set(now_open) == {other.id}
    assert now_open[other.id].id == open_before[other.id].id
    (played,) = _of_match(tournament, first)
    assert played.closed_at == at
    assert played.closed_clock_us == _clock_at(tournament, at)
    assert _of_match(tournament, changed['winner']) == []
    assert _of_match(tournament, changed['loser']) == []

    # The second result completes both destinations.
    at2 = clock.now
    clock.readings = 0
    _play(other, admin)

    assert clock.readings == 1
    after2 = _stamps(tournament)
    assert {
        match.id
        for match in repo.get_matches_for_tournament(tournament.id)
        if after2[match.id] == at2
    } == {other.id, changed['winner'].id, changed['loser'].id}
    assert after2[first.id] == at
    opened = _open(tournament)
    assert set(opened) == {changed['winner'].id, changed['loser'].id}
    for episode in opened.values():
        assert episode.opened_at == at2
        assert episode.opened_clock_us == _clock_at(tournament, at2)
        assert episode.ack_revision == 0


def test_the_deciding_result_stamps_and_freezes_the_clock_at_one_time(
    started, admin, clock
):
    tournament = started(mode=SE, players=2)
    final = _match(tournament)
    start = _clock_of(tournament)
    at = clock.now
    clock.readings = 0

    _play(final, admin)

    assert clock.readings == 1
    found = _tournament(tournament)
    assert found.tournament_status is COMPLETED
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    assert frozen.elapsed_us == clock_value_us(start, at)
    assert _stamps(tournament)[final.id] == at
    (episode,) = _of_match(tournament, final)
    assert episode.closed_at == at
    assert episode.closed_clock_us == frozen.elapsed_us
    assert _open(tournament) == {}


def test_ko_retraction_stamps_every_match_it_changes_at_one_time(
    started, admin, clock
):
    tournament = started(mode=SE)
    first = _match(tournament)
    other = _match(tournament, order=1)
    final = _match(tournament, round=1)
    third = _match(tournament, bracket=THIRD_PLACE, round=1)
    _play(first, admin)
    _play(other, admin)
    assert set(_open(tournament)) == {final.id, third.id}
    before = _stamps(tournament)
    at = clock.now
    clock.readings = 0

    result = matches.unconfirm_match(first.id, admin.id, reason='Wrong')
    assert result.is_ok(), result.unwrap_err()

    assert clock.readings == 1
    after = _stamps(tournament)
    changed = {first.id, final.id, third.id}
    for match_id, stamp in after.items():
        if match_id in changed:
            assert stamp == at, match_id
        else:
            assert stamp == before[match_id], match_id
    # The destinations lost a contestant and so their demand; the retracted
    # match is due again, as a new episode.
    opened = _open(tournament)
    assert set(opened) == {first.id}
    assert opened[first.id].opened_at == at
    assert opened[first.id].opened_clock_us == _clock_at(tournament, at)
    assert len(_of_match(tournament, first)) == 2
    for match in (final, third):
        (closed,) = _of_match(tournament, match)
        assert closed.closed_at == at


def test_structural_walkover_advance_is_stamped_and_creates_no_demand(
    started, admin, clock
):
    tournament = started(mode=DE, players=5)
    # The loser of this match drops into a losers match without a second
    # feeder, which advances them and is confirmed as a structural walkover.
    played = _match(tournament, bracket=WINNERS, round=0, order=1)
    winners_next = _match(tournament, bracket=WINNERS, round=1, order=0)
    walkover = _match(tournament, bracket=LOSERS, round=1, order=1)
    beyond = _match(tournament, bracket=LOSERS, round=2, order=1)
    waiting = _match(tournament, bracket=WINNERS, round=1, order=1)
    before = _stamps(tournament)
    kept = _open(tournament)[waiting.id].id
    at = clock.now
    clock.readings = 0

    _play(played, admin)

    # One reading for the result and for the walkover it triggered.
    assert clock.readings == 1
    after = _stamps(tournament)
    changed = {played.id, winners_next.id, walkover.id, beyond.id}
    assert {k for k, v in after.items() if v == at} == changed
    assert all(after[k] == before[k] for k in after if k not in changed)
    assert _fresh(walkover).confirmed_by is not None
    # The played match and the half-filled or walked-over ones are no
    # demand; the match that the winner completed is, and the unrelated
    # waiting match keeps its episode.
    opened = _open(tournament)
    assert set(opened) == {waiting.id, winners_next.id}
    assert opened[waiting.id].id == kept
    assert opened[winners_next.id].opened_at == at
    assert _of_match(tournament, walkover) == []
    assert _of_match(tournament, beyond) == []


def _changed_at(tournament, at) -> set:
    return {k for k, v in _stamps(tournament).items() if v == at}


def test_a_retraction_over_confirmed_downstream_matches_shares_one_time(
    started, admin, clock
):
    tournament = started(mode=SE)
    first = _match(tournament)
    other = _match(tournament, order=1)
    final = _match(tournament, round=1)
    third = _match(tournament, bracket=THIRD_PLACE, round=1)
    for match in (first, other, final):
        _play(match, admin)
    assert _tournament(tournament).tournament_status is COMPLETED
    done = _clock_of(tournament)
    at = clock.now
    clock.readings = 0

    # The retraction of the first match takes the confirmed final with it,
    # and with that the completion of the tournament.
    result = matches.unconfirm_match(first.id, admin.id, reason='Wrong')
    assert result.is_ok(), result.unwrap_err()

    assert clock.readings == 1
    assert _changed_at(tournament, at) == {first.id, final.id, third.id}
    assert _fresh(final).confirmed_by is None
    assert _tournament(tournament).tournament_status is ONGOING
    resumed = _clock_of(tournament)
    assert resumed.running_since == at
    assert resumed.elapsed_us == done.elapsed_us
    opened = _open(tournament)
    assert set(opened) == {first.id}
    assert opened[first.id].opened_clock_us == done.elapsed_us


def test_a_retraction_over_the_loser_route_shares_one_time(
    started, admin, clock
):
    tournament = started(mode=DE)
    first = _match(tournament, bracket=WINNERS, round=0, order=0)
    other = _match(tournament, bracket=WINNERS, round=0, order=1)
    winners_final = _match(tournament, bracket=WINNERS, round=1)
    losers_first = _match(tournament, bracket=LOSERS, round=1)
    losers_second = _match(tournament, bracket=LOSERS, round=2)
    for match in (first, other, losers_first):
        _play(match, admin)
    assert _fresh(losers_first).confirmed_by is not None
    at = clock.now
    clock.readings = 0

    # The dropped loser had advanced through a confirmed losers match.
    result = matches.unconfirm_match(first.id, admin.id, reason='Wrong')
    assert result.is_ok(), result.unwrap_err()

    assert clock.readings == 1
    assert _changed_at(tournament, at) == {
        first.id,
        winners_final.id,
        losers_first.id,
        losers_second.id,
    }
    assert _fresh(losers_first).confirmed_by is None


def test_a_structural_walkover_is_retracted_with_its_result_at_one_time(
    started, admin, clock
):
    tournament = started(mode=DE, players=5)
    played = _match(tournament, bracket=WINNERS, round=0, order=1)
    winners_next = _match(tournament, bracket=WINNERS, round=1, order=0)
    walkover = _match(tournament, bracket=LOSERS, round=1, order=1)
    beyond = _match(tournament, bracket=LOSERS, round=2, order=1)
    _play(played, admin)
    assert _fresh(walkover).confirmed_by is not None
    at = clock.now
    clock.readings = 0

    result = matches.unconfirm_match(played.id, admin.id, reason='Wrong')
    assert result.is_ok(), result.unwrap_err()

    assert clock.readings == 1
    assert _changed_at(tournament, at) == {
        played.id,
        winners_next.id,
        walkover.id,
        beyond.id,
    }
    assert _fresh(walkover).confirmed_by is None
    assert _members(beyond) == []


@pytest.mark.parametrize('drawn', [False, True])
def test_a_round_robin_result_and_its_completion_share_one_operation_time(
    started, admin, clock, drawn
):
    tournament = started(mode=RR, players=3)
    everyone = sorted(
        {
            m
            for x in repo.get_matches_for_tournament(tournament.id)
            for m in _members(x)
        }
    )
    leader = everyone[0]
    played, last = [], None
    for match in repo.get_matches_for_tournament(tournament.id):
        if leader in _members(match):
            played.append(match)
        else:
            last = match
    for match in played:
        _play(match, admin, winner=leader)
    assert _tournament(tournament).tournament_status is ONGOING
    start = _clock_of(tournament)
    at = clock.now
    clock.readings = 0
    scores = _scores(last)
    if drawn:
        scores = {key: 1 for key in scores}

    result = matches.admin_set_and_confirm_match(last.id, admin.id, scores)
    assert result.is_ok(), result.unwrap_err()

    # The result, a draw's confirmation and the completion of the plain
    # round robin took one reading.
    assert clock.readings == 1
    assert _fresh(last).confirmed_by is not None
    assert _stamps(tournament)[last.id] == at
    assert _tournament(tournament).tournament_status is COMPLETED
    done = _clock_of(tournament)
    assert done.running_since is None
    assert done.elapsed_us == clock_value_us(start, at)
    assert _open(tournament) == {}
    (episode,) = _of_match(tournament, last)
    assert episode.closed_at == at
    assert episode.closed_clock_us == done.elapsed_us


# -- round robin: the frontier of the group --


def test_rr_confirm_and_retract_move_group_frontier(started, admin, clock):
    tournament = started(mode=RR)
    rounds = {
        (r, o): _match(tournament, round=r, order=o)
        for r in range(3)
        for o in range(2)
    }
    first, second = rounds[0, 0], rounds[0, 1]
    assert set(_open(tournament)) == {first.id, second.id}
    kept = _open(tournament)[second.id].id

    # A round is open until its last match is played: the sibling keeps
    # its episode, and the next round is not due yet.
    at = clock.now
    _play(first, admin)

    now_open = _open(tournament)
    assert set(now_open) == {second.id}
    assert now_open[second.id].id == kept
    (done,) = _of_match(tournament, first)
    assert done.closed_at == at

    # Its last match moves the frontier: the next round becomes due.
    at2 = clock.now
    _play(second, admin)

    opened = _open(tournament)
    assert set(opened) == {rounds[1, 0].id, rounds[1, 1].id}
    for episode in opened.values():
        assert episode.opened_at == at2
        assert episode.opened_clock_us == _clock_at(tournament, at2)

    # Playing ahead inside the frontier round changes nothing for others.
    _play(rounds[1, 0], admin)
    assert set(_open(tournament)) == {rounds[1, 1].id}

    # A retraction moves the frontier back: the round the group had
    # reached is no longer due, and the retracted match is due again.
    at3 = clock.now
    result = matches.unconfirm_match(first.id, admin.id, reason='Wrong')
    assert result.is_ok(), result.unwrap_err()

    reopened = _open(tournament)
    assert set(reopened) == {first.id}
    assert reopened[first.id].opened_at == at3
    (closed,) = _of_match(tournament, rounds[1, 1])
    assert closed.closed_at == at3
    assert closed.closed_clock_us == _clock_at(tournament, at3)
    assert _of_match(tournament, rounds[2, 0]) == []
    assert len(_of_match(tournament, first)) == 2


@contextmanager
def _siblings_probe(monkeypatch, tournament, probes):
    """Probe the group's rows from a second session at the first write."""
    original = repo.confirm_match

    def probe_then_confirm(match_id, *args, **kwargs):
        for row in repo.get_matches_for_tournament(tournament.id):
            with db.engine.connect() as connection:
                try:
                    connection.execute(
                        text(
                            'SELECT id FROM lan_tournament_matches'
                            ' WHERE id = :id FOR UPDATE NOWAIT'
                        ),
                        {'id': row.id},
                    )
                    probes[row.id] = 'free'
                except OperationalError:
                    probes[row.id] = 'locked'
        return original(match_id, *args, **kwargs)

    monkeypatch.setattr(repo, 'confirm_match', probe_then_confirm)
    try:
        yield
    finally:
        monkeypatch.setattr(repo, 'confirm_match', original)


def test_rr_group_siblings_are_locked_before_the_first_write(
    started, admin, clock, monkeypatch
):
    tournament = started(mode=RR)
    first = _match(tournament, round=0, order=0)
    probes: dict = {}

    with _siblings_probe(monkeypatch, tournament, probes):
        _play(first, admin)

    group = {m.id for m in repo.get_matches_for_tournament(tournament.id)}
    assert set(probes) == group
    # Nothing routes the siblings to the played match, and every one of the
    # group was held by the operation when it began to write.
    assert set(probes.values()) == {'locked'}


# -- destructive correction and the history of episodes --


def _force_the_full_correction_path(monkeypatch):
    monkeypatch.setattr(
        matches, '_plan_in_place_correction', lambda *args, **kwargs: None
    )


def test_same_pairing_restored_cannot_reuse_ack(
    started, admin, clock, monkeypatch
):
    tournament = started(mode=SE)
    first = _match(tournament)
    other = _match(tournament, order=1)
    final = _match(tournament, round=1)
    third = _match(tournament, bracket=THIRD_PLACE, round=1)
    _play(first, admin)
    winner = _members(final)[0]
    _play(other, admin)
    old = _open(tournament)
    assert set(old) == {final.id, third.id}
    pairing = {m: _members(m) for m in (final, third)}
    _acknowledge(old[final.id], admin.id)
    _force_the_full_correction_path(monkeypatch)
    at = clock.now

    # Same winner, other margin: the retraction strips the destinations and
    # the re-confirmation puts the very same pairings back.
    result = matches.correct_match_result(
        first.id,
        admin.id,
        reason='Wrong margin',
        corrected_scores=_scores(first, winner, margin=7),
    )
    assert result.is_ok(), result.unwrap_err()

    assert {m: _members(m) for m in (final, third)} == pairing
    assert _fresh(first).confirmed_by is not None
    renewed = _open(tournament)
    assert set(renewed) == {final.id, third.id}
    for match in (final, third):
        assert renewed[match.id].id != old[match.id].id
        assert renewed[match.id].opened_at == at
        assert renewed[match.id].ack_revision == 0
        retired = next(
            e for e in _of_match(tournament, match) if e.id == old[match.id].id
        )
        assert retired.closed_at == at
    # The old acknowledgement stays with the old episode.
    acks = db.session.scalars(
        select(DbMatchEscalationAck).where(
            DbMatchEscalationAck.tournament_id == tournament.id
        )
    ).all()
    assert [a.episode_id for a in acks] == [old[final.id].id]
    assert db.session.get(DbMatchDueEpisode, old[final.id].id).ack_revision == 1
    # The corrected match is confirmed again: no zero-length episode.
    assert len(_of_match(tournament, first)) == 1


def test_an_in_place_correction_keeps_the_downstream_episode_and_ack(
    started, admin, clock
):
    tournament = started(mode=SE)
    first = _match(tournament)
    other = _match(tournament, order=1)
    final = _match(tournament, round=1)
    _play(first, admin)
    winner = _members(final)[0]
    _play(other, admin)
    old = _open(tournament)[final.id]
    _acknowledge(old, admin.id)
    before = _stamps(tournament)
    at = clock.now
    clock.readings = 0

    result = matches.correct_match_result(
        first.id,
        admin.id,
        reason='Wrong margin',
        corrected_scores=_scores(first, winner, margin=7),
    )
    assert result.is_ok(), result.unwrap_err()

    # Nothing downstream changed, so the demand and its acknowledgement stay.
    assert clock.readings == 1
    kept = _open(tournament)[final.id]
    assert (kept.id, kept.opened_at, kept.ack_revision) == (
        old.id,
        old.opened_at,
        1,
    )
    after = _stamps(tournament)
    assert after[first.id] == at
    assert {k: v for k, v in after.items() if k != first.id} == {
        k: v for k, v in before.items() if k != first.id
    }


def test_a_correction_commits_the_ready_reset_together_with_the_new_episode(
    started, admin, clock, monkeypatch
):
    tournament = started(mode=SE)
    first = _match(tournament)
    other = _match(tournament, order=1)
    final = _match(tournament, round=1)
    _play(first, admin)
    winner = _members(final)[0]
    _play(other, admin)
    repo.set_side_ready_flush(final.id, MatchSide.A, START, admin.id)
    db.session.commit()
    before = _fresh(final)
    assert before.ready_at_a is not None
    old = _open(tournament)[final.id]
    _force_the_full_correction_path(monkeypatch)
    at = clock.now

    result = matches.correct_match_result(
        first.id,
        admin.id,
        reason='Wrong margin',
        corrected_scores=_scores(first, winner, margin=7),
    )
    assert result.is_ok(), result.unwrap_err()

    # The claim is gone and the revision moved, in the very transaction that
    # replaced the episode; the original occupancy of the final stays.
    after = _fresh(final)
    assert after.ready_at_a is None
    assert after.readiness_revision > before.readiness_revision
    assert after.occupied_since == before.occupied_since
    renewed = _open(tournament)[final.id]
    assert renewed.id != old.id
    assert renewed.opened_at == at


def _play_up_to_the_grand_final(tournament, admin):
    grand = _match(tournament, bracket=GRAND_FINAL)
    for _ in range(20):
        playable = [
            m
            for m in repo.get_matches_for_tournament(tournament.id)
            if m.confirmed_by is None
            and m.id != grand.id
            and len(_members(m)) == 2
        ]
        if not playable:
            return grand
        _play(playable[0], admin)
    raise AssertionError('the bracket does not drain')


def _champions(grand):
    """Return the winners' and the losers' champion of the grand final."""
    final = next(
        m
        for m in repo.get_matches_for_tournament(grand.tournament_id)
        if m.bracket is LOSERS and m.next_match_id == grand.id
    )
    losers = max(
        repo.get_contestants_for_match(final.id), key=lambda c: c.score or 0
    )
    losers_champion = str(losers.participant_id)
    winners_champion = next(m for m in _members(grand) if m != losers_champion)
    return winners_champion, losers_champion


def test_gf_reset_deletion_preserves_history(started, admin, clock):
    tournament = started(mode=DE)
    grand = _play_up_to_the_grand_final(tournament, admin)
    winners, losers = _champions(grand)

    # The losers' champion wins: the bracket resets, and the reset match is
    # due at once.
    at_reset = clock.now
    clock.readings = 0
    _play(grand, admin, winner=losers)
    reset = _match(tournament, bracket=GRAND_FINAL, order=1)
    (reset_episode,) = _of_match(tournament, reset)
    assert reset_episode.closed_at is None
    # The created match and its two contestants are stamped with the one
    # time of the result, too.
    assert clock.readings == 1
    assert _stamps(tournament)[reset.id] == at_reset
    assert reset_episode.opened_at == at_reset
    repo.save_match_pin_flush(
        reset.id,
        tournament.id,
        pinned_at=START,
        pinned_by=admin.id,
        updated_at=START,
        updated_by=admin.id,
        expected_revision=0,
    )
    db.session.commit()
    at = clock.now

    # Correcting the first grand final deletes the reset match.
    result = matches.correct_match_result(
        grand.id,
        admin.id,
        reason='Wrong winner',
        corrected_scores=_scores(grand, winners, margin=9),
        ack_critical=True,
    )
    assert result.is_ok(), result.unwrap_err()

    db.session.rollback()
    assert repo.find_match(reset.id) is None
    retired = db.session.get(DbMatchDueEpisode, reset_episode.id)
    assert retired is not None
    assert retired.match_id == reset.id
    assert retired.closed_at == at
    assert retired.closed_clock_us is not None
    assert retired.closed_clock_us >= retired.opened_clock_us
    assert _committed_rows(
        'SELECT count(*) FROM lan_tournament_match_dashboard_annotations'
        ' WHERE match_id = :id',
        id=reset.id,
    ) == [(0,)]
    # The winners' champion took the first grand final: the tournament is
    # over, and no demand is left.
    assert _tournament(tournament).tournament_status is COMPLETED
    assert _open(tournament) == {}

    # Retracting the decided grand final reopens it as a new demand.
    at2 = clock.now
    matches.unconfirm_match(grand.id, admin.id, reason='Again').unwrap()
    assert set(_open(tournament)) == {grand.id}
    assert _open(tournament)[grand.id].opened_at == at2

    # A recreated reset match is a new match with its own history.
    _play(grand, admin, winner=losers)
    recreated = _match(tournament, bracket=GRAND_FINAL, order=1)
    assert recreated.id != reset.id
    (fresh_episode,) = _of_match(tournament, recreated)
    assert fresh_episode.id != reset_episode.id
    assert fresh_episode.closed_at is None
    assert db.session.get(DbMatchDueEpisode, reset_episode.id).closed_at == at


def test_unconfirming_the_first_grand_final_retires_the_reset_match(
    started, admin, clock
):
    tournament = started(mode=DE)
    grand = _play_up_to_the_grand_final(tournament, admin)
    _, losers = _champions(grand)
    _play(grand, admin, winner=losers)
    reset = _match(tournament, bracket=GRAND_FINAL, order=1)
    (reset_episode,) = _of_match(tournament, reset)
    repo.save_match_pin_flush(
        reset.id,
        tournament.id,
        pinned_at=START,
        pinned_by=admin.id,
        updated_at=START,
        updated_by=admin.id,
        expected_revision=0,
    )
    db.session.commit()
    at = clock.now

    matches.unconfirm_match(grand.id, admin.id, reason='Wrong').unwrap()

    db.session.rollback()
    assert repo.find_match(reset.id) is None
    retired = db.session.get(DbMatchDueEpisode, reset_episode.id)
    assert retired.closed_at == at
    assert _committed_rows(
        'SELECT count(*) FROM lan_tournament_match_dashboard_annotations'
        ' WHERE match_id = :id',
        id=reset.id,
    ) == [(0,)]
    assert set(_open(tournament)) == {grand.id}


# -- every owner commits the result with its timing, or nothing --


def _log_count(tournament) -> int:
    return _committed_rows(
        'SELECT count(*) FROM lan_tournament_log_entries'
        ' WHERE tournament_id = :id',
        id=tournament.id,
    )[0][0]


def _state(tournament):
    return (
        _stamps(tournament),
        [(e.id, e.closed_at, e.closed_clock_us) for e in _episodes(tournament)],
        _committed_rows(
            'SELECT m.id, m.confirmed_by, c.id, c.score'
            ' FROM lan_tournament_matches m'
            ' LEFT JOIN lan_tournament_match_contestants c'
            '   ON c.tournament_match_id = m.id'
            ' WHERE m.tournament_id = :id ORDER BY m.id, c.id',
            id=tournament.id,
        ),
        _log_count(tournament),
    )


def _fail_reconcile(monkeypatch, how):
    def failing(tournament_id, *, occurred_at):
        if how == 'raises':
            raise RuntimeError('clock failure')
        return Err('clock_failure')

    monkeypatch.setattr(
        tournament_operational_service, 'reconcile_due_matches_flush', failing
    )


def _owner_confirm(tournament, admin, first):
    return matches.admin_set_and_confirm_match(
        first.id, admin.id, _scores(first)
    )


def _owner_unconfirm(tournament, admin, first):
    return matches.unconfirm_match(first.id, admin.id, reason='Wrong')


def _owner_correct(tournament, admin, first):
    return matches.correct_match_result(
        first.id,
        admin.id,
        reason='Wrong winner',
        corrected_scores=_scores(first, _members(first)[1]),
        ack_critical=True,
    )


def _owner_in_place(tournament, admin, first):
    return matches.correct_match_result(
        first.id,
        admin.id,
        reason='Wrong margin',
        corrected_scores=_scores(first, _members(first)[0], margin=9),
    )


@pytest.mark.parametrize('how', ['err', 'raises'])
@pytest.mark.parametrize(
    ('owner', 'played_first'),
    # fmt: off
    [
        (_owner_confirm, False),
        (_owner_unconfirm, True),
        (_owner_correct, True),
        (_owner_in_place, True),
    ],
    # fmt: on
    ids=['admin_set_and_confirm', 'unconfirm', 'correct', 'in_place'],
)
def test_clock_failure_leaves_no_result_audit_or_episode_behind(
    started, admin, clock, monkeypatch, owner, played_first, how
):
    tournament = started(mode=SE)
    first = _match(tournament)
    if played_first:
        _play(first, admin)
    before = _state(tournament)
    _fail_reconcile(monkeypatch, how)

    if how == 'raises':
        with pytest.raises(RuntimeError, match='clock failure'):
            owner(tournament, admin, first)
    else:
        result = owner(tournament, admin, first)
        assert result.is_err()
        assert result.unwrap_err() == 'clock_failure'

    # The result, its audit entry, the stamps and the episodes: nothing.
    assert _state(tournament) == before
    assert _fresh(first).confirmed_by == (admin.id if played_first else None)


def test_a_tournament_without_clock_history_gets_no_episodes_and_no_time(
    make_tournament, admin, clock, monkeypatch
):
    tournament = make_tournament(mode=SE)
    # A tournament that ran before the feature: started, but no history.
    db.session.execute(
        update(DbTournament)
        .where(DbTournament.id == tournament.id)
        .values(tournament_status=ONGOING.name)
    )
    db.session.commit()
    first = _match(tournament)
    seen = []
    monkeypatch.setattr(
        tournament_operational_service,
        'reconcile_due_matches_flush',
        lambda *args, **kwargs: seen.append(kwargs) or Err('never'),
    )
    clock.readings = 0

    _play(first, admin)

    assert seen == []
    assert _episodes(tournament) == []
    assert _clock_of(tournament) == OperationalClock()
    # The writers still stamp, each with its own server reading.
    assert _stamps(tournament)[first.id] is not None
    assert clock.readings >= 1


# -- the public owners share one operation time --


def _user_of(tournament, participant: str):
    return next(
        p.user_id
        for p in repo.get_participants_for_tournament(tournament.id)
        if str(p.id) == participant
    )


def _submit(owner, tournament, match, admin):
    scores = _scores(match)
    if owner == 'admin_set_and_confirm_match':
        return matches.admin_set_and_confirm_match(match.id, admin.id, scores)

    if owner == 'set_match_scores':
        loser = _members(match)[1]
        return matches.set_match_scores(
            match.id, _user_of(tournament, loser), scores
        )

    rows = {
        str(c.participant_id): c.id
        for c in repo.get_contestants_for_match(match.id)
    }
    repo.update_contestant_scores(
        {rows[str(key)]: value for key, value in scores.items()}
    )
    db.session.commit()
    return matches.confirm_match(match.id, admin.id)


@pytest.mark.parametrize(
    'owner',
    ['admin_set_and_confirm_match', 'set_match_scores', 'confirm_match'],
)
def test_every_result_owner_uses_one_operation_time(
    started, admin, clock, owner
):
    tournament = started(mode=SE)
    first = _match(tournament)
    other = _match(tournament, order=1)
    final = _match(tournament, round=1)
    third = _match(tournament, bracket=THIRD_PLACE, round=1)
    before = _stamps(tournament)
    clock.readings = 0
    at = clock.now

    result = _submit(owner, tournament, first, admin)

    assert result.is_ok(), result
    if owner == 'confirm_match':
        # One reading for the scores, one for the confirmation.
        assert clock.readings == 2
        at = at + clock.step
    else:
        assert clock.readings == 1
    after = _stamps(tournament)
    assert {k for k, v in after.items() if v == at} == {
        first.id,
        final.id,
        third.id,
    }
    assert after[other.id] == before[other.id]
    assert set(_open(tournament)) == {other.id}
    (played,) = _of_match(tournament, first)
    assert played.closed_at == at


# -- paused and settled --


def test_a_result_while_paused_opens_its_episodes_at_the_frozen_clock(
    started, admin, clock
):
    tournament = started(mode=RR)
    first = _match(tournament, round=0, order=0)
    second = _match(tournament, round=0, order=1)
    clock.now = START + timedelta(hours=2, minutes=10)
    _change(tournament, PAUSED, admin)
    frozen = _clock_of(tournament)
    assert frozen.running_since is None
    # An hour later, still paused.
    clock.now = START + timedelta(hours=3, minutes=10)
    at = clock.now

    _play(first, admin)
    _play(second, admin)

    # The round was finished while paused: the next round is due, and its
    # wait starts at the frozen clock, not an hour later.
    opened = _open(tournament)
    assert len(opened) == 2
    for episode in opened.values():
        assert episode.opened_clock_us == frozen.elapsed_us
        assert episode.opened_at >= at
    assert _clock_of(tournament) == frozen


def test_the_resume_that_completes_a_settled_round_robin_closes_the_clock_once(
    make_tournament, admin, clock
):
    tournament = make_tournament(mode=RR, players=3)
    clock.now = START + timedelta(hours=1)
    _change(tournament, ONGOING, admin)
    clock.now = START + timedelta(hours=2)
    _change(tournament, PAUSED, admin)
    frozen = _clock_of(tournament).elapsed_us
    clock.now = START + timedelta(hours=3)
    for match in sorted(
        repo.get_matches_for_tournament(tournament.id), key=lambda m: m.round
    ):
        _play(match, admin)
    assert _open(tournament) == {}
    clock.now = START + timedelta(hours=4)

    _change(tournament, ONGOING, admin)

    found = _tournament(tournament)
    assert found.tournament_status is COMPLETED
    done = _clock_of(tournament)
    assert done.running_since is None
    # The resume read the server once, and the completion once more: the
    # clock ran for exactly that second, and the completion took the one
    # operation time that it sampled itself.
    assert done.elapsed_us == frozen + 1_000_000
    assert _open(tournament) == {}
    assert all(e.closed_at is not None for e in _episodes(tournament))
