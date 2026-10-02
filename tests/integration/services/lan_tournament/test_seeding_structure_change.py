"""
tests.integration.services.lan_tournament.test_seeding_structure_change
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from itertools import count

import pytest

from byceps.database import db
from byceps.services.lan_tournament import (
    tournament_match_service,
    tournament_repository,
    tournament_seeding_repository,
    tournament_seeding_service as svc,
    tournament_service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.seeding import SeedingFormat
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-2026-seeding-structure')

SE = (GameFormat.ONE_V_ONE, EliminationMode.SINGLE_ELIMINATION)
RR = (GameFormat.ONE_V_ONE, EliminationMode.ROUND_ROBIN)
FFA = (GameFormat.FREE_FOR_ALL, EliminationMode.SINGLE_ELIMINATION)
FFA_KWARGS = {
    'max_players': 8,
    'group_size_min': 2,
    'group_size_max': 4,
    'advancement_count': 2,
    'point_table': [10, 6, 3, 1],
}
RR_KWARGS = {
    'playoff_game_format': GameFormat.ONE_V_ONE,
    'playoff_elimination_mode': EliminationMode.SINGLE_ELIMINATION,
    'playoff_group_count': 2,
    'playoff_qualifiers_per_group': 2,
    'playoff_release_mode': PlayoffReleaseMode.MANUAL,
}

_counter = count(1)


@pytest.fixture(scope='module')
def party(make_party, make_brand):
    brand = make_brand('seedingstructurebrand', 'Seeding Structure Brand')
    return make_party(brand, PARTY_ID, 'LAN Party 2026 Seeding Structure')


@pytest.fixture(scope='module')
def users(make_user):
    return [make_user(f'SeedingStructureUser{i}') for i in range(8)]


@pytest.fixture
def make_tournament(party, users):
    created = []

    def _make(mode=SE, **kwargs):
        result = tournament_service.create_tournament(
            PARTY_ID,
            f'Seeding Structure Tournament {next(_counter)}',
            contestant_type=ContestantType.SOLO,
            game_format=mode[0],
            elimination_mode=mode[1],
            tournament_status=TournamentStatus.REGISTRATION_CLOSED,
            **kwargs,
        )
        assert result.is_ok(), result.unwrap_err()
        tournament, _ = result.unwrap()
        created.append(tournament)
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
        return tournament

    yield _make
    db.session.rollback()
    for tournament in created:
        if tournament_repository.find_tournament(tournament.id) is not None:
            tournament_service.delete_tournament(tournament.id)


def _update(tournament, **overrides):
    current = tournament_repository.get_tournament(tournament.id)
    kwargs = {
        field: getattr(current, field)
        for field in (
            'name',
            'contestant_type',
            'game_format',
            'elimination_mode',
            'max_players',
            'group_size_min',
            'group_size_max',
            'advancement_count',
            'point_table',
            'playoff_game_format',
            'playoff_elimination_mode',
            'playoff_group_count',
            'playoff_qualifiers_per_group',
            'playoff_release_mode',
        )
    }
    kwargs.update(overrides)
    result = tournament_service.update_tournament(tournament.id, **kwargs)
    assert result.is_ok(), result.unwrap_err()
    db.session.commit()


def _board(tournament):
    db.session.rollback()
    return svc.get_board(tournament.id).unwrap()


def _reseed(tournament, board, admin):
    result = svc.apply_action(
        tournament.id,
        'initial',
        svc.ReseedKeepTiers(),
        expected_version=board.version,
        initiator_id=admin.id,
    )
    assert result.is_ok(), result.unwrap_err()
    return result.unwrap()


def _generate(tournament, board, admin):
    return svc.generate_from_seeding(
        tournament.id,
        expected_version=board.version,
        initiator_id=admin.id,
    )


def _brackets(tournament):
    db.session.rollback()
    return {
        m.bracket
        for m in tournament_match_service.get_matches_for_tournament(
            tournament.id
        )
    }


def _match_count(tournament):
    db.session.rollback()
    return len(
        tournament_match_service.get_matches_for_tournament(tournament.id)
    )


def _group_count(tournament):
    db.session.rollback()
    matches = tournament_match_service.get_matches_for_tournament(tournament.id)
    return len({m.group_order for m in matches})


def _generated_code(tournament):
    db.session.rollback()
    return tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    ).generated_seed_code


def _start(tournament, admin):
    return tournament_service.change_status(
        tournament.id, TournamentStatus.ONGOING, admin.id
    )


def test_se_to_de_after_the_first_open_draws_and_builds_de(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(SE)
    assert _board(tournament).state.format is SeedingFormat.SINGLE_ELIMINATION
    _update(tournament, elimination_mode=EliminationMode.DOUBLE_ELIMINATION)

    stale = _board(tournament)

    assert stale.stale
    assert stale.stale_structure
    assert stale.state.format is SeedingFormat.DOUBLE_ELIMINATION
    assert _generate(tournament, stale, admin).unwrap_err() == (
        svc.ERR_STRUCTURE_CHANGED
    )
    assert _match_count(tournament) == 0

    fresh = _reseed(tournament, stale, admin)
    assert not fresh.stale

    assert _generate(tournament, fresh, admin).is_ok()
    assert Bracket.LOSERS in _brackets(tournament)
    assert _start(tournament, admin).is_ok()


def test_rr_group_count_change_draws_and_builds_the_new_groups(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(RR, **RR_KWARGS)
    assert _board(tournament).state.param == 2
    _update(tournament, playoff_group_count=4, playoff_qualifiers_per_group=1)

    stale = _board(tournament)

    assert stale.stale_structure
    assert (stale.state.format, stale.state.param) == (
        SeedingFormat.ROUND_ROBIN,
        4,
    )
    assert _generate(tournament, stale, admin).unwrap_err() == (
        svc.ERR_STRUCTURE_CHANGED
    )
    assert _match_count(tournament) == 0

    fresh = _reseed(tournament, stale, admin)
    assert _generate(tournament, fresh, admin).is_ok()
    assert _group_count(tournament) == 4
    assert _start(tournament, admin).is_ok()


def test_ffa_lobby_size_change_draws_and_builds_the_new_lobbies(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(FFA, **FFA_KWARGS)
    assert _board(tournament).state.param == 4
    _update(tournament, group_size_max=2, advancement_count=1)

    stale = _board(tournament)

    assert stale.stale_structure
    assert (stale.state.format, stale.state.param) == (
        SeedingFormat.FREE_FOR_ALL,
        2,
    )
    assert _generate(tournament, stale, admin).unwrap_err() == (
        svc.ERR_STRUCTURE_CHANGED
    )
    assert _match_count(tournament) == 0

    fresh = _reseed(tournament, stale, admin)
    assert _generate(tournament, fresh, admin).is_ok()
    assert _match_count(tournament) == 4  # 8 players in lobbies of 2
    assert _start(tournament, admin).is_ok()


def test_generating_an_old_draft_writes_nothing(make_tournament, users):
    admin = users[0]
    tournament = make_tournament(RR, **RR_KWARGS)
    board = _board(tournament)
    _update(tournament, playoff_group_count=4, playoff_qualifiers_per_group=1)

    result = _generate(tournament, board, admin)

    assert result.unwrap_err() == svc.ERR_STRUCTURE_CHANGED
    assert _match_count(tournament) == 0
    assert _generated_code(tournament) is None
    stored = tournament_seeding_repository.find_seeding(
        tournament.id, 'initial'
    )
    assert stored.version == board.version


def test_change_after_generation_refuses_the_start_until_regenerated(
    make_tournament, users
):
    admin = users[0]
    tournament = make_tournament(RR, **RR_KWARGS)
    assert _generate(tournament, _board(tournament), admin).is_ok()
    _update(tournament, playoff_group_count=4, playoff_qualifiers_per_group=1)

    refused = _start(tournament, admin)

    assert refused.unwrap_err() == (svc.ERR_STRUCTURE_CHANGED_AFTER_GENERATION)
    db.session.rollback()
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        is TournamentStatus.REGISTRATION_CLOSED
    )

    stale = _board(tournament)
    assert stale.stale_structure
    fresh = _reseed(tournament, stale, admin)
    assert _generate(tournament, fresh, admin).is_ok()

    assert _group_count(tournament) == 4
    assert _start(tournament, admin).is_ok()


def test_se_to_de_after_generation_refuses_the_start(make_tournament, users):
    admin = users[0]
    tournament = make_tournament(SE)
    assert _generate(tournament, _board(tournament), admin).is_ok()
    _update(tournament, elimination_mode=EliminationMode.DOUBLE_ELIMINATION)

    refused = _start(tournament, admin)

    assert refused.unwrap_err() == (svc.ERR_STRUCTURE_CHANGED_AFTER_GENERATION)
    db.session.rollback()
    assert (
        tournament_repository.get_tournament(tournament.id).tournament_status
        is TournamentStatus.REGISTRATION_CLOSED
    )
