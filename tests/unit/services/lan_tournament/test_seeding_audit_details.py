"""
tests.unit.services.lan_tournament.test_seeding_audit_details
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Details cell of the seeding audit rows, the system rows on the
boards, and the release code.
"""

from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)


NAMES = {'a': 'Alice', 'b': 'Bob', 'c': 'Carol', 'd': 'Dave'}
USERS = {'u1': SimpleNamespace(screen_name='Ohrwurm')}
WHEN = datetime(2026, 9, 30, 16, 42, tzinfo=UTC)
CODE = 'ABCDEFGHJKLMNPQR'


@pytest.fixture(autouse=True)
def plain_translations(monkeypatch):
    monkeypatch.setattr(
        helpers,
        'gettext',
        lambda message, **params: message % params if params else message,
    )
    monkeypatch.setattr(
        helpers,
        'ngettext',
        lambda singular, plural, num, **params: (
            (singular if num == 1 else plural) % {**params, 'num': num}
        ),
    )


def _entry(event_type, data, at=WHEN, initiator_id='u1'):
    return SimpleNamespace(
        occurred_at=at,
        event_type=event_type,
        initiator_id=initiator_id,
        data=data,
    )


def _row(event_type, data):
    [row] = helpers.seeding_audit_rows(
        [_entry(event_type, data)], USERS, names=NAMES
    )
    return row


def _swap(fmt, unit_p, unit_q, a='a', b='b', **extra):
    return {
        'target': 'initial',
        'seed_code': CODE,
        'p': 0,
        'q': 2,
        'a': a,
        'b': b,
        'unit_p': unit_p,
        'unit_q': unit_q,
        'format': fmt,
        **extra,
    }


# fmt: off
@pytest.mark.parametrize(
    ('fmt', 'unit_p', 'unit_q', 'expected'),
    [
        ('SINGLE_ELIMINATION', 0, 2, 'M1 Alice ↔ M3 Bob'),
        ('DOUBLE_ELIMINATION', 1, 4, 'M2 Alice ↔ M5 Bob'),
        ('ROUND_ROBIN', 0, 1, 'Group A Alice ↔ Group B Bob'),
        ('FREE_FOR_ALL', 0, 2, 'Lobby 1 Alice ↔ Lobby 3 Bob'),
    ],
)
# fmt: on
def test_swap_details_name_units_and_occupants(
    fmt, unit_p, unit_q, expected
):
    assert _row('seeding-swapped', _swap(fmt, unit_p, unit_q))['details'] == (
        expected
    )


def test_swap_with_a_bye():
    row = _row('seeding-swapped', _swap('SINGLE_ELIMINATION', 0, 1, b=None))

    assert row['details'] == 'M1 Alice ↔ M2 Bye'


def test_tier_move_details():
    moved = _row(
        'seeding-tier-changed',
        {
            'seed_code': CODE,
            'contestant_id': 'c',
            'from_tier': 0,
            'to_tier': 2,
            'place_in_tier': 1,
        },
    )
    reordered = _row(
        'seeding-reordered',
        {
            'seed_code': CODE,
            'contestant_id': 'c',
            'from_tier': 1,
            'to_tier': 1,
            'place_in_tier': 3,
        },
    )

    assert moved['details'] == 'Carol: tier A → C'
    assert reordered['details'] == 'Carol is now seed 3 in tier B'


def test_resize_and_reset_details():
    plain = _row(
        'seeding-tiers-resized',
        {'seed_code': CODE, 'tier_count': 4, 'dropped_fixes': 0},
    )
    dropped = _row(
        'seeding-tiers-resized',
        {'seed_code': CODE, 'tier_count': 3, 'dropped_fixes': 2},
    )
    reset = _row(
        'seeding-fixes-reset', {'seed_code': CODE, 'dropped_fixes': 5}
    )

    assert plain['details'] == '4 tiers'
    assert dropped['details'] == '3 tiers · 2 layout corrections dropped'
    assert reset['details'] == '5 layout corrections dropped'


def test_reseed_details_name_leavers():
    row = _row(
        'seeding-roster-reseeded',
        {'seed_code': CODE, 'leaver_ids': ['b', 'zz'], 'joiner_ids': ['d']},
    )

    assert row['details'] == (
        'Bob, zz removed; tiers and order of the others kept'
    )


def test_generated_details_show_previous_code():
    first = _row(
        'bracket-generated',
        {'target': 'initial', 'seed_code': CODE, 'previous_seed_code': None},
    )
    again = _row(
        'bracket-regenerated',
        {
            'target': 'initial',
            'seed_code': CODE,
            'previous_seed_code': 'ZZZZYYYYXXXXWWWW',
        },
    )

    assert first['details'] == 'From ABCD-EFGH-…'
    assert again['details'] == 'From ABCD-EFGH-… (previously ZZZZ-YYYY-…)'


@pytest.mark.parametrize(
    'event_type',
    [
        'seeding-swapped',
        'seeding-tier-changed',
        'seeding-reordered',
        'seeding-tiers-resized',
        'seeding-fixes-reset',
        'seeding-roster-reseeded',
    ],
)
def test_old_entries_fall_back_to_the_code(event_type):
    row = _row(event_type, {'target': 'initial', 'seed_code': CODE})

    assert row['details'] == 'Seed code ABCD-EFGH-…'


def test_folded_swap_row_joins_child_details():
    entries = [
        _entry('seeding-swapped', _swap('SINGLE_ELIMINATION', 0, 1)),
        _entry(
            'seeding-swapped',
            _swap('SINGLE_ELIMINATION', 2, 3, a='c', b='d'),
            at=WHEN - timedelta(seconds=5),
        ),
    ]

    [row] = helpers.seeding_audit_rows(entries, USERS, names=NAMES)

    assert row['label'] == '2 × swapped'
    assert len(row['children']) == 2
    assert row['details'] == 'M1 Alice ↔ M2 Bob · M3 Carol ↔ M4 Dave'


def test_status_and_participant_rows_are_included():
    def status(new):
        return SimpleNamespace(
            event_type='tournament-status-changed',
            data={'old_status': 'REGISTRATION_OPEN', 'new_status': new},
        )

    def other(event_type):
        return SimpleNamespace(event_type=event_type, data={})

    assert helpers.is_seeding_audit_entry(status('REGISTRATION_CLOSED'))
    assert helpers.is_seeding_audit_entry(status('REGISTRATION_OPEN'))
    assert not helpers.is_seeding_audit_entry(status('ONGOING'))
    assert helpers.is_seeding_audit_entry(other('participant-left'))
    assert helpers.is_seeding_audit_entry(other('participant-removed'))
    assert helpers.is_seeding_audit_entry(other('seeding-drawn'))
    assert not helpers.is_seeding_audit_entry(other('tournament-created'))

    closed = _row(
        'tournament-status-changed',
        {'old_status': 'REGISTRATION_OPEN', 'new_status': 'REGISTRATION_CLOSED'},
    )
    reopened = _row(
        'tournament-status-changed',
        {'old_status': 'REGISTRATION_CLOSED', 'new_status': 'REGISTRATION_OPEN'},
    )
    left = _row(
        'participant-left',
        {'participant_id': 'a', 'roster_before': 4, 'roster_after': 3},
    )
    removed = _row(
        'participant-removed',
        {'participant_id': 'a', 'roster_before': 3, 'roster_after': 2},
    )

    assert closed['label'] == 'Registration closed'
    assert reopened['label'] == 'Registration reopened'
    assert left['label'] == 'Left'
    assert left['details'] == 'Roster 4 → 3'
    assert removed['label'] == 'Removed'
    assert removed['details'] == 'Roster 3 → 2'


@pytest.mark.parametrize('old_status', ['DRAFT', None])
def test_first_registration_opening_is_not_labelled_reopened(old_status):
    row = _row(
        'tournament-status-changed',
        {'old_status': old_status, 'new_status': 'REGISTRATION_OPEN'},
    )

    assert row['label'] == 'Registration opened'


def test_release_details_carry_the_playoff_code():
    entries = [
        _entry(
            'playoffs-released',
            {'mode': 'manual', 'match_count': 7, 'qualifier_count': 8},
        ),
        _entry(
            'bracket-generated',
            {'target': 'playoff', 'seed_code': CODE},
            at=WHEN - timedelta(milliseconds=40),
        ),
    ]

    [released, _] = helpers.seeding_audit_rows(entries, USERS, names=NAMES)

    assert released['details'] == '(Manual) · 8 qualifiers · Code ABCD-EFGH-…'


def test_release_code_is_never_guessed():
    released = _entry(
        'playoffs-released',
        {'mode': 'automatic', 'match_count': 7, 'qualifier_count': 8},
    )
    elsewhere = _entry(
        'bracket-generated',
        {'target': 'initial', 'seed_code': CODE},
        at=WHEN - timedelta(milliseconds=40),
    )
    stale = _entry(
        'bracket-regenerated',
        {'target': 'playoff', 'seed_code': CODE},
        at=WHEN - timedelta(hours=2),
    )

    for older in (elsewhere, stale):
        [row, _] = helpers.seeding_audit_rows(
            [released, older], USERS, names=NAMES
        )
        assert row['details'] == '(Automatic) · 8 qualifiers'
