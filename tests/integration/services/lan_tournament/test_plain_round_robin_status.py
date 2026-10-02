"""
tests.integration.services.lan_tournament.test_plain_round_robin_status
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A plain round robin completes only while it is ongoing; a change into
ongoing completes one that settled while it was not running.
"""

from datetime import datetime, UTC
from itertools import count
from uuid import UUID

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2026-plain-rr-status')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('plainrrstatusbrand', 'Plain RR Status Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Plain RR Status')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'PlainRrStatusUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(*, participants=4, contestant_type=ContestantType.SOLO):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Plain RR Status Tournament {next(_counter)}',
            contestant_type=contestant_type,
            game_format=GameFormat.ONE_V_ONE,
            elimination_mode=EliminationMode.ROUND_ROBIN,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
        for user in users[:participants]:
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _generate(tournament, admin):
    board = tournament_seeding_service.get_board(
        tournament.id, initiator_id=admin.id
    ).unwrap()
    tournament_seeding_service.generate_from_seeding(
        tournament.id, expected_version=board.version, initiator_id=admin.id
    ).unwrap()


def _change(tournament, status, admin):
    result = tournament_service.change_status(tournament.id, status, admin.id)
    assert result.is_ok(), result.unwrap_err()


def _matches(tournament):
    return tournament_repository.get_matches_for_tournament(tournament.id)


def _members(match):
    return [
        str(c.participant_id or c.team_id)
        for c in tournament_repository.get_contestants_for_match(match.id)
    ]


def _play(match, admin, winner=None):
    """Let `winner`, or else the lower ID, win 2:0."""
    members = _members(match)
    winner = winner or min(members)
    loser = next(c for c in members if c != winner)
    result = tournament_match_service.admin_set_and_confirm_match(
        match.id, admin.id, {UUID(winner): 2, UUID(loser): 0}
    )
    assert result.is_ok(), result.unwrap_err()


def _found(tournament):
    db.session.rollback()
    return tournament_repository.get_tournament(tournament.id)


def test_the_last_confirm_while_paused_does_not_complete(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _generate(tournament, admin)
    _change(tournament, TournamentStatus.ONGOING, admin)
    _change(tournament, TournamentStatus.PAUSED, admin)

    for match in _matches(tournament):
        _play(match, admin)

    found = _found(tournament)
    assert found.tournament_status is TournamentStatus.PAUSED
    assert found.winner_participant_id is None


def test_confirming_every_match_before_the_start_does_not_complete(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _generate(tournament, admin)

    for match in _matches(tournament):
        _play(match, admin)

    found = _found(tournament)
    assert found.tournament_status is TournamentStatus.REGISTRATION_CLOSED
    assert found.winner_participant_id is None


def test_the_resume_completes_a_round_robin_settled_while_paused(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _generate(tournament, admin)
    _change(tournament, TournamentStatus.ONGOING, admin)
    _change(tournament, TournamentStatus.PAUSED, admin)
    for match in _matches(tournament):
        _play(match, admin)
    leader = min(c for m in _matches(tournament) for c in _members(m))

    _change(tournament, TournamentStatus.ONGOING, admin)

    found = _found(tournament)
    assert found.tournament_status is TournamentStatus.COMPLETED
    assert str(found.winner_participant_id) == leader


def test_the_reopen_of_a_settled_round_robin_stays_ongoing(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament()
    _generate(tournament, admin)
    _change(tournament, TournamentStatus.ONGOING, admin)
    for match in _matches(tournament):
        _play(match, admin)
    assert _found(tournament).tournament_status is TournamentStatus.COMPLETED

    _change(tournament, TournamentStatus.ONGOING, admin)

    found = _found(tournament)
    assert found.tournament_status is TournamentStatus.ONGOING
    assert found.winner_participant_id is None


def test_a_winner_decision_does_not_complete_a_cancelled_round_robin(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(participants=3)
    _generate(tournament, admin)
    _change(tournament, TournamentStatus.ONGOING, admin)
    a, b, c = sorted({cid for m in _matches(tournament) for cid in _members(m)})
    cycle = {frozenset({a, b}): a, frozenset({b, c}): b, frozenset({c, a}): c}
    for match in _matches(tournament):
        _play(match, admin, cycle[frozenset(_members(match))])
    assert _found(tournament).tournament_status is TournamentStatus.ONGOING
    _change(tournament, TournamentStatus.CANCELLED, admin)

    tournament_qualification_service.save_decision(
        tournament.id,
        'winner',
        [a, b, c],
        reason='Decided by a coin toss.',
        initiator_id=admin.id,
    )

    found = _found(tournament)
    assert found.tournament_status is TournamentStatus.CANCELLED
    assert found.winner_participant_id is None
