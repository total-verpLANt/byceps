"""
tests.integration.services.lan_tournament.test_ffa_dead_end_guard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Round-one generation of a plain FFA single-elimination tournament is
refused when a later round would fall below the minimum lobby size or
would never shrink. Double elimination is not guarded.
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
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
from byceps.util.result import Err
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-ffa-dead-end-guard')

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('ffadeadendguardbrand', 'FFA Dead End Guard Brand')
    return make_party(brand, PARTY_ID, 'LAN Party FFA Dead End Guard')


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'FfaDeadEndPlayer{i}') for i in range(16)]


@pytest.fixture(scope='module')
def admin(make_user):
    return make_user('FfaDeadEndAdmin')


@pytest.fixture
def make_ffa(party, players):
    """Return a plain FFA tournament at REGISTRATION_CLOSED, not generated."""
    created = []

    def _make(*, size, minimum, maximum, cut, double=False):
        mode = (
            EliminationMode.DOUBLE_ELIMINATION
            if double
            else EliminationMode.SINGLE_ELIMINATION
        )
        tournament, _ = tournament_service.create_tournament(
            PARTY_ID,
            f'FFA Dead End {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=GameFormat.FREE_FOR_ALL,
            elimination_mode=mode,
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            max_players=16,
            point_table=[5, 3, 2, 1],
            group_size_min=minimum,
            group_size_max=maximum,
            advancement_count=cut,
        ).unwrap()
        created.append(tournament)
        for user in players[:size]:
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
    return tournament_seeding_service.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=admin.id,
    )


def _board_problems(tournament, admin):
    return list(
        tournament_seeding_service.get_board(
            tournament.id, initiator_id=admin.id
        )
        .unwrap()
        .problems
    )


def _matches(tournament):
    return tournament_repository.get_matches_for_tournament(tournament.id)


def test_a_stalling_plain_ffa_is_refused_before_round_one(make_ffa, admin):
    # 8 -> 6 -> [3, 3]: round two falls below the minimum of 4.
    tournament = make_ffa(size=8, minimum=4, maximum=4, cut=3)

    result = _generate(tournament, admin)

    assert result == Err(tournament_seeding_service.ERR_PROBLEMS)
    assert _board_problems(tournament, admin) == [
        tournament_seeding_service.PROBLEM_FFA_STALLS
    ]
    assert not _matches(tournament)


@pytest.mark.parametrize('explicit_ids', [True, False])
def test_public_generate_ffa_round_is_guarded(make_ffa, players, explicit_ids):
    tournament = make_ffa(size=8, minimum=4, maximum=4, cut=3)
    contestant_ids = None
    if explicit_ids:
        contestant_ids = [
            str(p.id)
            for p in tournament_repository.get_participants_for_tournament(
                tournament.id
            )
        ]

    result = tournament_match_service.generate_ffa_round(
        tournament.id, 0, contestant_ids
    )

    assert result.is_err()
    assert result.unwrap_err() == tournament_match_service.FFA_STALLS_ERROR
    db.session.rollback()
    assert not _matches(tournament)


def test_a_never_shrinking_plain_ffa_is_refused(make_ffa, admin):
    # Max 4, cut 3, no minimum: 8 -> 6 -> 5 -> ... the field never shrinks
    # to a single lobby.
    tournament = make_ffa(size=8, minimum=None, maximum=4, cut=3)

    result = _generate(tournament, admin)

    assert result == Err(tournament_seeding_service.ERR_PROBLEMS)
    assert _board_problems(tournament, admin) == [
        tournament_seeding_service.PROBLEM_FFA_NO_PROGRESS
    ]
    assert not _matches(tournament)


def test_the_same_tournament_generates_after_the_cut_changes(make_ffa, admin):
    tournament = make_ffa(size=8, minimum=4, maximum=4, cut=3)
    assert _generate(tournament, admin).is_err()

    tournament_service.update_tournament(
        tournament.id,
        name=tournament.name,
        max_players=tournament.max_players,
        contestant_type=tournament.contestant_type,
        game_format=tournament.game_format,
        elimination_mode=tournament.elimination_mode,
        point_table=tournament.point_table,
        advancement_count=2,
        group_size_min=4,
        group_size_max=4,
    ).unwrap()

    result = _generate(tournament, admin)

    assert result.is_ok(), result.unwrap_err()
    assert _matches(tournament)


def test_double_elimination_is_not_guarded(make_ffa, admin):
    tournament = make_ffa(size=8, minimum=4, maximum=4, cut=3, double=True)

    result = _generate(tournament, admin)

    assert result.is_ok(), result.unwrap_err()
    assert _matches(tournament)


def test_a_non_stalling_setup_is_unchanged(make_ffa, admin):
    # 16 -> 6 -> final of 6 (one lobby of at most 6).
    tournament = make_ffa(size=16, minimum=4, maximum=6, cut=2)

    result = _generate(tournament, admin)

    assert result.is_ok(), result.unwrap_err()
    assert _matches(tournament)
