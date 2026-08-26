"""
tests.unit.services.lan_tournament.test_site_match_overview_filter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the readiness buckets the site match views count and filter by.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import combinations
from uuid import uuid4

import pytest

from byceps.services.lan_tournament.blueprints.site.views import (
    _site_match_quantities,
)
from byceps.services.lan_tournament.lan_tournament_view_helpers import (
    filter_match_projections,
    match_filter_options,
)
from byceps.services.lan_tournament.models.game_format import GameFormat

from tests.unit.services.lan_tournament.test_readiness_surface_policy import (
    _project,
    _row,
    _tournament,
)


BUCKETS = (
    'waiting',
    'not_ready',
    'partially_ready',
    'both_ready',
    'no_readiness',
    'finished',
)


def _entries(tournament, *specs):
    """Build `match_data` entries from `(count, claims, confirmed)` specs."""
    rows = []
    for count, claims, confirmed in specs:
        match, contestants, pairing = _row(
            tournament, count=count, claims=claims
        )
        if confirmed:
            match = replace(match, confirmed_by=uuid4())
        rows.append((match, contestants, pairing))
    projections = _project(tournament, rows)
    return [
        {'match': m, 'contestants': c, 'readiness': projections[m.id]}
        for m, c, _ in rows
    ]


# waiting x2, not ready, partially ready, both ready, finished x2
# fmt: off
SOLO_SPECS = (
    (0, 0, False), (1, 0, False), (2, 0, False), (2, 1, False),
    (2, 2, False), (2, 2, True), (1, 0, True),
)
# fmt: on


def _projections(entries):
    return [entry['readiness'] for entry in entries]


def test_quantities_count_every_row_in_exactly_one_bucket():
    entries = _entries(_tournament(), *SOLO_SPECS)
    quantities = _site_match_quantities(entries)
    assert quantities == dict(
        all=7,
        waiting=2,
        not_ready=1,
        partially_ready=1,
        both_ready=1,
        no_readiness=0,
        finished=2,
    )
    assert sum(quantities[bucket] for bucket in BUCKETS) == quantities['all']


def test_quantities_have_no_personal_or_umbrella_keys():
    quantities = _site_match_quantities(_entries(_tournament(), *SOLO_SPECS))
    assert set(quantities) == {'all', *BUCKETS}
    assert not {'ready', 'playable', 'open'} & set(quantities)


def test_filter_buckets_are_disjoint_and_cover_all_rows():
    entries = _entries(_tournament(), *SOLO_SPECS)
    quantities = _site_match_quantities(entries)
    selected = {
        bucket: {
            p.match_id
            for p in filter_match_projections(
                _projections(entries), only=bucket
            )
        }
        for bucket in BUCKETS
    }
    for first, second in combinations(BUCKETS, 2):
        assert not selected[first] & selected[second]
    assert set().union(*selected.values()) == {e['match'].id for e in entries}
    assert {bucket: len(ids) for bucket, ids in selected.items()} == {
        bucket: quantities[bucket] for bucket in BUCKETS
    }


def test_unsupported_format_rows_count_in_their_own_bucket():
    tournament = _tournament(game_format=GameFormat.FREE_FOR_ALL)
    entries = _entries(
        tournament, (2, 0, False), (2, 0, False), (1, 0, False), (2, 0, True)
    )
    quantities = _site_match_quantities(entries)
    assert quantities['no_readiness'] == 2
    assert quantities['waiting'] == 1 and quantities['finished'] == 1
    assert quantities['not_ready'] == quantities['both_ready'] == 0
    keys = [key for key, _, _ in match_filter_options(quantities)]
    assert keys == [
        'waiting',
        'not_ready',
        'partially_ready',
        'both_ready',
        'no_readiness',
        'finished',
        'all',
    ]


@pytest.mark.parametrize('specs', [SOLO_SPECS, ((2, 0, False),)])
def test_bucket_without_rows_is_not_offered_for_supported_formats(specs):
    quantities = _site_match_quantities(_entries(_tournament(), *specs))
    keys = [key for key, _, _ in match_filter_options(quantities)]
    assert 'no_readiness' not in keys
    assert keys[-1] == 'all'
