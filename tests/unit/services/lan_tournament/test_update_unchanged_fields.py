"""
tests.unit.services.lan_tournament.test_update_unchanged_fields
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The edit form drops errors on values that still equal the stored ones.
"""

from datetime import datetime, UTC
from types import SimpleNamespace

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.blueprints.admin.forms import (
    TournamentUpdateForm,
)


@pytest.fixture(scope='module')
def app():
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def _tournament(**overrides):
    values = dict(
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        group_size_min=None,
        group_size_max=None,
        advancement_count=None,
        point_table=None,
        start_time=None,
    )
    return SimpleNamespace(**{**values, **overrides})


def _valid(tournament, **data) -> tuple[bool, TournamentUpdateForm]:
    form = TournamentUpdateForm(
        MultiDict({'name': 'Cup', 'category': 'MAIN', **data})
    )
    form.set_contestant_type_choices()
    form.set_game_format_choices()
    form.set_elimination_mode_choices()
    form.set_score_ordering_choices()
    return views._update_form_is_valid(form, tournament), form


def test_an_unchanged_out_of_range_count_is_accepted(app):
    with app.test_request_context():
        ok, _ = _valid(_tournament(min_players=0), min_players='0')

    assert ok


def test_a_changed_out_of_range_count_is_kept(app):
    with app.test_request_context():
        ok, form = _valid(_tournament(min_players=4), min_players='0')

    assert not ok
    assert form.min_players.errors


def test_an_unparseable_count_is_not_taken_for_an_unset_one(app):
    with app.test_request_context():
        ok, form = _valid(_tournament(), min_players='abc')

    assert not ok
    assert form.min_players.errors


def test_a_cleared_stored_count_is_unchanged(app):
    with app.test_request_context():
        ok, _ = _valid(_tournament(), min_players='')

    assert ok


# fmt: off
@pytest.mark.parametrize(
    ('stored', 'posted', 'expected_ok'),
    [
        ((5, 3), ('5', '3'), True),    # legacy pair, both unchanged
        ((5, 3), ('6', '3'), False),   # min changed
        ((5, 3), ('5', '4'), False),   # max changed, still below min
        ((5, 3), ('2', '3'), True),    # min changed to consistent
        ((5, 3), ('5', '8'), True),    # max changed to consistent
    ],
)
# fmt: on
def test_a_min_max_pair_error_stays_while_either_side_changed(
    app, stored, posted, expected_ok
):
    tournament = _tournament(min_players=stored[0], max_players=stored[1])

    with app.test_request_context():
        ok, _ = _valid(
            tournament, min_players=posted[0], max_players=posted[1]
        )

    assert ok is expected_ok


# fmt: off
@pytest.mark.parametrize(
    ('stored', 'posted', 'expected_ok'),
    [
        ([5, 3, 1], '5, 3, 1', True),
        ([5, 3, 1], '5,3,1', True),
        ([5, 3, 1], '5, 3, 2', True),
        (None, '', True),
    ],
)
# fmt: on
def test_the_point_table_field_compares_the_parsed_table(
    app, stored, posted, expected_ok
):
    with app.test_request_context():
        ok, _ = _valid(_tournament(point_table=stored), point_table=posted)

    assert ok is expected_ok


def test_an_unchanged_overlong_point_table_string_is_accepted(app):
    table = [999_999_999 - i for i in range(64)]
    assert len(', '.join(map(str, table))) > 500

    with app.test_request_context():
        ok, _ = _valid(
            _tournament(point_table=table),
            point_table=', '.join(map(str, table)),
        )

    assert ok


def test_a_changed_overlong_point_table_string_is_kept(app):
    table = [999_999_999 - i for i in range(64)]

    with app.test_request_context():
        ok, form = _valid(
            _tournament(point_table=[1]),
            point_table=', '.join(map(str, table)),
        )

    assert not ok
    assert form.point_table.errors


def test_an_out_of_window_stored_start_time_is_unchanged_only_if_equal(app):
    stored = datetime(1999, 5, 1, 18, 30, tzinfo=UTC)

    with app.test_request_context():
        ok_same, _ = _valid(
            _tournament(start_time=stored), start_time='1999-05-01T18:30'
        )
        ok_other, _ = _valid(
            _tournament(start_time=stored), start_time='1999-05-01T19:30'
        )

    assert ok_same
    assert not ok_other
