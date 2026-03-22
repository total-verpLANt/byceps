"""
:Copyright: 2014-2026 Jochen Kupperschmidt
:License: Revised BSD (see `LICENSE` file for details)
"""

import pytest

from tests.helpers import log_in_user


@pytest.fixture(scope='package')
def site_user(make_user):
    user = make_user()
    log_in_user(user.id)
    return user


@pytest.fixture(scope='package')
def site_client(make_client, site_app, site_user):
    return make_client(site_app, user_id=site_user.id)
