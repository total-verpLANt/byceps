"""
tests.integration.services.lan_tournament.test_seeding_timestamps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The seeding columns are naive `TIMESTAMP`: the service must hand them naive
UTC, or PostgreSQL casts an aware value with the session time zone.
"""

from datetime import datetime, timedelta, UTC
from itertools import count

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_seeding_service as svc,
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


PARTY_ID = PartyID('lan-party-2024-seeding-timestamps')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('seedingtimestampsbrand', 'Seeding Timestamps Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Seeding Timestamps')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'SeedingTimestampsUser{i}') for i in range(4)]


@pytest.fixture
def tournament(party, users):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Seeding Timestamps Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
    )
    tournament, _ = result.unwrap()
    for user in users:
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
    yield tournament
    db.session.rollback()
    tournament_service.delete_tournament(tournament.id)


def _berlin():
    """Make the session zone differ from UTC for the next transaction."""
    db.session.execute(text("SET LOCAL TIME ZONE 'Europe/Berlin'"))


def _stored(tournament):
    """Read the naive columns over a connection of their own."""
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                'SELECT created_at, updated_at, generated_at'
                ' FROM lan_tournament_seedings'
                ' WHERE tournament_id = :id AND target = :target'
            ),
            {'id': tournament.id, 'target': 'initial'},
        ).one()


def _assert_utc_now(value):
    assert value is not None
    now = datetime.now(UTC).replace(tzinfo=None)
    assert abs(now - value) < timedelta(minutes=1), (value, now)


def test_drawing_a_draft_stores_naive_utc(tournament):
    _berlin()

    assert svc.get_board(tournament.id, initiator_id=None).is_ok()

    stored = _stored(tournament)
    _assert_utc_now(stored.created_at)
    _assert_utc_now(stored.updated_at)


def test_an_orga_action_stores_naive_utc(tournament, users):
    board = svc.get_board(tournament.id).unwrap()
    db.session.rollback()
    _berlin()

    result = svc.apply_action(
        tournament.id,
        'initial',
        svc.Swap(0, 1),
        expected_version=board.version,
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    _assert_utc_now(_stored(tournament).updated_at)


def test_the_generation_stores_naive_utc(tournament, users):
    board = svc.get_board(tournament.id).unwrap()
    db.session.rollback()
    _berlin()

    result = svc.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=users[0].id,
    )

    assert result.is_ok(), result.unwrap_err()
    _assert_utc_now(_stored(tournament).generated_at)
