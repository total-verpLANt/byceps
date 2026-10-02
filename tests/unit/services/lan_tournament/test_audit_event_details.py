"""
tests.unit.services.lan_tournament.test_audit_event_details
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Details cell of the qualification and release audit rows, and that
the audit tables show the orga's reason text escaped.
"""

from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)


NAMES = {'a': 'Alice', 'b': 'Bob', 'c': 'Carol'}
USERS = {'u1': SimpleNamespace(screen_name='Ohrwurm')}
WHEN = datetime(2026, 9, 30, 16, 42, tzinfo=UTC)


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


def _row(event_type, data, names=NAMES):
    entry = SimpleNamespace(
        occurred_at=WHEN, event_type=event_type, initiator_id='u1', data=data
    )
    [row] = helpers.seeding_audit_rows([entry], USERS, names=names)
    return row


def test_tie_decided_shows_scope_order_and_reason():
    row = _row(
        'qualification-tie-decided',
        {'scope': 'group:1', 'contestant_ids': ['b', 'a'], 'reason': 'Stechen'},
    )

    assert row['details'] == 'Group B: Bob, Alice · Reason: Stechen'


@pytest.mark.parametrize(
    'event_type', ['qualification-tie-decided', 'qualification-tie-withdrawn']
)
def test_tie_events_with_a_first_place_number_the_order(event_type):
    row = _row(
        event_type,
        {
            'scope': 'group:1',
            'contestant_ids': ['b', 'a'],
            'rank_from': 3,
            'rank_to': 4,
            'reason': 'Stechen',
        },
    )

    assert row['details'] == 'Group B: 3. Bob, 4. Alice · Reason: Stechen'


def test_tie_event_without_a_first_place_keeps_the_names_only_order():
    row = _row(
        'qualification-tie-decided',
        {'scope': 'group:1', 'contestant_ids': ['b', 'a'], 'reason': 'x'},
    )

    assert row['details'] == 'Group B: Bob, Alice · Reason: x'


def test_tie_decided_in_an_ffa_lobby_names_the_round_and_lobby():
    row = _row(
        'qualification-tie-decided',
        {'scope': 'ffa:WB:1:2', 'contestant_ids': ['a'], 'reason': 'x'},
    )

    assert row['details'].startswith('Winners Pool · Round 2 · Lobby 3: Alice')


@pytest.mark.parametrize(
    ('scope', 'label'),
    [('leaderboard', 'Leaderboard'), ('winner', 'Tournament win')],
)
def test_tie_decided_labels_the_other_scopes(scope, label):
    row = _row(
        'qualification-tie-decided',
        {'scope': scope, 'contestant_ids': ['a', 'b'], 'reason': 'x'},
    )

    assert row['details'] == f'{label}: Alice, Bob · Reason: x'


def test_tie_withdrawn_shows_both_reasons():
    row = _row(
        'qualification-tie-withdrawn',
        {
            'scope': 'group:0',
            'contestant_ids': ['a', 'b'],
            'reason': 'Wrong table',
            'decision_reason': 'Stechen',
        },
    )

    assert row['details'] == (
        'Group A: Alice, Bob · Reason: Wrong table · Original reason: Stechen'
    )


def test_playoffs_released_shows_mode_and_match_count():
    manual = _row('playoffs-released', {'mode': 'manual', 'match_count': 7})
    auto = _row('playoffs-released', {'mode': 'automatic', 'match_count': 1})

    assert manual['details'] == 'Manual · 7 matches'
    assert auto['details'] == 'Automatic · 1 match'


def test_playoffs_unreleased_shows_the_reason_and_the_suspension():
    plain = _row(
        'playoffs-unreleased',
        {'reason': 'Wrong qualifiers', 'auto_release_suspended': False},
    )
    suspended = _row(
        'playoffs-unreleased',
        {'reason': 'Wrong qualifiers', 'auto_release_suspended': True},
    )

    assert plain['details'] == 'Reason: Wrong qualifiers'
    assert suspended['details'] == (
        'Reason: Wrong qualifiers · Automatic release suspended'
    )


def test_leaderboard_closed_names_the_leaderboard():
    row = _row('qualification-leaderboard-closed', {})

    assert row['details'] == 'Leaderboard'


@pytest.mark.parametrize(
    'event_type',
    [
        'qualification-tie-decided',
        'qualification-tie-withdrawn',
        'playoffs-released',
        'playoffs-unreleased',
    ],
)
def test_missing_data_keeps_the_neutral_fallback(event_type):
    assert _row(event_type, {})['details'] == ''


def test_unknown_event_has_no_details():
    assert _row('qualification-brand-new', {'reason': 'x'})['details'] == ''


def test_unknown_contestant_falls_back_to_its_id():
    row = _row(
        'qualification-tie-decided',
        {'scope': 'group:0', 'contestant_ids': ['zz'], 'reason': 'x'},
        names={},
    )

    assert 'zz' in row['details']
