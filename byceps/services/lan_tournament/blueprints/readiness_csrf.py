"""Module-local CSRF tokens bound to the signed Flask session and user."""

import secrets
import string

from flask import session

from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok, Result


READINESS_CSRF_SESSION_KEY = 'lan_tournament_readiness_csrf'
READINESS_CSRF_TOKEN_LENGTH = 43  # token_urlsafe(32), without padding
_TOKEN_CHARACTERS = frozenset(string.ascii_letters + string.digits + '-_')


def get_readiness_csrf_token(user_id: UserID) -> str:
    """Reuse a valid token, rotating it whenever the user identity changes."""
    stored = session.get(READINESS_CSRF_SESSION_KEY)
    if (
        isinstance(stored, dict)
        and stored.get('user_id') == str(user_id)
        and _is_valid_token(stored.get('token'))
    ):
        return stored['token']
    token = secrets.token_urlsafe(32)
    session[READINESS_CSRF_SESSION_KEY] = {
        'user_id': str(user_id), 'token': token
    }
    return token


def validate_readiness_csrf_token(
    token: str | None, user_id: UserID
) -> Result[None, str]:
    """Reject malformed/foreign tokens without generating or rotating tokens."""
    if not _is_valid_token(token):
        return Err('csrf_invalid')
    stored = session.get(READINESS_CSRF_SESSION_KEY)
    if (
        not isinstance(stored, dict)
        or stored.get('user_id') != str(user_id)
        or not _is_valid_token(stored.get('token'))
        or not secrets.compare_digest(stored['token'], token)
    ):
        return Err('csrf_invalid')
    return Ok(None)


def _is_valid_token(token: object) -> bool:
    return (
        isinstance(token, str)
        and len(token) == READINESS_CSRF_TOKEN_LENGTH
        and all(character in _TOKEN_CHARACTERS for character in token)
    )
