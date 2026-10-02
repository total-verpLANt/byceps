from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament import (
    lan_tournament_view_helpers as helpers,
)
from byceps.services.lan_tournament.models.bracket import Bracket
from byceps.services.lan_tournament.tournament_match_service import (
    UndersizedPool,
)
from tests.unit.services.lan_tournament.test_seeding_render import (
    FFA,
    _board,
    _markup,
    _render,
    _render_site,
    env as env,
    plain_translations as plain_translations,
    site_env as site_env,
)


@pytest.mark.parametrize('surface', ['admin', 'site', 'bote'])
@pytest.mark.parametrize('sizes', [(2,), (3, 2)])
def test_natural_shortfall_notice_and_audit_are_truthful(
    env, site_env, surface, sizes
):
    pool = UndersizedPool(
        Bracket.WINNERS, 2, sum(sizes), sizes, 3, natural_shortfall=True
    )
    board = _board(FFA, 8, param=4, undersized=(pool,))
    payload = helpers.seeding_board_payload(board)
    expected = (
        f'Advancement leaves the winners pool with {sum(sizes)} contestants.'
        f' Lobby sizes: {", ".join(map(str, sizes))}, below the minimum of 3.'
    )
    assert payload['undersized'] == [expected]
    rows = helpers.seeding_audit_rows(
        [
            SimpleNamespace(
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                event_type='bracket-lobby-undersized',
                initiator_id=None,
                data={
                    'pool': 'WB',
                    'round': 2,
                    'count': sum(sizes),
                    'lobbies': list(sizes),
                    'minimum': 3,
                    'reason': 'natural_shortfall',
                },
            )
        ],
        {},
        {},
    )
    assert rows[0]['details'] == (
        f'Natural shortfall after advancement: the winners pool has lobbies of'
        f' {", ".join(map(str, sizes))}, below the minimum of 3.'
    )
    html = (
        _render(env, board, rows=rows)
        if surface == 'admin'
        else _render_site(site_env, board, name=surface, rows=rows)
    )
    assert expected in html
    assert rows[0]['details'] in html
    assert 'participants were removed' not in html
    assert 'Removed participants:' not in html
    assert '%(' not in _markup(html)


@pytest.mark.parametrize('surface', ['admin', 'site', 'bote'])
def test_natural_shortfall_notice_and_audit_escape_labels(
    env, site_env, surface, monkeypatch
):
    monkeypatch.setattr(
        helpers, '_pool_phrase', lambda code: '<script>WB</script>'
    )
    pool = UndersizedPool(
        Bracket.WINNERS, 2, 2, (2,), 3, natural_shortfall=True
    )
    board = _board(FFA, 8, param=4, undersized=(pool,))
    rows = helpers.seeding_audit_rows(
        [
            SimpleNamespace(
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                event_type='bracket-lobby-undersized',
                initiator_id=None,
                data={
                    'pool': 'WB',
                    'lobbies': [2],
                    'minimum': 3,
                    'reason': 'natural_shortfall',
                },
            )
        ],
        {},
        {},
    )
    html = (
        _render(env, board, rows=rows)
        if surface == 'admin'
        else _render_site(site_env, board, name=surface, rows=rows)
    )
    assert 'Advancement leaves &lt;script&gt;WB&lt;/script&gt;' in html
    assert (
        'Natural shortfall after advancement: &lt;script&gt;WB&lt;/script&gt;'
        in html
    )
    assert '<script>WB</script>' not in html


@pytest.mark.parametrize('reason', [None, 'removal', 'unknown'])
def test_legacy_removal_audit_retains_wording(reason):
    data = {'pool': 'WB', 'lobbies': [2], 'minimum': 3}
    if reason is not None:
        data['reason'] = reason
    assert helpers._undersized_event_details(data) == (
        'Removed participants: the winners pool has lobbies of 2, below the minimum of 3.'
    )
    assert helpers._undersized_notice(
        UndersizedPool(Bracket.WINNERS, 2, 2, (2,), 3)
    ) == (
        'Because participants were removed, the winners pool has only 2 contestants.'
        ' Lobby sizes: 2, below the minimum of 3.'
    )


def test_natural_shortfall_audit_uses_reason_not_removal_wording():
    assert helpers._undersized_event_details(
        {
            'pool': 'WB',
            'lobbies': [2],
            'minimum': 3,
            'reason': 'natural_shortfall',
        }
    ) == (
        'Natural shortfall after advancement: the winners pool has lobbies of 2,'
        ' below the minimum of 3.'
    )
