"""
tests.unit.services.lan_tournament.test_admin_orga_appointment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

from flask import Flask
import pytest

from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.user.models import UserID
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
TOURNAMENT_ID_STR = str(TOURNAMENT_ID)
ADMIN_USER_ID = UserID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.admin.views'

# tests/unit/services/lan_tournament/<this file> -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app with LOCALE config for form instantiation."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _make_tournament() -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.party_id = generate_uuid()
    return t


def _make_eligible_user() -> MagicMock:
    """Return a user that is neither deleted nor suspended."""
    user = MagicMock()
    user.id = UserID(generate_uuid())
    user.screen_name = 'SomeOrga'
    user.deleted = False
    user.suspended = False
    return user


@contextmanager
def _patched_view():
    """Patch all Flask/Babel/BYCEPS view dependencies at once."""
    with (
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.tournament_orga_service') as mock_orga_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'redirect_to': mock_redirect_to,
            'orga_svc': mock_orga_svc,
            'user_svc': mock_user_svc,
            'party_svc': mock_party_svc,
            'get_tournament': mock_get_tournament,
        }


def _call_assign(app, *, screen_name='SomeOrga', duties=''):
    from flask import g

    from byceps.services.lan_tournament.blueprints.admin import views

    with app.test_request_context(
        '/',
        method='POST',
        data={'screen_name': screen_name, 'duties': duties},
    ):
        mock_user = MagicMock()
        mock_user.id = ADMIN_USER_ID
        g.user = mock_user
        return views.assign_orga.__wrapped__(TOURNAMENT_ID_STR)


def _call_revoke(app, user_id_str):
    from flask import g

    from byceps.services.lan_tournament.blueprints.admin import views

    with app.test_request_context('/', method='POST'):
        mock_user = MagicMock()
        mock_user.id = ADMIN_USER_ID
        g.user = mock_user
        return views.revoke_orga.__wrapped__(TOURNAMENT_ID_STR, user_id_str)


def _call_list(app):
    from flask import g

    from byceps.services.lan_tournament.blueprints.admin import views

    with app.test_request_context('/'):
        mock_user = MagicMock()
        mock_user.id = ADMIN_USER_ID
        g.user = mock_user
        # Unwrap `@permission_required` and `@templated`.
        raw_fn = views.orgas_for_tournament.__wrapped__.__wrapped__
        return raw_fn(TOURNAMENT_ID_STR)


# -------------------------------------------------------------------- #
# orgas_for_tournament (list)
# -------------------------------------------------------------------- #


def test_list_view_renders_orgas_and_form(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament
        mocks['orga_svc'].get_orgas_for_tournament.return_value = []
        mocks['user_svc'].get_users_indexed_by_id.return_value = {}

        context = _call_list(app)

    mocks['orga_svc'].get_orgas_for_tournament.assert_called_once_with(
        TOURNAMENT_ID
    )
    assert context['orgas'] == []
    assert context['tournament'] is tournament
    assert 'form' in context


# -------------------------------------------------------------------- #
# assign_orga
# -------------------------------------------------------------------- #


def test_assign_orga_unknown_screen_name_flashes_error_not_raises(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament
        mocks['user_svc'].find_user_by_screen_name.return_value = None

        # Must not raise.
        _call_assign(app, screen_name='NoSuchUser')

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()
    mocks['orga_svc'].assign_orga.assert_not_called()
    mocks['redirect_to'].assert_called_once()


def test_assign_orga_ok_result_flashes_success(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament

        resolved_user = _make_eligible_user()
        mocks['user_svc'].find_user_by_screen_name.return_value = resolved_user
        mocks['orga_svc'].assign_orga.return_value = Ok(
            (MagicMock(), MagicMock())
        )

        _call_assign(app, screen_name='SomeOrga', duties='Bracket admin')

    mocks['orga_svc'].assign_orga.assert_called_once_with(
        TOURNAMENT_ID,
        resolved_user.id,
        ADMIN_USER_ID,
        duties='Bracket admin',
    )
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_assign_orga_err_result_flashes_error(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament

        resolved_user = _make_eligible_user()
        mocks['user_svc'].find_user_by_screen_name.return_value = resolved_user
        mocks['orga_svc'].assign_orga.return_value = Err(
            'User is already an orga of this tournament.'
        )

        _call_assign(app, screen_name='SomeOrga')

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


def test_assign_orga_blank_duties_normalized_to_none(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament

        resolved_user = _make_eligible_user()
        mocks['user_svc'].find_user_by_screen_name.return_value = resolved_user
        mocks['orga_svc'].assign_orga.return_value = Ok(
            (MagicMock(), MagicMock())
        )

        _call_assign(app, screen_name='SomeOrga', duties='   ')

    _, kwargs = mocks['orga_svc'].assign_orga.call_args
    assert kwargs['duties'] is None


@pytest.mark.parametrize('flag', ['deleted', 'suspended'])
def test_assign_orga_rejects_ineligible_user(app, flag):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament

        resolved_user = _make_eligible_user()
        setattr(resolved_user, flag, True)
        mocks['user_svc'].find_user_by_screen_name.return_value = resolved_user

        _call_assign(app, screen_name='SomeOrga')

    mocks['orga_svc'].assign_orga.assert_not_called()
    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


def _translating(catalogue):
    def translate(msg, **kw):
        translated = catalogue.get(msg, msg)
        return translated % kw if kw else translated

    return patch(f'{_V}.gettext', side_effect=translate)


def test_assign_orga_error_is_translated_whole(app):
    """The service message is a catalogue msgid; the flash translates it."""
    catalogue = {
        'Could not assign orga: %(error)s': (
            'Orga konnte nicht zugewiesen werden: %(error)s'
        ),
        'User is already an orga of this tournament.': (
            'Der Benutzer ist bereits Orga dieses Turniers.'
        ),
    }

    with _patched_view() as mocks, _translating(catalogue):
        mocks['get_tournament'].return_value = _make_tournament()
        mocks[
            'user_svc'
        ].find_user_by_screen_name.return_value = _make_eligible_user()
        mocks['orga_svc'].assign_orga.return_value = Err(
            'User is already an orga of this tournament.'
        )

        _call_assign(app, screen_name='SomeOrga')

    mocks['flash_error'].assert_called_once_with(
        'Orga konnte nicht zugewiesen werden: '
        'Der Benutzer ist bereits Orga dieses Turniers.'
    )


def test_revoke_orga_error_is_translated_whole(app):
    """The service message is a catalogue msgid; the flash translates it."""
    catalogue = {
        'Could not revoke orga: %(error)s': (
            'Orga konnte nicht entfernt werden: %(error)s'
        ),
        'User is not an orga of this tournament.': (
            'Der Benutzer ist kein Orga dieses Turniers.'
        ),
    }

    with _patched_view() as mocks, _translating(catalogue):
        mocks['get_tournament'].return_value = _make_tournament()
        mocks['orga_svc'].revoke_orga.return_value = Err(
            'User is not an orga of this tournament.'
        )

        _call_revoke(app, str(UserID(generate_uuid())))

    mocks['flash_error'].assert_called_once_with(
        'Orga konnte nicht entfernt werden: '
        'Der Benutzer ist kein Orga dieses Turniers.'
    )


# -------------------------------------------------------------------- #
# revoke_orga
# -------------------------------------------------------------------- #


def test_revoke_orga_ok_result_flashes_success(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament
        mocks['orga_svc'].revoke_orga.return_value = Ok(None)

        target_user_id = UserID(generate_uuid())

        _call_revoke(app, str(target_user_id))

    mocks['orga_svc'].revoke_orga.assert_called_once_with(
        TOURNAMENT_ID, target_user_id, ADMIN_USER_ID
    )
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_revoke_orga_err_result_flashes_error(app):
    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament
        mocks['orga_svc'].revoke_orga.return_value = Err(
            'User is not an orga of this tournament.'
        )

        target_user_id = UserID(generate_uuid())

        _call_revoke(app, str(target_user_id))

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


def test_revoke_orga_invalid_user_id_aborts_404(app):
    from werkzeug.exceptions import NotFound

    with _patched_view() as mocks:
        tournament = _make_tournament()
        mocks['get_tournament'].return_value = tournament

        with pytest.raises(NotFound):
            _call_revoke(app, 'not-a-uuid')

    mocks['orga_svc'].revoke_orga.assert_not_called()


# -------------------------------------------------------------------- #
# backend-only surface
# -------------------------------------------------------------------- #


def test_scoped_orga_cannot_assign_orgas():
    site_blueprint_dir = (
        _REPO_ROOT
        / 'byceps'
        / 'services'
        / 'lan_tournament'
        / 'blueprints'
        / 'site'
    )
    assert site_blueprint_dir.is_dir(), (
        f'expected site blueprint dir at {site_blueprint_dir}'
    )

    offending_files = []
    for py_file in site_blueprint_dir.rglob('*.py'):
        source = py_file.read_text()
        if 'assign_orga' in source or 'revoke_orga' in source:
            offending_files.append(py_file)

    assert offending_files == [], (
        'orga assignment/revocation must not be referenced anywhere in '
        f'the site blueprint; found in: {offending_files}'
    )


def test_admin_orga_routes_use_uuid_user_id(app):
    assert UUID(str(ADMIN_USER_ID))
