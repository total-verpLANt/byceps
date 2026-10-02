from importlib import import_module
from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, g
import pytest

from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.util.result import Err, Ok


@pytest.fixture(params=['admin', 'site'])
def route(request, monkeypatch):
    surface = request.param
    views = import_module(
        f'byceps.services.lan_tournament.blueprints.{surface}.views'
    )
    tournament = SimpleNamespace(
        id='t0',
        game_format=GameFormat.FREE_FOR_ALL,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )
    monkeypatch.setattr(views, '_get_tournament_or_404', lambda _: tournament)
    monkeypatch.setattr(
        views,
        'gettext',
        lambda message, **params: message % params if params else message,
    )
    prepare = Mock()
    generate = Mock()
    monkeypatch.setattr(
        views.tournament_seeding_service, 'generate_from_seeding', generate
    )
    monkeypatch.setattr(
        views.tournament_seeding_service, 'prepare_ffa_round_draft', prepare
    )
    redirect = Mock()
    success = Mock()
    error = Mock()
    monkeypatch.setattr(views, 'redirect_to', redirect)
    monkeypatch.setattr(views, 'flash_success', success)
    monkeypatch.setattr(views, 'flash_error', error)
    function = getattr(
        views,
        'advance_ffa_round_action'
        if surface == 'admin'
        else 'orga_advance_ffa_round',
    )
    return SimpleNamespace(
        surface=surface,
        function=unwrap(function),
        prepare=prepare,
        generate=generate,
        generate_function=unwrap(
            getattr(
                views,
                'seeding_generate'
                if surface == 'admin'
                else 'orga_seeding_generate',
            )
        ),
        redirect=redirect,
        success=success,
        error=error,
    )


def _call(route):
    app = Flask(__name__)
    with app.test_request_context('/', method='POST'):
        g.user = SimpleNamespace(id='u0')
        route.function('t0')


def test_lone_survivor_completion_redirects_to_bracket(route):
    route.prepare.return_value = Ok('completed')

    _call(route)

    route.prepare.assert_called_once_with('t0', pool=None, initiator_id='u0')
    route.redirect.assert_called_once_with('.bracket', tournament_id='t0')
    route.success.assert_called_once_with(
        'The tournament is complete. The lone survivor wins.'
    )
    route.error.assert_not_called()


def test_ordinary_ffa_draft_still_redirects_to_seeding(route):
    route.prepare.return_value = Ok('ffa:WB:2')

    _call(route)

    route.redirect.assert_called_once_with(
        '.seeding' if route.surface == 'admin' else '.orga_seeding',
        tournament_id='t0',
        target='ffa:WB:2',
    )
    route.success.assert_not_called()
    route.error.assert_not_called()


def test_refused_ffa_advance_redirects_to_bracket(route):
    route.prepare.return_value = Err('The round has unconfirmed matches.')

    _call(route)

    route.redirect.assert_called_once_with('.bracket', tournament_id='t0')
    route.error.assert_called_once_with('The round has unconfirmed matches.')
    route.success.assert_not_called()


@pytest.mark.parametrize('result', ['completed', 2])
def test_generate_handles_completion_and_normal_match_count(route, result):
    route.generate.return_value = Ok(result)
    app = Flask(__name__)
    with app.test_request_context(
        '/', method='POST', data={'target': 'ffa:SE:2', 'version': '3'}
    ):
        g.user = SimpleNamespace(id='u0')
        route.generate_function('t0')

    route.generate.assert_called_once_with(
        't0', 'ffa:SE:2', expected_version=3, initiator_id='u0'
    )
    if result == 'completed':
        route.redirect.assert_called_once_with('.bracket', tournament_id='t0')
        route.success.assert_called_once_with(
            'The tournament is complete. The lone survivor wins.'
        )
    else:
        route.redirect.assert_called_once_with(
            '.seeding' if route.surface == 'admin' else '.orga_seeding',
            tournament_id='t0',
            target='ffa:SE:2',
        )
        route.success.assert_called_once()
    route.error.assert_not_called()
