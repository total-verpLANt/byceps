"""
tests.unit.services.lan_tournament.test_tournament_request_views_site
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the site-side tournament request routes and forms.

Routes under test:
  GET  /requests/propose            -- propose_form
  POST /requests/propose             -- propose
  GET  /requests                     -- my_requests
  GET  /requests/<id>/update          -- update_request_form
  POST /requests/<id>/update          -- update_request
  POST /requests/<id>/withdraw        -- withdraw_request

Also covers the shared visibility contract on
``tournament_request_service.get_visible_requests_for_user`` (party- and
proposer-scoped for everyone except an explicit admin caller) and proves
the site index route never touches request data at all -- an unaccepted
request cannot leak into the site tournament listing because nothing in
``index()`` looks at it in the first place.
"""

from contextlib import contextmanager
from datetime import datetime, UTC
import inspect
from unittest.mock import MagicMock, patch

from flask import Flask
from flask_babel import Babel
import pytest
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament import (
    tournament_request_domain_service,
    tournament_request_service,
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
from byceps.services.party.models import PartyID
from byceps.util.result import Ok

from tests.helpers import generate_uuid


_V = 'byceps.services.lan_tournament.blueprints.site.views'

PARTY_ID = PartyID(str(generate_uuid()))
PROPOSER_USER_ID = generate_uuid()
OTHER_USER_ID = generate_uuid()


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def app():
    """A minimal Flask app with Babel wired up.

    `LocalizedForm` reads `current_app.config['LOCALE']`, and
    `gettext`/`lazy_gettext`/`to_utc`/`to_user_timezone` all need a
    live Babel extension instance -- without it they raise `KeyError`
    on `app.extensions['babel']` rather than merely misbehaving.
    """
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_LOCALE'] = 'en'
    a.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'
    Babel(a)
    return a


def _make_user(user_id, *, authenticated=True):
    u = MagicMock()
    u.id = user_id
    u.authenticated = authenticated
    return u


def _make_party(*, max_ticket_quantity=None):
    p = MagicMock()
    p.id = PARTY_ID
    p.max_ticket_quantity = max_ticket_quantity
    return p


def _make_request(
    *,
    status=TournamentRequestStatus.submitted,
    proposer_id=PROPOSER_USER_ID,
    number=1,
    name='Test Cup',
    special_rules=None,
    notes=None,
    desired_template=None,
    rejection_reason=None,
    created_tournament_id=None,
) -> TournamentRequest:
    now = datetime.now(UTC)
    return TournamentRequest(
        id=TournamentRequestID(generate_uuid()),
        party_id=PARTY_ID,
        number=number,
        proposer_id=proposer_id,
        created_at=now,
        status=status,
        name=name,
        game='Test Game',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
        team_size=1,
        participant_limit=8,
        preferred_start_time=now,
        preferred_end_time=now,
        description='A description.',
        special_rules=special_rules,
        notes=notes,
        desired_template=desired_template,
        rejection_reason=rejection_reason,
        created_tournament_id=created_tournament_id,
    )


_VALID_PROPOSE_FORM_DATA = {
    'name': 'Test Cup',
    'game': 'Test Game',
    'game_format': GameFormat.ONE_V_ONE.value,
    'elimination_mode': EliminationMode.SINGLE_ELIMINATION.value,
    'team_size': '1',
    'participant_limit': '8',
    'preferred_start_time': '2026-06-01T10:00',
    'preferred_end_time': '2026-06-01T12:00',
    'description': 'A description.',
}


@contextmanager
def _patched_propose_view(
    app,
    *,
    authenticated=True,
    has_ticket=True,
    party_max_ticket_quantity=None,
):
    party = _make_party(max_ticket_quantity=party_max_ticket_quantity)

    with app.app_context():
        with (
            patch(f'{_V}.gettext', side_effect=lambda msg, **kw: msg),
            patch(f'{_V}.flash_error') as mock_flash_error,
            patch(f'{_V}.flash_success') as mock_flash_success,
            patch(f'{_V}.redirect_to') as mock_redirect_to,
            patch(f'{_V}.ticket_service') as mock_ticket_svc,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.propose_form') as mock_propose_form,
            patch(f'{_V}.g') as mock_g,
        ):
            mock_get_party.return_value = party
            mock_ticket_svc.uses_any_ticket_for_party.return_value = has_ticket
            mock_g.user = _make_user(
                PROPOSER_USER_ID, authenticated=authenticated
            )
            mock_g.party = MagicMock()
            mock_g.party.id = PARTY_ID

            yield {
                'flash_error': mock_flash_error,
                'flash_success': mock_flash_success,
                'redirect_to': mock_redirect_to,
                'ticket_svc': mock_ticket_svc,
                'get_party': mock_get_party,
                'propose_form': mock_propose_form,
                'g': mock_g,
                'party': party,
            }


# ------------------------------------------------------------------ #
# 1. login required on every request route
# ------------------------------------------------------------------ #


def test_propose_requires_login():
    from byceps.services.lan_tournament.blueprints.site import views

    assert hasattr(views.propose_form, '__wrapped__')
    assert hasattr(views.propose_form.__wrapped__, '__wrapped__')
    assert hasattr(views.propose, '__wrapped__')


def test_all_request_routes_require_login():
    from byceps.services.lan_tournament.blueprints.site import views

    for name in (
        'propose_form',
        'propose',
        'my_requests',
        'update_request_form',
        'update_request',
        'withdraw_request',
    ):
        fn = getattr(views, name)
        assert hasattr(fn, '__wrapped__'), f'{name} is missing @login_required'


# ------------------------------------------------------------------ #
# 2. ticket gate on propose
# ------------------------------------------------------------------ #


def test_propose_requires_party_ticket(app):
    """POST propose without a party ticket flashes and redirects, no submit."""
    with _patched_propose_view(app, has_ticket=False) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.propose.__wrapped__

        with (
            app.test_request_context('/', method='POST', data={}),
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
        ):
            raw_fn()

        mock_request_svc.submit_request.assert_not_called()

    mocks['flash_error'].assert_called_once_with(
        'You must have a valid ticket for this party to propose a tournament.'
    )
    mocks['redirect_to'].assert_called_once_with('.propose_form')


# ------------------------------------------------------------------ #
# 3. mandatory field validation on the form itself
# ------------------------------------------------------------------ #


def test_propose_form_validates_mandatory_fields(app):
    from byceps.services.lan_tournament.blueprints.site.forms import (
        TournamentProposeForm,
    )

    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict({}))
        form.set_format_choices()

        assert form.validate() is False

        for field_name in (
            'name',
            'game',
            'game_format',
            'elimination_mode',
            'team_size',
            'participant_limit',
            'preferred_start_time',
            'preferred_end_time',
            'description',
        ):
            assert getattr(form, field_name).errors, (
                f'{field_name} should be required'
            )

        for field_name in ('special_rules', 'notes', 'desired_template'):
            assert not getattr(form, field_name).errors, (
                f'{field_name} should be optional'
            )


# ------------------------------------------------------------------ #
# 3b. no-JS gap: set_format_choices with no/invalid selected format
# (workspace-vxrc.3, issue k.1)
# ------------------------------------------------------------------ #


def test_set_format_choices_with_no_format_selected_disables_no_option(app):
    """A no-JS user who hasn't posted a `game_format` yet must see
    every elimination mode enabled on the first render -- silently
    guessing ONE_V_ONE's restrictions here would have blocked a
    format-appropriate first pick (e.g. Highscore-only NONE) on
    submit, since disabled radios don't post. The real compatibility
    rule still runs in `validate_request_fields`."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        TournamentProposeForm,
    )

    with app.test_request_context('/'):
        form = TournamentProposeForm(MultiDict({}))
        form.set_format_choices()

        assert form.game_format.data is None
        assert form.elimination_mode_options
        assert all(
            not option['disabled'] for option in form.elimination_mode_options
        )
        assert all(
            option['reason'] is None
            for option in form.elimination_mode_options
        )


def test_set_format_choices_with_invalid_format_disables_no_option(app):
    """Same guarantee for a posted value that doesn't parse as a
    `GameFormat` at all (e.g. a tampered/stale value)."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        TournamentProposeForm,
    )

    with app.test_request_context('/'):
        form = TournamentProposeForm(
            MultiDict({'game_format': 'NOT_A_REAL_FORMAT'})
        )
        form.set_format_choices()

        assert all(
            not option['disabled'] for option in form.elimination_mode_options
        )


def test_set_format_choices_with_valid_format_still_disables_incompatible_modes(
    app,
):
    """Regression guard: once a format IS selected, behaviour is
    unchanged -- incompatible modes are still disabled with a reason."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        TournamentProposeForm,
    )

    with app.test_request_context('/'):
        form = TournamentProposeForm(
            MultiDict({'game_format': GameFormat.HIGHSCORE.value})
        )
        form.set_format_choices()

        disabled = {
            option['value']: option['reason']
            for option in form.elimination_mode_options
            if option['disabled']
        }
        assert disabled
        assert all(reason is not None for reason in disabled.values())
        assert EliminationMode.NONE.value not in disabled


# ------------------------------------------------------------------ #
# 4. visibility contract on get_visible_requests_for_user
# ------------------------------------------------------------------ #


def test_request_visibility_hides_submitted_from_other_users():
    """A non-admin caller only ever gets their own, proposer-scoped rows.

    `get_requests_for_party` (the all-of-party read) is never even
    called on this path -- another user's submitted request has no
    way to reach a non-admin caller's result set.
    """
    own_request = _make_request(proposer_id=PROPOSER_USER_ID)

    with patch(
        'byceps.services.lan_tournament.tournament_request_service'
        '.tournament_request_repository'
    ) as mock_repo:
        mock_repo.get_requests_for_proposer.return_value = [own_request]

        result = tournament_request_service.get_visible_requests_for_user(
            PARTY_ID, PROPOSER_USER_ID, is_admin=False
        )

        mock_repo.get_requests_for_proposer.assert_called_once_with(
            PARTY_ID, PROPOSER_USER_ID
        )
        mock_repo.get_requests_for_party.assert_not_called()
        assert result == [own_request]


def test_request_visibility_shows_own_request_to_proposer():
    own_request = _make_request(proposer_id=PROPOSER_USER_ID, name='Mine')

    with patch(
        'byceps.services.lan_tournament.tournament_request_service'
        '.tournament_request_repository'
    ) as mock_repo:
        mock_repo.get_requests_for_proposer.return_value = [own_request]

        result = tournament_request_service.get_visible_requests_for_user(
            PARTY_ID, PROPOSER_USER_ID, is_admin=False
        )

        assert result == [own_request]
        assert result[0].proposer_id == PROPOSER_USER_ID


def test_request_visibility_shows_all_to_admin():
    """An admin caller sees every request for the party, any proposer."""
    own_request = _make_request(proposer_id=PROPOSER_USER_ID, name='Mine')
    other_request = _make_request(proposer_id=OTHER_USER_ID, name='Theirs')

    with patch(
        'byceps.services.lan_tournament.tournament_request_service'
        '.tournament_request_repository'
    ) as mock_repo:
        mock_repo.get_requests_for_party.return_value = [
            own_request,
            other_request,
        ]

        result = tournament_request_service.get_visible_requests_for_user(
            PARTY_ID, PROPOSER_USER_ID, is_admin=True
        )

        mock_repo.get_requests_for_party.assert_called_once_with(PARTY_ID)
        mock_repo.get_requests_for_proposer.assert_not_called()
        assert result == [own_request, other_request]


def test_request_visibility_excludes_requests_from_site_index():
    """`index()` never touches request data -- invisibility is structural.

    An unaccepted request never becomes a tournament, so it cannot
    appear in `get_tournaments_for_party`'s results either; this test
    additionally proves the view never even imports/calls the request
    service, so there is no filter here to get wrong later.
    """
    from byceps.services.lan_tournament.blueprints.site import views

    assert 'tournament_request' not in inspect.getsource(views.index)

    with (
        patch(f'{_V}.tournament_request_service') as mock_request_svc,
        patch(f'{_V}.tournament_service') as mock_tournament_svc,
        patch(f'{_V}.tournament_team_service') as mock_team_svc,
        patch(f'{_V}._get_current_party_or_404') as mock_get_party,
    ):
        mock_get_party.return_value = _make_party()
        mock_tournament_svc.get_tournaments_for_party.return_value = []
        mock_tournament_svc.get_participant_counts_for_tournaments.return_value = {}
        mock_team_svc.get_team_counts_for_tournaments.return_value = {}

        result = views.index.__wrapped__()

        assert set(result.keys()) == {
            'tournaments',
            'participant_counts',
            'team_counts',
        }
        assert mock_request_svc.method_calls == []


# ------------------------------------------------------------------ #
# 5. blank optional fields normalise to None
# ------------------------------------------------------------------ #


def test_submit_request_stores_optional_fields_as_none_when_absent(app):
    """special_rules/notes/desired_template, submitted as blank textarea
    values (WTForms hands back `''`, not `None`), reach the service as
    `None`.
    """
    with _patched_propose_view(app) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.propose.__wrapped__

        data = dict(_VALID_PROPOSE_FORM_DATA)
        data['special_rules'] = ''
        data['notes'] = ''
        data['desired_template'] = ''

        fake_request = _make_request()
        with (
            app.test_request_context('/', method='POST', data=data),
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
        ):
            mock_request_svc.submit_request.return_value = Ok(
                (fake_request, MagicMock())
            )
            raw_fn()

            mock_request_svc.submit_request.assert_called_once()
            call_kwargs = mock_request_svc.submit_request.call_args.kwargs
            assert call_kwargs['special_rules'] is None
            assert call_kwargs['notes'] is None
            assert call_kwargs['desired_template'] is None

    mocks['flash_success'].assert_called_once()


# ------------------------------------------------------------------ #
# 6. participant-limit/party-capacity cap survives a bypassed JS check
# ------------------------------------------------------------------ #


def test_propose_rejects_participant_limit_above_party_capacity(app):
    """A POST that satisfies the form's own NumberRange but exceeds the
    party's ticket capacity is still rejected -- by the real,
    un-mocked service and domain validation, exactly as it would be
    if a client-side cap helper had been bypassed or never ran.
    """
    with _patched_propose_view(app, party_max_ticket_quantity=100) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.propose.__wrapped__

        data = dict(_VALID_PROPOSE_FORM_DATA)
        data['team_size'] = '1'
        data['participant_limit'] = '1000'  # cap is 100 // 1 = 100

        with app.test_request_context('/', method='POST', data=data):
            # tournament_request_service is intentionally left real:
            # validate_request_fields rejects this before any
            # repository access is attempted.
            raw_fn()

    mocks['flash_error'].assert_called_once_with(
        'Participant limit exceeds party capacity.'
    )
    mocks['propose_form'].assert_called_once()


def test_participant_limit_cap_is_enforced_by_the_domain_service_directly():
    """Same rule, isolated at the function the view relies on."""
    result = tournament_request_domain_service.validate_request_fields(
        name='Test Cup',
        game='Test Game',
        team_size=1,
        participant_limit=101,
        party_capacity=100,
        preferred_start_time=datetime.now(UTC),
        preferred_end_time=datetime.now(UTC),
        description='A description.',
        game_format=GameFormat.ONE_V_ONE,
        elimination_mode=EliminationMode.SINGLE_ELIMINATION,
    )

    assert result.is_err()
    assert result.unwrap_err() == 'Participant limit exceeds party capacity.'


# ------------------------------------------------------------------ #
# 7. request-scoped ownership check, adversarial client-supplied ID
# ------------------------------------------------------------------ #


def test_get_own_request_or_404_rejects_a_request_not_visible_to_the_caller(
    app,
):
    """A request ID that is not in the caller's own visible set 404s.

    Covers both "belongs to someone else" and "belongs to another
    party" in one go, since `get_visible_requests_for_user(is_admin=False)`
    already scopes on both -- the view never trusts the client-supplied
    ID by itself.
    """
    from werkzeug.exceptions import NotFound

    from byceps.services.lan_tournament.blueprints.site import views

    someone_elses_request = _make_request(proposer_id=OTHER_USER_ID)
    callers_own_other_request = _make_request(proposer_id=PROPOSER_USER_ID)
    party = _make_party()

    with app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            # The caller does have visible requests of their own -- just
            # not this particular ID. A helper that returned the first
            # visible request unconditionally (ignoring the ID match)
            # would wrongly hand back `callers_own_other_request` here;
            # a non-empty-but-non-matching list is what actually
            # exercises the per-ID ownership check, unlike an empty list
            # (which 404s either way).
            mock_request_svc.get_visible_requests_for_user.return_value = [
                callers_own_other_request
            ]

            with pytest.raises(NotFound):
                views._get_own_request_or_404(
                    party, str(someone_elses_request.id)
                )

            mock_request_svc.get_visible_requests_for_user.assert_called_once_with(
                party.id, PROPOSER_USER_ID, is_admin=False
            )


def test_get_own_request_or_404_returns_the_caller_s_own_request(app):
    from byceps.services.lan_tournament.blueprints.site import views

    own_request = _make_request(proposer_id=PROPOSER_USER_ID)
    party = _make_party()

    with app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_request_svc.get_visible_requests_for_user.return_value = [
                own_request
            ]

            result = views._get_own_request_or_404(party, str(own_request.id))

            assert result is own_request


# ------------------------------------------------------------------ #
# 8. my_requests: DRAFT tournaments never get a live link
# (workspace-vxrc.3, issue h)
# ------------------------------------------------------------------ #


def test_my_requests_computes_draft_tournament_ids(app):
    """`my_requests`'s `draft_tournament_ids` set must include a
    DRAFT-status created tournament and exclude a live one -- the
    template gates the link on this precomputed set (site `view`
    404s DRAFT for everyone, proposer included) rather than a
    template-only status check."""
    from byceps.services.lan_tournament.blueprints.site import views
    from byceps.services.lan_tournament.models.tournament import TournamentID
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )

    draft_tournament_id = TournamentID(generate_uuid())
    live_tournament_id = TournamentID(generate_uuid())

    draft_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=draft_tournament_id,
        number=1,
    )
    live_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=live_tournament_id,
        number=2,
    )

    draft_tournament = MagicMock(
        id=draft_tournament_id, tournament_status=TournamentStatus.DRAFT
    )
    live_tournament = MagicMock(
        id=live_tournament_id,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    def _find_tournament(tournament_id):
        return {
            draft_tournament_id: draft_tournament,
            live_tournament_id: live_tournament,
        }[tournament_id]

    with app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
            patch(f'{_V}.tournament_service') as mock_tournament_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_request_svc.get_visible_requests_for_user.return_value = [
                draft_request,
                live_request,
            ]
            mock_tournament_svc.find_tournament.side_effect = _find_tournament
            mock_tournament_svc.get_participant_counts_for_tournaments.return_value = {}

            raw_fn = views.my_requests.__wrapped__.__wrapped__
            result = raw_fn()

    assert result['draft_tournament_ids'] == {draft_tournament_id}
