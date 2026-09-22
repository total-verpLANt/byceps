"""
tests.unit.services.lan_tournament.test_site_view_match_orga_context
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest

from byceps.services.lan_tournament.models.game_format import GameFormat
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_match import (
    CorrectionCase,
    TournamentMatch,
    TournamentMatchID,
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
    return Flask(__name__)


def _make_tournament(status, game_format=GameFormat.ONE_V_ONE):
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.party_id = generate_uuid()
    t.tournament_status = status
    t.game_format = game_format
    return t


def _make_match(match_id=MATCH_ID, *, confirmed=True):
    m = MagicMock(spec=TournamentMatch)
    m.id = match_id
    m.tournament_id = TOURNAMENT_ID
    m.confirmed_by = generate_uuid() if confirmed else None
    return m


def _contestant():
    c = MagicMock()
    c.participant_id = generate_uuid()
    c.team_id = None
    return c


@contextmanager
def _patched(app, tournament, match, contestants):
    with (
        app.app_context(),
        patch(f'{_V}.tournament_match_service') as match_svc,
        patch(f'{_V}._get_tournament_or_404', return_value=tournament),
        patch(f'{_V}.build_contestant_name_lookups', return_value=({}, {})),
        patch(f'{_V}.build_hover_lookups', return_value=({}, {})),
        patch(f'{_V}.user_service'),
        patch(f'{_V}.may_administrate_tournament', return_value=True),
        patch(f'{_V}.g') as g,
    ):
        g.user.authenticated = False
        match_svc.get_match.return_value = match
        match_svc.get_contestants_for_match.return_value = contestants
        match_svc.get_comments_from_match.return_value = []
        match_svc.ffa_round_already_advanced.return_value = False
        yield match_svc


def _view_match(app):
    from byceps.services.lan_tournament.blueprints.site import views

    with app.test_request_context('/'):
        return views.view_match.__wrapped__(str(MATCH_ID))


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
