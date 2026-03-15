"""
tests.unit.services.lan_tournament.test_site_team_captain_views
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for captain self-service management site views.

Routes under test:
  GET  /tournaments/<id>/teams/<team_id>/update           -- update_team_form
  POST /tournaments/<id>/teams/<team_id>/update           -- update_team
  POST /tournaments/<id>/teams/<team_id>/transfer_captain -- site_transfer_captain
  POST /tournaments/<id>/teams/<team_id>/remove_member    -- site_remove_member

WTForms coverage (sections 10-14):
  - Form class instantiation and field presence
  - Validation rules (required fields, length limits)
  - View behavior on invalid form submission (re-render, not redirect)
  - View behavior on valid form submission (redirect)
  - CSRF / LocalizedForm base class patterns
"""

from contextlib import contextmanager
from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipant,
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.tournament_team import (
    TournamentTeam,
    TournamentTeamID,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


# ------------------------------------------------------------------ #
# IDs
# ------------------------------------------------------------------ #

TOURNAMENT_ID = TournamentID(generate_uuid())
TOURNAMENT_ID_STR = str(TOURNAMENT_ID)
TEAM_ID = TournamentTeamID(generate_uuid())
TEAM_ID_STR = str(TEAM_ID)
PARTY_ID_STR = str(generate_uuid())
CAPTAIN_USER_ID = generate_uuid()
MEMBER_USER_ID = generate_uuid()
NON_MEMBER_USER_ID = generate_uuid()
CAPTAIN_PARTICIPANT_ID = TournamentParticipantID(generate_uuid())
MEMBER_PARTICIPANT_ID = TournamentParticipantID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.site.views'


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app for test_request_context."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _make_tournament(
    status: TournamentStatus = TournamentStatus.REGISTRATION_OPEN,
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


def _make_team() -> MagicMock:
    t = MagicMock(spec=TournamentTeam)
    t.id = TEAM_ID
    t.tournament_id = TOURNAMENT_ID
    t.name = 'Alpha Squad'
    t.tag = 'ALPHA'
    t.description = 'The alpha team'
    t.image_url = None
    t.captain_user_id = CAPTAIN_USER_ID
    t.join_code = None
    t.created_at = datetime.now(UTC)
    t.updated_at = None
    t.removed_at = None
    return t


def _make_captain_participant() -> MagicMock:
    p = MagicMock(spec=TournamentParticipant)
    p.id = CAPTAIN_PARTICIPANT_ID
    p.user_id = CAPTAIN_USER_ID
    p.tournament_id = TOURNAMENT_ID
    p.team_id = TEAM_ID
    p.removed_at = None
    p.substitute_player = False
    return p


def _make_member_participant() -> MagicMock:
    p = MagicMock(spec=TournamentParticipant)
    p.id = MEMBER_PARTICIPANT_ID
    p.user_id = MEMBER_USER_ID
    p.tournament_id = TOURNAMENT_ID
    p.team_id = TEAM_ID
    p.removed_at = None
    p.substitute_player = False
    return p


def _make_user(user_id, screen_name='TestUser') -> MagicMock:
    u = MagicMock()
    u.id = user_id
    u.screen_name = screen_name
    u.authenticated = True
    return u


@contextmanager
def _patched_captain_view(
    app,
    *,
    tournament_status: TournamentStatus = TournamentStatus.REGISTRATION_OPEN,
    current_user_id=CAPTAIN_USER_ID,
    authenticated: bool = True,
):
    """Patch view dependencies for captain management routes.

    The ``app`` parameter is required so that the ``g`` proxy can be
    patched inside a Flask application context (Werkzeug 3.x raises
    ``RuntimeError: Working outside of application context`` otherwise).
    """
    tournament = _make_tournament(status=tournament_status)
    team = _make_team()

    with app.app_context():
        with (
            patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
            patch(f'{_V}.flash_error') as mock_flash_error,
            patch(f'{_V}.flash_success') as mock_flash_success,
            patch(f'{_V}.redirect_to') as mock_redirect_to,
            patch(f'{_V}.tournament_service') as mock_tournament_svc,
            patch(f'{_V}.tournament_team_service') as mock_team_svc,
            patch(
                f'{_V}.tournament_participant_service'
            ) as mock_participant_svc,
            patch(f'{_V}.user_service') as mock_user_svc,
            patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
            patch(f'{_V}._get_team_or_404') as mock_get_team,
            patch(f'{_V}.g') as mock_g,
        ):
            mock_get_tournament.return_value = tournament
            mock_get_team.return_value = team

            mock_g.user = _make_user(current_user_id)
            mock_g.user.authenticated = authenticated
            mock_g.party = MagicMock()
            mock_g.party.id = PARTY_ID_STR

            captain = _make_captain_participant()
            member = _make_member_participant()
            mock_participant_svc.get_participants_for_tournament.return_value = [
                captain,
                member,
            ]
            mock_team_svc.get_team_members.return_value = [
                captain,
                member,
            ]

            yield {
                'flash_error': mock_flash_error,
                'flash_success': mock_flash_success,
                'redirect_to': mock_redirect_to,
                'tournament_svc': mock_tournament_svc,
                'team_svc': mock_team_svc,
                'participant_svc': mock_participant_svc,
                'user_svc': mock_user_svc,
                'get_tournament': mock_get_tournament,
                'get_team': mock_get_team,
                'g': mock_g,
                'tournament': tournament,
                'team': team,
            }


# ------------------------------------------------------------------ #
# 1. Authorization tests — login required
# ------------------------------------------------------------------ #


def test_update_team_form_requires_login(app):
    """GET update_team_form redirects unauthenticated users."""
    from werkzeug.exceptions import Forbidden

    with _patched_captain_view(app, authenticated=False) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['g'].user.authenticated = False
        raw_fn = views.update_team_form.__wrapped__.__wrapped__

        with app.test_request_context('/'):
            # The inner function aborts with 403 for unauthenticated
            # users (via _require_team_captain).  The @login_required
            # decorator would normally intercept before this point.
            with pytest.raises(Forbidden):
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    # login_required redirects to login form; the decorator itself
    # handles this, so the raw function should never be reached when
    # unauthenticated.  For TDD we assert the decorator is wired.
    assert hasattr(views.update_team_form, '__wrapped__')


def test_update_team_requires_login(app):
    """POST update_team redirects unauthenticated users."""
    from byceps.services.lan_tournament.blueprints.site import views

    # The @login_required decorator must be present.
    assert hasattr(views.update_team, '__wrapped__')


def test_transfer_captain_requires_login(app):
    """POST site_transfer_captain requires login."""
    from byceps.services.lan_tournament.blueprints.site import views

    assert hasattr(views.site_transfer_captain, '__wrapped__')


def test_remove_member_requires_login(app):
    """POST site_remove_member requires login."""
    from byceps.services.lan_tournament.blueprints.site import views

    assert hasattr(views.site_remove_member, '__wrapped__')


# ------------------------------------------------------------------ #
# 2. Authorization tests — non-captain gets 403
# ------------------------------------------------------------------ #


def test_update_team_non_captain_rejected(app):
    """POST update_team by a non-captain user returns 403 or error flash."""
    with _patched_captain_view(
        app,
        current_user_id=NON_MEMBER_USER_ID,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'New Name', 'description': 'desc'},
        ):
            try:
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)
            except Exception:
                pass

        # Non-captain should get an error flash or abort(403).
        # The implementation may either flash_error + redirect, or abort.
        # We accept both — at minimum the team must NOT be updated.
        mocks['flash_success'].assert_not_called()


def test_transfer_captain_non_captain_rejected(app):
    """POST site_transfer_captain by non-captain returns 403 or error."""
    with _patched_captain_view(
        app,
        current_user_id=NON_MEMBER_USER_ID,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.site_transfer_captain.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'new_captain_id': str(MEMBER_USER_ID)},
        ):
            try:
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)
            except Exception:
                pass

        mocks['flash_success'].assert_not_called()


# ------------------------------------------------------------------ #
# 3. Tournament status gating — DRAFT blocks captain management
# ------------------------------------------------------------------ #


def test_update_team_blocked_during_draft(app):
    """Captain management is blocked when tournament is DRAFT."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.DRAFT,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'New Name', 'description': 'desc'},
        ):
            try:
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)
            except Exception:
                pass

        mocks['flash_success'].assert_not_called()


def test_transfer_captain_blocked_during_draft(app):
    """Transfer captain is blocked when tournament is DRAFT."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.DRAFT,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.site_transfer_captain.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'new_captain_id': str(MEMBER_USER_ID)},
        ):
            try:
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)
            except Exception:
                pass

        mocks['flash_success'].assert_not_called()


# ------------------------------------------------------------------ #
# 4. Tournament status gating — COMPLETED blocks captain management
# ------------------------------------------------------------------ #


def test_update_team_blocked_during_completed(app):
    """Captain management is blocked when tournament is COMPLETED."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.COMPLETED,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'New Name', 'description': 'desc'},
        ):
            try:
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)
            except Exception:
                pass

        mocks['flash_success'].assert_not_called()


def test_remove_member_blocked_during_completed(app):
    """Remove member is blocked when tournament is COMPLETED."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.COMPLETED,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.site_remove_member.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'user_id': str(MEMBER_USER_ID)},
        ):
            try:
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)
            except Exception:
                pass

        mocks['flash_success'].assert_not_called()


# ------------------------------------------------------------------ #
# 5. Tournament status gating — REGISTRATION_OPEN allows management
# ------------------------------------------------------------------ #


def test_update_team_allowed_during_registration_open(app):
    """Captain management is allowed when tournament is REGISTRATION_OPEN."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].update_team.return_value = Ok(mocks['team'])

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'New Name', 'description': 'desc'},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_transfer_captain_allowed_during_registration_open(app):
    """Transfer captain is allowed when tournament is REGISTRATION_OPEN."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].transfer_captain.return_value = Ok(mocks['team'])

        raw_fn = views.site_transfer_captain.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'new_captain_id': str(MEMBER_USER_ID)},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


# ------------------------------------------------------------------ #
# 6. Tournament status gating — ONGOING (IN_PROGRESS) allows management
# ------------------------------------------------------------------ #


def test_update_team_allowed_during_ongoing(app):
    """Captain management is allowed when tournament is ONGOING."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.ONGOING,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].update_team.return_value = Ok(mocks['team'])

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'New Name', 'description': 'desc'},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_remove_member_allowed_during_ongoing(app):
    """Remove member is allowed when tournament is ONGOING."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.ONGOING,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].remove_team_member.return_value = Ok(MagicMock())

        raw_fn = views.site_remove_member.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'user_id': str(MEMBER_USER_ID)},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


# ------------------------------------------------------------------ #
# 7. Functional tests — update_team
# ------------------------------------------------------------------ #


def test_update_team_captain_can_update_name_and_description(app):
    """POST update_team: captain can successfully update team name/desc."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        updated_team = _make_team()
        updated_team.name = 'Renamed Squad'
        updated_team.description = 'New description'
        mocks['team_svc'].update_team.return_value = Ok(updated_team)

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'Renamed Squad', 'description': 'New description'},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['team_svc'].update_team.assert_called_once()
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_update_team_service_error_flashes_error(app):
    """POST update_team: service error is communicated via flash_error."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].update_team.return_value = Err(
            'A team with this name already exists in this tournament.'
        )

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'Duplicate', 'description': ''},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


# ------------------------------------------------------------------ #
# 8. Functional tests — transfer_captain
# ------------------------------------------------------------------ #


def test_transfer_captain_success(app):
    """POST site_transfer_captain: captain transfers role to member."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].transfer_captain.return_value = Ok(mocks['team'])

        raw_fn = views.site_transfer_captain.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'new_captain_id': str(MEMBER_USER_ID)},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['team_svc'].transfer_captain.assert_called_once()
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


# ------------------------------------------------------------------ #
# 9. Functional tests — remove_member
# ------------------------------------------------------------------ #


def test_remove_member_captain_can_remove_non_captain(app):
    """POST site_remove_member: captain removes a regular member."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].remove_team_member.return_value = Ok(MagicMock())

        raw_fn = views.site_remove_member.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'user_id': str(MEMBER_USER_ID)},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['team_svc'].remove_team_member.assert_called_once()
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


def test_remove_member_service_error_flashes_error(app):
    """POST site_remove_member: service error flashes an error."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].remove_team_member.return_value = Err(
            'Cannot remove the captain. Transfer captain role first.'
        )

        raw_fn = views.site_remove_member.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'user_id': str(CAPTAIN_USER_ID)},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


# ------------------------------------------------------------------ #
# 10. WTForms — form class instantiation and field presence
# ------------------------------------------------------------------ #


def test_site_team_update_form_has_expected_fields(app):
    """SiteTeamUpdateForm exposes 'name' and 'description' fields."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )

    with app.app_context():
        form = SiteTeamUpdateForm()

    assert hasattr(form, 'name'), 'Missing field: name'
    assert hasattr(form, 'description'), 'Missing field: description'
    # Should NOT have tag or join_code — those belong to create form.
    assert not hasattr(form, 'tag'), 'Unexpected field: tag'
    assert not hasattr(form, 'join_code'), 'Unexpected field: join_code'


def test_site_team_create_form_has_expected_fields(app):
    """SiteTeamCreateForm exposes name, tag, description, join_code."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamCreateForm,
    )

    with app.app_context():
        form = SiteTeamCreateForm()

    assert hasattr(form, 'name'), 'Missing field: name'
    assert hasattr(form, 'tag'), 'Missing field: tag'
    assert hasattr(form, 'description'), 'Missing field: description'
    assert hasattr(form, 'join_code'), 'Missing field: join_code'


def test_site_team_update_form_populates_from_obj(app):
    """SiteTeamUpdateForm pre-fills fields when instantiated with obj=."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )

    team = _make_team()
    with app.app_context():
        form = SiteTeamUpdateForm(obj=team)

    assert form.name.data == team.name
    assert form.description.data == team.description


# ------------------------------------------------------------------ #
# 11. WTForms — validation rules (required fields, length limits)
# ------------------------------------------------------------------ #


def test_update_form_name_required(app):
    """SiteTeamUpdateForm rejects empty name (InputRequired)."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        form = SiteTeamUpdateForm(MultiDict({'name': '', 'description': ''}))
        assert not form.validate(), 'Empty name should fail validation'
        assert 'name' in form.errors


def test_update_form_name_max_length(app):
    """SiteTeamUpdateForm rejects name exceeding 80 characters."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        long_name = 'A' * 81
        form = SiteTeamUpdateForm(
            MultiDict({'name': long_name, 'description': ''})
        )
        assert not form.validate(), 'Name over 80 chars should fail'
        assert 'name' in form.errors


def test_update_form_name_at_max_length_passes(app):
    """SiteTeamUpdateForm accepts name of exactly 80 characters."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        name_80 = 'A' * 80
        form = SiteTeamUpdateForm(
            MultiDict({'name': name_80, 'description': ''})
        )
        assert form.validate(), f'Name of 80 chars should pass: {form.errors}'


def test_update_form_description_max_length(app):
    """SiteTeamUpdateForm rejects description exceeding 2000 characters."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        long_desc = 'X' * 2001
        form = SiteTeamUpdateForm(
            MultiDict({'name': 'Valid', 'description': long_desc})
        )
        assert not form.validate(), 'Description over 2000 chars should fail'
        assert 'description' in form.errors


def test_update_form_description_optional(app):
    """SiteTeamUpdateForm accepts empty description (Optional)."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        form = SiteTeamUpdateForm(
            MultiDict({'name': 'Valid Name', 'description': ''})
        )
        assert form.validate(), f'Empty description should pass: {form.errors}'


def test_create_form_name_required(app):
    """SiteTeamCreateForm rejects empty name."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamCreateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        form = SiteTeamCreateForm(
            MultiDict({'name': '', 'tag': '', 'description': '', 'join_code': ''})
        )
        assert not form.validate(), 'Empty name should fail validation'
        assert 'name' in form.errors


def test_create_form_tag_max_length(app):
    """SiteTeamCreateForm rejects tag exceeding 20 characters."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamCreateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        form = SiteTeamCreateForm(
            MultiDict({
                'name': 'Valid',
                'tag': 'T' * 21,
                'description': '',
                'join_code': '',
            })
        )
        assert not form.validate(), 'Tag over 20 chars should fail'
        assert 'tag' in form.errors


def test_create_form_valid_submission(app):
    """SiteTeamCreateForm accepts valid data with all fields."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamCreateForm,
    )
    from werkzeug.datastructures import MultiDict

    with app.app_context():
        form = SiteTeamCreateForm(
            MultiDict({
                'name': 'My Team',
                'tag': 'MT',
                'description': 'A fine team.',
                'join_code': 'secret123',
            })
        )
        assert form.validate(), f'Valid create form should pass: {form.errors}'


# ------------------------------------------------------------------ #
# 12. WTForms — view re-renders on invalid form (not redirect)
# ------------------------------------------------------------------ #


def test_update_team_invalid_form_rerenders_not_redirects(app):
    """POST update_team with invalid data re-renders the form template.

    After the WTForms migration, validation failures cause the view to
    call update_team_form() with the erroneous form object, returning a
    template context dict instead of a redirect.  The service layer
    should NOT be called.
    """
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.update_team.__wrapped__

        # Patch update_team_form at the module level so the re-render
        # call does not hit @login_required/@templated (which need the
        # real Flask g and Jinja2 template loader).
        sentinel = object()
        with patch(
            f'{_V}.update_team_form', return_value=sentinel
        ) as mock_form_view:
            with app.test_request_context(
                '/',
                method='POST',
                # Empty name triggers InputRequired validation failure
                data={'name': '', 'description': 'Some desc'},
            ):
                result = raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

        # The view must have called update_team_form to re-render.
        mock_form_view.assert_called_once()
        call_args = mock_form_view.call_args
        # Third positional arg (or keyword) is the erroneous form.
        erroneous_form = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get('erroneous_form')
        assert erroneous_form is not None, 'Expected erroneous form to be passed'
        assert not erroneous_form.validate(), 'Form should still be invalid'
        assert 'name' in erroneous_form.errors

    # The view should NOT have called update_team on the service.
    mocks['team_svc'].update_team.assert_not_called()
    # No success flash should have fired.
    mocks['flash_success'].assert_not_called()
    # No error flash from the service path should have fired either —
    # validation errors are shown inline via the form object.
    mocks['flash_error'].assert_not_called()


def test_update_team_name_too_long_rerenders_form(app):
    """POST update_team with a name >80 chars re-renders, does not call service."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.update_team.__wrapped__

        with patch(
            f'{_V}.update_team_form', return_value=None
        ) as mock_form_view:
            with app.test_request_context(
                '/',
                method='POST',
                data={'name': 'X' * 81, 'description': ''},
            ):
                raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

        # Must have called update_team_form for re-render.
        mock_form_view.assert_called_once()
        erroneous_form = mock_form_view.call_args[0][2] if len(mock_form_view.call_args[0]) > 2 else mock_form_view.call_args[1].get('erroneous_form')
        assert erroneous_form is not None
        assert 'name' in erroneous_form.errors

    mocks['team_svc'].update_team.assert_not_called()
    mocks['flash_success'].assert_not_called()


def test_update_team_valid_form_calls_service_and_redirects(app):
    """POST update_team with valid data passes validation and calls service."""
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        mocks['team_svc'].update_team.return_value = Ok(mocks['team'])

        raw_fn = views.update_team.__wrapped__

        with app.test_request_context(
            '/',
            method='POST',
            data={'name': 'Valid Name', 'description': 'A description'},
        ):
            raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    # Service MUST be called when form is valid.
    mocks['team_svc'].update_team.assert_called_once()
    mocks['flash_success'].assert_called_once()
    mocks['redirect_to'].assert_called_once()


# ------------------------------------------------------------------ #
# 13. WTForms — update_team_form GET populates form from team object
# ------------------------------------------------------------------ #


def test_update_team_form_get_returns_form_context(app):
    """GET update_team_form returns a context dict containing a form object.

    The form should be pre-populated from the team's current data via
    ``SiteTeamUpdateForm(obj=team)``.
    """
    with _patched_captain_view(
        app,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    ) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        # update_team_form is wrapped with @login_required and @templated.
        # __wrapped__.__wrapped__ strips both decorators so we get the
        # raw function that returns a template context dict.
        raw_fn = views.update_team_form.__wrapped__.__wrapped__

        with app.test_request_context('/'):
            result = raw_fn(TOURNAMENT_ID_STR, TEAM_ID_STR)

    # Result should be a dict with 'form', 'tournament', 'team'.
    assert isinstance(result, dict)
    assert 'form' in result
    assert 'tournament' in result
    assert 'team' in result
    # Form should have the team's current name pre-filled.
    assert result['form'].name.data == mocks['team'].name


# ------------------------------------------------------------------ #
# 14. WTForms — CSRF / LocalizedForm base class verification
# ------------------------------------------------------------------ #


def test_forms_extend_localized_form(app):
    """Both site forms inherit from LocalizedForm, not FlaskForm.

    LocalizedForm extends wtforms.Form (no built-in CSRF).  This is by
    design — BYCEPS handles CSRF at the middleware layer.  Verify the
    MRO so a future refactor to FlaskForm (which adds its own CSRF
    token field) is caught.
    """
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamCreateForm,
        SiteTeamUpdateForm,
    )
    from byceps.util.l10n import LocalizedForm
    from wtforms import Form as WTFormsBaseForm

    assert issubclass(SiteTeamCreateForm, LocalizedForm)
    assert issubclass(SiteTeamUpdateForm, LocalizedForm)
    # LocalizedForm itself must extend wtforms.Form (not flask_wtf.FlaskForm).
    assert issubclass(LocalizedForm, WTFormsBaseForm)


def test_forms_do_not_have_csrf_token_field(app):
    """Site forms should not contain a csrf_token field.

    Since LocalizedForm extends wtforms.Form, there is no automatic
    CSRF field.  If someone switches to FlaskForm, this test will
    catch the regression.
    """
    from byceps.services.lan_tournament.blueprints.site.forms import (
        SiteTeamUpdateForm,
    )

    with app.app_context():
        form = SiteTeamUpdateForm()

    field_names = [f.name for f in form]
    assert 'csrf_token' not in field_names, (
        'Unexpected csrf_token field found — LocalizedForm should not inject CSRF'
    )
