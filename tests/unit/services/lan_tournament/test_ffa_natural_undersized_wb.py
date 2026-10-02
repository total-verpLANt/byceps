"""Limit natural shortfalls to advancing WB pools with at least two entrants."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as service,
    tournament_seeding_service as seeding,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.util.result import Ok


# fmt: off
@pytest.mark.parametrize(('pool', 'rnd', 'count', 'removed', 'natural', 'allowed'), [
    (Bracket.WINNERS, 2, 2, 0, True, True),
    (Bracket.WINNERS, 2, 1, 0, False, False),
    (Bracket.WINNERS, 0, 2, 0, False, False),
    (Bracket.LOSERS, 2, 2, 0, False, False),
    (None, 2, 2, 0, True, True),
    (None, 2, 5, 0, False, False),
    (Bracket.WINNERS, 2, 3, 0, False, False),
    (Bracket.WINNERS, 2, 5, 0, True, True),
    (Bracket.WINNERS, 2, 2, 1, False, True),
    (Bracket.LOSERS, 2, 2, 1, False, True),
])
# fmt: on
def test_natural_shortfall_scope(pool, rnd, count, removed, natural, allowed):
    tournament = SimpleNamespace(group_size_min=3, group_size_max=4)
    lookup = Mock(return_value=removed)
    short = service._undersized_pool(tournament, pool, rnd, count, lookup)
    assert (short is not None) is allowed
    if short:
        assert short.natural_shortfall is natural
        assert short.minimum == 3
        assert short.count == count


# fmt: off
@pytest.mark.parametrize(('pool', 'rnd', 'sizes', 'removed', 'natural'), [
    (Bracket.WINNERS, 2, (2,), 0, True),
    (Bracket.WINNERS, 2, (1,), 0, False),
    (Bracket.WINNERS, 0, (2,), 0, False),
    (Bracket.LOSERS, 2, (2,), 0, False),
    (None, 2, (2,), 0, True),
    (None, 2, (2, 2), 0, False),
    (Bracket.WINNERS, 2, (2, 3), 0, True),
    (Bracket.WINNERS, 2, (1, 2), 0, False),
    (Bracket.WINNERS, 2, (2,), 1, False),
])
# fmt: on
def test_generated_notice_preserves_natural_shortfall_scope(
    pool, rnd, sizes, removed, natural, monkeypatch
):
    tournament = SimpleNamespace(group_size_min=3, group_size_max=4)
    lobbies = [
        SimpleNamespace(id=i, bracket=pool, round=rnd, group_order=i)
        for i in range(len(sizes))
    ]
    repository = Mock()
    repository.get_contestants_for_matches.return_value = {
        i: [object()] * size for i, size in enumerate(sizes)
    }
    monkeypatch.setattr(seeding, 'tournament_repository', repository)
    ids = frozenset(str(i) for i in range(removed))
    monkeypatch.setattr(
        service, 'removed_in_race', lambda t: service.RemovedInRace(ids, ids)
    )
    (short,) = seeding._generated_undersized_pools(tournament, lobbies)
    assert short.natural_shortfall is natural
    assert short.lobbies == sizes


@pytest.mark.parametrize(('minimum', 'count'), [(3, 2), (4, 3), (5, 4)])
def test_natural_shortfalls_use_configured_minimum(minimum, count):
    tournament = SimpleNamespace(group_size_min=minimum, group_size_max=6)
    short = service._undersized_pool(
        tournament, Bracket.WINNERS, 2, count, lambda: 0
    )
    assert short is not None and short.natural_shortfall
    assert short.minimum == minimum
    assert short.lobbies == (count,)


def test_natural_shortfall_never_permits_a_one_player_lobby():
    tournament = SimpleNamespace(group_size_min=3, group_size_max=2)
    assert service._undersized_pool(
        tournament, Bracket.WINNERS, 2, 3, lambda: 0
    ) is None


def test_natural_shortfall_does_not_authorize_singleton_draft_groups(monkeypatch):
    tournament = SimpleNamespace(id='t', group_size_min=3, group_size_max=4)
    plan = service.FfaAdvancePlan(Bracket.WINNERS, 2, ('a', 'b'), {}, False)
    monkeypatch.setattr(
        service,
        'removed_in_race',
        lambda t: service.RemovedInRace(frozenset(), frozenset()),
    )
    generate = Mock(return_value=Ok(2))
    audit = Mock()
    monkeypatch.setattr(service, '_generate_ffa_round_impl', generate)
    monkeypatch.setattr(service, 'create_log_entry', audit)
    result = service._create_planned_ffa_rounds(
        tournament, plan, groups=[['a'], ['b']]
    )
    assert result.is_err()
    assert result.unwrap_err() == service.FFA_LOBBY_BELOW_MINIMUM_ERROR
    generate.assert_not_called()
    audit.assert_not_called()


# fmt: off
@pytest.mark.parametrize(('pool', 'rnd', 'count', 'removed', 'natural', 'allowed'), [
    (Bracket.WINNERS, 2, 2, 1, True, True),
    (Bracket.WINNERS, 2, 2, 2, False, True),
    (Bracket.WINNERS, 2, 3, 1, False, True),
    (None, 2, 3, 1, False, True),
    (None, 2, 2, 1, True, True),
])
# fmt: on
def test_a_pool_short_of_the_minimum_stays_natural_with_its_removals_back(
    pool, rnd, count, removed, natural, allowed
):
    tournament = SimpleNamespace(group_size_min=4, group_size_max=4)
    short = service._undersized_pool(
        tournament, pool, rnd, count, lambda: removed
    )
    assert (short is not None) is allowed
    if short:
        assert short.natural_shortfall is natural


def test_a_removal_shortfall_never_permits_a_one_player_lobby():
    tournament = SimpleNamespace(group_size_min=2, group_size_max=2)
    assert service._undersized_pool(
        tournament, None, 1, 3, lambda: 1
    ) is None


def test_a_single_track_round_of_two_lobbies_is_no_final():
    tournament = SimpleNamespace(group_size_min=4, group_size_max=4)
    assert service._undersized_pool(
        tournament, None, 1, 6, lambda: 0
    ) is None
