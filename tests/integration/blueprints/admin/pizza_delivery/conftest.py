"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from tests.helpers import log_in_user


@pytest.fixture(scope='package')
def pizza_admin(make_admin):
    permission_ids = {
        'admin.access',
        'ticketing.checkin',
    }
    admin = make_admin(permission_ids)
    log_in_user(admin.id)
    return admin


@pytest.fixture(scope='package')
def pizza_admin_client(make_client, admin_app, pizza_admin):
    return make_client(admin_app, user_id=pizza_admin.id)
