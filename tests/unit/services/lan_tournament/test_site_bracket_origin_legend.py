"""
tests.unit.services.lan_tournament.test_site_bracket_origin_legend
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The site bracket page explains the origin chip, on the playoff bracket only.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.util.result import Ok

from tests.unit.services.lan_tournament.test_readiness_surface_policy import (
    _row,
    NOW,
)
from tests.unit.services.lan_tournament.test_site_readiness_filters import (
    site as site,
)


LEGEND = 'data-lt-origin-legend'
THEMES = ['generic', 'totalverplant-36']


def _playoff_site(site, monkeypatch, *, origin):
    site.tournament = replace(
        site.tournament,
        game_format=GameFormat.FREE_FOR_ALL,
        playoff_game_format=GameFormat.ONE_V_ONE,
        playoff_elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    site.rows = [_row(site.tournament, phase=2)]
    contestants = site.rows[0][1]
    labels = {str(contestants[0].participant_id): 'C2'} if origin else {}
    state = SimpleNamespace(source='groups', released_at=NOW)
    monkeypatch.setattr(
        views.tournament_qualification_service,
        'get_qualification',
        lambda _: Ok(state),
    )
    monkeypatch.setattr(views, 'participant_rankings', lambda *a: [])
    monkeypatch.setattr(views, 'contestant_names', lambda *a: {})
    monkeypatch.setattr(views, 'playoff_waiting_reason', lambda *a: None)
    monkeypatch.setattr(views, 'playoff_origin_labels', lambda *a: labels)


@pytest.mark.parametrize('theme', THEMES)
def test_the_playoff_bracket_explains_the_origin_chip_once(
    site, monkeypatch, theme
):
    _playoff_site(site, monkeypatch, origin=True)

    context = site.context('bracket')
    html = site.render(context, 'bracket', theme)

    assert context['phase_view'] == 2
    assert 'Group C, place 2' in str(context['bracket_json'])
    assert html.count(LEGEND) == 1
    assert 'where the player qualified from' in html
    assert html.index(LEGEND) < html.index("id='bracket-app'")


@pytest.mark.parametrize('theme', THEMES)
def test_a_playoff_bracket_without_origins_has_no_legend(
    site, monkeypatch, theme
):
    _playoff_site(site, monkeypatch, origin=False)

    html = site.render(site.context('bracket'), 'bracket', theme)

    assert "id='bracket-app'" in html
    assert LEGEND not in html


@pytest.mark.parametrize('theme', THEMES)
def test_the_group_phase_has_no_legend(site, monkeypatch, theme):
    _playoff_site(site, monkeypatch, origin=True)

    context = site.context('bracket', query='?phase=1')
    html = site.render(context, 'bracket', theme)

    assert context['phase_view'] == 1
    assert "id='bracket-app'" not in html
    assert LEGEND not in html


@pytest.mark.parametrize('theme', THEMES)
def test_a_plain_bracket_has_no_legend(site, theme):
    site.tournament = replace(
        site.tournament, elimination_mode=EliminationMode.SINGLE_ELIMINATION
    )
    site.rows = [_row(site.tournament)]

    context = site.context('bracket')
    html = site.render(context, 'bracket', theme)

    assert context['phase_view'] is None
    assert "id='bracket-app'" in html
    assert LEGEND not in html


# fmt: off
@pytest.mark.parametrize('bracket_json', [
    {},
    {'matches': []},
    {'matches': [{}]},
    {'matches': [{'contestants': [{}]}]},
    {'matches': [{'contestants': [{'origin': None}]}]},
])
# fmt: on
def test_the_legend_check_tolerates_missing_payload_keys(
    site, monkeypatch, bracket_json
):
    _playoff_site(site, monkeypatch, origin=False)
    context = {**site.context('bracket'), 'bracket_json': bracket_json}

    html = site.render(context, 'bracket')

    assert LEGEND not in html
