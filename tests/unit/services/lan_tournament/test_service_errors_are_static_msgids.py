"""
tests.unit.services.lan_tournament.test_service_errors_are_static_msgids
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import ast
import pathlib

import pytest


_MODULE = pathlib.Path('byceps/services/lan_tournament')
_SERVICES = sorted(_MODULE.glob('*.py'))

# Computed texts that never reach `gettext`: the CLI-only log purge and the
# start check, which joins raw bracket-structure invariant violations.
_ALLOWED = (
    ('tournament_log_service.py', 'failed to purge log entries'),
    ('tournament_service.py', 'Cannot start tournament: '),
)


def _dynamic_errors(path):
    """Yield `(line, source)` for `Err(...)` with a computed message."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if getattr(func, 'id', getattr(func, 'attr', None)) != 'Err':
            continue
        arg = node.args[0]
        computed = isinstance(arg, ast.JoinedStr) or (
            isinstance(arg, ast.BinOp)
            and isinstance(arg.op, (ast.Add, ast.Mod))
        )
        if isinstance(arg, ast.Call) and getattr(arg.func, 'attr', None) in {
            'format',
            'join',
        }:
            computed = True
        source = ast.unparse(arg)
        allowed = any(
            path.name == name and marker in source for name, marker in _ALLOWED
        )
        if computed and not allowed:
            yield node.lineno, source


def test_services_are_found():
    assert _SERVICES


@pytest.mark.parametrize('path', _SERVICES, ids=lambda p: p.name)
def test_service_err_is_a_static_msgid(path):
    """A computed `Err` text has no msgid, so the view cannot translate it."""
    assert list(_dynamic_errors(path)) == []
