from dataclasses import fields
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)
from tests.unit.services.lan_tournament.test_seeding_render import (
    FFA,
    _board,
    _render,
    _render_site,
    env as env,
    plain_translations as plain_translations,
    site_env as site_env,
)


_NOTICE = 'Player 03 is waiting for the Grand Final while the lower bracket continues.'


def _waiting_board(names=('Player 03',)):
    board = _board(FFA, 8, param=4)
    values = {field.name: getattr(board, field.name) for field in fields(board)}
    return SimpleNamespace(**(values | {'waiting_winners': names}))


def test_payload_names_waiting_winner_separately_from_losers_byes():
    payload = helpers.seeding_board_payload(_waiting_board())

    assert payload['waiting_winners'] == [_NOTICE]
    assert payload['lobby_byes'] == []


@pytest.mark.parametrize('surface', ['admin', 'site', 'bote'])
def test_waiting_winner_notice_renders_on_orga_boards(env, site_env, surface):
    board = _waiting_board()
    html = (
        _render(env, board)
        if surface == 'admin'
        else _render_site(site_env, board, name=surface)
    )

    banners = html[html.index('data-lt-seed-banners') :]
    assert _NOTICE in banners
    assert 'is alone in the losers pool' not in banners


@pytest.mark.parametrize('surface', ['admin', 'site', 'bote'])
def test_waiting_winner_notice_escapes_contestant_name(env, site_env, surface):
    board = _waiting_board(('<script>Winner</script>',))
    html = (
        _render(env, board)
        if surface == 'admin'
        else _render_site(site_env, board, name=surface)
    )

    banners = html[html.index('data-lt-seed-banners') :]
    assert '&lt;script&gt;Winner&lt;/script&gt; is waiting' in banners
    assert '<script>Winner</script> is waiting' not in banners


@pytest.mark.parametrize('surface', ['admin', 'site', 'bote'])
def test_no_waiting_notice_without_waiting_winner(env, site_env, surface):
    html = (
        _render(env, _waiting_board(()))
        if surface == 'admin'
        else _render_site(site_env, _waiting_board(()), name=surface)
    )

    assert 'is waiting for the Grand Final' not in html


def test_winners_bye_audit_names_the_winner_and_grand_final():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-lobby-bye',
        initiator_id=None,
        data={'pool': 'WB', 'round': 1, 'contestant': 'c03'},
    )

    (row,) = helpers.seeding_audit_rows([entry], {}, {'c03': 'Player 03'})

    assert row['label'] == 'Bye'
    assert row['details'] == (
        'Player 03 won the winners bracket, round 1, and waits for the Grand Final.'
    )


def test_winners_bye_audit_falls_back_to_contestant_id():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-lobby-bye',
        initiator_id=None,
        data={'pool': 'WB', 'round': 1, 'contestant': 'c03'},
    )

    (row,) = helpers.seeding_audit_rows([entry], {}, {})

    assert row['details'] == (
        'c03 won the winners bracket, round 1, and waits for the Grand Final.'
    )


def test_lone_survivor_audit_names_the_tournament_winner():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-single-survivor',
        initiator_id=None,
        data={'pool': 'SE', 'round': 2, 'contestant': 'c03'},
    )

    (row,) = helpers.seeding_audit_rows([entry], {}, {'c03': 'Player 03'})

    assert row['label'] == 'Lone survivor wins'
    assert row['details'] == (
        'Player 03 is the lone survivor and wins the tournament, round 2.'
    )


def test_losers_bye_audit_preserves_existing_details():
    entry = SimpleNamespace(
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        event_type='bracket-lobby-bye',
        initiator_id=None,
        data={'pool': 'LB', 'round': 1, 'contestant': 'c03'},
    )
    (row,) = helpers.seeding_audit_rows([entry], {}, {'c03': 'Player 03'})

    assert row['label'] == 'Bye'
    assert row['details'] == 'Bye: Player 03 waits in the losers pool, round 1.'
