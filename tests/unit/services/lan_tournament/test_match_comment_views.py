"""
tests.unit.services.lan_tournament.test_match_comment_views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for match comment submission site views.

Routes under test:
  POST /matches/<match_id>/add_comment  -- add_comment
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_match_service import (
    MatchUserRole,
)
from byceps.util.result import Ok

from tests.helpers import generate_uuid


# ------------------------------------------------------------------ #
# IDs
# ------------------------------------------------------------------ #

TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())
MATCH_ID_STR = str(MATCH_ID)
PARTY_ID_STR = str(generate_uuid())
CONTESTANT_USER_ID = generate_uuid()
NON_CONTESTANT_USER_ID = generate_uuid()
ADMIN_USER_ID = generate_uuid()

_V = 'byceps.services.lan_tournament.blueprints.site.views'


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app for test_request_context."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _make_tournament(
    status: TournamentStatus = TournamentStatus.ONGOING,
) -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.party_id = PARTY_ID_STR
    t.name = 'Test Tournament'
    t.tournament_status = status
    t.tournament_mode = TournamentMode.SINGLE_ELIMINATION
    t.contestant_type = ContestantType.TEAM
    t.max_players = None
    return t


def _make_match(confirmed: bool = False) -> MagicMock:
    m = MagicMock(spec=TournamentMatch)
    m.id = MATCH_ID
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = generate_uuid() if confirmed else None
    return m


def _make_contestant() -> MagicMock:
    c = MagicMock(spec=TournamentMatchToContestant)
    c.participant_id = generate_uuid()
    c.team_id = None
    c.score = None
    return c


def _make_user(user_id, *, authenticated=True, permissions=None):
    u = MagicMock()
    u.id = user_id
    u.screen_name = 'TestUser'
    u.authenticated = authenticated
    u.permissions = permissions or frozenset()
    return u


@contextmanager
def _patched_comment_view(
    app,
    *,
    tournament_status=TournamentStatus.ONGOING,
    current_user_id=CONTESTANT_USER_ID,
    authenticated=True,
    is_contestant=True,
    permissions=None,
):
    """Patch view dependencies for add_comment route."""
    tournament = _make_tournament(status=tournament_status)
    match = _make_match()
    contestant = _make_contestant()

    role_with_contestant = MatchUserRole(contestant, False, False, False)
    role_without_contestant = MatchUserRole(None, False, False, False)

    with app.app_context():
        with (
            patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
            patch(f'{_V}.flash_error') as mock_flash_error,
            patch(f'{_V}.flash_success') as mock_flash_success,
            patch(f'{_V}.redirect_to') as mock_redirect_to,
            patch(f'{_V}.tournament_match_service') as mock_match_svc,
            patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
            patch(f'{_V}.g') as mock_g,
            patch(
                f'{_V}.has_current_user_permission'
            ) as mock_has_perm,
        ):
            mock_get_tournament.return_value = tournament
            mock_match_svc.get_match.return_value = match
            mock_match_svc.get_contestants_for_match.return_value = [contestant]
            mock_match_svc.get_user_match_role.return_value = (
                role_with_contestant if is_contestant else role_without_contestant
            )
            mock_match_svc.add_comment.return_value = Ok(None)
            mock_match_svc.TournamentMatchID = TournamentMatchID

            mock_g.user = _make_user(
                current_user_id,
                authenticated=authenticated,
                permissions=permissions,
            )
            mock_g.party = MagicMock()
            mock_g.party.id = PARTY_ID_STR

            user_perms = permissions or frozenset()
            mock_has_perm.side_effect = lambda p: p in user_perms

            mock_redirect_to.return_value = 'redirected'

            yield {
                'flash_error': mock_flash_error,
                'flash_success': mock_flash_success,
                'redirect_to': mock_redirect_to,
                'match_svc': mock_match_svc,
                'get_tournament': mock_get_tournament,
                'g': mock_g,
                'has_perm': mock_has_perm,
                'tournament': tournament,
                'match': match,
            }


# ------------------------------------------------------------------ #
# 1. Happy path — contestant posts comment
# ------------------------------------------------------------------ #


def test_add_comment_as_contestant_succeeds(app):
    """Authenticated match contestant can post a comment."""
    with _patched_comment_view(app, is_contestant=True) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.add_comment.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'comment': 'GG well played'},
        ):
            raw_fn(MATCH_ID_STR)

        mocks['match_svc'].add_comment.assert_called_once()
        call_args = mocks['match_svc'].add_comment.call_args
        assert call_args[0][2] == 'GG well played'
        mocks['flash_success'].assert_called_once()


# ------------------------------------------------------------------ #
# 2. Happy path — admin (non-contestant) posts comment
# ------------------------------------------------------------------ #


def test_add_comment_as_admin_succeeds(app):
    """User with lan_tournament.administrate can comment even if not contestant."""
    with _patched_comment_view(
        app,
        current_user_id=ADMIN_USER_ID,
        is_contestant=False,
        permissions=frozenset({'lan_tournament.administrate'}),
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.add_comment.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'comment': 'Admin note: match reviewed'},
        ):
            raw_fn(MATCH_ID_STR)

        mocks['match_svc'].add_comment.assert_called_once()
        mocks['flash_success'].assert_called_once()


# ------------------------------------------------------------------ #
# 3. Non-contestant, non-admin → 403
# ------------------------------------------------------------------ #


def test_add_comment_non_contestant_non_admin_returns_403(app):
    """User not in match and without admin perm gets 403."""
    with _patched_comment_view(
        app,
        current_user_id=NON_CONTESTANT_USER_ID,
        is_contestant=False,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.add_comment.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'comment': 'Should not work'},
        ):
            with pytest.raises(Forbidden):
                raw_fn(MATCH_ID_STR)

        mocks['match_svc'].add_comment.assert_not_called()


# ------------------------------------------------------------------ #
# 4. Unauthenticated → login_required decorator present
# ------------------------------------------------------------------ #


def test_add_comment_requires_login(app):
    """add_comment has @login_required decorator."""
    from byceps.services.lan_tournament.blueprints.site import views

    assert hasattr(views.add_comment, '__wrapped__')


# ------------------------------------------------------------------ #
# 5. Tournament not ONGOING → flash error
# ------------------------------------------------------------------ #


def test_add_comment_tournament_not_ongoing_flashes_error(app):
    """Comment rejected when tournament is not ONGOING."""
    with _patched_comment_view(
        app,
        tournament_status=TournamentStatus.COMPLETED,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.add_comment.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'comment': 'Too late'},
        ):
            raw_fn(MATCH_ID_STR)

        mocks['flash_error'].assert_called_once()
        mocks['match_svc'].add_comment.assert_not_called()


# ------------------------------------------------------------------ #
# 6. Empty comment body → validation failure
# ------------------------------------------------------------------ #


def test_add_comment_empty_body_flashes_error(app):
    """Empty comment fails form validation."""
    with _patched_comment_view(app, is_contestant=True) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.add_comment.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'comment': ''},
        ):
            raw_fn(MATCH_ID_STR)

        mocks['flash_error'].assert_called_once()
        mocks['match_svc'].add_comment.assert_not_called()


# ------------------------------------------------------------------ #
# 7. Comment exceeds max length → validation failure
# ------------------------------------------------------------------ #


def test_add_comment_exceeds_max_length_flashes_error(app):
    """Comment over 1000 chars fails form validation."""
    with _patched_comment_view(app, is_contestant=True) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.add_comment.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'comment': 'x' * 1001},
        ):
            raw_fn(MATCH_ID_STR)

        mocks['flash_error'].assert_called_once()
        mocks['match_svc'].add_comment.assert_not_called()
