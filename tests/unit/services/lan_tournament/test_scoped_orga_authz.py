"""
tests.unit.services.lan_tournament.test_scoped_orga_authz
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, UTC
from pathlib import Path
import re
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest

import byceps
from byceps.services.lan_tournament.blueprints.site import authz
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.user.models import UserID

from tests.helpers import generate_uuid


MOCK_PREFIX = 'byceps.services.lan_tournament.blueprints.site.authz'

TOURNAMENT_ID = TournamentID(generate_uuid())
OTHER_TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())

_ADMIN_VIEWS_PATH = (
    Path(byceps.__file__).parent
    / 'services'
    / 'lan_tournament'
    / 'blueprints'
    / 'admin'
    / 'views.py'
)


# -------------------------------------------------------------------- #
# helpers
# -------------------------------------------------------------------- #


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app for `test_request_context`."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    return a


def _make_user(*, permissions=frozenset(), authenticated=True) -> MagicMock:
    user = MagicMock()
    user.id = UserID(generate_uuid())
    user.authenticated = authenticated
    user.has_permission.side_effect = lambda permission: (
        permission in permissions
    )
    return user


def _make_match(
    *,
    match_id: TournamentMatchID = MATCH_ID,
    tournament_id: TournamentID = TOURNAMENT_ID,
) -> TournamentMatch:
    return TournamentMatch(
        id=match_id,
        tournament_id=tournament_id,
        group_order=None,
        match_order=0,
        round=0,
        next_match_id=None,
        confirmed_by=None,
        created_at=datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC),
    )


def _read_admin_views_source() -> str:
    """Return the admin blueprint's views source."""
    return _ADMIN_VIEWS_PATH.read_text()


# -------------------------------------------------------------------- #
# may_administrate_tournament
# -------------------------------------------------------------------- #


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
def test_scoped_orga_may_administrate_own_tournament(mock_orga_svc):
    user = _make_user()
    mock_orga_svc.is_orga_for_tournament.return_value = True

    result = authz.may_administrate_tournament(user, TOURNAMENT_ID)

    assert result is True
    mock_orga_svc.is_orga_for_tournament.assert_called_once_with(
        user.id, TOURNAMENT_ID
    )


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
def test_scoped_orga_may_not_administrate_other_tournament(mock_orga_svc):
    user = _make_user()
    mock_orga_svc.is_orga_for_tournament.return_value = False

    result = authz.may_administrate_tournament(user, OTHER_TOURNAMENT_ID)

    assert result is False
    mock_orga_svc.is_orga_for_tournament.assert_called_once_with(
        user.id, OTHER_TOURNAMENT_ID
    )


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
def test_global_admin_may_administrate_any_tournament(mock_orga_svc):
    user = _make_user(permissions=frozenset({'lan_tournament.administrate'}))
    mock_orga_svc.is_orga_for_tournament.return_value = False

    result = authz.may_administrate_tournament(user, TOURNAMENT_ID)

    assert result is True
    mock_orga_svc.is_orga_for_tournament.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
def test_anonymous_visitor_may_not_administrate_without_a_query(
    mock_orga_svc,
):
    user = _make_user(authenticated=False)

    result = authz.may_administrate_tournament(user, TOURNAMENT_ID)

    assert result is False
    mock_orga_svc.is_orga_for_tournament.assert_not_called()
    user.has_permission.assert_not_called()


@patch(f'{MOCK_PREFIX}.tournament_orga_service')
def test_plain_user_may_not_administrate(mock_orga_svc):
    user = _make_user()
    mock_orga_svc.is_orga_for_tournament.return_value = False

    result = authz.may_administrate_tournament(user, TOURNAMENT_ID)

    assert result is False


# -------------------------------------------------------------------- #
# scoped_orga_required
# -------------------------------------------------------------------- #


def test_scoped_orga_route_returns_403_for_non_orga(app):
    from werkzeug.exceptions import Forbidden

    view = MagicMock(return_value='ok')
    decorated = authz.scoped_orga_required(view)

    user = _make_user()

    with app.test_request_context('/'):
        with (
            patch(f'{MOCK_PREFIX}.g') as mock_g,
            patch(f'{MOCK_PREFIX}.tournament_orga_service') as mock_orga_svc,
        ):
            mock_g.user = user
            mock_orga_svc.is_orga_for_tournament.return_value = False

            with pytest.raises(Forbidden):
                decorated(tournament_id=TOURNAMENT_ID)

    view.assert_not_called()


def test_scoped_orga_route_allows_assigned_orga(app):
    view = MagicMock(return_value='ok')
    decorated = authz.scoped_orga_required(view)

    user = _make_user()

    with app.test_request_context('/'):
        with (
            patch(f'{MOCK_PREFIX}.g') as mock_g,
            patch(f'{MOCK_PREFIX}.tournament_orga_service') as mock_orga_svc,
        ):
            mock_g.user = user
            mock_orga_svc.is_orga_for_tournament.return_value = True

            result = decorated(tournament_id=TOURNAMENT_ID)

    assert result == 'ok'
    view.assert_called_once_with(tournament_id=TOURNAMENT_ID)


def test_scoped_orga_decorator_resolves_tournament_from_match_id(app):
    view = MagicMock(return_value='ok')
    decorated = authz.scoped_orga_required(view)

    user = _make_user(permissions=frozenset({'lan_tournament.administrate'}))
    match = _make_match(tournament_id=TOURNAMENT_ID)

    with app.test_request_context('/'):
        with (
            patch(f'{MOCK_PREFIX}.g') as mock_g,
            patch(f'{MOCK_PREFIX}.tournament_match_service') as mock_match_svc,
            patch(f'{MOCK_PREFIX}.tournament_orga_service'),
        ):
            mock_g.user = user
            mock_match_svc.get_match.return_value = match

            result = decorated(match_id=str(MATCH_ID))

    assert result == 'ok'
    mock_match_svc.get_match.assert_called_once_with(MATCH_ID)
    view.assert_called_once_with(match_id=str(MATCH_ID))


def test_scoped_orga_decorator_missing_match_yields_404(app):
    """Answer 404, not 403, so the match's existence does not leak."""
    from werkzeug.exceptions import NotFound

    view = MagicMock(return_value='ok')
    decorated = authz.scoped_orga_required(view)

    user = _make_user(permissions=frozenset({'lan_tournament.administrate'}))

    with app.test_request_context('/'):
        with (
            patch(f'{MOCK_PREFIX}.g') as mock_g,
            patch(f'{MOCK_PREFIX}.tournament_match_service') as mock_match_svc,
        ):
            mock_g.user = user
            mock_match_svc.get_match.side_effect = ValueError(
                'Unknown match ID'
            )

            with pytest.raises(NotFound):
                decorated(match_id=str(MATCH_ID))

    view.assert_not_called()


def test_scoped_orga_decorator_malformed_tournament_id_yields_404(app):
    from werkzeug.exceptions import NotFound

    view = MagicMock(return_value='ok')
    decorated = authz.scoped_orga_required(view)

    user = _make_user(permissions=frozenset({'lan_tournament.administrate'}))

    with app.test_request_context('/'):
        with (
            patch(f'{MOCK_PREFIX}.g') as mock_g,
            patch(f'{MOCK_PREFIX}.tournament_orga_service') as mock_orga_svc,
        ):
            mock_g.user = user

            with pytest.raises(NotFound):
                decorated(tournament_id='not-a-uuid')

    view.assert_not_called()
    mock_orga_svc.is_orga_for_tournament.assert_not_called()


def test_scoped_orga_cannot_reach_admin_blueprint():
    admin_source = _read_admin_views_source()

    assert 'scoped_orga_required' not in admin_source
    assert 'may_administrate_tournament' not in admin_source


def test_admin_permission_sites_unchanged():
    """Keep every admin route on a global permission literal."""
    admin_source = _read_admin_views_source()

    decorator_args = re.findall(
        r'@permission_required\(([^)]*)\)', admin_source
    )

    assert len(decorator_args) >= 50
    assert 'scoped_orga_required' not in admin_source

    for args in decorator_args:
        assert re.fullmatch(r"\s*'[\w.]+'\s*", args), args
