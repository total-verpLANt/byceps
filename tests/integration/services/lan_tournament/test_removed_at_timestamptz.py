"""
tests.integration.services.lan_tournament.test_removed_at_timestamptz
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

`removed_at` is TIMESTAMPTZ in production while match `created_at` is a
plain TIMESTAMP. The test schema must use the production type, and the
count of contestants removed after generation must not compare the two
in Python.
"""

from datetime import datetime, UTC

import pytest
from sqlalchemy import text

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
    tournament_score_service,
    tournament_seeding_service,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
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

from tests.integration.services.lan_tournament.test_ffa_undersized_pool import (
    _first_round,
    _generate_from_draft,
    _lobbies,
    _members,
    _play,
    _remove,
)


PARTY_ID = PartyID('lan-party-removed-at-timestamptz')

KINDS = ['plain_ffa', 'highscore_ffa']


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('removedattzbrand', 'Removed At Brand')
    return make_party(brand, PARTY_ID, 'LAN Party Removed At')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'RemovedAtPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('RemovedAtAdmin')


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


@pytest.fixture
def make_de(party, players, admin):
    """Return a started FFA DE tournament; the two WB lobbies are open."""
    created = []

    def _make(kind):
        common = {
            'contestant_type': ContestantType.SOLO,
            'point_table': [5, 3, 2, 1],
            'group_size_min': 4,
            'group_size_max': 4,
            'advancement_count': 2,
        }
        if kind == 'plain_ffa':
            args = {
                **common,
                'game_format': GameFormat.FREE_FOR_ALL,
                'elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'tournament_status': TournamentStatus.REGISTRATION_CLOSED,
                'max_players': 8,
            }
        else:
            args = {
                **common,
                'game_format': GameFormat.HIGHSCORE,
                'elimination_mode': EliminationMode.NONE,
                'score_ordering': ScoreOrdering.HIGHER_IS_BETTER,
                'tournament_status': TournamentStatus.ONGOING,
                'playoff_game_format': GameFormat.FREE_FOR_ALL,
                'playoff_elimination_mode': EliminationMode.DOUBLE_ELIMINATION,
                'playoff_qualifier_count': 8,
                'playoff_release_mode': PlayoffReleaseMode.AUTOMATIC,
            }
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID, f'Removed At {kind} {len(created)}', **args
        ).unwrap()
        created.append(tournament)
        _add_participants(tournament, players)
        if kind == 'plain_ffa':
            board = tournament_seeding_service.get_board(
                tournament.id, initiator_id=admin.id
            ).unwrap()
            tournament_seeding_service.generate_from_seeding(
                tournament.id,
                expected_version=board.version,
                initiator_id=admin.id,
            ).unwrap()
            tournament_service.change_status(
                tournament.id, TournamentStatus.ONGOING, admin.id
            ).unwrap()
        else:
            participants = (
                tournament_repository.get_participants_for_tournament(
                    tournament.id
                )
            )
            for value, participant in enumerate(participants, start=1):
                tournament_score_service.submit_score(
                    tournament.id, value * 10, participant_id=participant.id
                ).unwrap()
            tournament_score_service.close_leaderboard(
                tournament.id, initiator_id=admin.id
            ).unwrap()
        assert len(_lobbies(tournament)) == 2
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


@pytest.mark.parametrize(
    'table', ['lan_tournament_participants', 'lan_tournament_teams']
)
def test_removed_at_has_the_prod_column_type(party, table):  # noqa: F811
    data_type = db.session.execute(
        text(
            'SELECT data_type FROM information_schema.columns'
            ' WHERE table_name = :table AND column_name = :column'
        ),
        {'table': table, 'column': 'removed_at'},
    ).scalar_one()

    assert data_type == 'timestamp with time zone'


def _in_race(tournament):
    removed = tournament_match_service.removed_in_race(tournament)
    return removed.count_for(Bracket.WINNERS)


@pytest.mark.parametrize('kind', KINDS)
def test_a_removal_after_generation_is_counted(
    make_de,
    admin,
    kind,  # noqa: F811
):
    tournament = make_de(kind)
    lobbies = _first_round(tournament)
    victim = sorted(_members(lobbies[0]))[0]
    for lobby in lobbies:
        _play(lobby, admin)
    assert _in_race(tournament) == 0

    _remove(tournament, victim, admin)

    assert _in_race(tournament) == 1
    _target, _board, generated = _generate_from_draft(tournament, admin)
    assert generated.is_ok()


def test_a_removal_before_the_phase_is_not_counted(
    party,
    players,
    admin,  # noqa: F811
):
    tournament, _ = tournament_service.create_tournament(
        PARTY_ID,
        'Removed Before Release',
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        tournament_status=TournamentStatus.ONGOING,
        point_table=[5, 3, 2, 1],
        group_size_min=2,
        group_size_max=4,
        advancement_count=1,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.DOUBLE_ELIMINATION,
        playoff_qualifier_count=4,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
    ).unwrap()
    try:
        _add_participants(tournament, players)
        participants = tournament_repository.get_participants_for_tournament(
            tournament.id
        )
        leaver = participants[0]
        _remove(tournament, leaver.id, admin)
        for value, participant in enumerate(participants[1:], start=1):
            tournament_score_service.submit_score(
                tournament.id, value * 10, participant_id=participant.id
            ).unwrap()
        tournament_score_service.close_leaderboard(
            tournament.id, initiator_id=admin.id
        ).unwrap()

        assert tournament_repository.get_matches_for_tournament(tournament.id)
        assert _in_race(tournament) == 0
    finally:
        db.session.rollback()
        tournament_service.delete_tournament(tournament.id)
