"""
:License: Revised BSD (see `LICENSE` file for details)
"""

from importlib import import_module
from itertools import permutations
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

from flask import Flask, url_for
from flask_babel import Babel, force_locale
import pytest

from byceps.services.chair_planning.blueprints.admin.navigation import (
    install_party_navigation,
)
from byceps.services.more.blueprints.admin import item_service
from byceps.services.more.blueprints.admin.item_service import MoreItem


@pytest.fixture(autouse=True)
def isolated_party_navigation(monkeypatch):
    """Restore both process-wide hooks, including pre-existing installation."""
    monkeypatch.setattr(item_service, 'get_party_items', lambda party: [])
    monkeypatch.setattr(
        item_service,
        '_chair_planning_party_navigation_wrapper',
        None,
        raising=False,
    )


@pytest.fixture
def make_navigation_app():
    def _make(*, with_chair=True):
        app = Flask(__name__)
        Babel(app, default_locale='en')
        if with_chair:
            views = import_module(
                'byceps.services.chair_planning.blueprints.admin.views'
            )
            app.register_blueprint(
                views.blueprint, url_prefix='/chair_planning'
            )
        return app

    return _make


def _item(url, *, permission='seating.view', label='Existing item'):
    return MoreItem(
        label=label,
        icon='seating-area',
        url=url,
        required_permission=permission,
    )


def test_party_navigation_preserves_original_items_and_adds_chair_once(
    monkeypatch, make_navigation_app, party
):
    original_items = [
        _item('/seating/areas'),
        _item('/sold_products', permission='shop_order.view'),
        _item('/timetable', permission='timetable.update'),
    ]
    original_snapshot = original_items.copy()
    monkeypatch.setattr(
        item_service, 'get_party_items', lambda party: original_items
    )
    app = make_navigation_app()
    installed = item_service.get_party_items
    install_party_navigation()
    assert item_service.get_party_items is installed

    with app.test_request_context(), force_locale('en'):
        expected_url = url_for('chair_planning_admin.index', party_id=party.id)
        first = item_service.get_party_items(party)
        second = item_service.get_party_items(party)

    assert first == second
    assert first[0] == _item(expected_url, label='Seat management')
    assert first[1:] == original_snapshot
    assert all(a is b for a, b in zip(first[1:], original_items, strict=True))
    assert sum(item.url == expected_url for item in first) == 1
    assert original_items == original_snapshot
    assert first is not original_items
    assert second is not first


@pytest.mark.parametrize('existing_permission', ['seating.view', 'party.view'])
def test_party_navigation_deduplicates_by_url_not_permission(
    monkeypatch, make_navigation_app, party, existing_permission
):
    app = make_navigation_app()
    with app.test_request_context(), force_locale('en'):
        chair_url = url_for('chair_planning_admin.index', party_id=party.id)

    existing_items = [
        _item('/seating/areas'),
        _item(
            chair_url, permission=existing_permission, label='Existing chair'
        ),
        _item('/seating/export'),
    ]
    # Install around this predecessor in a fresh, isolated installation state.
    monkeypatch.setattr(
        item_service, 'get_party_items', lambda party: existing_items
    )
    monkeypatch.delattr(
        item_service, '_chair_planning_party_navigation_wrapper'
    )
    install_party_navigation()

    with app.test_request_context(), force_locale('en'):
        items = item_service.get_party_items(party)

    assert items == existing_items
    assert items is not existing_items
    assert sum(item.url == chair_url for item in items) == 1
    assert {item.url for item in items} == {
        '/seating/areas',
        chair_url,
        '/seating/export',
    }
    assert items[1].required_permission == existing_permission


def test_party_navigation_uses_party_specific_urls(
    make_navigation_app, make_party
):
    app = make_navigation_app()
    first_party = make_party()
    second_party = make_party()

    with app.test_request_context(), force_locale('en'):
        first_items = item_service.get_party_items(first_party)
        second_items = item_service.get_party_items(second_party)
        assert first_items[0].url == url_for(
            'chair_planning_admin.index', party_id=first_party.id
        )
        assert second_items[0].url == url_for(
            'chair_planning_admin.index', party_id=second_party.id
        )

    assert first_items[0].url != second_items[0].url
    assert first_items[0].label == second_items[0].label == 'Seat management'


def test_party_navigation_preserves_predecessor_markers(
    monkeypatch, make_navigation_app
):
    def original(party):
        return []

    original._lan_tournament_patched = True
    original._pizza_delivery_patched = True
    monkeypatch.setattr(item_service, 'get_party_items', original)

    make_navigation_app()

    assert item_service.get_party_items.__wrapped__ is original
    assert item_service.get_party_items.__name__ == original.__name__
    assert item_service.get_party_items._lan_tournament_patched
    assert item_service.get_party_items._pizza_delivery_patched


def test_party_navigation_skips_apps_without_chair_blueprint(
    monkeypatch, make_navigation_app, party
):
    original_items = [_item('/seating/areas')]
    monkeypatch.setattr(
        item_service, 'get_party_items', lambda party: original_items
    )
    chair_app = make_navigation_app()
    without_chair_app = make_navigation_app(with_chair=False)

    with without_chair_app.test_request_context(), force_locale('en'):
        items = item_service.get_party_items(party)

    assert items == original_items
    assert items is not original_items
    assert 'chair_planning_admin' not in without_chair_app.blueprints

    with chair_app.test_request_context(), force_locale('en'):
        assert len(item_service.get_party_items(party)) == 2
    assert original_items == [_item('/seating/areas')]


@pytest.mark.parametrize(
    'outer_marker', ['_lan_tournament_patched', '_pizza_delivery_patched']
)
def test_party_navigation_repeated_apps_keep_outer_wrapper_with_hidden_markers(
    monkeypatch, make_navigation_app, party, outer_marker
):
    first_app = make_navigation_app()
    installed = item_service.get_party_items

    def outer(party):
        return installed(party) + [_item('/outer', permission='admin.access')]

    setattr(outer, outer_marker, True)
    monkeypatch.setattr(item_service, 'get_party_items', outer)
    assert not hasattr(outer, '__wrapped__')

    second_app = make_navigation_app()
    install_party_navigation()
    assert item_service.get_party_items is outer
    assert item_service._chair_planning_party_navigation_wrapper is installed

    for app in (first_app, second_app):
        with app.test_request_context(), force_locale('en'):
            items = item_service.get_party_items(party)
            chair_url = url_for('chair_planning_admin.index', party_id=party.id)
        assert sum(item.url == chair_url for item in items) == 1
        assert [item.url for item in items] == [chair_url, '/outer']


@pytest.mark.parametrize(
    'order',
    [('lan', 'chair'), ('chair', 'lan')]
    + list(permutations(('lan', 'pizza', 'chair'))),
    ids='-'.join,
)
def test_party_navigation_composes_real_lan_and_pizza_in_fresh_process(order):
    """Exercise import-time LAN/pizza wrappers without mutating their globals."""
    script = dedent("""\
        from importlib import import_module
        import json
        import sys
        from types import SimpleNamespace

        from flask import Flask, url_for
        from flask_babel import Babel, force_locale

        from byceps.services.more.blueprints.admin import item_service

        order = json.loads(sys.argv[1])
        original = item_service.get_party_items

        def make_app():
            app = Flask(__name__)
            Babel(app, default_locale='en')
            endpoints = (
                'party_admin.export_for_lanpartydb',
                'orga_presence.view',
                'shop_sold_products_admin.index',
                'timetable_admin.view',
                'tourney_tourney_admin.index',
                'lan_tournament_admin.overview',
                'pizza_delivery_admin.index_for_party',
            )
            for endpoint in endpoints:
                app.add_url_rule(
                    '/core/' + endpoint + '/<party_id>',
                    endpoint=endpoint,
                    view_func=lambda party_id: '',
                )
            return app

        modules = {
            'lan': 'byceps.services.lan_tournament.blueprints.admin.views',
            'pizza': 'byceps.services.pizza_delivery.blueprints.admin.views',
            'chair': 'byceps.services.chair_planning.blueprints.admin.views',
        }
        first_app = make_app()
        for module in order:
            views = import_module(modules[module])
            if module == 'chair':
                first_app.register_blueprint(
                    views.blueprint, url_prefix='/chair_planning'
                )

        outer = item_service.get_party_items
        chair_views = import_module(modules['chair'])
        second_app = make_app()
        second_app.register_blueprint(
            chair_views.blueprint, url_prefix='/chair_planning'
        )
        assert item_service.get_party_items is outer

        for app in (first_app, second_app):
            for party_id in ('first-party', 'second-party'):
                party = SimpleNamespace(id=party_id)
                with app.test_request_context(), force_locale('en'):
                    baseline = original(party)
                    items = item_service.get_party_items(party)
                    repeated = item_service.get_party_items(party)
                    chair_url = url_for(
                        'chair_planning_admin.index', party_id=party_id
                    )
                    lan_url = url_for(
                        'lan_tournament_admin.overview', party_id=party_id
                    )
                    pizza_url = url_for(
                        'pizza_delivery_admin.index_for_party',
                        party_id=party_id,
                    )

                assert items == repeated
                assert items[0].url == chair_url
                assert items[0].label == 'Seat management'
                assert items[0].required_permission == 'seating.view'
                assert sum(item.url == chair_url for item in items) == 1
                assert sum(item.url == lan_url for item in items) == 1
                assert not any(
                    item.required_permission == 'tourney.view' for item in items
                )
                preserved = [
                    item for item in baseline
                    if item.required_permission != 'tourney.view'
                ]
                assert [item for item in items if item in preserved] == preserved
                assert len(items) == len(preserved) + 2 + ('pizza' in order)
                assert sum(item.url == pizza_url for item in items) == (
                    1 if 'pizza' in order else 0
                )
        print('PASS: composition, party URLs, and repeated apps')
    """)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, '-c', script, json.dumps(order)],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        'PASS: composition, party URLs, and repeated apps' in completed.stdout
    )


def test_party_navigation_composes_with_actual_admin_registration_and_pizza():
    """Run the real registrar twice, with no view or endpoint substitutions."""
    script = dedent("""\
        from importlib import import_module
        import socket
        from types import SimpleNamespace
        from unittest.mock import patch

        from flask import Flask, url_for
        from flask_babel import Babel, force_locale
        from sqlalchemy.engine import Engine

        from byceps.blueprints.admin import register_admin_blueprints
        from byceps.services.more.blueprints.admin import item_service

        original = item_service.get_party_items

        def make_app():
            app = Flask(__name__)
            Babel(app, default_locale='en')
            register_admin_blueprints(app)
            return app

        # Registration and URL composition must stay entirely DB/network-free.
        with (
            patch.object(
                Engine, 'connect',
                side_effect=AssertionError('Registrar unit attempted DB access'),
            ),
            patch.object(
                socket.socket, 'connect',
                side_effect=AssertionError('Registrar unit attempted network access'),
            ),
        ):
            first_app = make_app()
            installed = item_service.get_party_items
            chair_wrapper = item_service._chair_planning_party_navigation_wrapper
            second_app = make_app()
            assert item_service.get_party_items is installed
            assert item_service._chair_planning_party_navigation_wrapper is chair_wrapper

            real_blueprints = {
                import_module(path).blueprint.name: import_module(path).blueprint
                for path in (
                    'byceps.services.chair_planning.blueprints.admin.views',
                    'byceps.services.lan_tournament.blueprints.admin.views',
                    'byceps.services.pizza_delivery.blueprints.admin.views',
                )
            }
            for app in (first_app, second_app):
                for name, blueprint in real_blueprints.items():
                    assert app.blueprints[name] is blueprint

                for party_id in ('first-party', 'second-party'):
                    party = SimpleNamespace(id=party_id)
                    with app.test_request_context(), force_locale('en'):
                        baseline = original(party)
                        items = item_service.get_party_items(party)
                        repeated = item_service.get_party_items(party)
                        chair_url = url_for(
                            'chair_planning_admin.index', party_id=party_id
                        )
                        lan_url = url_for(
                            'lan_tournament_admin.overview', party_id=party_id
                        )
                        pizza_url = url_for(
                            'pizza_delivery_admin.index_for_party',
                            party_id=party_id,
                        )
                        assert url_for(
                            'more_admin.view_party', party_id=party_id
                        ) == '/more/parties/' + party_id

                    assert chair_url == '/chair_planning/for_party/' + party_id
                    assert lan_url == (
                        '/lan-tournaments/for_party/' + party_id + '/overview'
                    )
                    assert pizza_url == '/pizza-deliveries/parties/' + party_id
                    assert items == repeated
                    assert items[0].url == chair_url
                    assert items[0].label == 'Seat management'
                    assert items[0].required_permission == 'seating.view'
                    for expected_url in (chair_url, lan_url, pizza_url):
                        assert sum(item.url == expected_url for item in items) == 1
                    preserved = [
                        item for item in baseline
                        if item.required_permission != 'tourney.view'
                    ]
                    assert [item for item in items if item in preserved] == preserved
                    assert len(items) == len(preserved) + 3
                    assert not any(
                        item.required_permission == 'tourney.view' for item in items
                    )
        print('PASS: real admin registrar, real views/routes, and repeated apps')
    """)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        'PASS: real admin registrar, real views/routes, and repeated apps'
        in completed.stdout
    )
