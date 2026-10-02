"""
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from tests.helpers import log_in_user


@pytest.fixture(scope='package')
def chair_admin(make_admin):
    admin = make_admin({'admin.access', 'seating.view'})
    log_in_user(admin.id)
    return admin


@pytest.fixture(scope='package')
def chair_admin_client(make_client, admin_app, chair_admin):
    return make_client(admin_app, user_id=chair_admin.id)


@pytest.fixture(scope='package')
def admin_without_seating_permission(make_admin):
    admin = make_admin({'admin.access'})
    log_in_user(admin.id)
    return admin


@pytest.fixture(scope='package')
def unauthorized_chair_admin_client(
    make_client, admin_app, admin_without_seating_permission
):
    return make_client(admin_app, user_id=admin_without_seating_permission.id)
