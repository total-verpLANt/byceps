"""
tests.integration.services.lan_tournament.test_roster_order
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2024-roster-order')

BASE = datetime(2024, 1, 1, 12, 0, 0)

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('rosterorderbrand', 'Roster Order Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2024 Roster Order')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'RosterOrderUser{i}') for i in range(5)]


@pytest.fixture
def tournament(party):
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'Roster Order Tournament {next(_counter)}',
        contestant_type=ContestantType.SOLO,
    )
    assert result.is_ok()
    tournament, _ = result.unwrap()
    yield tournament
    db.session.rollback()


def test_participants_ordered_by_created_at_then_id(tournament, users):
    # Insert in an order that differs from the expected result order.
    offsets = [3, 1, 2, 1, 0]
    ids = [TournamentParticipantID(generate_uuid7()) for _ in users]
    for user, offset, pid in zip(users, offsets, ids, strict=True):
        tournament_repository.create_participant(
            TournamentParticipant(
                id=pid,
                user_id=user.id,
                tournament_id=tournament.id,
                substitute_player=False,
                team_id=None,
                created_at=BASE + timedelta(seconds=offset),
            )
        )

    actual = tournament_repository.get_participants_for_tournament(
        tournament.id
    )

    expected = sorted(
        zip(offsets, ids, strict=True), key=lambda pair: (pair[0], pair[1])
    )
    assert [p.id for p in actual] == [pid for _, pid in expected]


def test_teams_ordered_by_created_at_then_id(tournament, users):
    offsets = [2, 0, 1, 1, 3]
    ids = [TournamentTeamID(generate_uuid7()) for _ in users]
    for i, (user, offset, tid) in enumerate(
        zip(users, offsets, ids, strict=True)
    ):
        tournament_repository.create_team(
            TournamentTeam(
                id=tid,
                tournament_id=tournament.id,
                name=f'Roster Order Team {i}',
                tag=None,
                description=None,
                image_url=None,
                captain_user_id=user.id,
                join_code=None,
                created_at=BASE + timedelta(seconds=offset),
            )
        )

    actual = tournament_repository.get_teams_for_tournament(tournament.id)

    expected = sorted(
        zip(offsets, ids, strict=True), key=lambda pair: (pair[0], pair[1])
    )
    assert [t.id for t in actual] == [tid for _, tid in expected]


def test_matches_for_round_ordered_by_group_then_match_order(tournament):
    # (group_order, match_order); NULL group_order sorts last.
    keys = [(None, 1), (1, 2), (0, 1), (1, 1), (None, 0), (0, 0)]
    ids = []
    for group_order, match_order in keys:
        match_id = TournamentMatchID(generate_uuid7())
        ids.append(match_id)
        tournament_repository.create_match(
            TournamentMatch(
                id=match_id,
                tournament_id=tournament.id,
                group_order=group_order,
                match_order=match_order,
                round=1,
                next_match_id=None,
                confirmed_by=None,
                created_at=BASE,
            )
        )

    actual = tournament_repository.get_matches_for_round(tournament.id, 1)

    by_id = dict(zip(ids, keys, strict=True))
    assert [by_id[m.id] for m in actual] == [
        (0, 0),
        (0, 1),
        (1, 1),
        (1, 2),
        (None, 0),
        (None, 1),
    ]
