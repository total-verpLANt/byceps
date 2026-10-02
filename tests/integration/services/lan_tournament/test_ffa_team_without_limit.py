"""
tests.integration.services.lan_tournament.test_ffa_team_without_limit
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Team FFA without a team limit (`max_teams` unset means unlimited) must
generate and release. A stored limit below the lobby minimum is refused
with a static msgid.
"""

from dataclasses import replace
from datetime import datetime, UTC
from uuid import uuid4

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_qualification_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
    tournament_team_service,
)
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


PARTY_ID = PartyID('lan-party-ffa-team-no-limit')


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ffateamnolimitbrand', 'FFA Team No Limit Brand')
    return make_party(brand, PARTY_ID, 'LAN Party FFA Team No Limit')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'FfaTeamNoLimitUser{i}') for i in range(4)]


@pytest.fixture
def cleanup():
    created = []
    yield created
    db.session.rollback()
    for tournament_id in created:
        tournament_service.delete_tournament(tournament_id)


def _add_teams(tournament, users, *, with_scores=False):
    for i, user in enumerate(users):
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
        team = tournament_team_service.create_team(
            tournament.id, f'No limit team {i}', user.id
        ).unwrap()[0]
        if with_scores:
            tournament_score_service.submit_score(
                tournament.id, 100 - i, team_id=team.id
            ).unwrap()


def _close_registration(tournament, user):
    tournament_service.change_status(
        tournament.id, TournamentStatus.REGISTRATION_CLOSED, user.id
    ).unwrap()


def _create_team_ffa(party, **overrides):
    kwargs = {
        'contestant_type': ContestantType.TEAM,
        'game_format': GameFormat.FREE_FOR_ALL,
        'elimination_mode': EliminationMode.SINGLE_ELIMINATION,
        'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
        'tournament_status': TournamentStatus.REGISTRATION_OPEN,
        'point_table': [5, 3, 2, 1],
        'group_size_min': 3,
        'group_size_max': 4,
        'advancement_count': 2,
        'min_players_in_team': 1,
        'max_players_in_team': 2,
    }
    kwargs.update(overrides)
    return tournament_service.create_tournament(
        party.id, 'Team FFA no limit ' + uuid4().hex, **kwargs
    ).unwrap()[0]


def test_team_highscore_playoffs_without_a_team_limit_release(
    party, users, cleanup
):
    tournament = _create_team_ffa(
        party,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=4,
        playoff_release_mode=PlayoffReleaseMode.MANUAL,
    )
    cleanup.append(tournament.id)
    assert tournament.max_teams is None
    _add_teams(tournament, users, with_scores=True)
    _close_registration(tournament, users[0])
    tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, users[0].id
    ).unwrap()
    tournament_score_service.close_leaderboard(
        tournament.id, initiator_id=users[0].id
    ).unwrap()
    board = tournament_seeding_service.ensure_playoff_draft(
        tournament.id
    ).unwrap()

    released = tournament_qualification_service.release_playoffs(
        tournament.id, expected_version=board.version, initiator_id=users[0].id
    )

    assert released.is_ok(), released
    matches = tournament_repository.get_matches_for_tournament(tournament.id)
    assert matches
    assert all(match.phase == 2 for match in matches)


def test_plain_team_ffa_without_a_team_limit_generates(party, users, cleanup):
    tournament = _create_team_ffa(party)
    cleanup.append(tournament.id)
    assert tournament.max_teams is None
    _add_teams(tournament, users)
    _close_registration(tournament, users[0])

    generated = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=users[0].id
    )

    assert generated.is_ok(), generated
    assert tournament_repository.get_matches_for_tournament(tournament.id)


def test_a_team_limit_below_the_lobby_minimum_is_refused_with_a_static_msgid(
    party, users, cleanup
):
    tournament = _create_team_ffa(party)
    cleanup.append(tournament.id)
    _add_teams(tournament, users)
    _close_registration(tournament, users[0])
    tournament_repository.update_tournament(
        replace(tournament, max_teams=2, group_size_min=3)
    )
    db.session.commit()

    generated = tournament_match_service.generate_ffa_round(
        tournament.id, initiator_id=users[0].id
    )

    assert generated.is_err()
    assert (
        generated.unwrap_err()
        == tournament_match_service.FFA_TEAM_LIMIT_BELOW_LOBBY_MIN_ERROR
    )
