"""Real detail bodies and canonical projections; repository/authority boundaries mocked."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import g
from flask_babel import Babel
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import pytest

from byceps.services.lan_tournament.blueprints.admin import views as admin
from byceps.services.lan_tournament.blueprints.site import views as site
from byceps.services.lan_tournament.models.tournament_match import MatchSide
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.services.lan_tournament.models.game_format import GameFormat

from tests.unit.services.lan_tournament.test_match_readiness_rendering import (
    render_readiness,  # noqa: F401 -- shared real native GET/render fixture
)


DETAIL = 'admin/lan_tournament/view_match.html'
ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def detail(render_readiness, monkeypatch):  # noqa: F811 -- imported pytest fixture
    def read(*, role='global_orga', **state):
        rendered = render_readiness(role=role, **state)
        Babel(rendered.app)
        base = rendered.context
        match, tournament = base['match'], base['tournament']
        actor_id = uuid4()
        monkeypatch.setattr(admin, '_get_match_or_404', lambda _: match)
        monkeypatch.setattr(
            site.tournament_match_service, 'get_match', lambda _: match
        )
        monkeypatch.setattr(
            admin, '_get_tournament_or_404', lambda _: tournament
        )
        monkeypatch.setattr(
            admin.party_service,
            'get_party',
            lambda _: SimpleNamespace(id=tournament.party_id, title='Party'),
        )
        monkeypatch.setattr(
            admin,
            'build_contestant_name_lookups',
            lambda *a: (base['teams_by_id'], base['participants_by_id']),
        )
        monkeypatch.setattr(admin, 'build_hover_lookups', lambda *a: ({}, {}))
        monkeypatch.setattr(
            admin.user_service, 'get_users_indexed_by_id', lambda _: {}
        )
        monkeypatch.setattr(
            admin.tournament_match_service,
            'ffa_round_already_advanced',
            lambda *a: False,
        )
        # Only the outer admin shell is minimal; owned detail and imported macros
        # are real, autoescaped and StrictUndefined. This is not browser evidence.
        env = rendered.env.overlay()
        env.loader = ChoiceLoader(
            [
                DictLoader(
                    {
                        'layout/admin/lan_tournament.html': '{% block head %}{% endblock %}{% block before_body %}{% endblock %}{% block body %}{% endblock %}{% block scripts %}{% endblock %}'
                    }
                ),
                FileSystemLoader(
                    [
                        ROOT
                        / 'byceps/services/lan_tournament/blueprints/admin/templates',
                        *sorted(
                            (ROOT / 'byceps/services').glob(
                                '*/blueprints/admin/templates'
                            )
                        ),
                    ]
                ),
                rendered.env.loader,
            ]
        )
        env.globals['url_for'] = lambda endpoint, **kw: '/endpoint/' + endpoint
        with rendered.app.test_request_context(
            '/lan-tournaments/matches/' + str(match.id)
        ):
            g.user = SimpleNamespace(
                id=actor_id,
                authenticated=role != 'anonymous',
                has_permission=lambda _: False,
            )
            g.party = SimpleNamespace(id=tournament.party_id)
            admin_context = admin.view_match.__wrapped__.__wrapped__(
                str(match.id)
            )
            site_context = site.view_match.__wrapped__(str(match.id))
            html = env.get_template(DETAIL).render(**admin_context)
        return SimpleNamespace(
            html=html,
            context=admin_context,
            site=site_context,
            match=match,
            app=rendered.app,
            env=env,
            actor_id=actor_id,
        )

    return read


# fmt: off
@pytest.mark.parametrize('role', ['global_orga', 'scoped_orga', 'member_orga'])
# fmt: on
def test_admin_match_view_renders_no_history(detail, role):
    result = detail(role=role, ready=tuple(MatchSide))
    for hook in ('readiness-history', 'Readiness history', 'Last revocation'):
        assert hook not in result.html
    assert 'No retained readiness history' not in result.html
    for key in ('readiness_history', 'can_view_readiness_history', 'readiness_history_users_by_id'):
        assert key not in result.context
    assert 'data-readiness-status="both_ready"' in result.html


# fmt: off
@pytest.mark.parametrize('role, backend_view, status', [
    ('scoped_orga', False, 403),
    ('global_orga', False, 403),
    ('outsider', True, 200),
    ('scoped_orga', True, 200),
    ('global_orga', True, 200),
])
# fmt: on
def test_backend_permission_gate_is_not_broadened(detail, role, backend_view, status):
    result = detail(role=role)
    result.app.register_blueprint(admin.blueprint, url_prefix='/admin/lan-tournaments')
    result.app.jinja_env = result.env

    @result.app.before_request
    def user_context():
        g.user = SimpleNamespace(id=result.actor_id, authenticated=True,
                                 has_permission=lambda permission: backend_view and permission == 'lan_tournament.view')

    response = result.app.test_client().get('/admin/lan-tournaments/matches/' + str(result.match.id))
    assert response.status_code == status
    assert 'readiness-history' not in response.text


# fmt: off
@pytest.mark.parametrize('state, label', [
    ({}, 'Not ready'),
    ({'assigned': 0}, 'Waiting for opponent'),
    ({'assigned': 1}, 'Waiting for opponent'),
    ({'ready': (MatchSide.A,)}, 'Partially ready'),
    ({'ready': tuple(MatchSide)}, 'Both ready'),
    ({'ready': tuple(MatchSide), 'status': TournamentStatus.PAUSED}, 'Both ready'),
    ({'ready': tuple(MatchSide), 'status': TournamentStatus.COMPLETED}, 'Tournament completed'),
    ({'ready': tuple(MatchSide), 'status': TournamentStatus.CANCELLED}, 'Tournament cancelled'),
    ({'ready': tuple(MatchSide), 'confirmed': True}, 'Confirmed'),
    ({'assigned': 1, 'confirmed': True}, 'DEFWIN'),
    ({'format_': GameFormat.FREE_FOR_ALL}, 'Readiness is not available'),
    ({'format_': GameFormat.FREE_FOR_ALL, 'confirmed': True}, 'Confirmed'),
    ({'reverse': True, 'ready': (MatchSide.A,)}, 'Partially ready'),
    ({'stale': True, 'ready': tuple(MatchSide)}, 'Not ready'),
])
# fmt: on
def test_detail_parity_uses_canonical_projection(detail, state, label):
    result = detail(**state)
    assert result.context['readiness'] == result.site['readiness']
    projection = result.context['readiness']
    assert f'data-readiness-status="{projection.display_status}"' in result.html
    assert label in result.html
    for timestamp in (projection.original_occupied_since, projection.pairing_started_at):
        if timestamp:
            assert timestamp.isoformat() in result.html
    if not projection.supports_readiness or not projection.pairing_valid:
        assert 'data-ready-at=' not in result.html
    else:
        for timestamp in (projection.ready_at_a, projection.ready_at_b):
            if timestamp:
                assert timestamp.isoformat() in result.html
    assert 'readiness-claim-form' not in result.html
    assert 'readiness-revoke-form' not in result.html
