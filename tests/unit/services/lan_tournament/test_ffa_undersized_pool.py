"""
tests.unit.services.lan_tournament.test_ffa_undersized_pool
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import (
    tournament_match_service as service,
)
from byceps.services.lan_tournament.models import ContestantType
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.tournament_match_service import (
    _undersized_pool,
    ffa_lobby_byes,
    FfaAdvancePlan,
    is_lone_losers_round,
    LobbyBye,
    UndersizedPool,
)


def _tournament(minimum, maximum):
    return SimpleNamespace(group_size_min=minimum, group_size_max=maximum)


# fmt: off
@pytest.mark.parametrize(
    ('minimum', 'maximum', 'count', 'removed', 'expected_sizes'),
    [
        # one removal leaves 3 of a lobby of 4
        (4, 4, 3, 1, (3,)),
        # two lobbies of 4 become 3 + 4
        (4, 4, 7, 1, (3, 4)),
        # no removal: the minimum stays
        (4, 4, 3, 0, None),
        # the pool was too small before the removal as well
        (4, 4, 2, 1, None),
        # no shortfall at all
        (4, 4, 4, 1, None),
        (2, 4, 3, 1, None),
        # a lone contestant never makes a lobby
        (2, 4, 1, 1, None),
    ],
)
# fmt: on
def test_undersized_pool_needs_a_removal_that_explains_the_shortfall(
    minimum, maximum, count, removed, expected_sizes
):
    found = _undersized_pool(
        _tournament(minimum, maximum),
        Bracket.LOSERS,
        1,
        count,
        lambda: removed,
    )

    if expected_sizes is None:
        assert found is None
    else:
        assert found == UndersizedPool(
            Bracket.LOSERS, 1, count, expected_sizes, minimum
        )


def test_removals_are_only_looked_up_when_the_pool_is_short():
    def removed():
        raise AssertionError('looked up without a shortfall')

    assert _undersized_pool(_tournament(4, 4), None, 1, 4, removed) is None


def _plan(pool, survivors, lb_pool=(), lb_round_number=None):
    return FfaAdvancePlan(
        pool=pool,
        round_number=1,
        survivors=tuple(survivors),
        bands={},
        grand_final_eligible=False,
        lb_pool=tuple(lb_pool),
        lb_round_number=lb_round_number,
    )


# fmt: off
@pytest.mark.parametrize(
    ('lb_pool', 'expected'),
    [
        (('a',), (LobbyBye(Bracket.LOSERS, 2, 'a'),)),
        (('a', 'b'), ()),
        ((), ()),
    ],
)
# fmt: on
def test_a_losers_pool_of_one_is_a_bye(lb_pool, expected):
    plan = _plan(Bracket.WINNERS, 'wxyz', lb_pool, lb_round_number=2)

    assert ffa_lobby_byes(plan) == expected


def test_a_losers_pool_without_a_round_number_has_no_bye():
    assert ffa_lobby_byes(_plan(Bracket.WINNERS, 'wxyz', ('a',))) == ()


# fmt: off
@pytest.mark.parametrize(
    ('pool', 'survivors', 'expected'),
    [
        (Bracket.LOSERS, 'a', True),
        (Bracket.LOSERS, 'ab', False),
        (Bracket.WINNERS, 'a', False),
        (None, 'a', False),
    ],
)
# fmt: on
def test_only_a_losers_round_of_one_is_lone(pool, survivors, expected):
    assert is_lone_losers_round(_plan(pool, survivors)) is expected


def test_a_bye_is_no_undersized_pool():
    found = _undersized_pool(
        _tournament(2, 4), Bracket.LOSERS, 1, 1, lambda: 1
    )

    assert found is None


def test_the_generator_refuses_a_lone_lobby_even_when_undersized_is_allowed(
    monkeypatch,
):
    tournament = SimpleNamespace(
        id='t',
        game_format=GameFormat.FREE_FOR_ALL,
        contestant_type=ContestantType.SOLO,
        group_size_min=2,
        group_size_max=2,
    )
    repository = Mock()
    repository.get_tournament.return_value = tournament
    monkeypatch.setattr(service, 'tournament_repository', repository)

    result = service._generate_ffa_round_impl(
        't',
        1,
        ['a', 'b', 'c'],
        groups=[['a'], ['b', 'c']],
        allow_undersized=True,
    )

    assert result.is_err()
    assert result.unwrap_err() == service.FFA_LOBBY_BELOW_MINIMUM_ERROR
    repository.create_match.assert_not_called()
