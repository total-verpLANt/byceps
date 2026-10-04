"""Unit proofs of the registered, fully decorated security-fix routes."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from flask import Flask, g
import pytest

from byceps.services.authn.session.models import CurrentUser
from byceps.services.lan_tournament.blueprints.admin import views as admin_views
from byceps.services.lan_tournament.blueprints.site import authz
from byceps.services.lan_tournament.blueprints.site import views as site_views
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import TournamentID
from byceps.services.lan_tournament.models.tournament_match import (
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util import views as view_utils
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


ADMIN_PERMISSION = 'lan_tournament.administrate'
REOPEN_REFUSAL = (
    'A completed tournament can only be reopened by an administrator.'
)


def _translate(message, **kwargs):
    return message % kwargs if kwargs else message


@pytest.fixture
def route_environment(monkeypatch, make_user):
    """Keep decorators, lookups, parsing, redirects and flashes real; mock services."""
    tournament = SimpleNamespace(
        id=TournamentID(generate_uuid()),
        party_id='security-fix-party',
        tournament_status=TournamentStatus.ONGOING,
        game_format=GameFormat.FREE_FOR_ALL,
        has_playoffs=False,
    )
    match = SimpleNamespace(
        id=TournamentMatchID(generate_uuid()),
        tournament_id=tournament.id,
        confirmed_by=None,
        phase=1,
    )
    user = make_user()
    find_tournament = MagicMock(return_value=tournament)
    get_match = MagicMock(return_value=match)
    find_match = MagicMock(return_value=match)
    change_status = MagicMock(return_value=Ok((tournament, None)))
    submit_ffa = MagicMock(return_value=Ok(None))
    set_placements = MagicMock(return_value=Ok(None))
    confirm_ffa = MagicMock(return_value=Ok(None))
    is_orga = MagicMock()

    monkeypatch.setattr(
        site_views.tournament_service, 'find_tournament', find_tournament
    )
    monkeypatch.setattr(
        site_views.tournament_service, 'change_status', change_status
    )
    monkeypatch.setattr(
        site_views.tournament_match_service, 'get_match', get_match
    )
    monkeypatch.setattr(
        site_views.tournament_match_service, 'find_match', find_match
    )
    monkeypatch.setattr(
        site_views.tournament_match_service,
        'set_and_confirm_ffa_match',
        submit_ffa,
    )
    monkeypatch.setattr(
        site_views.tournament_match_service,
        'set_ffa_placements',
        set_placements,
    )
    monkeypatch.setattr(
        site_views.tournament_match_service, 'confirm_ffa_match', confirm_ffa
    )
    monkeypatch.setattr(
        authz.tournament_orga_service, 'is_orga_for_tournament', is_orga
    )
    monkeypatch.setattr(site_views, 'gettext', _translate)
    monkeypatch.setattr(admin_views, 'gettext', _translate)
    monkeypatch.setattr(view_utils, 'gettext', _translate)
    monkeypatch.setattr(
        'byceps.services.lan_tournament.lan_tournament_view_helpers.gettext',
        _translate,
    )

    def create_client(
        surface, *, permissions=frozenset(), assigned=True, logged_in=True
    ):
        current_user = (
            CurrentUser.create_authenticated(user, None, permissions)
            if logged_in
            else CurrentUser.create_anonymous(None)
        )
        is_orga.side_effect = lambda user_id, tournament_id: (
            assigned
            and user_id == user.id
            and str(tournament_id) == str(tournament.id)
        )
        app = Flask(__name__)
        app.config.update(
            TESTING=True, SECRET_KEY='security-fix-routes-test-only'
        )
        views = site_views if surface == 'site' else admin_views
        app.register_blueprint(views.blueprint, url_prefix='/lan-tournaments')
        app.add_url_rule(
            '/log-in',
            endpoint='authn_login.log_in_form',
            view_func=lambda: 'Log in.',
        )

        @app.before_request
        def bind_request_context():
            g.user = current_user
            g.party = SimpleNamespace(id='security-fix-party')
            g.app_mode = SimpleNamespace(is_admin=lambda: surface == 'admin')

        return app.test_client()

    return SimpleNamespace(
        create_client=create_client,
        tournament=tournament,
        match=match,
        user=user,
        find_tournament=find_tournament,
        get_match=get_match,
        find_match=find_match,
        change_status=change_status,
        submit_ffa=submit_ffa,
        set_placements=set_placements,
        confirm_ffa=confirm_ffa,
        is_orga=is_orga,
    )


def _flashes(client):
    with client.session_transaction() as session:
        return [
            (message['category'], message['text'])
            for _, message in session['_flashes']
        ]


@pytest.mark.parametrize(
    'global_admin', [False, True], ids=['scoped-orga', 'global-admin']
)
@pytest.mark.parametrize('action', ['start', 'resume', 'pause', 'complete'])
@pytest.mark.parametrize('confirmation', [None, '', '1'])
def test_scoped_orga_reopen_form_flag_is_ignored(
    route_environment, global_admin, action, confirmation
):
    env = route_environment
    client = env.create_client(
        'site',
        permissions=frozenset({ADMIN_PERMISSION})
        if global_admin
        else frozenset(),
    )
    env.change_status.return_value = Err(REOPEN_REFUSAL)
    data = {'allow_completed_reopen': '1'}
    if confirmation is not None:
        data['confirm_generated_layout'] = confirmation

    response = client.post(
        f'/lan-tournaments/orga/tournaments/{env.tournament.id}/{action}',
        data=data,
    )

    assert response.status_code == 302
    assert response.location == f'/lan-tournaments/{env.tournament.id}'
    statuses = {
        'start': TournamentStatus.ONGOING,
        'resume': TournamentStatus.ONGOING,
        'pause': TournamentStatus.PAUSED,
        'complete': TournamentStatus.COMPLETED,
    }
    env.change_status.assert_called_once_with(
        env.tournament.id,
        statuses[action],
        env.user.id,
        confirm_generated_layout=bool(confirmation),
        allow_completed_reopen=False,
    )
    if global_admin:
        env.is_orga.assert_not_called()
    else:
        env.is_orga.assert_called_once_with(env.user.id, str(env.tournament.id))
    assert _flashes(client) == [
        ('danger', f'Status change failed: {REOPEN_REFUSAL}')
    ]


def test_admin_reopen_requires_global_permission(route_environment):
    env = route_environment
    client = env.create_client(
        'admin', permissions=frozenset({'lan_tournament.view'})
    )

    response = client.post(
        f'/lan-tournaments/tournaments/{env.tournament.id}/reopen',
        data={'allow_completed_reopen': '1'},
    )

    assert response.status_code == 403
    env.find_tournament.assert_not_called()
    env.change_status.assert_not_called()


@pytest.mark.parametrize(
    ('action', 'status', 'expected_status', 'allow_reopen'),
    # fmt: off
    [
        ('reopen', TournamentStatus.COMPLETED, TournamentStatus.ONGOING, True),
        (
            'start',
            TournamentStatus.REGISTRATION_CLOSED,
            TournamentStatus.ONGOING,
            False,
        ),
        ('resume', TournamentStatus.PAUSED, TournamentStatus.ONGOING, False),
        (
            'open_registration',
            TournamentStatus.DRAFT,
            TournamentStatus.REGISTRATION_OPEN,
            False,
        ),
        (
            'close_registration',
            TournamentStatus.REGISTRATION_OPEN,
            TournamentStatus.REGISTRATION_CLOSED,
            False,
        ),
        ('pause', TournamentStatus.ONGOING, TournamentStatus.PAUSED, False),
        (
            'complete',
            TournamentStatus.ONGOING,
            TournamentStatus.COMPLETED,
            False,
        ),
        ('cancel', TournamentStatus.ONGOING, TournamentStatus.CANCELLED, False),
    ],
    # fmt: on
)
@pytest.mark.parametrize('confirmation', [None, '', '1'])
def test_admin_reopen_alone_passes_explicit_opt_in(
    route_environment,
    action,
    status,
    expected_status,
    allow_reopen,
    confirmation,
):
    env = route_environment
    env.tournament.tournament_status = status
    client = env.create_client(
        'admin', permissions=frozenset({ADMIN_PERMISSION})
    )
    data = {'allow_completed_reopen': '1' if not allow_reopen else '0'}
    if confirmation is not None:
        data['confirm_generated_layout'] = confirmation

    response = client.post(
        f'/lan-tournaments/tournaments/{env.tournament.id}/{action}', data=data
    )

    assert response.status_code == 302
    assert (
        response.location == f'/lan-tournaments/tournaments/{env.tournament.id}'
    )
    env.find_tournament.assert_called_once_with(env.tournament.id)
    env.change_status.assert_called_once_with(
        env.tournament.id,
        expected_status,
        env.user.id,
        confirm_generated_layout=bool(confirmation),
        allow_completed_reopen=allow_reopen,
    )
    status_title = expected_status.name.replace('_', ' ').title()
    assert _flashes(client) == [
        ('success', f'Tournament status has been changed to "{status_title}".')
    ]


def test_decorated_orga_ffa_submission_uses_atomic_operation(route_environment):
    env = route_environment
    client = env.create_client('site')
    contestant_a, contestant_b = str(generate_uuid()), str(generate_uuid())

    response = client.post(
        f'/lan-tournaments/orga/matches/{env.match.id}/submit_ffa_result',
        data={
            f'placement_{contestant_b}': '2',
            f'placement_{contestant_a}': '1',
        },
    )

    assert response.status_code == 302
    assert response.location == f'/lan-tournaments/matches/{env.match.id}'
    env.is_orga.assert_called_once_with(env.user.id, env.tournament.id)
    env.find_tournament.assert_called_once_with(env.match.tournament_id)
    env.submit_ffa.assert_called_once_with(
        env.match.id, {contestant_a: 1, contestant_b: 2}, env.user.id
    )
    env.set_placements.assert_not_called()
    env.confirm_ffa.assert_not_called()
    assert _flashes(client) == [('success', 'FFA match has been confirmed.')]


@pytest.mark.parametrize('action', ['status', 'ffa'])
@pytest.mark.parametrize(
    ('guard', 'expected_status'),
    # fmt: off
    [
        ('anonymous', 302),
        ('unassigned', 403),
        ('other-party', 404),
        ('missing-party', 404),
    ],
    # fmt: on
)
def test_decorated_orga_actions_preserve_login_scope_and_party_checks(
    route_environment, action, guard, expected_status
):
    env = route_environment
    client = env.create_client(
        'site', assigned=guard != 'unassigned', logged_in=guard != 'anonymous'
    )
    if guard == 'other-party':
        env.tournament.party_id = 'another-party'
    elif guard == 'missing-party':

        @client.application.before_request
        def remove_party():
            g.party = None

    path = (
        f'/lan-tournaments/orga/tournaments/{env.tournament.id}/resume'
        if action == 'status'
        else f'/lan-tournaments/orga/matches/{env.match.id}/submit_ffa_result'
    )
    response = client.post(path, data={f'placement_{generate_uuid()}': '1'})

    assert response.status_code == expected_status
    env.change_status.assert_not_called()
    env.submit_ffa.assert_not_called()
    env.set_placements.assert_not_called()
    env.confirm_ffa.assert_not_called()
    if guard == 'anonymous':
        assert response.location.startswith('/log-in?next=')
        env.is_orga.assert_not_called()
        env.get_match.assert_not_called()
        env.find_tournament.assert_not_called()
    elif guard == 'unassigned':
        env.is_orga.assert_called_once()
        env.find_tournament.assert_not_called()


@pytest.mark.parametrize(
    ('guard', 'error_message'),
    # fmt: off
    [
        ('status', 'Tournament is not in progress.'),
        ('format', 'Placements apply only to free-for-all matches.'),
        ('invalid-placement', 'Invalid placement value for contestant.'),
        ('empty', 'No placement data submitted.'),
    ],
    # fmt: on
)
def test_decorated_orga_ffa_refusals_never_call_atomic_operation(
    route_environment, guard, error_message
):
    env = route_environment
    client = env.create_client('site')
    data = {f'placement_{generate_uuid()}': '1'}
    if guard == 'status':
        env.tournament.tournament_status = TournamentStatus.PAUSED
    elif guard == 'format':
        env.tournament.game_format = GameFormat.ONE_V_ONE
    elif guard == 'invalid-placement':
        data = {f'placement_{generate_uuid()}': 'first'}
    else:
        data = {}

    response = client.post(
        f'/lan-tournaments/orga/matches/{env.match.id}/submit_ffa_result',
        data=data,
    )

    assert response.status_code == 302
    env.submit_ffa.assert_not_called()
    env.set_placements.assert_not_called()
    env.confirm_ffa.assert_not_called()
    assert _flashes(client) == [('danger', error_message)]


def test_decorated_orga_ffa_failure_has_only_generic_error_feedback(
    route_environment,
):
    env = route_environment
    client = env.create_client('site')
    error = 'Match is already confirmed.'
    env.submit_ffa.return_value = Err(error)

    response = client.post(
        f'/lan-tournaments/orga/matches/{env.match.id}/submit_ffa_result',
        data={f'placement_{generate_uuid()}': '1'},
    )

    assert response.status_code == 302
    env.submit_ffa.assert_called_once()
    env.set_placements.assert_not_called()
    env.confirm_ffa.assert_not_called()
    assert _flashes(client) == [
        ('danger', f'Error confirming FFA match: {error}')
    ]


def test_decorated_orga_completed_fast_refusal_skips_status_service(
    route_environment,
):
    env = route_environment
    env.tournament.tournament_status = TournamentStatus.COMPLETED
    client = env.create_client(
        'site', permissions=frozenset({ADMIN_PERMISSION})
    )

    response = client.post(
        f'/lan-tournaments/orga/tournaments/{env.tournament.id}/resume',
        data={'allow_completed_reopen': '1'},
    )

    assert response.status_code == 302
    env.change_status.assert_not_called()
    assert _flashes(client) == [('danger', REOPEN_REFUSAL)]


def test_decorated_admin_ffa_keeps_separate_set_and_confirm_apis(
    route_environment,
):
    env = route_environment
    client = env.create_client(
        'admin', permissions=frozenset({ADMIN_PERMISSION})
    )
    contestant = str(generate_uuid())

    set_response = client.post(
        f'/lan-tournaments/matches/{env.match.id}/set_ffa_placements',
        data={f'placement_{contestant}': '1'},
    )

    assert set_response.status_code == 302
    env.set_placements.assert_called_once_with(env.match.id, {contestant: 1})
    env.confirm_ffa.assert_not_called()
    env.submit_ffa.assert_not_called()

    confirm_response = client.post(
        f'/lan-tournaments/matches/{env.match.id}/confirm_ffa'
    )

    assert confirm_response.status_code == 302
    env.set_placements.assert_called_once()
    env.confirm_ffa.assert_called_once_with(env.match.id, env.user.id)
    env.submit_ffa.assert_not_called()
    assert _flashes(client) == [
        ('success', 'Placements have been set.'),
        ('success', 'FFA match has been confirmed.'),
    ]
