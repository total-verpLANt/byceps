"""
tests.integration.services.lan_tournament.test_ffa_de_podium
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The podium of a plain FFA double elimination (no playoffs) comes from
the grand final lobby, whatever order the matches are fetched in.
"""

from datetime import datetime, UTC

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
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
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-ffa-de-podium')


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ffadepodiumbrand', 'FFA DE Podium Brand')
    return make_party(brand, PARTY_ID, 'LAN Party FFA DE Podium')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'FfaDePodiumUser{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaDePodiumAdmin')


def _completed_ffa(party, users, admin, elimination_mode, lobbies):
    """Return a completed FFA tournament; `lobbies` are
    (bracket, round, group, [participant indexes in placement order])."""
    result = tournament_service.create_tournament(
        PARTY_ID,
        f'FFA DE Podium {generate_uuid7()}',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=elimination_mode,
        tournament_status=TournamentStatus.REGISTRATION_CLOSED,
        max_players=16,
        group_size_min=2,
        group_size_max=4,
        advancement_count=2,
        point_table=[4, 3, 2, 1],
    )
    assert result.is_ok(), result.unwrap_err()
    tournament, _ = result.unwrap()
    now = datetime.now(UTC)
    participants = []
    for user in users:
        participant = TournamentParticipant(
            id=TournamentParticipantID(generate_uuid7()),
            user_id=user.id,
            tournament_id=tournament.id,
            substitute_player=False,
            team_id=None,
            created_at=now,
        )
        tournament_repository.create_participant(participant)
        participants.append(participant)

    for bracket, round_, group, placed in lobbies:
        match_id = TournamentMatchID(generate_uuid7())
        tournament_repository.create_match(
            TournamentMatch(
                id=match_id,
                tournament_id=tournament.id,
                group_order=group,
                match_order=0,
                round=round_,
                next_match_id=None,
                confirmed_by=None,
                created_at=now,
                bracket=bracket,
                phase=None,
            )
        )
        for place, index in enumerate(placed, start=1):
            tournament_repository.create_match_contestant(
                TournamentMatchToContestant(
                    id=TournamentMatchToContestantID(generate_uuid7()),
                    tournament_match_id=match_id,
                    team_id=None,
                    participant_id=participants[index].id,
                    score=None,
                    created_at=now,
                    placement=place,
                    points=5 - place,
                )
            )
        tournament_repository.confirm_match(match_id, admin.id)
    db.session.commit()
    tournament_repository.set_tournament_winner(
        tournament.id,
        winner_team_id=None,
        winner_participant_id=participants[lobbies[-1][3][0]].id,
    )
    tournament_repository.set_tournament_status_flush(
        tournament.id, TournamentStatus.COMPLETED
    )
    db.session.commit()
    return tournament_repository.get_tournament(tournament.id)


DE_LOBBIES = [
    (Bracket.WINNERS, 0, 0, [0, 1, 2, 3]),
    (Bracket.WINNERS, 0, 1, [4, 5, 6, 7]),
    (Bracket.WINNERS, 1, 0, [0, 4, 1, 5]),
    (Bracket.LOSERS, 1, 0, [2, 6, 3, 7]),
    (Bracket.LOSERS, 1, 1, [3, 7, 2, 6]),
    (Bracket.GRAND_FINAL, 0, 0, [4, 2, 0]),
]


@pytest.mark.parametrize('reverse', [False, True])
def test_plain_ffa_de_podium_comes_from_the_grand_final(
    party, users, admin, monkeypatch, reverse
):
    tournament = _completed_ffa(
        party, users, admin, EliminationMode.DOUBLE_ELIMINATION, DE_LOBBIES
    )
    if reverse:
        fetch = tournament_match_service.get_matches_for_tournament
        monkeypatch.setattr(
            tournament_match_service,
            'get_matches_for_tournament',
            lambda tournament_id: list(reversed(fetch(tournament_id))),
        )

    podium = tournament_service.resolve_podium_display_names(tournament)

    assert podium == {
        'runner_up': users[2].screen_name,
        'bronze': users[0].screen_name,
    }


def test_plain_ffa_se_podium_still_comes_from_the_last_round(
    party, users, admin
):
    lobbies = [
        (Bracket.WINNERS, 0, 0, [0, 1, 2, 3]),
        (Bracket.WINNERS, 0, 1, [4, 5, 6, 7]),
        (Bracket.WINNERS, 1, 0, [4, 0, 5, 1]),
    ]
    tournament = _completed_ffa(
        party, users, admin, EliminationMode.SINGLE_ELIMINATION, lobbies
    )

    podium = tournament_service.resolve_podium_display_names(tournament)

    assert podium == {
        'runner_up': users[0].screen_name,
        'bronze': users[5].screen_name,
    }
