"""
tests.unit.services.lan_tournament.test_site_view_match_orga_context
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import pytest

from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.match_readiness import (
    MatchReadiness,
    ReadinessDisplayStatus,
)
from byceps.services.lan_tournament.models.tournament import (
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    TournamentMatch,
    TournamentMatchID,
)
from byceps.services.lan_tournament.models.tournament_match_to_contestant import (
    TournamentMatchToContestant,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Ok

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
MATCH_ID = TournamentMatchID(generate_uuid())

_V = 'byceps.services.lan_tournament.blueprints.site.views'


@pytest.fixture(scope='module')
def app():
    app = Flask(__name__)
    app.config.update(LOCALE='en', SECRET_KEY='unit-test-only')
    return app


def _make_tournament(status, game_format=GameFormat.ONE_V_ONE):
    return SimpleNamespace(
        id=TOURNAMENT_ID,
        party_id=generate_uuid(),
        tournament_status=status,
        game_format=game_format,
        has_playoffs=False,
    )


def _make_match(match_id=MATCH_ID, *, confirmed=True):
    return TournamentMatch(
        id=match_id,
        tournament_id=TOURNAMENT_ID,
        group_order=None,
        match_order=1,
        round=1,
        next_match_id=None,
        confirmed_by=generate_uuid() if confirmed else None,
        created_at=datetime(2026, 10, 5, 12),
    )


def _contestant():
    return TournamentMatchToContestant(
        id=generate_uuid(),
        tournament_match_id=MATCH_ID,
        participant_id=generate_uuid(),
        team_id=None,
        score=None,
        created_at=datetime(2026, 10, 5, 12),
    )


@contextmanager
def _patched(app, tournament, match, contestants, *, may_administrate=True):
    # The canonical batch read is an external collaborator, not correction
    # policy. These confirmed/no-pair fixtures expose no readiness capability.
    readiness = MatchReadiness(
        match_id=match.id,
        status=ReadinessDisplayStatus.OPEN,
        ready_sides=(),
        pairing_generation=match.pairing_generation,
        readiness_revision=match.readiness_revision,
        outcome='confirmed' if match.confirmed_by else None,
    )
    with (
        app.app_context(),
        patch(f'{_V}.tournament_match_service.get_match', return_value=match),
        patch(
            f'{_V}.tournament_match_service.get_contestants_for_match',
            return_value=contestants,
        ),
        patch(f'{_V}.tournament_match_service.get_comments_from_match', return_value=[]),
        patch(f'{_V}.tournament_match_service.classify_result_correction') as classify,
        patch(f'{_V}.tournament_match_service.get_matches_by_ids') as get_matches,
        patch(
            f'{_V}.tournament_match_service.ffa_round_already_advanced',
            return_value=False,
        ) as ffa_advanced,
        patch(f'{_V}._get_tournament_or_404', return_value=tournament),
        patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
        patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
        patch(f'{_V}.user_service.get_users_indexed_by_id', return_value={}),
        patch(
            f'{_V}.build_match_readiness_projections',
            return_value={match.id: readiness},
        ) as projections,
        patch(
            f'{_V}.tournament_domain_service.game_format_for_phase',
            return_value=tournament.game_format,
        ) as effective_format,
        patch(f'{_V}.get_readiness_csrf_token') as token,
        patch(f'{_V}.may_administrate_tournament', return_value=may_administrate),
        patch(f'{_V}.g') as g,
    ):
        g.user.authenticated = False
        yield SimpleNamespace(
            classify_result_correction=classify,
            get_matches_by_ids=get_matches,
            ffa_round_already_advanced=ffa_advanced,
        )
        projections.assert_called_once_with(tournament, [match], {match.id: contestants})
        effective_format.assert_called_once_with(tournament, match.phase)
        token.assert_not_called()  # Anonymous context must not mint session tokens.


def _view_match(app):
    from byceps.services.lan_tournament.blueprints.site import views

    with app.test_request_context('/'):
        context = views.view_match.__wrapped__(str(MATCH_ID))
    assert context['max_match_score'] == 999_999_999
    assert context['readiness_controls_enabled'] is False
    assert context['readiness_csrf_token'] is None
    assert context['claim_form'].expected_pairing_generation.data == 0
    assert context['revoke_form'].expected_readiness_revision.data == 0
    return context


def test_critical_correction_binds_ack_to_matches_shown(app):
    downstream = _make_match(TournamentMatchID(generate_uuid()))

    with _patched(
        app,
        _make_tournament(TournamentStatus.ONGOING),
        _make_match(),
        [_contestant(), _contestant()],
    ) as match_svc:
        match_svc.classify_result_correction.return_value = Ok(
            (CorrectionCase.CONFIRMED_DOWNSTREAM, [downstream.id])
        )
        match_svc.get_matches_by_ids.return_value = [downstream]

        context = _view_match(app)

    assert context['ack_match_ids'] == [str(downstream.id)]
    assert context['results_editable'] is True


@pytest.mark.parametrize(
    'status',
    # DRAFT is hidden from the site entirely (404).
    [
        s
        for s in TournamentStatus
        if s not in (TournamentStatus.ONGOING, TournamentStatus.DRAFT)
    ],
    ids=lambda s: s.name,
)
def test_no_correction_classified_unless_ongoing(app, status):
    with _patched(
        app,
        _make_tournament(status),
        _make_match(),
        [_contestant(), _contestant()],
    ) as match_svc:
        context = _view_match(app)

    match_svc.classify_result_correction.assert_not_called()
    assert context['results_editable'] is False


def test_walkover_is_flagged_and_not_classified(app):
    with _patched(
        app,
        _make_tournament(TournamentStatus.ONGOING),
        _make_match(),
        [_contestant()],
    ) as match_svc:
        context = _view_match(app)

    match_svc.classify_result_correction.assert_not_called()
    assert context['is_walkover'] is True


def test_consumed_ffa_result_is_flagged_and_not_classified(app):
    with _patched(
        app,
        _make_tournament(
            TournamentStatus.ONGOING, game_format=GameFormat.FREE_FOR_ALL
        ),
        _make_match(),
        [_contestant(), _contestant()],
    ) as match_svc:
        match_svc.ffa_round_already_advanced.return_value = True

        context = _view_match(app)

    match_svc.classify_result_correction.assert_not_called()
    assert context['is_ffa'] is True
    assert context['ffa_result_consumed'] is True


def test_correction_classification_requires_administration_scope(app):
    with _patched(
        app,
        _make_tournament(TournamentStatus.ONGOING),
        _make_match(),
        [_contestant(), _contestant()],
        may_administrate=False,
    ) as match_svc:
        context = _view_match(app)

    match_svc.classify_result_correction.assert_not_called()
    assert context['may_administrate'] is False
    assert context['ack_match_ids'] == []
