import pathlib
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)

from tests.unit.services.lan_tournament import (
    test_tournament_request_render_bote as bote,
)


_BOTE_STYLE_PARTIAL = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
    '/_bote_request_style.html'
)


def _rule_block(src: str, selector: str) -> str:
    """Return the declaration block of `selector`."""
    start = src.index(selector)
    end = src.index('}', start)
    return src[start : end + 1]


def test_b3_request_head_resets_theme_font():
    """`.request-page .head` resets the theme's header font."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    rule = _rule_block(src, '.request-page .head {')

    assert 'font-family: var(--body)' in rule
    assert 'letter-spacing: normal' in rule


def test_b4_disabled_mode_strikes_only_the_name():
    """A disabled mode option strikes only its name."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    rule = _rule_block(src, '.request-page .opt.dis .opt-label b {')

    assert 'text-decoration: line-through' in rule
    assert '.opt-label.struck' not in src


@pytest.fixture(scope='module')
def bote_env():
    """The render tests' own stub environment, reduced to the dashboard."""
    return bote._make_env(
        {'my_requests': bote._snippet(bote._BOTE_MY_REQUESTS_TEMPLATE)}
    )


def test_b2_win_card_shows_team_signups(bote_env):
    """The win card shows team signups for team tournaments."""
    created = bote._fake_request_row(
        'tournament_created',
        number=3,
        name='Team Cup',
        created_tournament_id='t-1',
    )
    tournament = SimpleNamespace(
        id='t-1',
        name='Team Tournament',
        contestant_type=ContestantType.TEAM,
        max_players=None,
        max_teams=8,
        start_time=None,
    )

    html = bote._render_my_requests(
        bote_env,
        requests=[created],
        live_requests=[created],
        tournaments_by_request_id={created.id: tournament},
        signup_counts=bote._signup_counts_for(
            tournament, {}, {tournament.id: 3}
        ),
    )

    assert '<dt>Signed up</dt>' in html
    assert '3 of 8' in html
