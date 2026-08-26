from unittest.mock import Mock

from flask import Flask, session
import pytest

from byceps.services.lan_tournament.blueprints import readiness_csrf as csrf
from byceps.services.user.models import UserID
from byceps.util.uuid import generate_uuid7


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'readiness-unit-test-only'

    @app.route('/token/<uuid:user_id>')
    def token(user_id):
        return csrf.get_readiness_csrf_token(UserID(user_id))

    @app.route('/validate/<uuid:user_id>', methods=['POST'])
    def validate(user_id):
        from flask import request

        result = csrf.validate_readiness_csrf_token(
            request.form.get('csrf_token'), UserID(user_id)
        )
        if result.is_err():
            return result.unwrap_err(), 403
        return 'valid'

    return app


def test_token_is_session_and_user_bound(app):
    user = UserID(generate_uuid7())
    other_user = UserID(generate_uuid7())
    first, second = app.test_client(), app.test_client()
    token = first.get(f'/token/{user}').text
    other_token = second.get(f'/token/{user}').text
    assert token != other_token
    assert len(token) == csrf.READINESS_CSRF_TOKEN_LENGTH
    assert first.get(f'/token/{user}').text == token
    assert first.post(f'/validate/{user}', data={'csrf_token': token}).status_code == 200
    for client, actor in [(second, user), (first, other_user)]:
        response = client.post(f'/validate/{actor}', data={'csrf_token': token})
        assert response.status_code == 403
        assert response.text == 'csrf_invalid'


def test_identity_change_rotates_token(app):
    first, second = UserID(generate_uuid7()), UserID(generate_uuid7())
    client = app.test_client()
    original = client.get(f'/token/{first}').text
    replacement = client.get(f'/token/{second}').text
    assert replacement != original
    assert client.post(f'/validate/{first}', data={'csrf_token': original}).text == 'csrf_invalid'
    assert client.post(f'/validate/{second}', data={'csrf_token': original}).text == 'csrf_invalid'
    assert client.post(f'/validate/{second}', data={'csrf_token': replacement}).status_code == 200
    assert client.get(f'/token/{first}').text not in {original, replacement}


def test_token_comparison_is_constant_time(app, monkeypatch):
    user = UserID(generate_uuid7())
    real_compare = csrf.secrets.compare_digest
    compare = Mock(wraps=real_compare)
    monkeypatch.setattr(csrf.secrets, 'compare_digest', compare)
    with app.test_request_context():
        token = csrf.get_readiness_csrf_token(user)
        assert csrf.validate_readiness_csrf_token(token, user).is_ok()
        compare.assert_called_once_with(token, token)
        compare.reset_mock()
        wrong = ('A' if token[0] != 'A' else 'B') + token[1:]
        assert csrf.validate_readiness_csrf_token(wrong, user).unwrap_err() == 'csrf_invalid'
        compare.assert_called_once_with(token, wrong)


# fmt: off
@pytest.mark.parametrize('token', [
    None, '', 'wrong', 'a' * 42, 'a' * 44, 'a' * 100_000,
    'é' * 43, '!' * 43, b'a' * 43, 123, ['a' * 43], {'token': 'a' * 43},
])
# fmt: on
def test_missing_wrong_and_malformed_tokens_return_csrf_invalid(app, monkeypatch, token):
    user = UserID(generate_uuid7())
    compare = Mock(side_effect=AssertionError('Malformed token reached comparison'))
    monkeypatch.setattr(csrf.secrets, 'compare_digest', compare)
    with app.test_request_context():
        csrf.get_readiness_csrf_token(user)
        before = dict(session)
        assert csrf.validate_readiness_csrf_token(token, user).unwrap_err() == 'csrf_invalid'
        assert dict(session) == before
        compare.assert_not_called()


def test_missing_session_returns_csrf_invalid_without_creating_token(app):
    user = UserID(generate_uuid7())
    with app.test_request_context():
        assert csrf.validate_readiness_csrf_token('a' * 43, user).unwrap_err() == 'csrf_invalid'
        assert not session


# fmt: off
@pytest.mark.parametrize('stored', [
    None, 'a' * 43, {}, {'user_id': 'foreign', 'token': 'a' * 43},
    {'token': 'a' * 43}, {'user_id': 'current', 'token': 'é' * 43},
    {'user_id': 'current', 'token': 'a' * 44},
])
# fmt: on
def test_invalid_session_state_refused_and_replaced(app, stored):
    user = UserID(generate_uuid7())
    if isinstance(stored, dict) and stored.get('user_id') == 'current':
        stored = {**stored, 'user_id': str(user)}
    with app.test_request_context():
        session[csrf.READINESS_CSRF_SESSION_KEY] = stored
        assert csrf.validate_readiness_csrf_token('a' * 43, user).unwrap_err() == 'csrf_invalid'
        token = csrf.get_readiness_csrf_token(user)
        assert csrf.validate_readiness_csrf_token(token, user).is_ok()


def test_token_generation_uses_32_random_bytes(app, monkeypatch):
    random = Mock(return_value='a' * 43)
    monkeypatch.setattr(csrf.secrets, 'token_urlsafe', random)
    with app.test_request_context():
        user = UserID(generate_uuid7())
        assert csrf.get_readiness_csrf_token(user) == 'a' * 43
        assert csrf.get_readiness_csrf_token(user) == 'a' * 43
        random.assert_called_once_with(32)
