"""Count only the removed contestants who were still in the race."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from byceps.services.lan_tournament import tournament_match_service as service
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.models.game_format import GameFormat


@pytest.fixture
def world(monkeypatch):
    """Patch the repositories; return a `run(...)` helper for one case."""
    repository = Mock()
    seedings = Mock()
    monkeypatch.setattr(service, 'tournament_repository', repository)
    monkeypatch.setattr(service, 'tournament_seeding_repository', seedings)

    def run(
        lobbies,
        removed,
        *,
        cut=1,
        blocks=(),
        snapshot=None,
        brackets=None,
    ):
        """`lobbies`: [(bracket, confirmed, {contestant: points})]."""
        tournament = SimpleNamespace(
            id='t',
            game_format=GameFormat.FREE_FOR_ALL,
            contestant_type=None,
            advancement_count=cut,
        )
        matches, entries = [], {}
        for index, (bracket, confirmed, points) in enumerate(lobbies):
            match = SimpleNamespace(
                id=f'm{index}',
                tournament_id='t',
                phase=1,
                bracket=bracket,
                round=index,
                group_order=0,
                confirmed_by='orga' if confirmed else None,
                seeding_target='initial' if index == 0 else None,
            )
            matches.append(match)
            entries[match.id] = [
                SimpleNamespace(
                    participant_id=cid,
                    team_id=None,
                    points=value,
                    placement=None,
                )
                for cid, value in points.items()
            ]
        repository.get_contestant_ids_removed_since_phase_start.return_value = (
            frozenset(removed)
        )
        repository.get_matches_for_tournament.return_value = matches
        repository.get_contestants_for_matches.return_value = entries
        if snapshot is None:
            seedings.find_seeding.return_value = None
        else:
            seedings.find_seeding.return_value = SimpleNamespace(
                generated_seed_code='code',
                roster_snapshot=tuple(
                    SimpleNamespace(id=cid) for cid in snapshot
                ),
            )
        monkeypatch.setattr(
            service,
            'ffa_decisions',
            lambda tournament_id: (
                {service.ffa_lobby_scope(matches[0]): tuple(blocks)}
                if blocks
                else {}
            ),
        )
        return service.removed_in_race(tournament)

    return run


SE, WB, LB, GF = None, Bracket.WINNERS, Bracket.LOSERS, Bracket.GRAND_FINAL
TIED = {'a': 5, 'b': 5, 'c': 5, 'd': 0}
BLOCK = (('a', 'b', 'c'),)


# fmt: off
@pytest.mark.parametrize(
    ('bracket', 'confirmed', 'points', 'cut', 'blocks', 'in_winners', 'in_racing'),
    [
        # unconfirmed: the entry carries no result yet
        (SE, False, {'x': 0, 'y': 5}, 1, (), True,  True),
        # confirmed: the leaver won or lost the cut
        (SE, True,  {'x': 5, 'y': 0}, 1, (), True,  True),
        (SE, True,  {'x': 0, 'y': 5}, 1, (), False, False),
        (WB, True,  {'x': 5, 'y': 0}, 1, (), True,  True),
        (WB, True,  {'x': 0, 'y': 5}, 1, (), False, True),
        (LB, True,  {'x': 0, 'y': 5}, 1, (), False, False),
        (LB, True,  {'x': 5, 'y': 0}, 1, (), False, True),
        (GF, True,  {'x': 5, 'y': 0}, 1, (), False, False),
    ],
)
# fmt: on
def test_removed_in_race(
    world, bracket, confirmed, points, cut, blocks, in_winners, in_racing
):
    result = world([(bracket, confirmed, points)], {'x'}, cut=cut, blocks=blocks)
    assert ('x' in result.winners) is in_winners
    assert ('x' in result.racing) is in_racing


# fmt: off
@pytest.mark.parametrize(
    ('bracket', 'blocks', 'in_winners', 'in_racing'),
    [
        # a decided tie eliminates a leaver with as many points as a survivor
        (SE, BLOCK, False, False),
        (WB, BLOCK, False, True),
        (LB, BLOCK, False, False),
        # an undecided tie across the cut never cut the leaver
        (SE, (),    True,  True),
        (WB, (),    True,  True),
        (LB, (),    False, True),
    ],
)
# fmt: on
def test_a_decided_cut_tie_eliminates_a_leaver_with_equal_points(
    world, bracket, blocks, in_winners, in_racing
):
    result = world([(bracket, True, TIED)], {'c'}, cut=2, blocks=blocks)
    assert ('c' in result.winners) is in_winners
    assert ('c' in result.racing) is in_racing


def test_a_losers_entry_takes_the_leaver_out_of_the_winners(world):
    result = world(
        [(WB, True, {'x': 5, 'y': 0}), (LB, False, {'x': 0, 'z': 0})], {'x'}
    )
    assert 'x' not in result.winners
    assert 'x' in result.racing


# fmt: off
@pytest.mark.parametrize(
    ('snapshot', 'counted'),
    [
        (None, True),
        (('u', 'v'), True),
        (('v',), False),
    ],
)
# fmt: on
def test_a_leaver_who_never_played_counts_only_if_seeded(
    world, snapshot, counted
):
    result = world([(SE, False, {'v': 0, 'w': 0})], {'u'}, snapshot=snapshot)
    assert ('u' in result.winners) is counted
    assert ('u' in result.racing) is counted


def test_nothing_is_counted_without_removals(world):
    result = world([(SE, True, {'x': 5, 'y': 0})], set())
    assert result == service.RemovedInRace(frozenset(), frozenset())


# fmt: off
@pytest.mark.parametrize(
    ('pool', 'expected'),
    [(WB, 1), (SE, 1), (LB, 2), (GF, 1)],
)
# fmt: on
def test_count_for_reads_the_pool_side(pool, expected):
    removed = service.RemovedInRace(frozenset({'a'}), frozenset({'a', 'b'}))
    assert removed.count_for(pool) == expected
