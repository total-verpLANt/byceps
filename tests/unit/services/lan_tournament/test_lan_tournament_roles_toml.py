"""
tests.unit.services.lan_tournament.test_lan_tournament_roles_toml
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from pathlib import Path
import tomllib


TOML_PATH = (
    Path(__file__).resolve().parents[4]
    / 'scripts'
    / 'data'
    / 'lan_tournament_roles.toml'
)


def _load_role(role_id: str) -> dict:
    data = tomllib.loads(TOML_PATH.read_text())
    return next(role for role in data['roles'] if role['id'] == role_id)


def test_lan_tournament_admin_lists_request_permissions():
    role = _load_role('lan_tournament_admin')
    permissions = role['assigned_permissions']

    assert 'lan_tournament.request_view' in permissions
    assert 'lan_tournament.request_decide' in permissions


def test_lan_tournament_viewer_does_not_list_request_permissions():
    role = _load_role('lan_tournament_viewer')
    permissions = role['assigned_permissions']

    assert 'lan_tournament.request_view' not in permissions
    assert 'lan_tournament.request_decide' not in permissions


def test_lan_tournament_admin_lists_maintain_permission():
    role = _load_role('lan_tournament_admin')

    assert 'lan_tournament.maintain' in role['assigned_permissions']


def test_lan_tournament_viewer_does_not_list_maintain_permission():
    role = _load_role('lan_tournament_viewer')

    assert 'lan_tournament.maintain' not in role['assigned_permissions']
