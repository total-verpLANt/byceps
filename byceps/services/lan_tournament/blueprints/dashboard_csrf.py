"""
byceps.services.lan_tournament.blueprints.dashboard_csrf
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import secrets
import string
from typing import TypeGuard

from flask import session
from flask_babel import lazy_gettext

from byceps.services.authn.session.models import CurrentUser
from byceps.util.result import Err, Ok, Result


CSRF_SESSION_KEY = 'lan_tournament_dashboard_csrf'
CSRF_TOKEN_LENGTH = 43  # `token_urlsafe(32)`, without padding
CSRF_INVALID_ERROR = 'csrf_invalid'
CSRF_INVALID_NOTICE = lazy_gettext(
    'The form check has expired. Please reload the page;'
    ' your draft stays visible.'
)

_TOKEN_CHARACTERS = frozenset(string.ascii_letters + string.digits + '-_')


def get_dashboard_csrf_token(user: CurrentUser) -> str:
    """Return the token of this session and user, minting it if needed.

    The token is stored in the signed session next to the user ID. Another
    user in the same session replaces it. An anonymous user gets an empty
    token and no session entry.
    """
    if not user.authenticated:
        return ''

    token = _find_stored_token(user)
    if token is not None:
        return token

    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = {'user_id': str(user.id), 'token': token}
    return token


def validate_dashboard_csrf(
    user: CurrentUser, token: str | None
) -> Result[None, str]:
    """Check `token` against the session token of `user`.

    Never mints, rotates or otherwise writes the session.
    """
    if not user.authenticated or not _is_valid_token(token):
        return Err(CSRF_INVALID_ERROR)

    stored_token = _find_stored_token(user)
    if stored_token is None or not secrets.compare_digest(stored_token, token):
        return Err(CSRF_INVALID_ERROR)

    return Ok(None)


def _find_stored_token(user: CurrentUser) -> str | None:
    stored = session.get(CSRF_SESSION_KEY)
    if not isinstance(stored, dict) or stored.get('user_id') != str(user.id):
        return None

    token = stored.get('token')
    if not _is_valid_token(token):
        return None

    return token


def _is_valid_token(token: object) -> TypeGuard[str]:
    return (
        isinstance(token, str)
        and len(token) == CSRF_TOKEN_LENGTH
        and all(character in _TOKEN_CHARACTERS for character in token)
    )
