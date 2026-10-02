from datetime import datetime
from uuid import UUID

import pytest

from byceps.services.lan_tournament.dbmodels.qualification_decision import (
    blocks_from_json,
    blocks_to_json,
)
from byceps.services.lan_tournament.models.qualification_decision import (
    DecisionBlock,
)
from byceps.services.user.models import UserID


ROW_USER = UserID(UUID('00000000-0000-0000-0000-0000000000a1'))
BLOCK_USER = UserID(UUID('00000000-0000-0000-0000-0000000000b2'))
ROW_AT = datetime(2026, 10, 2, 12, 0, 0)
BLOCK_AT = datetime(2026, 10, 1, 8, 30, 15)


def _read(raw):
    return blocks_from_json(
        raw, reason='row reason', decided_by=ROW_USER, decided_at=ROW_AT
    )


def test_blocks_round_trip_with_per_block_meta():
    blocks = (
        DecisionBlock(
            contestant_ids=('a', 'c'),
            reason='first',
            decided_by=BLOCK_USER,
            decided_at=BLOCK_AT,
        ),
        DecisionBlock(
            contestant_ids=('b', 'd'),
            reason='second',
            decided_by=ROW_USER,
            decided_at=ROW_AT,
        ),
    )

    assert _read(blocks_to_json(blocks)) == blocks


def test_legacy_flat_list_reads_as_one_block_with_row_meta():
    (block,) = _read('["c", "a", "b"]')

    assert block == DecisionBlock(
        contestant_ids=('c', 'a', 'b'),
        reason='row reason',
        decided_by=ROW_USER,
        decided_at=ROW_AT,
    )


def test_list_of_lists_reads_with_row_meta():
    blocks = _read('[["a", "c"], ["b", "d"]]')

    assert [b.contestant_ids for b in blocks] == [('a', 'c'), ('b', 'd')]
    assert {b.reason for b in blocks} == {'row reason'}
    assert {b.decided_by for b in blocks} == {ROW_USER}
    assert {b.decided_at for b in blocks} == {ROW_AT}


def test_an_object_without_meta_falls_back_to_the_row():
    (block,) = _read('[{"ids": ["a", "b"]}]')

    assert block.contestant_ids == ('a', 'b')
    assert (block.reason, block.decided_by, block.decided_at) == (
        'row reason',
        ROW_USER,
        ROW_AT,
    )


def test_empty_list_reads_as_no_blocks():
    assert _read('[]') == ()


# fmt: off
@pytest.mark.parametrize(
    'raw',
    [
        'not json',
        '{"ids": ["a", "b"]}',
        '"a"',
        '[1, 2]',
        '[["a", 2]]',
        '[{"reason": "no ids"}]',
        '[{"ids": "ab"}]',
        '[{"ids": ["a", "b"], "decided_by": "not-a-uuid"}]',
        '[{"ids": ["a", "b"], "decided_at": "yesterday"}]',
        '[{"ids": ["a", "b"], "decided_by": 7}]',
        '[["a", "b"], "c"]',
    ],
)
# fmt: on
def test_malformed_blocks_raise(raw):
    with pytest.raises(ValueError, match='Malformed qualification decision'):
        _read(raw)
