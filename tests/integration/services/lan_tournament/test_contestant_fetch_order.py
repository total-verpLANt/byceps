"""
tests.integration.services.lan_tournament.test_contestant_fetch_order
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The contestants of a round share one ``created_at``, so the fetchers
break that tie by ID instead of leaving it to the heap order.
"""

from datetime import datetime, UTC
from uuid import UUID

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-contestant-fetch-order')


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('contestantfetchorderbrand', 'Fetch Order Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Fetch Order')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FetchOrderPlayer{i}') for i in range(4)]


@pytest.fixture
def lobby(party, players):
    """Return a match whose entries went in against ID order."""
    tournament, _ = tournament_service.create_tournament(
        party.id,
        'Fetch Order',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    ).unwrap()
    now = datetime.now(UTC)
    match = TournamentMatch(
        id=TournamentMatchID(generate_uuid7()),
        tournament_id=tournament.id,
        group_order=0,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=None,
        created_at=now,
    )
    tournament_repository.create_match(match)
    entry_ids = sorted(generate_uuid7() for _ in players)
    for entry_id, player in zip(reversed(entry_ids), players, strict=True):
        participant = TournamentParticipant(
            id=TournamentParticipantID(generate_uuid7()),
            user_id=player.id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=None,
            created_at=now,
        )
        tournament_repository.create_participant(participant)
        tournament_repository.create_match_contestant(
            TournamentMatchToContestant(
                id=TournamentMatchToContestantID(entry_id),
                tournament_match_id=match.id,
                team_id=None,
                participant_id=participant.id,
                score=None,
                created_at=now,
            )
        )
    db.session.commit()
    yield tournament, match, entry_ids
    db.session.rollback()
    tournament_service.delete_tournament(tournament.id)


def _ids(contestants) -> list[UUID]:
    return [UUID(str(c.id)) for c in contestants]


def test_get_contestants_for_match_breaks_the_tie_by_id(lobby):
    _tournament, match, entry_ids = lobby

    fetched = tournament_repository.get_contestants_for_match(match.id)

    assert _ids(fetched) == [UUID(str(i)) for i in entry_ids]


def test_get_contestants_for_matches_breaks_the_tie_by_id(lobby):
    _tournament, match, entry_ids = lobby

    fetched = tournament_repository.get_contestants_for_matches([match.id])

    assert _ids(fetched[match.id]) == [UUID(str(i)) for i in entry_ids]


def test_get_contestants_for_tournament_breaks_the_tie_by_id(lobby):
    tournament, match, entry_ids = lobby

    fetched = tournament_repository.get_contestants_for_tournament(
        tournament.id
    )

    assert _ids(fetched[match.id]) == [UUID(str(i)) for i in entry_ids]
