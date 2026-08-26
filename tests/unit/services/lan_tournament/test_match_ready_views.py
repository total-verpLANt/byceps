"""Native HTTP caller, bounded fields, and read-only readiness context."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from flask import Flask, g
from werkzeug.datastructures import MultiDict

from byceps.services.lan_tournament.blueprints.readiness_csrf import (
    get_readiness_csrf_token,
)
from byceps.services.lan_tournament.blueprints.readiness_forms import (
    MatchReadyClaimForm,
    parse_readiness_revision,
)
from byceps.services.lan_tournament.blueprints.site import views
from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    ContestantIdentity,
    MatchPairing,
    MatchReadiness,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.readiness_change import (
    ReadinessChange,
)
from byceps.services.lan_tournament.models.tournament_match import (
    MatchSide,
    MatchUserRole,
    TournamentMatch,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Err, Ok


_V = 'byceps.services.lan_tournament.blueprints.site.views'
MATCH_ID, TOURNAMENT_ID, PARTY_ID, USER_ID = (uuid4() for _ in range(4))
NOW = datetime(2026, 10, 5, 12)
MATCH = TournamentMatch(
    id=MATCH_ID,
    tournament_id=TOURNAMENT_ID,
    group_order=None,
    match_order=1,
    round=1,
    next_match_id=None,
    confirmed_by=None,
    created_at=NOW,
    pairing_generation=3,
    readiness_revision=7,
    pairing_id=uuid4(),
)
PAIR = MatchPairing(
    id=MATCH.pairing_id,
    match_id=MATCH_ID,
    tournament_id=TOURNAMENT_ID,
    generation=3,
    side_a=ContestantIdentity(kind='participant', id=uuid4()),
    side_b=ContestantIdentity(kind='participant', id=uuid4()),
    started_at=NOW,
)
CONTESTANTS = [
    TournamentMatchToContestant(
        id=uuid4(),
        tournament_match_id=MATCH_ID,
        team_id=None,
        participant_id=identity.id,
        score=None,
        created_at=NOW,
    )
    for identity in (PAIR.side_a, PAIR.side_b)
]
CHANGE = ReadinessChange(
    match=replace(MATCH, readiness_revision=8, ready_at_a=NOW),
    readiness=MatchReadiness(
        status=ReadinessDisplayStatus.PARTIALLY_READY,
        ready_sides=(MatchSide.A,),
    ),
    actor_role='player',
)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(TESTING=True, LOCALE='en', SECRET_KEY='unit-test-only')
    app.register_blueprint(views.blueprint, url_prefix='/lan-tournaments')

    @app.before_request
    def current_user():
        g.user = SimpleNamespace(
            id=USER_ID,
            authenticated=True,
            has_permission=lambda permission: False,
        )
        g.party = SimpleNamespace(id=PARTY_ID)

    return app


@contextmanager
def collaborators(*, status=TournamentStatus.ONGOING):
    tournament = SimpleNamespace(
        id=TOURNAMENT_ID,
        party_id=PARTY_ID,
        tournament_status=status,
        game_format=GameFormat.ONE_V_ONE,
        has_playoffs=False,
    )
    with (
        patch(f'{_V}.tournament_match_service') as matches,
        patch(f'{_V}.tournament_readiness_service') as service,
        patch(f'{_V}.tournament_repository') as repo,
        patch(
            f'{_V}.tournament_service.find_tournament', return_value=tournament
        ),
        patch(f'{_V}.gettext', side_effect=lambda message, **kw: message),
        patch(f'{_V}.flash_error') as error,
        patch(f'{_V}.flash_success') as success,
    ):
        matches.get_match.return_value = MATCH
        matches.get_contestants_for_match.return_value = CONTESTANTS
        service.claim_ready_flush.return_value = Ok(CHANGE)
        service.revoke_ready_flush.return_value = Ok(CHANGE)
        service.dispatch_readiness_effects.return_value = Ok(None)
        yield SimpleNamespace(
            matches=matches,
            service=service,
            repo=repo,
            error=error,
            success=success,
            tournament=tournament,
        )


def native_post(app, client, action='claim', *, data=None, user_id=USER_ID):
    with app.test_request_context('/'):
        token = get_readiness_csrf_token(user_id)
        from flask import session

        state = dict(session)
    with client.session_transaction() as session:
        session.update(state)
    fields = dict(
        side='a',
        expected_pairing_generation='3',
        expected_readiness_revision='7',
        csrf_token=token,
    )
    fields.update(data or {})
    return client.post(
        f'/lan-tournaments/matches/{MATCH_ID}/ready/{action}', data=fields
    )


@pytest.mark.parametrize('action', ['claim', 'revoke'])
def test_native_post_owns_commit_and_redirect(app, action):
    with collaborators() as c:
        order = []
        operation = getattr(c.service, f'{action}_ready_flush')
        operation.side_effect = lambda *args, **kwargs: (
            order.append('flush'),
            Ok(CHANGE),
        )[1]
        c.repo.commit_session.side_effect = lambda: order.append('commit')
        c.service.dispatch_readiness_effects.side_effect = lambda change: (
            order.append('dispatch'),
            Ok(None),
        )[1]
        response = native_post(app, app.test_client(), action)
    assert response.status_code == 302
    assert response.location.endswith(f'/lan-tournaments/matches/{MATCH_ID}')
    assert order == ['flush', 'commit', 'dispatch']
    assert operation.call_args.args == (MATCH_ID, MatchSide.A, USER_ID)
    assert operation.call_args.kwargs == dict(
        expected_pairing_generation=3, expected_readiness_revision=7
    )
    c.repo.commit_session.assert_called_once_with()
    c.repo.rollback_session.assert_not_called()
    c.service.dispatch_readiness_effects.assert_called_once_with(CHANGE)


@pytest.mark.parametrize(
    'error,code', [('readiness_conflict', 302), ('readiness_forbidden', 403)]
)
@pytest.mark.parametrize('action', ['claim', 'revoke'])
def test_operation_error_rolls_back_without_effects(app, error, code, action):
    with collaborators() as c:
        getattr(c.service, f'{action}_ready_flush').return_value = Err(error)
        response = native_post(app, app.test_client(), action)
    assert response.status_code == code
    c.repo.rollback_session.assert_called_once_with()
    c.repo.commit_session.assert_not_called()
    c.service.dispatch_readiness_effects.assert_not_called()


@pytest.mark.parametrize('phase', ['operation', 'commit'])
def test_precommit_exception_rolls_back(app, phase):
    with collaborators() as c:
        target = (
            c.service.claim_ready_flush
            if phase == 'operation'
            else c.repo.commit_session
        )
        target.side_effect = RuntimeError('failure')
        with pytest.raises(RuntimeError, match='failure'):
            native_post(app, app.test_client())
    c.repo.rollback_session.assert_called_once_with()
    c.service.dispatch_readiness_effects.assert_not_called()


def test_postcommit_failure_does_not_claim_rollback(app):
    with collaborators() as c:
        c.service.dispatch_readiness_effects.return_value = Err(
            'readiness_dispatch_failed'
        )
        response = native_post(app, app.test_client())
    assert response.status_code == 302
    c.repo.commit_session.assert_called_once_with()
    c.repo.rollback_session.assert_not_called()
    c.error.assert_called_once_with('readiness_dispatch_failed')


def test_postcommit_exception_never_rolls_back_committed_facts(app):
    with collaborators() as c:
        c.service.dispatch_readiness_effects.side_effect = RuntimeError(
            'dispatch'
        )
        with pytest.raises(RuntimeError, match='dispatch'):
            native_post(app, app.test_client())
    c.repo.commit_session.assert_called_once_with()
    c.repo.rollback_session.assert_not_called()


# fmt: off
@pytest.mark.parametrize('field,value', [
    ('side', 'x'), ('side', ''), ('side', 'aa'),
    ('expected_pairing_generation', ''), ('expected_readiness_revision', ''),
    ('expected_pairing_generation', '-1'), ('expected_readiness_revision', 'True'),
    ('expected_pairing_generation', '1.0'), ('expected_readiness_revision', ' 1'),
    ('expected_pairing_generation', '9' * 20),
    ('expected_readiness_revision', '9223372036854775808'),
    ('expected_readiness_revision', '١'),
])
# fmt: on
@pytest.mark.parametrize('action', ['claim', 'revoke'])
def test_bounded_fields_checked_before_queries(app, field, value, action):
    with collaborators() as c:
        response = native_post(app, app.test_client(), action, data={field: value})
    assert response.status_code == 302
    c.matches.get_match.assert_not_called()
    c.service.claim_ready_flush.assert_not_called()
    c.service.revoke_ready_flush.assert_not_called()
    c.repo.commit_session.assert_not_called()


@pytest.mark.parametrize('extra', [{}, {'reason': 'legacy client'}])
def test_revoke_post_without_reason_succeeds(app, extra):
    with collaborators() as c:
        response = native_post(app, app.test_client(), 'revoke', data=extra)
    assert response.status_code == 302
    assert response.location.endswith(f'/lan-tournaments/matches/{MATCH_ID}')
    c.service.revoke_ready_flush.assert_called_once_with(
        MATCH_ID, MatchSide.A, USER_ID,
        expected_pairing_generation=3, expected_readiness_revision=7,
    )
    c.repo.commit_session.assert_called_once_with()
    c.repo.rollback_session.assert_not_called()
    c.error.assert_not_called()
    c.success.assert_called_once_with('Readiness revoked.')


@pytest.mark.parametrize('token', ['', 'wrong', 'x' * 43, 'x' * 100_000, 'é' * 43])
def test_invalid_token_maps403_before_queries(app, token):
    with collaborators() as c:
        response = native_post(app, app.test_client(), data={'csrf_token': token})
    assert response.status_code == 403
    c.matches.get_match.assert_not_called()
    c.service.claim_ready_flush.assert_not_called()


def test_foreign_user_token_maps403(app):
    with collaborators() as c:
        response = native_post(app, app.test_client(), user_id=uuid4())
    assert response.status_code == 403
    c.matches.get_match.assert_not_called()


def test_missing_session_token_maps403(app):
    with collaborators() as c:
        response = app.test_client().post(f'/lan-tournaments/matches/{MATCH_ID}/ready/claim',
                                         data={'csrf_token': 'x' * 43})
    assert response.status_code == 403
    c.matches.get_match.assert_not_called()


@pytest.mark.parametrize('field', ['side', 'expected_pairing_generation', 'expected_readiness_revision', 'csrf_token'])
def test_duplicate_security_fields_rejected_before_queries(app, field):
    client = app.test_client()
    with app.test_request_context('/'):
        token = get_readiness_csrf_token(USER_ID)
        from flask import session
        state = dict(session)
    with client.session_transaction() as session:
        session.update(state)
    data = MultiDict(dict(side='a', expected_pairing_generation='3',
                          expected_readiness_revision='7', csrf_token=token))
    data.add(field, data[field])
    with collaborators() as c:
        response = client.post(f'/lan-tournaments/matches/{MATCH_ID}/ready/revoke', data=data)
    assert response.status_code == (403 if field == 'csrf_token' else 302)
    c.matches.get_match.assert_not_called()
    c.service.revoke_ready_flush.assert_not_called()


def test_untrusted_orga_and_boolean_fields_are_not_service_arguments(app):
    with collaborators() as c:
        response = native_post(app, app.test_client(), data={'is_orga': 'true', 'expected_ready': 'ready'})
    assert response.status_code == 302
    assert set(c.service.claim_ready_flush.call_args.kwargs) == {
        'expected_pairing_generation', 'expected_readiness_revision'}


@pytest.mark.parametrize('status,code', [(TournamentStatus.PAUSED, 302), (TournamentStatus.DRAFT, 404)])
def test_lifecycle_preserved(app, status, code):
    with collaborators(status=status) as c:
        response = native_post(app, app.test_client())
    assert response.status_code == code
    c.service.claim_ready_flush.assert_not_called()
    c.repo.commit_session.assert_not_called()


@pytest.mark.parametrize('method', ['post', 'get'])
def test_cross_party_subject_hidden(app, method):
    with collaborators() as c:
        c.tournament.party_id = uuid4()
        response = (
            native_post(app, app.test_client()) if method == 'post'
            else app.test_client().get(f'/lan-tournaments/matches/{MATCH_ID}')
        )
    assert response.status_code == 404
    c.service.claim_ready_flush.assert_not_called()


@pytest.mark.parametrize('value', [True, False, -1, 9223372036854775808, None, '0' * 20])
def test_revision_parser_rejects_non_bigint(value):
    with pytest.raises(ValueError):
        parse_readiness_revision(value)


@pytest.mark.parametrize('value', [0, 7, 9223372036854775807, '0', '9223372036854775807'])
def test_revision_parser_accepts_boundaries(value):
    assert parse_readiness_revision(value) == int(value)


def test_required_revisions(app):
    with app.test_request_context('/'):
        assert not MatchReadyClaimForm(MultiDict({'side': 'a', 'csrf_token': 'x' * 43})).validate()


def test_native_get_context_performs_no_database_writes(app):
    with collaborators() as c:
        c.repo.get_match_pairing.return_value = PAIR
        c.matches.get_comments_from_match.return_value = []
        c.matches.get_user_match_role.return_value = MatchUserRole(
            contestant=None, is_loser=False, can_confirm=False, can_submit=False,
        )
        with (
            patch('byceps.services.lan_tournament.lan_tournament_view_helpers.tournament_repository.get_match_pairings_for_matches', return_value={MATCH_ID: PAIR}),
            patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
            patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
            patch(f'{_V}.user_service'),
            patch(f'{_V}.may_administrate_tournament', return_value=False),
            patch(f'{_V}.get_permissions_for_user', return_value=set()),
            patch(f'{_V}.tournament_orga_service.is_orga_for_tournament', return_value=False),
            patch(f'{_V}.tournament_readiness_authorization_service.get_user_readiness_sides', return_value=Ok(frozenset())),
            patch('byceps.util.framework.templating.render_template', return_value='read-only context') as render,
        ):
            response = app.test_client().get(f'/lan-tournaments/matches/{MATCH_ID}')
    assert response.status_code == 200
    context = render.call_args.kwargs
    assert context['readiness'].pairing_id == PAIR.id
    c.repo.commit_session.assert_not_called()
    c.repo.flush_session.assert_not_called()
    c.service.claim_ready_flush.assert_not_called()
    c.service.revoke_ready_flush.assert_not_called()


@pytest.mark.parametrize('kind', ['participant', 'team'])
@pytest.mark.parametrize('case', [
    'normal', 'reversed', 'paused', 'confirmed', 'missing', 'stale_generation',
    'foreign_pointer', 'foreign_match', 'foreign_tournament', 'ended',
    'wrong_identity', 'incomplete', 'duplicate_identity', 'foreign_membership',
    'ambiguous_identity', 'unsupported', 'changed_after_projection',
])
def test_readiness_side_map_uses_verified_logical_pair_without_score_reordering(app, kind, case):
    identities = [ContestantIdentity(kind=kind, id=uuid4()) for _ in range(2)]
    pair = replace(PAIR, side_a=identities[0], side_b=identities[1])
    # Row UUID/time order deliberately disagrees with logical A/B identity.
    logical_rows = [
        replace(
            CONTESTANTS[index], id=uuid4(),
            participant_id=identity.id if kind == 'participant' else None,
            team_id=identity.id if kind == 'team' else None,
            created_at=datetime(2026, 10, 5, 12, 1 - index), score=10 + index,
        ) for index, identity in enumerate(identities)
    ]
    rows = list(reversed(logical_rows)) if case == 'reversed' else list(logical_rows)
    match = MATCH
    status = TournamentStatus.PAUSED if case == 'paused' else TournamentStatus.ONGOING
    format_ = GameFormat.FREE_FOR_ALL if case == 'unsupported' else GameFormat.ONE_V_ONE
    if case == 'confirmed':
        match = replace(match, confirmed_by=USER_ID)
    elif case == 'missing':
        pair = None
    elif case == 'stale_generation':
        pair = replace(pair, generation=2)
    elif case == 'foreign_pointer':
        pair = replace(pair, id=uuid4())
    elif case == 'foreign_match':
        pair = replace(pair, match_id=uuid4())
    elif case == 'foreign_tournament':
        pair = replace(pair, tournament_id=uuid4())
    elif case == 'ended':
        pair = replace(pair, ended_at=NOW)
    elif case == 'wrong_identity':
        pair = replace(pair, side_b=ContestantIdentity(kind=kind, id=uuid4()))
    elif case == 'incomplete':
        rows = rows[:1]
    elif case == 'duplicate_identity':
        rows = [rows[0], replace(rows[0], id=uuid4())]
    elif case == 'foreign_membership':
        rows[1] = replace(rows[1], tournament_match_id=uuid4())
    elif case == 'ambiguous_identity':
        rows[1] = replace(rows[1], participant_id=uuid4(), team_id=uuid4())
    current_pair = (
        replace(pair, side_a=ContestantIdentity(kind=kind, id=uuid4()))
        if case == 'changed_after_projection' else pair
    )
    with collaborators(status=status) as c, app.test_request_context('/'):
        g.user = SimpleNamespace(id=USER_ID, authenticated=True, has_permission=lambda p: False)
        g.party = SimpleNamespace(id=PARTY_ID)
        c.tournament.game_format = format_
        c.matches.get_match.return_value = match
        c.matches.get_contestants_for_match.return_value = rows
        c.matches.get_comments_from_match.return_value = []
        c.matches.get_user_match_role.return_value = MatchUserRole(
            contestant=None, is_loser=False, can_confirm=False, can_submit=False,
        )
        c.repo.get_match_pairing.return_value = current_pair
        with (
            patch('byceps.services.lan_tournament.lan_tournament_view_helpers.tournament_repository.get_match_pairings_for_matches', return_value={MATCH_ID: pair} if pair else {}),
            patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
            patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
            patch(f'{_V}.user_service'),
            patch(f'{_V}.may_administrate_tournament', return_value=False),
            patch(f'{_V}.get_permissions_for_user', return_value=set()),
            patch(f'{_V}.tournament_orga_service.is_orga_for_tournament', return_value=False),
            patch(f'{_V}.tournament_readiness_authorization_service.get_user_readiness_sides', return_value=Ok(frozenset({MatchSide.A}))) as authority,
        ):
            context = views.view_match.__wrapped__(str(MATCH_ID))
    mapping = context['readiness_contestants_by_side']
    valid = case in {'normal', 'reversed', 'paused', 'confirmed'}
    if valid:
        assert mapping == {MatchSide.A: logical_rows[0], MatchSide.B: logical_rows[1]}
        assert mapping[MatchSide.A] is logical_rows[0]
        assert mapping[MatchSide.B] is logical_rows[1]
    else:
        assert mapping == {}
        assert context['readiness_sides'] == set()
        assert not context['readiness_controls_enabled']
    assert context['contestants'] is rows
    assert context['contestants'] == rows
    assert [row.score for row in context['contestants']] == [row.score for row in rows]
    if case in {'normal', 'reversed'}:
        authority.assert_called_once_with(TOURNAMENT_ID, current_pair, USER_ID)
    else:
        authority.assert_not_called()
    c.repo.commit_session.assert_not_called()
    c.repo.flush_session.assert_not_called()
    c.service.claim_ready_flush.assert_not_called()


@pytest.mark.parametrize('role,sides', [
    ('solo', {MatchSide.A}), ('captain', {MatchSide.B}),
    ('member', set()), ('outsider', set()),
    ('scoped_orga', set(MatchSide)), ('global_orga', set(MatchSide)),
])
def test_current_context_typed_sides_token_and_pairing(app, role, sides):
    is_orga = role.endswith('orga')
    with collaborators() as c, app.test_request_context('/'):
        g.user = SimpleNamespace(id=USER_ID, authenticated=True, has_permission=lambda p: False)
        g.party = SimpleNamespace(id=PARTY_ID)
        c.repo.get_match_pairing.return_value = PAIR
        c.matches.get_comments_from_match.return_value = []
        c.matches.get_user_match_role.return_value = MatchUserRole(
            contestant=None, is_loser=False, can_confirm=False, can_submit=False)
        with (
            patch('byceps.services.lan_tournament.lan_tournament_view_helpers.tournament_repository.get_match_pairings_for_matches', return_value={MATCH_ID: PAIR}),
            patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
            patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
            patch(f'{_V}.user_service'),
            patch(f'{_V}.may_administrate_tournament', return_value=is_orga),
            patch(f'{_V}.get_permissions_for_user', return_value={'lan_tournament.administrate'} if role == 'global_orga' else set()),
            patch(f'{_V}.tournament_orga_service.is_orga_for_tournament', return_value=role == 'scoped_orga'),
            patch(f'{_V}.tournament_domain_service.game_format_for_phase', return_value=GameFormat.ONE_V_ONE),
            patch(f'{_V}.tournament_readiness_authorization_service.get_user_readiness_sides', return_value=Ok(frozenset(sides))) as authority,
        ):
            context = views.view_match.__wrapped__(str(MATCH_ID))
    assert context['readiness_sides'] == sides
    assert all(isinstance(side, MatchSide) for side in context['readiness_sides'])
    assert context['effective_match_format'] == GameFormat.ONE_V_ONE
    assert len(context['readiness_csrf_token']) == 43
    assert context['readiness'].pairing_id == PAIR.id
    assert context['claim_form'].expected_pairing_generation.data == 3
    assert context['revoke_form'].expected_readiness_revision.data == 7
    assert context['readiness_controls_enabled'] == bool(sides)
    authority.assert_called_once_with(TOURNAMENT_ID, PAIR, USER_ID)
    c.repo.commit_session.assert_not_called()
    c.repo.flush_session.assert_not_called()
    c.service.claim_ready_flush.assert_not_called()


@pytest.mark.parametrize('role', ['outsider', 'scoped_orga', 'global_orga'])
def test_view_match_context_has_no_history(app, role):
    with collaborators() as c, app.test_request_context('/'):
        g.user = SimpleNamespace(id=USER_ID, authenticated=True, has_permission=lambda p: False)
        g.party = SimpleNamespace(id=PARTY_ID)
        c.repo.get_match_pairing.return_value = PAIR
        c.matches.get_comments_from_match.return_value = []
        c.matches.get_user_match_role.return_value = MatchUserRole(
            contestant=None, is_loser=False, can_confirm=False, can_submit=False)
        with (
            patch('byceps.services.lan_tournament.lan_tournament_view_helpers.tournament_repository.get_match_pairings_for_matches', return_value={MATCH_ID: PAIR}),
            patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
            patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
            patch(f'{_V}.user_service'),
            patch(f'{_V}.may_administrate_tournament', return_value=role.endswith('orga')),
            patch(f'{_V}.get_permissions_for_user', return_value={'lan_tournament.administrate'} if role == 'global_orga' else set()),
            patch(f'{_V}.tournament_orga_service.is_orga_for_tournament', return_value=role == 'scoped_orga'),
            patch(f'{_V}.tournament_domain_service.game_format_for_phase', return_value=GameFormat.ONE_V_ONE),
            patch(f'{_V}.tournament_readiness_authorization_service.get_user_readiness_sides', return_value=Ok(frozenset())),
        ):
            context = views.view_match.__wrapped__(str(MATCH_ID))
    assert context['is_orga'] is role.endswith('orga')
    assert not {key for key in context if 'history' in key}
    assert context['match'] is MATCH
    c.repo.get_readiness_history.assert_not_called()


@pytest.mark.parametrize('status,format_,pair,confirmed', [
    (TournamentStatus.PAUSED, GameFormat.ONE_V_ONE, PAIR, False),
    (TournamentStatus.COMPLETED, GameFormat.ONE_V_ONE, PAIR, False),
    (TournamentStatus.CANCELLED, GameFormat.ONE_V_ONE, PAIR, False),
    (TournamentStatus.ONGOING, GameFormat.FREE_FOR_ALL, PAIR, False),
    (TournamentStatus.ONGOING, GameFormat.ONE_V_ONE, None, False),
    (TournamentStatus.ONGOING, GameFormat.ONE_V_ONE, replace(PAIR, generation=2), False),
    (TournamentStatus.ONGOING, GameFormat.ONE_V_ONE, PAIR, True),
])
def test_context_capability_never_overrides_lifecycle_format_or_fresh_pair(app, status, format_, pair, confirmed):
    with collaborators(status=status) as c, app.test_request_context('/'):
        g.user = SimpleNamespace(id=USER_ID, authenticated=True, has_permission=lambda p: False)
        g.party = SimpleNamespace(id=PARTY_ID)
        c.matches.get_match.return_value = replace(MATCH, confirmed_by=USER_ID if confirmed else None)
        c.tournament.game_format = format_
        c.repo.get_match_pairing.return_value = pair
        c.matches.get_comments_from_match.return_value = []
        c.matches.get_user_match_role.return_value = MatchUserRole(
            contestant=None, is_loser=False, can_confirm=False, can_submit=False)
        with (
            patch('byceps.services.lan_tournament.lan_tournament_view_helpers.tournament_repository.get_match_pairings_for_matches', return_value={MATCH_ID: pair} if pair else {}),
            patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
            patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
            patch(f'{_V}.user_service'),
            patch(f'{_V}.may_administrate_tournament', return_value=False),
            patch(f'{_V}.get_permissions_for_user', return_value=set()),
            patch(f'{_V}.tournament_orga_service.is_orga_for_tournament', return_value=False),
            patch(f'{_V}.tournament_readiness_authorization_service.get_user_readiness_sides', return_value=Ok(frozenset(MatchSide))) as authority,
        ):
            context = views.view_match.__wrapped__(str(MATCH_ID))
    assert not context['readiness_controls_enabled']
    assert not context['readiness'].mutation_available
    assert context['readiness_sides'] == set()
    authority.assert_not_called()
    assert context['effective_match_format'] == format_
    if status in {TournamentStatus.COMPLETED, TournamentStatus.CANCELLED}:
        assert context['readiness'].outcome == status.name.lower()
    c.repo.commit_session.assert_not_called()
    c.repo.flush_session.assert_not_called()
