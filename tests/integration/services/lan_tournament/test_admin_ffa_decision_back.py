"""
tests.integration.services.lan_tournament.test_admin_ffa_decision_back
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drives where an FFA cut tie decision of the admin returns to: the page it
was posted from, chosen from a whitelist of endpoints.
"""

import pytest

from tests.helpers import log_in_user
from tests.integration.services.lan_tournament import (
    test_ffa_cut_tie_routes as base,
)


make_ffa = base.make_ffa
_decision_forms = base._decision_forms
_get = base._get
_lobby_scopes = base._lobby_scopes
_post = base._post
_save_form = base._save_form
ADMIN_URL = base.ADMIN_URL


@pytest.fixture(scope='module')
def players(make_user):
    return [make_user(f'AdminFfaBackPlayer{i}') for i in range(8)]


@pytest.fixture(scope='module')
def admin(make_admin):
    user = make_admin(
        {'admin.access', 'lan_tournament.administrate', 'lan_tournament.view'}
    )
    log_in_user(user.id)
    return user


@pytest.fixture(scope='module')
def orga(make_user):
    user = make_user('AdminFfaBackOrga')
    log_in_user(user.id)
    return user


def _page_url(tournament, page):
    return f'{ADMIN_URL}/tournaments/{tournament.id}/{page}'


@pytest.mark.parametrize('page', ['bracket', 'ffa_standings'])
def test_a_decision_returns_to_the_page_it_was_posted_from(
    admin_app,
    admin,
    make_ffa,
    page,
):
    tournament = make_ffa()
    scope = _lobby_scopes(tournament)[0]
    html = _get(admin_app, admin, _page_url(tournament, page)).get_data(
        as_text=True
    )
    action, data = _save_form(html, scope)

    response = _post(admin_app, admin, action, data)

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/{tournament.id}/{page}')


@pytest.mark.parametrize('page', ['bracket', 'ffa_standings'])
def test_a_withdrawal_returns_to_the_page_it_was_posted_from(
    admin_app,
    admin,
    make_ffa,
    page,
):
    tournament = make_ffa()
    scope = _lobby_scopes(tournament)[0]
    html = _get(admin_app, admin, _page_url(tournament, page)).get_data(
        as_text=True
    )
    action, data = _save_form(html, scope)
    _post(admin_app, admin, action, data)
    html = _get(admin_app, admin, _page_url(tournament, page)).get_data(
        as_text=True
    )
    withdraw_action, withdraw_data = next(
        form
        for form in _decision_forms(html, scope)
        if form[1].get('action') == 'withdraw'
    )

    response = _post(
        admin_app,
        admin,
        withdraw_action,
        {**withdraw_data, 'reason': 'Counted wrong.'},
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/{tournament.id}/{page}')


@pytest.mark.parametrize(
    'back',
    [
        '',
        'qualification',
        '.qualification',
        'https://evil.example/',
        '//evil.example/',
        '../../etc',
    ],
)
def test_an_unlisted_back_value_falls_back_to_the_bracket(
    admin_app,
    admin,
    make_ffa,
    back,
):
    tournament = make_ffa()
    scope = _lobby_scopes(tournament)[0]
    html = _get(admin_app, admin, _page_url(tournament, 'bracket')).get_data(
        as_text=True
    )
    action, data = _save_form(html, scope)

    response = _post(admin_app, admin, action, {**data, 'back': back})

    assert response.status_code == 302
    location = response.headers['Location']
    assert location.endswith(f'/{tournament.id}/bracket')
    assert 'evil' not in location
