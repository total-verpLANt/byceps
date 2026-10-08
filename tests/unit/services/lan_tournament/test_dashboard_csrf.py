from unittest.mock import Mock

from flask import Flask, request, session
import pytest

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament.blueprints import (
    dashboard_csrf as csrf,
    readiness_csrf,
)
from byceps.services.user.models import User, UserID
from byceps.util.uuid import generate_uuid7


def _authenticated(user_id: UserID) -> CurrentUser:
    return CurrentUser.create_authenticated(
        User(
            id=user_id,
            screen_name='Orga',
            initialized=True,
            suspended=False,
            deleted=False,
            avatar_url='',
        ),
        None,
        frozenset(),
    )


def _anonymous() -> CurrentUser:
    return CurrentUser.create_anonymous(None)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'dashboard-unit-test-only'

    @app.route('/token/<uuid:user_id>')
    def token(user_id):
        return csrf.get_dashboard_csrf_token(_authenticated(UserID(user_id)))

    @app.route('/anonymous-token')
    def anonymous_token():
        return csrf.get_dashboard_csrf_token(_anonymous()) or 'empty'

    @app.route('/readiness-token/<uuid:user_id>')
    def readiness_token(user_id):
        return readiness_csrf.get_readiness_csrf_token(UserID(user_id))

    @app.route('/validate/<uuid:user_id>', methods=['POST'])
    def validate(user_id):
        result = csrf.validate_dashboard_csrf(
            _authenticated(UserID(user_id)), request.form.get('csrf_token')
        )
        if result.is_err():
            return result.unwrap_err(), 403
        return 'valid'

    @app.route('/validate-anonymous', methods=['POST'])
    def validate_anonymous():
        result = csrf.validate_dashboard_csrf(
            _anonymous(), request.form.get('csrf_token')
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
    other_session_token = second.get(f'/token/{user}').text

    assert len(token) == csrf.CSRF_TOKEN_LENGTH
    assert token != other_session_token
    assert first.get(f'/token/{user}').text == token
    assert (
        first.post(f'/validate/{user}', data={'csrf_token': token}).text
        == 'valid'
    )
    # The same user in another session, and another user in this session.
    for client, actor in [(second, user), (first, other_user)]:
        response = client.post(f'/validate/{actor}', data={'csrf_token': token})
        assert response.status_code == 403
        assert response.text == csrf.CSRF_INVALID_ERROR


# fmt: off
@pytest.mark.parametrize('token', [
    None, '', 'wrong', 'a' * 42, 'a' * 44, 'a' * 100_000,
    'é' * 43, '!' * 43, ' ' * 43, ('a' * 42) + '\n',
    b'a' * 43, 123, ['a' * 43], {'token': 'a' * 43},
])
# fmt: on
def test_missing_wrong_and_foreign_token_refused(app, monkeypatch, token):
    user = _authenticated(UserID(generate_uuid7()))
    compare = Mock(side_effect=AssertionError('A malformed token was compared'))
    with app.test_request_context():
        csrf.get_dashboard_csrf_token(user)
        before = dict(session)
        monkeypatch.setattr(csrf.secrets, 'compare_digest', compare)

        result = csrf.validate_dashboard_csrf(user, token)

        assert result.unwrap_err() == csrf.CSRF_INVALID_ERROR
        assert dict(session) == before
        compare.assert_not_called()


def test_well_formed_wrong_token_is_compared_and_refused(app):
    user = _authenticated(UserID(generate_uuid7()))
    with app.test_request_context():
        token = csrf.get_dashboard_csrf_token(user)
        wrong = ('A' if token[0] != 'A' else 'B') + token[1:]

        assert (
            csrf.validate_dashboard_csrf(user, wrong).unwrap_err()
            == csrf.CSRF_INVALID_ERROR
        )
        assert csrf.validate_dashboard_csrf(user, token).is_ok()


def test_foreign_tokens_of_other_users_sessions_and_purposes_are_refused(app):
    user = UserID(generate_uuid7())
    other_user = UserID(generate_uuid7())
    victim, attacker = app.test_client(), app.test_client()
    victim_token = victim.get(f'/token/{user}').text
    attacker_token = attacker.get(f'/token/{other_user}').text
    readiness_token = victim.get(f'/readiness-token/{user}').text

    def post(client, actor, token):
        return client.post(
            f'/validate/{actor}', data={'csrf_token': token}
        ).text

    # A token of the victim's session, replayed from the attacker's session.
    assert post(attacker, other_user, victim_token) == csrf.CSRF_INVALID_ERROR
    # The attacker's own token, replayed in the victim's session.
    assert post(victim, user, attacker_token) == csrf.CSRF_INVALID_ERROR
    # The readiness token of the same user and session is another purpose.
    assert readiness_token != victim_token
    assert post(victim, user, readiness_token) == csrf.CSRF_INVALID_ERROR
    assert post(victim, user, victim_token) == 'valid'


def test_missing_session_refuses_without_creating_a_token(app):
    user = UserID(generate_uuid7())
    with app.test_request_context():
        result = csrf.validate_dashboard_csrf(_authenticated(user), 'a' * 43)

        assert result.unwrap_err() == csrf.CSRF_INVALID_ERROR
        assert not session


def test_anonymous_gets_no_token_and_can_never_validate(app):
    client = app.test_client()
    user = UserID(generate_uuid7())
    token = client.get(f'/token/{user}').text

    response = app.test_client().get('/anonymous-token')
    assert response.text == 'empty'
    assert 'Set-Cookie' not in response.headers
    # The token minted for a logged-in user does not carry over to a user who
    # is anonymous in the same session.
    response = client.post('/validate-anonymous', data={'csrf_token': token})
    assert response.status_code == 403
    assert response.text == csrf.CSRF_INVALID_ERROR

    with app.test_request_context():
        assert csrf.get_dashboard_csrf_token(_anonymous()) == ''
        assert not session


def test_anonymous_never_validates_even_a_forged_session_entry(app):
    anonymous = _anonymous()
    with app.test_request_context():
        session[csrf.CSRF_SESSION_KEY] = {
            'user_id': str(anonymous.id),
            'token': 'a' * 43,
        }

        result = csrf.validate_dashboard_csrf(anonymous, 'a' * 43)

        assert result.unwrap_err() == csrf.CSRF_INVALID_ERROR


def test_identity_change_rotates_token(app):
    first, second = UserID(generate_uuid7()), UserID(generate_uuid7())
    client = app.test_client()

    original = client.get(f'/token/{first}').text
    replacement = client.get(f'/token/{second}').text

    assert replacement != original
    assert len(replacement) == csrf.CSRF_TOKEN_LENGTH

    def post(actor, token):
        return client.post(
            f'/validate/{actor}', data={'csrf_token': token}
        ).text

    assert post(first, original) == csrf.CSRF_INVALID_ERROR
    assert post(second, original) == csrf.CSRF_INVALID_ERROR
    assert post(first, replacement) == csrf.CSRF_INVALID_ERROR
    assert post(second, replacement) == 'valid'
    # Switching back mints a third token, never the retired one.
    returned = client.get(f'/token/{first}').text
    assert returned not in {original, replacement}
    assert post(first, returned) == 'valid'
    assert post(second, replacement) == csrf.CSRF_INVALID_ERROR


def test_csrf_comparison_is_constant_time(app, monkeypatch):
    user = _authenticated(UserID(generate_uuid7()))
    compare = Mock(wraps=csrf.secrets.compare_digest)
    monkeypatch.setattr(csrf.secrets, 'compare_digest', compare)

    with app.test_request_context():
        token = csrf.get_dashboard_csrf_token(user)
        compare.assert_not_called()

        assert csrf.validate_dashboard_csrf(user, token).is_ok()
        compare.assert_called_once_with(token, token)

        compare.reset_mock()
        wrong = ('A' if token[0] != 'A' else 'B') + token[1:]
        assert (
            csrf.validate_dashboard_csrf(user, wrong).unwrap_err()
            == csrf.CSRF_INVALID_ERROR
        )
        compare.assert_called_once_with(token, wrong)


def test_validation_never_writes_the_session(app):
    user = UserID(generate_uuid7())
    client = app.test_client()
    token = client.get(f'/token/{user}').text

    for sent in (token, 'b' * 43, '', None):
        data = {} if sent is None else {'csrf_token': sent}
        response = client.post(f'/validate/{user}', data=data)

        assert 'Set-Cookie' not in response.headers

    fresh = app.test_client()
    response = fresh.post(f'/validate/{user}', data={'csrf_token': token})
    assert response.status_code == 403
    assert 'Set-Cookie' not in response.headers


# fmt: off
@pytest.mark.parametrize('stored', [
    None, 'a' * 43, [], {}, {'token': 'a' * 43},
    {'user_id': 'foreign', 'token': 'a' * 43},
    {'user_id': 'current', 'token': 'é' * 43},
    {'user_id': 'current', 'token': 'a' * 44},
    {'user_id': 'current', 'token': None},
    {'user_id': 'current', 'token': ['a' * 43]},
])
# fmt: on
def test_invalid_session_state_is_refused_and_replaced(app, stored):
    user = _authenticated(UserID(generate_uuid7()))
    if isinstance(stored, dict) and stored.get('user_id') == 'current':
        stored = {**stored, 'user_id': str(user.id)}
    with app.test_request_context():
        session[csrf.CSRF_SESSION_KEY] = stored

        assert (
            csrf.validate_dashboard_csrf(user, 'a' * 43).unwrap_err()
            == csrf.CSRF_INVALID_ERROR
        )
        token = csrf.get_dashboard_csrf_token(user)
        assert token != 'a' * 43
        assert csrf.validate_dashboard_csrf(user, token).is_ok()


def test_token_generation_uses_32_random_bytes(app, monkeypatch):
    random = Mock(return_value='a' * 43)
    monkeypatch.setattr(csrf.secrets, 'token_urlsafe', random)
    user = _authenticated(UserID(generate_uuid7()))
    with app.test_request_context():
        assert csrf.get_dashboard_csrf_token(user) == 'a' * 43
        assert csrf.get_dashboard_csrf_token(user) == 'a' * 43

        random.assert_called_once_with(32)


def test_token_is_stored_next_to_the_user_in_a_separate_session_key(app):
    user = _authenticated(UserID(generate_uuid7()))
    with app.test_request_context():
        token = csrf.get_dashboard_csrf_token(user)
        readiness_token = readiness_csrf.get_readiness_csrf_token(user.id)

        assert session[csrf.CSRF_SESSION_KEY] == {
            'user_id': str(user.id),
            'token': token,
        }
        assert csrf.CSRF_SESSION_KEY != readiness_csrf.READINESS_CSRF_SESSION_KEY
        assert token != readiness_token
        assert csrf.validate_dashboard_csrf(user, token).is_ok()
        assert csrf.validate_dashboard_csrf(user, readiness_token).is_err()
        assert readiness_csrf.validate_readiness_csrf_token(
            readiness_token, user.id
        ).is_ok()
        assert readiness_csrf.validate_readiness_csrf_token(
            token, user.id
        ).is_err()


def test_the_error_code_is_the_stable_transport_code():
    # Clients dispatch on this code; a CSRF failure is never a lost session.
    assert csrf.CSRF_INVALID_ERROR == 'csrf_invalid'
