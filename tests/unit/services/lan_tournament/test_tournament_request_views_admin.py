"""
tests.unit.services.lan_tournament.test_tournament_request_views_admin
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, g
from flask_babel import Babel
import pytest
from werkzeug.exceptions import Forbidden

from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequestStatus,
)
from byceps.util.result import Err, Ok


_V = 'byceps.services.lan_tournament.blueprints.admin.views'


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app for `test_request_context`, with Babel wired up.

    `LocalizedForm` reads `current_app.config['LOCALE']`, and
    `gettext`/`to_utc`/`to_user_timezone` all need a live Babel
    extension instance -- without it they raise `KeyError` on
    `app.extensions['babel']` rather than merely misbehaving. Mirrors
    the `app` fixture in `test_tournament_request_views_site.py`.
    """
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def _make_user(*, permissions=frozenset()) -> MagicMock:
    user = MagicMock()
    user.has_permission.side_effect = lambda permission: (
        permission in permissions
    )
    return user


def _make_tournament_request(
    *,
    id='11111111-1111-1111-1111-111111111111',
    status=TournamentRequestStatus.submitted,
    proposer_id='u1',
    decided_at=None,
    created_tournament_id=None,
    tournament_deleted=False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        party_id='p1',
        number=7,
        proposer_id=proposer_id,
        status=status,
        name='Some Tournament',
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=16,
        preferred_start_time=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 1, 1, 18, 0, tzinfo=UTC),
        description='A description',
        special_rules=None,
        notes=None,
        desired_template=None,
        decided_at=decided_at,
        created_tournament_id=created_tournament_id,
        # A `SimpleNamespace` has no computed properties, unlike the
        # real `TournamentRequest` dataclass -- callers that need a
        # `tournament_created` request with its link cleared must set
        # this explicitly rather than deriving it from `status` and
        # `created_tournament_id` the way the real property does.
        tournament_deleted=tournament_deleted,
        # Mirrors the real dataclass's `is_editable` property (`status
        # is submitted`) -- straightforward enough not to need its own
        # override param, unlike `tournament_deleted` above.
        is_editable=status is TournamentRequestStatus.submitted,
    )


_VALID_FORM_DATA = {
    'name': 'Updated name',
    'game': 'Some Game',
    'game_format': GameFormat.ONE_V_ONE.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '2',
    'participant_limit': '16',
    'preferred_start_time': '2026-01-01T10:00',
    'preferred_end_time': '2026-01-01T18:00',
    'description': 'A description',
    'special_rules': '',
    'notes': '',
    'desired_template': '',
}


def test_admin_queue_requires_request_view_permission(app):
    """The party-wide request queue is gated on `request_view`.

    Holding the module's generic `view` permission (which gates the
    tournament list and overview) must not be enough on its own --
    the two are separate grants (see permissions.py).
    """
    user = _make_user(permissions=frozenset({'lan_tournament.view'}))

    with app.test_request_context('/'):
        g.user = user

        with pytest.raises(Forbidden):
            views.requests_for_party('some-party-id')

    user.has_permission.assert_called_with('lan_tournament.request_view')


def test_admin_queue_computes_stale_request_ids(app):
    """workspace-dim0.16 AC3: the queue view passes `stale_request_ids`,
    limited to accepted requests decided at least 48h ago.

    A submitted request is never stale, regardless of `decided_at`, and
    a recently-accepted request is not yet stale either -- only the
    accepted request decided 3 days ago belongs in the set.
    """
    now = datetime.now(UTC)
    submitted = _make_tournament_request(
        id='r-submitted',
        status=TournamentRequestStatus.submitted,
        proposer_id='u1',
    )
    fresh_accepted = _make_tournament_request(
        id='r-fresh',
        status=TournamentRequestStatus.accepted,
        proposer_id='u2',
        decided_at=now - timedelta(hours=1),
    )
    stale_accepted = _make_tournament_request(
        id='r-stale',
        status=TournamentRequestStatus.accepted,
        proposer_id='u3',
        decided_at=now - timedelta(days=3),
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        # `@templated` renders the returned dict via `render_template`,
        # which needs a real template folder; stub it out to inspect
        # the context dict the view builds without rendering it.
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_party_svc.find_party.return_value = party
        mock_request_svc.get_visible_requests_for_user.return_value = [
            submitted,
            fresh_accepted,
            stale_accepted,
        ]
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_render_template.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        views.requests_for_party(party.id)

    context = mock_render_template.call_args.kwargs
    assert context['stale_request_ids'] == {'r-stale'}


def test_admin_accept_requires_request_decide_permission(app):
    """Accepting a request is gated on `request_decide`, not `request_view`.

    A user who may only look at requests must not be able to decide
    on them.
    """
    user = _make_user(permissions=frozenset({'lan_tournament.request_view'}))

    with app.test_request_context('/'):
        g.user = user

        with pytest.raises(Forbidden):
            views.accept_request('11111111-1111-1111-1111-111111111111')

    user.has_permission.assert_called_with('lan_tournament.request_decide')


def test_admin_accept_requires_request_view_permission_too(app):
    """G1 (bead workspace-c9o4.3): `request_decide` alone is no longer
    enough for `accept_request` -- both the success and error paths
    redirect to `.view_request`/`.requests_for_party`, which need
    `request_view`. A decide-only admin (who lacks `request_view`)
    must be refused before the service is ever called, not after
    committing the decision.
    """
    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        app.test_request_context('/', method='POST'),
    ):
        user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide'})
        )
        g.user = user

        with pytest.raises(Forbidden):
            views.accept_request('11111111-1111-1111-1111-111111111111')

        mock_repo.find_request.assert_not_called()
        mock_request_svc.accept_request.assert_not_called()

    user.has_permission.assert_called_with('lan_tournament.request_view')


def test_admin_reject_request_requires_request_view_permission_too(app):
    """G1 (bead workspace-c9o4.3): same additional `request_view`
    requirement on `reject_request` -- the over-long-reason path
    re-renders through `view_request`, whose own decorator re-checks
    `request_view`, and the success/error paths redirect to routes
    that need it too."""
    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        app.test_request_context(
            '/', method='POST', data={'reason': 'Venue unavailable'}
        ),
    ):
        user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide'})
        )
        g.user = user

        with pytest.raises(Forbidden):
            views.reject_request('11111111-1111-1111-1111-111111111111')

        mock_repo.find_request.assert_not_called()
        mock_request_svc.reject_request.assert_not_called()

    user.has_permission.assert_called_with('lan_tournament.request_view')


# --------------------------------------------------------------------- #
# Admin reject request -- reason validation (n)
# --------------------------------------------------------------------- #


def test_admin_reject_request_empty_reason_flashes_required_and_redirects(
    app,
):
    """n: an empty reason keeps its existing behavior -- flash and
    redirect (there is no typed text worth preserving)."""
    tournament_request = _make_tournament_request()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data={'reason': ''}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_redirect_to.return_value = 'redirected'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_view_request.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'A reason is required to reject a request.'
    )
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    assert result == 'redirected'


def test_admin_reject_request_whitespace_only_reason_flashes_required(app):
    """n: whitespace-only slips past `InputRequired`/`Length(min=1)` in
    the form (both see a non-empty raw string) -- `form.validate()`
    itself passes it through. It still reaches the user as 'reason
    required', just via the service's own `reason.strip()` check
    (mirrored at the service level by
    `test_reject_request_requires_reason`) and the existing
    Ok/Err `match` below, not the view's own pre-`validate()` branch."""
    tournament_request = _make_tournament_request()
    err_message = 'A reason is required to reject a request.'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data={'reason': '   '}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_request_svc.reject_request.return_value = Err(err_message)
        mock_redirect_to.return_value = 'redirected'
        user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )
        g.user = user

        views.reject_request(tournament_request.id)

    # `form.validate()` passed the whitespace-only reason through, so
    # the service is what actually caught it -- called with the
    # stripped (now empty) reason.
    mock_request_svc.reject_request.assert_called_once_with(
        tournament_request.id, user.id, ''
    )
    mock_view_request.assert_not_called()
    mock_flash_error.assert_called_once_with(err_message)
    mock_redirect_to.assert_called_once_with(
        '.requests_for_party', party_id=tournament_request.party_id
    )


def test_admin_reject_request_over_max_length_preserves_text_and_rerenders(
    app,
):
    """n: a too-long reason must not be lost -- re-render the request
    detail page with the erroneous (bound) form instead of
    redirecting, so the admin's typed text survives.

    J3: the flashed message is now the form's own error text for the
    field (gettext'd), not a hardcoded 'too long' string -- so any
    other future validator failure (a control-char check, say) gets
    the same treatment without a code change here. Reproduce the
    expected text from the bound form itself rather than hardcoding
    wtforms' internal wording."""
    tournament_request = _make_tournament_request()
    long_reason = 'x' * 2001

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data={'reason': long_reason}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_view_request.return_value = 'rendered-detail'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_redirect_to.assert_not_called()
    mock_view_request.assert_called_once()
    call = mock_view_request.call_args
    assert call.args[0] == tournament_request.id
    erroneous_form = call.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.data == long_reason
    assert erroneous_form.reason.errors
    mock_flash_error.assert_called_once_with(erroneous_form.reason.errors[0])
    assert result == 'rendered-detail'


def test_admin_reject_request_other_form_error_flashes_form_message_and_rerenders(
    app,
):
    """J3: any non-empty-after-strip form validation failure -- not
    just 'too long' -- must flash the form's own error message and
    re-render with the bound form, so a future validator (a
    control-char check, say) gets the same treatment without a code
    change here. The form itself is mocked so this doesn't depend on
    which validators the form module actually has wired up (or when)."""
    tournament_request = _make_tournament_request()

    mock_form = MagicMock()
    mock_form.validate.return_value = False
    mock_form.reason.data = 'bad\x00reason'
    mock_form.reason.errors = [
        'The reason must not contain control characters.'
    ]

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(f'{_V}.TournamentRequestRejectForm', return_value=mock_form),
        app.test_request_context(
            '/', method='POST', data={'reason': 'bad\x00reason'}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_view_request.return_value = 'rendered-detail'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_redirect_to.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'The reason must not contain control characters.'
    )
    mock_view_request.assert_called_once_with(
        tournament_request.id, erroneous_reject_form=mock_form
    )
    assert result == 'rendered-detail'


def test_admin_reject_request_service_control_char_err_preserves_text_and_rerenders(
    app,
):
    """J3: when the SERVICE (not the form) is what catches an invalid
    reason -- e.g. a control character the form doesn't validate for --
    re-render with the bound form instead of redirecting, so the typed
    text isn't lost. `form.validate()` is forced to pass regardless of
    whether the form module's own control-char validator has landed
    yet, so this targets the service's Err path specifically."""
    tournament_request = _make_tournament_request()
    bad_reason = 'bad\x00reason'
    err_message = 'The reason must not contain control characters.'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        # A real function, not a bare `MagicMock`: WTForms' `FormMeta`
        # rescans `dir(cls)` for unbound fields whenever `_unbound_fields`
        # is invalidated, and `hasattr(x, '_formfield')` is trivially
        # `True` on a `MagicMock` (it auto-creates any attribute) --
        # that would sweep the patched `validate` into the form's own
        # field list and blow up unrelated field-sorting.
        patch.object(
            views.TournamentRequestRejectForm,
            'validate',
            new=lambda self: True,
        ),
        app.test_request_context(
            '/', method='POST', data={'reason': bad_reason}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_request_svc.reject_request.return_value = Err(err_message)
        mock_view_request.return_value = 'rendered-detail'
        user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )
        g.user = user

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_called_once_with(
        tournament_request.id, user.id, bad_reason
    )
    mock_redirect_to.assert_not_called()
    mock_flash_error.assert_called_once_with(err_message)
    mock_view_request.assert_called_once()
    call = mock_view_request.call_args
    assert call.args[0] == tournament_request.id
    erroneous_form = call.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.data == bad_reason
    assert result == 'rendered-detail'


def test_admin_reject_request_valid_reason_calls_service_and_redirects_to_queue(
    app,
):
    tournament_request = _make_tournament_request()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data={'reason': 'Venue unavailable'}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_request_svc.reject_request.return_value = Ok(tournament_request)
        mock_redirect_to.return_value = 'redirected'
        user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )
        g.user = user

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_called_once_with(
        tournament_request.id, user.id, 'Venue unavailable'
    )
    mock_flash_success.assert_called_once()
    mock_redirect_to.assert_called_once_with(
        '.requests_for_party', party_id=tournament_request.party_id
    )
    assert result == 'redirected'


def test_admin_reject_request_service_err_is_flashed(app):
    """The service's own `expected_status` precondition (a race lost
    between page-load and submit) is still a defense-in-depth path
    reachable through the view -- it must be flashed, not swallowed."""
    tournament_request = _make_tournament_request()
    err_message = 'Request is no longer in the expected state.'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data={'reason': 'Venue unavailable'}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_request_svc.reject_request.return_value = Err(err_message)
        mock_redirect_to.return_value = 'redirected'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_decide', 'lan_tournament.request_view'})
        )

        views.reject_request(tournament_request.id)

    mock_flash_error.assert_called_once_with(err_message)
    mock_redirect_to.assert_called_once_with(
        '.requests_for_party', party_id=tournament_request.party_id
    )


# --------------------------------------------------------------------- #
# Admin request edit (workspace-dim0.9)
# --------------------------------------------------------------------- #


def test_admin_update_request_form_requires_request_decide_permission(app):
    """AC2: the edit form is gated on `request_decide`, not `request_view`."""
    user = _make_user(permissions=frozenset({'lan_tournament.request_view'}))

    with app.test_request_context('/'):
        g.user = user

        with pytest.raises(Forbidden):
            views.update_request_form('11111111-1111-1111-1111-111111111111')

    user.has_permission.assert_called_with('lan_tournament.request_decide')


def test_admin_update_request_requires_request_decide_permission(app):
    """AC2: submitting the edit is gated on `request_decide`."""
    user = _make_user(permissions=frozenset({'lan_tournament.request_view'}))

    with app.test_request_context('/'):
        g.user = user

        with pytest.raises(Forbidden):
            views.update_request('11111111-1111-1111-1111-111111111111')

    user.has_permission.assert_called_with('lan_tournament.request_decide')


def test_admin_update_request_routes_resolve_under_hyphenated_prefix():
    """AC2/AC6: both routes exist, mounted under `/lan-tournaments`.

    A standalone throwaway app/registration, kept separate from the
    module-scoped `app` fixture used by every other test here, so this
    is the only test that cares about real URL routing.
    """
    routing_app = Flask(__name__)
    routing_app.register_blueprint(
        views.blueprint, url_prefix='/lan-tournaments'
    )

    request_id = '11111111-1111-1111-1111-111111111111'
    with routing_app.test_request_context('/'):
        from flask import url_for

        get_url = url_for(
            'lan_tournament_admin.update_request_form', request_id=request_id
        )
        post_url = url_for(
            'lan_tournament_admin.update_request', request_id=request_id
        )

    assert get_url == f'/lan-tournaments/requests/{request_id}/update'
    assert post_url == f'/lan-tournaments/requests/{request_id}/update'


def test_admin_update_request_attributes_the_edit_to_admin(app):
    """AC4: the view calls `update_request` with `by='admin'`.

    `tournament_request_service.update_request`'s own `by`/log-entry
    contract (the log entry carrying `data={'by': 'admin'}`) is
    exercised at the service level by
    `test_update_request_records_changed_field_names`; this covers
    the view's part of that contract -- that it actually passes
    `by='admin'` through, not the default `'proposer'`.
    """
    tournament_request = _make_tournament_request()
    party = SimpleNamespace(id='p1', max_ticket_quantity=100)
    updated_request = SimpleNamespace(id=tournament_request.id)
    event = MagicMock()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=_VALID_FORM_DATA),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_request_svc.update_request.return_value = Ok(
            (updated_request, event)
        )
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.request_decide',
                    'lan_tournament.request_view',
                }
            )
        )

        views.update_request(tournament_request.id)

    mock_request_svc.update_request.assert_called_once()
    call = mock_request_svc.update_request.call_args
    assert call.args[0] == tournament_request.id
    assert call.kwargs['by'] == 'admin'
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    mock_flash_success.assert_called_once()


def test_admin_update_request_race_lost_between_load_and_submit_is_flashed(
    app,
):
    """AC5 (defense in depth): the view's own `is_editable` pre-check
    (below) already refuses editing a non-`submitted` request before
    the service is even called (see
    `test_admin_update_request_redirects_when_not_editable`); this
    covers the narrower race window that pre-check cannot close --
    the status changes between this view's own fresh load and the
    service's row-locked re-read. The request here loads as
    `submitted` (the pre-check passes), but the service's own
    `expected_status` precondition (service-level:
    `test_update_request_on_accepted_returns_err`) still catches it:
    the error reaches `flash_error` via `gettext(...)`, and the form
    is re-rendered rather than the request silently succeeding.
    """
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.submitted
    )
    party = SimpleNamespace(id='p1', max_ticket_quantity=100)
    err_message = 'Request is no longer in the expected state.'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.update_request_form') as mock_update_request_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=_VALID_FORM_DATA),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_request_svc.update_request.return_value = Err(err_message)
        mock_update_request_form.return_value = 'rendered-form'
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.request_decide',
                    'lan_tournament.request_view',
                }
            )
        )

        result = views.update_request(tournament_request.id)

    # The error reaches the user -- it is not swallowed.
    mock_flash_error.assert_called_once_with(err_message)

    # The form is re-rendered (not a silent redirect to success).
    mock_update_request_form.assert_called_once()
    assert result == 'rendered-form'


def test_admin_update_request_form_redirects_when_not_editable(app):
    """m: GET refuses upfront (redirect), rather than rendering a form
    that would always fail, once the request is no longer `submitted`."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_redirect_to.return_value = 'redirected'
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.request_decide',
                    'lan_tournament.request_view',
                }
            )
        )

        result = views.update_request_form(tournament_request.id)

    mock_flash_error.assert_called_once_with(
        'This request can no longer be edited.'
    )
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    assert result == 'redirected'


def test_admin_update_request_redirects_when_not_editable(app):
    """m: POST refuses upfront (redirect) the same way, without ever
    calling the service."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_VALID_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_redirect_to.return_value = 'redirected'
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.request_decide',
                    'lan_tournament.request_view',
                }
            )
        )

        result = views.update_request(tournament_request.id)

    mock_request_svc.update_request.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'This request can no longer be edited.'
    )
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    assert result == 'redirected'


def test_admin_update_request_form_requires_request_view_permission_too(app):
    """m: `request_decide` alone is no longer enough for the edit form
    -- `request_view` is required too, so a `request_decide`-only
    admin (who cannot see private notes) is refused."""
    user = _make_user(
        permissions=frozenset({'lan_tournament.request_decide'})
    )

    with app.test_request_context('/'):
        g.user = user

        with pytest.raises(Forbidden):
            views.update_request_form('11111111-1111-1111-1111-111111111111')

    user.has_permission.assert_called_with('lan_tournament.request_view')


def test_admin_update_request_requires_request_view_permission_too(app):
    """m: same additional `request_view` requirement on the POST."""
    user = _make_user(
        permissions=frozenset({'lan_tournament.request_decide'})
    )

    with app.test_request_context('/'):
        g.user = user

        with pytest.raises(Forbidden):
            views.update_request('11111111-1111-1111-1111-111111111111')

    user.has_permission.assert_called_with('lan_tournament.request_view')


# --------------------------------------------------------------------- #
# Admin create-from-request status guard (workspace-dim0.13)
# --------------------------------------------------------------------- #

_CREATE_FORM_DATA = {
    'name': 'New Tournament',
    'from_request_id': '11111111-1111-1111-1111-111111111111',
}


# fmt: off
@pytest.mark.parametrize(
    'stale_status',
    [
        TournamentRequestStatus.submitted,
        TournamentRequestStatus.rejected,
        TournamentRequestStatus.withdrawn,
    ],
)
# fmt: on
def test_admin_create_from_non_accepted_request_never_calls_create_tournament(
    app, stale_status
):
    """AC1: a non-accepted source request never reaches `create_tournament`.

    Reachable without tampering: admin A opens the create form while
    the request is `accepted`, admin B rejects/decides/the proposer
    withdraws it, then A submits -- the hidden `from_request_id`
    field still names that now-stale request. `create_tournament`
    must never be called, and no tournament (orphaned, carrying a
    dead `created_from_request_id`) may be committed.
    """
    tournament_request = _make_tournament_request(status=stale_status)
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))

        result = views.create(party.id)

    mock_tournament_svc.create_tournament.assert_not_called()
    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'Request is no longer in the expected state.'
    )
    mock_create_form.assert_called_once()
    assert result == 'rendered-form'

    # x: the hidden field must not keep pointing at a request that can
    # never be linked -- otherwise every resubmit fails the same way.
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    mock_flash_notice.assert_called_once()


def test_admin_create_from_accepted_request_creates_and_links(app):
    """AC1 (positive case): an accepted source request still creates and links.

    The status guard added for the non-accepted cases above must not
    also block the one status it is meant to let through. Linking
    itself now happens inside `create_tournament`'s own transaction
    (fix cycle workspace-cg0k.2); the view's own follow-up call is the
    best-effort orga appointment.
    """
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')
    tournament = SimpleNamespace(
        id='22222222-2222-2222-2222-222222222222', name='New Tournament'
    )
    event = MagicMock()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Ok(
            (tournament, event)
        )
        mock_request_svc.appoint_proposer_orga.return_value = Ok(None)
        user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))
        g.user = user

        views.create(party.id)

    mock_tournament_svc.create_tournament.assert_called_once()
    assert (
        mock_tournament_svc.create_tournament.call_args.kwargs[
            'created_from_request_id'
        ]
        == tournament_request.id
    )
    assert (
        mock_tournament_svc.create_tournament.call_args.kwargs[
            'initiator_id'
        ]
        == user.id
    )
    mock_request_svc.appoint_proposer_orga.assert_called_once_with(
        tournament.id, tournament_request.proposer_id, user.id
    )
    mock_redirect_to.assert_called_once_with(
        '.view', tournament_id=tournament.id
    )
    mock_flash_success.assert_called_once()


def test_admin_create_flashes_warning_when_proposer_not_appointed(app):
    """I5: a negative orga-appointment outcome surfaces as a flashed
    warning, but never blocks the tournament create/redirect -- the
    tournament and its request link are already committed by the time
    this runs."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')
    tournament = SimpleNamespace(
        id='55555555-5555-5555-5555-555555555555', name='New Tournament'
    )
    event = MagicMock()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Ok(
            (tournament, event)
        )
        mock_request_svc.appoint_proposer_orga.return_value = Err(
            'The proposer could not be appointed as orga of the tournament.'
        )
        user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))
        g.user = user

        views.create(party.id)

    mock_request_svc.appoint_proposer_orga.assert_called_once_with(
        tournament.id, tournament_request.proposer_id, user.id
    )
    # Regression: this used to flash_error right next to the create
    # success flash, reading as if the create itself had failed.
    mock_flash_notice.assert_called_once_with(
        'The proposer could not be appointed as orga of the tournament.'
    )
    mock_flash_error.assert_not_called()
    mock_flash_success.assert_called_once()
    mock_redirect_to.assert_called_once_with(
        '.view', tournament_id=tournament.id
    )


# --------------------------------------------------------------------- #
# Admin create -- create_tournament Err clears the hidden link (x)
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    'dead_link_err_message',
    [
        'Request is no longer in the expected state.',
        'A tournament has already been created from this request.',
    ],
)
def test_admin_create_clears_request_link_on_dead_link_create_tournament_err(
    app, dead_link_err_message
):
    """x: `create_tournament`'s own row-locked re-check can still
    refuse a source request the view's own UX fast path
    (`_is_request_recreatable`) let through -- a race, or the unique
    constraint on `created_from_request_id`. That link is exactly as
    dead as one the fast path itself would have caught, so clear it
    the same way rather than leaving the hidden field to fail every
    resubmit for the same reason."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Err(
            dead_link_err_message
        )
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))

        result = views.create(party.id)

    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_error.assert_called_once_with(dead_link_err_message)
    mock_flash_notice.assert_called_once()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert result == 'rendered-form'


def test_admin_create_keeps_request_link_for_unrelated_create_tournament_err(
    app,
):
    """x (negative case): a `create_tournament` Err unrelated to the
    request link (plain field validation) must not clear the hidden
    field -- the admin's next submit should still try the same link,
    once they fix the actual problem."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')
    err_message = 'Tournament name must not exceed 80 characters.'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Err(err_message)
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))

        result = views.create(party.id)

    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_error.assert_called_once_with(err_message)
    mock_flash_notice.assert_not_called()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == str(tournament_request.id)
    assert result == 'rendered-form'


def test_admin_create_refuses_live_linked_tournament_created_request(app):
    """AC4 (refuse case): a `tournament_created` request with a *live*
    link (fix cycle 3, .17) is never treated as recreatable --
    `tournament_deleted` is False, so the guard falls through to the
    same refusal as any other non-recreatable status.
    """
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id='33333333-3333-3333-3333-333333333333',
        tournament_deleted=False,
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))

        result = views.create(party.id)

    mock_tournament_svc.create_tournament.assert_not_called()
    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'Request is no longer in the expected state.'
    )
    mock_create_form.assert_called_once()
    assert result == 'rendered-form'

    # x: a live-linked request can never become recreatable either --
    # clear the hidden field the same way.
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    mock_flash_notice.assert_called_once()


def test_admin_create_allows_recreate_after_tournament_deleted(app):
    """AC4 (allow case): `tournament_deleted` (fix cycle 3, .17) lets
    `create` proceed and link, exactly like the `accepted` positive
    case above.
    """
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=None,
        tournament_deleted=True,
    )
    party = SimpleNamespace(id='p1')
    tournament = SimpleNamespace(
        id='44444444-4444-4444-4444-444444444444', name='New Tournament'
    )
    event = MagicMock()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Ok(
            (tournament, event)
        )
        mock_request_svc.appoint_proposer_orga.return_value = Ok(None)
        user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))
        g.user = user

        views.create(party.id)

    mock_tournament_svc.create_tournament.assert_called_once()
    assert (
        mock_tournament_svc.create_tournament.call_args.kwargs[
            'created_from_request_id'
        ]
        == tournament_request.id
    )
    mock_request_svc.appoint_proposer_orga.assert_called_once_with(
        tournament.id, tournament_request.proposer_id, user.id
    )
    mock_redirect_to.assert_called_once_with(
        '.view', tournament_id=tournament.id
    )
    mock_flash_success.assert_called_once()


def test_admin_create_with_unresolvable_from_request_id_never_creates(app):
    """L2: `from_request_id` present but unresolvable (malformed, unknown,
    or belonging to another party) must never fall through to a silent,
    unlinked create."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_repo.find_request.return_value = None
        mock_party_svc.find_party.return_value = party
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_decide', 'lan_tournament.request_view'}))

        result = views.create(party.id)

    mock_tournament_svc.create_tournament.assert_not_called()
    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'Request is no longer in the expected state.'
    )
    mock_create_form.assert_called_once()
    assert result == 'rendered-form'

    # x: an unresolvable from_request_id must not keep re-appearing on
    # every resubmit either.
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    mock_flash_notice.assert_called_once()


# --------------------------------------------------------------------- #
# create_form GET prefill (L1, fix cycle workspace-cg0k.2)
# --------------------------------------------------------------------- #


def test_create_form_prefills_when_request_is_accepted(app):
    """L1 (positive case): an `accepted` source request prefills the form."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context(
            f'/?from_request={tournament_request.id}'
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_view', 'lan_tournament.request_decide'}))

        views.create_form(party.id)

    mock_flash_error.assert_not_called()
    context = mock_render.call_args.kwargs
    assert context['form'].from_request_id.data == str(tournament_request.id)
    assert context['form'].name.data == tournament_request.name


def test_create_form_skips_prefill_and_flashes_when_request_not_linkable(app):
    """L1 (negative case): a stale (non-`accepted`, link-cleared) source
    request must not be prefilled -- render the empty form and flash
    instead."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.rejected
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context(
            f'/?from_request={tournament_request.id}'
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_view', 'lan_tournament.request_decide'}))

        views.create_form(party.id)

    mock_flash_error.assert_called_once_with(
        'Request is no longer in the expected state.'
    )
    context = mock_render.call_args.kwargs
    assert not context['form'].from_request_id.data


def test_create_form_flashes_when_from_request_is_unresolvable(app):
    """Regression: `from_request` present but unresolvable (malformed,
    unknown, or belonging to another party) rendered the empty form
    silently on GET -- the POST path already flashed for the same
    case."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context(
            '/?from_request=00000000-0000-0000-0000-000000000000'
        ),
    ):
        mock_repo.find_request.return_value = None
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create', 'lan_tournament.request_view', 'lan_tournament.request_decide'}))

        views.create_form(party.id)

    mock_flash_error.assert_called_once_with(
        'Request is no longer in the expected state.'
    )
    context = mock_render.call_args.kwargs
    assert not context['form'].from_request_id.data


# --------------------------------------------------------------------- #
# create_form/create honour request_view/request_decide (G2, bead
# workspace-c9o4.3)
# --------------------------------------------------------------------- #


def test_create_form_skips_prefill_and_flashes_without_request_view(app):
    """G2: an admin with only `create` must not learn anything about a
    request through the prefill -- not even whether it resolves.
    Refuse before the lookup, and render the plain (unprefilled) form.
    """
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context(
            '/?from_request=11111111-1111-1111-1111-111111111111'
        ),
    ):
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create'}))

        views.create_form(party.id)

    mock_repo.find_request.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'You are not allowed to create a tournament from a request.'
    )
    context = mock_render.call_args.kwargs
    assert not context['form'].from_request_id.data


def test_create_form_skips_prefill_and_flashes_with_only_request_view(app):
    """J1 (core regression case): `request_view` alone used to be
    enough for the prefill, but submitting it was then refused by
    `create`'s own from-request gate (which required `request_decide`)
    -- clearing the hidden field, so a second submit created an
    unlinked tournament while the request stayed accepted forever.
    Both permissions are now required for the prefill too."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context(
            '/?from_request=11111111-1111-1111-1111-111111111111'
        ),
    ):
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset(
                {'lan_tournament.create', 'lan_tournament.request_view'}
            )
        )

        views.create_form(party.id)

    mock_repo.find_request.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'You are not allowed to create a tournament from a request.'
    )
    context = mock_render.call_args.kwargs
    assert not context['form'].from_request_id.data


def test_create_form_skips_prefill_and_flashes_with_only_request_decide(app):
    """J1 (matrix case): `request_decide` alone was already refused
    before this fix (the old check was solely on `request_view`) --
    still refused now that both are required."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context(
            '/?from_request=11111111-1111-1111-1111-111111111111'
        ),
    ):
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset(
                {'lan_tournament.create', 'lan_tournament.request_decide'}
            )
        )

        views.create_form(party.id)

    mock_repo.find_request.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'You are not allowed to create a tournament from a request.'
    )
    context = mock_render.call_args.kwargs
    assert not context['form'].from_request_id.data


def test_admin_create_refuses_from_request_without_request_decide(app):
    """G2: consuming a request via `from_request_id` moves it to
    `tournament_created` and appoints its proposer as orga -- a
    decision, not a view. A forged hidden field must not let
    `create`-only permission reach it."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_party_svc.find_party.return_value = party
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(permissions=frozenset({'lan_tournament.create'}))

        result = views.create(party.id)

    mock_repo.find_request.assert_not_called()
    mock_tournament_svc.create_tournament.assert_not_called()
    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_notice.assert_called_once()
    mock_flash_error.assert_called_once_with(
        'You are not allowed to create a tournament from a request.'
    )
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert result == 'rendered-form'


def test_admin_create_refuses_from_request_with_only_request_decide(app):
    """J1 (core regression case): before this fix, `request_decide`
    alone was enough to consume the request here -- an admin without
    `request_view` could convert it blind, then potentially 403 on the
    redirect that follows (`.view` requires `lan_tournament.view`, but
    the appoint-orga flash and the request's own state assume a
    `request_view`-capable admin). `request_view` is now required too."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_party_svc.find_party.return_value = party
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(
            permissions=frozenset(
                {'lan_tournament.create', 'lan_tournament.request_decide'}
            )
        )

        result = views.create(party.id)

    mock_repo.find_request.assert_not_called()
    mock_tournament_svc.create_tournament.assert_not_called()
    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_notice.assert_called_once()
    mock_flash_error.assert_called_once_with(
        'You are not allowed to create a tournament from a request.'
    )
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert result == 'rendered-form'


def test_admin_create_refuses_from_request_with_only_request_view(app):
    """J1 (matrix case): `request_view` alone was already refused
    before this fix (the old check was solely on `request_decide`) --
    still refused now that both are required."""
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.create_form') as mock_create_form,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_notice') as mock_flash_notice,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data=_CREATE_FORM_DATA
        ),
    ):
        mock_party_svc.find_party.return_value = party
        mock_create_form.return_value = 'rendered-form'
        g.user = _make_user(
            permissions=frozenset(
                {'lan_tournament.create', 'lan_tournament.request_view'}
            )
        )

        result = views.create(party.id)

    mock_repo.find_request.assert_not_called()
    mock_tournament_svc.create_tournament.assert_not_called()
    mock_request_svc.appoint_proposer_orga.assert_not_called()
    mock_flash_notice.assert_called_once()
    mock_flash_error.assert_called_once_with(
        'You are not allowed to create a tournament from a request.'
    )
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert result == 'rendered-form'


# --------------------------------------------------------------------- #
# Admin nav pending-request count (workspace-dim0.13, ADVISORY 3)
# --------------------------------------------------------------------- #


def test_pending_request_count_for_nav_skips_query_without_request_view(app):
    """AC4: viewers without `request_view` never touch the repository.

    They never see the requests tab this count feeds, so the nav
    global must return 0 without loading a single row.
    """
    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        app.test_request_context('/'),
    ):
        g.user = _make_user(permissions=frozenset({'lan_tournament.view'}))

        count = views._pending_request_count_for_nav('p1')

    assert count == 0
    mock_repo.count_requests_for_party_with_statuses.assert_not_called()
    mock_repo.get_requests_for_party.assert_not_called()


def test_pending_request_count_for_nav_uses_one_count_query(app):
    """AC4: with `request_view`, the nav count issues one COUNT query."""
    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        app.test_request_context('/'),
    ):
        mock_repo.count_requests_for_party_with_statuses.return_value = 3
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        count = views._pending_request_count_for_nav('p1')

    assert count == 3
    mock_repo.count_requests_for_party_with_statuses.assert_called_once_with(
        'p1', views._PENDING_REQUEST_STATUSES
    )
    mock_repo.get_requests_for_party.assert_not_called()
