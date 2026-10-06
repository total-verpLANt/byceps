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
import dataclasses
from datetime import datetime, UTC
import inspect
from types import SimpleNamespace
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
    """POST propose without a party ticket flashes and re-renders the
    form in place -- a redirect would discard everything the user had
    typed."""
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
    mocks['redirect_to'].assert_not_called()
    mocks['propose_form'].assert_called_once()


def test_propose_without_ticket_keeps_entered_values(app):
    """The form re-rendered by the ticket gate still carries what the
    user typed -- proving the gate re-renders rather than redirects."""
    with _patched_propose_view(app, has_ticket=False) as mocks:
        from byceps.services.lan_tournament.blueprints.site import views

        raw_fn = views.propose.__wrapped__

        data = dict(_VALID_PROPOSE_FORM_DATA)
        data['name'] = 'Kept Name'

        with app.test_request_context('/', method='POST', data=data):
            raw_fn()

    mocks['propose_form'].assert_called_once()
    rendered_form = mocks['propose_form'].call_args.args[0]
    assert rendered_form.name.data == 'Kept Name'


def test_propose_form_passes_has_ticket_flag(app):
    """The create-mode GET context mirrors the ticket check, so the
    template can show the gate notice without a POST/redirect round
    trip."""
    from byceps.services.lan_tournament.blueprints.site import views

    with app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.ticket_service') as mock_ticket_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_ticket_svc.uses_any_ticket_for_party.return_value = False

            raw_fn = views.propose_form.__wrapped__.__wrapped__
            result = raw_fn()

    assert result['has_ticket'] is False
    mock_ticket_svc.uses_any_ticket_for_party.assert_called_once_with(
        PROPOSER_USER_ID, PARTY_ID
    )


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


def test_request_visibility_excludes_requests_from_site_index(app):
    """`index()` never touches request data -- invisibility is structural.

    An unaccepted request never becomes a tournament, so it cannot
    appear in `get_tournaments_for_party`'s results either; this test
    additionally proves the view never even imports/calls the request
    service, so there is no filter here to get wrong later.
    """
    from byceps.services.lan_tournament.blueprints.site import views

    assert 'tournament_request' not in inspect.getsource(views.index)

    with (
        app.test_request_context('/'),
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
            'overview_mode',
            'category_filter',
            'categories',
            'category_filter_args',
            'total_count',
            'tournaments',
            'tournament_groups',
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
            patch(f'{_V}.tournament_orga_service') as mock_orga_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_request_svc.get_visible_requests_for_user.return_value = [
                draft_request,
                live_request,
            ]
            mock_tournament_svc.find_tournament.side_effect = _find_tournament
            mock_tournament_svc.get_participant_counts_for_tournaments.return_value = {}
            mock_orga_svc.is_orga_for_tournament.return_value = False

            raw_fn = views.my_requests.__wrapped__.__wrapped__
            result = raw_fn()

    assert result['draft_tournament_ids'] == {draft_tournament_id}


def test_my_requests_passes_orga_tournament_ids(app):
    """AC5: `my_requests`'s context carries an `orga_tournament_ids` set
    -- the ids of every linked tournament the caller is a scoped orga
    of -- built from `tournament_orga_service.is_orga_for_tournament`,
    per tournament, never trusting a template-only check."""
    from byceps.services.lan_tournament.blueprints.site import views
    from byceps.services.lan_tournament.models.tournament import TournamentID
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )

    orga_tournament_id = TournamentID(generate_uuid())
    other_tournament_id = TournamentID(generate_uuid())

    orga_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=orga_tournament_id,
        number=1,
    )
    other_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=other_tournament_id,
        number=2,
    )

    orga_tournament = MagicMock(
        id=orga_tournament_id,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )
    other_tournament = MagicMock(
        id=other_tournament_id,
        tournament_status=TournamentStatus.REGISTRATION_OPEN,
    )

    def _find_tournament(tournament_id):
        return {
            orga_tournament_id: orga_tournament,
            other_tournament_id: other_tournament,
        }[tournament_id]

    def _is_orga_for_tournament(user_id, tournament_id):
        return tournament_id == orga_tournament_id

    with app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
            patch(f'{_V}.tournament_service') as mock_tournament_svc,
            patch(f'{_V}.tournament_orga_service') as mock_orga_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_request_svc.get_visible_requests_for_user.return_value = [
                orga_request,
                other_request,
            ]
            mock_tournament_svc.find_tournament.side_effect = _find_tournament
            mock_tournament_svc.get_participant_counts_for_tournaments.return_value = {}
            mock_orga_svc.is_orga_for_tournament.side_effect = (
                _is_orga_for_tournament
            )

            raw_fn = views.my_requests.__wrapped__.__wrapped__
            result = raw_fn()

            mock_orga_svc.is_orga_for_tournament.assert_any_call(
                PROPOSER_USER_ID, orga_tournament_id
            )
            mock_orga_svc.is_orga_for_tournament.assert_any_call(
                PROPOSER_USER_ID, other_tournament_id
            )

            assert result['orga_tournament_ids'] == {orga_tournament_id}


# ------------------------------------------------------------------ #
# 9. update_request_form: frozen context carries created_tournament
# (workspace-pv3b.19, G1)
# ------------------------------------------------------------------ #


def test_update_request_form_frozen_context_carries_created_tournament(app):
    """G1: the frozen context must pass `created_tournament` (looked up
    via `tournament_service.find_tournament`) -- the bote template
    needs it to tell a DRAFT (which the site `view()` 404s) from a
    tournament it can safely link.
    """
    from byceps.services.lan_tournament.blueprints.site import views
    from byceps.services.lan_tournament.models.tournament import TournamentID
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )

    tournament_id = TournamentID(generate_uuid())
    frozen_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
    )
    created_tournament = MagicMock(
        id=tournament_id, tournament_status=TournamentStatus.DRAFT
    )

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
                frozen_request
            ]
            mock_request_svc.get_request_history.return_value = []
            mock_tournament_svc.find_tournament.return_value = (
                created_tournament
            )

            raw_fn = views.update_request_form.__wrapped__.__wrapped__
            result = raw_fn(str(frozen_request.id))

    assert result['mode'] == 'frozen'
    assert result['created_tournament'] is created_tournament
    mock_tournament_svc.find_tournament.assert_called_once_with(tournament_id)


def test_update_request_form_frozen_context_no_tournament_when_not_created(
    app,
):
    """A frozen request that never reached `tournament_created` (e.g.
    rejected/withdrawn) must not look up a tournament at all --
    `created_tournament_id` is `None`, so `created_tournament` stays
    `None` and the template's `is defined and created_tournament`
    guard skips the link/draft-tag branch entirely."""
    from byceps.services.lan_tournament.blueprints.site import views

    rejected_request = _make_request(
        status=TournamentRequestStatus.rejected,
        rejection_reason='Not enough interest.',
    )

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
                rejected_request
            ]
            mock_request_svc.get_request_history.return_value = []

            raw_fn = views.update_request_form.__wrapped__.__wrapped__
            result = raw_fn(str(rejected_request.id))

    assert result['mode'] == 'frozen'
    assert result['created_tournament'] is None
    mock_tournament_svc.find_tournament.assert_not_called()


def _call_update_request_form(app, tournament_request, history):
    from byceps.services.lan_tournament.blueprints.site import views

    with app.test_request_context('/'):
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
            patch(f'{_V}.tournament_service'),
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_request_svc.get_visible_requests_for_user.return_value = [
                tournament_request
            ]
            mock_request_svc.get_request_history.return_value = history

            raw_fn = views.update_request_form.__wrapped__.__wrapped__
            return raw_fn(str(tournament_request.id))


def _log_entry(event_type, data=None):
    return SimpleNamespace(event_type=event_type, data=data or {})


def _edited(previous_values, **data):
    return _log_entry(
        'tournament-request-edited',
        {'changed_fields': list(previous_values), **data}
        | {'previous_values': previous_values},
    )


def test_update_request_form_frozen_context_carries_request_labels(app):
    """The frozen summary shows the labels passed by the view."""
    from byceps.services.lan_tournament.blueprints.site.forms import (
        game_format_label,
        request_mode_label,
    )

    accepted_request = dataclasses.replace(
        _make_request(status=TournamentRequestStatus.accepted),
        game_format=GameFormat.HIGHSCORE,
        elimination_mode=EliminationMode.NONE,
    )

    result = _call_update_request_form(app, accepted_request, [])

    assert result['mode'] == 'frozen'
    assert result['format_label'] == game_format_label(GameFormat.HIGHSCORE)
    assert result['request_mode_label'] == request_mode_label(
        EliminationMode.NONE
    )
    assert result['request_mode_label'] != result['format_label']
    assert 'latest_changes' not in result


def test_update_request_form_edit_context_carries_latest_changes(app):
    open_request = _make_request()
    history = [
        _log_entry('tournament-request-submitted'),
        _edited({'name': 'Older Cup'}),
        _edited({'participant_limit': 32, 'special_rules': None}),
    ]

    result = _call_update_request_form(app, open_request, history)

    assert result['mode'] == 'edit'
    assert result['latest_changes'] == {
        'participant_limit': '32',
        'special_rules': '\N{EM DASH}',
    }
    assert result['history'] == history


def test_update_request_form_edit_context_without_edits_has_no_changes(app):
    result = _call_update_request_form(
        app,
        _make_request(),
        [_log_entry('tournament-request-submitted')],
    )

    assert result['latest_changes'] == {}


def test_latest_request_changes_newest_edit_wins(app):
    from byceps.services.lan_tournament.blueprints.site import views

    history = [
        _log_entry('tournament-request-submitted'),
        _edited({'name': 'First'}),
        _edited({'game': 'Second'}),
        _log_entry('tournament-request-withdrawn'),
    ]

    with app.test_request_context('/'):
        assert views._latest_request_changes(history) == {'game': 'Second'}


@pytest.mark.parametrize(
    'history',
    [
        [],
        [_log_entry('tournament-request-submitted')],
        [_log_entry('tournament-request-edited', {'changed_fields': ['name']})],
        [_log_entry('tournament-request-edited', {'previous_values': None})],
        [_log_entry('tournament-request-edited', {'previous_values': []})],
        [
            _edited({'name': 'Old'}),
            _log_entry(
                'tournament-request-edited', {'changed_fields': ['name']}
            ),
        ],
    ],
)
def test_latest_request_changes_is_empty_without_previous_values(app, history):
    from byceps.services.lan_tournament.blueprints.site import views

    with app.test_request_context('/'):
        assert views._latest_request_changes(history) == {}


def test_latest_request_changes_converts_logged_values_to_display_values(
    app,
):
    """Logged previous values are shown the way the form shows them."""
    from byceps.services.lan_tournament.blueprints.site import views
    from byceps.services.lan_tournament.blueprints.site.forms import (
        game_format_label,
        request_mode_label,
    )

    berlin = Flask(__name__)
    berlin.config['LOCALE'] = 'en'
    berlin.config['BABEL_DEFAULT_LOCALE'] = 'en'
    berlin.config['BABEL_DEFAULT_TIMEZONE'] = 'Europe/Berlin'
    Babel(berlin)
    history = [
        _edited(
            {
                'preferred_start_time': '2026-10-03T12:00:00+00:00',
                'preferred_end_time': '2026-10-03T21:30:00',
                'game_format': 'FREE_FOR_ALL',
                'elimination_mode': 'DOUBLE_ELIMINATION',
                'team_size': 3,
                'participant_limit': 32,
                'name': 'Rollator-Rallye',
                'notes': None,
            }
        )
    ]

    with berlin.test_request_context('/'):
        changes = views._latest_request_changes(history)

    assert changes == {
        # 12:00 UTC is 14:00 in Berlin (CEST); a naive value counts as UTC.
        'preferred_start_time': '03.10. 14:00',
        'preferred_end_time': '03.10. 23:30',
        'game_format': game_format_label(GameFormat.FREE_FOR_ALL),
        'elimination_mode': request_mode_label(
            EliminationMode.DOUBLE_ELIMINATION
        ),
        'team_size': '3',
        'participant_limit': '32',
        'name': 'Rollator-Rallye',
        'notes': '\N{EM DASH}',
    }
    assert isinstance(changes['team_size'], str)


def test_latest_request_changes_keeps_unconvertible_values_as_logged(app):
    from byceps.services.lan_tournament.blueprints.site import views

    history = [
        _edited(
            {
                'preferred_start_time': 'not a date',
                'game_format': 'RETIRED_FORMAT',
                'elimination_mode': 'RETIRED_MODE',
            }
        )
    ]

    with app.test_request_context('/'):
        assert views._latest_request_changes(history) == {
            'preferred_start_time': 'not a date',
            'game_format': 'RETIRED_FORMAT',
            'elimination_mode': 'RETIRED_MODE',
        }


# ------------------------------------------------------------------ #
# 10. my_requests: "Submitted today" uses calendar days, not "<24h"
# (B5, workspace-pv3b.22)
# ------------------------------------------------------------------ #


def test_my_requests_waiting_days_uses_local_calendar_day_not_24h():
    """B5: a request submitted yesterday 23:00 local and viewed today
    01:00 local is 1 day old, not "today" -- even though under two
    hours separate the two clock times. The old `(now - created_at)
    .days` computation compared raw UTC instants; the fix compares
    calendar dates in the display timezone (`to_user_timezone`)."""
    from byceps.services.lan_tournament.blueprints.site import views

    # January: Berlin is CET (UTC+1), no DST ambiguity.
    tz_app = Flask(__name__)
    tz_app.config['TESTING'] = True
    tz_app.config['LOCALE'] = 'en'
    tz_app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    tz_app.config['BABEL_DEFAULT_TIMEZONE'] = 'Europe/Berlin'
    Babel(tz_app)

    created_at = datetime(2026, 1, 14, 22, 0)  # naive UTC: 23:00 CET, the 14th
    fixed_now = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)  # 01:00 CET, the 15th

    submitted_request = dataclasses.replace(
        _make_request(status=TournamentRequestStatus.submitted, number=1),
        created_at=created_at,
    )

    with tz_app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
            patch(f'{_V}.tournament_service') as mock_tournament_svc,
            patch(f'{_V}.datetime') as mock_datetime,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_request_svc.get_visible_requests_for_user.return_value = [
                submitted_request
            ]
            mock_tournament_svc.get_participant_counts_for_tournaments.return_value = {}
            mock_datetime.now.return_value = fixed_now

            raw_fn = views.my_requests.__wrapped__.__wrapped__
            result = raw_fn()

    assert result['waiting_days_by_request_id'][submitted_request.id] == 1


_REJECT_REASON = (
    'Samstagnachmittag ist die Bühne mit dem Hauptturnier belegt. '
    'Reich es gern für Sonntagvormittag neu ein.'
)


def _call_my_requests(
    app,
    requests,
    *,
    tournaments=None,
    participant_counts=None,
    team_counts=None,
):
    """Run the unwrapped `my_requests` view against fake services."""
    from byceps.services.lan_tournament.blueprints.site import views

    tournaments = tournaments or {}

    with app.app_context():
        with (
            patch(f'{_V}.g') as mock_g,
            patch(f'{_V}._get_current_party_or_404') as mock_get_party,
            patch(f'{_V}.tournament_request_service') as mock_request_svc,
            patch(f'{_V}.tournament_service') as mock_tournament_svc,
            patch(f'{_V}.tournament_team_service') as mock_team_svc,
            patch(f'{_V}.tournament_orga_service') as mock_orga_svc,
        ):
            mock_g.user = _make_user(PROPOSER_USER_ID)
            mock_get_party.return_value = _make_party()
            mock_request_svc.get_visible_requests_for_user.return_value = (
                requests
            )
            mock_tournament_svc.find_tournament.side_effect = tournaments.get
            mock_tournament_svc.get_participant_counts_for_tournaments.return_value = (
                participant_counts or {}
            )
            mock_team_svc.get_team_counts_for_tournaments.return_value = (
                team_counts or {}
            )
            mock_orga_svc.is_orga_for_tournament.return_value = False

            raw_fn = views.my_requests.__wrapped__.__wrapped__
            return raw_fn(), mock_team_svc


def test_my_requests_orders_submitted_before_accepted(app):
    """The "In progress" cards list submitted requests first."""
    accepted_1 = _make_request(
        status=TournamentRequestStatus.accepted, number=1
    )
    submitted_3 = _make_request(
        status=TournamentRequestStatus.submitted, number=3
    )
    accepted_4 = _make_request(
        status=TournamentRequestStatus.accepted, number=4
    )
    submitted_2 = _make_request(
        status=TournamentRequestStatus.submitted, number=2
    )
    rejected_5 = _make_request(
        status=TournamentRequestStatus.rejected,
        number=5,
        rejection_reason='No.',
    )

    result, _ = _call_my_requests(
        app, [accepted_1, submitted_3, rejected_5, accepted_4, submitted_2]
    )

    assert [r.number for r in result['open_requests']] == [2, 3, 1, 4]
    assert [r.status for r in result['open_requests']] == [
        TournamentRequestStatus.submitted,
        TournamentRequestStatus.submitted,
        TournamentRequestStatus.accepted,
        TournamentRequestStatus.accepted,
    ]
    assert result['archived_requests'] == [rejected_5]


def test_my_requests_reason_lead_splits_first_sentence(app):
    """A rejected reason is cut after its first sentence."""
    two_sentences = _make_request(
        status=TournamentRequestStatus.rejected,
        number=1,
        rejection_reason=_REJECT_REASON,
    )
    one_sentence = _make_request(
        status=TournamentRequestStatus.rejected,
        number=2,
        rejection_reason='  Das Programm ist voll.  ',
    )
    question_first = _make_request(
        status=TournamentRequestStatus.rejected,
        number=3,
        rejection_reason='Warum gerade Samstag? Sonntag geht auch!',
    )
    decimal_point = _make_request(
        status=TournamentRequestStatus.rejected,
        number=4,
        rejection_reason='Version 3.5 ist zu alt.',
    )

    result, _ = _call_my_requests(
        app, [two_sentences, one_sentence, question_first, decimal_point]
    )

    leads = result['reason_leads_by_request_id']
    assert leads[two_sentences.id] == (
        'Samstagnachmittag ist die Bühne mit dem Hauptturnier belegt.',
        True,
    )
    assert leads[one_sentence.id] == ('Das Programm ist voll.', False)
    assert leads[question_first.id] == ('Warum gerade Samstag?', True)
    assert leads[decimal_point.id] == ('Version 3.5 ist zu alt.', False)


# fmt: off
@pytest.mark.parametrize(
    ('reason', 'expected'),
    [
        ('Leider ist am 3. Oktober alles belegt. Mehr Text.',
         ('Leider ist am 3. Oktober alles belegt.', True)),
        ('Der 2. Platz zählt nicht. Mehr Text.',
         ('Der 2. Platz zählt nicht.', True)),
        ('Nicht genug Plätze, z. B. am Freitag. Mehr Text.',
         ('Nicht genug Plätze, z. B. am Freitag.', True)),
        ('Es gibt ca. 40 Anmeldungen zu wenig. Mehr Text.',
         ('Es gibt ca. 40 Anmeldungen zu wenig.', True)),
        ('Siehe Nr. 5 für Details. Mehr Text.',
         ('Siehe Nr. 5 für Details.', True)),
        ('Fr bzw. Sa ist voll, Fr usw. auch. Mehr Text.',
         ('Fr bzw. Sa ist voll, Fr usw. auch.', True)),
        ('Das gilt u. a. für Fr, evtl. auch Sa, ggf. So. Mehr Text.',
         ('Das gilt u. a. für Fr, evtl. auch Sa, ggf. So.', True)),
        ('Leider sind max. 16 Teams möglich. Mehr Text.',
         ('Leider sind max. 16 Teams möglich.', True)),
        ('Nicht am Freitag, z.B. Freitag früh. Mehr Text.',
         ('Nicht am Freitag, z.B. Freitag früh.', True)),
        ('Alles belegt, d. h. wir sind voll. Mehr Text.',
         ('Alles belegt, d. h. wir sind voll.', True)),
        ('Zu teuer, inkl. Getränke und min. 2 Tische. Mehr Text.',
         ('Zu teuer, inkl. Getränke und min. 2 Tische.', True)),
        ('Leider belegt. 16 Teams sind schon da. Mehr Text.',
         ('Leider belegt. 16 Teams sind schon da.', True)),
        ('Nein. Mehr Text.', ('Nein.', True)),
        ('Am 3. Oktober ist alles belegt.',
         ('Am 3. Oktober ist alles belegt.', False)),
    ],
)
# fmt: on
def test_split_reason_lead_ignores_ordinals_and_abbreviations(
    reason, expected
):
    from byceps.services.lan_tournament.blueprints.site import views

    assert views._split_reason_lead(reason) == expected


def test_my_requests_reason_leads_cover_only_rejected_requests_with_a_reason(
    app,
):
    """Withdrawn, open and reasonless rejected requests have no lead."""
    rejected = _make_request(
        status=TournamentRequestStatus.rejected,
        number=1,
        rejection_reason='Belegt.',
    )
    rejected_blank = _make_request(
        status=TournamentRequestStatus.rejected,
        number=2,
        rejection_reason='   ',
    )
    rejected_none = _make_request(
        status=TournamentRequestStatus.rejected, number=3
    )
    withdrawn = _make_request(
        status=TournamentRequestStatus.withdrawn,
        number=4,
        rejection_reason='Belegt.',
    )
    submitted = _make_request(
        status=TournamentRequestStatus.submitted, number=5
    )

    result, _ = _call_my_requests(
        app, [rejected, rejected_blank, rejected_none, withdrawn, submitted]
    )

    assert result['reason_leads_by_request_id'] == {
        rejected.id: ('Belegt.', False)
    }


def test_my_requests_passes_team_counts_for_team_tournaments(app):
    """`team_counts` is fetched for the TEAM tournaments only."""
    from byceps.services.lan_tournament.models.contestant_type import (
        ContestantType,
    )
    from byceps.services.lan_tournament.models.tournament import TournamentID
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )

    team_tournament_id = TournamentID(generate_uuid())
    solo_tournament_id = TournamentID(generate_uuid())

    team_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=team_tournament_id,
        number=1,
    )
    solo_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=solo_tournament_id,
        number=2,
    )
    tournaments = {
        team_tournament_id: MagicMock(
            id=team_tournament_id,
            contestant_type=ContestantType.TEAM,
            max_players=None,
            max_teams=8,
            tournament_status=TournamentStatus.REGISTRATION_OPEN,
        ),
        solo_tournament_id: MagicMock(
            id=solo_tournament_id,
            contestant_type=ContestantType.SOLO,
            max_players=16,
            max_teams=8,
            tournament_status=TournamentStatus.REGISTRATION_OPEN,
        ),
    }

    result, mock_team_svc = _call_my_requests(
        app,
        [team_request, solo_request],
        tournaments=tournaments,
        participant_counts={solo_tournament_id: 5},
        team_counts={team_tournament_id: 3},
    )

    mock_team_svc.get_team_counts_for_tournaments.assert_called_once_with(
        [team_tournament_id]
    )
    assert result['signup_counts'] == {
        team_tournament_id: (3, 8),
        solo_tournament_id: (5, 16),
    }
    assert set(result['tournaments_by_request_id'].values()) == set(
        tournaments.values()
    )


def test_my_requests_team_tournament_without_max_teams_has_no_limit(app):
    from byceps.services.lan_tournament.models.contestant_type import (
        ContestantType,
    )
    from byceps.services.lan_tournament.models.tournament import TournamentID
    from byceps.services.lan_tournament.models.tournament_status import (
        TournamentStatus,
    )

    tournament_id = TournamentID(generate_uuid())
    tournament_request = _make_request(
        status=TournamentRequestStatus.tournament_created,
        created_tournament_id=tournament_id,
        number=1,
    )
    tournaments = {
        tournament_id: MagicMock(
            id=tournament_id,
            contestant_type=ContestantType.TEAM,
            max_players=32,
            max_teams=None,
            tournament_status=TournamentStatus.REGISTRATION_OPEN,
        )
    }

    result, _ = _call_my_requests(
        app,
        [tournament_request],
        tournaments=tournaments,
        participant_counts={tournament_id: 20},
        team_counts={tournament_id: 3},
    )

    assert result['signup_counts'] == {tournament_id: (3, None)}


def test_my_requests_without_linked_tournaments_asks_for_no_team_counts(app):
    result, mock_team_svc = _call_my_requests(
        app, [_make_request(status=TournamentRequestStatus.submitted)]
    )

    mock_team_svc.get_team_counts_for_tournaments.assert_called_once_with([])
    assert result['signup_counts'] == {}
