from datetime import datetime, timedelta, timezone, UTC

import pytest
from sqlalchemy import select, text, update

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service as engine,
    tournament_repository as repo,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import uuid7


NOW = datetime(2026, 10, 7, 12, 0)
MINUTE = timedelta(minutes=1)
PLUS_TWO = timezone(timedelta(hours=2))

ONE_V_ONE = GameFormat.ONE_V_ONE
FREE_FOR_ALL = GameFormat.FREE_FOR_ALL
SINGLE_ELIMINATION = EliminationMode.SINGLE_ELIMINATION

# Tournament kinds: (game format, extra fields).
ONE_V_ONE_KIND = {'game_format': ONE_V_ONE}
FFA_KIND = {'game_format': FREE_FOR_ALL}
GROUPS_THEN_1V1_KIND = {
    'game_format': ONE_V_ONE,
    'elimination_mode': EliminationMode.ROUND_ROBIN,
    'playoff_game_format': ONE_V_ONE,
    'playoff_elimination_mode': SINGLE_ELIMINATION,
    'playoff_group_count': 2,
    'playoff_qualifiers_per_group': 1,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}
LEADERBOARD_THEN_FFA_KIND = {
    'game_format': GameFormat.HIGHSCORE,
    'playoff_game_format': FREE_FOR_ALL,
    'playoff_elimination_mode': SINGLE_ELIMINATION,
    'playoff_qualifier_count': 4,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(
        brand, PartyID(f'f03-occupancy-{uuid7().hex[:8]}'), 'F03 occupancy'
    )


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'F03OccupancyMarker{i}') for i in range(4)]


@pytest.fixture(autouse=True)
def _session(party):
    """Provide the app context, and leave no open transaction behind."""
    yield
    db.session.rollback()


@pytest.fixture
def make_tournament(party, users):
    def _make(kind, *, status=TournamentStatus.ONGOING) -> TournamentID:
        fields = {'elimination_mode': SINGLE_ELIMINATION, **kind}
        tournament_id = TournamentID(uuid7())
        repo.create_tournament(
            Tournament(
                id=tournament_id,
                party_id=party.id,
                name=f'Occupancy {tournament_id}',
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


def _participant_ids(tournament_id) -> list[TournamentParticipantID]:
    return list(
        db.session.scalars(
            select(DbTournamentParticipant.id)
            .where(DbTournamentParticipant.tournament_id == tournament_id)
            .order_by(DbTournamentParticipant.id)
        )
    )


def _make_match(tournament_id, *, phase=1) -> TournamentMatchID:
    match_id = TournamentMatchID(uuid7())
    repo.create_match(
        TournamentMatch(
            id=match_id,
            tournament_id=tournament_id,
            group_order=None,
            match_order=0,
            round=0,
            next_match_id=None,
            confirmed_by=None,
            created_at=NOW,
            phase=phase,
        ),
        changed_at=NOW - timedelta(days=1),
    )
    return match_id


def _contestant(match_id, participant_id) -> TournamentMatchToContestant:
    return TournamentMatchToContestant(
        id=TournamentMatchToContestantID(uuid7()),
        tournament_match_id=match_id,
        team_id=None,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )


def _occupied_since(match_id) -> datetime | None:
    """Read the column itself, never a cached row."""
    return db.session.execute(
        select(DbTournamentMatch.occupied_since).where(
            DbTournamentMatch.id == match_id
        )
    ).scalar_one()


def _last_changed_at(match_id) -> datetime | None:
    return db.session.execute(
        select(DbTournamentMatch.last_changed_at).where(
            DbTournamentMatch.id == match_id
        )
    ).scalar_one()


def _committed_occupied_since(match_id) -> datetime | None:
    """Read through a separate connection: only committed data is visible."""
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                'SELECT occupied_since FROM lan_tournament_matches'
                ' WHERE id = :id'
            ),
            {'id': match_id},
        ).scalar_one()


def _seat_one_by_one(match_id, tournament_id, count, *, changed_at=None):
    """Insert contestants and return the occupancy after each insert."""
    seen = []
    for i, participant_id in enumerate(_participant_ids(tournament_id)[:count]):
        repo.create_match_contestant(
            _contestant(match_id, participant_id),
            changed_at=NOW + i * MINUTE if changed_at is None else changed_at,
        )
        seen.append(_occupied_since(match_id))
    return seen


# -------------------------------------------------------------------- #
# the two-row shortcut
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'kind, phase, occupied',
    [
        (ONE_V_ONE_KIND, 1, True),
        (GROUPS_THEN_1V1_KIND, 1, True),
        (GROUPS_THEN_1V1_KIND, 2, True),
        (FFA_KIND, 1, False),
        (LEADERBOARD_THEN_FFA_KIND, 2, False),
    ],
    ids=['1v1', 'groups-phase-1', '1v1-playoff-phase-2', 'ffa',
         'ffa-playoff-phase-2'],
)
# fmt: on
def test_two_row_occupancy_only_for_effective_1v1(
    make_tournament, kind, phase, occupied
):
    tournament_id = make_tournament(kind)
    match_id = _make_match(tournament_id, phase=phase)

    seen = _seat_one_by_one(match_id, tournament_id, 4)

    # The second row fixes both sides of a 1v1 match. Later rows never move
    # the original occupancy, and a lobby is not occupied by row count.
    expected = [None, NOW + MINUTE, NOW + MINUTE, NOW + MINUTE]
    assert seen == (expected if occupied else [None] * 4)


def test_the_engine_insert_occupies_1v1_and_leaves_an_ffa_lobby_alone(
    make_tournament,
):
    one_v_one = make_tournament(ONE_V_ONE_KIND)
    lobby = make_tournament(FFA_KIND)
    match_id = _make_match(one_v_one)
    lobby_id = _make_match(lobby)
    db.session.commit()

    seats = ((one_v_one, match_id, 2), (lobby, lobby_id, 3))
    for tournament_id, target, count in seats:
        for participant_id in _participant_ids(tournament_id)[:count]:
            engine._create_match_contestant_flush(
                _contestant(target, participant_id)
            )
    db.session.commit()

    assert _committed_occupied_since(match_id) is not None
    assert _committed_occupied_since(lobby_id) is None


# -------------------------------------------------------------------- #
# the completed-lobby marker writer
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'kind, phase',
    [(FFA_KIND, 1), (LEADERBOARD_THEN_FFA_KIND, 2)],
    ids=['ffa', 'ffa-playoff-phase-2'],
)
# fmt: on
def test_ffa_lobby_occupancy_set_by_completed_lobby_marker(
    make_tournament, monkeypatch, kind, phase
):
    tournament_id = make_tournament(kind)
    match_id = _make_match(tournament_id, phase=phase)
    assert _seat_one_by_one(match_id, tournament_id, 4) == [None] * 4
    db.session.commit()

    def refuse(*args, **kwargs):
        raise AssertionError('the marker writer must leave the commit alone')

    monkeypatch.setattr(db.session, 'commit', refuse)
    monkeypatch.setattr(repo, 'commit_session', refuse)

    # Once, with the time it is given.
    assert repo.set_ffa_lobby_occupied_since_if_unset_flush(match_id, NOW)
    assert _occupied_since(match_id) == NOW
    # Unset-guarded: a repeat neither succeeds nor moves the original.
    assert not repo.set_ffa_lobby_occupied_since_if_unset_flush(
        match_id, NOW + 5 * MINUTE
    )
    assert _occupied_since(match_id) == NOW

    # Flush only: the owner's transaction decides.
    assert _committed_occupied_since(match_id) is None
    monkeypatch.undo()
    repo.rollback_session()
    assert _occupied_since(match_id) is None

    assert repo.set_ffa_lobby_occupied_since_if_unset_flush(match_id, NOW)
    db.session.commit()
    assert _committed_occupied_since(match_id) == NOW


def test_the_marker_never_resets_an_original_occupancy(make_tournament):
    tournament_id = make_tournament(FFA_KIND)
    match_id = _make_match(tournament_id)
    original = NOW - timedelta(hours=1)
    db.session.execute(
        update(DbTournamentMatch)
        .where(DbTournamentMatch.id == match_id)
        .values(occupied_since=original)
    )
    db.session.commit()

    assert not repo.set_ffa_lobby_occupied_since_if_unset_flush(match_id, NOW)

    assert _occupied_since(match_id) == original


# fmt: off
@pytest.mark.parametrize(
    'kind, phase',
    [
        (ONE_V_ONE_KIND, 1),
        (GROUPS_THEN_1V1_KIND, 2),
        (LEADERBOARD_THEN_FFA_KIND, 1),
    ],
    ids=['1v1', '1v1-playoff-phase-2', 'ffa-playoff-leaderboard-phase-1'],
)
# fmt: on
def test_the_marker_refuses_a_match_that_is_not_an_ffa_lobby(
    make_tournament, kind, phase
):
    tournament_id = make_tournament(kind)
    match_id = _make_match(tournament_id, phase=phase)
    db.session.commit()

    with pytest.raises(ValueError, match='not a free-for-all lobby'):
        repo.set_ffa_lobby_occupied_since_if_unset_flush(match_id, NOW)

    assert _occupied_since(match_id) is None


def test_the_marker_refuses_an_unknown_match():
    with pytest.raises(ValueError, match='Unknown match ID'):
        repo.set_ffa_lobby_occupied_since_if_unset_flush(
            TournamentMatchID(uuid7()), NOW
        )


# -------------------------------------------------------------------- #
# the time
# -------------------------------------------------------------------- #


# fmt: off
@pytest.mark.parametrize(
    'operation_time, expected',
    [
        (NOW, NOW),
        (NOW.replace(tzinfo=UTC), NOW),
        (NOW.replace(tzinfo=PLUS_TWO), NOW - timedelta(hours=2)),
    ],
    ids=['naive', 'aware-utc', 'aware-plus-two'],
)
# fmt: on
def test_occupancy_time_is_naive_utc_operation_time(
    make_tournament, operation_time, expected
):
    one_v_one = make_tournament(ONE_V_ONE_KIND)
    lobby = make_tournament(FFA_KIND)
    match_id = _make_match(one_v_one)
    lobby_id = _make_match(lobby)
    db.session.commit()
    db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))

    _seat_one_by_one(match_id, one_v_one, 2, changed_at=operation_time)
    _seat_one_by_one(lobby_id, lobby, 3, changed_at=operation_time)
    assert repo.set_ffa_lobby_occupied_since_if_unset_flush(
        lobby_id, operation_time
    )

    assert _occupied_since(match_id) == expected
    assert _occupied_since(lobby_id) == expected
    # The last change takes the same instant: one operation time.
    assert _last_changed_at(match_id) == expected


def test_the_server_clock_is_the_operation_time_when_none_is_given(
    make_tournament,
):
    tournament_id = make_tournament(ONE_V_ONE_KIND)
    match_id = _make_match(tournament_id)
    db.session.commit()
    db.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
    before = datetime.now(UTC).replace(tzinfo=None)

    participant_ids = _participant_ids(tournament_id)
    for participant_id in participant_ids[:2]:
        repo.create_match_contestant(_contestant(match_id, participant_id))

    after = datetime.now(UTC).replace(tzinfo=None)
    stored = _occupied_since(match_id)
    assert stored.tzinfo is None
    # A zone slip would be hours, not seconds.
    assert before - MINUTE <= stored <= after + MINUTE
    # The stamp and the occupancy share the one sample of the second call.
    assert _last_changed_at(match_id) == stored
