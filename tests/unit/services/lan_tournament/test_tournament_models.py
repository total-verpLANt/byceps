"""
tests.unit.services.lan_tournament.test_tournament_models
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_comment import (
    TournamentMatchComment,
    TournamentMatchCommentID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
    TournamentMatchToContestantID,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_seed import (
    TournamentSeed,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models.user import UserID

from tests.helpers import generate_uuid


NOW = datetime(2025, 6, 15, 14, 0, 0)


def test_tournament_creation_with_all_fields():
    tournament_id = TournamentID(generate_uuid())

    tournament = Tournament(
        id=tournament_id,
        party_id=PartyID('lan-party-2025'),
        name='CS2 Championship',
        game='Counter-Strike 2',
        description='Annual CS2 tournament',
        image_url='https://example.com/banner.png',
        ruleset='Standard competitive rules',
        start_time=datetime(2025, 7, 1, 18, 0),
        created_at=NOW,
        min_players=2,
        max_players=64,
        min_teams=4,
        max_teams=16,
        min_players_in_team=5,
        max_players_in_team=5,
        contestant_type=ContestantType.TEAM,
        tournament_status=TournamentStatus.DRAFT,
        tournament_mode=TournamentMode.SINGLE_ELIMINATION,
    )

    assert tournament.id == tournament_id
    assert tournament.party_id == PartyID('lan-party-2025')
    assert tournament.name == 'CS2 Championship'
    assert tournament.game == 'Counter-Strike 2'
    assert tournament.description == 'Annual CS2 tournament'
    assert tournament.image_url == 'https://example.com/banner.png'
    assert tournament.ruleset == 'Standard competitive rules'
    assert tournament.start_time == datetime(2025, 7, 1, 18, 0)
    assert tournament.created_at == NOW
    assert tournament.min_players == 2
    assert tournament.max_players == 64
    assert tournament.min_teams == 4
    assert tournament.max_teams == 16
    assert tournament.min_players_in_team == 5
    assert tournament.max_players_in_team == 5
    assert tournament.contestant_type == ContestantType.TEAM
    assert tournament.tournament_status == TournamentStatus.DRAFT
    assert tournament.tournament_mode == TournamentMode.SINGLE_ELIMINATION


def test_tournament_creation_with_none_optional_fields():
    tournament = Tournament(
        id=TournamentID(generate_uuid()),
        party_id=PartyID('lan-party-2025'),
        name='Quick Tournament',
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
        tournament_status=None,
        tournament_mode=None,
    )

    assert tournament.game is None
    assert tournament.description is None
    assert tournament.image_url is None
    assert tournament.ruleset is None
    assert tournament.start_time is None
    assert tournament.min_players is None
    assert tournament.max_players is None
    assert tournament.min_teams is None
    assert tournament.max_teams is None
    assert tournament.min_players_in_team is None
    assert tournament.max_players_in_team is None
    assert tournament.contestant_type is None
    assert tournament.tournament_status is None
    assert tournament.tournament_mode is None


def test_tournament_is_frozen():
    tournament = _create_tournament()

    with pytest.raises(FrozenInstanceError):
        tournament.name = 'New Name'


def test_tournament_team_creation():
    team_id = TournamentTeamID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    captain_id = UserID(generate_uuid())

    team = TournamentTeam(
        id=team_id,
        tournament_id=tournament_id,
        name='Team Alpha',
        tag='ALPHA',
        description='The best team',
        image_url='https://example.com/team.png',
        captain_user_id=captain_id,
        join_code='secret123',
        created_at=NOW,
    )

    assert team.id == team_id
    assert team.tournament_id == tournament_id
    assert team.name == 'Team Alpha'
    assert team.tag == 'ALPHA'
    assert team.description == 'The best team'
    assert team.captain_user_id == captain_id
    assert team.join_code == 'secret123'
    assert team.created_at == NOW


def test_tournament_team_is_frozen():
    team = TournamentTeam(
        id=TournamentTeamID(generate_uuid()),
        tournament_id=TournamentID(generate_uuid()),
        name='Team',
        tag=None,
        description=None,
        image_url=None,
        captain_user_id=UserID(generate_uuid()),
        join_code=None,
        created_at=NOW,
    )

    with pytest.raises(FrozenInstanceError):
        team.name = 'New Name'


def test_tournament_participant_creation():
    participant_id = TournamentParticipantID(generate_uuid())
    user_id = UserID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    team_id = TournamentTeamID(generate_uuid())

    participant = TournamentParticipant(
        id=participant_id,
        user_id=user_id,
        tournament_id=tournament_id,
        substitute_player=False,
        team_id=team_id,
        created_at=NOW,
    )

    assert participant.id == participant_id
    assert participant.user_id == user_id
    assert participant.tournament_id == tournament_id
    assert participant.substitute_player is False
    assert participant.team_id == team_id
    assert participant.created_at == NOW


def test_tournament_participant_without_team():
    participant = TournamentParticipant(
        id=TournamentParticipantID(generate_uuid()),
        user_id=UserID(generate_uuid()),
        tournament_id=TournamentID(generate_uuid()),
        substitute_player=True,
        team_id=None,
        created_at=NOW,
    )

    assert participant.team_id is None
    assert participant.substitute_player is True


def test_tournament_participant_is_frozen():
    participant = TournamentParticipant(
        id=TournamentParticipantID(generate_uuid()),
        user_id=UserID(generate_uuid()),
        tournament_id=TournamentID(generate_uuid()),
        substitute_player=False,
        team_id=None,
        created_at=NOW,
    )

    with pytest.raises(FrozenInstanceError):
        participant.substitute_player = True


def test_tournament_match_creation():
    match_id = TournamentMatchID(generate_uuid())
    tournament_id = TournamentID(generate_uuid())
    confirmed_by = UserID(generate_uuid())

    match = TournamentMatch(
        id=match_id,
        tournament_id=tournament_id,
        group_order=1,
        match_order=3,
        round=0,
        next_match_id=None,
        confirmed_by=confirmed_by,
        created_at=NOW,
    )

    assert match.id == match_id
    assert match.tournament_id == tournament_id
    assert match.group_order == 1
    assert match.match_order == 3
    assert match.round == 0
    assert match.next_match_id is None
    assert match.confirmed_by == confirmed_by
    assert match.created_at == NOW


def test_tournament_match_unconfirmed():
    match = TournamentMatch(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=TournamentID(generate_uuid()),
        group_order=None,
        match_order=None,
        round=None,
        next_match_id=None,
        confirmed_by=None,
        created_at=NOW,
    )

    assert match.group_order is None
    assert match.match_order is None
    assert match.round is None
    assert match.next_match_id is None
    assert match.confirmed_by is None


def test_tournament_match_comment_creation():
    comment_id = TournamentMatchCommentID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    user_id = UserID(generate_uuid())

    comment = TournamentMatchComment(
        id=comment_id,
        tournament_match_id=match_id,
        created_by=user_id,
        comment='Great match!',
        created_at=NOW,
    )

    assert comment.id == comment_id
    assert comment.tournament_match_id == match_id
    assert comment.created_by == user_id
    assert comment.comment == 'Great match!'
    assert comment.created_at == NOW


def test_tournament_match_to_contestant_creation():
    contestant_id = TournamentMatchToContestantID(generate_uuid())
    match_id = TournamentMatchID(generate_uuid())
    team_id = TournamentTeamID(generate_uuid())

    contestant = TournamentMatchToContestant(
        id=contestant_id,
        tournament_match_id=match_id,
        team_id=team_id,
        participant_id=None,
        score=13,
        created_at=NOW,
    )

    assert contestant.id == contestant_id
    assert contestant.tournament_match_id == match_id
    assert contestant.team_id == team_id
    assert contestant.participant_id is None
    assert contestant.score == 13


def test_tournament_match_to_contestant_with_participant():
    participant_id = TournamentParticipantID(generate_uuid())

    contestant = TournamentMatchToContestant(
        id=TournamentMatchToContestantID(generate_uuid()),
        tournament_match_id=TournamentMatchID(generate_uuid()),
        team_id=None,
        participant_id=participant_id,
        score=None,
        created_at=NOW,
    )

    assert contestant.team_id is None
    assert contestant.participant_id == participant_id
    assert contestant.score is None


def test_tournament_seed_creation():
    seed = TournamentSeed(
        match_order=1,
        round=0,
        entry_a='Team A',
        entry_b='Team B',
    )

    assert seed.match_order == 1
    assert seed.round == 0
    assert seed.entry_a == 'Team A'
    assert seed.entry_b == 'Team B'


def test_tournament_seed_is_frozen():
    seed = TournamentSeed(
        match_order=1,
        round=0,
        entry_a='Team A',
        entry_b='Team B',
    )

    with pytest.raises(FrozenInstanceError):
        seed.match_order = 2


# -------------------------------------------------------------------- #
# helpers


def _create_tournament(**kwargs) -> Tournament:
    defaults = {
        'id': TournamentID(generate_uuid()),
        'party_id': PartyID('test-party'),
        'name': 'Test Tournament',
        'game': None,
        'description': None,
        'image_url': None,
        'ruleset': None,
        'start_time': None,
        'created_at': NOW,
        'min_players': None,
        'max_players': None,
        'min_teams': None,
        'max_teams': None,
        'min_players_in_team': None,
        'max_players_in_team': None,
        'contestant_type': None,
        'tournament_status': None,
        'tournament_mode': None,
    }
    defaults.update(kwargs)
    return Tournament(**defaults)
