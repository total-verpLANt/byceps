"""
tests.integration.services.lan_tournament.test_ffa_cut_required
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The service refuses an FFA phase that needs a cut and has none, but
never blocks an unrelated edit of a locked legacy row.
"""

import dataclasses

import pytest

from byceps.services.lan_tournament import (
    tournament_domain_service,
    tournament_repository,
    tournament_service,
)
from byceps.services.lan_tournament.models import (
    ContestantType,
    EliminationMode,
    GameFormat,
    TournamentStatus,
)
from byceps.services.lan_tournament.models.playoff import PlayoffReleaseMode
from byceps.services.lan_tournament.models.score_ordering import ScoreOrdering
from byceps.services.party.models import PartyID
from byceps.util.uuid import generate_uuid7


PARTY_ID = PartyID('lan-party-ffa-cut-required')

CUT_REQUIRED = tournament_domain_service.FFA_CUT_REQUIRED_MSGID


@pytest.fixture(scope='module')
def party(make_party, brand):
    return make_party(brand, PARTY_ID, 'FFA Cut Required Party')


def _ffa_kwargs(**overrides):
    kwargs = dict(
        contestant_type=ContestantType.SOLO,
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        point_table=[3, 2, 1],
        group_size_min=3,
        group_size_max=4,
    )
    kwargs.update(overrides)
    return kwargs


def _create(party, **overrides):
    return tournament_service.create_tournament(
        party.id,
        f'FFA Cut Required {generate_uuid7()}',
        **_ffa_kwargs(**overrides),
    )


def _create_ok(party, **overrides):
    tournament, _ = _create(party, **overrides).unwrap()
    return tournament


def _update(tournament, **overrides):
    """Save the stored tournament again, changing only `overrides`."""
    fields = {
        name: getattr(tournament, name)
        for name in (
            'name',
            'game',
            'description',
            'image_url',
            'ruleset',
            'start_time',
            'min_players',
            'max_players',
            'min_teams',
            'max_teams',
            'min_players_in_team',
            'max_players_in_team',
            'contestant_type',
            'game_format',
            'elimination_mode',
            'score_ordering',
            'point_table',
            'advancement_count',
            'group_size_min',
            'group_size_max',
            'points_carry_to_losers',
        )
    }
    fields.update(overrides)
    return tournament_service.update_tournament(tournament.id, **fields)


def _clear_cut_in_storage(tournament):
    """Store an empty cut the way a legacy row has one."""
    tournament_repository.update_tournament(
        dataclasses.replace(tournament, advancement_count=None)
    )
    tournament_repository.commit_session()


def test_service_refuses_a_plain_ffa_without_a_needed_cut(party):
    before = len(tournament_repository.get_tournaments_for_party(party.id))

    result = _create(party)

    assert result.is_err()
    assert result.unwrap_err() == CUT_REQUIRED
    after = len(tournament_repository.get_tournaments_for_party(party.id))
    assert after == before


def test_service_refuses_highscore_playoffs_without_a_needed_cut(party):
    result = _create(
        party,
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
        score_ordering=ScoreOrdering.HIGHER_IS_BETTER,
        playoff_game_format=GameFormat.FREE_FOR_ALL,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        playoff_qualifier_count=8,
        playoff_release_mode=PlayoffReleaseMode.AUTOMATIC,
    )

    assert result.is_err()
    assert result.unwrap_err() == CUT_REQUIRED


def test_single_lobby_ffa_needs_no_cut(party):
    result = _create(party, max_players=4)

    assert result.is_ok()


def test_update_cannot_clear_a_needed_cut(party):
    tournament = _create_ok(
        party,
        advancement_count=2,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    result = _update(tournament, advancement_count=None)

    assert result.is_err()
    assert result.unwrap_err() == CUT_REQUIRED
    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.advancement_count == 2


def test_update_of_an_editable_legacy_row_requires_the_cut(party):
    tournament = _create_ok(
        party,
        advancement_count=2,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    _clear_cut_in_storage(tournament)
    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.advancement_count is None

    refused = _update(stored, description='Edited')
    assert refused.is_err()
    assert refused.unwrap_err() == CUT_REQUIRED

    accepted = _update(stored, description='Edited', advancement_count=2)
    assert accepted.is_ok()


def test_update_of_a_locked_legacy_row_is_not_blocked(party):
    tournament = _create_ok(
        party,
        advancement_count=2,
        tournament_status=TournamentStatus.ONGOING,
    )
    _clear_cut_in_storage(tournament)
    stored = tournament_repository.get_tournament(tournament.id)
    assert stored.advancement_count is None

    result = _update(stored, description='Edited')

    assert result.is_ok()
