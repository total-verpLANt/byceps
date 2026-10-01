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
from werkzeug.exceptions import Forbidden, MethodNotAllowed, NotFound

from byceps.services.lan_tournament import tournament_request_service
from byceps.services.lan_tournament.blueprints.admin import views
from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.elimination_mode import (
    EliminationMode,
)
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament_request import (
    TournamentRequest,
    TournamentRequestID,
    TournamentRequestStatus,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.tournament_request_domain_service import (
    analyze_field_gap,
)
from byceps.services.party.models import PartyID
from byceps.services.user.models import UserID
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


@pytest.fixture(autouse=True)
def _stub_create_wizard_context():
    """Keep `create_form` off `url_for` and the real party model.

    The wizard context is covered by `test_create_wizard_views_admin.py`
    and the render tests; these tests only care about the F-17 keys.
    """
    with (
        patch(f'{_V}._create_wizard_urls', return_value={}),
        patch(f'{_V}.build_create_wizard_context', return_value={}),
    ):
        yield


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
    created_at=None,
    decided_at=None,
    decided_by_id=None,
    created_tournament_id=None,
    tournament_deleted=False,
    team_size=1,
    participant_limit=16,
    special_rules=None,
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
        team_size=team_size,
        participant_limit=participant_limit,
        preferred_start_time=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        preferred_end_time=datetime(2026, 1, 1, 18, 0, tzinfo=UTC),
        # `requests_for_party` sorts on `.created_at`, so default it.
        created_at=created_at or datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        description='A description',
        special_rules=special_rules,
        notes=None,
        desired_template=None,
        decided_at=decided_at,
        decided_by_id=decided_by_id,
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
        is_editable_by_admin=status
        in (
            TournamentRequestStatus.submitted,
            TournamentRequestStatus.accepted,
        ),
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


# --------------------------------------------------------------------- #
# Admin request detail -- created tournament (AC4)
# --------------------------------------------------------------------- #


def test_view_request_passes_created_tournament(app):
    """AC4: the detail view resolves the created tournament (and
    whether the proposer is one of its orgas), so the Decision box's
    template can link to it and mention the appointment -- neither was
    ever passed into the context before this issue."""
    tournament_id = '22222222-2222-2222-2222-222222222222'
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
    )
    tournament = SimpleNamespace(id=tournament_id, name='Spring Cup')
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_orga_service') as mock_orga_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_request_svc.get_request_history.return_value = []
        mock_tournament_svc.find_tournament.return_value = tournament
        mock_orga_svc.is_orga_for_tournament.return_value = True
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_render_template.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        views.view_request(tournament_request.id)

    mock_tournament_svc.find_tournament.assert_called_once_with(tournament_id)
    mock_orga_svc.is_orga_for_tournament.assert_called_once_with(
        tournament_request.proposer_id, tournament_id
    )
    context = mock_render_template.call_args.kwargs
    assert context['created_tournament'] is tournament
    assert context['proposer_is_orga'] is True


def test_view_request_skips_tournament_lookup_without_created_tournament_id(
    app,
):
    """The common case (no tournament created yet, or its link was
    cleared by a deletion cascade) must not call the tournament/orga
    services at all -- `created_tournament_id` is `None`."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.submitted,
        created_tournament_id=None,
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_orga_service') as mock_orga_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_request_svc.get_request_history.return_value = []
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_render_template.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        views.view_request(tournament_request.id)

    mock_tournament_svc.find_tournament.assert_not_called()
    mock_orga_svc.is_orga_for_tournament.assert_not_called()
    context = mock_render_template.call_args.kwargs
    assert context['created_tournament'] is None
    assert context['proposer_is_orga'] is False


def test_view_request_passes_seats_for_history_initiators(app):
    """Issue 13: the History table's seat column needs one batch seat
    lookup, scoped to the request's party, over the history entries'
    initiator IDs -- not the proposer, the decider, or a system entry
    with no initiator at all."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted,
        proposer_id='u1',
        decided_by_id='u2',
    )
    party = SimpleNamespace(id='p1')
    history = [
        SimpleNamespace(
            initiator_id='u3', event_type='tournament-request-submitted'
        ),
        SimpleNamespace(
            initiator_id='u4', event_type='tournament-request-accepted'
        ),
        SimpleNamespace(
            initiator_id=None,
            event_type='tournament-request-tournament-deleted',
        ),
    ]

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_orga_service'),
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.build_seat_lookup') as mock_build_seat_lookup,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_request_svc.get_request_history.return_value = history
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_build_seat_lookup.return_value = {'u3': 'A12'}
        mock_render_template.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        views.view_request(tournament_request.id)

    mock_tournament_svc.find_tournament.assert_not_called()
    mock_build_seat_lookup.assert_called_once_with({'u3', 'u4'}, party.id)
    context = mock_render_template.call_args.kwargs
    assert context['seats_by_user_id'] == {'u3': 'A12'}


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
# --------------------------------------------------------------------- #


_REASON_REQUIRED = 'A reason is required to reject a request.'
_REASON_CONTROL_CHARS = 'The reason must not contain control characters.'
_REASON_TOO_LONG = 'The reason must not exceed 2000 characters.'


def _translate(msg, **kw):
    """Stand in for `gettext`: make a missed `gettext()` call visible."""
    return f'[de] {msg}'


def _decider_and_viewer() -> MagicMock:
    return _make_user(
        permissions=frozenset(
            {'lan_tournament.request_decide', 'lan_tournament.request_view'}
        )
    )


def test_reject_blank_reason_has_no_flash(app):
    """An empty reason is one field error on the re-rendered detail page."""
    tournament_request = _make_tournament_request()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=_translate),
        app.test_request_context('/', method='POST', data={'reason': ''}),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_view_request.return_value = 'rendered-detail'
        g.user = _decider_and_viewer()

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_flash_error.assert_not_called()
    mock_redirect_to.assert_not_called()
    mock_view_request.assert_called_once()
    call = mock_view_request.call_args
    assert call.args[0] == tournament_request.id
    erroneous_form = call.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.errors == [f'[de] {_REASON_REQUIRED}']
    assert result == 'rendered-detail'


def test_reject_whitespace_reason_has_no_flash(app):
    """A whitespace-only reason passes the form and the service catches it."""
    tournament_request = _make_tournament_request()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=_translate),
        app.test_request_context('/', method='POST', data={'reason': '   '}),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_request_svc.reject_request.return_value = Err(_REASON_REQUIRED)
        mock_view_request.return_value = 'rendered-detail'
        user = _decider_and_viewer()
        g.user = user

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_called_once_with(
        tournament_request.id, user.id, ''
    )
    mock_flash_error.assert_not_called()
    mock_redirect_to.assert_not_called()
    mock_view_request.assert_called_once()
    call = mock_view_request.call_args
    assert call.args[0] == tournament_request.id
    erroneous_form = call.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.data == '   '
    assert erroneous_form.reason.errors == [f'[de] {_REASON_REQUIRED}']
    assert result == 'rendered-detail'


def test_admin_reject_request_over_max_length_preserves_text_and_rerenders(
    app,
):
    """A too-long reason re-renders the detail page with the bound form."""
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
        g.user = _decider_and_viewer()

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_redirect_to.assert_not_called()
    mock_flash_error.assert_not_called()
    mock_view_request.assert_called_once()
    call = mock_view_request.call_args
    assert call.args[0] == tournament_request.id
    erroneous_form = call.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.data == long_reason
    assert erroneous_form.reason.errors == [_REASON_TOO_LONG]
    assert result == 'rendered-detail'


def test_reject_reason_failing_two_validators_shows_one_error(app):
    """A reason failing two validators shows one error."""
    tournament_request = _make_tournament_request()
    bad_reason = 'x' * 2001 + '\x00'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context(
            '/', method='POST', data={'reason': bad_reason}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_view_request.return_value = 'rendered-detail'
        g.user = _decider_and_viewer()

        views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_flash_error.assert_not_called()
    erroneous_form = mock_view_request.call_args.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.data == bad_reason
    assert erroneous_form.reason.errors == [_REASON_TOO_LONG]


def test_admin_reject_request_other_form_error_shows_form_message_and_rerenders(
    app,
):
    """Any form validation failure shows its message on the field."""
    tournament_request = _make_tournament_request()

    mock_form = MagicMock()
    mock_form.validate.return_value = False
    mock_form.reason.data = 'bad\x00reason'
    mock_form.reason.errors = [_REASON_CONTROL_CHARS]

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
        g.user = _decider_and_viewer()

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_not_called()
    mock_redirect_to.assert_not_called()
    mock_flash_error.assert_not_called()
    mock_view_request.assert_called_once_with(
        tournament_request.id, erroneous_reject_form=mock_form
    )
    assert mock_form.reason.errors == [_REASON_CONTROL_CHARS]
    assert result == 'rendered-detail'


@pytest.mark.parametrize(
    'err_message',
    [_REASON_CONTROL_CHARS, _REASON_TOO_LONG],
)
def test_reject_service_reason_err_is_a_field_error_not_a_flash(
    app, err_message
):
    """A service error on the reason is the field's one error."""
    tournament_request = _make_tournament_request()
    bad_reason = 'bad\x00reason'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=_translate),
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
        user = _decider_and_viewer()
        g.user = user

        result = views.reject_request(tournament_request.id)

    mock_request_svc.reject_request.assert_called_once_with(
        tournament_request.id, user.id, bad_reason
    )
    mock_redirect_to.assert_not_called()
    mock_flash_error.assert_not_called()
    mock_view_request.assert_called_once()
    call = mock_view_request.call_args
    assert call.args[0] == tournament_request.id
    erroneous_form = call.kwargs['erroneous_reject_form']
    assert erroneous_form.reason.data == bad_reason
    assert erroneous_form.reason.errors == [f'[de] {err_message}']
    assert result == 'rendered-detail'


def test_reject_reason_error_msgids_match_the_service():
    """The view routes a service `Err` to the reason field by msgid."""
    request_id = '11111111-1111-1111-1111-111111111111'

    service_errors = {
        tournament_request_service.reject_request(
            request_id, 'u1', reason
        ).unwrap_err()
        for reason in (
            '   ',
            'bad\x00reason',
            'x' * 2001,
        )
    }

    assert service_errors == views._REJECT_REASON_ERRORS
    assert service_errors == {
        _REASON_REQUIRED,
        _REASON_CONTROL_CHARS,
        _REASON_TOO_LONG,
    }


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
            permissions=frozenset(
                {'lan_tournament.request_decide', 'lan_tournament.request_view'}
            )
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


def test_reject_non_field_service_error_flashes_and_redirects(app):
    """An `expected_status` failure is flashed, not put on the field."""
    tournament_request = _make_tournament_request()
    err_message = 'Request is no longer in the expected state.'

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.view_request') as mock_view_request,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=_translate),
        app.test_request_context(
            '/', method='POST', data={'reason': 'Venue unavailable'}
        ),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_request_svc.reject_request.return_value = Err(err_message)
        mock_redirect_to.return_value = 'redirected'
        g.user = _decider_and_viewer()

        result = views.reject_request(tournament_request.id)

    mock_view_request.assert_not_called()
    mock_flash_error.assert_called_once_with(f'[de] {err_message}')
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    assert result == 'redirected'


def test_reject_get_redirects_to_detail(app):
    """A GET on the reject URL redirects to the detail page."""
    tournament_request = _make_tournament_request()

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_redirect_to.return_value = 'redirected'
        g.user = _decider_and_viewer()

        result = views.reject_request_redirect(tournament_request.id)

    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    assert result == 'redirected'


def test_reject_get_requires_request_decide_permission(app):
    """The GET carries the POST's `request_decide` gate."""
    user = _make_user(permissions=frozenset({'lan_tournament.request_view'}))

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        app.test_request_context('/'),
    ):
        g.user = user

        with pytest.raises(Forbidden):
            views.reject_request_redirect(
                '11111111-1111-1111-1111-111111111111'
            )

        mock_repo.find_request.assert_not_called()
        mock_redirect_to.assert_not_called()

    user.has_permission.assert_called_with('lan_tournament.request_decide')


def test_reject_get_requires_request_view_permission_too(app):
    """The redirect target needs `request_view`."""
    user = _make_user(permissions=frozenset({'lan_tournament.request_decide'}))

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        app.test_request_context('/'),
    ):
        g.user = user

        with pytest.raises(Forbidden):
            views.reject_request_redirect(
                '11111111-1111-1111-1111-111111111111'
            )

        mock_repo.find_request.assert_not_called()
        mock_redirect_to.assert_not_called()

    user.has_permission.assert_called_with('lan_tournament.request_view')


@pytest.mark.parametrize(
    'request_id',
    ['not-a-uuid', '11111111-1111-1111-1111-111111111111'],
    ids=['malformed-id', 'unknown-id'],
)
def test_reject_get_unknown_request_is_404(app, request_id):
    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = None
        g.user = _decider_and_viewer()

        with pytest.raises(NotFound):
            views.reject_request_redirect(request_id)

        mock_redirect_to.assert_not_called()


def test_reject_url_routes_get_to_redirect_and_post_to_reject():
    """The GET and POST resolve to their own endpoints."""
    routing_app = Flask(__name__)
    routing_app.register_blueprint(
        views.blueprint, url_prefix='/lan-tournaments'
    )
    path = (
        '/lan-tournaments/requests/11111111-1111-1111-1111-111111111111/reject'
    )
    adapter = routing_app.url_map.bind('localhost')

    get_endpoint, _ = adapter.match(path, method='GET')
    post_endpoint, _ = adapter.match(path, method='POST')

    assert get_endpoint == 'lan_tournament_admin.reject_request_redirect'
    assert post_endpoint == 'lan_tournament_admin.reject_request'
    with pytest.raises(MethodNotAllowed):
        adapter.match(path, method='PUT')


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
    """A service `Err` after the guard is flashed and re-renders the form."""
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


_ADMIN_EDITABLE_STATUSES = [
    TournamentRequestStatus.submitted,
    TournamentRequestStatus.accepted,
]
_ADMIN_LOCKED_STATUSES = [
    TournamentRequestStatus.rejected,
    TournamentRequestStatus.withdrawn,
    TournamentRequestStatus.tournament_created,
]


def _make_real_tournament_request(status) -> TournamentRequest:
    """Build a real dataclass, so `is_editable_by_admin` is the model's rule."""
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    return TournamentRequest(
        id=TournamentRequestID('11111111-1111-1111-1111-111111111111'),
        party_id=PartyID('p1'),
        number=7,
        proposer_id=UserID('u1'),
        created_at=now,
        status=status,
        name='Some Tournament',
        game='Some Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=16,
        preferred_start_time=now,
        preferred_end_time=now + timedelta(hours=8),
        description='A description',
    )


@pytest.mark.parametrize('status', _ADMIN_LOCKED_STATUSES)
def test_admin_update_request_form_redirects_when_not_admin_editable(
    app, status
):
    """The edit form is refused by redirect and flash for a decided request."""
    tournament_request = _make_real_tournament_request(status)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_redirect_to.return_value = 'redirected'
        g.user = _decider_and_viewer()

        result = views.update_request_form(tournament_request.id)

    mock_flash_error.assert_called_once_with(
        'This request can no longer be edited.'
    )
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    mock_render_template.assert_not_called()
    assert result == 'redirected'


@pytest.mark.parametrize('status', _ADMIN_LOCKED_STATUSES)
def test_admin_update_request_post_is_refused_when_not_admin_editable(
    app, status
):
    """A POST edit to a decided request never reaches the service."""
    tournament_request = _make_real_tournament_request(status)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=_VALID_FORM_DATA),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_redirect_to.return_value = 'redirected'
        g.user = _decider_and_viewer()

        result = views.update_request(tournament_request.id)

    mock_request_svc.update_request.assert_not_called()
    mock_flash_success.assert_not_called()
    mock_flash_error.assert_called_once_with(
        'This request can no longer be edited.'
    )
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )
    assert result == 'redirected'


@pytest.mark.parametrize('status', _ADMIN_EDITABLE_STATUSES)
def test_admin_update_request_form_is_allowed_when_admin_editable(app, status):
    """A submitted or an accepted request renders the edit form."""
    tournament_request = _make_real_tournament_request(status)
    party = SimpleNamespace(id='p1', max_ticket_quantity=240)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_render_template.return_value = 'rendered'
        g.user = _decider_and_viewer()

        result = views.update_request_form(tournament_request.id)

    mock_redirect_to.assert_not_called()
    mock_flash_error.assert_not_called()
    assert result == 'rendered'
    context = mock_render_template.call_args.kwargs
    assert context['tournament_request'] is tournament_request
    assert context['party_capacity'] == 240


@pytest.mark.parametrize('status', _ADMIN_EDITABLE_STATUSES)
def test_admin_update_request_post_reaches_the_service_when_admin_editable(
    app, status
):
    """A submitted or an accepted request is updated by the admin."""
    tournament_request = _make_real_tournament_request(status)
    party = SimpleNamespace(id='p1', max_ticket_quantity=100)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=_VALID_FORM_DATA),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_request_svc.update_request.return_value = Ok(
            (tournament_request, MagicMock())
        )
        g.user = _decider_and_viewer()

        views.update_request(tournament_request.id)

    mock_flash_error.assert_not_called()
    mock_flash_success.assert_called_once()
    mock_request_svc.update_request.assert_called_once()
    assert mock_request_svc.update_request.call_args.kwargs['by'] == 'admin'
    mock_redirect_to.assert_called_once_with(
        '.view_request', request_id=tournament_request.id
    )


def test_admin_update_request_form_passes_the_proposer_name(app):
    """The edit form names the proposer in its header and notice."""
    tournament_request = _make_tournament_request(proposer_id='u1')
    party = SimpleNamespace(id='p1', max_ticket_quantity=240)
    proposer = SimpleNamespace(screen_name='Oma_Gerda')

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_user_svc.get_users_indexed_by_id.return_value = {'u1': proposer}
        mock_render_template.return_value = 'rendered'
        g.user = _decider_and_viewer()

        views.update_request_form(tournament_request.id)

    mock_user_svc.get_users_indexed_by_id.assert_called_once_with({'u1'})
    context = mock_render_template.call_args.kwargs
    assert context['proposer_name'] == 'Oma_Gerda'
    assert context['users_by_id'] == {'u1': proposer}


@pytest.mark.parametrize(
    ('users_by_id', 'expected'),
    [
        ({'u1': SimpleNamespace(screen_name=None)}, '[de] Deleted user'),
        ({}, 'u1'),
    ],
)
def test_admin_update_request_form_proposer_name_fallbacks(
    app, users_by_id, expected
):
    """A deleted proposer reads as such; an unknown one shows the raw ID."""
    tournament_request = _make_tournament_request(proposer_id='u1')
    party = SimpleNamespace(id='p1', max_ticket_quantity=None)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}.gettext', side_effect=_translate),
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.get_party.return_value = party
        mock_user_svc.get_users_indexed_by_id.return_value = users_by_id
        mock_render_template.return_value = 'rendered'
        g.user = _decider_and_viewer()

        views.update_request_form(tournament_request.id)

    context = mock_render_template.call_args.kwargs
    assert context['proposer_name'] == expected


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
    'category': 'USER_ORGANIZED',
    'name': 'New Tournament',
    'from_request_id': '11111111-1111-1111-1111-111111111111',
    'contestant_type': 'SOLO',
    'game_format': 'ONE_V_ONE',
    'elimination_mode': 'SINGLE_ELIMINATION',
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
    mock_flash_error.assert_not_called()
    mock_create_form.assert_called_once()
    assert result == 'rendered-form'
    assert (
        'Request #%(number)s is no longer accepted. '
        'No tournament was created; your entries are kept.'
        in mock_create_form.call_args.args[1].from_request_id.errors
    )

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
    mock_flash_error.assert_not_called()
    mock_flash_notice.assert_called_once()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert dead_link_err_message in rerendered_form.from_request_id.errors
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
    mock_flash_error.assert_not_called()
    mock_flash_notice.assert_not_called()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == str(tournament_request.id)
    assert err_message in rerendered_form.form_errors
    assert not rerendered_form.from_request_id.errors
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
    mock_flash_error.assert_not_called()
    mock_create_form.assert_called_once()
    assert result == 'rendered-form'
    assert (
        'Request #%(number)s is no longer accepted. '
        'No tournament was created; your entries are kept.'
        in mock_create_form.call_args.args[1].from_request_id.errors
    )

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
    mock_flash_error.assert_not_called()
    mock_create_form.assert_called_once()
    assert result == 'rendered-form'
    assert (
        'Request is no longer in the expected state.'
        in mock_create_form.call_args.args[1].from_request_id.errors
    )

    # x: an unresolvable from_request_id must not keep re-appearing on
    # every resubmit either.
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    mock_flash_notice.assert_called_once()


# --------------------------------------------------------------------- #
# Admin create -- the effective contestant type clears irrelevant
# constraints (B3, workspace-pv3b.22)
# --------------------------------------------------------------------- #


def test_admin_create_team_type_clears_solo_fields(app):
    """B3: the "clear irrelevant constraints" block must branch on the
    effective contestant type, or `max_players`/`min_players`
    (SOLO-only fields) survive and wrongly cap joins on what is
    actually a team tournament. A blank type is no longer accepted by
    `create` (structure is required), so the type is explicit here."""
    party = SimpleNamespace(id='p1')
    tournament = SimpleNamespace(
        id='44444444-4444-4444-4444-444444444444', name='Blank Type Team'
    )
    event = MagicMock()

    data = {
        'name': 'Blank Type Team',
        'category': 'MAIN',
        'contestant_type': 'TEAM',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'max_players': '20',
        'max_players_in_team': '2',
    }

    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=data),
    ):
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Ok(
            (tournament, event)
        )
        g.user = _make_user(permissions=frozenset({'lan_tournament.create'}))

        views.create(party.id)

    mock_tournament_svc.create_tournament.assert_called_once()
    kwargs = mock_tournament_svc.create_tournament.call_args.kwargs
    assert kwargs['contestant_type'] is ContestantType.TEAM
    assert kwargs['max_players'] is None
    assert kwargs['min_players'] is None
    assert kwargs['max_players_in_team'] == 2


def test_admin_create_solo_type_clears_team_fields(app):
    """B3 (the other branch): a SOLO tournament must have the TEAM-only
    fields cleared, not left stale."""
    party = SimpleNamespace(id='p1')
    tournament = SimpleNamespace(
        id='66666666-6666-6666-6666-666666666666', name='Blank Type Solo'
    )
    event = MagicMock()

    data = {
        'name': 'Blank Type Solo',
        'category': 'MAIN',
        'contestant_type': 'SOLO',
        'game_format': 'ONE_V_ONE',
        'elimination_mode': 'SINGLE_ELIMINATION',
        'max_players_in_team': '1',
        'max_teams': '8',
    }

    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=data),
    ):
        mock_party_svc.find_party.return_value = party
        mock_tournament_svc.create_tournament.return_value = Ok(
            (tournament, event)
        )
        g.user = _make_user(permissions=frozenset({'lan_tournament.create'}))

        views.create(party.id)

    mock_tournament_svc.create_tournament.assert_called_once()
    kwargs = mock_tournament_svc.create_tournament.call_args.kwargs
    assert kwargs['contestant_type'] is ContestantType.SOLO
    assert kwargs['min_teams'] is None
    assert kwargs['max_teams'] is None
    assert kwargs['min_players_in_team'] is None
    assert kwargs['max_players_in_team'] is None


# --------------------------------------------------------------------- #
# Admin update -- same blank-type derivation on the UPDATE path (C3,
# workspace-pv3b.25)
# --------------------------------------------------------------------- #


def test_admin_update_blank_type_with_team_size_two_clears_solo_fields(app):
    """C3: the UPDATE path's own "clear irrelevant constraints" block
    must derive the effective type the same way the create path does
    (B3, fix 2) -- a blank `contestant_type` with team size 2 still
    derives to TEAM at the service layer, so `max_players`/
    `min_players` (SOLO-only fields) must be cleared, not survive to
    wrongly cap joins on what is actually a team tournament."""
    tournament = SimpleNamespace(
        id='44444444-4444-4444-4444-444444444444',
        name='Blank Type Team Update',
        tournament_status=TournamentStatus.DRAFT,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        group_size_min=None,
        group_size_max=None,
        advancement_count=None,
        point_table=None,
        start_time=None,
    )
    updated_tournament = SimpleNamespace(id=tournament.id, name=tournament.name)

    data = {
        'name': 'Blank Type Team Update',
        'category': 'MAIN',
        'max_players': '20',
        'max_players_in_team': '2',
    }

    with (
        patch(f'{_V}._get_tournament_or_404', return_value=tournament),
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=data),
    ):
        mock_tournament_svc.update_tournament.return_value = Ok(
            updated_tournament
        )
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.update'})
        )

        views.update(tournament.id)

    mock_tournament_svc.update_tournament.assert_called_once()
    kwargs = mock_tournament_svc.update_tournament.call_args.kwargs
    # Left `None` for the service layer's own derivation -- never
    # overwritten with the derived type here.
    assert kwargs['contestant_type'] is None
    assert kwargs['max_players'] is None
    assert kwargs['min_players'] is None
    assert kwargs['max_players_in_team'] == 2


def test_admin_update_blank_type_with_team_size_one_clears_team_fields(app):
    """C3 (the other branch): a blank `contestant_type` with team size
    1 derives to SOLO on the UPDATE path too -- the block must clear
    the TEAM-only fields, not leave them stale on what is actually a
    solo tournament."""
    tournament = SimpleNamespace(
        id='66666666-6666-6666-6666-666666666666',
        name='Blank Type Solo Update',
        tournament_status=TournamentStatus.DRAFT,
        min_players=None,
        max_players=None,
        min_teams=None,
        max_teams=None,
        min_players_in_team=None,
        max_players_in_team=None,
        group_size_min=None,
        group_size_max=None,
        advancement_count=None,
        point_table=None,
        start_time=None,
    )
    updated_tournament = SimpleNamespace(id=tournament.id, name=tournament.name)

    data = {
        'name': 'Blank Type Solo Update',
        'category': 'MAIN',
        'max_players_in_team': '1',
        'max_teams': '8',
    }

    with (
        patch(f'{_V}._get_tournament_or_404', return_value=tournament),
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.redirect_to'),
        patch(f'{_V}.flash_success'),
        patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
        app.test_request_context('/', method='POST', data=data),
    ):
        mock_tournament_svc.update_tournament.return_value = Ok(
            updated_tournament
        )
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.update'})
        )

        views.update(tournament.id)

    mock_tournament_svc.update_tournament.assert_called_once()
    kwargs = mock_tournament_svc.update_tournament.call_args.kwargs
    assert kwargs['contestant_type'] is None
    assert kwargs['min_teams'] is None
    assert kwargs['max_teams'] is None
    assert kwargs['min_players_in_team'] is None
    assert kwargs['max_players_in_team'] is None


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
        patch(f'{_V}.user_service') as mock_user_svc,
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
        mock_user_svc.find_screen_name.return_value = 'Proposer'
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
# create_form prefill -- team size/limit/contestant type, provenance
# banner context (workspace-pv3b.2, defect workspace-hm1a)
# --------------------------------------------------------------------- #

# `analyze_field_gap(request).supplied` fields (minus `party`/`origin`,
# which have no create-form counterpart at all) mapped to the form
# attribute the prefill puts them in. `team_size`/`participant_limit`
# only resolve to a single, unambiguous attribute for a *team* request
# (see the module docstring on `_prefill_form_from_request`); a solo
# request folds both into `max_players` instead.
_SUPPLIED_FIELD_TO_TEAM_FORM_ATTR = {
    'name': 'name',
    'game': 'game',
    'game_format': 'game_format',
    'elimination_mode': 'elimination_mode',
    'contestant_type': 'contestant_type',
    'team_size': 'min_players_in_team',
    'participant_limit': 'max_teams',
    'preferred_start_time': 'start_time',
    'description': 'description',
    'ruleset': 'ruleset',
}


def _create_form_via_get(app, tournament_request, party, *, screen_name='Proposer'):
    """Drive `create_form`'s GET/`?from_request=` prefill path and
    return the rendered context dict, with the request-lookup and
    user-lookup collaborators mocked out."""
    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
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
        mock_user_svc.find_screen_name.return_value = screen_name
        mock_render.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.create',
                    'lan_tournament.request_view',
                    'lan_tournament.request_decide',
                }
            )
        )

        views.create_form(party.id)

    mock_flash_error.assert_not_called()
    return mock_render.call_args.kwargs


def test_create_form_prefills_solo_limit_into_max_players(app):
    """Team size 1 derives contestant type SOLO and maps the request's
    limit into `max_players`."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted,
        team_size=1,
        participant_limit=16,
    )
    party = SimpleNamespace(id='p1')

    context = _create_form_via_get(app, tournament_request, party)

    form = context['form']
    assert form.contestant_type.data == 'SOLO'
    assert form.max_players.data == 16


def test_create_form_prefills_team_size_and_limit_into_team_fields(app):
    """Team size > 1 derives contestant type TEAM, maps the limit into
    `max_teams`, and the team size into both team-size bounds."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted,
        team_size=2,
        participant_limit=12,
    )
    party = SimpleNamespace(id='p1')

    context = _create_form_via_get(app, tournament_request, party)

    form = context['form']
    assert form.contestant_type.data == 'TEAM'
    assert form.max_teams.data == 12
    assert form.min_players_in_team.data == 2
    assert form.max_players_in_team.data == 2


def test_prefill_covers_every_supplied_field(app):
    """Every field `analyze_field_gap` claims as "supplied" -- other
    than `party`/`origin`, which name no create-form field at all --
    must land in a real, non-`None` form attribute after a prefill."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted,
        team_size=2,
        participant_limit=12,
        special_rules='No pocket picking.',
    )
    party = SimpleNamespace(id='p1')

    context = _create_form_via_get(app, tournament_request, party)
    form = context['form']

    gap = analyze_field_gap(tournament_request)
    for field_name in gap.supplied:
        if field_name in ('party', 'origin'):
            continue
        form_attr = _SUPPLIED_FIELD_TO_TEAM_FORM_ATTR[field_name]
        assert getattr(form, form_attr).data is not None, field_name


def test_create_form_passes_source_request_for_banner(app):
    """GET `?from_request=<accepted>` passes the request itself so the
    template can render the provenance banner and back link."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')

    context = _create_form_via_get(app, tournament_request, party)

    assert context['source_request'] is not None
    assert context['source_request'].id == tournament_request.id


def test_create_form_rerender_keeps_source_request(app):
    """An erroneous-form re-render (`create`'s own `create_form(party.id,
    form)` call after a validation error) must keep showing the
    provenance banner -- it reads `from_request_id` off the posted
    form, not the query string."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')
    erroneous_form = MagicMock()
    erroneous_form.from_request_id.data = str(tournament_request.id)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_user_svc.find_screen_name.return_value = 'Proposer'
        mock_render.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.create',
                    'lan_tournament.request_view',
                    'lan_tournament.request_decide',
                }
            )
        )

        views.create_form(party.id, erroneous_form)

    context = mock_render.call_args.kwargs
    assert context['form'] is erroneous_form
    assert context['source_request'] is not None
    assert context['source_request'].id == tournament_request.id


def test_create_form_rerender_skips_lookup_without_request_view_and_decide(
    app,
):
    """Regression: the same permission gate the GET prefill branch
    applies must also cover the erroneous-form re-render lookup -- a
    `create`-only admin must not learn about a request (via the
    banner) just by forging `from_request_id` into a form submission
    that happens to fail validation."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.accepted
    )
    party = SimpleNamespace(id='p1')
    erroneous_form = MagicMock()
    erroneous_form.from_request_id.data = str(tournament_request.id)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_render.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.create'})
        )

        views.create_form(party.id, erroneous_form)

    mock_repo.find_request.assert_not_called()
    mock_user_svc.find_screen_name.assert_not_called()
    context = mock_render.call_args.kwargs
    assert context['source_request'] is None


def test_create_form_rerender_drops_banner_for_unrecreatable_request(app):
    """A1 (fix cycle 1, Issue 2): an erroneous-form re-render must
    apply the same recreatability check (`_is_request_recreatable`)
    as the GET prefill branch -- a request that moved on (decided by
    another admin, withdrawn) since the form was first rendered must
    not keep showing the provenance banner."""
    tournament_request = _make_tournament_request(
        status=TournamentRequestStatus.submitted
    )
    party = SimpleNamespace(id='p1')
    erroneous_form = MagicMock()
    erroneous_form.from_request_id.data = str(tournament_request.id)

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render,
        app.test_request_context('/'),
    ):
        mock_repo.find_request.return_value = tournament_request
        mock_party_svc.find_party.return_value = party
        mock_user_svc.find_screen_name.return_value = 'Proposer'
        mock_render.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset(
                {
                    'lan_tournament.create',
                    'lan_tournament.request_view',
                    'lan_tournament.request_decide',
                }
            )
        )

        views.create_form(party.id, erroneous_form)

    context = mock_render.call_args.kwargs
    assert context['form'] is erroneous_form
    assert context['source_request'] is None


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
    mock_flash_error.assert_not_called()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert (
        'You are not allowed to create a tournament from a request.'
        in rerendered_form.from_request_id.errors
    )
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
    mock_flash_error.assert_not_called()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert (
        'You are not allowed to create a tournament from a request.'
        in rerendered_form.from_request_id.errors
    )
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
    mock_flash_error.assert_not_called()
    mock_create_form.assert_called_once()
    rerendered_form = mock_create_form.call_args.args[1]
    assert rerendered_form.from_request_id.data == ''
    assert (
        'You are not allowed to create a tournament from a request.'
        in rerendered_form.from_request_id.errors
    )
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
        'p1', views._OPEN_REQUEST_STATUSES
    )
    mock_repo.get_requests_for_party.assert_not_called()


def test_nav_count_counts_submitted_only(app):
    """The nav badge counts only `submitted` requests."""
    assert views._OPEN_REQUEST_STATUSES == frozenset(
        {TournamentRequestStatus.submitted}
    )
    assert (
        TournamentRequestStatus.accepted not in views._OPEN_REQUEST_STATUSES
    )
    assert (
        TournamentRequestStatus.accepted in views._PENDING_REQUEST_STATUSES
    )

    with (
        patch(f'{_V}.tournament_request_repository') as mock_repo,
        app.test_request_context('/'),
    ):
        mock_repo.count_requests_for_party_with_statuses.return_value = 1
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        views._pending_request_count_for_nav('p1')

    mock_repo.count_requests_for_party_with_statuses.assert_called_once_with(
        'p1', frozenset({TournamentRequestStatus.submitted})
    )


def test_queue_orders_submitted_first_newest_first(app):
    """Pending requests list `submitted` first, newest first."""
    now = datetime.now(UTC)
    old_submitted = _make_tournament_request(
        id='r-old-submitted',
        status=TournamentRequestStatus.submitted,
        proposer_id='u1',
        created_at=now - timedelta(days=2),
    )
    new_submitted = _make_tournament_request(
        id='r-new-submitted',
        status=TournamentRequestStatus.submitted,
        proposer_id='u1',
        created_at=now - timedelta(hours=1),
    )
    newest_accepted = _make_tournament_request(
        id='r-newest-accepted',
        status=TournamentRequestStatus.accepted,
        proposer_id='u1',
        created_at=now,
    )
    old_done = _make_tournament_request(
        id='r-old-done',
        status=TournamentRequestStatus.rejected,
        proposer_id='u1',
        created_at=now - timedelta(days=5),
    )
    new_done = _make_tournament_request(
        id='r-new-done',
        status=TournamentRequestStatus.withdrawn,
        proposer_id='u1',
        created_at=now - timedelta(days=1),
    )
    party = SimpleNamespace(id='p1')

    with (
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(
            'byceps.util.framework.templating.render_template'
        ) as mock_render_template,
        app.test_request_context('/'),
    ):
        mock_party_svc.find_party.return_value = party
        mock_request_svc.get_visible_requests_for_user.return_value = [
            newest_accepted,
            old_submitted,
            new_done,
            new_submitted,
            old_done,
        ]
        mock_user_svc.get_users_indexed_by_id.return_value = {}
        mock_render_template.return_value = 'rendered'
        g.user = _make_user(
            permissions=frozenset({'lan_tournament.request_view'})
        )

        views.requests_for_party(party.id)

    context = mock_render_template.call_args.kwargs
    assert [r.id for r in context['pending_requests']] == [
        'r-new-submitted',
        'r-old-submitted',
        'r-newest-accepted',
    ]
    assert [r.id for r in context['done_requests']] == [
        'r-new-done',
        'r-old-done',
    ]
