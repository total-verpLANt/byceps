"""
tests.integration.services.lan_tournament.test_ffa_lobby_mail
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Free-for-all lobbies are mailed once, when the tournament is ongoing, and
never at the generation before the start.
"""

from datetime import datetime, UTC
from itertools import count

import pytest
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_lobby_mail_service,
    tournament_notification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.dbmodels.match import DbTournamentMatch
from byceps.services.lan_tournament.dbmodels.match_contestant import (
    DbTournamentMatchToContestant,
)
from byceps.services.lan_tournament.dbmodels.participant import (
    DbTournamentParticipant,
)
from byceps.services.lan_tournament.dbmodels.tournament import DbTournament
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-ffa-lobby-mail')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'LAN Party FFA Lobby Mail')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaLobbyMailPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaLobbyMailAdmin')


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Record each lobby mail with the announcements committed at that time."""
    calls = []

    def spy(tournament_id, match_id):
        calls.append((tournament_id, match_id, _announced_ids(tournament_id)))

    monkeypatch.setattr(
        tournament_notification_service, 'send_ffa_lobby_emails', spy
    )
    return calls


@pytest.fixture
def make_ffa_tournament(party, players, admin):
    """Return a tournament with its initial lobbies generated, not started."""
    created = []

    def _make():
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID,
            f'FFA Lobby Mail {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=EliminationMode.SINGLE_ELIMINATION,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            point_table=[4, 3, 2, 1],
            group_size_min=4,
            group_size_max=6,
            advancement_count=2,
            max_players=8,
        ).unwrap()
        created.append(tournament)
        _add_participants(tournament, players)
        board = tournament_seeding_service.get_board(
            tournament.id, initiator_id=admin.id
        ).unwrap()
        tournament_seeding_service.generate_from_seeding(
            tournament.id,
            expected_version=board.version,
            initiator_id=admin.id,
        ).unwrap()
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _add_participants(tournament, players):
    for user in players:
        tournament_repository.create_participant(
            TournamentParticipant(
                id=TournamentParticipantID(generate_uuid7()),
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=datetime.now(UTC),
            )
        )
    db.session.commit()


def _start(tournament, admin):
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    ).unwrap()


def _lobby_ids(tournament):
    return [
        m.id
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
    ]


def _mailed_ids(sent):
    return [str(match_id) for _, match_id, _ in sent]


def _announced_ids(tournament_id) -> set[str]:
    """Return the announcements that other connections can see."""
    with Session(db.engine) as independent:
        rows = independent.execute(
            text(
                "SELECT data->>'match_id' FROM lan_tournament_log_entries"
                ' WHERE tournament_id = :tournament_id'
                " AND event_type = 'match-lobby-announced'"
            ),
            {'tournament_id': str(tournament_id)},
        ).all()
    return {row[0] for row in rows}


def _assert_tournament_row_is_not_locked(tournament_id):
    with Session(db.engine) as independent:
        independent.execute(
            text(
                'SELECT id FROM lan_tournaments'
                ' WHERE id = :tournament_id FOR UPDATE NOWAIT'
            ),
            {'tournament_id': str(tournament_id)},
        )
        independent.rollback()


def _add_lobby(tournament, contestant_count, *, confirmed_by=None):
    """Insert an unplayed FFA lobby; return its ID."""
    now = datetime.now(UTC).replace(tzinfo=None)
    match = DbTournamentMatch(
        generate_uuid7(),
        tournament.id,
        now,
        match_order=0,
        round=9,
        confirmed_by=confirmed_by,
    )
    db.session.add(match)
    db.session.flush()
    _add_contestants(
        match.id,
        tournament_repository.get_participants_for_tournament(tournament.id),
        contestant_count,
    )
    db.session.commit()
    return match.id


def _add_contestants(match_id, participants, contestant_count):
    now = datetime.now(UTC).replace(tzinfo=None)
    for participant in participants[:contestant_count]:
        db.session.add(
            DbTournamentMatchToContestant(
                generate_uuid7(),
                match_id,
                now,
                participant_id=participant.id,
            )
        )
    db.session.flush()


def test_generation_before_start_mails_nothing(make_ffa_tournament, sent):
    tournament = make_ffa_tournament()

    assert len(_lobby_ids(tournament)) == 2
    assert sent == []
    assert _announced_ids(tournament.id) == set()


def test_start_mails_every_lobby_once(make_ffa_tournament, sent, admin):
    tournament = make_ffa_tournament()

    _start(tournament, admin)

    lobbies = _lobby_ids(tournament)
    assert len(lobbies) == 2
    assert sorted(_mailed_ids(sent)) == sorted(str(i) for i in lobbies)
    assert {tournament_id for tournament_id, _, _ in sent} == {tournament.id}
    assert _announced_ids(tournament.id) == {str(i) for i in lobbies}


def test_lobbies_are_mailed_only_after_their_record_is_committed(
    make_ffa_tournament, sent, admin
):
    tournament = make_ffa_tournament()

    _start(tournament, admin)

    lobbies = {str(i) for i in _lobby_ids(tournament)}
    assert len(sent) == len(lobbies)
    for _, _, committed_at_send in sent:
        assert committed_at_send == lobbies


def test_pause_and_resume_do_not_mail_again(make_ffa_tournament, sent, admin):
    tournament = make_ffa_tournament()
    _start(tournament, admin)
    assert len(sent) == 2

    tournament_service.change_status(
        tournament.id, TournamentStatus.PAUSED, admin.id
    ).unwrap()
    _start(tournament, admin)

    assert len(sent) == 2


def test_repeat_announce_is_idempotent(make_ffa_tournament, sent, admin):
    tournament = make_ffa_tournament()
    _start(tournament, admin)
    assert len(sent) == 2

    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)
    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    assert len(sent) == 2


def test_new_lobby_while_ongoing_is_mailed_alone(
    make_ffa_tournament, sent, admin
):
    tournament = make_ffa_tournament()
    _start(tournament, admin)
    assert len(sent) == 2

    new_lobby_id = _add_lobby(tournament, 2)
    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    assert len(sent) == 3
    assert sent[2][:2] == (tournament.id, new_lobby_id)


def test_confirmed_and_short_lobbies_are_skipped(
    make_ffa_tournament, sent, admin
):
    tournament = make_ffa_tournament()
    _start(tournament, admin)
    assert len(sent) == 2

    _add_lobby(tournament, 2, confirmed_by=admin.id)
    short_lobby_id = _add_lobby(tournament, 1)
    _add_lobby(tournament, 0)
    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    assert len(sent) == 2

    # A skipped lobby is not recorded, so it is mailed once it is complete.
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    _add_contestants(short_lobby_id, participants[1:], 1)
    db.session.commit()
    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    assert len(sent) == 3
    assert sent[2][:2] == (tournament.id, short_lobby_id)


@pytest.mark.parametrize('field', ['placement', 'points', 'score'])
def test_lobby_with_an_entered_result_is_skipped(
    make_ffa_tournament, sent, admin, field
):
    tournament = make_ffa_tournament()
    _start(tournament, admin)
    assert len(sent) == 2

    playing_lobby_id = _add_lobby(tournament, 2)
    db.session.execute(
        update(DbTournamentMatchToContestant)
        .where(
            DbTournamentMatchToContestant.tournament_match_id
            == playing_lobby_id
        )
        .values({field: 1})
    )
    db.session.commit()
    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    assert len(sent) == 2
    assert str(playing_lobby_id) not in _announced_ids(tournament.id)


def test_one_v_one_tournament_announces_nothing(party, players, sent):
    now = datetime.now(UTC).replace(tzinfo=None)
    tournament = DbTournament(
        generate_uuid7(),
        party.id,
        f'Lobby Mail 1v1 {next(_counter)}',
        now,
        game_format='ONE_V_ONE',
        tournament_status='ONGOING',
    )
    db.session.add(tournament)
    db.session.flush()
    participants = [
        DbTournamentParticipant(generate_uuid7(), user.id, tournament.id, now)
        for user in players[:2]
    ]
    db.session.add_all(participants)
    match = DbTournamentMatch(
        generate_uuid7(), tournament.id, now, match_order=0, round=0
    )
    db.session.add(match)
    db.session.flush()
    _add_contestants(match.id, participants, 2)
    db.session.commit()

    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    assert sent == []
    assert _announced_ids(tournament.id) == set()


def test_playoff_lobbies_are_mailed_when_released_while_ongoing(
    party, players, admin, sent
):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        f'FFA Lobby Mail Playoffs {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        point_table=[4, 3, 2, 1],
        group_size_min=4,
        group_size_max=6,
        advancement_count=2,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=8,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
    ).unwrap()
    _add_participants(tournament, players)
    participants = tournament_repository.get_participants_for_tournament(
        tournament.id
    )
    for value, participant in enumerate(participants, start=1):
        tournament_score_service.submit_score(
            tournament.id, value * 10, participant_id=participant.id
        ).unwrap()
    assert sent == []

    tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=admin.id
    ).unwrap()

    lobbies = _lobby_ids(tournament)
    assert len(lobbies) == 2
    assert sorted(_mailed_ids(sent)) == sorted(str(i) for i in lobbies)
    assert {
        m.phase
        for m in tournament_repository.get_matches_for_tournament(tournament.id)
    } == {2}


@pytest.mark.parametrize('branch', ['not_ongoing', 'failure'])
def test_announce_releases_the_tournament_lock(
    make_ffa_tournament, sent, admin, monkeypatch, branch
):
    tournament = make_ffa_tournament()
    if branch == 'failure':
        _start(tournament, admin)
        monkeypatch.setattr(
            tournament_lobby_mail_service.repository,
            'count_log_entries_with_data',
            _raise_unexpected,
        )
        _add_lobby(tournament, 2)

    tournament_lobby_mail_service.announce_ffa_lobbies(tournament.id)

    _assert_tournament_row_is_not_locked(tournament.id)
    assert len(sent) == (2 if branch == 'failure' else 0)


def _raise_unexpected(*args, **kwargs):
    raise RuntimeError('unexpected')
