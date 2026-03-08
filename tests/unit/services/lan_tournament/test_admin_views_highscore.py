"""
tests.unit.services.lan_tournament.test_admin_views_highscore
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for the highscore admin views: leaderboard rendering,
score submission, negative-score rejection, wrong-mode rejection,
and bulk score deletion.
"""

from contextlib import contextmanager
from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from byceps.services.lan_tournament.models.contestant_type import (
    ContestantType,
)
from byceps.services.lan_tournament.models.score_ordering import (
    ScoreOrdering,
)
from byceps.services.lan_tournament.models.score_submission import (
    ScoreSubmission,
    ScoreSubmissionID,
)
from byceps.services.lan_tournament.models.tournament import (
    Tournament,
    TournamentID,
)
from byceps.services.lan_tournament.models.tournament_mode import (
    TournamentMode,
)
from byceps.services.lan_tournament.models.tournament_participant import (
    TournamentParticipantID,
)
from byceps.services.lan_tournament.models.tournament_status import (
    TournamentStatus,
)
from byceps.util.result import Err, Ok

from tests.helpers import generate_uuid


TOURNAMENT_ID = TournamentID(generate_uuid())
TOURNAMENT_ID_STR = str(TOURNAMENT_ID)
PARTY_ID_STR = str(generate_uuid())
PARTICIPANT_ID = TournamentParticipantID(generate_uuid())
USER_ID = generate_uuid()

_V = 'byceps.services.lan_tournament.blueprints.admin.views'


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


@pytest.fixture(scope='module')
def app():
    """Minimal Flask app with LOCALE config for form instantiation."""
    a = Flask(__name__)
    a.config['TESTING'] = True
    a.config['LOCALE'] = 'en'
    return a


def _make_tournament(
    mode: TournamentMode,
    contestant_type: ContestantType = ContestantType.SOLO,
) -> MagicMock:
    t = MagicMock(spec=Tournament)
    t.id = TOURNAMENT_ID
    t.tournament_mode = mode
    t.tournament_status = TournamentStatus.ONGOING
    t.party_id = PARTY_ID_STR
    t.contestant_type = contestant_type
    t.score_ordering = ScoreOrdering.HIGHER_IS_BETTER
    return t


def _make_party() -> MagicMock:
    p = MagicMock()
    p.id = PARTY_ID_STR
    return p


def _make_participant() -> MagicMock:
    p = MagicMock()
    p.id = PARTICIPANT_ID
    p.user_id = USER_ID
    p.removed_at = None
    return p


def _make_user() -> MagicMock:
    u = MagicMock()
    u.id = USER_ID
    u.screen_name = 'TestPlayer'
    return u


def _make_submission(
    score: int = 100,
) -> ScoreSubmission:
    return ScoreSubmission(
        id=ScoreSubmissionID(generate_uuid()),
        tournament_id=TOURNAMENT_ID,
        participant_id=PARTICIPANT_ID,
        team_id=None,
        score=score,
        submitted_at=datetime.now(UTC),
        submitted_by=None,
        is_official=True,
        note=None,
    )


@contextmanager
def _patched_highscore_view():
    """Patch view dependencies for highscore routes."""
    with (
        patch(
            f'{_V}.gettext',
            side_effect=lambda msg, **kw: msg,
        ),
        patch(f'{_V}.flash_error') as mock_flash_error,
        patch(f'{_V}.flash_success') as mock_flash_success,
        patch(f'{_V}.redirect_to') as mock_redirect_to,
        patch(f'{_V}.party_service') as mock_party_svc,
        patch(f'{_V}.tournament_score_service') as mock_score_svc,
        patch(f'{_V}.tournament_participant_service') as mock_participant_svc,
        patch(f'{_V}.tournament_team_service') as mock_team_svc,
        patch(f'{_V}.user_service') as mock_user_svc,
        patch(f'{_V}._get_tournament_or_404') as mock_get_tournament,
    ):
        mock_party_svc.get_party.return_value = _make_party()
        participant = _make_participant()
        mock_participant_svc.get_participants_for_tournament.return_value = [
            participant
        ]
        user = _make_user()
        mock_user_svc.get_users_indexed_by_id.return_value = {USER_ID: user}
        mock_team_svc.get_teams_for_tournament.return_value = []
        yield {
            'flash_error': mock_flash_error,
            'flash_success': mock_flash_success,
            'redirect_to': mock_redirect_to,
            'score_svc': mock_score_svc,
            'get_tournament': mock_get_tournament,
            'participant_svc': mock_participant_svc,
            'team_svc': mock_team_svc,
            'user_svc': mock_user_svc,
        }


def _call_highscore(app):
    """Call the raw highscore GET view."""
    from byceps.services.lan_tournament.blueprints.admin import (
        views,
    )

    raw_fn = views.highscore.__wrapped__.__wrapped__

    with app.test_request_context('/'):
        return raw_fn(TOURNAMENT_ID_STR)


def _call_highscore_submit(app, form_data):
    """Call the raw highscore_submit POST view."""
    from byceps.services.lan_tournament.blueprints.admin import (
        views,
    )

    raw_fn = views.highscore_submit.__wrapped__

    with app.test_request_context('/', method='POST', data=form_data):
        with patch(f'{_V}.g') as mock_g:
            mock_g.user.id = USER_ID
            return raw_fn(TOURNAMENT_ID_STR)


def _call_highscore_delete_all(app):
    """Call the raw highscore_delete_all POST view."""
    from byceps.services.lan_tournament.blueprints.admin import (
        views,
    )

    raw_fn = views.highscore_delete_all.__wrapped__

    with app.test_request_context('/', method='POST'):
        return raw_fn(TOURNAMENT_ID_STR)


# ------------------------------------------------------------------ #
# GET leaderboard renders
# ------------------------------------------------------------------ #


def test_highscore_leaderboard_renders(app):
    """GET highscore returns leaderboard data."""
    with _patched_highscore_view() as mocks:
        tournament = _make_tournament(
            TournamentMode.HIGHSCORE,
        )
        mocks['get_tournament'].return_value = tournament

        submission = _make_submission(score=200)
        mocks['score_svc'].get_leaderboard.return_value = Ok([submission])

        result = _call_highscore(app)

    assert result['leaderboard'] == [submission]
    assert result['tournament'] is tournament
    assert 'form' in result


# ------------------------------------------------------------------ #
# POST submit valid score
# ------------------------------------------------------------------ #


def test_highscore_submit_valid_score(app):
    """POST with valid score creates submission and flashes success."""
    with _patched_highscore_view() as mocks:
        tournament = _make_tournament(
            TournamentMode.HIGHSCORE,
        )
        mocks['get_tournament'].return_value = tournament

        submission = _make_submission(score=150)
        mocks['score_svc'].submit_score.return_value = Ok(submission)

        _call_highscore_submit(
            app,
            {
                'contestant': str(PARTICIPANT_ID),
                'score': '150',
                'note': '',
            },
        )

    mocks['score_svc'].submit_score.assert_called_once()
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()


# ------------------------------------------------------------------ #
# POST negative score rejected
# ------------------------------------------------------------------ #


def test_highscore_submit_negative_score_rejected(app):
    """POST with negative score fails validation, flashes error."""
    with _patched_highscore_view() as mocks:
        tournament = _make_tournament(
            TournamentMode.HIGHSCORE,
        )
        mocks['get_tournament'].return_value = tournament

        _call_highscore_submit(
            app,
            {
                'contestant': str(PARTICIPANT_ID),
                'score': '-5',
                'note': '',
            },
        )

    # Negative score should fail form validation
    # (NumberRange(min=0)) so submit_score is never called.
    mocks['score_svc'].submit_score.assert_not_called()
    mocks['flash_error'].assert_called_once()


# ------------------------------------------------------------------ #
# POST non-highscore mode rejected
# ------------------------------------------------------------------ #


def test_highscore_submit_non_highscore_mode_rejected(app):
    """POST to a non-HIGHSCORE tournament returns error from service."""
    with _patched_highscore_view() as mocks:
        tournament = _make_tournament(
            TournamentMode.HIGHSCORE,
        )
        mocks['get_tournament'].return_value = tournament

        mocks['score_svc'].submit_score.return_value = Err(
            'Tournament mode must be HIGHSCORE to submit scores.'
        )

        _call_highscore_submit(
            app,
            {
                'contestant': str(PARTICIPANT_ID),
                'score': '100',
                'note': '',
            },
        )

    mocks['flash_error'].assert_called_once()
    mocks['flash_success'].assert_not_called()


# ------------------------------------------------------------------ #
# POST delete all scores
# ------------------------------------------------------------------ #


def test_highscore_delete_all_scores(app):
    """Admin can clear all scores, success flash shown."""
    with _patched_highscore_view() as mocks:
        tournament = _make_tournament(
            TournamentMode.HIGHSCORE,
        )
        mocks['get_tournament'].return_value = tournament
        mocks['score_svc'].delete_scores_for_tournament.return_value = Ok(None)

        _call_highscore_delete_all(app)

    mocks['score_svc'].delete_scores_for_tournament.assert_called_once_with(
        TOURNAMENT_ID,
    )
    mocks['flash_success'].assert_called_once()
    mocks['flash_error'].assert_not_called()
