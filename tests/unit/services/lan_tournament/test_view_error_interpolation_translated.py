"""
tests.unit.services.lan_tournament.test_view_error_interpolation_translated
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import ast
import pathlib

import pytest


_BLUEPRINTS = pathlib.Path('byceps/services/lan_tournament/blueprints')
_VIEWS = sorted(_BLUEPRINTS.glob('*/views.py'))


def _raw_error_interpolations(path):
    """Yield `(line, name)` for `gettext(..., error=<bare name>)` calls."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, 'id', getattr(func, 'attr', None)) != 'gettext':
            continue
        for keyword in node.keywords:
            if keyword.arg == 'error' and isinstance(keyword.value, ast.Name):
                yield node.lineno, keyword.value.id


def test_views_are_found():
    assert _VIEWS


@pytest.mark.parametrize('path', _VIEWS, ids=lambda p: p.parent.name)
def test_service_errors_are_translated_before_interpolation(path):
    """A service `Err` is an English msgid and needs its own `gettext`.

    Interpolating it raw leaves a German prefix with an English tail.
    """
    assert list(_raw_error_interpolations(path)) == []
